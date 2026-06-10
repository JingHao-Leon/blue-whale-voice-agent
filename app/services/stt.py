"""
Speech-to-text layer.

Three providers, picked at runtime by `make_transcriber()`:

  1. **Deepgram** (`nova-2`): preferred for telephony. ~250 ms interim
     latency, native Chinese, accepts G.711 μ-law at 8 kHz directly.
  2. **Bailian (百炼) Paraformer** (`paraformer-realtime-v1`): Alibaba's
     streaming ASR. Same low-latency profile, slightly different API. Use
     when running in mainland China or on Alibaba Cloud.
  3. **OpenAI Whisper** (fallback): batch transcription. Higher latency
     but works without a Deepgram/Bailian key.

The interface is intentionally tiny: `StreamTranscriber.__aiter__` yields
interim + final transcripts. The caller doesn't care which provider.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from typing import AsyncIterator, Optional, Protocol

from openai import AsyncOpenAI

from app.config import get_settings
from app.logging import logger


class TranscriptEvent:
    __slots__ = ("text", "is_final", "confidence", "speech_final")

    def __init__(self, text: str, *, is_final: bool, confidence: float = 1.0, speech_final: bool = False):
        self.text = text
        self.is_final = is_final
        self.confidence = confidence
        self.speech_final = speech_final  # True when user paused → caller can act

    def __repr__(self) -> str:
        flag = "F" if self.is_final else "I"
        if self.speech_final:
            flag += "/S"
        return f"<Transcript {flag} {self.text!r}>"


class Transcriber(Protocol):
    def send_audio(self, mulaw_8k: bytes) -> None: ...
    def mark_utterance_end(self) -> None: ...
    async def events(self) -> AsyncIterator[TranscriptEvent]: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Deepgram streaming
# ---------------------------------------------------------------------------

class DeepgramTranscriber:
    """Wraps Deepgram's live transcription WebSocket."""

    def __init__(self, *, language: str = "zh-CN", model: str = "nova-2") -> None:
        self.settings = get_settings()
        if not self.settings.has_deepgram:
            raise RuntimeError("DEEPGRAM_API_KEY not configured")
        self.language = language
        self.model = model
        self._queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()
        self._closed = False
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "DeepgramTranscriber":
        await self._connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _connect(self) -> None:
        # Lazy import so the package is only required if you use Deepgram.
        from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions

        client = DeepgramClient(self.settings.deepgram_api_key)
        self._ws = client.listen.asynclive.v("1")
        options = LiveOptions(
            language=self.language,
            model=self.model,
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            interim_results=True,
            endpointing=300,           # ms of silence → final
            vad_events=True,
            smart_format=True,
            punctuate=True,
        )

        async def on_message(_self, result, **kwargs):
            try:
                alt = result.channel.alternatives[0]
                text = (alt.transcript or "").strip()
                if not text:
                    return
                is_final = bool(result.is_final)
                speech_final = bool(getattr(result, "speech_final", False))
                evt = TranscriptEvent(
                    text,
                    is_final=is_final,
                    confidence=float(alt.confidence or 1.0),
                    speech_final=speech_final,
                )
                await self._queue.put(evt)
            except Exception as e:  # noqa: BLE001
                logger.debug("Deepgram on_message swallowed: {}", e)

        async def on_error(_self, error, **kwargs):
            logger.error("Deepgram error: {}", error)
            await self._queue.put(TranscriptEvent("", is_final=False))

        self._ws.on(LiveTranscriptionEvents.Transcript, on_message)
        self._ws.on(LiveTranscriptionEvents.Error, on_error)
        if not await self._ws.start(options):
            raise RuntimeError("Deepgram live start failed")
        logger.debug("Deepgram connected (model={}, lang={})", self.model, self.language)

    def send_audio(self, mulaw_8k: bytes) -> None:
        if self._closed or self._ws is None:
            return
        # Fire-and-forget; Deepgram SDK accepts concurrent sends.
        asyncio.create_task(self._send(mulaw_8k))

    async def _send(self, mulaw_8k: bytes) -> None:
        async with self._send_lock:
            try:
                await self._ws.send(mulaw_8k)
            except Exception as e:  # noqa: BLE001
                logger.debug("Deepgram send failed (transcriber closed?): {}", e)

    def mark_utterance_end(self) -> None:
        # Deepgram does endpointing server-side, so this is a no-op.
        pass

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while not self._closed:
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                yield evt
            except asyncio.TimeoutError:
                # Periodic heartbeat to give the caller a chance to do VAD
                if self._closed:
                    return
                continue

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.finish()
            except Exception as e:  # noqa: BLE001
                logger.debug("Deepgram close swallowed: {}", e)


