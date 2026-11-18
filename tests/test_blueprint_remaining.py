"""Tests for the remaining blueprint items (rbit 1.5.1).

Covers:
- hook timeout / output cap / call budget / approval gate
- MCP tool risk classification and its permission gate
- subagent budget cap
- router policies (cheapest / best_value) and capability checks
- self-test and dry-run
- skill and plugin installation (incl. the "disabled until approved" rule)
- config migration with backup and the redacted diagnostic bundle
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from rbit.install import (
    InstallError,
    install_plugin,
    install_skill,
    list_installed,
    remove_plugin,
    set_plugin_enabled,
    validate_plugin_manifest,
    verify_sources,
)
from rbit.migrate import diagnostic_bundle, migrate_config
from rbit.models import Session
from rbit.policy import (
    Candidate,
    HookPolicy,
    check_capabilities,
    classify_mcp_tool,
    mcp_action_for,
    normalize_router_policy,
    pick_candidate,
    run_hook_safely,
)
from rbit.router import route_with_policy
from rbit.selftest import simulate, self_test
from rbit.tools import ToolExecutor


# --- hook policy ----------------------------------------------------------


def test_unapproved_hook_is_skipped_and_recorded() -> None:
    """Cloning a repo must never execute its hooks without consent."""
    policy = HookPolicy(require_approval=True)
    assert policy.allows("evil") is False
    assert "evil" in policy.pending
    policy.approved.add("ok")
    assert policy.allows("ok") is True


def test_rejecting_approval_disables_everything() -> None:
    policy = HookPolicy(require_approval=False)
    assert policy.allows("anything") is True


def test_disabled_hook_never_runs() -> None:
    policy = HookPolicy(require_approval=False, disabled={"loud"})
    assert policy.allows("loud") is False


def test_hook_timeout_keeps_the_original_context() -> None:
    async def slow(_ctx: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"leaked": True}

    failures: list[str] = []
    policy = HookPolicy(timeout=0.05, require_approval=False)

    result = asyncio.run(
        run_hook_safely("slow", slow, {"a": 1}, policy, lambda n, e: failures.append(e))
    )
    assert result == {"a": 1}
    assert policy.failures["slow"] == 1
    assert failures and "timeout" in failures[0]


def test_hook_output_is_capped() -> None:
    def hugo(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"content": "x" * 50_000}

    policy = HookPolicy(max_output_chars=100, require_approval=False)
    result = asyncio.run(run_hook_safely("hugo", hugo, {}, policy))
    assert len(result["content"]) < 200
    assert "truncated" in result["content"]


def test_hook_call_budget_stops_invocations() -> None:
    calls: list[int] = []

    def count(_ctx: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {}

    policy = HookPolicy(max_calls=2, require_approval=False)
    for _ in range(5):
        asyncio.run(run_hook_safely("c", count, {}, policy))
    assert len(calls) == 2


def test_hook_policy_round_trips_through_config() -> None:
    policy = HookPolicy.from_config(
        {"hooks": {"require_approval": False, "timeout_s": 2.5, "approved": ["a"], "disabled": ["b"]}}
    )
    assert policy.require_approval is False
    assert policy.timeout == 2.5
    assert policy.allows("a") is True
    assert policy.allows("b") is False
    patch = policy.to_config_patch()
    assert patch["approved"] == ["a"] and patch["disabled"] == ["b"]


# --- MCP tool risk --------------------------------------------------------


def test_mcp_annotations_win() -> None:
    assert classify_mcp_tool({"name": "x", "annotations": {"readOnlyHint": True}}, "x") == "read"
    assert classify_mcp_tool({"name": "x", "annotations": {"destructiveHint": True}}, "x") == "destructive"


def test_mcp_name_fallback_handles_underscores() -> None:
    """`db_delete_rows` has no word boundary before `delete` — matching must too."""
    assert classify_mcp_tool(None, "db_delete_rows") == "destructive"
    assert classify_mcp_tool(None, "get_issue") == "read"
    assert classify_mcp_tool(None, "create_pr") == "write"
    assert classify_mcp_tool(None, "list_files") == "read"


def test_unknown_mcp_tool_is_assumed_to_write() -> None:
    assert classify_mcp_tool(None, "mystery") == "write"


def test_mcp_action_mapping() -> None:
    assert mcp_action_for("read") == "mcp_read"
    assert mcp_action_for("write") == "mcp_write"
    assert mcp_action_for("destructive") == "mcp_destructive"


@pytest.mark.asyncio
async def test_mcp_tool_permission_gate_blocks_without_a_rule(tmp_path: Path) -> None:
    """A destructive MCP tool must be refused when no rule allows it."""
    executor = ToolExecutor(
        workspace=tmp_path,
        session=Session(),
        get_config=lambda: {"permission_mode": "ask"},
    )

    async def deny(_tool: str, _detail: str) -> bool:
        return False

    executor.permission_cb = deny
    blocked = await executor._authorize_mcp_tool("mcp__db__delete_rows", "build")
    assert blocked is not None
    assert blocked.ok is False
    assert "destructive" in blocked.summary


# --- routing policies -----------------------------------------------------


def test_policy_normalization() -> None:
    assert normalize_router_policy("cheap") == "cheapest"
    assert normalize_router_policy("quality") == "best_value"
    assert normalize_router_policy("auto") == "balanced"
    assert normalize_router_policy("nonsense") == "balanced"


def test_pick_cheapest_respects_context_requirement() -> None:
    candidates = [
        Candidate("small", 0.0005, 32_000),
        Candidate("mid", 0.001, 128_000),
        Candidate("big", 0.003, 200_000),
    ]
    chosen, reason = pick_candidate(candidates, "cheapest")
    assert chosen is not None and chosen.model == "small"

    chosen, reason = pick_candidate(candidates, "cheapest", required_context=150_000)
    assert chosen is not None and chosen.model == "big"
    assert "cheapest" in reason


def test_pick_best_value_prefers_quality_per_dollar() -> None:
    candidates = [
        Candidate("tiny", 0.0002, 8_000),
        Candidate("mid", 0.001, 128_000),
        Candidate("pricey", 0.01, 200_000),
    ]
    chosen, _reason = pick_candidate(candidates, "best_value")
    assert chosen is not None and chosen.model == "mid"


def test_pick_nothing_when_budget_too_low() -> None:
    candidates = [Candidate("mid", 0.01, 128_000)]
    chosen, reason = pick_candidate(candidates, "cheapest", max_cost_per_1k=0.0001)
    assert chosen is None
    assert "no model under" in reason


def test_tool_incapable_model_is_never_chosen() -> None:
    candidates = [Candidate("no-tools", 0.0001, 128_000, tools=False)]
    chosen, reason = pick_candidate(candidates, "cheapest")
    assert chosen is None
    assert "no capable" in reason


def test_route_with_policy_end_to_end() -> None:
    candidates = [Candidate("cheap", 0.0005, 32_000), Candidate("best", 0.003, 200_000)]
    cfg = {"model_router": {"enabled": True, "policy": "cheapest"}}
    decision = route_with_policy("refactor auth", "best", cfg, candidates=candidates)
    assert decision.model == "cheap"
    assert "cheapest" in decision.reason

    cfg["model_router"]["pin"] = "pinned-model"
    assert route_with_policy("refactor", "best", cfg, candidates=candidates).pinned is True


def test_capability_check_flags_missing_tools() -> None:
    caps = check_capabilities("m", supports_tools=False)
    assert caps.usable is False
    assert "tool calling" in caps.missing

    caps = check_capabilities("m", context_window=8_000, required_context=32_000)
    assert caps.usable is False
    assert "context window" in caps.missing[0]


# --- self-test and dry-run ------------------------------------------------


def test_self_test_passes_on_a_healthy_install(tmp_path: Path) -> None:
    report = self_test(tmp_path)
    assert report.ok is True, report.text()
    names = {c.name for c in report.checks}
    assert {"imports", "tool schema", "permission engine"} <= names


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {})
    result = simulate("write", {"filePath": "a.txt", "content": "hello"}, executor)
    assert result.would_change is True
    assert "would modify" in result.summary
    assert not (tmp_path / "a.txt").exists()


def test_dry_run_reports_shell_without_running(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {})
    marker = tmp_path / "marker.txt"
    result = simulate("bash", {"command": f"touch {marker}"}, executor)
    assert result.would_change is True
    assert "would run" in result.summary
    assert not marker.exists()


def test_dry_run_marks_reads_as_harmless(tmp_path: Path) -> None:
    executor = ToolExecutor(workspace=tmp_path, session=Session(), get_config=lambda: {})
    assert simulate("read", {"filePath": "a.txt"}, executor).would_change is False


# --- skill and plugin installation ---------------------------------------


@pytest.fixture()
def skill_source(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "demo-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n---\nUse this skill.\n", encoding="utf-8"
    )
    return root


def test_install_skill_names_it_after_the_directory(tmp_path: Path, skill_source: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    result = install_skill(str(skill_source), workspace=ws)
    assert result.name == "demo-skill"
    assert (ws / ".rbit" / "skills" / "demo-skill" / "SKILL.md").is_file()


def test_install_skill_refuses_to_clobber_a_changed_copy(
    tmp_path: Path, skill_source: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    install_skill(str(skill_source), workspace=ws)
    (skill_source / "SKILL.md").write_text("---\nname: demo-skill\n---\nchanged\n", encoding="utf-8")

    with pytest.raises(InstallError, match="already exists"):
        install_skill(str(skill_source), workspace=ws)

    replaced = install_skill(str(skill_source), workspace=ws, force=True)
    assert replaced.replaced is True


def test_verify_sources_detects_local_edits(tmp_path: Path, skill_source: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    install_skill(str(skill_source), workspace=ws)
    assert verify_sources(ws) == []

    target = ws / ".rbit" / "skills" / "demo-skill" / "SKILL.md"
    target.write_text("tampered", encoding="utf-8")
    warnings = verify_sources(ws)
    assert any("changed on disk" in w for w in warnings)


def test_install_skill_rejects_a_source_without_skill_md(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    empty = tmp_path / "empty"
    empty.mkdir()
    ws.mkdir()
    with pytest.raises(InstallError, match="no SKILL.md"):
        install_skill(str(empty), workspace=ws)


@pytest.fixture()
def plugin_source(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "myplug"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "myplug", "version": "1.0.0", "entry": "plugin.py"}),
        encoding="utf-8",
    )
    (root / "plugin.py").write_text("def on_tool_before(ctx):\n    return ctx\n", encoding="utf-8")
    return root


def test_plugin_manifest_validation(plugin_source: Path) -> None:
    manifest = validate_plugin_manifest(plugin_source)
    assert manifest["name"] == "myplug"


def test_plugin_manifest_tolerates_a_bom(tmp_path: Path) -> None:
    """Windows editors write a BOM; a manifest must still parse."""
    root = tmp_path / "bomplug"
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps({"name": "bomplug", "entry": "p.py"}), encoding="utf-8-sig"
    )
    (root / "p.py").write_text("x = 1\n", encoding="utf-8")
    assert validate_plugin_manifest(root)["name"] == "bomplug"


def test_plugin_manifest_rejects_bad_input(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    with pytest.raises(InstallError, match="missing plugin.json"):
        validate_plugin_manifest(root)

    (root / "plugin.json").write_text(json.dumps({"version": "1"}), encoding="utf-8")
    with pytest.raises(InstallError, match="must declare a `name`"):
        validate_plugin_manifest(root)

    (root / "plugin.json").write_text(json.dumps({"name": "ok", "entry": "nope.py"}), encoding="utf-8")
    with pytest.raises(InstallError, match="does not exist"):
        validate_plugin_manifest(root)


def test_plugin_installs_disabled_until_enabled(tmp_path: Path, plugin_source: Path) -> None:
    """Third-party code must not start running just because it was downloaded."""
    ws = tmp_path / "ws"
    ws.mkdir()
    result = install_plugin(str(plugin_source), workspace=ws)
    assert result.name == "myplug"

    config_path = ws / ".rbit" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config.get("plugins") == []

    set_plugin_enabled(ws, "myplug", True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert ".rbit/plugins/myplug" in config["plugins"]

    remove_plugin(ws, "myplug")
    assert not (ws / ".rbit" / "plugins" / "myplug").exists()
    rows = [r for r in list_installed(ws) if r["kind"] == "plugin"]
    assert rows == []


def test_plugin_install_can_approve_immediately(tmp_path: Path, plugin_source: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    install_plugin(str(plugin_source), workspace=ws, approve=True)
    config = json.loads((ws / ".rbit" / "config.json").read_text(encoding="utf-8"))
    assert ".rbit/plugins/myplug" in config["plugins"]


def test_list_installed_records_origin(tmp_path: Path, skill_source: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    install_skill(str(skill_source), workspace=ws)
    rows = list_installed(ws)
    assert rows and rows[0]["name"] == "demo-skill"
    assert rows[0]["remote"] is False
    assert rows[0]["installed_at"]


# --- migration and diagnostics -------------------------------------------


def test_migrate_upgrades_legacy_keys_and_backs_up(tmp_path: Path) -> None:
    config_dir = tmp_path / ".rbit"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(
        json.dumps({"budget_usd": 3, "auto_approve": True}), encoding="utf-8"
    )

    result = migrate_config(tmp_path)
    assert result.changes
    assert result.backup is not None and result.backup.is_file()

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["permission_mode"] == "auto"
    assert migrated["daily_budget_usd"] == 3.0
    assert "budget_usd" not in migrated
    assert migrated["audit_log"] is True
    assert migrated["config_version"] == 2


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    config_dir = tmp_path / ".rbit"
    config_dir.mkdir()
    path = config_dir / "config.json"
    original = json.dumps({"budget_usd": 3})
    path.write_text(original, encoding="utf-8")

    result = migrate_config(tmp_path, dry_run=True)
    assert result.dry_run is True
    assert result.changes
    assert path.read_text(encoding="utf-8") == original
    assert result.backup is None


def test_migrate_is_a_no_op_when_current(tmp_path: Path) -> None:
    config_dir = tmp_path / ".rbit"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(
        json.dumps(
            {
                "permission_mode": "edits",
                "audit_log": True,
                "prompt_injection_guard": "warn",
                "budget_mode": "warn",
                "config_version": 2,
            }
        ),
        encoding="utf-8",
    )
    result = migrate_config(tmp_path)
    assert result.changes == []
    assert "up to date" in result.summary()


def test_diagnostic_bundle_redacts_secrets(tmp_path: Path) -> None:
    config_dir = tmp_path / ".rbit"
    config_dir.mkdir()
    secret = "sk" + "-" + "live-abcdefgh1234"
    (config_dir / "config.json").write_text(
        json.dumps({"api_key": secret, "model": "m"}), encoding="utf-8"
    )

    bundle = diagnostic_bundle(tmp_path)
    assert secret not in bundle
    assert "***REDACTED***" in bundle
    assert '"model": "m"' in bundle
    assert "## environment" in bundle
    assert "## self-test" in bundle
