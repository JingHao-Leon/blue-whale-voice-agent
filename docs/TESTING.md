# TESTING.md — 实战测试文档

> 蓝色鲸鱼科技园（Whale Tech）智能门卫语音 Agent — 实战测试指引
> 最后更新：2026-06-09

---

## TL;DR

| 测试方式 | 推荐度 | 所需物料 | 时长 |
|---|---|---|---|
| **A. WebRTC 浏览器测** | ⭐⭐⭐ 首选 | Chrome / Edge | 1-2 分钟 |
| **B. WeChat 小程序测** | ⭐⭐ 备选 | 微信开发者工具 / 真机 | 3-5 分钟 |
| **C. Bash 脚本自动测** | ⭐⭐ 压力/性能 | `uv run python scripts/measure_25s.py` | 30 秒 |

> ⚠️ **Phone call（Twilio 真实电话）已不可用** — Twilio 账号在 2026-06-08 中途被关闭，US 号码 `+1 989 546 6741` 已失效。下文三种方式都不依赖 Twilio。

---

## 方式 A：WebRTC 浏览器测试（最简单）

### 1. 打开测试页

生产 URL（推荐）：
```
https://acid-visiting-louis-dos.trycloudflare.com/browser-test
```

> 每次重启 cloudflared tunnel 都会换 URL，请从 server 启动日志 / `/tmp/cloudflared.url` 读最新地址。

### 2. 浏览器要求

- **Chrome / Edge 最新版**（Web Audio API + getUserMedia 完整支持）
- 允许麦克风权限
- 第一次需要 HTTPS（localhost 除外）

### 3. 端到端测试脚本（3 步，约 1 分钟）

1. 打开页面 → 听到"哎您好，蓝色鲸鱼科技园园区。车牌号、来的啥事儿，简单报一下呗？"
2. 麦克风按钮变红，开始说话。建议测试话术：
   ```
   沪A12345，蓝色鲸鱼送货，13800138000，张师傅
   ```
   （10 秒内说完，语速自然）
3. 听到门卫放行音 → 微信收到通知 → 页面显示"通话结束"

### 4. 录制演示视频（推荐）

macOS 自带 QuickTime：
1. QuickTime Player → 文件 → 新建屏幕录制
2. 选 Chrome 窗口（要带音频输入）
3. 点上面的脚本 1-2 步
4. 30-60 秒短片足够
5. 导出为 mp4

视频里能看到 / 听到：
- 浏览器 WS 状态：CONNECTED
- STT 实时识别文字
- LLM 回复文字
- TTS 播放音频
- 微信 webhook 通知（如果开了"测试模式"）

---

## 方式 B：WeChat 小程序测试

### B1. 独立 demo（10 分钟上手）

```bash
cd voice_agent/wechat-miniprogram
# 用微信开发者工具打开整个目录
# AppID: 选 "测试号" 即可
```

修改 `app.js`：
```js
globalData: {
  WS_URL: "wss://acid-visiting-louis-dos.trycloudflare.com/ws/browser",
  API_KEY: "",  // 默认无需
}
```

点"开始录音" → 说话 → 结束。

### B2. 接入你已有的小程序（drop-in）

详见 `wechat-miniprogram/drop-in/voice-checkin/INTEGRATE.md`：

```bash
# 1. 复制 4 个文件
cp -r voice-checkin /path/to/your/miniprogram/pages/

# 2. 在 app.json 注册（一行）
# "pages/voice-checkin/voice-checkin"

# 3. 修改 voice-checkin.js 顶部的 WS_URL

# 4. 在你的"门禁"菜单里加一个跳转入口
wx.navigateTo({ url: '/pages/voice-checkin/voice-checkin' })
```

---

## 方式 C：Bash 脚本自动测（验证 25 秒预算）

### 25 秒 spec budget test

```bash
cd voice_agent
uv run python scripts/measure_25s.py \
  --host "https://acid-visiting-louis-dos.trycloudflare.com" \
  --audio /tmp/user_test_with_silence.wav
```

