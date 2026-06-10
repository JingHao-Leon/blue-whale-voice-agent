#!/usr/bin/env python3
"""
Browser-path stress test for the voice agent.

Connects N concurrent WebSocket clients to /ws/browser, measures
end-to-end latency for each. Skips actual STT (would need 16 kHz audio
+ a transcriber call per session which costs more than the test is
worth) — instead measures:

  - T_handshake   : WS connect → first `ready` from server
  - T_greeting    : first `ready` → last `agent_audio_end` for greeting
  - T_session     : WS connect → WS close

Audio output is captured for the first call to spot-check for 200 OK
or 5xx-style errors. Reports p50 / p95 latencies.

Usage:
  uv run python scripts/stress_browser.py --calls 5
  uv run python scripts/stress_browser.py --calls 20 --host https://...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class CallResult:
    idx: int
    handshake_ms: float
    greeting_ms: float
    total_ms: float
    bytes_received: int
    audio_chunks: int
    error: str = ""


async def run_one(idx: int, host: str) -> CallResult:
    """One WS session: open, wait for ready + greeting, close."""
    ws_url = host.replace("https://", "wss://").replace("http://", "ws://") + "/ws/browser"
    t0 = time.perf_counter()
    bytes_rx = 0
    audio_chunks = 0
    handshake_ms = 0.0
    greeting_ms = 0.0
    got_ready = False
    got_greeting_end = False
    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
            # Wait for ready
            while not got_ready:
                msg = await ws.recv()
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "ready":
                        got_ready = True
                        handshake_ms = (time.perf_counter() - t0) * 1000
                else:
                    bytes_rx += len(msg)
            # Wait for greeting audio to finish
            t_greet = time.perf_counter()
            while not got_greeting_end:
                msg = await ws.recv()
                if isinstance(msg, str):
                    data = json.loads(msg)
                    if data.get("type") == "agent_audio_end":
                        got_greeting_end = True
                        greeting_ms = (time.perf_counter() - t_greet) * 1000
                else:
                    audio_chunks += 1
                    bytes_rx += len(msg)
            # Send close
            await ws.send("bye")
    except Exception as e:
        return CallResult(
            idx=idx,
            handshake_ms=handshake_ms,
            greeting_ms=greeting_ms,
            total_ms=(time.perf_counter() - t0) * 1000,
            bytes_received=bytes_rx,
            audio_chunks=audio_chunks,
            error=str(e)[:200],
        )
    total_ms = (time.perf_counter() - t0) * 1000
    return CallResult(
        idx=idx,
        handshake_ms=handshake_ms,
        greeting_ms=greeting_ms,
        total_ms=total_ms,
        bytes_received=bytes_rx,
        audio_chunks=audio_chunks,
    )


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=10, help="Concurrent sessions to run")
    ap.add_argument("--host", default="https://polyphonic-provinces-from-informal.trycloudflare.com")
    args = ap.parse_args()

    print(f"🚀 Stress test: {args.calls} concurrent browser sessions against {args.host}")
    print(f"   endpoint: /ws/browser")
    print(f"   testing: handshake + greeting delivery (no STT/LLM cost)")
    print()

    t_start = time.perf_counter()
    results = await asyncio.gather(*[run_one(i + 1, args.host) for i in range(args.calls)])
    wall = time.perf_counter() - t_start

    ok = [r for r in results if not r.error]
    fail = [r for r in results if r.error]

    # Per-metric stats
    def stats(name: str, xs: list[float]) -> str:
        if not xs:
            return f"  {name:14s}  (no successful samples)"
        return (
            f"  {name:14s}  p50={pct(xs, 50):7.1f}ms  "
            f"p95={pct(xs, 95):7.1f}ms  "
            f"max={max(xs):7.1f}ms  "
            f"mean={statistics.mean(xs):7.1f}ms"
        )

    print(f"📊 Results — {len(ok)}/{args.calls} succeeded in {wall:.2f}s wall")
    print()
    print(stats("handshake (ms)", [r.handshake_ms for r in ok]))
    print(stats("greeting (ms)",  [r.greeting_ms for r in ok]))
    print(stats("total (ms)",     [r.total_ms for r in ok]))
    print()
    if fail:
        print(f"❌ Failed: {len(fail)}")
        for r in fail:
            print(f"   [{r.idx}] {r.error}")
    else:
        print("✅ All sessions passed")
    print()
    total_bytes = sum(r.bytes_received for r in ok)
    total_audio = sum(r.audio_chunks for r in ok)
    print(f"  total bytes rx : {total_bytes:,}  ({total_bytes/1024/1024:.2f} MB)")
    print(f"  total audio chunks: {total_audio:,}")


if __name__ == "__main__":
    asyncio.run(main())
