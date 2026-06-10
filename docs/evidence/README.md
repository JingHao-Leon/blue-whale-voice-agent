# Evidence Pack

This directory contains verifiable artifacts proving the voice agent system
ran end-to-end during the take-home.

| File | What it proves |
|---|---|
| `stress-test-5x.md` / `.json` | 5 concurrent calls @ concurrency=3 all completed end-to-end in 10s, T1st ~2s, SLA 5/5 (≤25s) |
| `demo-3turn-28s.wav` | Real server output: full 3-turn conversation (greeting + plate + completion), 28s |
| `tts-bailian-cherry-sample.wav` | Raw output of `qwen3-tts-flash` voice `Cherry` from Bailian, for sound quality comparison |
| `voice-agent-server.log` | Server stdout during stress test + real Twilio calls (search `📞 Call started` for the 9 real call SIDs) |
| `cloudflared.log` | cloudflared HTTP/2 tunnel log proving WSS upgrade succeeded (`Registered tunnel connection ... protocol=http2`) |

## Twilio 真实通话记录（截图待补）

9 inbound calls from `+8615909130000` to `+1 989 546 6741`, all `completed`,
all charged $0.0045 per call. The Twilio account was subsequently closed,
so the number is no longer reachable. Server log entries like:

```
[INFO] app.services.twilio_handler:_on_start:109
📞 Call started sid=CA2d39c0337fdc324688033cd258888528
   from=+8615909130000 to=+19895466741
[INFO] app.services.tts:_synth_bailian:130
   Bailian TTS (qwen3-tts-flash/Cherry) 1.71s → 288044 bytes
[INFO] connection closed
```

are the receipts: full Twilio call → TwiML → Media Stream → Bailian TTS → back to Twilio all happened.

## 怎么复核

```bash
# 1. 听真实 TTS（无需登录任何服务）
afplay docs/evidence/tts-bailian-cherry-sample.wav

# 2. 看压力测试摘要
cat docs/evidence/stress-test-5x.md

# 3. grep 出真实 Twilio call 的 server log 行
grep "Call started" docs/evidence/voice-agent-server.log
```
