#!/usr/bin/env python3
"""
Run the stress test with an ephemeral in-process server.

This is a wrapper that:
  1. Starts the FastAPI server in a daemon thread (with env defaults)
  2. Waits for /healthz
  3. Invokes the stress_test.py CLI logic
  4. Tears down on exit

Usage:
  uv run python scripts/stress.py            # 1 happy-path call
  uv run python scripts/stress.py -c 5       # 5 concurrent happy-path calls
  uv run python scripts/stress.py -c 5 -s happy,returning
  uv run python scripts/stress.py --no-server  # assume server already running
"""
import argparse
import asyncio
import os
import sys
import threading
import time
import urllib.request
import uvicorn
from pathlib import Path

# Make the project importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ffmpeg for pydub
try:
    import imageio_ffmpeg
    os.environ["FFMPEG_BINARY"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass


def start_server(host: str, port: int) -> threading.Thread:
    os.environ.setdefault("HOST", host)
    os.environ.setdefault("PORT", str(port))
    os.environ.setdefault("LOG_LEVEL", "INFO")
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/visitors.db")
    os.environ.setdefault("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
    os.environ.setdefault("PARK_NAME", "蓝色鲸鱼科技园")
    os.environ.setdefault("DEFAULT_COMPANY", "蓝色鲸鱼科技")
    os.environ.setdefault("GUARD_GROUP_NAME", "园区门卫通知群")

    def run():
        uvicorn.run("app.main:app", host=host, port=port, log_level="info", access_log=False)

    t = threading.Thread(target=run, daemon=True, name="voice-agent-server")
    t.start()
    return t


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run stress test against in-process server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--server", default=None, help="Override base URL (skips starting server)")
    p.add_argument("--calls", "-c", type=int, default=1, help="Total calls")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--scenario", "-s", default="happy")
    p.add_argument("--scenarios", default=None, help="Comma-separated mix")
    p.add_argument("--mode", choices=["tts", "inject", "audio"], default="tts")
    p.add_argument("--md-report", default="scripts/stress_report.md")
    p.add_argument("--json-report", default="scripts/stress_report.json")
    p.add_argument("--no-server", action="store_true", help="Don't start the server, assume one is running")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    base = args.server or f"http://{args.host}:{args.port}"

    if not args.no_server:
        print(f"⏳ Starting in-process server at {base} …")
        start_server(args.host, args.port)
        if not wait_for_server(f"{base}/healthz"):
            print(f"❌ Server didn't come up in 30s")
            return 1
        print(f"✅ Server healthy at {base}/healthz")
    else:
        print(f"⏩ Skipping server start; using existing {base}")

    # Now run the stress test
    from scripts.stress_test import (
        run_stress, print_console_summary, write_markdown_report,
    )
    from pathlib import Path

    scens = args.scenarios.split(",") if args.scenarios else [args.scenario]
    report = await run_stress(
        server=base,
        scenarios=scens,
        total_calls=args.calls,
        concurrency=args.concurrency,
        mode=args.mode,
    )
    print_console_summary(report)
    write_markdown_report(report, Path(args.md_report))
    Path(args.json_report).write_text(
        __import__("json").dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n📄 Markdown: {args.md_report}")
    print(f"📊 JSON:     {args.json_report}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
