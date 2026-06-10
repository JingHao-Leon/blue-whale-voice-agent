// app.js
App({
  globalData: {
    // 改成你的 cloudflared / ngrok 公网 wss URL
    WS_URL: 'wss://cars-gathering-divx-bent.trycloudflare.com/ws/browser',
    SAMPLE_RATE: 16000,
  },
});
