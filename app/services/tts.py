"""
Text-to-speech layer.

Three providers, picked at runtime:

  1. **Bailian (百炼) CosyVoice** (`cosyvoice-v3.5-plus`): Alibaba's
     streaming WebSocket TTS. 2026 generation, lowest TTFB (first-audio
     latency) of any Bailian TTS — server starts streaming audio chunks
     within ~200 ms of run-task. We default to this when DASHSCOPE_API_KEY
     is set (the mainland-China friendly stack).
  2. **Microsoft Edge TTS** via the `edge-tts` package: free, no API key,
     very natural Chinese voices. Streams MP3 chunks. Used as fallback when
     Bailian is unavailable.
  3. **OpenAI TTS** (tts-1): paid, available as a final fallback.

Twilio's Media Streams expects raw **μ-law at 8 kHz**. The browser test
page takes raw PCM 16 kHz. We use CosyVoice's `format=pcm, sample_rate=24000`
output and resample to the format each transport needs (16 kHz PCM for
browser, 8 kHz μ-law for Twilio) via the stdlib `audioop` module.

Architecture
------------

Each provider is a class that exposes:
  - `available(settings) -> bool`
  - `synth_pcm24k(text) -> bytes`      # one-shot
  - `stream_pcm24k(text) -> AsyncIterator[bytes]`  # optional streaming

`PROVIDERS` is an ordered list; `pick_provider(settings)` returns the
first one whose `available()` is True. Adding a new provider is two
steps: implement the class, append to `PROVIDERS`. No more touching
the three public `synthesize_to_*` functions to keep them in sync.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import subprocess
import time
from typing import AsyncIterator, Optional, Protocol

from openai import AsyncOpenAI

from app.config import get_settings
from app.logging import logger


# ---------------------------------------------------------------------------
# Bailian CosyVoice rate limit + queue
# ---------------------------------------------------------------------------
#
# The Bailian CosyVoice API rejects concurrent WebSocket calls with
# "Requests rate limit exceeded" once we exceed a handful of parallel
# requests (the cap depends on the account's QPS tier). For a single-
# session voice agent this never matters; for stress tests / multi-tenant
# deployments, the cap is the difference between "works" and "every
# session times out at 23 s".
#
# Solution: a single global semaphore that caps CosyVoice concurrency.
# When the cap is hit, additional calls `await` until a slot frees up.
# Latency goes up under contention, but no session fails outright.
#
# Cap value (3) is empirically what the default BaiLian key allows before
# rate-limit errors appear in 10-concurrent stress tests. Lower it if your
# account has a tighter QPS budget; raise it if you have a paid tier.
_COSYVOICE_SEMAPHORE = asyncio.Semaphore(3)


# ---------------------------------------------------------------------------
# Provider protocol + registry
# ---------------------------------------------------------------------------


class TTSProvider(Protocol):
    """Interface every TTS provider implements.

    A provider is selected at call-time by `pick_provider()` based on
    its `available()` check. `name` is a short stable identifier used
    for logging and metrics.
    """

    name: str

    def available(self, settings) -> bool: ...

    async def synth_pcm24k(self, text: str) -> bytes:
        """One-shot synthesis. Returns raw PCM 24 kHz mono s16le, no header.
        Empty bytes on failure (callers should fall back or skip TTS)."""
        ...

    async def stream_pcm24k(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        """Optional streaming variant. Yields PCM 24 kHz mono s16le chunks
        as they arrive from the upstream service. Providers that don't
        support streaming should NOT implement this — `stream_to_*`
        callers will fall back to one-shot."""
        ...


# Ordered list. First available wins. Append-only: when you add a new
# provider, add it where you want it in the priority chain.
PROVIDERS: list[TTSProvider] = []  # populated below to keep the file flat


def _is_cosyvoice_model(model: str) -> bool:
    return "cosyvoice" in model.lower()


# ---------------------------------------------------------------------------
# Provider: Bailian (CosyVoice WS streaming + qwen3-tts-flash REST)
# ---------------------------------------------------------------------------


class BailianProvider:
    """Bailian TTS — picks CosyVoice WS streaming when the configured model
    is a CosyVoice variant, otherwise falls back to the qwen3-tts-flash
    REST one-shot endpoint. Both paths return PCM 24 kHz mono s16le."""

    name = "bailian"

    def available(self, settings) -> bool:
        return bool(settings.has_bailian)

    async def synth_pcm24k(self, text: str) -> bytes:
        settings = get_settings()
        if _is_cosyvoice_model(settings.bailian_tts_model):
            return await _synth_cosyvoice_pcm24k(text)
        return await _synth_qwen3tts_pcm24k(text)

    async def stream_pcm24k(self, text: str) -> AsyncIterator[bytes]:
        settings = get_settings()
        if _is_cosyvoice_model(settings.bailian_tts_model):
            async for chunk in _stream_cosyvoice_to_pcm24k(text):
                yield chunk
            return
        # qwen3-tts-flash is REST one-shot; degrade gracefully.
        yield await _synth_qwen3tts_pcm24k(text)


# ---------------------------------------------------------------------------
# Provider: Microsoft Edge TTS (free, no API key)
# ---------------------------------------------------------------------------


class EdgeProvider:
    """Microsoft Edge TTS — free, no key, streams MP3 chunks."""

    name = "edge"

    def available(self, settings) -> bool:
        return bool(settings.edge_tts_voice)

    async def synth_pcm24k(self, text: str) -> bytes:
        settings = get_settings()
        mp3 = await _synth_edge(text, voice=settings.edge_tts_voice)
        return _mp3_to_pcm24k(mp3)

    async def stream_pcm24k(self, text: str) -> AsyncIterator[bytes]:
        settings = get_settings()
        async for chunk in _stream_edge_to_pcm24k(text, voice=settings.edge_tts_voice):
            yield chunk


# ---------------------------------------------------------------------------
# Provider: OpenAI TTS (paid)
# ---------------------------------------------------------------------------


class OpenAITTSProvider:
    """OpenAI TTS — paid, returns MP3 we decode to PCM via ffmpeg."""

    name = "openai"

    def available(self, settings) -> bool:
        return bool(settings.has_openai)

    async def synth_pcm24k(self, text: str) -> bytes:
        mp3 = await _synth_openai(text)
        return _mp3_to_pcm24k(mp3)

    # OpenAI has no streaming audio.speech endpoint we can rely on across
    # versions; we deliberately don't implement stream_pcm24k. Callers
    # that need streaming will fall back to one-shot.


# Populate the registry now that the classes are defined.
PROVIDERS.extend([BailianProvider(), EdgeProvider(), OpenAITTSProvider()])


def pick_provider(settings=None) -> TTSProvider:
    """Return the first provider whose `available()` is True.

    Raises RuntimeError if none are configured — caller should fall back
    to whatever error path its transport provides (Twilio can still
    <Hangup> with a TwiML, the browser can show a text error).
    """
    s = settings or get_settings()
    for p in PROVIDERS:
        if p.available(s):
            return p
    raise RuntimeError(
        "No TTS provider configured (need DASHSCOPE_API_KEY, EDGE_TTS_VOICE, or OPENAI_API_KEY)"
    )


# ---------------------------------------------------------------------------
# Public API — format-specific wrappers
# ---------------------------------------------------------------------------
#
# All three functions reduce to the same pattern:
#   1. pick a provider
#   2. ask it for PCM 24 kHz (one-shot or stream)
#   3. resample to the transport's target format (μ-law 8 k / PCM 16 k)
#
# The format conversion is the only thing that varies between wrappers.


async def synthesize_to_mulaw8k(text: str) -> bytes:
    """Synthesise text and return a single μ-law 8 kHz byte string."""
    if not text.strip():
        return b""
    pcm = await pick_provider().synth_pcm24k(text)
    return _pcm24k_to_mulaw8k(pcm)


async def synthesize_to_pcm16k(text: str) -> bytes:
    """Synthesise text and return raw PCM 16 kHz mono s16le. For browser / WebRTC."""
    if not text.strip():
        return b""
    pcm = await pick_provider().synth_pcm24k(text)
    return _pcm24k_to_pcm16k(pcm)


async def synthesize_streaming_mulaw8k(text: str) -> AsyncIterator[bytes]:
    """Stream μ-law 8 kHz chunks. Useful for low TTFB (first-byte latency).

    If the active provider supports streaming, chunks arrive within ~200 ms
    of run-task (CosyVoice). Otherwise we degrade to one-shot.
    """
    if not text.strip():
        return
    provider = pick_provider()
    stream_fn = getattr(provider, "stream_pcm24k", None)
    if stream_fn is None:
        # No streaming — one-shot and yield a single chunk.
        yield await synthesize_to_mulaw8k(text)
        return
    async for pcm_chunk in stream_fn(text):
        if not pcm_chunk:
            continue
        # Slice into ~80 ms frames and convert to μ-law 8 kHz
        target = 1920 * 2  # 80 ms at 24 kHz s16le mono
        for off in range(0, len(pcm_chunk), target):
            yield _pcm24k_to_mulaw8k(pcm_chunk[off:off + target])


# ---------------------------------------------------------------------------
# Bailian (百炼) CosyVoice WebSocket streaming
# ---------------------------------------------------------------------------

COSYVOICE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


async def _synth_qwen3tts_pcm24k(text: str) -> bytes:
    """One-shot synthesis via Bailian's qwen3-tts-flash REST endpoint.
    Returns raw PCM 24 kHz mono s16le (decoded from the WAV the server
    returns). Returns empty bytes on failure.

    REST endpoint, model family: `qwen3-tts-flash` (Cherry voice).
    Differs from the CosyVoice WS path below in three ways:
      - synchronous (one HTTP call, full audio, no streaming)
      - server returns a JSON with `output.audio.url` pointing to a
        downloadable WAV file
      - model name is `qwen3-tts-flash`, voice is `Cherry`
    """
    import httpx

    settings = get_settings()
    t0 = time.perf_counter()
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    payload = {
        "model": settings.bailian_tts_model,
        "input": {"text": text},
        "parameters": {
            "voice": settings.bailian_tts_voice,
            "format": "wav",  # server returns WAV at 24 kHz
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            logger.warning("Bailian TTS HTTP {}: {}", r.status_code, r.text[:300])
            return b""
        body = r.json()
        audio_url = body.get("output", {}).get("audio", {}).get("url")
        if not audio_url:
            logger.warning("Bailian TTS no audio URL: {}", body)
            return b""

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            r2 = await client.get(audio_url)
        wav = r2.content
        if not wav or len(wav) < 100:
            logger.warning("Bailian TTS audio download empty/short: {}B", len(wav))
            return b""
    except Exception as e:
        logger.warning("Bailian TTS REST error: {}", e)
        return b""

    # Decode the WAV (ffmpeg handles the 24 kHz → 24 kHz PCM s16le decode
    # trivially; rate is unchanged, we just strip the header).
    pcm = _wav_to_pcm24k(wav)
    elapsed = time.perf_counter() - t0
    seconds = len(pcm) / 2 / 24000
    logger.info(
        "Bailian TTS ({}/{}) {:.2f}s → {} bytes ({:.2f}s @ 24kHz)",
        settings.bailian_tts_model, settings.bailian_tts_voice,
        elapsed, len(pcm), seconds,
    )
    return pcm


async def _synth_cosyvoice_pcm24k(text: str) -> bytes:
    """One-shot synthesis via CosyVoice WebSocket. Returns raw PCM 24 kHz
    mono s16le (no WAV header). Returns empty bytes on failure.

    Uses the Bailian inference WebSocket protocol — same shape as Fun-ASR
    (run-task / finish-task / result-generated), but with `task=tts` and
    `function=SpeechSynthesizer`.
    """
    settings = get_settings()
    if not settings.dashscope_api_key:
        return b""

    import websockets  # local import keeps it optional

    t0 = time.perf_counter()
    task_id = f"ca-{int(time.time() * 1000)}"
    pcm_chunks: list[bytes] = []
    headers = {"Authorization": f"Bearer {settings.dashscope_api_key}"}
    async with _COSYVOICE_SEMAPHORE:
        try:
            async with websockets.connect(
                COSYVOICE_WS_URL, additional_headers=headers, max_size=10 * 1024 * 1024
            ) as ws:
                await ws.send(json.dumps({
                    "header": {"action": "run-task", "task_id": task_id, "streaming": True},
                    "payload": {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": settings.bailian_tts_model,
                        "input": {"text": text},
                        "parameters": {
                            "voice": settings.bailian_tts_voice,
                            "format": "pcm",
                            "sample_rate": 24000,
                        },
                    },
                }))
                async for raw in ws:
                    msg = json.loads(raw)
                    ev = msg.get("header", {}).get("event") or msg.get("event")
                    if ev == "result-generated":
                        out = msg.get("payload", {}).get("output", {})
                        if out.get("data"):
                            pcm_chunks.append(base64.b64decode(out["data"]))
                        if out.get("finished"):
                            break
                    elif ev == "task-finished" or ev == "task-failed":
                        break
        except Exception as e:
            logger.warning("CosyVoice WS error: {}", e)
            return b""

    pcm = b"".join(pcm_chunks)
    elapsed = time.perf_counter() - t0
    logger.info(
        "CosyVoice TTS ({}/{}) {:.2f}s → {} bytes ({:.2f}s @ 24kHz)",
        settings.bailian_tts_model, settings.bailian_tts_voice,
        elapsed, len(pcm), len(pcm) / 2 / 24000,
    )
    return pcm


async def _stream_cosyvoice_to_pcm24k(text: str) -> AsyncIterator[bytes]:
    """Stream PCM 24 kHz chunks from CosyVoice WS. Yields as soon as each
    chunk arrives (typically first chunk in ~200 ms)."""
    settings = get_settings()
    if not settings.dashscope_api_key:
        return
    import websockets

    t0 = time.perf_counter()
    task_id = f"ca-{int(time.time() * 1000)}"
    headers = {"Authorization": f"Bearer {settings.dashscope_api_key}"}
    first_chunk_at: Optional[float] = None
    async with _COSYVOICE_SEMAPHORE:
        try:
            async with websockets.connect(
                COSYVOICE_WS_URL, additional_headers=headers, max_size=10 * 1024 * 1024
            ) as ws:
                await ws.send(json.dumps({
                    "header": {"action": "run-task", "task_id": task_id, "streaming": True},
                    "payload": {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": settings.bailian_tts_model,
                        "input": {"text": text},
                        "parameters": {
                            "voice": settings.bailian_tts_voice,
                            "format": "pcm",
                            "sample_rate": 24000,
                        },
                    },
                }))
                async for raw in ws:
                    msg = json.loads(raw)
                    ev = msg.get("header", {}).get("event") or msg.get("event")
                    if ev == "result-generated":
                        out = msg.get("payload", {}).get("output", {})
                        if out.get("data"):
                            chunk = base64.b64decode(out["data"])
                            if first_chunk_at is None:
                                first_chunk_at = time.perf_counter()
                            yield chunk
                        if out.get("finished"):
                            break
                    elif ev == "task-finished" or ev == "task-failed":
                        break
        except Exception as e:
            logger.warning("CosyVoice stream WS error: {}", e)
            return

    if first_chunk_at is not None:
        logger.info(
            "CosyVoice stream TTS first-chunk {:.3f}s (total {:.2f}s)",
            first_chunk_at - t0, time.perf_counter() - t0,
        )


async def _synth_bailian(text: str) -> bytes:
    """Back-compat: WAV-wrapped 24 kHz PCM. Kept for any external callers
    that still expect a WAV file from the legacy `_synth_bailian` entry point."""
    pcm = await _synth_cosyvoice_pcm24k(text)
    return _pcm24k_to_wav(pcm)


# ---------------------------------------------------------------------------
# Edge TTS
# ---------------------------------------------------------------------------

async def _synth_edge(text: str, *, voice: str) -> bytes:
    import edge_tts

    t0 = time.perf_counter()
    communicate = edge_tts.Communicate(text, voice=voice, rate="+0%", volume="+0%")
    buf = bytearray()
    async for ev in communicate.stream():
        if ev.get("type") == "audio":
            buf.extend(ev["data"])
    logger.info("Edge TTS ({}) {:.2f}s → {} bytes", voice, time.perf_counter() - t0, len(buf))
    return bytes(buf)


async def _stream_edge_to_pcm24k(text: str, *, voice: str) -> AsyncIterator[bytes]:
    """Stream raw PCM 24 kHz chunks from Edge TTS (decoded from MP3 in flight)."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice, rate="+0%", volume="+0%")
    async for ev in communicate.stream():
        if ev.get("type") != "audio":
            continue
        mp3_chunk = ev["data"]
        if not mp3_chunk:
            continue
        pcm_chunk = _mp3_to_pcm24k(mp3_chunk)
        if pcm_chunk:
            yield pcm_chunk


