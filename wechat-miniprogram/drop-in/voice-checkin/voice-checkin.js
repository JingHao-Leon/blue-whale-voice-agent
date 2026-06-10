// pages/voice-checkin/voice-checkin.js
// Drop-in page for any existing visitor mini-program.
// Self-contained — does NOT modify your app.js / app.json.
// Configure WS_URL below; everything else is automatic.

const SAMPLE_RATE = 16000;

// ★★ 改成你的 voice agent 公网 WSS ★★
// 例: 'wss://your-tunnel.trycloudflare.com/ws/browser'
const WS_URL = 'wss://cars-gathering-divx-bent.trycloudflare.com/ws/browser';

let ws = null;
let recorderManager = null;
let audioCtx = null;
let nextStartTime = 0;
let msgId = 0;

Page({
  data: {
    status: '未连接',
    connected: false,
    recording: false,
    micMuted: false,
    messages: [],
    lastMsgId: '',
    wsHost: WS_URL.replace(/^wss?:\/\//, '').replace(/\/.*$/, ''),
  },

  onLoad() { this._connect(); },
  onUnload() { this._teardown(); },

  // ----------------------------------------------------------------
  // WebSocket
  // ----------------------------------------------------------------
  _connect() {
    this._setStatus('正在连接…');
    ws = wx.connectSocket({ url: WS_URL });
    ws.onOpen(() => {
      wx.sendSocketMessage({
        data: JSON.stringify({ type: 'config', sample_rate: SAMPLE_RATE }),
      });
    });
    ws.onMessage((res) => {
      if (typeof res.data === 'string') {
        try { this._onJson(res.data); } catch (e) { console.error(e); }
      } else {
        this._onPcm(res.data);
      }
    });
    ws.onError((err) => {
      this._setStatus('WebSocket 错误：' + (err.errMsg || ''));
      this.setData({ connected: false });
    });
    ws.onClose(() => {
      this.setData({ connected: false });
      this._setStatus('已断开');
    });
  },

  _onJson(raw) {
    const msg = JSON.parse(raw);
    switch (msg.type) {
      case 'ready':
        this.setData({ connected: true });
        this._setStatus('就绪，等开场白…');
        break;
      case 'transcript':
        if (msg.is_final) this._append('user', msg.text);
        break;
      case 'agent_text':
        this._append('agent', msg.text);
        break;
      case 'agent_audio_start':
        this.setData({ micMuted: true });
        nextStartTime = 0;
        this._setStatus('🤖 AI 正在说…');
        break;
      case 'agent_audio_end':
        this.setData({ micMuted: false });
        setTimeout(() => this._setStatus('👂 轮到你了，点击按钮说话'), 200);
        break;
      case 'wechat_ok':
        this._append('sys', '✓ 门卫通知 ' + (msg.ok ? 'OK' : 'FAILED'));
        break;
      case 'done':
        this._append('sys', '✓ 登记完成：' + (msg.summary || ''));
        this.setData({ connected: false });
        this._stopRecording();
        // 可选：登记成功后返回上一页
        setTimeout(() => wx.navigateBack(), 1500);
        break;
      case 'error':
        this._append('sys', '✗ ' + msg.message);
        break;
    }
  },

  _onPcm(arrayBuffer) { this._playPcm(arrayBuffer); },

  // ----------------------------------------------------------------
  // Recorder
  // ----------------------------------------------------------------
  _ensureRecorder() {
    if (recorderManager) return recorderManager;
    recorderManager = wx.getRecorderManager();
    recorderManager.onError((err) => {
      this._setStatus('录音错误：' + (err.errMsg || ''));
    });
    recorderManager.onFrameRecorded((res) => {
      if (!ws || this.data.micMuted) return;
      try { wx.sendSocketMessage({ data: res.frameBuffer }); }
      catch (e) { console.error(e); }
    });
    return recorderManager;
  },

  _startRecording() {
    this._ensureRecorder();
    recorderManager.start({
      duration: 60_000,
      sampleRate: SAMPLE_RATE,
      numberOfChannels: 1,
      encodeBitRate: 256_000,
      format: 'PCM',
      frameSize: 4,            // 4 KB ≈ 125 ms / frame
    });
    this.setData({ recording: true });
    this._setStatus('🎙 正在听…');
  },

  _stopRecording() {
    if (recorderManager && this.data.recording) {
      try { recorderManager.stop(); } catch (e) {}
    }
    this.setData({ recording: false });
  },

  // ----------------------------------------------------------------
  // Audio playback (wx.createWebAudioContext, base lib 2.19.0+)
  // ----------------------------------------------------------------
  _ensureAudioCtx() {
    if (audioCtx) return audioCtx;
    if (typeof wx.createWebAudioContext !== 'function') {
      this._append('sys', '✗ 微信基础库 ≥ 2.19.0');
      return null;
    }
    audioCtx = wx.createWebAudioContext();
    return audioCtx;
  },

  _playPcm(arrayBuffer) {
    const ctx = this._ensureAudioCtx();
    if (!ctx) return;
    const i16 = new Int16Array(arrayBuffer);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
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

  // ----------------------------------------------------------------
  // UI
  // ----------------------------------------------------------------
  onMicTap() {
    if (this.data.recording) {
      this._stopRecording();
      try { wx.sendSocketMessage({ data: JSON.stringify({ type: 'end' }) }); } catch (e) {}
    } else {
      this._startRecording();
    }
  },

  // ----------------------------------------------------------------
  // helpers
  // ----------------------------------------------------------------
  _setStatus(s) { this.setData({ status: s }); },
  _append(kind, text) {
    const id = ++msgId;
    this.setData({
      messages: this.data.messages.concat([{ id, kind, text }]),
      lastMsgId: 'msg-' + id,
    });
  },
  _teardown() {
    this._stopRecording();
    if (ws) { try { wx.closeSocket({ code: 1000 }); } catch (e) {} ws = null; }
  },
});
