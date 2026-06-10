"""
WeChat Work (企业微信) group robot integration.

Two things we send to the robot:
  1. Markdown card with visitor details  (used at end of call)
  2. A "callback" message containing the
     call_sid in the content list, so a guard reply (1/0) can be detected by
     a separate listener endpoint.

The webhook URL looks like:
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

No authentication, but the key is the secret. Keep it in `.env`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.logging import logger
from app.schemas import VisitorInfo


class WeChatSendError(RuntimeError):
    pass


async def send_visitor_card(visitor: VisitorInfo) -> dict[str, Any]:
    """Send a markdown card to the WeChat Work group robot.

    Returns the parsed JSON response from the WeChat API. Raises
    WeChatSendError on transport / non-zero errcode.
    """
    settings = get_settings()
    if not settings.has_wechat:
        raise WeChatSendError("WECHAT_WEBHOOK_URL not configured")

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": visitor.to_wechat_card(),
        },
    }
    return await _post(payload)


async def send_text(text: str, *, mentioned_user_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """Send a plain text message. Used for guard alerts, debug, etc."""
    settings = get_settings()
    if not settings.has_wechat:
        raise WeChatSendError("WECHAT_WEBHOOK_URL not configured")
    payload: dict[str, Any] = {
        "msgtype": "text",
        "text": {"content": text, "mentioned_list": mentioned_user_ids or []},
    }
    return await _post(payload)


async def send_markdown(content: str) -> dict[str, Any]:
    """Send a raw markdown message (the WeChat group robot supports a subset of Markdown)."""
    settings = get_settings()
    if not settings.has_wechat:
        raise WeChatSendError("WECHAT_WEBHOOK_URL not configured")
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    return await _post(payload)


async def _post(payload: dict[str, Any], *, max_retries: int = 3) -> dict[str, Any]:
    settings = get_settings()
    url = settings.wechat_webhook_url
    timeout = httpx.Timeout(10.0, connect=5.0)

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            body = resp.json()
            errcode = body.get("errcode", 0)
            if errcode == 0:
                logger.info(
                    "WeChat sent ({:.2f}s): {}",
                    time.perf_counter() - t0,
                    payload.get("msgtype"),
                )
                return body
            # Known retryable errors
            if errcode in (40001, 42001, 43004, 43005):  # auth-related
                logger.warning("WeChat auth error {}: {}", errcode, body)
                # No refresh-token flow for robots — fail fast
                raise WeChatSendError(f"wechat errcode={errcode} errmsg={body.get('errmsg')}")
            logger.warning("WeChat non-zero errcode {} (attempt {}/{}): {}", errcode, attempt, max_retries, body)
            last_exc = WeChatSendError(f"wechat errcode={errcode}")
        except httpx.HTTPError as e:
            logger.warning("WeChat transport error (attempt {}/{}): {}", attempt, max_retries, e)
            last_exc = e
        # Exponential backoff (small because we're on a phone call)
        await asyncio.sleep(0.4 * attempt)

    raise WeChatSendError(f"WeChat post failed after {max_retries} attempts: {last_exc}")