# ---------------------------------------------------------------------------
# Bailian (百炼) Paraformer streaming
# ---------------------------------------------------------------------------

class BailianParaformerTranscriber:
    """Streams audio to Alibaba Bailian's Paraformer realtime ASR over
    a raw WebSocket connection.

    We bypass the dashscope SDK entirely:
    - The SDK's `Recognition` class has a hardcoded model list that doesn't
      include the actual model name (e.g. `funasr-realtime` returns
      "Model not found").
    - The SDK's `SpeechSynthesizer.call` (TTS) has a `KeyError: 'begin_time'`
      bug.
    - The WebSocket protocol the SDK uses is undocumented; we reverse-
      engineered enough of it to talk to the server directly.

    The first-message action is `run-task` (not `run`, not `start`).
    The model name is `paraformer-realtime-v1` (the streaming ASR model
    Alibaba actually accepts).

    **Multi-turn handling (added 2026-06-09)**: Bailian's Fun-ASR
    WebSocket is single-task-per-WS — after `task-finished` the server
    closes the connection and any further `send_audio()` calls are
    silently dropped. For multi-turn phone calls (visitor speaks
    → agent responds → visitor speaks again) we run a supervisor
    loop that reopens the WS for each user turn. The caller signals
    the end of a turn via `mark_utterance_end()` which sends a
    `finish-task` action — that's what makes the server emit a final
    transcript without waiting for the full 23 s server-side silence
    timeout.
    """

    WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.has_bailian:
            raise RuntimeError("DASHSCOPE_API_KEY not configured")
        self._out: asyncio.Queue[TranscriptEvent] = asyncio.Queue()
        self._closed = False
        self._ws: Optional[object] = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        # The current task_id of the run-task in progress. Used so
        # mark_utterance_end() can send a matching finish-task.
        self._task_id: Optional[str] = None
        # The currently-active task's "send finish-task" flag. Set to True
        # by mark_utterance_end() (called from sync context); consumed
        # by the supervisor loop.
        self._finish_pending = False
        # Signal that audio arrived while WS was closed → supervisor
        # should reopen. Without this, the supervisor would have to
        # spin a new task blindly every turn (and the server would
        # 23 s time it out if no audio followed).
        self._audio_pending = False
        self._audio_pending_event = asyncio.Event()

    async def __aenter__(self) -> "BailianParaformerTranscriber":
        self._task = asyncio.create_task(self._supervisor())
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def send_audio(self, audio_bytes: bytes) -> None:
        if self._closed:
            return
        if self._ws is None or self._ws_loop is None or self._task_id is None:
            # WS not ready (between turns, or just-opened). Drop the audio
            # but tell the supervisor we have pending data so it knows to
            # reopen. We lose at most a few frames of pre-VAD audio,
            # acceptable for turn-taking.
            self._audio_pending = True
            self._audio_pending_event.set()
            return
        # Bailian expects raw PCM/μ-law; we send μ-law 8 kHz directly.
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(audio_bytes), self._ws_loop)
        except Exception as e:  # noqa: BLE001
            logger.debug("WS audio send failed: {}", e)

    def mark_utterance_end(self) -> None:
        """Caller (browser session / Twilio handler) tells us the user has
        finished speaking this turn. We send a `finish-task` action to
        Bailian so it emits a final transcript and tears down the
        current task — that gives us a fast handoff to the next turn
        instead of waiting for the 23 s server silence timeout.
        """
        if self._closed or self._ws is None or self._task_id is None or self._ws_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": self._task_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    )
                ),
                self._ws_loop,
            )
            self._finish_pending = True
        except Exception as e:  # noqa: BLE001
            logger.debug("WS finish-task send failed: {}", e)

    async def _supervisor(self) -> None:
        """Outer loop: keep one ASR WS session alive at a time, reopen
        immediately whenever the previous task ended. We don't wait
        for explicit audio — every user turn needs a fresh task because
        Bailian's Fun-ASR WebSocket is single-task-per-WS.

        Trade-off: a task that opens without receiving any audio will
        time out 23 s later on the server side. We accept this cost
        because in a real voice call, audio always follows within
        1-2 s (the user's natural pause between turns is short).
        """
        while not self._closed:
            try:
                await self._run_one_session()
            except Exception as e:  # noqa: BLE001
                logger.warning("Bailian ASR WS error: {}", e)
            if self._closed:
                return
            # Reset pending state and reopen immediately so the next
            # audio frame finds an open WS. A small delay avoids
            # burning a 23 s server task if the session was closed
            # because the user hung up.
            self._audio_pending = False
            self._audio_pending_event.clear()
            await asyncio.sleep(0.05)

    async def _run_one_session(self) -> None:
        """One ASR task: open WS, send run-task, pump events until
        task-finished / task-failed / finish-task + final received.
        """
        import time as _t
        import websockets  # local import keeps the dependency optional

        task_id = f"ca-{int(_t.time() * 1000)}"
        self._task_id = task_id
        try:
            async with websockets.connect(
                self.WS_URL,
                additional_headers={"Authorization": f"Bearer {self.settings.dashscope_api_key}"},
                max_size=10 * 1024 * 1024,
            ) as ws:
                self._ws = ws
                self._ws_loop = asyncio.get_running_loop()
                # Send the run-task message
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "run-task",
                                "task_id": task_id,
                                "streaming": "duplex",
                            },
                            "payload": {
                                "model": self.settings.bailian_stt_model,
                                "task_group": "audio",
                                "task": "asr",
                                "function": "recognition",
                                "parameters": {
                                    "format": "pcm",
                                    # 16 kHz gives Paraformer/Fun-ASR much better
                                    # recognition on browser-mic audio than
                                    # the 8 kHz phone-grade setting; the
                                    # browser_session path already pipes
                                    # 16 kHz PCM straight in, so no resample
                                    # happens on the server side.
                                    "sample_rate": 16000,
                                    # Fun-ASR (https://bailian.console.aliyun.com/
                                    # cn-beijing?tab=api#/api/?type=model&url=2983775)
                                    # protocol params. Without `language_hints`
                                    # the model sometimes routes short Chinese
                                    # utterances to English.
                                    #
                                    # `max_sentence_silence`: how long the STT
                                    # waits after the user stops talking before
                                    # emitting a final transcript. 2000 ms
                                    # matches the browser-side VAD threshold
                                    # (25 × 80 ms = 2.0 s) so the Fun-ASR service
                                    # and the browser both agree on what counts
                                    # as a sentence boundary. The user explicitly
                                    # asked for "停顿 2 秒再去识别" — anything
                                    # shorter than 2 s is treated as a mid-
                                    # sentence pause, not a turn end, so a
                                    # single multi-field sentence is captured
                                    # as one STT final.
                                    #
                                    # `heartbeat: true` keeps the WS alive
                                    # across the 60 s idle-disconnect timer.
                                    "language_hints": ["zh"],
                                    "max_sentence_silence": 2000,
                                    "heartbeat": True,
                                },
                                "input": {},
                                "output": {},
                            },
                        }
                    )
                )
                logger.info("Bailian ASR WS opened, model={}", self.settings.bailian_stt_model)
                # Read events until task ends
                while not self._closed:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    evt = self._parse_event(msg)
                    if evt is not None:
                        await self._out.put(evt)
                    event = msg.get("header", {}).get("event", "")
                    if event in ("task-finished", "task-failed"):
                        break
        except Exception as e:  # noqa: BLE001
            logger.warning("Bailian ASR WS error: {}", e)
        finally:
            self._ws = None
            self._task_id = None
            self._finish_pending = False

    def _parse_event(self, msg: dict) -> Optional[TranscriptEvent]:
        event = msg.get("header", {}).get("event", "")
        if event == "task-failed":
            logger.warning("Bailian task failed: {}", msg.get("header", {}).get("error_message", "?"))
            return None
        if event != "result-generated":
            return None
        payload = msg.get("payload", {}) or {}
        output = payload.get("output", {}) or {}
        sentence = output.get("sentence")
        if not sentence:
            return None
        text = sentence.get("text", "")
        if not text:
            return None
        # Bailian's fun-asr-realtime emits `end_time != null` on TWO
        # different kinds of events, and they look identical to us
        # unless we correlate with what we sent:
        #
        #   (a) Text-stability update mid-utterance: user paused ~600 ms
        #       between words/fields, ASR is confident in the text so
        #       far, but the user is still talking. end_time is set,
        #       `is_final=True` from ASR's POV, but the utterance has
        #       NOT ended. This is the case the previous code treated
        #       as "end of turn" and triggered the LLM mid-sentence.
        #
        #   (b) True end of utterance: only happens after we sent
        #       `finish-task` (via mark_utterance_end, which the
        #       server's 2 s silence VAD triggers). Bailian then
        #       emits the final result-generated and task-finished.
        #
        # The discriminator: `_finish_pending` is True ONLY when we
        # already sent finish-task for the current task. So:
        #   is_final=True && _finish_pending=False  → (a) text update
        #   is_final=True && _finish_pending=True   → (b) true end
        #
        # We emit ALL events with their real `is_final` flag (so the UI
        # can show text-stability updates as proper user bubbles) but
        # only mark `speech_final=True` on the true end. The drain loop
        # uses `speech_final` (not `is_final`) to decide when to fire
        # the LLM — that's what kills the "agent answers before I'm
        # done" false-trigger.
        end_time = sentence.get("end_time")
        is_final = end_time is not None
        speech_final = bool(is_final and self._finish_pending)
        # DEBUG (not INFO) so production logs stay clean. If a user reports
        # "agent answered too early" again, set loglevel=DEBUG and you'll see
        # exactly which is_final events are text-stability vs true end.
        logger.debug("Bailian evt: is_final={} speech_final={} text={!r}",
                     is_final, speech_final, text.strip()[:40])
        if speech_final:
            self._finish_pending = False  # consume
        return TranscriptEvent(
            text.strip(),
            is_final=is_final,
            speech_final=speech_final,
            confidence=float(sentence.get("confidence", 1.0)),
        )

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while not self._closed:
            try:
                evt = await asyncio.wait_for(self._out.get(), timeout=0.1)
                yield evt
            except asyncio.TimeoutError:
                if self._closed:
                    return
                continue

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# OpenAI Whisper fallback (batch)
# ---------------------------------------------------------------------------

