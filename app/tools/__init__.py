"""
Function-calling tools exposed to the LLM.

Each tool is a small dataclass-shaped bundle:

  - `name`        : the function name the model invokes
  - `description` : the system-facing description (becomes the LLM's
                    primary instruction for when to call the tool)
  - `parameters`  : JSON Schema dict for the arguments
  - `handler`     : async (args: dict, ctx: ToolContext) -> dict
                    applied against the agent's state. The handler
                    returns a dict that becomes the tool-call result
                    sent back to the LLM.

The agent's `step()` method looks up the handler by name and dispatches.
Adding a new tool is two steps:
  1. write a `Tool(...)` instance below
  2. add it to `TOOLS_REGISTRY` (or `TOOLS_SCHEMA` for the LLM-facing list)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.schemas import VisitorInfo


# ---------------------------------------------------------------------------
# ToolContext: what every tool handler gets to operate on
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call mutable state passed to every tool handler.

    The agent owns the underlying objects; tools mutate `visitor` in place
    and use `is_final` to signal the call should hang up after this turn.
    `summary` lets a tool override the LLM's text reply (used by the end-call
    tool to set the final farewell sentence).
    """

    visitor: VisitorInfo
    is_final: bool = False
    summary: Optional[str] = None  # when set, overrides the LLM's text_reply


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    @property
    def schema(self) -> dict[str, Any]:
        """The OpenAI function-calling JSON schema entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# Handler: update_visitor_info
# ---------------------------------------------------------------------------


async def _handle_update_visitor_info(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Incrementally update visitor info slots.

    Plate is run through `normalize_plate` as a defensive backstop — the
    system prompt does the heavy lifting (pinyin → Chinese char, self-
    correction), but a few edge cases slip through and we don't want them
    to reach the WeChat card. Phone is normalised by VisitorInfo's field
    validator.

    REJECTION: if the plate doesn't match the final pattern (province
    Chinese char + city letter + 5-7 alphanumeric) after normalisation,
    we DON'T write it to the visitor and we return an error string. The
    LLM is then expected to ask the user to re-confirm. Without this
    guard, a STT-garbled partial like "V12345" (missing the province)
    would be persisted to the DB and pushed to the WeChat group, leaving
    the guard wondering why every car today is registered to a
    letter-only "province".
    """
    # Local import to avoid a cycle: app.plate imports app.config which
    # doesn't pull in tools, but we keep this lazy for clarity.
    from app.plate import normalize_plate
    from app.schemas import PLATE_RE

    rejected: list[str] = []
    for field in ("plate", "company", "reason", "contact_name", "phone", "duration"):
        v = args.get(field)
        if not v:
            continue
        if field == "plate" and isinstance(v, str):
            normalised = normalize_plate(v)
            if normalised and normalised != v:
                v = normalised
            # Reject plates that don't look like a valid Chinese plate.
            # normalize_plate passes the raw value through if it can't
            # normalise, so we must validate HERE.
            if not PLATE_RE.match(v):
                rejected.append(
                    f"plate={v!r} doesn't look like a valid Chinese plate "
                    f"(need province + letter + 5-7 alphanumeric, e.g. 沪A12345). "
                    f"Ask the user to confirm."
                )
                continue
        setattr(ctx.visitor, field, v)

    # Re-run Pydantic validators by re-instantiating. Round-trip is cheap
    # (6 fields) and guarantees the post-update state passes schema rules.
    try:
        ctx.visitor = VisitorInfo.model_validate(ctx.visitor.model_dump())
    except Exception:  # noqa: BLE001
        # Pydantic sometimes rejects intermediate states (e.g. when a
        # partial update sets a slot that another validator conflicts
        # with). The slots we just set are already on the object; we
        # simply log on the agent's side and let the next turn re-validate.
        pass

    result = {
        "ok": not rejected,
        "current": ctx.visitor.model_dump(mode="json", exclude_none=True),
    }
    if rejected:
        result["errors"] = rejected
    return result


# ---------------------------------------------------------------------------
# Handler: send_to_guard_and_end_call
# ---------------------------------------------------------------------------


async def _handle_send_and_end(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The only way to end a call.

    The caller (agent.step) reads `ctx.is_final` and `ctx.summary` after
    the handler returns and threads them into the AgentReply. The handler
    itself just flips the flags and returns a confirmation blob.
    """
    ctx.is_final = True
    summary = args.get("summary")
    if summary:
        ctx.summary = summary
    return {"ok": True, "will_end_call": True}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOLS_REGISTRY: dict[str, Tool] = {
    "update_visitor_info": Tool(
        name="update_visitor_info",
        description=(
            "Incremental update of visitor info slots. Call this as soon as you hear "
            "ANY field. Do not wait until the end. You may call multiple times per turn."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plate": {
                    "type": "string",
                    "description": "车牌号，例：'沪A12345'。Normalise: uppercase, no spaces.",
                },
                "company": {
                    "type": "string",
                    "description": "受访公司，例：'蓝色鲸鱼科技'",
                },
                "reason": {
                    "type": "string",
                    "description": "来访事由，例：'送货'、'面试'、'拜访张总'",
                },
                "contact_name": {
                    "type": "string",
                    "description": "访客姓名（可选）",
                },
                "phone": {
                    "type": "string",
                    "description": "手机号或固定电话，digits only",
                },
                "duration": {
                    "type": "string",
                    "description": "预计停留时间，例：'2小时'、'半天'",
                },
            },
        },
        handler=_handle_update_visitor_info,
    ),
    "send_to_guard_and_end_call": Tool(
        name="send_to_guard_and_end_call",
        description=(
            "CRITICAL: Call this EXACTLY ONCE when all required slots are filled "
            "(or the visitor is a confirmed returning visitor). This is the ONLY way "
            "to end the call. Do not say goodbye to the user before calling this."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A short confirmation sentence you will speak, e.g. "
                    "'好的，沪A12345，蓝色鲸鱼送货，已通知门卫，请稍等放行。'",
                },
            },
            "required": ["summary"],
        },
        handler=_handle_send_and_end,
    ),
}


# ---------------------------------------------------------------------------
# LLM-facing schema list (the order is what the LLM sees; keep stable)
# ---------------------------------------------------------------------------


def tools_schema() -> list[dict[str, Any]]:
    """JSON-Schema list passed to chat.completions.create(tools=...)."""
    return [tool.schema for tool in TOOLS_REGISTRY.values()]


__all__ = [
    "Tool",
    "ToolContext",
    "ToolHandler",
    "TOOLS_REGISTRY",
    "tools_schema",
]
