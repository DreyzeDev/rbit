"""Optional browser automation tool backed by Playwright.

Install with `pip install rbit[browser]`. The tool is only exposed when
Playwright is importable; each action runs in a worker thread because
Playwright's sync API is not asyncio-native.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class BrowserResult:
    ok: bool
    summary: str
    output: str
    screenshot: str = ""


class BrowserSession:
    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    @staticmethod
    def available() -> bool:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = context.new_page()

    def open(self, url: str) -> BrowserResult:
        self._ensure()
        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        title = self._page.title()
        text = self._page.inner_text("body")[:20_000]
        return BrowserResult(True, f"opened: {title}", f"# {title}\n\n{self._page.url}\n\n{text}")

    def click(self, selector: str) -> BrowserResult:
        self._ensure()
        assert self._page is not None
        self._page.click(selector, timeout=15_000)
        return BrowserResult(True, f"clicked: {selector}", self._page.url)

    def fill(self, selector: str, value: str) -> BrowserResult:
        self._ensure()
        assert self._page is not None
        self._page.fill(selector, value, timeout=15_000)
        return BrowserResult(True, f"filled: {selector}", self._page.url)

    def content(self) -> BrowserResult:
        self._ensure()
        assert self._page is not None
        return BrowserResult(True, "page content", self._page.content()[:50_000])

    def screenshot(self, path: str) -> BrowserResult:
        self._ensure()
        assert self._page is not None
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(target), full_page=True)
        return BrowserResult(True, f"screenshot: {target.name}", str(target), screenshot=str(target))

    def close(self) -> BrowserResult:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._page = None
        self._playwright = None
        return BrowserResult(True, "browser closed", "Browser session closed.")


def run_browser_action(action: str, **kwargs: Any) -> BrowserResult:
    session = BrowserSession()
    try:
        if action == "open":
            return session.open(str(kwargs.get("url") or ""))
        if action == "click":
            return session.click(str(kwargs.get("selector") or ""))
        if action == "fill":
            return session.fill(str(kwargs.get("selector") or ""), str(kwargs.get("value") or ""))
        if action in ("content", "snapshot"):
            return session.content()
        if action == "screenshot":
            return session.screenshot(str(kwargs.get("path") or "screenshot.png"))
        if action == "close":
            return session.close()
        return BrowserResult(False, f"unknown browser action: {action}", "Use open|click|fill|content|screenshot|close.")
    except Exception as exc:  # noqa: BLE001 - Playwright errors are user-facing
        return BrowserResult(False, "browser action failed", f"{type(exc).__name__}: {exc}")
    finally:
        if action != "close" and session._browser is not None:
            session.close()