class WhisperTranscriber:
    """Record audio in chunks, transcribe on demand (no streaming)."""

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.has_openai:
            raise RuntimeError("OPENAI_API_KEY not configured (Whisper fallback)")
        self.client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        self._buf = bytearray()
        self._mu = asyncio.Event()  # signalled when user marks end
        self._closed = False
        self._out: asyncio.Queue[TranscriptEvent] = asyncio.Queue()

    def send_audio(self, mulaw_8k: bytes) -> None:
        self._buf.extend(mulaw_8k)

    def mark_utterance_end(self) -> None:
        # Kick off transcription in the background.
        asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        if not self._buf:
            return
        # Convert μ-law 8k to wav 16k for Whisper
        wav = _mulaw8k_to_wav16k(bytes(self._buf))
        self._buf.clear()
        t0 = time.perf_counter()
        try:
            resp = await self.client.audio.transcriptions.create(
                model="whisper-1",
                file=("audio.wav", wav, "audio/wav"),
                language="zh",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Whisper failed: {}", e)
            return
        text = (resp.text or "").strip()
        if text:
            await self._out.put(
                TranscriptEvent(text, is_final=True, speech_final=True, confidence=1.0)
            )
            logger.info("Whisper transcribed in {:.2f}s: {}", time.perf_counter() - t0, text)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while not self._closed:
            try:
                evt = await asyncio.wait_for(self._out.get(), timeout=0.1)
                yield evt
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mulaw8k_to_wav16k(mulaw: bytes) -> bytes:
    """Convert raw 8 kHz μ-law bytes to a 16 kHz PCM WAV file in memory.

    Twilio sends μ-law 8 kHz. Whisper expects PCM 16 kHz. We do the upsampling
    in pure Python so we don't pull in scipy for a 30-line function.
    """
    import audioop
    pcm8k = audioop.ulaw2lin(mulaw, 2)
    pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
    return _wrap_wav(pcm16k, sample_rate=16000, sample_width=2, channels=1)


def _wrap_wav(pcm: bytes, *, sample_rate: int, sample_width: int, channels: int) -> bytes:
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def make_transcriber() -> Transcriber:
    """Pick the best available transcriber. Bailian takes priority."""
    settings = get_settings()
    if settings.has_bailian:
        return BailianParaformerTranscriber()
    if settings.has_deepgram:
        return DeepgramTranscriber()
    if settings.has_openai:
        return WhisperTranscriber()
    raise RuntimeError(
        "No STT provider configured (need DASHSCOPE_API_KEY, DEEPGRAM_API_KEY, or OPENAI_API_KEY)"
    )
