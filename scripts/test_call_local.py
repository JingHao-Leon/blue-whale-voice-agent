"""Local dev: simulate a call without Twilio. Drives the agent with text input."""
import asyncio
import sys

from app.agent import VisitorLLMAgent
from app.config import get_settings
from app.logging import setup_logging
from app.schemas import VisitorInfo


async def main() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.has_openai:
        print("ERROR: OPENAI_API_KEY is required for this local demo.")
        sys.exit(1)

    visitor = VisitorInfo(call_sid="LOCAL-DEMO", started_at=__import__("datetime").datetime.utcnow())
    agent = VisitorLLMAgent(
        call_sid="LOCAL-DEMO",
        visitor_info=visitor,
    )

    # Simulated 3-turn conversation. Replace with `input()` for true interactive.
    script = [
        "您好，蓝色鲸鱼科技园，请讲一下您的车牌号、来哪家公司、什么事？",  # agent opening
        "沪A12345，来蓝色鲸鱼送货的。",                                       # user
        # agent reply + TTS simulated
    ]

    print("AGENT:", script[0])
    for user_text in script[1:]:
        print("\nUSER:", user_text)
        agent.push_user_utterance(user_text)
        reply = await agent.step()
        print("AGENT:", reply.text_to_speak)
        print("(would push to WeChat now)" if reply.is_final else "")
        if reply.is_final:
            break

    # Round 2
    print("\nUSER: 13812345678。")
    agent.push_user_utterance("13812345678。")
    reply = await agent.step()
    print("AGENT:", reply.text_to_speak)
    print("(would push to WeChat now)" if reply.is_final else "")

    print("\n--- final visitor info ---")
    print(agent.visitor.model_dump_json(indent=2, exclude_none=True))


if __name__ == "__main__":
    asyncio.run(main())
