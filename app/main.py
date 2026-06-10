"""
FastAPI application entry point.

Endpoints:
  GET  /healthz                 - liveness probe
  GET  /                        - marketing / status
  POST /twiml/incoming          - Twilio TwiML for inbound calls (returns <Connect><Stream>)
  WS   /twilio/media-stream     - bidirectional WebSocket for call audio
  GET  /api/visitors            - guard query: list recent visitors (JSON)
  GET  /api/visitors/stats      - guard query: stats summary
  POST /api/guard/chat          - guard query: natural language Q&A over visitor history
  POST /api/wechat/test         - send a test WeChat message (DEV only)
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi import UploadFile, File as FastAPIFile
from pathlib import Path
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import get_settings
from app.database import engine, init_db
from app.logging import logger, setup_logging
from app.services import wechat
from app.services.browser_session import BrowserSession
from app.services.guard_agent import GuardQueryAgent
from app.services.twilio_handler import TwilioCall
from app import sql as sql_module


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
    settings = get_settings()
    missing = settings.validate_for_call()
    if missing:
        logger.warning("⚠️  Some integrations are not configured:")
        for m in missing:
            logger.warning("   - {}", m)
        logger.warning("App will start, but real phone calls need: {}", ", ".join(missing))
    else:
        logger.info("✅ All integrations configured")
    logger.info("🚀 Voice agent started on http://{}:{}", settings.host, settings.port)

    # Warm browser-session TTS cache + greeting at boot so the first
    # user turn pays ~0 ms for the greeting (TTS is the slowest link).
    try:
        from app.services.browser_session import (
            BrowserSession, _warm_tts_cache, _synth_pcm16k,
        )
        await _warm_tts_cache()
        # Formal front-desk greeting variants. Each asks for all 4
        # required fields in one breath so the user can report everything
        # in a single utterance (avoids turn-by-turn interruption).
        # Random pick so each session sounds slightly different.
        import random as _g_r
        _greeting_variants = [
            f"您好，{settings.park_name}园区。请提供您的车牌号、来访事由、手机号和受访人姓名。",
            f"您好，{settings.park_name}园区。请问车牌、来访事由、联系电话分别是？",
            f"您好，{settings.park_name}园区。麻烦报一下车牌号、来访事由和手机号。",
        ]
        greeting = _g_r.choice(_greeting_variants)
        # Cap boot greeting synthesis at 10s; if TTS is slow (e.g. CosyVoice
        # rate-limited) we still let the server start and synthesise the
        # greeting on the first session open.
        try:
            pcm = await asyncio.wait_for(_synth_pcm16k(greeting), timeout=10.0)
        except Exception:
            pcm = b""
        if pcm:
            # Stash on the class so session.run() can use it instantly.
            BrowserSession._BOOT_GREETING_TEXT = greeting
            BrowserSession._BOOT_GREETING_PCM = pcm
            logger.info("🌐 boot greeting pre-synth: {} bytes", len(pcm))
    except Exception as e:  # noqa: BLE001
        logger.warning("Browser TTS warm-up failed (non-fatal): {}", e)

    yield


app = FastAPI(
    title="Voice Agent - 园区访客登记",
    version="0.1.0",
    description="端到端语音AI访客登记系统。电话接听 → 自然对话 → 企业微信通知门卫。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"}


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    settings = get_settings()
    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Voice Agent - {settings.park_name}</title>
    <style>
      body{{font-family:-apple-system,system-ui,sans-serif;max-width:780px;margin:60px auto;padding:0 24px;color:#1f2328;line-height:1.6}}
      h1{{font-size:28px}} code{{background:#f6f8fa;padding:2px 6px;border-radius:4px;font-size:13px}}
      a{{color:#0969da}}
    </style></head>
    <body>
      <h1>📞 Voice Agent · {settings.park_name}</h1>
      <p>端到端语音AI访客登记系统</p>
      <ul>
        <li>POST <code>/twiml/incoming</code> &mdash; Twilio webhook (配置到 Twilio Phone Number 的 Voice webhook)</li>
        <li>WS <code>/twilio/media-stream</code> &mdash; Twilio Media Streams</li>
        <li>GET <code>/api/visitors</code> &mdash; 门卫查询：最近访客</li>
        <li>GET <code>/api/visitors/stats</code> &mdash; 门卫查询：统计</li>
        <li>POST <code>/api/guard/chat</code> &mdash; 门卫查询：自然语言问答</li>
      </ul>
      <p>健康检查：<a href="/healthz">/healthz</a></p>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Twilio TwiML (call entrypoint)
# ---------------------------------------------------------------------------

@app.post("/twiml/incoming")
async def twiml_incoming(request: Request) -> JSONResponse:
    """Return TwiML that connects the caller to our Media Stream."""
    settings = get_settings()
    if not settings.public_base_url:
        raise HTTPException(500, "PUBLIC_BASE_URL not set")

    ws_url = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/twilio/media-stream"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" track="both_tracks">
      <Parameter name="caller" value="visitor"/>
    </Stream>
  </Connect>
</Response>"""
    return HTMLResponse(content=xml, media_type="text/xml")


