"""Tool collector acceptance and adapter regressions."""

from __future__ import annotations

import datetime as dt
import json
import plistlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_cli as cli,
)
from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_collect_tools as tools,
)
from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_render as render,
)
from omnimarket.nodes.node_capability_catalog_effect.models import (
    model_capability_catalog_collect_tools_request as request_model,
)
from omnimarket.nodes.node_capability_catalog_effect.models import (
    model_capability_catalog_sources as sources_model,
)
from omnimarket.nodes.node_capability_catalog_effect.models.model_capability_catalog import (
    ProbeState,
)

ModelCapabilityCatalogCollectToolsRequest = (
    request_model.ModelCapabilityCatalogCollectToolsRequest
)
RemoteResult = sources_model.RemoteResult

NOW = "2026-10-09T00:30:00Z"
FACTS: dict[str, Any] = {
    "engines": [
        {"engine_id": "codex", "display_name": "Codex", "invocation": "codex exec"},
        {
            "engine_id": "jev",
            "display_name": "Jev (TypeSafe)",
            "invocation": "typed decisions",
            "credential_ref": "llm.typesafe.api_key",
        },
    ],
    "front_door_skills": ["omni:capabilities"],
}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def tool_request(tmp_path: Path) -> ModelCapabilityCatalogCollectToolsRequest:
    home = tmp_path / "user"
    root = tmp_path / "registry"
    home.mkdir()
    root.mkdir()
    return ModelCapabilityCatalogCollectToolsRequest(
        home=home, registry_root=root, now=NOW
    )


def handler(
    responses: dict[tuple[str, ...], str],
) -> tools.HandlerCapabilityCatalogCollectTools:
    def runner(argv: Any, timeout: float) -> RemoteResult:
        assert timeout <= 30
        key = tuple(argv)
        if key in responses:
            return RemoteResult(stdout=responses[key], exit_code=0, timed_out=False)
        return RemoteResult(stdout="", exit_code=127, timed_out=False)

    return tools.HandlerCapabilityCatalogCollectTools(runner=runner, static_facts=FACTS)


