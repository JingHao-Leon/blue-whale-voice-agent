// pages/index/index.js
const app = getApp();
const SAMPLE_RATE = 16000;

let ws = null;
let recorderManager = null;
let audioCtx = null;          // wx.createWebAudioContext() 实例
let nextStartTime = 0;         // 音频调度游标（避免 chunk 重叠）
let msgId = 0;
let serverGreeting = '';      // server 主动推送的 greeting text

Page({
  data: {
    status: '未连接',
    connected: false,
    recording: false,
    micMuted: false,           // agent 说话时静音 mic
    messages: [],
    lastMsgId: '',
    wsHost: app.globalData.WS_URL.replace(/^wss?:\/\//, '').replace(/\/.*$/, ''),
  },

  onLoad() {
    this._connect();
  },

  onUnload() {
    this._teardown();
  },

  // ---------------------------------------------------------------
  // WebSocket
  // ---------------------------------------------------------------

  _connect() {
    const url = app.globalData.WS_URL;
    this._setStatus('正在连接 ' + url);
    ws = wx.connectSocket({
      url,
      success: () => console.log('[ws] connecting…'),
    });
    ws.onOpen(() => {
      console.log('[ws] open');
      wx.sendSocketMessage({
        data: JSON.stringify({ type: 'config', sample_rate: SAMPLE_RATE }),
      });
    });
    ws.onMessage((res) => {
      // 微信 onMessage 的 res.data 是 string | ArrayBuffer
      if (typeof res.data === 'string') {
        try { this._onJson(res.data); } catch (e) { console.error(e); }
      } else {
        this._onPcm(res.data);
      }
    });
    ws.onError((err) => {
      console.error('[ws] error', err);
      this._setStatus('WebSocket 错误：' + (err.errMsg || ''));
      this.setData({ connected: false });
    });
    ws.onClose(() => {
      console.log('[ws] close');
      this.setData({ connected: false });
      this._setStatus('已断开');
    });
  },

  _onJson(raw) {
    const msg = JSON.parse(raw);
    switch (msg.type) {
      case 'ready':
        this.setData({ connected: true });
        this._setStatus('就绪，等 agent 开场白…');
        break;
      case 'transcript':
        if (msg.is_final) this._append('user', msg.text);
        break;
      case 'agent_text':
        serverGreeting = msg.text;
        this._append('agent', msg.text);
        break;
      case 'agent_audio_start':
        this.setData({ micMuted: true });
        nextStartTime = 0;        // reset 调度游标
        this._setStatus('🤖 AI 正在说…（麦克风已静音）');
        break;
      case 'agent_audio_end':
        this.setData({ micMuted: false });
        setTimeout(() => {
          this._setStatus('👂 轮到你了，按住按钮说话');
        }, 200);
        break;
      case 'wechat_ok':
        this._append('sys', '✓ WeChat webhook ' + (msg.ok ? 'OK' : 'FAILED'));
        break;
      case 'done':
        this._append('sys', '✓ 会话结束：' + (msg.summary || ''));
        this.setData({ connected: false });
        this._stopRecording();
        break;
      case 'error':
        this._append('sys', '✗ ' + msg.message);
        break;
    }
  },

  _onPcm(arrayBuffer) {
    if (this.data.micMuted) {
      // agent 说话期间不接收用户输入（防止 TTS 漏音触发 STT）
    }
    this._playPcm(arrayBuffer);
  },

  // ---------------------------------------------------------------
  // 录音（PCM 16 kHz mono s16le）
  // ---------------------------------------------------------------

  _ensureRecorder() {
    if (recorderManager) return recorderManager;
    recorderManager = wx.getRecorderManager();
    recorderManager.onError((err) => {
      console.error('[rec] error', err);
      this._setStatus('录音错误：' + (err.errMsg || ''));
    });
    // 关键：frameSize 单位 KB，onFrameRecorded 按这个粒度回调 PCM
    recorderManager.onFrameRecorded((res) => {
      if (!ws || this.data.micMuted) return;
      const pcm = res.frameBuffer;  // ArrayBuffer of Int16 LE 16kHz mono
      try {
        wx.sendSocketMessage({ data: pcm });
      } catch (e) {
        console.error('[ws] send err', e);
      }
    });
    return recorderManager;
  },

  _startRecording() {
    this._ensureRecorder();
    recorderManager.start({
      duration: 60_000,                // 1 分钟/段
      sampleRate: SAMPLE_RATE,
      numberOfChannels: 1,
      encodeBitRate: 256_000,
      format: 'PCM',                    // 原始 Int16 LE
      frameSize: 4,                     // 4 KB ≈ 125 ms
    });
    this.setData({ recording: true });
    this._setStatus('🎙 正在听你说话…');
  },

  _stopRecording() {
    if (recorderManager && this.data.recording) {
      try { recorderManager.stop(); } catch (e) { /* ignore */ }
    }
    this.setData({ recording: false });
  },

  // ---------------------------------------------------------------
  // 播放（wx.createWebAudioContext）
  // ---------------------------------------------------------------

  _ensureAudioCtx() {
    if (audioCtx) return audioCtx;
    if (typeof wx.createWebAudioContext !== 'function') {
      this._append('sys', '✗ 微信基础库版本太低，需要 2.19.0+');
      return null;
    }
    audioCtx = wx.createWebAudioContext();
    console.log('[audio] ctx sample rate:', audioCtx.sampleRate);
    return audioCtx;
  },

  _playPcm(arrayBuffer) {
    const ctx = this._ensureAudioCtx();
    if (!ctx) return;

    // Int16LE → Float32 [-1, 1]
    const i16 = new Int16Array(arrayBuffer);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) {
      f32[i] = i16[i] / 32768;
    }

    const buf = ctx.createBuffer(1, f32.length, SAMPLE_RATE);
    buf.copyToChannel(f32, 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);

    const now = ctx.currentTime;
    if (nextStartTime < now) nextStartTime = now + 0.02;
    src.start(nextStartTime);
    nextStartTime += buf.duration;
  },

  // ---------------------------------------------------------------
  // UI 事件
  // ---------------------------------------------------------------

  onMicTap() {
    if (this.data.recording) {
      // 主动结束当前 turn
      this._stopRecording();
      this._setStatus('处理中…');
      // 告诉 server "end utterance"，让 STT 立即出 final
      try { wx.sendSocketMessage({ data: JSON.stringify({ type: 'end' }) }); } catch (e) {}
    } else {
      this._startRecording();
    }
  },

  // ---------------------------------------------------------------
  // helpers
  // ---------------------------------------------------------------

  _setStatus(s) { this.setData({ status: s }); },

  _append(kind, text) {
    const id = ++msgId;
    const messages = this.data.messages.concat([{ id, kind, text }]);
    this.setData({ messages, lastMsgId: 'msg-' + id });
  },

  _teardown() {
    this._stopRecording();
    if (ws) {
      try { wx.closeSocket({ code: 1000 }); } catch (e) {}
      ws = null;
    }
  },
});
