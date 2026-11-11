"""Export SVG screenshots of rbit TUI states into docs/screenshots/.

Usage:
    python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from rbit.app import RbitApp
from rbit.modals import ModelSelectorModal

OUT_DIR = ROOT / "docs" / "screenshots"
SIZE = (130, 40)


async def capture_home() -> bytes:
    app = RbitApp(workspace=ROOT)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.pause()
        return app.export_screenshot(title="rbit — Home View").encode("utf-8")


async def capture_session() -> bytes:
    demo_ws = Path(tempfile.mkdtemp(prefix="rbit-shot-"))
    (demo_ws / "docs" / "notes.md").parent.mkdir(parents=True)
    (demo_ws / "docs" / "notes.md").write_text("# Meeting notes\n\n- demo file for the screenshot\n", encoding="utf-8")
    app = RbitApp(workspace=demo_ws)
    app.config_data["model"] = "rbit/smart-local"
    async with app.run_test(size=SIZE) as pilot:
        inp = app.query_one("#prompt-input")
        inp.focus()
        inp.value = "создай файл calc.py с калькулятором"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        return app.export_screenshot(title="rbit — Agent Session").encode("utf-8")


async def capture_models_modal() -> bytes:
    app = RbitApp(workspace=ROOT)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await app.push_screen(
            ModelSelectorModal(app.config_data.get("model", "rbit/smart-local"))
        )
        await pilot.pause()
        await pilot.pause()
        return app.export_screenshot(title="rbit — Model Selector").encode("utf-8")


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, coro in (
        ("home.svg", capture_home()),
        ("session.svg", capture_session()),
        ("models.svg", capture_models_modal()),
    ):
        data = await coro
        path = OUT_DIR / filename
        path.write_bytes(data)
        print(f"saved {path} ({len(data):,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
