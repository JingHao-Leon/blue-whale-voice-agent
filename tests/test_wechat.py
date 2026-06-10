"""Test the WeChat webhook transport. We mock httpx to avoid hitting the network."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.schemas import VisitorInfo
from app.services import wechat


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def json(self) -> dict:
        return self._body


def _patched_httpx(responses: list[dict]):
    """Return a `patch("httpx.AsyncClient")` context manager that yields
    captured payloads and returns the given responses in order.
    """
    captured: dict = {}
    call_log: list = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, **kwargs):
            call_log.append(url)
            captured["last_url"] = url
            captured["last_json"] = json
            return _FakeResponse(responses.pop(0) if responses else {"errcode": 0, "errmsg": "ok"})

    return FakeClient, captured, call_log


@pytest.mark.asyncio
async def test_send_visitor_card_success(monkeypatch):
    monkeypatch.setattr(wechat.get_settings(), "wechat_webhook_url", "https://example.com/wh", raising=True)
    visitor = VisitorInfo(plate="沪A12345", reason="送货", phone="13812345678")
    FakeClient, captured, _ = _patched_httpx([{"errcode": 0, "errmsg": "ok"}])
    with patch("httpx.AsyncClient", FakeClient):
        result = await wechat.send_visitor_card(visitor)
    assert result == {"errcode": 0, "errmsg": "ok"}
    assert captured["last_url"] == "https://example.com/wh"
    assert captured["last_json"]["msgtype"] == "markdown"
    assert "沪A12345" in captured["last_json"]["markdown"]["content"]


@pytest.mark.asyncio
async def test_send_visitor_card_no_retry_on_auth_error(monkeypatch):
    """40001 is a hard error (auth) — should NOT retry."""
    monkeypatch.setattr(wechat.get_settings(), "wechat_webhook_url", "https://example.com/wh", raising=True)
    FakeClient, _, _ = _patched_httpx([{"errcode": 40001, "errmsg": "bad auth"}])
    with patch("httpx.AsyncClient", FakeClient):
        with pytest.raises(wechat.WeChatSendError):
            await wechat.send_visitor_card(VisitorInfo(plate="沪A12345", reason="送货", phone="13812345678"))


@pytest.mark.asyncio
async def test_send_text_no_url(monkeypatch):
    monkeypatch.setattr(wechat.get_settings(), "wechat_webhook_url", "", raising=True)
    with pytest.raises(wechat.WeChatSendError):
        await wechat.send_text("hi")