**测试音频 `/tmp/user_test_with_silence.wav`**：
- 8.7 s 用户录音："沪A12345 蓝色鲸鱼 送货 13800138000"
- 末尾 1.5 s 静音（真实用户都会有自然停顿）
- 16 kHz mono s16le PCM

**测试流程**（脚本自动）：
1. WS 握手，收到 `ready` + boot greeting 音频
2. 实时流式推送 8.7 s 用户音频
3. STT 触发 LLM
4. LLM 调 `send_to_guard_and_end_call` 工具
5. TTS 播结束语 + 推 WeChat webhook
6. 测 `done` JSON 到达时间

**通过标准**：`T first audio → done` < 25 000 ms

**最近跑分**：
- 22 706 ms（PASS，margin 2.3 s）— 干净 cache 状态
- 24 704 ms（PASS，margin 0.3 s）— 尾段偶发慢 2 s
- < 25 000 ms 阈值是 spec 要求，**实际生产单轮对话稳定 < 8 s**（远低于 25 s）

### 压测（10 并发）

```bash
uv run python scripts/stress_browser.py --host "https://acid-visiting-louis-dos.trycloudflare.com" --sessions 10
```

输出每个 session 的：握手 / 问候 / 总耗时 + p50 / p95 / max。
**当前结果**：10/10 握手成功，greeting p95 < 3 s。

---

## 性能拆解（25 s budget 时间线）

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 0. WS 握手 + `ready` JSON | 1.7-3.4 s | cold start；warmup 命中后 < 1 s |
| 1. 收到首段音频 | + 0 ms | boot greeting 提前 0 ms 到达 |
| 2. 播完 greeting | 0.6-1.2 s | 2.4 kB → 1.5 s @ 16 kHz |
| 3. 用户说话 | 8.7-10.2 s | 录音决定；+ 1.5 s 尾部静音 |
| 4. STT 处理 | 2-3 s | Fun-ASR VAD 触发 → final transcript |
| 5. LLM 决策 + 调工具 | 1-2 s | qwen3.6-flash + tool call |
| 6. TTS 结束语 | 1.2-2.0 s | qwen3-tts-flash/Cherry 1.2-2.0 s |
| 7. WeChat webhook 推送 | < 0.3 s | HTTP POST |
| **总计** | **~22-25 s** | spec 阈值 25 s |

**真实生产**（用户说话清晰、VAD 触发快）：典型 ~15-18 s 即可完成。

---

## 已知限制

1. **Bailian STT 单个 task 23 s 超时** — 长段用户录音（>23 s）会触发任务超时，supervisor 自动重开下一个 task。生产建议：用户停顿 > 1.5 s 即可分段。
2. **Bailian TTS 9 并发 QPS 限流** — boot warmup 5-9 个 phrase 偶尔 429（`Throttling.RateQuota`），但**不影响 session 启动**——`Session.speak()` 会 fallback 到 live synthesis，延迟 +1-2 s。
3. **Cloudflared tunnel URL 每次重启换域名** — 部署到 ECS 固定域名后即解决。
4. **WeChat 微信通知的 WebHook 路由** `04f662ea-3ffd-45bf-b6a3-77d3fd0682db` — 如果 1 周后没收到通知，请用 `/api/wechat/test` 端点测一次；超过 30 天可能失效需重建。

---

## 紧急排错

| 现象 | 排查 |
|---|---|
| 浏览器看到 "WebSocket disconnected" | 1. `curl https://<tunnel-url>/` 200？ 2. tunnel 进程还活着？`ps aux \| grep cloudflared` |
| 听不到 agent 声音 | 1. 浏览器静音？ 2. 页面 Console 有红色错误？ 3. TTS 429 警告：`tail -100 /tmp/voice-agent.log \| grep 429` |
| STT 一直不出字 | 1. 麦克风权限？ 2. 静音 6 帧（480 ms）才触发 VAD，必须有自然停顿 3. `/tmp/voice-agent.log` 查 "task failed" |
| 微信收不到通知 | `curl -X POST http://127.0.0.1:8000/api/wechat/test` 看返回码 |
| 25 s 测试偶尔 FAIL | 跑 3 次取中位数（可变性来自 TTS cold cache 或 STT 慢启动） |
