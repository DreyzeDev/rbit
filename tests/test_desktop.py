"""Smoke and contract tests for the Electron Desktop client (`desktop/`)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_desktop_js_syntax_and_csp() -> None:
    desktop = Path(__file__).resolve().parents[1] / "desktop"
    assert (desktop / "main.js").is_file()
    assert (desktop / "preload.js").is_file()
    html = (desktop / "renderer" / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html
    assert "default-src 'self'" in html

    main_js = (desktop / "main.js").read_text(encoding="utf-8")
    assert "contextIsolation: true" in main_js
    assert "nodeIntegration: false" in main_js
    assert "sandbox: true" in main_js

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for rel in ("main.js", "preload.js", "renderer/app.js"):
        proc = subprocess.run([node, "--check", str(desktop / rel)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{rel}: {proc.stderr}"
