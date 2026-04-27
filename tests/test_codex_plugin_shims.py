"""Tests for the repo-local Codex ONEX plugin shim surface."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "onex"


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
    plugin = marketplace["plugins"][0]
    assert plugin["name"] == "onex"
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
    assert {path.parent.name for path in skill_paths} == {
        "aislop-sweep",
        "merge-sweep",
        "session-bootstrap",
        "session-orchestrator",
    }

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
        assert "scripts/run_codex_runtime_request.py" in text
        assert "output_payloads[0]" in text
        if path.parent.name == "session-bootstrap":
            assert '--node-alias "session_bootstrap"' in text
            assert "--timeout-ms 30000" in text
        elif path.parent.name == "session-orchestrator":
            assert '--node-alias "session_orchestrator"' in text
            assert "--timeout-ms 300000" in text
        elif path.parent.name == "merge-sweep":
            assert '--node-alias "pr_lifecycle_orchestrator"' in text
            assert "--timeout-ms 300000" in text
        elif path.parent.name == "aislop-sweep":
            assert '--node-alias "aislop_sweep"' in text
            assert "--timeout-ms 120000" in text
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
    assert source_skill_paths

    for path in source_skill_paths:
        text = path.read_text()
        assert "scripts/run_codex_runtime_request.py" in text
        assert "output_payloads[0]" in text
        if path.parent.name == "session-bootstrap":
            assert '--node-alias "session_bootstrap"' in text
            assert "--timeout-ms 30000" in text
        elif path.parent.name == "session-orchestrator":
            assert '--node-alias "session_orchestrator"' in text
            assert "--timeout-ms 300000" in text
        elif path.parent.name == "aislop-sweep":
            assert '--node-alias "aislop_sweep"' in text
            assert "--timeout-ms 120000" in text
        else:
            raise AssertionError(f"unexpected skill path: {path}")
        assert ".venv/bin/python -m omnimarket.nodes." not in text
        assert ".venv/bin/onex run-node" not in text
