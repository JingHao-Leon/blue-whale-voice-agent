"""Pytest config and shared fixtures."""
import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _tmp_env(monkeypatch, tmp_path):
    # Use a throwaway SQLite DB for every test so we don't clobber real data.
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setenv("WECHAT_WEBHOOK_URL", "https://example.com/webhook")
    # Reset cached settings
    from app.config import get_settings
    get_settings.cache_clear()
    yield


@pytest_asyncio.fixture
async def db_initialised():
    from app.database import init_db
    await init_db()
    yield
