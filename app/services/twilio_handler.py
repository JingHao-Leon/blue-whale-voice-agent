"""
Twilio Media Streams handler.

Twilio's voice API has two ways to play audio into a call:
  1. <Say>/<Play>  - server-driven, no streaming
  2. <Connect><Stream> - bidirectional WebSocket

We use #2 because it lets us:
  - Get the caller's audio in real time (μ-law 8 kHz)
  - Push TTS audio back as it generates
  - End the call programmatically

Wire format we exchange with Twilio:
  - inbound events:  connected → start → media (payload=b64 μ-law) → stop
  - outbound events: media (payload=b64 μ-law)  or  mark (for latency tracking)
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import WebSocket

from app.agent import VisitorLLMAgent
from app.config import get_settings
from app.database import find_returning_visitor, save_conversation, save_visit
from app.logging import logger
from app.schemas import VisitorInfo
from app.services import stt, tts, wechat


@dataclass
class TwilioMeta:
    """Resolved from the `start` event payload."""

    stream_sid: str
    call_sid: str
    from_number: str
    to_number: str


class TwilioCall:
    """One in-flight call. Lives only as long as the WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.meta: Optional[TwilioMeta] = None
        self.agent: Optional[VisitorLLMAgent] = None
        self.transcriber: Optional[stt.Transcriber] = None
        self.ended = False
        self._greeted = False
        self._ended_by_silence = False
        self._agent_speaking = False
        self._first_user_audio_at: Optional[float] = None
        self._first_wechat_at: Optional[float] = None
        self._wechat_ok = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Top-level loop: process Twilio events until the call ends."""
        try:
            async for raw in self.ws.iter_text():
                if self.ended:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Bad JSON from Twilio: {}", raw[:200])
                    continue
                await self._handle_event(msg)
        except Exception:  # noqa: BLE001
            logger.exception("TwilioCall.run crashed")
        finally:
            await self._cleanup()

    async def _handle_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "connected":
            logger.info("Twilio connected (protocol={})", msg.get("protocol", {}).get("name"))
        elif event == "start":
            await self._on_start(msg)
        elif event == "media":
            await self._on_media(msg)
        elif event == "mark":
            await self._on_mark(msg)
        elif event == "stop":
            logger.info("Twilio stop event")
            self.ended = True
        else:
            logger.debug("Unknown Twilio event: {}", event)

    async def _on_start(self, msg: dict) -> None:
        start = msg.get("start", {})
        self.meta = TwilioMeta(
            stream_sid=start.get("streamSid", ""),
            call_sid=start.get("callSid", ""),
            from_number=start.get("from", ""),
            to_number=start.get("to", ""),
        )
        logger.info(
            "📞 Call started sid={} from={} to={}",
            self.meta.call_sid,
            self.meta.from_number,
            self.meta.to_number,
        )

        # Build the agent. Detect returning visitor only AFTER we hear a plate
        # (because the plate is the lookup key), so we start with empty history.
        self.agent = VisitorLLMAgent(
            call_sid=self.meta.call_sid,
            visitor_info=VisitorInfo(call_sid=self.meta.call_sid, started_at=datetime.utcnow()),
        )

        # Spin up STT
        try:
            self.transcriber = stt.make_transcriber()
            if hasattr(self.transcriber, "__aenter__"):
                await self.transcriber.__aenter__()
        except Exception as e:  # noqa: BLE001
            logger.error("STT init failed: {}", e)
            await self._speak_and_hangup("抱歉，语音系统暂时不可用，请稍后再拨。")
            return

        # Kick off three concurrent coroutines: STT events, audio pump.
        asyncio.create_task(self._stt_event_loop())
        asyncio.create_task(self._silence_watchdog())

        # Greet the caller. This is the "agent started speaking" timestamp used
        # for the 25 s SLA.
        greeting = (
            f"您好，这里是{get_settings().park_name}，"
            f"请讲一下您的车牌号、来哪家公司、什么事？"
        )
        await self._speak(greeting)

    async def _on_media(self, msg: dict) -> None:
        if self.transcriber is None:
            return
        payload_b64 = msg.get("media", {}).get("payload", "")
        if not payload_b64:
            return
        try:
            audio_bytes = base64.b64decode(payload_b64)
        except Exception:  # noqa: BLE001
            return
        # Twilio sends ~20 ms frames. We forward them straight through.
        self.transcriber.send_audio(audio_bytes)
        if self._first_user_audio_at is None and len(audio_bytes) > 0:
            self._first_user_audio_at = time.perf_counter()

    async def _on_mark(self, msg: dict) -> None:
        # We send mark events after each agent utterance. The "mark" event
        # from Twilio means "audio finished playing" — that's when we know
        # the agent has stopped speaking.
        name = msg.get("mark", {}).get("name", "")
        if name.startswith("agent_end:"):
            self._agent_speaking = False
        if name == "hangup":
            self.ended = True

    # ------------------------------------------------------------------
    # Audio pump
    # ------------------------------------------------------------------

    async def _speak(self, text: str) -> None:
        """Synthesise `text` to μ-law and stream it back to Twilio."""
        if not text.strip() or self.meta is None:
            return
        self._agent_speaking = True
        try:
            first_chunk = True
            async for mulaw in tts.synthesize_streaming_mulaw8k(text):
                if first_chunk:
                    logger.info("TTFB TTS→Twilio: {:.2f}s", time.perf_counter() - (self._first_user_audio_at or time.perf_counter()))
                    first_chunk = False
                await self.ws.send_json(
                    {
                        "event": "media",
                        "streamSid": self.meta.stream_sid,
                        "media": {"payload": base64.b64encode(mulaw).decode("ascii")},
                    }
                )
        finally:
            # Send a mark so we know when Twilio has flushed.
            await self.ws.send_json(
                {
                    "event": "mark",
                    "streamSid": self.meta.stream_sid,
                    "mark": {"name": f"agent_end:{int(time.time() * 1000)}"},
                }
            )

    async def _speak_and_hangup(self, text: str) -> None:
        """Speak, send mark=hangup, then close the WebSocket (Twilio ends the call)."""
        await self._speak(text)
        if self.meta is not None:
            await self.ws.send_json(
                {
                    "event": "mark",
                    "streamSid": self.meta.stream_sid,
                    "mark": {"name": "hangup"},
                }
            )
        self.ended = True

    # ------------------------------------------------------------------
    # STT event loop
    # ------------------------------------------------------------------

    async def _stt_event_loop(self) -> None:
        """Consume STT events and feed the LLM agent."""
        assert self.transcriber is not None
        assert self.agent is not None
        last_final_text = ""
        try:
            async for evt in self.transcriber.events():
                if self.ended:
                    break
                if not evt.text:
                    continue
                # We only act on FINAL transcripts (interim are noise from the
                # LLM's perspective).
                if not evt.is_final:
                    continue
                if evt.text == last_final_text:
                    continue
                last_final_text = evt.text

                # Lazy returning-visitor lookup: the first time we hear a plate
                # number, look it up. If found, mutate the agent context.
                await self._maybe_lookup_returning(evt.text)

                self.agent.push_user_utterance(evt.text)
                reply = await self.agent.step()
                if reply.text_to_speak:
                    await self._speak(reply.text_to_speak)
                if reply.is_final:
                    await self._finalise_call(reply.visitor_info)
                    return
        except Exception:  # noqa: BLE001
            logger.exception("STT event loop crashed")

    # ------------------------------------------------------------------
    # Returning visitor detection
    # ------------------------------------------------------------------

    PLATE_HINT = ("车牌",)

    async def _maybe_lookup_returning(self, utterance: str) -> None:
        """If the utterance looks like a plate and we haven't detected returning yet,
        look up the DB and rebuild the agent with returning context.
        """
        if not self.agent or self.agent.visitor.is_returning or self.agent.visitor.plate:
            return
        candidate = _extract_plate(utterance)
        if not candidate:
            return
        history = await find_returning_visitor(candidate, limit=3)
        if not history:
            return
        # Rebuild the agent with returning context.
        new_info = self.agent.visitor.model_copy(
            update={
                "plate": candidate,
                "is_returning": True,
                "call_history": history,
            }
        )
        history_text = " / ".join(
            f"{h.started_at:%Y-%m-%d} {h.reason or '-'}" for h in history if h.started_at
        )
        logger.info("♻️ Returning visitor: {} (history: {})", candidate, history_text)
        self.agent = VisitorLLMAgent(
            call_sid=self.agent.call_sid,
            visitor_info=new_info,
            returning_history=history_text,
        )

    # ------------------------------------------------------------------
    # Silence watchdog
    # ------------------------------------------------------------------

    async def _silence_watchdog(self) -> None:
        """If the caller is silent for > 8s and we don't have a complete record,
        gently prompt. If 15s still nothing, end the call politely.
        """
        warned = False
        for _ in range(15):  # 15 × 1 s = 15 s
            if self.ended:
                return
            await asyncio.sleep(1.0)
            if self._agent_speaking:
                warned = False
                continue
            if self.agent and self.agent.visitor.is_complete():
                return
            if not warned and self._first_user_audio_at is not None:
                await self._speak("还在吗？")
                warned = True
            elif warned and self._first_user_audio_at is not None:
                # Still nothing after the prompt → give up.
                await self._speak_and_hangup("好的，没听到您回应，请稍后再拨。")
                self._ended_by_silence = True
                return

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    async def _finalise_call(self, info: VisitorInfo) -> None:
        info.ended_at = datetime_now()
        info.duration_seconds = (info.ended_at - (info.started_at or info.ended_at)).total_seconds()
        info.call_history = info.call_history  # already populated

        # Persist (best-effort, don't fail the call)
        try:
            await save_visit(info)
        except Exception:  # noqa: BLE001
            logger.exception("save_visit failed")
        try:
            await save_conversation(
                call_sid=info.call_sid,
                transcript=self.agent.transcript() if self.agent else "",
                final_card=info.to_wechat_card(),
                plate=info.plate,
            )
        except Exception:  # noqa: BLE001
            logger.exception("save_conversation failed")

        # Send to WeChat. This is the latency SLA — first byte to WeChat must
        # be within 25 s of agent first speaking.
        try:
            t0 = time.perf_counter()
            await wechat.send_visitor_card(info)
            self._wechat_ok = True
            self._first_wechat_at = t0
            logger.info("✅ WeChat card sent in {:.2f}s", time.perf_counter() - t0)
        except Exception as e:  # noqa: BLE001
            logger.exception("WeChat send failed: {}", e)

        # Speak the final confirmation. Use a friendly summary.
        summary = _build_summary(info)
        await self._speak_and_hangup(summary)

        # Log SLA
        if self._first_user_audio_at is not None and self._first_wechat_at is not None:
            sla = self._first_wechat_at - self._first_user_audio_at
            logger.info("⏱️  end-to-end (caller first audio → WeChat sent) ≈ {:.2f}s (target ≤25s)", sla)

    async def _cleanup(self) -> None:
        try:
            if self.transcriber is not None:
                await self.transcriber.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def datetime_now():
    return datetime.utcnow()


_PLATE_RE = re.compile(
    r"(?:^|\D)([\u4e00-\u9fa5][A-Z][A-Z0-9]{5,7}|[A-Z]{1,2}[A-Z0-9]{5,7})(?:\D|$)"
)


def _extract_plate(text: str) -> Optional[str]:
    text_u = text.upper()
    m = _PLATE_RE.search(text_u)
    if m:
        return m.group(1)
    # Try a looser pattern: any 6+ alnum sequence with a leading letter
    for token in re.findall(r"[A-Z0-9\u4e00-\u9fa5]{6,8}", text_u):
        if re.search(r"[A-Z]", token) and re.search(r"\d", token):
            return token
    return None


def _build_summary(info: VisitorInfo) -> str:
    plate = info.plate or "您的车辆"
    reason = info.reason or "到访"
    company = info.company or get_settings().default_company
    return f"好的，{plate}，{company}{reason}，已通知门卫，请稍等放行。"
