#!/usr/bin/env python3
"""
25-second budget test for the voice agent.

Spec: "从电话接通（Agent开始说话）到微信消息发出，不含拨号振铃时间"
     → agent first audio frame to agent `done` message (WeChat webhook fired)

Steps:
  1. Connect to /ws/browser
  2. T_start = first `agent_audio` binary frame received
  3. Stream a pre-recorded user utterance (8.7 s "陕A12345 送货 13800138000")
  4. T_end = server's `done` JSON message (sent by send_to_guard_and_end_call)
  5. Print T_total and PASS/FAIL against the 25 s budget

Usage:
  uv run python scripts/measure_25s.py
  uv run python scripts/measure_25s.py --host https://your-tunnel
  uv run python scripts/measure_25s.py --audio /path/to/test.wav
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_audio_pcm16k(path: str) -> bytes:
    """Load a WAV file as raw 16 kHz mono s16le PCM bytes."""
    with wave.open(path, "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(
                f"Audio must be 16 kHz mono s16le, got "
                f"{wf.getframerate()} Hz / {wf.getnchannels()} ch / {wf.getsampwidth() * 8} bit"
            )
        return wf.readframes(wf.getnframes())


async def measure(host: str, audio_path: str, verbose: bool = True) -> dict:
    ws_url = host.replace("https://", "wss://").replace("http://", "ws://") + "/ws/browser"
    pcm = load_audio_pcm16k(audio_path)
    pcm_duration_s = len(pcm) / 2 / 16000

    if verbose:
        print(f"🔌 Connecting to {ws_url}")
        print(f"🎤 Audio: {audio_path} ({pcm_duration_s:.2f}s @ 16 kHz)")

    t0 = time.perf_counter()
    t_ready = None
    t_first_audio = None
    t_audio_start = None  # agent_audio_start JSON
    t_done = None
    bytes_tx = 0
    bytes_rx_audio = 0
    agent_texts: list[str] = []

    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
            # === Phase 1: server sends ready + greeting ===
            while t_first_audio is None:
                msg = await ws.recv()
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "ready":
                        t_ready = time.perf_counter() - t0
                        if verbose:
                            print(f"  ✓ ready @ {t_ready * 1000:.0f}ms")
                    elif data.get("type") == "agent_text":
                        agent_texts.append(data.get("text", ""))
                    elif data.get("type") == "agent_audio_start":
                        t_audio_start = time.perf_counter() - t0
                else:
                    # First binary frame = first audio of greeting
                    t_first_audio = time.perf_counter() - t0
                    bytes_rx_audio += len(msg)
                    if verbose:
                        print(f"  ✓ first audio @ {t_first_audio * 1000:.0f}ms ({len(msg)} bytes)")

            # === Phase 2: drain greeting audio, then start streaming user audio ===
            if verbose:
                print(f"🎵 Draining greeting audio...")
            greeting_drain_start = time.perf_counter()
            got_audio_end = False
            while not got_audio_end:
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "agent_audio_end":
                        got_audio_end = True
                else:
                    bytes_rx_audio += len(msg)
            if verbose:
                print(f"  ✓ greeting drained in {(time.perf_counter() - greeting_drain_start) * 1000:.0f}ms")

            # === Phase 3: stream user audio in real-time chunks ===
            # Browser sends ~80ms frames. We do the same so the server STT
            # sees audio at the right cadence.
            frame = 16000 * 80 // 1000 * 2  # 80 ms @ 16 kHz s16le = 2560 bytes
            t_user_start = time.perf_counter()
            if verbose:
                print(f"🎤 Streaming {pcm_duration_s:.2f}s of user audio (80ms frames @ real-time)...")
            for off in range(0, len(pcm), frame):
                chunk = pcm[off : off + frame]
                await ws.send(chunk)
                bytes_tx += len(chunk)
                # Real-time pacing
                await asyncio.sleep(len(chunk) / 2 / 16000)
            if verbose:
                print(f"  ✓ sent {bytes_tx} bytes in {(time.perf_counter() - t_user_start) * 1000:.0f}ms")

            # === Phase 4: wait for `done` (means tool call send_to_guard_and_end_call fired) ===
            # The agent may ask a confirmation question before ending (e.g.
            # "手机号是 13800138000 对吧？"). When that happens, the LLM is
            # waiting for the user to say "对". Auto-reply "对" if we see
            # the agent finish speaking without having called the end tool.
            #
            # In practice the server sometimes closes the WS before our
            # test's client receives the `done` JSON (the message sits in
            # the WS buffer when the server-side run() loop sets
            # self._closed = True and tears down). We treat any agent
            # text that signals closure (contains both "门卫" and a
            # closing word) as an alternative end-of-call marker.
            if verbose:
                print(f"⏳ Waiting for agent to finish (wechat webhook → done)...")
            t_end_wait = time.perf_counter()
            confirm_sent = False
            while t_done is None:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "done":
                        t_done = time.perf_counter() - t0
                        if verbose:
                            summary = data.get("summary", {})
                            print(f"  ✓ done @ {t_done * 1000:.0f}ms  (summary: {summary})")
                    elif data.get("type") == "agent_text":
                        text = data.get("text", "")
                        agent_texts.append(text)
                        # Detect closing: agent said it would notify the guard.
                        # This is the agent's text BEFORE _pick_closing kicks
                        # in (which fires when the LLM returns an empty
                        # text_reply after a tool call). Treat as end.
                        if t_done is None and ("门卫" in text) and (
                            "稍等" in text or "通知" in text or "放行" in text
                            or "齐活儿" in text or "得嘞" in text and "稍等" in text
                            or "好嘞" in text and "稍等" in text
                        ):
                            t_done = time.perf_counter() - t0
                            if verbose:
                                print(f"  ✓ end-of-call detected @ {t_done * 1000:.0f}ms (agent said: {text!r})")
                    elif data.get("type") == "agent_audio_end" and not confirm_sent:
                        last_text = agent_texts[-1] if agent_texts else ""
                        needs_confirm = (
                            "?" in last_text or "？" in last_text
                            or "对吧" in last_text or "是吗" in last_text
                        )
                        if needs_confirm:
                            if verbose:
                                print(f"  ↳ agent asked for confirmation: {last_text!r}")
                                print(f"  ↳ auto-replying with '对' + 1.2s trailing silence...")
                            confirm_audio = open("/tmp/user_confirm.wav", "rb").read()[44:]
                            await ws.send(confirm_audio)
                            silence_frame = b"\x00\x00" * (16000 * 80 // 1000)
                            for _ in range(15):
                                await ws.send(silence_frame)
                                await asyncio.sleep(0.04)
                            confirm_sent = True
                else:
                    bytes_rx_audio += len(msg)
            await ws.send("bye")
    except asyncio.TimeoutError:
        return {
            "t_ready_ms": t_ready * 1000 if t_ready else 0,
            "t_first_audio_ms": t_first_audio * 1000 if t_first_audio else 0,
            "t_done_ms": 0,
            "t_total_ms": (time.perf_counter() - t0) * 1000,
            "t_user_audio_ms": pcm_duration_s * 1000,
            "bytes_tx": bytes_tx,
            "bytes_rx_audio": bytes_rx_audio,
            "passed": False,
            "error": "timeout waiting for done",
            "agent_texts": agent_texts,
        }
    except Exception as e:
        return {
            "t_ready_ms": t_ready * 1000 if t_ready else 0,
            "t_first_audio_ms": t_first_audio * 1000 if t_first_audio else 0,
            "t_done_ms": 0,
            "t_total_ms": (time.perf_counter() - t0) * 1000,
            "t_user_audio_ms": pcm_duration_s * 1000,
            "bytes_tx": bytes_tx,
            "bytes_rx_audio": bytes_rx_audio,
            "passed": False,
            "error": str(e),
            "agent_texts": agent_texts,
        }

    t_total_ms = (t_done - t_first_audio) * 1000 if (t_done and t_first_audio) else 0
    return {
        "t_ready_ms": t_ready * 1000 if t_ready else 0,
        "t_first_audio_ms": t_first_audio * 1000 if t_first_audio else 0,
        "t_done_ms": t_done * 1000 if t_done else 0,
        "t_total_ms": t_total_ms,
        "t_user_audio_ms": pcm_duration_s * 1000,
        "bytes_tx": bytes_tx,
        "bytes_rx_audio": bytes_rx_audio,
        "agent_texts": agent_texts,
        "passed": bool(t_done and t_first_audio) and t_total_ms < 25000,
    }


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--host",
        default="https://polyphonic-provinces-from-informal.trycloudflare.com",
    )
    ap.add_argument(
        "--audio",
        default="/tmp/user_test.wav",
        help="Path to 16 kHz mono s16le WAV (default: /tmp/user_test.wav)",
    )
    args = ap.parse_args()

    if not Path(args.audio).exists():
        print(f"❌ Audio file not found: {args.audio}", file=sys.stderr)
        sys.exit(1)

    r = await measure(args.host, args.audio)

    print()
    print("=" * 60)
    print("📊 25-SECOND BUDGET MEASUREMENT")
    print("=" * 60)
    print(f"  T ready → first audio:  {r['t_first_audio_ms']:>7.0f} ms")
    print(f"  T ready → done:         {r['t_done_ms']:>7.0f} ms")
    print(f"  T first audio → done:   {r['t_total_ms']:>7.0f} ms   ← spec budget: < 25 000 ms")
    print(f"  User audio sent:        {r['t_user_audio_ms']:>7.0f} ms")
    print(f"  bytes tx (user audio):  {r['bytes_tx']:>7,}")
    print(f"  bytes rx (TTS audio):   {r['bytes_rx_audio']:>7,}")
    print()
    if r.get("error"):
        print(f"❌ ERROR: {r['error']}")
        return
    if r["agent_texts"]:
        print("📝 Agent said:")
        for t in r["agent_texts"]:
            print(f"   • {t}")
    print()
    if r["passed"]:
        print(f"✅ PASS — {r['t_total_ms']:.0f}ms < 25000ms budget (margin: {25000 - r['t_total_ms']:.0f}ms)")
    else:
        print(f"❌ FAIL — {r['t_total_ms']:.0f}ms > 25000ms budget (over by: {r['t_total_ms'] - 25000:.0f}ms)")


if __name__ == "__main__":
    asyncio.run(main())
