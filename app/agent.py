"""
LLM agent with function calling.

We use OpenAI's Chat Completions API (works for any OpenAI-compatible endpoint,
including Azure, OpenRouter, local vLLM). The agent exposes three function tools
the model can call:

  1. update_visitor_info         - incremental slot fill
  2. send_to_guard_and_end_call  - the ONLY way to end the call
  3. (reserved)                  - guard_query (read-only) for the guard-facing agent

Why these tools:
  - `update_visitor_info` lets the model commit slots as soon as it hears them,
    not just at the end. This makes the conversation robust to bad audio or
    early hangups: even if the call drops mid-sentence, the slots heard so
    far are already captured.
  - `send_to_guard_and_end_call` is a single chokepoint that:
      * marks the visitor info complete
      * fires the WeChat webhook
      * schedules the Twilio hangup
    This guarantees the agent can never end the call without notifying the guard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import get_settings
from app.logging import logger
from app.prompts import build_system_prompt
from app.schemas import VisitorInfo
from app.tools import TOOLS_REGISTRY, ToolContext, tools_schema


# ---------------------------------------------------------------------------
# Agent event types
# ---------------------------------------------------------------------------

@dataclass
class AgentReply:
    """One turn of agent output."""

    text_to_speak: str  # what we TTS and play to the caller
    is_final: bool      # True means: stop the call after speaking
    visitor_info: VisitorInfo  # current snapshot


# ---------------------------------------------------------------------------
# VisitorLLMAgent
# ---------------------------------------------------------------------------

class VisitorLLMAgent:
    """A stateful agent for a single phone call.

    We keep the OpenAI conversation list in memory for the duration of the call.
    After hangup we drop it. (We also persist a transcript to SQLite for audit.)
    """

    def __init__(
        self,
        *,
        call_sid: str,
        visitor_info: VisitorInfo,
        returning_history: str = "",
        client: Optional[AsyncOpenAI] = None,
    ) -> None:
        self.settings = get_settings()
        self.client = client or AsyncOpenAI(
            api_key=self.settings.openai_api_key or "missing",
            base_url=self.settings.llm_base_url,
        )
        self.call_sid = call_sid
        self.visitor = visitor_info
        self.messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(
                    company=self.settings.default_company,
                    returning_history=returning_history,
                ),
            }
        ]
        self._turn_count = 0

    # -- public API --

    def push_user_utterance(self, text: str) -> None:
        """Add the caller's latest utterance to history."""
        if not text.strip():
            return
        self.messages.append({"role": "user", "content": text.strip()})
        self._turn_count += 1
        logger.info("[{}] USER: {}", self.call_sid, text)

    async def step(self) -> AgentReply:
        """One LLM call → one AgentReply.

        We use non-streaming for simplicity: the call is short (< 25s) and the
        latency hit is negligible (≈300ms) compared to TTS. If we ever need to
        stream we can switch to AsyncOpenAI's stream API later.

        For Qwen3.7-Max we pass `extra_body={"enable_thinking": False}` because
        (1) thinking mode on this model is **streaming-only** and
        (2) it adds 1-3 s of reasoning latency that destroys turn-end timing
        on a phone call. Voice agents need fast + correct, not deep + slow.
        """
        try:
            kwargs: dict[str, Any] = {
                "model": self.settings.llm_model,
                "messages": self.messages,
                "tools": tools_schema(),
                "tool_choice": "auto",
                "temperature": 0,  # Deterministic gatekeeper behaviour —
                # the same user input should always produce the same reply.
                # Was 0.4 which sometimes caused the LLM to re-say the
                # opening greeting or to ask for fields it had already
                # confirmed were collected.
                "max_completion_tokens": 200,
            }
            if "qwen3" in self.settings.llm_model:
                # DashScope OpenAI-compatible endpoint forwards extra_body to
                # the underlying Qwen model. Other models ignore unknown keys.
                kwargs["extra_body"] = {"enable_thinking": False}
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.exception("LLM call failed: {}", e)
            return AgentReply(
                text_to_speak="抱歉，系统有点忙，请稍后再拨。",
                is_final=True,
                visitor_info=self.visitor,
            )

        msg = response.choices[0].message
        text_reply = (msg.content or "").strip()
        tool_calls = msg.tool_calls or []

        # Record assistant turn in history
        self.messages.append(
            {
                "role": "assistant",
                "content": text_reply or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
                or None,
            }
        )

        # Process tool calls — dispatch via TOOLS_REGISTRY. Handlers mutate
        # `ctx.visitor` in place and set `ctx.is_final` / `ctx.summary` to
        # signal the call should hang up after this turn.
        is_final = False
        ctx = ToolContext(visitor=self.visitor)
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.warning("Bad tool args: {}", tc.function.arguments)
                args = {}

            tool = TOOLS_REGISTRY.get(tc.function.name)
            if tool is None:
                logger.warning("Unknown tool call: {}", tc.function.name)
                result = {"ok": False, "error": "unknown tool"}
            else:
                result = await tool.handler(args, ctx)
                # The end-call tool can override the spoken text via ctx.summary.
                if ctx.summary and tool.name == "send_to_guard_and_end_call":
                    text_reply = ctx.summary
                if ctx.is_final:
                    is_final = True

            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        # Fallback: model said goodbye without calling the end tool. We still
        # honour the user's intent and end the call (with a synthetic end-call).
        if not is_final and self._looks_like_goodbye(text_reply):
            is_final = self.visitor.is_complete()

        if not text_reply:
            # Some model versions return empty text when only tool calls. The
            # next turn will speak, so give a minimal bridge.
            if self.visitor.is_complete():
                text_reply = self._pick_closing()
                is_final = True
            else:
                text_reply = "好的，请继续。"

        # If the model produced the stilted default closing "已通知门卫，请稍等
        # 放行" (which the LLM defaults to about 80% of the time), replace with
        # a random variant so the conversation doesn't sound like a tape
        # recorder. Skip if the model already chose a more natural variant.
        if is_final and self.visitor.is_complete() and "请稍等放行" in text_reply:
            text_reply = self._pick_closing()

        # === Safety net: if all 4 required fields are already in the
        # visitor (plate, reason, phone, contact_name) but the LLM didn't
        # call send_to_guard_and_end_call and didn't say goodbye, force
        # the end. This catches the ~5% case where qwen3.6-flash
        # hallucinates "还差手机号" right after it just confirmed
        # "手机号13800138000 已记下" — the visitor info is authoritative,
        # not the LLM's self-report. Without this guard, the user hears
        # "good, plate X reason Y phone Z, name W. Still missing your
        # phone number." which is contradictory and the call never ends.
        if not is_final and self.visitor.is_complete():
            logger.info(
                "[{}] all 4 fields present ({}), forcing end despite LLM reply {!r}",
                self.call_sid,
                self.visitor.summary(),
                text_reply[:60],
            )
            text_reply = self._pick_closing()
            is_final = True

        # === Text-parser fallback (v1.15.13 behaviour: only runs when
        # not is_final; v1.15.14 moved it out of this guard so phone
        # got captured even when LLM said "请稍候放行", but the side
        # effect was a stuttering / over-talking LLM in some flows.
        # v1.15.16 reverts to the v1.15.13 behaviour — the user said
        # v1.15.13 was snappier and the v1.15.14/15 noise-floor logic
        # was making the system "识别出来但不回复" because the parser
        # was running on text-stable events and racing with the LLM.
        if not is_final:
            import re as _re
            from app.schemas import PLATE_RE as _PLATE_RE
            m_plate = _re.search(
                r"车牌[号为]?\s*"
                r"([\u4e00-\u9fa5][A-Z][A-Z0-9]{5,7})",
                text_reply,
            )
            if m_plate and not self.visitor.plate and _PLATE_RE.match(m_plate.group(1)):
                self.visitor.plate = m_plate.group(1)
                logger.info("[{}] text-parser extracted plate={}", self.call_sid, self.visitor.plate)
            phone_digits = _re.sub(r"\D", "", text_reply)
            m_phone = _re.search(r"(1[3-9]\d{9})", phone_digits)
            if m_phone and not self.visitor.phone:
                self.visitor.phone = m_phone.group(1)
                logger.info("[{}] text-parser extracted phone={}", self.call_sid, self.visitor.phone)
            m_reason = _re.search(r"事由[为是]?\s*([^，。\s、]{1,8})", text_reply)
            if m_reason and not self.visitor.reason:
                self.visitor.reason = m_reason.group(1)
                logger.info("[{}] text-parser extracted reason={}", self.call_sid, self.visitor.reason)
            m_name = _re.search(r"(?:受访人[是为]?\s*|师傅[为]?\s*|姓\s*)([一-龥]{1,3})", text_reply)
            if m_name and not self.visitor.contact_name:
                self.visitor.contact_name = m_name.group(1)
                logger.info("[{}] text-parser extracted name={}", self.call_sid, self.visitor.contact_name)
            if self.visitor.is_complete():
                logger.info(
                    "[{}] text-parser filled all 4 fields ({}), forcing end",
                    self.call_sid, self.visitor.summary(),
                )
                text_reply = self._pick_closing()
                is_final = True

        logger.info("[{}] AGENT: {} (final={})", self.call_sid, text_reply, is_final)
        return AgentReply(text_to_speak=text_reply, is_final=is_final, visitor_info=self.visitor)

    def _pick_closing(self) -> str:
        """Pick a natural Chinese gatekeeper closing at random.

        Prefers one of the 5 variable-free closings (cached in
        browser_session at boot → 37 ms playback instead of a 2-3 s
        TTS round-trip). Falls back to a templated one with the actual
        plate/reason/phone/contact injected for the rare cases where
        the LLM provided an empty text_reply and we want to echo
        back the user's specific data.

        The LLM defaults to "已通知门卫，请稍等放行" almost every time
        because the prompt's example uses that phrasing. Picking from
        a small bank of colloquial variants gives the user a different
        feel each call and breaks the "tape recorder" pattern.
        """
        # Variable-free closings (5) — prefer these 80% of the time so
        # the browser hits the pre-warmed TTS cache.
        cacheable = [
            "得嘞了，门卫马上放行，您开到闸口那儿就行。",
            "齐活儿了，门卫那头我打声招呼，您开到门口会有人接您。",
            "OK 收到，这就通知门卫，您稍等片刻。",
            "嗯嗯，门卫已经知道了，您开到门口跟前就行。",
            "行嘞，发门卫了啊，您开到门口就行。",
        ]
        # Templated closings (with visitor data) — used 20% of the time
        # so the user still hears their plate/phone echoed back sometimes.
        v = self.visitor
        plate = v.plate or "车牌"
        reason_phrase = (
            "送货" if v.reason and "送" in v.reason else (v.reason or "办事")
        )
        contact = f"、{v.contact_name}师傅" if v.contact_name else ""
        phone = v.phone or ""
        templated = [
            f"好嘞，{plate}、蓝色鲸鱼{reason_phrase}{contact}、{phone}——齐活儿了，这就给您发门卫啊。",
            f"好了啊{contact}，门卫这就放行，您稍等。",
        ]
        import random as _r
        if _r.random() < 0.8:
            return _r.choice(cacheable)
        return _r.choice(templated)

    def transcript(self) -> str:
        """Render the conversation as a simple plain-text transcript."""
        lines = []
        for m in self.messages:
            role = m.get("role", "")
            if role == "system":
                continue
            if role == "user":
                lines.append(f"用户：{m['content']}")
            elif role == "assistant":
                content = m.get("content") or ""
                if content:
                    lines.append(f"门卫：{content}")
                for tc in m.get("tool_calls") or []:
                    fn = tc["function"]["name"]
                    args = tc["function"]["arguments"]
                    lines.append(f"  [tool_call] {fn}({args})")
            elif role == "tool":
                lines.append(f"  [tool_result] {m['content'][:120]}")
        return "\n".join(lines)

    # -- internals --

    @staticmethod
    def _looks_like_goodbye(text: str) -> bool:
        if not text:
            return False
        markers = ("请稍等放行", "已通知门卫", "欢迎光临", "再见", "挂了", "好了，再见", "祝您一路平安")
        return any(m in text for m in markers)