# ---------------------------------------------------------------------------
# Twilio Media Streams WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/twilio/media-stream")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    call = TwilioCall(ws)
    await call.run()


# ---------------------------------------------------------------------------
# Browser-direct voice session (no telephony — same STT/LLM/TTS pipeline)
# ---------------------------------------------------------------------------

@app.websocket("/ws/browser")
async def browser_session_ws(ws: WebSocket) -> None:
    """Browser-direct live test. Bypasses Twilio entirely.

    Browser sends PCM 16 kHz mono over WebSocket; server runs the same
    Bailian ASR → qwen-plus → qwen3-TTS pipeline used by the Twilio path.
    """
    session = BrowserSession(ws)
    await session.run()


@app.get("/browser-test", response_class=HTMLResponse)
async def browser_test_page() -> HTMLResponse:
    """Live voice test page — speak into your mic, hear the agent reply."""
    from pathlib import Path
    settings = get_settings()
    html_path = Path(__file__).parent.parent / "static" / "browser_test.html"
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=html.replace("{{WS_URL}}", _browser_ws_url(settings)),
        media_type="text/html",
    )


@app.get("/clone_voice", response_class=HTMLResponse)
async def clone_voice_page() -> HTMLResponse:
    """Voice-clone reference recording page for CosyVoice v3.5+ enrollment."""
    from pathlib import Path
    html_path = Path(__file__).parent.parent / "static" / "clone_voice.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"), media_type="text/html")


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>Voice Agent — Guard Dashboard</title>
  <meta http-equiv="refresh" content="10">  <!-- auto-refresh every 10 s -->
  <style>
    :root { font-family: -apple-system, "PingFang SC", sans-serif; }
    body { margin: 0; padding: 24px; background: #f5f6f8; color: #222; }
    h1 { margin: 0 0 16px 0; }
    h2 { margin: 24px 0 8px 0; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .card { background: #fff; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    .card .num { font-size: 32px; font-weight: 600; color: #1a73e8; }
    .card .lbl { font-size: 12px; color: #666; margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }
    th { background: #fafafa; font-weight: 600; color: #555; }
    tr:last-child td { border-bottom: none; }
    .plate { font-family: monospace; font-weight: 600; color: #1a73e8; }
    .returning { background: #fff3cd; color: #856404; padding: 1px 6px; border-radius: 3px; font-size: 11px; }
    .empty { padding: 40px; text-align: center; color: #999; }
    .toolbar { display: flex; gap: 12px; margin-bottom: 12px; }
    .toolbar a { padding: 6px 14px; background: #1a73e8; color: #fff; text-decoration: none; border-radius: 4px; font-size: 13px; }
    .toolbar a:hover { background: #1557b0; }
  </style>
</head>
<body>
  <h1>🚪 门卫语音 Agent · 实时面板</h1>
  <div class="toolbar">
    <a href="/api/visitors?days=7" target="_blank">📋 Raw JSON /api/visitors</a>
    <a href="/api/visitors/stats" target="_blank">📊 Raw JSON /stats</a>
    <a href="/browser-test" target="_blank">🎙️ 浏览器测试页</a>
    <a href="/api/wechat/test" onclick="testWebhook(event)">🔔 测试 Webhook</a>
  </div>

  <h2>近况</h2>
  <div class="grid">
    <div class="card"><div class="num" id="kpi-total">—</div><div class="lbl">总访问次数</div></div>
    <div class="card"><div class="num" id="kpi-unique">—</div><div class="lbl">不重复车辆</div></div>
    <div class="card"><div class="num" id="kpi-peak">—</div><div class="lbl">高峰时段</div></div>
    <div class="card"><div class="num" id="kpi-fresh">—</div><div class="lbl">最新 1 小时</div></div>
  </div>

  <h2>按事由</h2>
  <div class="grid" id="kpi-reasons"></div>

  <h2>最近访问（最近 50 条）</h2>
  <table>
    <thead><tr>
      <th>#</th><th>车牌</th><th>公司</th><th>事由</th><th>联系人</th>
      <th>手机号</th><th>回访</th><th>开始时间 (CST)</th><th>时长</th>
    </tr></thead>
    <tbody id="vrows"><tr><td colspan="9" class="empty">加载中…</td></tr></tbody>
  </table>

  <script>
    // Convert UTC "YYYY-MM-DD HH:MM:SS" → CST (UTC+8) string
    function toCST(utcStr) {
      if (!utcStr) return "—";
      // SQLite returns naive UTC. Treat it as UTC, add 8 h for CST display.
      const iso = utcStr.includes("T") ? utcStr : utcStr.replace(" ", "T") + "Z";
      const d = new Date(iso);
      if (isNaN(d)) return utcStr;
      return d.toLocaleString("zh-CN", {
        hour12: false, timeZone: "Asia/Shanghai",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    }
    function ago(utcStr) {
      if (!utcStr) return "—";
      const iso = utcStr.includes("T") ? utcStr : utcStr.replace(" ", "T") + "Z";
      const d = new Date(iso);
      const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
      if (sec < 60) return sec + "s 前";
      if (sec < 3600) return Math.floor(sec / 60) + " 分前";
      if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
      return Math.floor(sec / 86400) + " 天前";
    }
    function esc(s) {
      return (s ?? "—").toString().replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[c]));
    }
    async function testWebhook(e) {
      e.preventDefault();
      const r = await fetch("/api/wechat/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "🔔 Dashboard 触发测试 (CST " + new Date().toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"}) + ")" }),
      });
      const j = await r.json();
      alert(j.errcode === 0 ? "✅ Webhook OK" : "❌ " + (j.errmsg || JSON.stringify(j)));
    }
    async function load() {
      try {
        const [v, s] = await Promise.all([
          fetch("/api/visitors?days=7&limit=50").then(r => r.json()),
          fetch("/api/visitors/stats?days=7").then(r => r.json()),
        ]);
        document.getElementById("kpi-total").textContent = s.total_visits ?? 0;
        document.getElementById("kpi-unique").textContent = s.unique_plates ?? 0;
        document.getElementById("kpi-peak").textContent = s.peak_hour?.hour != null
          ? s.peak_hour.hour + ":00 (" + s.peak_hour.visits + ")" : "—";
        const now = Date.now();
        const fresh = (v.visitors || []).filter(x => {
          if (!x.started_at) return false;
          const t = new Date(x.started_at.replace(" ", "T") + "Z").getTime();
          return now - t < 3600 * 1000;
        }).length;
        document.getElementById("kpi-fresh").textContent = fresh;
        const reasons = document.getElementById("kpi-reasons");
        reasons.innerHTML = "";
        (s.by_reason || []).forEach(r => {
          const div = document.createElement("div");
          div.className = "card";
          div.innerHTML = `<div class="num">${r.visits}</div><div class="lbl">${esc(r.reason)}</div>`;
          reasons.appendChild(div);
        });
        const rows = document.getElementById("vrows");
        if (!v.visitors || v.visitors.length === 0) {
          rows.innerHTML = '<tr><td colspan="9" class="empty">暂无访问记录 — 打开 <a href="/browser-test">/browser-test</a> 试一次</td></tr>';
        } else {
          rows.innerHTML = v.visitors.map(x => `
            <tr>
              <td>${x.id}</td>
              <td><span class="plate">${esc(x.plate)}</span></td>
              <td>${esc(x.company)}</td>
              <td>${esc(x.reason)}</td>
              <td>${esc(x.contact_name)}</td>
              <td>${esc(x.phone)}</td>
              <td>${x.is_returning ? '<span class="returning">回访</span>' : ""}</td>
              <td title="${ago(x.started_at)}">${toCST(x.started_at)}</td>
              <td>${x.duration_seconds != null ? x.duration_seconds + "s" : "—"}</td>
            </tr>
          `).join("");
        }
      } catch (e) {
        document.getElementById("vrows").innerHTML =
          '<tr><td colspan="9" class="empty">❌ 加载失败: ' + esc(e.message) + '</td></tr>';
      }
    }
    load();
  </script>
</body>
</html>"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """Real-time guard dashboard: KPIs + recent visitors, auto-refresh 10 s.
    Reads from /api/visitors and /api/visitors/stats so the same data
    is exposed as JSON for any external dashboard.
    """
    return HTMLResponse(content=_DASHBOARD_HTML, media_type="text/html")


def _browser_ws_url(settings) -> str:
    base = settings.public_base_url or "http://localhost:8000"
    return base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/browser"

# ---------------------------------------------------------------------------
# CosyVoice v3.5+ voice clone — reference audio upload
# ---------------------------------------------------------------------------

@app.post("/api/clone/upload")
async def clone_upload(audio: UploadFile = FastAPIFile(...)) -> dict:
    """Accept a reference WAV from the browser, save to /tmp/voice_clone_ref.wav.

    Used by the voice-clone recording page (/clone_voice.html). The user
    records 10-30 s of clean speech; the browser converts to 16 kHz mono
    WAV; we save it for the clone_voice.py CLI to pick up.
    """
    out_path = Path("/tmp/voice_clone_ref.wav")
    data = await audio.read()
    out_path.write_bytes(data)
    # Compute duration from WAV header
    try:
        import wave
        with wave.open(str(out_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            duration = round(frames / rate, 2) if rate else 0
    except Exception:
        duration = 0
    logger.info("Voice clone reference saved: {} ({} bytes, {}s)", out_path, len(data), duration)
    return {"path": str(out_path), "size": len(data), "duration": duration}


@app.api_route("/api/clone/audio", methods=["GET", "HEAD"])
async def clone_audio() -> "Response":
    """Serve /tmp/voice_clone_ref.wav as a public URL via the cloudflared
    tunnel. Used as the `url` parameter for Bailian voice enrollment —
    avoids uploading through 0x0.st (slow from China).

    The file is only ~300 KB; Aliyun will fetch it once, then we can
    delete the route. NB: Bailian's URL fetcher probes with HEAD first;
    if the route doesn't accept HEAD it returns 405 and Bailian rejects
    the URL with "url error". We register both GET and HEAD explicitly.
    """
    from fastapi.responses import FileResponse
    p = Path("/tmp/voice_clone_ref.wav")
    if not p.exists():
        return JSONResponse({"error": "no reference audio"}, status_code=404)
    return FileResponse(
        p,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Guard query API
# ---------------------------------------------------------------------------

@app.get("/api/visitors")
async def list_visitors(
    plate: str | None = Query(None),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """List recent visits, optionally filtered by plate."""
    rows = await sql_module.list_recent(plate=plate, days=days, limit=limit)
    return {
        "count": len(rows),
        "plate": plate,
        "days": days,
        "visitors": rows,
    }


@app.get("/api/visitors/stats")
async def visitors_stats(days: int = Query(7, ge=1, le=365)) -> dict:
    """Summary statistics for the guard dashboard / chat agent."""
    return await sql_module.stats(days=days)


class GuardChatIn(BaseModel):
    question: str
    history: list[dict] | None = None


@app.post("/api/guard/chat")
async def guard_chat(body: GuardChatIn) -> dict:
    """Natural-language Q&A over the visitor history.

    Examples:
      "本周一共多少访问车辆？"
      "什么时间段访问最多？"
      "张师傅这个月来了几次？"
    """
    settings = get_settings()
    if not settings.has_openai:
        raise HTTPException(500, "OPENAI_API_KEY not configured (LLM required for guard chat)")
    agent = GuardQueryAgent()
    answer = await agent.ask(body.question, history=body.history or [])
    return {"answer": answer}


class WeChatTestIn(BaseModel):
    markdown: str | None = None
    text: str | None = None


@app.post("/api/wechat/test")
async def wechat_test(body: WeChatTestIn) -> dict:
    """Send a test message to the configured WeChat group robot."""
    settings = get_settings()
    if not settings.has_wechat:
        raise HTTPException(400, "WECHAT_WEBHOOK_URL not configured")
    if body.markdown:
        return await wechat.send_markdown(body.markdown)
    if body.text:
        return await wechat.send_text(body.text)
    raise HTTPException(400, "Provide `markdown` or `text`")


# ---------------------------------------------------------------------------
# Run with `python -m app.main` (used for local dev)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
