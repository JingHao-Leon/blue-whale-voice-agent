"""Unit tests for the LLM agent. Run with: uv run pytest --unit (or pytest tests/)."""
from __future__ import annotations

import pytest

from app.agent import VisitorLLMAgent
from app.prompts import build_system_prompt
from app.schemas import VisitorInfo


def test_prompt_includes_company():
    p = build_system_prompt(company="蓝色鲸鱼")
    assert "蓝色鲸鱼" in p
    assert "25 秒" in p or "25秒" in p


def test_prompt_includes_returning_history():
    p = build_system_prompt(company="蓝色鲸鱼", returning_history="2025-05-12 送货")
    assert "is_returning: true" in p
    assert "2025-05-12 送货" in p


def test_visitor_info_validates_plate():
    v = VisitorInfo(plate="hu a12345")
    # Invalid plate is kept as-is so the LLM can re-prompt.
    assert v.plate == "hu a12345"


def test_visitor_info_phone_digits():
    v = VisitorInfo(phone="138 1234 5678")
    assert v.phone == "13812345678"


def test_visitor_info_missing_required():
    v = VisitorInfo(plate="沪A12345")
    assert "reason" in v.missing_required()
    assert "phone" in v.missing_required()
    assert "plate" not in v.missing_required()


def test_visitor_info_complete_new_visitor():
    v = VisitorInfo(plate="沪A12345", reason="送货", phone="13812345678")
    assert v.is_complete()
    assert not v.missing_required()


def test_visitor_info_returning_shortcut():
    v = VisitorInfo(plate="沪A12345", is_returning=True)
    # Returning visitors can complete with just a plate.
    assert v.is_complete()


def test_wechat_card_includes_required_fields():
    v = VisitorInfo(plate="沪A12345", reason="送货", phone="13812345678", company="蓝色鲸鱼")
    card = v.to_wechat_card()
    assert "沪A12345" in card
    assert "送货" in card
    assert "13812345678" in card
    assert "新访客登记" in card


def test_wechat_card_returning_tag():
    v = VisitorInfo(plate="沪A12345", reason="送货", is_returning=True)
    card = v.to_wechat_card()
    assert "♻️ 回访" in card


def test_agent_push_user_utterance():
    agent = VisitorLLMAgent(call_sid="T1", visitor_info=VisitorInfo(call_sid="T1"))
    agent.push_user_utterance("沪A12345")
    assert any(m.get("role") == "user" and "沪A12345" in m["content"] for m in agent.messages)


@pytest.mark.asyncio
async def test_agent_step_handles_missing_provider(monkeypatch):
    # Force the agent to use a dummy URL that will fail; we just want to
    # confirm graceful fallback (returns AgentReply, no crash).
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "openai_api_key", "test-key")
    monkeypatch.setattr(s, "llm_base_url", "http://127.0.0.1:1")  # unreachable

    agent = VisitorLLMAgent(
        call_sid="T-FAIL",
        visitor_info=VisitorInfo(call_sid="T-FAIL"),
    )
    agent.push_user_utterance("测一下")
    reply = await agent.step()
    # It should still produce *some* reply (graceful), even when the network fails.
    assert reply.text_to_speak
    assert reply.is_final  # error path marks final so we hang up politely
