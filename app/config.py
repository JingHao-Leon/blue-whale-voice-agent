"""
Application configuration loaded from environment variables.

We use pydantic-settings so all config is type-safe and validated at startup.
If a required secret is missing the app will fail-fast instead of crashing
mid-call.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Voice agent runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Twilio ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_webhook_auth_token: str = ""

    # --- STT ---
    deepgram_api_key: str = ""
    openai_api_key: str = ""

    # --- LLM defaults (overridden by Bailian in post-init if key is set) ---
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"

    # --- TTS ---
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"
    openai_tts_voice: str = "nova"
    openai_tts_model: str = "tts-1"

    # --- Alibaba Bailian (百炼) — DashScope ---
    # Set DASHSCOPE_API_KEY to use Bailian for STT/LLM/TTS.
    # Bailian is OpenAI-compatible, so we just point the OpenAI client at the
    # Bailian endpoint with the DashScope key.
    dashscope_api_key: str = ""
    bailian_stt_model: str = "fun-asr-realtime"  # Fun-ASR (URL 2983775)
    bailian_tts_model: str = "qwen3-tts-flash"  # 通义 TTS · 实时流式中文(稳)
    bailian_tts_voice: str = "Cherry"             # 通义 TTS · Cherry 女声
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # Qwen3.7-Max supports a `enable_thinking` toggle. For real-time voice
    # agent we keep it OFF — thinking mode forces streaming and adds 1-3 s of
    # reasoning latency, which destroys turn-end timing on a phone call.
    bailian_llm_enable_thinking: bool = False

    # --- WeChat Work ---
    wechat_webhook_url: str = ""

    # --- Storage ---
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT}/data/visitors.db"

    # --- Park config ---
    park_name: str = "蓝色鲸鱼科技园"
    default_company: str = "蓝色鲸鱼科技"
    guard_group_name: str = "园区门卫通知群"

    # --- Time budget ---
    target_total_seconds: int = 25  # hard requirement from the take-home spec

    @property
    def has_twilio(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def has_deepgram(self) -> bool:
        return bool(self.deepgram_api_key)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_bailian(self) -> bool:
        return bool(self.dashscope_api_key)

    @property
    def has_wechat(self) -> bool:
        return bool(self.wechat_webhook_url)

    @property
    def has_e164_twilio_number(self) -> bool:
        return self.twilio_phone_number.startswith("+") and len(self.twilio_phone_number) >= 8

    def validate_for_call(self) -> list[str]:
        """Return a list of human-readable warnings about missing config.

        We don't hard-fail because the user may want to start the server without
        all secrets and fill them in later. But we surface what's missing.
        """
        missing: list[str] = []
        if not self.has_twilio:
            missing.append("Twilio credentials (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)")
        if not self.has_e164_twilio_number:
            missing.append("TWILIO_PHONE_NUMBER (must be E.164, e.g. +15551234567)")
        if not self.has_deepgram and not self.has_openai and not self.has_bailian:
            missing.append("One of DEEPGRAM_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY for STT")
        if not self.has_openai and not self.has_bailian:
            missing.append("One of OPENAI_API_KEY / DASHSCOPE_API_KEY for LLM")
        if not self.has_wechat:
            missing.append("WECHAT_WEBHOOK_URL for guard notification")
        return missing


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Smart defaults: if Bailian key is set and the user hasn't explicitly
    # chosen a non-Bailian LLM model, auto-route LLM to Bailian.
    if s.dashscope_api_key and s.llm_model in {"gpt-4o-mini", "gpt-4o", "qwen-plus", "qwen3.7-max", ""}:
        s.llm_base_url = s.bailian_base_url
        s.llm_model = "qwen3.6-flash"  # flash · 低延迟优先,实时语音首选
        # Also feed the dashscope key into the openai-compatible client
        # (the AsyncOpenAI instance reads `openai_api_key`).
        if not s.openai_api_key:
            s.openai_api_key = s.dashscope_api_key
    return s
