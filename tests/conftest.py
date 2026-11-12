"""Shared pytest fixtures: isolated workspace and a scrubbed provider environment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_PROVIDER_ENV_PREFIXES = ("RBIT_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_", "DEEPSEEK_")


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes API keys and provider overrides so tests never hit the network by accident."""
    for key in list(os.environ):
        if key.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Temporary workspace with an empty .rbit state directory."""
    (tmp_path / ".rbit").mkdir()
    return tmp_path
