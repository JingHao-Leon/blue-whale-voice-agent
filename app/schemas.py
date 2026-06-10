"""
Visitor information captured during a single call.

We model the slots the LLM is supposed to extract. The Pydantic model also
serves as a single source of truth for:
- the JSON schema exposed to the LLM as function-call arguments
- the data we send to the WeChat webhook
- what we persist in SQLite
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# Plate number pattern: 省+字母+5 alphanumeric chars. We are intentionally
# strict here — anything that doesn't look like a real Chinese plate is kept
# as-is so the LLM can re-prompt the user.
PLATE_RE = re.compile(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,7}$")

# Chinese mobile: 1 + 10 digits. Landline is also acceptable. International
# format (+86 ...) is also accepted — the +86 prefix is stripped.
PHONE_RE = re.compile(r"^(?:\+?86)?1[3-9]\d{9}$|^\+?\d{7,12}$")


class VisitorInfo(BaseModel):
    """Slots the LLM extracts from a single call."""

    plate: Optional[str] = Field(default=None, description="车牌号")
    company: Optional[str] = Field(default=None, description="受访公司")
    reason: Optional[str] = Field(default=None, description="来访事由")
    contact_name: Optional[str] = Field(default=None, description="访客姓名（可选）")
    phone: Optional[str] = Field(default=None, description="手机号或固定电话")
    duration: Optional[str] = Field(default=None, description="预计停留时间")

    # Conversation metadata
    call_sid: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    is_returning: bool = False
    call_history: list["VisitorInfo"] = Field(default_factory=list)  # prior visits

    @field_validator("plate")
    @classmethod
    def _normalise_plate(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        # Strip spaces / dots but keep the original casing for the LLM to reason about.
        cleaned = v.upper().replace(" ", "").replace("·", "").replace("-", "")
        # Only normalise if it actually matches a valid Chinese plate. Otherwise
        # return as-is so the LLM can see the raw STT output (e.g. pinyin
        # "HU-A12345" before the user says "沪" clearly) and re-prompt.
        if PLATE_RE.match(cleaned):
            return cleaned
        return v

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        # Strip spaces, dashes, parens, and the +86 country code
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if cleaned.startswith("+86"):
            cleaned = cleaned[3:]
        if PHONE_RE.match(cleaned):
            return cleaned
        return v  # return as-is; the agent will politely ask to repeat

    def missing_required(self) -> list[str]:
        """Required slots for non-returning visitors."""
        return [
            name
            for name, val in (
                ("plate", self.plate),
                ("reason", self.reason),
                ("phone", self.phone),
            )
            if not val
        ]

    def is_complete(self, allow_returning_shortcut: bool = True) -> bool:
        if allow_returning_shortcut and self.is_returning and self.plate:
            # Returning visitors: we already know who they are, just need a quick confirm.
            return True
        return not self.missing_required()

    def summary(self) -> dict:
        """JSON-serializable summary used as the WS `done` message payload.

        Returns a flat dict of the six user-facing slots so the browser
        test page can render the final card without re-parsing the
        conversation. Datetime fields are ISO-formatted.
        """
        return {
            "plate": self.plate,
            "company": self.company,
            "reason": self.reason,
            "contact_name": self.contact_name,
            "phone": self.phone,
            "duration": self.duration,
            "is_returning": self.is_returning,
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "ended_at": (
                self.ended_at.isoformat() if self.ended_at else None
            ),
        }

    def to_wechat_card(self) -> str:
        """Markdown card we send to the WeChat group robot."""
        returning_tag = "♻️ 回访" if self.is_returning else "🆕 新访"
        lines = [
            f"## 🚗 新访客登记 {returning_tag}",
            "",
            f"**车牌号**：{self.plate or '未提供'}",
            f"**受访公司**：{self.company or '未提供'}",
            f"**来访事由**：{self.reason or '未提供'}",
            f"**访客姓名**：{self.contact_name or '-'}",
            f"**联系电话**：{self.phone or '-'}",
            # 预计停留 — removed in v1.15.18 per user request. The field
            # is still on VisitorInfo for any future feature that needs
            # it (history/duration reports), but it's no longer shown
            # on the gatekeeper-facing WeChat card.
        ]
        if self.call_history:
            lines.append("")
            lines.append("### 📋 历史来访")
            for prior in self.call_history[:5]:
                lines.append(
                    f"- {prior.started_at:%Y-%m-%d %H:%M} · "
                    f"{prior.reason or '-'} · {prior.company or '-'}"
                )
        if self.duration_seconds is not None:
            lines.append("")
            lines.append(f"_通话时长 {self.duration_seconds:.1f}s_")
        return "\n".join(lines)


VisitorInfo.model_rebuild()
