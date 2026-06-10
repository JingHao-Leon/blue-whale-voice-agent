"""
Browser-based voice session for live testing without telephony.

Bypasses Twilio Media Streams. Browser captures mic via getUserMedia,
streams PCM frames over WebSocket, server runs the SAME STT/LLM/TTS
pipeline used by the Twilio path.

Wire protocol (single WebSocket, hybrid text+binary):

  Browser → server
    {"type":"config","sample_rate":16000}   -- optional, must precede audio
    binary frame: int16 little-endian PCM, mono, at the configured sample rate
    {"type":"end"}                          -- force STT to emit final transcript
    {"type":"stop"}                         -- close session

  Server → browser
    {"type":"ready"}                        -- STT/TTS warmed up
    {"type":"transcript","text":"…","is_final":true,"speech_final":true}
    {"type":"agent_text","text":"…"}        -- LLM response (after tool calls resolved)
    {"type":"agent_audio_start"}
    binary frame: int16 little-endian PCM, mono, 16 kHz   (TTS chunks)
    {"type":"agent_audio_end"}
    {"type":"wechat_ok","ok":true|false}    -- only on completion
    {"type":"done","summary":"…"}           -- session complete
    {"type":"error","message":"…"}

This is intentionally a thin transport so it can be re-pointed at any
audio-capable transport (browser, mobile, custom client) without
touching the agent core.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import WebSocket

from app.agent import VisitorLLMAgent
from app.config import get_settings
from app.database import save_visit
from app.logging import logger
from app.schemas import VisitorInfo
from app.services import stt, tts, wechat


OUT_PCM_RATE = 16_000  # what we stream TTS audio back at (browser-friendly)


# ---------------------------------------------------------------------------
# TTS cache — short phrases that repeat a lot. Pre-synthesised at module load
# so the first turn doesn't pay a 1-2s TTS round trip.
# ---------------------------------------------------------------------------

_TTS_CACHE: dict[str, bytes] = {}
_TTS_CACHE_LOCK = asyncio.Lock()
_TTS_CACHE_WARMED = False


async def _warm_tts_cache() -> None:
    """Synthesise the common short phrases ahead of time.

    Safe to call multiple times — guarded by a module-level flag.
    Failures are swallowed; cache stays empty and we fall through to live
    synthesis on each call.
    """
    global _TTS_CACHE_WARMED
    if _TTS_CACHE_WARMED:
        return
    async with _TTS_CACHE_LOCK:
        if _TTS_CACHE_WARMED:
            return
        phrases = [
            # 门卫用语 ending — "请稍候，门卫会放行" 而**不是**"欢迎光临"
            # (欢迎光临是酒店前台用语，不是园区门卫用语)
            "好的，已通知门卫，请您稍候，门卫会放行。",
            "请讲。",
            "请再说一遍。",
            "好的。",
            # Variable-free closing messages — agent.py's _pick_closing()
            # picks one of these on final turns so we hit the cache instead
            # of paying a 2-3 s TTS round-trip. Pre-synthesised at boot.
            "好的，已通知门卫，请您稍候放行。",
            "齐活儿了，请您稍候，门卫会放行。",
            "OK 收到，已通知门卫，请您稍候。",
            "好的，已通知门卫，请稍候放行。",
            "行嘞，已通知门卫，请您稍等，门卫马上放行。",
        ]
        # Parallelise so a single slow TTS call doesn't block the boot for
        # 25s × N phrases. Each call has a 5s hard cap (CosyVoice WS
        # 23s default timeout is way too long for a non-essential warmup).
        async def _one(p: str) -> tuple[str, bytes]:
            try:
                return p, await asyncio.wait_for(_synth_pcm16k(p), timeout=5.0)
            except Exception:  # noqa: BLE001
                return p, b""

        results = await asyncio.gather(*[_one(p) for p in phrases])
        for p, pcm in results:
            if pcm:
                _TTS_CACHE[p] = pcm
        _TTS_CACHE_WARMED = True
        logger.info("🌐 TTS cache warmed: {} phrases", len(_TTS_CACHE))


async def _synth_pcm16k(text: str) -> bytes:
    """Synthesise text → PCM 16 kHz mono s16le. Returns empty bytes on failure.

    Uses the CosyVoice WebSocket TTS path: 24 kHz PCM from the model, then
    a single audioop ratecv down to 16 kHz. No ffmpeg, no WAV roundtrip.
    """
    from app.services import tts as _tts
    return await _tts.synthesize_to_pcm16k(text)


@dataclass
class _State:
    sample_rate: int = 16_000
    in_pcm_buf: bytes = b""           # accumulator for chunked browser frames
    last_partial: str = ""


class BrowserSession:
    """One in-flight browser session. Lives as long as the WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.state = _State()
        self.transcriber = None
        self.agent: Optional[VisitorLLMAgent] = None
        self._stt_task: Optional[asyncio.Task] = None
        self._closed = False
        # Server-side VAD: counts consecutive quiet 80 ms frames. When it
        # hits SILENCE_FRAMES, we ask the STT to emit a final transcript
        # for the current turn (faster than waiting for Bailian's 23 s
        # server-side silence timeout). `_speech_frames` counts active
        # (non-silent) frames — VAD can only fire after at least
        # MIN_SPEECH_FRAMES of speech, so a brief 1-2 word utterance
        # never gets mis-fired as "end of turn".
        self._silent_frames = 0
        self._speech_frames = 0
        # Silence-nudge watchdog: after the agent finishes speaking, if the
        # user goes silent for too long without filling in all required
        # fields, periodically re-prompt them with what they're still
        # missing. Started in _handle_turn after a non-final reply; reset
        # by any user transcript (in _drain_stt).
        self._silence_watchdog_task: Optional[asyncio.Task] = None
        self._nudge_count = 0
        self._last_user_activity: float = time.time()

    # ------------------------------------------------------------------
    # WebSocket driver
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self.ws.accept()
        logger.info("🌐 Browser session opened")
        try:
            # Warm up STT + pre-synth greeting (greeting is already cached on
            # self by _init so we can stream it instantly below)
            await self._init()
            await self.ws.send_json({"type": "ready"})

            # Greeting — send pre-synthesised audio with zero TTS wait.
            # Falls back to live synth if pre-synth failed for any reason.
            await self.ws.send_json({"type": "agent_text", "text": self._greeting_text})
            await self.ws.send_json({"type": "agent_audio_start"})
            try:
                pcm = self._greeting_pcm or await _synth_pcm16k(self._greeting_text)
                if pcm:
                    frame = int(OUT_PCM_RATE * 80 / 1000) * 2
                    for off in range(0, len(pcm), frame):
                        await self.ws.send_bytes(pcm[off:off + frame])
            finally:
                await self.ws.send_json({"type": "agent_audio_end"})

            # Main loop: read frames, hand binary audio to STT, JSON to control
            while not self._closed:
                msg = await self.ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    await self._on_pcm(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await self._on_json(msg["text"])
        except Exception as e:  # noqa: BLE001
            logger.exception("Browser session error: {}", e)
            try:
                await self.ws.send_json({"type": "error", "message": str(e)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------
    # Init / shutdown
    # ------------------------------------------------------------------

    async def _init(self) -> None:
        # STT
        try:
            self.transcriber = stt.make_transcriber()
            if hasattr(self.transcriber, "__aenter__"):
                await self.transcriber.__aenter__()
        except Exception as e:  # noqa: BLE001
            logger.error("STT init failed: {}", e)
            await self.ws.send_json({"type": "error", "message": f"STT init: {e}"})
            return

        # LLM agent (no call_sid context; browser has no Twilio call)
        self.agent = VisitorLLMAgent(
            call_sid="browser",
            visitor_info=VisitorInfo(call_sid="browser", started_at=__import__("datetime").datetime.utcnow()),
        )

        # Background task: drain STT events
        self._stt_task = asyncio.create_task(self._drain_stt())

        # Warm TTS cache (fast if already warmed at boot) and pull the
        # boot-pre-synthesised greeting so the first turn is instant.
        asyncio.create_task(_warm_tts_cache())
        self._greeting_text = (
            getattr(BrowserSession, "_BOOT_GREETING_TEXT", None)
            or f"您好，这里是{get_settings().park_name}，"
               f"请讲一下您的车牌号、来哪家公司、什么事？"
        )
        self._greeting_pcm = getattr(BrowserSession, "_BOOT_GREETING_PCM", b"")
        if not self._greeting_pcm:
            # Boot warm-up didn't run (or failed). Synthesise now as fallback.
            self._greeting_pcm = await _synth_pcm16k(self._greeting_text)
        if self._greeting_pcm:
            logger.info("🌐 greeting ready: {} bytes (boot-cached={})",
                        len(self._greeting_pcm),
                        bool(getattr(BrowserSession, "_BOOT_GREETING_PCM", None)))

    async def _shutdown(self) -> None:
        self._closed = True
        if self._stt_task:
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.transcriber is not None:
            try:
                await self.transcriber.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("🌐 Browser session closed")

    # ------------------------------------------------------------------
    # Inbound: browser → server
    # ------------------------------------------------------------------

    async def _on_json(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = msg.get("type")
        if t == "config":
            sr = int(msg.get("sample_rate", 16000))
            if sr in (8000, 16000, 24000, 48000):
                self.state.sample_rate = sr
                logger.info("🌐 Browser sample_rate={}", sr)
        elif t == "end":
            if self.transcriber:
                self.transcriber.mark_utterance_end()
        elif t == "barge_in":
            # User interrupted the agent mid-TTS. Log it; the browser has
            # already cut the in-flight TTS playback and started streaming
            # new mic audio, so the agent's next LLM call will be based on
            # the new context. We also nudge the STT to flush any partial
            # transcript from the previous turn so it doesn't get appended
            # to the new turn.
            logger.info("🛑 user barge-in: cutting agent TTS, new audio incoming")
            if self.transcriber:
                self.transcriber.mark_utterance_end()
        elif t == "stop":
            self._closed = True

    async def _on_pcm(self, pcm: bytes) -> None:
        if self.transcriber is None:
            return
        # Browser already captures at 16 kHz mono PCM (see static/browser_test.html
        # SAMPLE_RATE). We now run Bailian Paraformer at 16 kHz too — feeding
        # 16 kHz in directly (no resample) gives dramatically better recognition
        # than the previous 16→8 kHz downsample, which threw away the
        # high-frequency half of every phoneme and made e.g. "选" garble.
        pcm16k = self._ensure_pcm16k(pcm, self.state.sample_rate)
        # Server-side VAD: compute RMS energy; if energy drops below threshold
        # for SILENCE_FRAMES consecutive frames, treat the user as silent and
        # mark end-of-utterance so the STT emits a final transcript quickly
        # (otherwise Bailian's server-side 23 s silence timeout would kick
        # in long after the user has actually stopped talking). Without this,
        # every turn costs the full 23 s + 23 s of idle wait between turns.
        import audioop
        rms = audioop.rms(pcm16k, 2)
        # v1.15.12: ADAPTIVE threshold (replaces the v1.15.11 static 200).
        # The static value was a poor trade-off: at 200, ambient room noise
        # (fan + AC + keyboard all bleeding in on the user's Mac) easily
        # crosses 200 too, so `_speech_frames` keeps ticking up during
        # what the user thinks is silence, and bailian's
        # `max_sentence_silence=2000` never gets a clean "speech → silence"
        # transition to fire on. At 1500, the user's actual voice
        # (rms 214-606) never registered at all.
        #
        # Adaptive formula: use the QUIETEST frame in the last 3 seconds
        # as the noise floor, then set threshold = 2.5× that. Floor at
        # 200 (catches quiet voice) and ceiling at 1500 (very strict when
        # the room itself is loud — user has to raise their voice).
        #
        # v1.15.13: reverted from 1.2× (v1.15.15) back to 2.5×. The
        # 1.2× formula was too aggressive on the user's Mac + Chrome
        # setup: the noise floor and the voice peak land in the same
        # RMS band (both ≈ 400), so multiplying by 1.2× only got us to
        # 480, which let *both* voice and noise through — and once
        # noise is treated as speech, the 1.5-s VAD never sees a clean
        # "speech → silence" boundary, so the LLM never fires. 2.5×
        # forces a real SNR margin (1.0 → 2.5) so the floor and the
        # voice are clearly separated; in the user's room, the floor
        # stabilises at the true noise level and the voice peaks
        # (which compress to 600+ with AGC) clear the bar reliably.
        #
        # Worked examples (v1.15.13, multiplier 2.5):
        #   quiet room   (noise_floor ≈ 50)  → threshold = max(200, 125)  = 200
        #   user's room  (noise_floor ≈ 250) → threshold = max(200, 625)  = 625
        #   medium room  (noise_floor ≈ 400) → threshold = max(200, 1000) = 1000
        #   loud room    (noise_floor ≈ 600) → threshold = max(200, 1500) = 1500
        now = time.time()
        hist = getattr(self, "_rms_history", None)
        if hist is None:
            hist = []
            self._rms_history = hist
        hist.append((now, rms))
        cutoff = now - 3.0  # 3-second window
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        # Noise floor: minimum RMS in window (the "quiet" reference)
        if hist:
            noise_floor = min(r for _, r in hist)
            ENERGY_THRESHOLD = int(
                max(200, min(1500, noise_floor * 2.5))
            )
        else:
            ENERGY_THRESHOLD = 200
        # v1.15.8 diagnostic: log audio energy every ~6 s so we can see
        # whether the browser is actually sending the user's voice. The
        # 23 s bailian timeouts in v1.15.6 were caused by audio being
        # either silent or too quiet; this log makes that visible.
        # ~80 chunks/s × 6 s = 480 chunks; sample every 480th to log ~once
        # every 6 s, avoiding log spam.
        self._pcm_chunk_count = getattr(self, "_pcm_chunk_count", 0) + 1
        if self._pcm_chunk_count % 480 == 1:
            noise_dbg = min((r for _, r in hist), default=0) if hist else 0
            logger.info(
                "🌐 audio-diag: chunks={} rms={} noise_floor={} "
                "THRESHOLD={} (adaptive 2.5×, floor 200 / ceiling 1500)",
                self._pcm_chunk_count, rms, int(noise_dbg), ENERGY_THRESHOLD,
            )
        # The user's Mac + Chrome setup records their voice at rms 214-606
        # (the original 1500 was the "normal speech" floor at ~5-15k RMS,
        # but the user's actual mic signal is well below that — whether
        # because of a quiet built-in mic, OS input level, or the disabled
        # AGC not kicking in). At 1500 the server counted ZERO speech
        # frames, so MIN_SPEECH_FRAMES=13 was never reached, so
        # mark_utterance_end never fired, and Bailian 23-s hard timeout
        # was the only thing that ever ended a turn. Dropping to 200
        # lets rms 214-606 count as speech; the 2.5-s silence VAD then
        # properly times the end of turn. The browser's mic gate
        # (MIC_GATE_THRESHOLD=0.025) is a separate, per-frame filter
        # and stays put — together the two layers now form: 0.025
        # normalized peak (≈ 820 int16) to even get the frame sent,
        # then ≥200 int16 RMS for the server to count it as speech.
        # 19 × 80 ms = 1.5 s of silence ends the turn. v1.15.13: lowered
        # from 2.5 s → 1.5 s per user feedback "现在就是太慢了". Safe at
        # 1.5 s because v1.15.2's `_finish_pending` discriminator
        # prevents Bailian's mid-utterance "text-stable" finals from
        # firing the LLM at all — the agent only ever acts on the
        # truly-final transcript (which itself is gated by bailian's
        # own max_sentence_silence=2000). End-to-end wait time:
        #   1.5 s VAD + ~1.5 s LLM + ~1 s TTS ≈ 4 s
        # (was 6 s at 2.5 s VAD).
        # v1.15.5 history:
        #   - 2.0 s (v1.14):   user said "我还没说完就抢答"
        #   - 3.0 s (v1.15.3): user said "隔很久了才回"
        #   - 2.5 s (v1.15.5): compromise, now too slow
        #   - 1.5 s (v1.15.13): user asked for snappier reply
        SILENCE_FRAMES = 19
        SILENCE_FRAMES = 31
        # Minimum speech duration before VAD can fire: protects against
        # a single short utterance being mis-fired as the end of the turn
        # (e.g. "嗯" or "对" alone). The user must speak at least
        # 1.0 s of audio before silence can be interpreted as "I'm done".
        MIN_SPEECH_FRAMES = 13   # 13 × 80 ms ≈ 1.0 s
        if rms < ENERGY_THRESHOLD:
            self._silent_frames += 1
        else:
            self._silent_frames = 0
            self._speech_frames += 1
        if (self._silent_frames == SILENCE_FRAMES
                and self._speech_frames >= MIN_SPEECH_FRAMES):
            # Exactly hit the silence threshold after speech: ask the STT
            # to emit a final transcript and tear down the current task.
            try:
                self.transcriber.mark_utterance_end()
            except Exception as e:  # noqa: BLE001
                logger.debug("mark_utterance_end err: {}", e)
        try:
            self.transcriber.send_audio(pcm16k)
        except Exception as e:  # noqa: BLE001
            logger.debug("STT send_audio err: {}", e)

    @staticmethod
    def _to_pcm8k(pcm: bytes, in_rate: int) -> bytes:
        if in_rate == 8000:
            return pcm
        if in_rate not in (16000, 24000, 48000):
            in_rate = 16000
        converted, _ = audioop.ratecv(pcm, 2, 1, in_rate, 8000, None)
        return converted

    @staticmethod
    def _ensure_pcm16k(pcm: bytes, in_rate: int) -> bytes:
        """Make sure audio is Int16 LE mono at 16 kHz. Pass-through if it
        already is; upsample 8 kHz; resample anything else to 16 kHz.

        Bailian Paraformer-realtime-v1 is configured at 16 kHz in stt.py;
        sending a different rate would either get rejected or produce garbled
        transcripts.
        """
        if in_rate == 16000:
            return pcm
        if in_rate not in (8000, 16000, 24000, 48000):
            in_rate = 16000  # assume OK; downstream will degrade gracefully
        converted, _ = audioop.ratecv(pcm, 2, 1, in_rate, 16000, None)
        return converted

    # ------------------------------------------------------------------
    # STT event pump
    # ------------------------------------------------------------------

    async def _drain_stt(self) -> None:
        assert self.transcriber is not None
        try:
            async for evt in self.transcriber.events():
                if self._closed:
                    break
                if not evt.text:
                    continue
                if not evt.is_final:
                    # Stream partial transcript back so the UI can show it live
                    if evt.text != self.state.last_partial:
                        self.state.last_partial = evt.text
                        await self.ws.send_json({
                            "type": "transcript",
                            "text": evt.text,
                            "is_final": False,
                        })
                    continue
                # is_final=True with speech_final=False: a text-stability
                # update from Bailian (user paused ~600 ms mid-utterance
                # and ASR is confident in the text so far). Stream to the
                # UI as a final bubble so the user sees what was captured,
                # but DO NOT trigger the LLM — the user is still talking.
                # is_final=True with speech_final=True: the true end of
                # utterance (came after we sent `finish-task`, which the
                # 2 s server-side VAD triggers). Now we run the agent.
                self.state.last_partial = ""
                await self.ws.send_json({
                    "type": "transcript",
                    "text": evt.text,
                    "is_final": True,
                })
                if not evt.speech_final:
                    logger.debug("🌐 mid-utterance final (text-stable): {!r}", evt.text)
                    continue
                # User just spoke — update activity timestamp and stop any
                # pending silence-nudge watchdog (it'll be restarted after
                # the agent's reply if more fields are still missing).
                self._last_user_activity = time.time()
                self._nudge_count = 0
                if self._silence_watchdog_task and not self._silence_watchdog_task.done():
                    self._silence_watchdog_task.cancel()
                await self._handle_turn(evt.text)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("STT drain error: {}", e)

    # ------------------------------------------------------------------
    # LLM → TTS → outbound
    # ------------------------------------------------------------------

    async def _handle_turn(self, user_text: str) -> None:
        if self.agent is None:
            return
        t_llm_start = time.perf_counter()
        try:
            self.agent.push_user_utterance(user_text)
            reply = await self.agent.step()
        except Exception as e:  # noqa: BLE001
            logger.exception("agent.step failed: {}", e)
            await self.ws.send_json({"type": "error", "message": f"agent: {e}"})
            return
        llm_ms = int((time.perf_counter() - t_llm_start) * 1000)

        info = reply.visitor_info
        logger.info(
            "🌐 browser turn user='{}' → agent='{}' (LLM={}ms, final={})",
            user_text[:40], reply.text_to_speak[:40], llm_ms, reply.is_final,
        )

        await self.ws.send_json({"type": "agent_text", "text": reply.text_to_speak})
        await self.ws.send_json({
            "type": "timing",
            "llm_ms": llm_ms,
            "tts_cached": reply.text_to_speak in _TTS_CACHE,
        })

        if reply.is_final:
            # Persist + push WeChat + say goodbye + close
            # v1.15.17: re-add auto-fill for fields the LLM never set
            # via update_visitor_info tool. The LLM tool schema only
            # exposes {plate, reason, phone, contact_name} — `company`
            # has no setter — so the LLM often *says* "蓝色鲸鱼" in
            # its reply without ever writing it to the visitor state,
            # and the WeChat card shows "未提供". Same for
            # `duration` (prompt never asks, so always None). Server-
            # side default is the right level for these.
            try:
                # v1.15.18: company auto-fill stays (门卫需要知道去哪家),
                # but duration auto-fill REMOVED — user said the gatekeeper
                # doesn't need the "预计停留" line on the WeChat card.
                if not info.company:
                    info.company = get_settings().park_name
            except Exception as e:  # noqa: BLE001
                logger.warning("auto-fill fields failed: {}", e)
            try:
                await save_visit(info)
            except Exception as e:  # noqa: BLE001
                logger.warning("save_visit failed: {}", e)
            try:
                ok = await wechat.send_visitor_card(info)
            except Exception as e:  # noqa: BLE001
                logger.warning("wechat push failed: {}", e)
                ok = False
            await self.ws.send_json({"type": "wechat_ok", "ok": ok})
            if reply.text_to_speak:
                await self._speak(reply.text_to_speak)
            # NOTE: we previously appended a hardcoded "好的，已通知门卫，
            # 欢迎光临。" after the LLM's reply here. That overrode whatever
            # the model had said (including the cache-friendly "请稍等放行"
            # variant from _pick_closing), so the user always heard the
            # hotel-front-desk "欢迎光临" instead of the gatekeeper "请稍
            # 候放行". The model's reply + the cached variable-free closing
            # is sufficient on its own — no fallback needed.
            await self.ws.send_json({"type": "done", "summary": info.summary()})
            self._closed = True
        else:
            if reply.text_to_speak:
                await self._speak(reply.text_to_speak)
            # Start the silence-nudge watchdog: if the user goes quiet for
            # 8+ s without filling the remaining fields, replay a short
            # reminder. The watchdog self-cancels on the next user turn
            # (see _drain_stt) and on visitor completion.
            await self._start_silence_watchdog()

    async def _speak(self, text: str) -> None:
        """Speak `text` to the browser. Uses Bailian TTS directly at 24 kHz
        WAV → 16 kHz PCM (one resample, no μ-law round-trip) so the user
        gets near-original quality instead of phone-band audio.
        """
        if not text.strip():
            return
        await self.ws.send_json({"type": "agent_audio_start"})
        try:
            # Try cache first — short common phrases return immediately
            pcm = _TTS_CACHE.get(text)
            if pcm is None:
                pcm = await _synth_pcm16k(text)
            if pcm:
                frame = int(OUT_PCM_RATE * 80 / 1000) * 2
                for off in range(0, len(pcm), frame):
                    await self.ws.send_bytes(pcm[off:off + frame])
        finally:
            await self.ws.send_json({"type": "agent_audio_end"})

    # ------------------------------------------------------------------
    # Silence-nudge watchdog
    # ------------------------------------------------------------------

    async def _start_silence_watchdog(self) -> None:
        """Start (or restart) the silence-nudge watchdog. Called after every
        non-final agent reply. The watchdog wakes up every 8 s and, if the
        user has been silent AND visitor is not complete, plays a short
        reminder that lists the still-missing fields.

        Why server-generated (not LLM-generated) text?
          - 1.5 s LLM call is too slow when the user is already 8 s silent
          - The missing-field info is server-authoritative; the LLM is just
            guessing from the conversation history (it sometimes forgets)
          - Wording is fixed → cache-friendly for TTS
        """
        if self._silence_watchdog_task and not self._silence_watchdog_task.done():
            self._silence_watchdog_task.cancel()
        self._nudge_count = 0
        self._silence_watchdog_task = asyncio.create_task(self._silence_watchdog_loop())

    async def _stop_silence_watchdog(self) -> None:
        if self._silence_watchdog_task and not self._silence_watchdog_task.done():
            self._silence_watchdog_task.cancel()
            try:
                await self._silence_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _silence_watchdog_loop(self) -> None:
        NUDGE_INTERVAL = 8.0   # seconds between nudges
        MAX_NUDGES = 3         # after this many, give up politely
        RECENT_ACTIVITY_WINDOW = 2.0  # skip nudge if user spoke in last N s
        try:
            while not self._closed and self._nudge_count < MAX_NUDGES:
                await asyncio.sleep(NUDGE_INTERVAL)
                if self._closed:
                    return
                # User activity guard: if they spoke in the last window
                # (still in the middle of an utterance, or just finished),
                # don't nudge — they're either thinking or done.
                if (time.time() - self._last_user_activity) < RECENT_ACTIVITY_WINDOW:
                    logger.debug("🌐 silence watchdog: user active, skip nudge")
                    continue
                # Visitor complete: nothing left to ask about, exit.
                if self.agent is None or self.agent.visitor.is_complete():
                    return
                # WebSocket dead: nothing to do.
                if self._ws is None or self._ws.client_state.name != "CONNECTED":
                    return
                await self._send_silence_nudge()
                self._nudge_count += 1
                # Treat the nudge as a turn boundary: pretend user is now
                # active so we don't immediately re-nudge while the nudge
                # audio is still playing.
                self._last_user_activity = time.time()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.debug("silence watchdog err: {}", e)

    async def _send_silence_nudge(self) -> None:
        """Build a short reminder from `visitor` state and play it. Wording
        varies by nudge count so the user doesn't hear the same phrase 3x.
        """
        if self.agent is None or self._ws is None:
            return
        missing = self._get_missing_field_names()
        if not missing:
            return
        missing_text = "、".join(missing)
        if self._nudge_count == 0:
            text = f"您好，还在么？还差{missing_text}，麻烦您说一声。"
        elif self._nudge_count == 1:
            text = f"您好，还在听么？还差{missing_text}。"
        else:
            # 3rd nudge: give up politely
            text = "好的，没听清没关系，再见。"
            await self._speak(text)
            logger.info("🌐 silence nudge #{} (give up): {}", self._nudge_count, text)
            # End the call cleanly
            try:
                await self.ws.send_json({"type": "done", "summary": self.agent.visitor.summary()})
            except Exception:  # noqa: BLE001
                pass
            self._closed = True
            return
        logger.info("🌐 silence nudge #{}: {}", self._nudge_count, text)
        await self._speak(text)

    def _get_missing_field_names(self) -> list[str]:
        """Human-readable names of the still-missing required fields, in the
        order the prompt lists them (车牌 → 事由 → 手机 → 姓名)."""
        if self.agent is None:
            return []
        FIELD_NAMES = [
            ("plate", "车牌号"),
            ("reason", "来访事由"),
            ("phone", "手机号"),
            ("contact_name", "姓名"),
        ]
        v = self.agent.visitor
        return [name for attr, name in FIELD_NAMES if not getattr(v, attr, None)]

    @staticmethod
    async def _tts_pcm16k_chunks(text: str, *, chunk_ms: int = 80):
        """Synthesise text via CosyVoice WebSocket TTS, return PCM 16 kHz
        mono chunks. CosyVoice streams audio chunks; we downsample 24 → 16 kHz
        once and slice into ~80 ms frames for low-latency streaming playback.
        """
        from app.services import tts as _tts

        pcm = await _tts.synthesize_to_pcm16k(text)
        if not pcm:
            return
        # 80 ms @ 16 kHz s16le mono = 16000 * 0.08 * 2 = 2560 bytes
        frame = int(OUT_PCM_RATE * chunk_ms / 1000) * 2
        for off in range(0, len(pcm), frame):
            yield pcm[off:off + frame]
