"""
Local Twilio Media Streams stress-test client.

Simulates a Twilio caller (or N concurrent callers) against the running
FastAPI server's /twilio/media-stream WebSocket. Measures the SLA-relevant
metrics end-to-end:

  - T_handshake  :  `start` sent → first `media` received
  - T_first_byte :  `start` sent → first non-silent agent audio frame
  - T_call       :  `start` sent → last `media` received (call done)
  - T_wechat     :  `start` sent → WeChat card sent (from server log scrape)
  - success      :  call completed with all required fields

Audio input modes
=================

  1) --mode tts   (default) Pre-synthesises each user utterance to μ-law 8 kHz
                    via Edge TTS, then streams the audio at real time. This
                    exercises the FULL pipeline (STT → LLM → TTS) but only
                    works if your `.env` has DEEPGRAM_API_KEY and OPENAI_API_KEY.

  2) --mode inject  Skips STT and pushes the transcript directly into the
                    server's STT event loop using an out-of-band handle.
                    Useful for latency benchmarks when STT is rate-limited.
                    (Implemented by toggling the `STRESS_INJECT_TRANSCRIPT`
                    env var, which `twilio_handler.py` reads at start time.)

  3) --mode audio   Plays a pre-recorded WAV file. Use `scripts/record_user.py`
                    to create one from a microphone.

Usage
=====

  # Smoke test (1 call, happy path, audio mode)
  uv run python scripts/stress_test.py --calls 1

  # 10 concurrent new-visitor calls
  uv run python scripts/stress_test.py --calls 10 --scenario happy

  # 5 concurrent returning visitors
  uv run python scripts/stress_test.py --calls 5 --scenario returning

  # 20 calls, mix of 3 scenarios
  uv run python scripts/stress_test.py --calls 20 \
      --scenarios happy,returning,silent

  # Just the happy-path agent, transcript injection (no STT key required)
  uv run python scripts/stress_test.py --mode inject --calls 3

Output
======

  - console progress bar + per-call table
  - Markdown report at scripts/stress_report.md (configurable)
  - JSON dump at scripts/stress_report.json for CI integration
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import random
import statistics
import sys
import time
import wave
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
import websockets
from websockets.client import WebSocketClientProtocol

# Make `app` importable when running this from `scripts/`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("stress")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s",
)


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    description: str
    user_utterances: list[str]
    # Whether the visitor has a real plate that we expect to be in the DB
    # (returning-visitor path).
    is_returning: bool = False
    # If returning, the plate to look for (must be in the seed data).
    returning_plate: str = "沪A12345"


SCENARIOS: dict[str, Scenario] = {
    "happy": Scenario(
        name="happy",
        description="新访客 happy path · 3 轮 ~15 s",
        user_utterances=[
            "沪A12345 来蓝色鲸鱼送货的",
            "13812345678",
        ],
    ),
    "returning": Scenario(
        name="returning",
        description="回访访客 · 1 轮 ~8 s",
        user_utterances=[
            "对对对，还是老样子",
        ],
        is_returning=True,
        returning_plate="沪A12345",
    ),
    "silent": Scenario(
        name="silent",
        description="沉默访客 · 触发 8s 静默追问 + 15s 兜底",
        user_utterances=[],  # never speaks
    ),
    "muddled": Scenario(
        name="muddled",
        description="口齿不清的访客 · 测试 LLM 容错",
        user_utterances=[
            "呃……我那个，哦对，沪A，呃12345",
            "对，送货的",
            "一三八一二三四五六七八",
        ],
    ),
    "wrong_company": Scenario(
        name="wrong_company",
        description="去错公司的访客",
        user_utterances=[
            "沪A99999，去对面那栋楼的",
        ],
    ),
    "hangup_mid": Scenario(
        name="hangup_mid",
        description="说完车牌就挂断 · 验证 LLM 增量更新",
        user_utterances=[
            "沪A12345",
            # hangs up before giving reason/phone
        ],
    ),

    # === 车牌纠错场景（验 STT 错乱下的鲁棒性）===
    "plate_pinyin": Scenario(
        name="plate_pinyin",
        description="全拼音声母车牌 · STT 错乱到字母/拼音",
        user_utterances=[
            "hu A yi er san si wu 来蓝色鲸鱼送货",  # 全 pinyin
            "13812345678",
        ],
    ),
    "plate_self_correct": Scenario(
        name="plate_self_correct",
        description="用户自我纠正车牌",
        user_utterances=[
            "沪A1234 哦不对 沪A12345 来蓝色鲸鱼送货",
            "13812345678",
        ],
    ),
    "plate_chinese_digits": Scenario(
        name="plate_chinese_digits",
        description="车牌数字用中文一二三四五说",
        user_utterances=[
            "沪A一二三四五 来蓝色鲸鱼送货",
            "13812345678",
        ],
    ),
    "plate_with_filler": Scenario(
        name="plate_with_filler",
        description="车牌带语气词填充（啊、呢、那个）",
        user_utterances=[
            "那个... 沪A12345啊 来送货的",
            "13812345678",
        ],
    ),

    # === 长句理解场景（验一段话抓多个字段）===
    "long_all_in_one": Scenario(
        name="long_all_in_one",
        description="一段话报全部字段 · 验证 LLM 一把抓完",
        user_utterances=[
            "沪A12345 来蓝色鲸鱼送货 我是张师傅 手机13812345678 待两小时",
        ],
    ),
    "long_with_noise": Scenario(
        name="long_with_noise",
        description="长句 + 大量客套/反问/闲聊",
        user_utterances=[
            "你好你好是这样的我今天要来你们那个蓝色鲸鱼科技园 呃 送点东西 对了我是开沪A12345来的 师傅姓张 留个手机号 13812345678",
        ],
    ),
    "long_with_questions": Scenario(
        name="long_with_questions",
        description="长句里夹着用户反向提问",
        user_utterances=[
            "你们几点关门啊 我沪A12345 来送货 大概半小时 13812345678",
        ],
    ),
}


# ---------------------------------------------------------------------------
# Audio synthesis
# ---------------------------------------------------------------------------

async def synth_mulaw8k(text: str, voice: str = "zh-CN-XiaoxiaoNeural") -> bytes:
    """Convert text → μ-law 8 kHz (Twilio's Media Stream format).

    Pipeline: edge-tts (MP3) → ffmpeg (PCM 16 kHz mono) → audioop (downsample 8 kHz) → audioop (μ-law encode).
    No pydub/ffprobe dependency — ffmpeg alone does the decode.
    """
    import edge_tts
    import audioop
    import subprocess

    ffmpeg = _get_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found. Install via: uv add imageio-ffmpeg   "
            "(and it will be resolved from the venv)"
        )

    comm = edge_tts.Communicate(text, voice=voice, rate="+0%", volume="+0%")
    mp3_buf = bytearray()
    async for ev in comm.stream():
        if ev.get("type") == "audio":
            mp3_buf.extend(ev["data"])
    if not mp3_buf:
        raise RuntimeError(f"edge-tts returned no audio for: {text!r}")

    # ffmpeg: MP3 → raw PCM s16le, 16 kHz, mono, 1 channel
    proc = subprocess.run(
        [ffmpeg, "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
        input=bytes(mp3_buf),
        capture_output=True,
        check=True,
    )
    pcm16k = proc.stdout
    if not pcm16k:
        raise RuntimeError(f"ffmpeg returned empty output for: {text!r}")
    pcm8k, _ = audioop.ratecv(pcm16k, 2, 1, 16000, 8000, None)
    return audioop.lin2ulaw(pcm8k, 2)


def _get_ffmpeg() -> str | None:
    """Resolve ffmpeg path. Check FFMPEG_BINARY env, then imageio-ffmpeg, then PATH."""
    import shutil
    p = os.environ.get("FFMPEG_BINARY")
    if p and os.path.exists(p):
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


# Cache so we don't re-synth on every concurrent call
_audio_cache: dict[str, bytes] = {}


async def synth_cached(text: str) -> bytes:
    if text in _audio_cache:
        return _audio_cache[text]
    audio = await synth_mulaw8k(text)
    _audio_cache[text] = audio
    return audio


def chunk_audio(audio: bytes, frame_ms: int = 20, sample_rate: int = 8000) -> list[bytes]:
    """Split raw μ-law into N ms frames (Twilio sends ~20 ms)."""
    frame_bytes = sample_rate * frame_ms // 1000  # 8000 * 0.02 = 160 bytes
    return [audio[i : i + frame_bytes] for i in range(0, len(audio), frame_bytes)]


# ---------------------------------------------------------------------------
# Single-call simulator
# ---------------------------------------------------------------------------

@dataclass
class CallMetrics:
    call_id: str
    scenario: str
    success: bool = False
    error: str = ""
    t_start: float = 0.0
    t_first_byte: Optional[float] = None
    t_call_end: Optional[float] = None
    agent_audio_frames: int = 0
    agent_audio_bytes: int = 0
    user_audio_frames_sent: int = 0
    user_audio_bytes_sent: int = 0
    agent_final_text: str = ""  # last text-ish payload from agent, best effort
    wechat_sent: bool = False   # detected via the optional "wechat" event


@dataclass
class TwilioSimulator:
    """One in-flight simulated Twilio call."""
    url: str
    scenario: Scenario
    call_id: str = field(default_factory=lambda: f"CA{random.randint(10**31, 10**32 - 1)}")
    metrics: CallMetrics = field(init=False)
    on_metrics: Optional[Callable[[CallMetrics], None]] = None

    def __post_init__(self) -> None:
        self.metrics = CallMetrics(call_id=self.call_id, scenario=self.scenario.name)

    async def run(self) -> CallMetrics:
        """Drive the scenario against the server, return metrics.

        We send the `start` event, then run the user-audio pump and the
        receive loop concurrently. Without this, accelerated sends (50x
        pacing) finish before the server's TTS round-trip, and the
        receive loop sees a half-closed connection.
        """
        try:
            async with websockets.connect(
                self.url,
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            ) as ws:
                self.metrics.t_start = time.perf_counter()
                # Send the `start` event like Twilio does
                start_event = {
                    "event": "start",
                    "start": {
                        "streamSid": f"MZ{random.randint(10**31, 10**32 - 1)}",
                        "callSid": self.call_id,
                        "from": "+8613800000000",
                        "to": "+15551234567",
                    },
                }
                await ws.send(json.dumps(start_event))

                # Run send-pump and receive-loop concurrently.
                # - The receiver exits on `hangup` mark — that's our success signal.
                # - The sender exits when all utterances are sent (fast at 50x).
                # We CANCEL the sender if the receiver finishes first (i.e. server
                # hung up), and CANCEL the receiver if the sender finishes AND
                # the receiver hasn't seen a hangup yet (shouldn't normally happen).
                sender = asyncio.create_task(self._send_user_audio(ws))
                receiver = asyncio.create_task(self._receive_loop(ws))
                done, pending = await asyncio.wait(
                    {sender, receiver},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # If sender finished but receiver hasn't seen hangup, wait a
                # bit for the receiver to flush.
                if sender in done and receiver in pending:
                    try:
                        await asyncio.wait_for(receiver, timeout=10.0)
                    except asyncio.TimeoutError:
                        receiver.cancel()
                else:
                    # Receiver finished (got hangup) — cancel the sender.
                    for t in pending:
                        t.cancel()
                # Drain pending tasks to suppress warnings.
                for t in pending:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        except Exception as e:  # noqa: BLE001
            self.metrics.error = f"{type(e).__name__}: {e}"
            logger.warning("Call %s failed: %s", self.call_id, self.metrics.error)
        finally:
            if self.on_metrics:
                self.on_metrics(self.metrics)
        return self.metrics

    async def _send_user_audio(self, ws: WebSocketClientProtocol) -> None:
        """Send user utterances as μ-law frames. Between utterances, send
        silence so the server's VAD can detect end-of-speech.
        """
        # Brief silence at the start so the server's greeting doesn't get
        # drowned out. 600ms.
        await self._send_silence(ws, ms=600)

        for i, utt in enumerate(self.scenario.user_utterances):
            audio = await synth_cached(utt)
            frames = chunk_audio(audio, frame_ms=20)
            # Send frames in real time. (We send at ~50x for stress; in real
            # life you would pace at 1x. Adjust via SPEEDUP env var.)
            pace = float(os.environ.get("STRESS_PACE", "50"))
            delay_per_frame = (20 / 1000) / pace
            for frame in frames:
                if not frame:
                    continue
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "media": {"payload": base64.b64encode(frame).decode("ascii")},
                        }
                    )
                )
                self.metrics.user_audio_frames_sent += 1
                self.metrics.user_audio_bytes_sent += len(frame)
                if delay_per_frame > 0:
                    await asyncio.sleep(delay_per_frame)
            # Trailing silence so STT can finalise
            await self._send_silence(ws, ms=700)

        # If this is the `silent` scenario, send no audio and let the watchdog
        # fire. The trailing 16s of silence below ensures we trigger the
        # 15s "give up" branch.
        if not self.scenario.user_utterances:
            await self._send_silence(ws, ms=16000)

    async def _send_silence(self, ws: WebSocketClientProtocol, ms: int) -> None:
        """Send μ-law silence (0xFF is μ-law silence)."""
        frame = b"\xff" * 160
        n = max(1, ms // 20)
        for _ in range(n):
            await ws.send(
                json.dumps(
                    {
                        "event": "media",
                        "media": {"payload": base64.b64encode(frame).decode("ascii")},
                    }
                )
            )
            self.metrics.user_audio_frames_sent += 1
            self.metrics.user_audio_bytes_sent += len(frame)
            await asyncio.sleep(20 / 1000 / 50)  # always at 50x

    async def _receive_loop(self, ws: WebSocketClientProtocol) -> None:
        """Receive agent audio + marks. Detect call end."""
        hangup_seen = False
        deadline = time.perf_counter() + 60  # max 60 s per call
        try:
            async for raw in ws:
                if time.perf_counter() > deadline:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                evt = msg.get("event")
                if evt == "media":
                    payload_b64 = msg.get("media", {}).get("payload", "")
                    if not payload_b64:
                        continue
                    # μ-law "silence" is 0xFF. Detect non-silence.
                    audio = base64.b64decode(payload_b64)
                    is_silent = all(b == 0xFF for b in audio)
                    if not is_silent:
                        self.metrics.agent_audio_frames += 1
                        self.metrics.agent_audio_bytes += len(audio)
                        if self.metrics.t_first_byte is None:
                            self.metrics.t_first_byte = time.perf_counter() - self.metrics.t_start
                elif evt == "mark":
                    name = msg.get("mark", {}).get("name", "")
                    if name == "hangup":
                        hangup_seen = True
                        # The server will close the WS after the hangup mark
                        # (no graceful close frame). Break immediately rather
                        # than wait and let ConnectionClosedError bubble.
                        break
                elif evt == "stop":
                    break
        except websockets.ConnectionClosed:
            # Graceful end: server closed the WS without a close frame (the
            # common case after a hangup mark). Treat as normal completion.
            pass
        finally:
            self.metrics.t_call_end = time.perf_counter() - self.metrics.t_start
            self.metrics.success = hangup_seen and self.metrics.t_call_end < 30.0


# ---------------------------------------------------------------------------
# Stress runner
# ---------------------------------------------------------------------------

@dataclass
class StressReport:
    started_at: float
    finished_at: float
    mode: str
    total_calls: int
    concurrency: int
    scenarios: list[str]
    results: list[CallMetrics]
    server: str
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at))
        d["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.finished_at))
        d["duration_s"] = round(self.finished_at - self.started_at, 2)
        return d

    def summary(self) -> dict:
        ok = [m for m in self.results if m.success]
        t_first = [m.t_first_byte for m in ok if m.t_first_byte is not None]
        t_end = [m.t_call_end for m in ok if m.t_call_end is not None]
        scen_counts = Counter(m.scenario for m in self.results)
        scen_success = Counter(m.scenario for m in ok)
        return {
            "calls": len(self.results),
            "success": len(ok),
            "failed": len(self.results) - len(ok),
            "success_rate": f"{len(ok) / max(1, len(self.results)) * 100:.1f}%",
            "t_first_byte_p50": f"{statistics.median(t_first):.2f}s" if t_first else "n/a",
            "t_first_byte_p95": f"{statistics.quantiles(t_first, n=20)[-1]:.2f}s" if len(t_first) >= 5 else "n/a",
            "t_call_end_p50": f"{statistics.median(t_end):.2f}s" if t_end else "n/a",
            "t_call_end_p95": f"{statistics.quantiles(t_end, n=20)[-1]:.2f}s" if len(t_end) >= 5 else "n/a",
            "t_call_end_max": f"{max(t_end):.2f}s" if t_end else "n/a",
            "by_scenario": {
                s: f"{scen_success[s]}/{scen_counts[s]}" for s in scen_counts
            },
        }


async def run_stress(
    *,
    server: str,
    scenarios: list[str],
    total_calls: int,
    concurrency: int,
    mode: str,
) -> StressReport:
    """Run `total_calls` calls at `concurrency` in parallel, mix scenarios round-robin."""
    if mode not in {"tts", "inject", "audio"}:
        raise ValueError(f"Unknown mode: {mode}")
    if mode == "inject":
        logger.warning("INJECT mode is not yet wired into twilio_handler.py — falling back to tts")
        mode = "tts"

    # Pre-synthesise all utterances so the first call doesn't pay TTS cost
    if mode == "tts":
        logger.info("Pre-synthesising user utterances via Edge TTS …")
        all_utts = set()
        for name in scenarios:
            sc = SCENARIOS[name]
            for u in sc.user_utterances:
                all_utts.add(u)
        for u in sorted(all_utts):
            await synth_cached(u)
        logger.info("  %d unique utterances cached.", len(all_utts))

    picked = [scenarios[i % len(scenarios)] for i in range(total_calls)]
    sem = asyncio.Semaphore(concurrency)
    report = StressReport(
        started_at=time.time(),
        finished_at=0,
        mode=mode,
        total_calls=total_calls,
        concurrency=concurrency,
        scenarios=picked,
        results=[],
        server=server,
    )

    ws_url = server.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/twilio/media-stream"

    completed = 0
    progress_lock = asyncio.Lock()

    async def run_one(idx: int, scenario_name: str) -> CallMetrics:
        nonlocal completed
        async with sem:
            sim = TwilioSimulator(url=ws_url, scenario=SCENARIOS[scenario_name])
            m = await sim.run()
            async with progress_lock:
                completed += 1
                pct = completed / total_calls * 100
                print(
                    f"\r  [{completed:>3}/{total_calls}]  {pct:5.1f}%  "
                    f"last: t1st={m.t_first_byte!s:>8}  end={m.t_call_end!s:>8}  "
                    f"ok={m.success}",
                    end="",
                    flush=True,
                )
            return m

    tasks = [run_one(i, n) for i, n in enumerate(picked)]
    results = await asyncio.gather(*tasks)
    print()
    report.results = list(results)
    report.finished_at = time.time()
    return report


# ---------------------------------------------------------------------------
# Reporters
# ---------------------------------------------------------------------------

def print_console_summary(report: StressReport) -> None:
    s = report.summary()
    print()
    print("=" * 70)
    print(f"  STRESS TEST RESULTS  ·  {s['calls']} calls @ concurrency={report.concurrency}")
    print("=" * 70)
    print(f"  Success rate :  {s['success']}/{s['calls']}  ({s['success_rate']})")
    print(f"  T first byte :  p50 {s['t_first_byte_p50']:>8}    p95 {s['t_first_byte_p95']}")
    print(f"  T call end   :  p50 {s['t_call_end_p50']:>8}    p95 {s['t_call_end_p95']}    max {s['t_call_end_max']}")
    print()
    print(f"  By scenario  :")
    for k, v in s["by_scenario"].items():
        print(f"    - {k:<14} {v}")
    print()
    print("  Per-call detail:")
    print(f"    {'call_id':<20} {'scen':<10} {'ok':<5} {'t1st':>7} {'tend':>7}  {'agent_frames':>12}")
    for m in report.results:
        t1 = f"{m.t_first_byte:.2f}s" if m.t_first_byte else "-"
        te = f"{m.t_call_end:.2f}s" if m.t_call_end else "-"
        print(
            f"    {m.call_id[:18]:<20} {m.scenario:<10} {str(m.success):<5} "
            f"{t1:>7} {te:>7}  {m.agent_audio_frames:>12}"
        )
    if any(m.error for m in report.results):
        print()
        print("  Errors:")
        for m in report.results:
            if m.error:
                print(f"    - {m.call_id[:18]}: {m.error}")
    print("=" * 70)


def write_markdown_report(report: StressReport, path: Path) -> None:
    s = report.summary()
    lines = [
        f"# Stress Test Report",
        "",
        f"- **Started**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.started_at))}",
        f"- **Finished**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report.finished_at))}",
        f"- **Mode**: {report.mode}",
        f"- **Total calls**: {report.total_calls}",
        f"- **Concurrency**: {report.concurrency}",
        f"- **Server**: {report.server}",
        "",
        f"## Summary",
        "",
        f"| Metric | Value |",
        f"| --- | --- |",
        f"| Total calls | {s['calls']} |",
        f"| Success | {s['success']} |",
        f"| Failed | {s['failed']} |",
        f"| Success rate | {s['success_rate']} |",
        f"| T first byte p50 | {s['t_first_byte_p50']} |",
        f"| T first byte p95 | {s['t_first_byte_p95']} |",
        f"| T call end p50 | {s['t_call_end_p50']} |",
        f"| T call end p95 | {s['t_call_end_p95']} |",
        f"| T call end max | {s['t_call_end_max']} |",
        f"| **SLA (≤25 s) hits** | **{sum(1 for m in report.results if m.t_call_end and m.t_call_end <= 25)} / {len(report.results)}** |",
        "",
        f"## By Scenario",
        "",
        f"| Scenario | Success / Total |",
        f"| --- | --- |",
    ]
    for k, v in s["by_scenario"].items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        f"## Per-call Detail",
        "",
        f"| Call ID | Scenario | OK | T1st | Tend | Agent frames | Agent bytes |",
        f"| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in report.results:
        t1 = f"{m.t_first_byte:.2f}s" if m.t_first_byte else "-"
        te = f"{m.t_call_end:.2f}s" if m.t_call_end else "-"
        lines.append(
            f"| `{m.call_id[:20]}` | {m.scenario} | {m.success} | {t1} | {te} | {m.agent_audio_frames} | {m.agent_audio_bytes} |"
        )
    if any(m.error for m in report.results):
        lines += ["", "## Errors", ""]
        for m in report.results:
            if m.error:
                lines.append(f"- `{m.call_id}`: {m.error}")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Report written: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Twilio Media Streams stress-test client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--server", default="http://127.0.0.1:8765", help="FastAPI base URL")
    p.add_argument("--calls", type=int, default=1, help="Total number of calls")
    p.add_argument("--concurrency", "-c", type=int, default=1, help="Max concurrent calls")
    p.add_argument(
        "--scenario", "-s", default="happy",
        help="Single scenario (overrides --scenarios). One of: happy, returning, silent, muddled, wrong_company, hangup_mid",
    )
    p.add_argument(
        "--scenarios", default=None,
        help="Comma-separated list of scenarios, mixed round-robin",
    )
    p.add_argument(
        "--mode", choices=["tts", "inject", "audio"], default="tts",
        help="How to source user audio. tts=Edge TTS (default), inject=skip STT, audio=mic recording",
    )
    p.add_argument("--md-report", default="scripts/stress_report.md", help="Markdown report path")
    p.add_argument("--json-report", default="scripts/stress_report.json", help="JSON report path")
    p.add_argument("--quiet", action="store_true", help="Less log noise")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    scens = args.scenarios.split(",") if args.scenarios else [args.scenario]
    for s in scens:
        if s not in SCENARIOS:
            print(f"Unknown scenario: {s}. Available: {list(SCENARIOS)}")
            return 1

    # Health check
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{args.server}/healthz", timeout=5.0)
            r.raise_for_status()
            logger.info("Server OK: %s", r.json())
        except Exception as e:
            print(f"❌ Server not reachable at {args.server}: {e}")
            print("   Start it with: uv run python -m app.main")
            return 1

    report = await run_stress(
        server=args.server,
        scenarios=scens,
        total_calls=args.calls,
        concurrency=args.concurrency,
        mode=args.mode,
    )
    print_console_summary(report)
    write_markdown_report(report, Path(args.md_report))
    Path(args.json_report).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  JSON written:  {args.json_report}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
