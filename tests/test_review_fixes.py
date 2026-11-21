"""Regressions for bugs found in the code review of rbit."""

from __future__ import annotations

from pathlib import Path

import pytest

from rbit.cli import _run_auth
from rbit.cli import main as cli_main
from rbit.install import InstallError, install_skill, remove_plugin, validate_plugin_manifest
from rbit.models import Session
from rbit.storage import StorageManager
from rbit.tools import ToolExecutor


def test_run_file_option_attaches_files(tmp_path: Path) -> None:
    """`rbit run -f FILE` used to be ignored: argparse stored it in `args.file`."""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    rc = cli_main(
        ["run", "-m", "rbit/smart-local", "--output-format", "jsonl", "-d", str(tmp_path),
         "-f", "a.txt", "посмотри файл"]
    )
    assert rc == 0
    sessions = StorageManager(tmp_path).list_sessions()
    first_user = next(m for m in sessions[0].messages if m.role == "user")
    assert "@a.txt" in first_user.content


@pytest.mark.asyncio
async def test_edit_with_empty_old_string_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("print(1)\n", encoding="utf-8")
    executor = ToolExecutor(workspace=tmp_path, session=Session())

    res = await executor.execute(
        "edit", {"filePath": "m.py", "oldString": "", "newString": "X", "replaceAll": True}
    )
    assert res.ok is False
    res = await executor.execute(
        "multi_edit",
        {"filePath": "m.py", "edits": [{"oldString": "", "newString": "X", "replaceAll": True}]},
    )
    assert res.ok is False
    assert target.read_text(encoding="utf-8") == "print(1)\n"


def test_skill_name_of_dots_cannot_target_the_rbit_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("# demo skill\n", encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / ".rbit").mkdir(parents=True)
    marker = ws / ".rbit" / "config.json"
    marker.write_text("{}", encoding="utf-8")

    result = install_skill(str(src), workspace=ws, name="..", force=True)

    assert marker.exists()
    assert result.path.parent.name == "skills"
    assert result.name == "skill"


def test_plugin_names_cannot_traverse(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".rbit" / "plugins").mkdir(parents=True)
    keep = ws / "keep.txt"
    keep.write_text("x", encoding="utf-8")

    for bad in ("..", "../..", ".", "a/b"):
        with pytest.raises(InstallError):
            remove_plugin(ws, bad)
    assert keep.exists() and (ws / ".rbit").exists()

    plugin_src = tmp_path / "plug"
    plugin_src.mkdir()
    (plugin_src / "plugin.json").write_text('{"name": ".."}', encoding="utf-8")
    with pytest.raises(InstallError):
        validate_plugin_manifest(plugin_src)


def test_auth_logout_of_unknown_name_keeps_the_active_key(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    cfg = storage.load_config()
    cfg["credentials"] = {"work": {"api_base": "https://w.example/v1", "api_key": "secret-work", "model": "m"}}
    cfg["active_credential"] = ""
    storage.save_config(cfg)
    assert _run_auth(tmp_path, "switch", None, name="work") == 0

    assert _run_auth(tmp_path, "logout", None, name="does-not-exist") == 1
    assert storage.load_config()["api_key"] == "secret-work"


def test_auth_ls_alias_lists_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_auth(tmp_path, "ls", None) == 0
    assert capsys.readouterr().out.strip()


# --- TUI: model/tool text must never be parsed as markup ---------------------


def _plain(widget: object) -> str:
    content = getattr(widget, "content", None)
    if content is None:
        content = getattr(widget, "renderable", "")
    return getattr(content, "plain", None) or str(content)


@pytest.mark.asyncio
async def test_notice_with_brackets_is_shown_literally_and_does_not_crash(tmp_path: Path) -> None:
    from rbit.app import RbitApp

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        app._post_notice("pytest: [/usr/bin/python] failed in pages/[id].tsx [INST] [/INST]")
        await pilot.pause(0.3)
        shown = " ".join(_plain(w) for w in app.query(".system-notice"))
        assert "[/usr/bin/python]" in shown
        assert "pages/[id].tsx" in shown


@pytest.mark.asyncio
async def test_permission_modal_shows_command_verbatim_and_ignores_early_keys(tmp_path: Path) -> None:
    from rbit.app import RbitApp
    from rbit.modals import PermissionModal

    app = RbitApp(workspace=tmp_path)
    results: list[str] = []
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        command = "rm -rf build[main] [/] && echo done"
        app.push_screen(PermissionModal("bash", command, command=command), results.append)
        await pilot.pause(0.1)
        body = " ".join(_plain(w) for w in app.screen.query(".oc-perm-body"))
        assert "build[main]" in body and "[/]" in body  # nothing swallowed or crashed

        await pilot.press("a")  # typed too early: must not approve "always"
        await pilot.pause(0.1)
        assert results == []

        await pilot.pause(PermissionModal.ARM_DELAY + 0.2)
        await pilot.press("y")
        await pilot.pause(0.2)
        assert results == ["once"]


@pytest.mark.asyncio
async def test_sidebar_file_link_survives_brackets_in_path(tmp_path: Path) -> None:
    from rbit.app import RbitApp
    from rbit.models import FileChangeStat

    app = RbitApp(workspace=tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        app.session.modified_files["pages/[id].tsx"] = FileChangeStat(path="pages/[id].tsx", added=3, removed=1)
        app.session.modified_files["x[/y].py"] = FileChangeStat(path="x[/y].py", added=1, removed=0)
        app._sync_ui()
        await pilot.pause(0.3)
        shown = _plain(app.query_one("#sb-files"))
        assert "[id].tsx" in shown  # `[id]` used to vanish from the label
