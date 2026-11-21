"""Skill / plugin installation is a supply-chain operation: pin down what it must refuse and keep out.

Covered here (the older tests in test_blueprint_remaining.py cover the happy paths):
symlinks and .git never reach the install, unsafe git sources never start git, git runs
non-interactively with `--` before the URL, read-only trees can be removed, a skill and a
plugin with the same name keep separate provenance records, and --force replaces a plugin
without enabling it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rbit.install as install_module
from rbit.install import (
    InstallError,
    fetch_source,
    install_plugin,
    install_skill,
    read_origins,
    remove_plugin,
)

SECRET = "TOP-SECRET-OUTSIDE-THE-SOURCE"


def _symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        return False


def _outside_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text(SECRET, encoding="utf-8")
    return secret


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _plugin_source(tmp_path: Path, name: str = "myplug", body: str = "def on_tool_before(ctx):\n    return ctx\n") -> Path:
    root = tmp_path / "src" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "entry": "plugin.py"}), encoding="utf-8"
    )
    (root / "plugin.py").write_text(body, encoding="utf-8")
    return root


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------ what is copied


def test_a_skill_install_never_copies_symlinks_or_git_data(tmp_path: Path) -> None:
    secret = _outside_secret(tmp_path)
    root = tmp_path / "src" / "demo"
    (root / "references").mkdir(parents=True)
    (root / "references" / "note.md").write_text("a real reference\n", encoding="utf-8")
    (root / "references" / ".git").mkdir()
    (root / "references" / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")
    if not _symlink(root / "references" / "innocent.txt", secret):
        pytest.skip("this account cannot create symlinks")

    ws = _workspace(tmp_path)
    result = install_skill(str(root), workspace=ws)

    installed = result.path
    assert (installed / "references" / "note.md").is_file()
    assert not (installed / "references" / "innocent.txt").exists()
    assert not (installed / "references" / ".git").exists()
    assert SECRET not in _all_text(installed)


def test_a_plugin_install_never_copies_symlinks_or_git_data(tmp_path: Path) -> None:
    secret = _outside_secret(tmp_path)
    root = _plugin_source(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    if not _symlink(root / "docs.txt", secret):
        pytest.skip("this account cannot create symlinks")

    ws = _workspace(tmp_path)
    result = install_plugin(str(root), workspace=ws)

    assert (result.path / "plugin.py").is_file()
    assert not (result.path / "docs.txt").exists()
    assert not (result.path / ".git").exists()
    assert SECRET not in _all_text(result.path)


# ---------------------------------------------------------- git sources


@pytest.mark.parametrize(
    "source",
    ["http://example.com/x.git", "git://example.com/x.git", "file:///tmp/x", "ext::sh -c id", "--upload-pack=touch pwned"],
)
def test_unsafe_sources_are_refused_and_git_is_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"git was started for {source!r}")

    monkeypatch.setattr(install_module.subprocess, "run", forbidden)
    with pytest.raises(InstallError):
        fetch_source(source, tmp_path)


def test_git_clone_is_non_interactive_and_the_url_follows_double_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(command, 128, "", "fatal: repository not found")

    monkeypatch.setattr(install_module.subprocess, "run", fake_run)
    url = "https://example.com/owner/repo.git"
    with pytest.raises(InstallError, match="git clone failed"):
        fetch_source(url, tmp_path)

    command = seen["command"]
    assert seen["env"].get("GIT_TERMINAL_PROMPT") == "0"  # never waits for a password
    assert command[command.index(url) - 1] == "--"  # the URL can never be read as an option
    assert "--depth" in command


# ----------------------------------------------------- removal and provenance


def test_remove_plugin_works_on_a_tree_with_read_only_files(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    result = install_plugin(str(_plugin_source(tmp_path)), workspace=ws, approve=True)
    locked = result.path / "plugin.py"
    os.chmod(locked, stat.S_IREAD)  # read-only, like files from a read-only checkout

    remove_plugin(ws, "myplug")

    assert not result.path.exists()
    config = json.loads((ws / ".rbit" / "config.json").read_text(encoding="utf-8"))
    assert ".rbit/plugins/myplug" not in config["plugins"]


def test_a_skill_and_a_plugin_with_the_same_name_keep_separate_origins(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    skill_root = tmp_path / "src" / "same"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("---\nname: same\ndescription: d\n---\nbody\n", encoding="utf-8")
    install_skill(str(skill_root), workspace=ws)
    install_plugin(str(_plugin_source(tmp_path, name="same")), workspace=ws)

    skills = ws / ".rbit" / "skills"
    plugins = ws / ".rbit" / "plugins"
    assert read_origins(skills)["same"]["kind"] == "skill"
    assert read_origins(plugins)["same"]["kind"] == "plugin"

    remove_plugin(ws, "same")
    assert "same" in read_origins(skills)  # removing the plugin does not erase the skill's record
    assert "same" not in read_origins(plugins)


def test_force_replaces_a_plugin_but_does_not_enable_it(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    source = _plugin_source(tmp_path)
    install_plugin(str(source), workspace=ws)
    (source / "plugin.py").write_text("def on_tool_before(ctx):\n    return {'v': 2}\n", encoding="utf-8")

    with pytest.raises(InstallError, match="already installed"):
        install_plugin(str(source), workspace=ws)
    result = install_plugin(str(source), workspace=ws, force=True)

    assert result.replaced is True
    assert "'v': 2" in (result.path / "plugin.py").read_text(encoding="utf-8")
    config = json.loads((ws / ".rbit" / "config.json").read_text(encoding="utf-8"))
    assert config["plugins"] == []  # third-party code stays off until `plugin enable`


# ------------------------------------------------ Windows junctions and link detection


def _junction(link: Path, target: Path) -> bool:
    """`mklink /J` needs no admin rights; `Path.is_symlink()` is False for the result."""
    if os.name != "nt":
        return False
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    return done.returncode == 0 and link.exists()


def test_a_junction_in_a_skill_source_does_not_leak_a_foreign_folder(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    root = tmp_path / "src" / "demo"
    (root / "references").mkdir(parents=True)
    (root / "references" / "note.md").write_text("real\n", encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")
    if not _junction(root / "references" / "linked", outside):
        pytest.skip("junctions need Windows")

    result = install_skill(str(root), workspace=_workspace(tmp_path))

    assert (result.path / "references" / "note.md").is_file()
    assert not (result.path / "references" / "linked").exists()
    assert SECRET not in _all_text(result.path)


def test_a_junction_as_the_references_folder_itself_is_skipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    root = tmp_path / "src" / "demo"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")
    if not _junction(root / "references", outside):
        pytest.skip("junctions need Windows")

    result = install_skill(str(root), workspace=_workspace(tmp_path))

    assert not (result.path / "references").exists()
    assert SECRET not in _all_text(result.path)


def test_a_junction_in_a_plugin_source_does_not_leak_a_foreign_folder(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    root = _plugin_source(tmp_path)
    if not _junction(root / "data", outside):
        pytest.skip("junctions need Windows")

    result = install_plugin(str(root), workspace=_workspace(tmp_path))

    assert (result.path / "plugin.py").is_file()
    assert not (result.path / "data").exists()
    assert SECRET not in _all_text(result.path)


def test_link_detection_covers_symlinks_without_creating_any(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs everywhere, even where symlinks cannot be created: the filter's logic itself."""
    (tmp_path / "evil.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("y", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "evil.txt")

    ignored = install_module._ignore_links_and_git(str(tmp_path), ["evil.txt", "ok.txt", ".git"])

    assert sorted(ignored) == [".git", "evil.txt"]
