"""One-shot CLI to send a single message to the configured WeChat group robot.

Usage:
    python -m app.cli.wechat_test "🤖 Hello from Voice Agent"
"""
import asyncio
import sys

from app.logging import setup_logging
from app.services import wechat


async def main(text: str) -> int:
    setup_logging()
    try:
        resp = await wechat.send_text(text)
    except wechat.WeChatSendError as e:
        print(f"❌ {e}")
        return 1
    print(f"✅ Sent: {resp}")
    return 0


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "🤖 Voice Agent is up!"
    sys.exit(asyncio.run(main(msg)))