async def _stream_edge_to_mulaw(text: str, *, voice: str) -> AsyncIterator[bytes]:
    """Stream μ-law chunks directly. Yields a small frame every ~50-100 ms."""
    async for pcm_chunk in _stream_edge_to_pcm24k(text, voice=voice):
        target = 1920 * 2  # 80 ms at 24 kHz s16le mono
        for off in range(0, len(pcm_chunk), target):
            yield _pcm24k_to_mulaw8k(pcm_chunk[off:off + target])


# ---------------------------------------------------------------------------
# OpenAI TTS
# ---------------------------------------------------------------------------

async def _synth_openai(text: str) -> bytes:
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    t0 = time.perf_counter()
    resp = await client.audio.speech.create(
        model=settings.openai_tts_model,
        voice=settings.openai_tts_voice,
        input=text,
        response_format="mp3",
        speed=1.0,
    )
    data = resp.read()
    logger.info("OpenAI TTS ({}) {:.2f}s → {} bytes", settings.openai_tts_voice, time.perf_counter() - t0, len(data))
    return data


# ---------------------------------------------------------------------------
# Audio conversion (ffmpeg for MP3 decode; audioop for everything else)
# ---------------------------------------------------------------------------

def _get_ffmpeg() -> Optional[str]:
    """Resolve ffmpeg path. Order: FFMPEG_BINARY env, imageio-ffmpeg, then PATH."""
    p = __import__("os").environ.get("FFMPEG_BINARY")
    if p and __import__("os").path.exists(p):
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _mp3_to_pcm24k(mp3: bytes) -> bytes:
    ffmpeg = _get_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found. Install via `uv add imageio-ffmpeg` (and it will be "
            "resolved from the venv) or set FFMPEG_BINARY env."
        )
    proc = subprocess.run(
        [ffmpeg, "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1", "pipe:1"],
        input=mp3, capture_output=True, check=True,
    )
    return proc.stdout


def _wav_to_pcm24k(wav: bytes) -> bytes:
    """Same as _mp3_to_pcm24k but for WAV input. ffmpeg auto-detects from header."""
    return _mp3_to_pcm24k(wav)


def _pcm24k_to_pcm16k(pcm: bytes) -> bytes:
    """24 kHz mono s16le → 16 kHz mono s16le. No ffmpeg — stdlib audioop."""
    import audioop
    pcm16k, _ = audioop.ratecv(pcm, 2, 1, 24000, 16000, None)
    return pcm16k


def _pcm24k_to_mulaw8k(pcm: bytes) -> bytes:
    """24 kHz mono s16le → 8 kHz mono μ-law (Twilio Media Stream format)."""
    import audioop
    pcm8k, _ = audioop.ratecv(pcm, 2, 1, 24000, 8000, None)
    return audioop.lin2ulaw(pcm8k, 2)


def _pcm24k_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM 24 kHz mono s16le in a WAV header. For back-compat with
    `_synth_bailian` callers that expect a WAV file.
    """
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return buf.getvalue()
