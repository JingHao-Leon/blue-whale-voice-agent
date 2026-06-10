# 微信小程序 · 园区访客登记（Voice Agent 入口）

> 这个小程序是 take-home 项目的**第三个入口**（除了 Twilio 真实电话、WebRTC 浏览器实测页）。
> 后端**完全复用** `voice_agent/app/services/browser_session.py`（已经写好、能处理"PCM 16kHz over WebSocket"），小程序只负责录音 + 播放。

---

## 三个入口对比

| 入口 | 谁能用 | 走谁的网络 | 适用 |
|---|---|---|---|
| Twilio 真实电话 | 任何有手机的人 | PSTN/移动运营商 | 真实用户体验 |
| **WebRTC 浏览器实测页** | 任何能开 Chrome 的人 | 浏览器 | 零成本演示 |
| **微信小程序** | 任何用微信的人 | 微信内网 | 园区扫码即用、零流量费 |

---

## 1. 注册小程序 AppID

1. 打开 https://mp.weixin.qq.com/ → 立即注册 → 选"小程序"
2. 选**个人主体**（5 分钟，不要企业认证）
3. 拿到 AppID（一串 `wx...` 字符）
4. **开发管理 → 开发设置 → 开发者 ID**

## 2. 配置合法域名（关键！）

小程序**只能**连白名单里的域名。后端是 cloudflared 的 trycloudflare 域名，需要加进白名单：

1. 微信公众平台 → 开发管理 → 开发设置 → **服务器域名**
2. **request 合法域名**：填 `https://cars-gathering-divx-bent.trycloudflare.com`
3. **socket 合法域名**：填 `wss://cars-gathering-divx-bent.trycloudflare.com`
4. （注：trycloudflare 是临时域名。如果有正式域名就更好——只需修改 `app.js` 里的 `WS_URL`）

> ⚠️ 个人主体小程序**没有"业务域名"配置权限**。如果跑正式版需要企业主体 + ICP 备案。开发/演示用上面这套够。

## 3. 导入项目

1. 下载 [微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)（macOS 版，~150 MB）
2. 打开 → 导入项目
3. 项目目录 = `wechat-miniprogram/` 这个文件夹
4. AppID = 你刚注册的
5. 后端服务 = "微信云开发"或"不使用云服务"（**选不使用**——我们用自己 server）

## 4. 启动后端

如果你的 `voice_agent` 还没跑起来：

```bash
cd voice_agent
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

确认 cloudflared tunnel 还活：

```bash
curl https://cars-gathering-divx-bent.trycloudflare.com/healthz
# 应该返回 {"status":"ok",...}
```

## 5. 预览

微信开发者工具 → 工具栏 → **预览** → 扫二维码 → 真机体验

> 真机调试时小程序会向 `cars-gathering-divx-bent.trycloudflare.com` 发 WebSocket 请求，**走微信内网**，不走你的手机运营商。
> 所以即使你在中国也不会有国际长途风控问题。

---

## 文件结构

```
wechat-miniprogram/
├── app.js                 # 全局 WS_URL 配置（改成你的）
├── app.json               # 小程序配置 + 麦克风权限声明
├── app.wxss               # 全局样式（这里没写，用 page 默认）
└── pages/
    └── index/
        ├── index.js       # 主逻辑：录音 + WebSocket + 播放
        ├── index.json     # 页面配置
        ├── index.wxml     # 页面模板
        └── index.wxss     # 页面样式
```

## 关键代码

**录音**（`pages/index/index.js`）：

```js
recorderManager.start({
  duration: 60_000,
  sampleRate: 16_000,
  numberOfChannels: 1,
  encodeBitRate: 256_000,
  format: 'PCM',           // 原始 Int16 LE
  frameSize: 4,            // 4 KB ≈ 125 ms 一帧
});
recorderManager.onFrameRecorded((res) => {
  if (micMuted) return;     // agent 说话时不发送
  wx.sendSocketMessage({ data: res.frameBuffer });
});
```

**播放**（接 server 推回来的 PCM 16 kHz）：

```js
audioCtx = wx.createWebAudioContext();
const i16 = new Int16Array(arrayBuffer);
const f32 = new Float32Array(i16.length);
for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
const buf = audioCtx.createBuffer(1, f32.length, 16_000);
buf.copyToChannel(f32, 0);
const src = audioCtx.createBufferSource();
src.buffer = buf;
src.connect(audioCtx.destination);
src.start(nextStartTime);
nextStartTime += buf.duration;
```

## 已知限制

- **基础库版本**：需要 `wx.createWebAudioContext` 支持（2.19.0+），覆盖 99% 用户
- **个人主体小程序**：仅能体验版/开发版，不能上架
- **微信号限制**：需要用户主动授权麦克风
- **音频延迟**：跟 WebRTC 浏览器实测类似（greeting ~37 ms ready，响应 ~1-3s）

## 怎么改 WS_URL

打开 `app.js`，改这一行：

```js
WS_URL: 'wss://your-tunnel.trycloudflare.com/ws/browser',
```

如果你的 server 在公网不同域名/IP，对应改掉就行。
