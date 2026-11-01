# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-path authority serves the locations the nodes used to hold as literals (OMN-21623)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers import runtime_paths
from omnimarket.handlers.runtime_paths import (
    runtime_dir,
    runtime_location,
    runtime_path,
    runtime_path_list,
)


class TestAuthorityValues:
    @pytest.mark.parametrize(
        ("reference", "expected"),
        [
            ("emit_daemon.socket_path", "/tmp/onex-emit.sock"),
            ("emit_daemon.pid_path", "/tmp/onex-emit.pid"),
            ("emit_daemon.spool_dir", "/tmp/onex-event-spool"),
            ("emit_daemon.health_probe_socket_path", "/tmp/onex_emit.sock"),
            ("claude.tasks_dir", "~/.claude/tasks"),
            (
                "claude.installed_plugins_file",
                "~/.claude/plugins/installed_plugins.json",
            ),
            ("codex.home_dir", "~/.codex"),
            ("onex_state.default_dir", "~/.onex_state"),
            ("landing_tlc_receipt.state_dir", "~/.local/state/omni/landing"),
            ("uv.config_home", "~/.config"),
            ("runtime_host.ci_disk_path", "/var/lib/docker"),
        ],
    )
    def test_path_keeps_its_previous_value(self, reference: str, expected: str) -> None:
        assert runtime_path(reference) == expected

    def test_home_roots_keep_their_order(self) -> None:
        assert runtime_path_list("brief_prerequisites.home_roots") == (
            "/",
            "~/",
            "$HOME/",
            "${HOME}/",
            "$ONEX_REGISTRY_ROOT/",
            "${ONEX_REGISTRY_ROOT}/",
        )

    def test_unit_directory_patterns_match_the_scanned_directories(self) -> None:
        patterns = runtime_path_list("automation_liveness.unit_directory_patterns")
        assert len(patterns) == 5


class TestFailClosed:
    def test_unknown_reference_raises(self) -> None:
        with pytest.raises(KeyError, match=r"emit_daemon\.nope"):
            runtime_path("emit_daemon.nope")

    def test_list_read_as_path_raises(self) -> None:
        with pytest.raises(KeyError, match="non-empty string"):
            runtime_path("brief_prerequisites.home_roots")

    def test_path_read_as_list_raises(self) -> None:
        with pytest.raises(KeyError, match="non-empty list"):
            runtime_path_list("emit_daemon.socket_path")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            runtime_paths._read_authority(tmp_path / "absent.yaml")

    def test_root_must_be_a_mapping(self, tmp_path: Path) -> None:
        bad = tmp_path / "runtime_paths.yaml"
        bad.write_text(yaml.safe_dump(["a"]), encoding="utf-8")
        with pytest.raises(KeyError, match="root must be a mapping"):
            runtime_paths._read_authority(bad)

    def test_blank_value_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blank = tmp_path / "runtime_paths.yaml"
        blank.write_text(yaml.safe_dump({"a": {"b": " "}}), encoding="utf-8")
        monkeypatch.setattr(
            runtime_paths,
            "_load_authority",
            lambda: runtime_paths._read_authority(blank),
        )
        with pytest.raises(KeyError, match="non-empty string"):
            runtime_path("a.b")


def test_shadow_parity_report_path_keeps_its_previous_value() -> None:
    assert (
        runtime_path("shadow_mode_parity_proof.report_path")
        == "/tmp/omn-15971-shadow-parity-report.json"
    )


class TestLocations:
    def test_runtime_location_prefers_env_then_xdg_then_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args = (
            "emit_daemon.socket_path",
            "ONEX_EMIT_SOCKET_PATH",
            ("onex", "emit.sock"),
        )
        monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert runtime_location(*args) == "/tmp/onex-emit.sock"
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
        assert runtime_location(*args) == "/run/user/1/onex/emit.sock"
        monkeypatch.setenv("ONEX_EMIT_SOCKET_PATH", "/x/e.sock")
        assert runtime_location(*args) == "/x/e.sock"

    def test_runtime_dir_expands_home_and_honours_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_TASKS_DIR", raising=False)
        assert runtime_dir("claude.tasks_dir", "CLAUDE_TASKS_DIR") == str(
            tmp_path / ".claude" / "tasks"
        )
        monkeypatch.setenv("CLAUDE_TASKS_DIR", "~/elsewhere")
        assert runtime_dir("claude.tasks_dir", "CLAUDE_TASKS_DIR") == str(
            tmp_path / "elsewhere"
        )
        assert runtime_dir("claude.tasks_dir") == str(tmp_path / ".claude" / "tasks")


def test_slice_has_no_literal_path_findings() -> None:
    """Every migrated module is scanned for forbidden filesystem literals."""
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "scripts/shadow_mode_parity_proof.py",
        root / "src/omnimarket/events/emit_client.py",
        root
        / "src/omnimarket/nodes/node_automation_liveness_overlay_check_compute/handlers/handler_automation_liveness_installer_scan.py",
        root
        / "src/omnimarket/nodes/node_brief_prerequisites_compute/handlers/handler_brief_prerequisites.py",
        root
        / "src/omnimarket/nodes/node_dispatch_worker/handlers/handler_dispatch_worker.py",
        root / "src/omnimarket/nodes/node_emit_daemon/__main__.py",
        root / "src/omnimarket/nodes/node_emit_daemon/handlers/handler_emit_daemon.py",
        root
        / "src/omnimarket/nodes/node_emit_daemon/models/model_emit_daemon_config.py",
        root
        / "src/omnimarket/nodes/node_environment_health_scanner/handlers/handler_environment_health_scanner.py",
        root
        / "src/omnimarket/nodes/node_environment_health_scanner/handlers/prober_emit_daemon.py",
        root
        / "src/omnimarket/nodes/node_install_canary_effect/handlers/handler_install_canary.py",
        root
        / "src/omnimarket/nodes/node_lab_disk_growth_effect/handlers/handler_lab_uv_cache_colocate.py",
        root
        / "src/omnimarket/nodes/node_landing_tlc_receipt_effect/runtime_landing_tlc_receipt_cli.py",
        root / "src/omnimarket/nodes/node_merge_sweep_compute/__main__.py",
        root / "src/omnimarket/nodes/node_merge_sweep_compute/consumer.py",
        root
        / "src/omnimarket/nodes/node_pr_lifecycle_orchestrator/handlers/handler_pr_lifecycle_orchestrator.py",
        root
        / "src/omnimarket/nodes/node_runner_orchestrator/models/model_runner_result.py",
    ]
    assert all(path.is_file() for path in paths), "a target module is missing"
    validator = ValidatorContractsAndDi()
    assert any(
        rule.rule_id == "literal_path" and rule.enabled
        for rule in validator.contract.rules
    ), "literal_path rule is not enabled"
    result = validator.validate(paths)
    assert result.metadata is not None
    assert result.metadata.files_processed == len(paths)
    findings = [
        issue
        for issue in result.issues
        if issue.rule_name in {"literal_path", "unparseable_file"}
    ]
    assert not findings, findings
