"""Tests for the repo-local Codex ONEX plugin shim surface."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "onex"
EXPECTED_CODEX_SKILLS = {
    "aislop-sweep",
    "bus-audit",
    "coderabbit-triage",
    "gap",
    "local-review",
    "merge-sweep",
    "pr-polish",
    "session-bootstrap",
    "session-orchestrator",
    "ticket-pipeline",
}


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n")
    _, raw, _body = text.split("---", 2)
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    return data


def test_codex_marketplace_registers_onex_plugin() -> None:
    marketplace_path = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text())

    assert marketplace["name"] == "omninode-tools"
    plugin = next(
        (item for item in marketplace["plugins"] if item.get("name") == "onex"),
        None,
    )
    assert plugin is not None, "onex plugin not found in marketplace plugins"
    assert plugin["source"] == {"source": "local", "path": "./plugins/onex"}
    assert plugin["policy"]["installation"] == "AVAILABLE"
    assert plugin["policy"]["authentication"] == "ON_INSTALL"


def test_codex_plugin_manifest_points_to_skills() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())

    assert manifest["name"] == "onex"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "ONEX"


def test_codex_skills_have_required_frontmatter() -> None:
    skill_paths = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
    assert {path.parent.name for path in skill_paths} == EXPECTED_CODEX_SKILLS

    for path in skill_paths:
        data = _frontmatter(path)
        assert data["name"] == path.parent.name
        assert "description" in data


def test_codex_shims_remain_dispatch_only() -> None:
    forbidden = (
        "gh pr list",
        "gh pr merge",
        "subprocess.",
        "os.system",
        "grep -R",
    )

    for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
        text = path.read_text()
        assert "Backing node:" in text
        assert "uv run python scripts/run_codex_runtime_request.py" in text
        assert "--target-runtime-address" in text
        assert "ONEX_TARGET_RUNTIME_ADDRESS" in text
        assert "runtime://..." in text
        assert "--compile-only" in text
        assert "event-bus-free preflight" in text
        assert "output_payloads[0]" in text
        if path.parent.name == "session-bootstrap":
            assert '--command-name "session_bootstrap"' in text
            assert "--timeout-ms 30000" in text
            assert '"contract": {' in text
            assert "cost_ceiling_usd" in text
        elif path.parent.name == "session-orchestrator":
            assert '--command-name "session_orchestrator"' in text
            assert "--timeout-ms 300000" in text
            assert "Generate a UUIDv4 `correlation_id`" in text
        elif path.parent.name == "merge-sweep":
            assert '--command-name "pr_lifecycle_orchestrator"' in text
            assert "--timeout-ms 300000" in text
            assert "run_id" in text
            assert "filesystem-safe identifier" in text
        elif path.parent.name == "aislop-sweep":
            assert '--command-name "aislop_sweep"' in text
            assert "--timeout-ms 120000" in text
        elif path.parent.name == "bus-audit":
            assert '--command-name "bus_audit_compute"' in text
            assert "--timeout-ms 30000" in text
            assert "registry_path" in text
            assert "contract_roots" in text
        elif path.parent.name == "gap":
            assert '--command-name "gap_compute"' in text
            assert "--timeout-ms 30000" in text
            assert "subcommand" in text
            assert "repo_roots" in text
        elif path.parent.name == "pr-polish":
            assert '--command-name "pr_polish"' in text
            assert "--timeout-ms 300000" in text
            assert "required_clean_runs" in text
            assert "no_automerge" in text
        elif path.parent.name == "local-review":
            assert '--command-name "local_review"' in text
            assert "--timeout-ms 300000" in text
            assert "correlation_id" in text
            assert "requested_at" in text
        elif path.parent.name == "coderabbit-triage":
            assert '--command-name "coderabbit_triage"' in text
            assert "--timeout-ms 120000" in text
            assert "blocking_count" in text
            assert "resolved_count" in text
        elif path.parent.name == "ticket-pipeline":
            assert '--command-name "ticket_pipeline"' in text
            assert "--timeout-ms 600000" in text
            assert "skip_test_iterate" in text
            assert "not_implemented" in text
        else:
            raise AssertionError(f"unexpected skill path: {path}")
        assert ".venv/bin/python -m omnimarket.nodes." not in text
        assert ".venv/bin/onex run-node" not in text
        for needle in forbidden:
            assert needle not in text


def test_source_codex_skill_examples_use_json_input_contract() -> None:
    source_skill_paths = sorted(
        (REPO_ROOT / "src" / "omnimarket" / "adapters" / "codex" / "skills").glob(
            "*/SKILL.md"
        )
    )
    assert {path.parent.name for path in source_skill_paths} == EXPECTED_CODEX_SKILLS

    for path in source_skill_paths:
        text = path.read_text()
        assert "uv run python scripts/run_codex_runtime_request.py" in text
        assert "--target-runtime-address" in text
        assert "ONEX_TARGET_RUNTIME_ADDRESS" in text
        assert "runtime://..." in text
        assert "--compile-only" in text
        assert "event-bus-free preflight" in text
        assert "output_payloads[0]" in text
        if path.parent.name == "session-bootstrap":
            assert '--command-name "session_bootstrap"' in text
            assert "--timeout-ms 30000" in text
            assert '"contract": {' in text
            assert "cost_ceiling_usd" in text
        elif path.parent.name == "session-orchestrator":
            assert '--command-name "session_orchestrator"' in text
            assert "--timeout-ms 300000" in text
            assert "Generate a UUIDv4 `correlation_id`" in text
        elif path.parent.name == "aislop-sweep":
            assert '--command-name "aislop_sweep"' in text
            assert "--timeout-ms 120000" in text
        elif path.parent.name == "merge-sweep":
            assert '--command-name "pr_lifecycle_orchestrator"' in text
            assert "--timeout-ms 300000" in text
            assert "run_id" in text
            assert "filesystem-safe identifier" in text
        elif path.parent.name == "bus-audit":
            assert '--command-name "bus_audit_compute"' in text
            assert "--timeout-ms 30000" in text
            assert "registry_path" in text
            assert "contract_roots" in text
        elif path.parent.name == "gap":
            assert '--command-name "gap_compute"' in text
            assert "--timeout-ms 30000" in text
            assert "subcommand" in text
            assert "repo_roots" in text
        elif path.parent.name == "pr-polish":
            assert '--command-name "pr_polish"' in text
            assert "--timeout-ms 300000" in text
            assert "required_clean_runs" in text
            assert "no_automerge" in text
        elif path.parent.name == "local-review":
            assert '--command-name "local_review"' in text
            assert "--timeout-ms 300000" in text
            assert "correlation_id" in text
            assert "requested_at" in text
        elif path.parent.name == "coderabbit-triage":
            assert '--command-name "coderabbit_triage"' in text
            assert "--timeout-ms 120000" in text
            assert "blocking_count" in text
            assert "resolved_count" in text
        elif path.parent.name == "ticket-pipeline":
            assert '--command-name "ticket_pipeline"' in text
            assert "--timeout-ms 600000" in text
            assert "skip_test_iterate" in text
            assert "not_implemented" in text
        else:
            raise AssertionError(f"unexpected skill path: {path}")
        assert ".venv/bin/python -m omnimarket.nodes." not in text
        assert ".venv/bin/onex run-node" not in text