def test_dead_plist_both_prefixes(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    agents = tool_request.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    labels = [
        "ai.omninode.loaded",
        "com.omninode.failing",
        "ai.omninode.dead",
        "com.omninode.dead",
    ]
    for label in [*labels, "com.other.ignored"]:
        (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
    catalog = (
        handler(
            {
                (
                    "launchctl",
                    "list",
                ): "PID\tStatus\tLabel\n-\t0\tai.omninode.loaded\n42\t7\tcom.omninode.failing\n"
            }
        )
        .handle(tool_request)
        .catalog
    )
    jobs = {j.label: j for j in catalog.launchd_jobs}
    assert set(jobs) == set(labels)
    assert jobs["ai.omninode.loaded"].state == "loaded-ok"
    assert jobs["com.omninode.failing"].state == "loaded-failing"
    assert jobs["com.omninode.failing"].last_exit_code == 7
    assert [jobs[label].state for label in labels[2:]] == ["dead", "dead"]


def test_unreadable_launchctl_never_calls_plist_dead(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    agents = tool_request.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.omninode.job.plist").write_bytes(
        plistlib.dumps({"Label": "ai.omninode.job"})
    )
    catalog = handler({}).handle(tool_request).catalog
    assert catalog.launchd_jobs[0].state == "unknown"
    assert catalog.launchd_jobs[0].probe.state == ProbeState.UNKNOWN


def test_plugin_lag(tool_request: ModelCapabilityCatalogCollectToolsRequest) -> None:
    clone = tool_request.home / "marketplace"
    write_json(
        tool_request.home / ".claude/plugins/installed_plugins.json",
        {
            "version": 2,
            "plugins": {
                "omni@internal": [
                    {
                        "version": "0.4.51",
                        "installPath": str(tool_request.home / "cache/omni"),
                    }
                ]
            },
        },
    )
    write_json(
        tool_request.home / ".claude/plugins/known_marketplaces.json",
        {"internal": {"installLocation": str(clone)}},
    )
    catalog = (
        handler(
            {
                (
                    "git",
                    "-C",
                    str(clone),
                    "show",
                    "origin/main:plugins/omni/.claude-plugin/plugin.json",
                ): '{"version":"0.4.53"}'
            }
        )
        .handle(tool_request)
        .catalog
    )
    plugin = catalog.plugins[0]
    assert (plugin.installed_version, plugin.available_version) == ("0.4.51", "0.4.53")
    assert plugin.update_command == "claude plugin update omni@internal"
    assert "claude plugin update omni@internal" in render.render_full_markdown(
        catalog, FACTS
    )
    assert "0.4.51->0.4.53" in render.render_digest(catalog, FACTS)


def test_never_reads_secret_value(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    database = tool_request.home / ".omninode/delegation/delegation.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE local_inference_credentials "
            "(secret_ref TEXT, provider TEXT, secret_value TEXT)"
        )
        conn.execute(
            "INSERT INTO local_inference_credentials VALUES (?, ?, ?)",
            ("llm.typesafe.api_key", "typesafe", "fixture-value-must-not-be-read"),
        )
    statements: list[str] = []

    def connect(uri: str, *, uri_enabled: bool) -> sqlite3.Connection:
        assert uri.endswith("?mode=ro")
        assert uri_enabled
        conn = sqlite3.connect(uri, uri=True)
        conn.set_authorizer(
            lambda action, _table, column, _db, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and column == "secret_value"
                else sqlite3.SQLITE_OK
            )
        )
        conn.set_trace_callback(statements.append)
        return conn

    collector = handler({})
    collector.connect = connect
    catalog = collector.handle(tool_request).catalog
    assert [r.to_dict() for r in catalog.credential_refs] == [
        {"secret_ref": "llm.typesafe.api_key", "provider": "typesafe"}
    ]
    assert statements == [
        "SELECT secret_ref, provider FROM local_inference_credentials"
    ]
    assert "fixture-value-must-not-be-read" not in json.dumps(catalog.to_dict())
    assert any(
        "llm.typesafe.api_key" in hit
        for hit in render.find_term(catalog, FACTS, "typesafe")
    )


def test_cache_and_source_skills_and_seven_day_use(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    cache = tool_request.home / "cache/omni"
    source = tool_request.registry_root / "omniclaude/plugins/onex"
    for directory in [cache / "skills/capabilities", source / "skills/source-only"]:
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("skill")
    write_json(
        tool_request.home / ".claude/plugins/installed_plugins.json",
        {"plugins": {"omni@internal": [{"version": "1", "installPath": str(cache)}]}},
    )
    now = dt.datetime.fromisoformat(NOW)
    recent = int((now - dt.timedelta(days=1)).timestamp() * 1000)
    stale = int((now - dt.timedelta(days=8)).timestamp() * 1000)
    write_json(
        tool_request.home / ".claude.json",
        {
            "skillUsage": {
                "omni:capabilities": {"lastUsedAt": recent},
                "onex:source-only": {"lastUsedAt": stale},
            }
        },
    )
    skills = handler({}).handle(tool_request).catalog.skills
    by_name = {s.name: s for s in skills}
    assert by_name["omni:capabilities"].front_door
    assert by_name["omni:capabilities"].last_used_at is not None
    assert by_name["onex:source-only"].last_used_at is None
    assert {s.origin for s in skills} == {"cache", "source"}


def test_cli_mcp_and_engine_versions(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    wrapper = str(tool_request.registry_root / "omnibase_infra/scripts/onex")
    catalog = (
        handler(
            {
                (
                    wrapper,
                    "--help",
                ): "Commands:\n  delegate  Delegate work\n  lab-work  Lab work\n",
                (
                    "claude",
                    "mcp",
                    "list",
                ): (
                    "Checking MCP server health...\n"
                    "linear: https://example.invalid/mcp (HTTP) - ✓ Connected\n"
                    "missing: command --token do-not-retain - ✗ Failed to connect\n"
                ),
                ("codex", "--version"): "codex-cli 0.80.0\n",
            }
        )
        .handle(tool_request)
        .catalog
    )
    assert {c.name for c in catalog.clis} >= {"onex delegate", "onex lab-work"}
    assert {s.name for s in catalog.mcp_servers} == {"linear", "missing"}
    assert "do-not-retain" not in json.dumps(catalog.to_dict())
    codex = next(e for e in catalog.engines if e.engine_id == "codex")
    assert codex.version == "codex-cli 0.80.0"
    assert codex.state == ProbeState.OK
    assert (
        next(e for e in catalog.engines if e.engine_id == "jev").state
        == ProbeState.UNKNOWN
    )


def test_recent_receipts_and_ledger_counts(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    for run, timestamp in [("recent", NOW), ("old", "2026-09-01T00:00:00Z")]:
        write_json(
            tool_request.registry_root / f".onex_state/runs/{run}/receipt.json",
            {
                "run_id": run,
                "started_at": timestamp,
                "backend_id": "local-coder",
                "model": "test-model",
                "status": "ok",
                "tokens_in": 10,
                "tokens_out": 20,
                "endpoint": "http://lab.invalid",
            },
        )
    ledger = tool_request.registry_root / "docs/tracking/ROLLING_WORK_LEDGER.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        f"{NOW} | STATUS | lane=fixture | engine=codex | host=lab-fixture | "
        "tokens_in=4 | tokens_out=8 | fixture\n"
    )
    request = tool_request.model_copy(update={"ledger_path": ledger})
    catalog = handler({}).handle(request).catalog
    assert len(catalog.delegation_runs) == 1
    assert catalog.delegation_runs[0]["model"] == "test-model"
    assert catalog.ledger_usage["engines"]["codex"]["tokens_out"] == 8


def test_receipt_without_runtime_identity_is_unknown(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    write_json(
        tool_request.registry_root / ".onex_state/runs/unreadable/receipt.json",
        {"receipt": None, "status": "failed"},
    )
    write_json(
        tool_request.registry_root / ".onex_state/runs/bus/receipt.json",
        {
            "backend_id": "local-coder",
            "model": "fixture-model",
            "receipt": {"runtime_identity": {"stamped_at": NOW, "host": "lab-fixture"}},
        },
    )
    catalog = handler({}).handle(tool_request).catalog
    assert len(catalog.delegation_runs) == 1
    assert catalog.delegation_runs[0]["host"] == "lab-fixture"
    assert catalog.tool_probes["delegation_runs"].state == ProbeState.UNKNOWN


def test_collect_tools_cli_over_contract_bus(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tool_request.registry_root))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tool_request.home))
    monkeypatch.setattr(
        tools,
        "default_runner",
        lambda _argv, _timeout: RemoteResult(stdout="", exit_code=127, timed_out=False),
    )
    assert cli.main(["collect", "--tools", "--out-dir", str(tmp_path / "out")]) == 0
    catalog = json.loads((tmp_path / "out/catalog.json").read_text())
    assert "engines" in catalog
    assert "tool_probes" in catalog


def test_publish_preserves_tool_sections(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_capability_catalog_effect.handlers import (
        handler_capability_catalog_publish as pub,
    )

    catalog = (
        handler({("codex", "--version"): "codex-cli fixture"})
        .handle(tool_request)
        .catalog
    )
    sources = tmp_path / "sources.yaml"
    sources.write_text("hosts: {}")

    def lab_collect(
        *args: Any, **kwargs: Any
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return [], [], []

    def tool_collect(**kwargs: Any) -> Any:
        return catalog

    assert (
        pub.run(
            registry_root=tool_request.registry_root,
            onex_state_dir=tmp_path / "state",
            kb_repo=tmp_path / "unused",
            sources_path=sources,
            dry_run=True,
            collect=lab_collect,
            collect_tools=tool_collect,
            clock=lambda: NOW,
        )
        == 0
    )
    stored = json.loads((tmp_path / "state/capability/catalog.json").read_text())
    assert stored["engines"][0]["version"] == "codex-cli fixture"
    assert "tool_probes" in stored


def test_delegation_health_verdict_is_reused(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    verdict = tool_request.registry_root / "verdict.json"
    write_json(
        verdict, {"read_at": NOW, "engines": {"jev": {"state": "down", "exit_code": 1}}}
    )
    with_health = tool_request.model_copy(update={"delegation_health_path": verdict})
    catalog = handler({}).handle(with_health).catalog
    assert (
        next(e for e in catalog.engines if e.engine_id == "jev").state
        == ProbeState.DOWN
    )
    write_json(
        verdict,
        {"read_at": "2026-09-01T00:00:00Z", "engines": {"jev": {"state": "ok"}}},
    )
    stale = handler({}).handle(with_health).catalog
    assert (
        next(e for e in stale.engines if e.engine_id == "jev").state
        == ProbeState.UNKNOWN
    )


def test_tool_readings_do_not_refresh_structural_publish(
    tool_request: ModelCapabilityCatalogCollectToolsRequest,
) -> None:
    from omnimarket.nodes.node_capability_catalog_effect.handlers import (
        handler_capability_catalog_publish as pub,
    )

    original = (
        handler({("codex", "--version"): "codex-cli fixture"})
        .handle(tool_request)
        .catalog
    )
    readings = replace(
        original,
        delegation_runs=({"run_id": "new-run", "tokens_out": 20},),
        ledger_usage={"engines": {"codex": {"tokens_out": 30}}},
    )
    assert pub.structural_hash(original) == pub.structural_hash(readings)
    changed = replace(
        original, engines=(replace(original.engines[0], version="new-version"),)
    )
    assert pub.structural_hash(original) != pub.structural_hash(changed)
