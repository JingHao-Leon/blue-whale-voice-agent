"""
Guard query agent (bonus feature from the take-home spec).

The guard can ask natural-language questions like:
   "本周一共多少访问车辆？"
   "什么时间段访问最多？"
   "张师傅这个月来了几次？"  (by name/contact_name, not plate)
   "送货的车辆有多少？"

We dispatch by keyword/heuristic to a small set of intent handlers that pull
aggregates from `app.sql` and produce a Chinese answer. The LLM is used as a
final rephraser so the answer reads naturally — never as the source of
numbers (so it can't hallucinate a count).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from openai import AsyncOpenAI

from app import sql
from app.config import get_settings
from app.logging import logger


class GuardQueryAgent:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.llm_base_url,
        )

    async def ask(self, question: str, *, history: list[dict] | None = None) -> str:
        question = question.strip()
        if not question:
            return "请描述您要查询的问题。"

        # 1. Detect intent + extract parameters
        intent, params = self._route(question)

        # 2. Pull numbers
        try:
            facts = await self._fetch_facts(intent, params)
        except Exception as e:  # noqa: BLE001
            logger.exception("guard query fact fetch failed: {}", e)
            return f"查询出错：{e}"

        if not facts:
            return f"没找到相关记录：{question}"

        # 3. Rephrase with LLM
        try:
            return await self._rephrase(question, intent, params, facts)
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM rephrase failed, returning raw facts: {}", e)
            return self._fallback_answer(intent, facts)

    # ------------------------------------------------------------------
    # Intent routing
    # ------------------------------------------------------------------

    def _route(self, q: str) -> tuple[str, dict]:
        ql = q.lower()

        # Window
        days = 7
        if "今天" in q:
            days = 1
        elif "本周" in q or "这周" in q:
            days = 7
        elif "本月" in q or "这个月" in q:
            days = 30
        elif "本季" in q or "这个季度" in q:
            days = 90
        m = re.search(r"近?(\d+)\s*天", q)
        if m:
            days = max(1, min(365, int(m.group(1))))

        # By name
        name = self._extract_name(q)
        if name and ("来了" in q or "来过" in q or "几次" in q or "多少次" in q or "多少回" in q):
            return "by_name", {"days": days, "name": name}

        # By plate (explicit "车牌" mention or 6+ alnum token starting with letter/digit)
        plate = self._extract_plate(q)
        if plate and ("来了" in q or "来过" in q or "几次" in q or "记录" in q):
            return "by_plate", {"days": days, "plate": plate}

        # Peak hour
        if "时间" in q and ("最多" in q or "高峰" in q or "几点" in q):
            return "peak_hour", {"days": days}

        # Total
        if re.search(r"一共|总共|多少[辆车访辆访人]|数量|统计", q):
            return "total", {"days": days}

        # By reason
        reason = self._extract_reason(q)
        if reason:
            return "by_reason", {"days": days, "reason": reason}

        # Default: free-form → LLM chooses, with full stats as context
        return "freeform", {"days": days, "raw": q}

    @staticmethod
    def _extract_name(q: str) -> Optional[str]:
        # Chinese names: 2-3 chars preceding "师傅" / "总" / "老师" / "先生" / "女士"
        m = re.search(r"([\u4e00-\u9fa5]{2,3})(?:师傅|总|老师|先生|女士|同学|经理|主任)", q)
        return m.group(1) if m else None

    @staticmethod
    def _extract_plate(q: str) -> Optional[str]:
        m = re.search(
            r"([\u4e00-\u9fa5][A-Z][A-Z0-9]{5,7}|[A-Z]{1,2}[A-Z0-9]{5,7})",
            q.upper(),
        )
        return m.group(1) if m else None

    @staticmethod
    def _extract_reason(q: str) -> Optional[str]:
        for kw in ("送货", "面试", "维修", "拜访", "参观", "提货", "拉货", "洽谈"):
            if kw in q:
                return kw
        return None

    # ------------------------------------------------------------------
    # Fact fetch
    # ------------------------------------------------------------------

    async def _fetch_facts(self, intent: str, params: dict) -> dict:
        days = params["days"]
        if intent == "total":
            stats = await sql.stats(days=days)
            return {"total": stats["total_visits"], "unique": stats["unique_plates"]}

        if intent == "peak_hour":
            stats = await sql.stats(days=days)
            return {"peak_hour": stats["peak_hour"], "by_hour": stats["by_hour"]}

        if intent == "by_reason":
            stats = await sql.stats(days=days)
            return {
                "by_reason": stats["by_reason"],
                "match": next(
                    (r for r in stats["by_reason"] if params["reason"] in (r["reason"] or "")),
                    None,
                ),
            }

        if intent == "by_plate":
            rows = await sql.plate_history(params["plate"], days=max(days, 90))
            return {"rows": rows}

        if intent == "by_name":
            # We don't index by name; just scan recent
            rows = await sql.list_recent(days=days, limit=500)
            matched = [r for r in rows if (r.get("contact_name") or "") == params["name"]]
            return {"rows": matched, "name": params["name"]}

        # freeform
        return {"stats": await sql.stats(days=days), "recent": await sql.list_recent(days=days, limit=20)}

    # ------------------------------------------------------------------
    # Rephrase
    # ------------------------------------------------------------------

    async def _rephrase(self, question: str, intent: str, params: dict, facts: dict) -> str:
        prompt = (
            "你是园区门卫的查询助手。下面是从数据库读出的原始事实，请用 1-2 句自然中文回答用户的问题。\n"
            "严格要求：\n"
            "1. 数字必须完全基于 `facts`，不要编造。\n"
            "2. 没有数据就直说「没有记录」。\n"
            "3. 回答尽量简洁，最多两句。\n"
            f"\n用户问题：{question}\n"
            f"事实：{facts}\n"
        )
        resp = await self.client.chat.completions.create(
            model=self.settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        return (resp.choices[0].message.content or "").strip()

    @staticmethod
    def _fallback_answer(intent: str, facts: dict) -> str:
        if intent == "total":
            return f"过去 {facts.get('days', '?')} 天内共有 {facts.get('total', 0)} 次访问，{facts.get('unique', 0)} 辆不同车辆。"
        if intent == "peak_hour":
            ph = facts.get("peak_hour") or {}
            return f"访问高峰是 {ph.get('hour')}:00（{ph.get('visits', 0)} 次）。"
        if intent == "by_reason":
            m = facts.get("match")
            if not m:
                return f"近 {facts.get('days', 7)} 天没有 '{facts.get('reason')}' 相关的访问记录。"
            return f"近 {facts.get('days', 7)} 天 '{facts.get('reason')}' 共 {m['visits']} 次。"
        if intent == "by_plate":
            rows = facts.get("rows", [])
            if not rows:
                return f"车牌 {facts.get('plate')} 近 90 天没有访问记录。"
            return f"车牌 {facts.get('plate')} 近 90 天来了 {len(rows)} 次，最近一次是 {rows[0].get('started_at', '')}，事由 {rows[0].get('reason', '-')}。"
        if intent == "by_name":
            rows = facts.get("rows", [])
            return f"{facts.get('name', '')} 近 {facts.get('days', 7)} 天来了 {len(rows)} 次。"
        return "查询完成。"
