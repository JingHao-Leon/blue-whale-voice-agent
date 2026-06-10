# Drop-in 集成指南

> 这 4 个文件**完整自包含**，拷到你现有项目就能跑。
> **不动** 你的 `app.js` / `app.json` / 任何已有页面。

## 1. 复制 4 个文件

```bash
# 在你现有小程序项目里
mkdir -p pages/voice-checkin
cp voice-checkin.{json,js,wxml,wxss} pages/voice-checkin/
```

## 2. 在你的 `app.json` 注册页面（**只加一行**）

```diff
 {
   "pages": [
     "pages/index/index",          ← 你原来的
     "pages/some/other",            ← 你原来的
+    "pages/voice-checkin/voice-checkin",
     ...
   ]
 }
```

## 3. 微信公众平台加白名单（**只加一次**）

1. https://mp.weixin.qq.com/ → 开发管理 → 开发设置 → 服务器域名
2. **request 合法域名**：`https://cars-gathering-divx-bent.trycloudflare.com`
3. **socket 合法域名**：`wss://cars-gathering-divx-bent.trycloudflare.com`

## 4. （可选）从你的首页跳转

```js
// 在你首页的 wxml 加一个入口按钮
<navigator url="/pages/voice-checkin/voice-checkin" class="entry-btn">
  📞 语音访客登记
</navigator>
```

## 5. 改了 WS_URL 怎么办

打开 `pages/voice-checkin/voice-checkin.js`，最上面那行：

```js
const WS_URL = 'wss://your-tunnel.trycloudflare.com/ws/browser';
```

换成你的 WSS 地址。

---

## 触发时机建议

| 入口位置 | 体验 |
|---|---|
| 首页大按钮 | 访客主动找 |
| 园区入口大屏 QR | 访客到现场扫 |
| 公众号菜单 | 从关注公众号进 |
| 门卫审核页"人工登记"按钮 | 兜底方案 |

## 不需要改的东西

- ✅ 你的 `app.js`（全局）
- ✅ 你的其他页面
- ✅ 你的 tabBar / 自定义导航
- ✅ 你的 wxss 主题色（改一下 `.mic-btn` 的 `background` 就行）

## 后端 0 改动

页面打的是 **同一个** `BrowserSession` 端点（`/ws/browser`），跟你之前看到的浏览器实测页用同一套 STT/LLM/TTS。所以**小程序的对话质量 = 真实电话的对话质量**。
