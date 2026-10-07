# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real subprocess regressions for diff-derived proof environment and diagnostics."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.cli.reporting import _summarize_payload
from omnimarket.nodes.node_dod_verify import __main__ as dod_cli
from omnimarket.nodes.node_dod_verify.__main__ import _build_probe_stdout
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_behavior_proof_dod_evidence_item,
)

pytestmark = pytest.mark.unit


def _item(command: str) -> dict[str, object]:
    return {
        "id": "dod-occ-diff-derived-behavior-proof",
        "description": "diff-derived behavior proof",
        "checks": [{"check_type": "test_passes", "check_value": command}],
    }


@pytest.mark.parametrize("check_type", ["test_passes", "command"])
def test_dod_verify_proof_ignores_ambient_postgres_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_type: str
) -> None:
    keys = [
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "DSN",
        "CUSTOM_POSTGRES_DSN",
        "DATABASE_URL",
        "OMNIBASE_INFRA_DB_URL",
        "PGHOST",
        "PGPORT",
        "PGPASSWORD",
        "PGSERVICEFILE",
    ]
    for key in keys:
        monkeypatch.setenv(key, "ambient-fixture-value")
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    test = tmp_path / "test_env.py"
    test.write_text(
        "import os\n"
        "def test_environment():\n"
        f"    keys = {keys!r}\n"
        "    assert all(key not in os.environ for key in keys), 'ambient database configuration leaked'\n"
    )
    check = {
        "check_type": check_type,
        "check_value": f"python3 -m pytest {shlex.quote(str(test))} -q",
    }
    ok, message = EvidenceCollector()._run_command_check(check, "OMN-20663")
    assert ok, message
    # Scrubbing the child must never mutate the launching process.
    assert os.environ["POSTGRES_PASSWORD"] == "ambient-fixture-value"


def test_dod_verify_proof_ignores_ambient_postgres_env_declares_database_requirement() -> (
    None
):
    rendered = render_behavior_proof_dod_evidence_item(
        repo="OmniNode-ai/omnimarket", pr_number=1, test_paths=["tests/test_example.py"]
    )
    item = yaml.safe_load("dod_evidence:\n" + rendered)["dod_evidence"][0]
    assert "Database requirement:" in item["description"]
    assert "provisioned by tests" in item["description"]
    assert "POSTGRES_*" in item["description"]


def test_failed_check_reports_test_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    test = tmp_path / "test_failure.py"
    test.write_text(
        "import sys\n"
        "def test_connect():\n"
        "    print('unrelated stderr noise', file=sys.stderr)\n"
        "    raise ConnectionRefusedError('fixture database unavailable')\n"
    )
    command = f"python3 -m pytest {shlex.quote(str(test))} -q; status=$?; echo unrelated-stderr-noise >&2; exit $status"
    result = EvidenceCollector()._check_evidence_item(_item(command), "OMN-20663")
    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.failure is not None
    assert result.failure.exit_code == 1
    assert any(
        test_id.endswith("test_failure.py::test_connect")
        for test_id in result.failure.failing_test_ids
    ), result.message
    assert (
        result.failure.first_error_line
        == "ConnectionRefusedError: fixture database unavailable"
    )
    state = HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id="OMN-20663"), evidence_results=[result]
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    assert "test_failure.py::test_connect" in (state.error_message or "")
    assert "ConnectionRefusedError" in (state.error_message or "")
    receipt = json.loads(_build_probe_stdout(state))
    assert receipt["details"][0]["failure"]["exit_code"] == 1
    summary = _summarize_payload(state.model_dump(mode="json"))
    assert "test_failure.py::test_connect" in summary["error_message"]

    monkeypatch.delenv("ONEX_EVIDENCE_ROOT", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dod_verify",
            "--ticket-id",
            "OMN-20663",
            "--dry-run",
            "--execution-audience",
            "local_done_gate",
        ],
    )
    monkeypatch.setattr(HandlerDodVerify, "run_verification", lambda *_a: (state, None))
    with pytest.raises(SystemExit) as exited:
        dod_cli.main()
    assert exited.value.code == 1
    printed = capsys.readouterr()
    assert "EVIDENCE_CHECK_FAILED" in printed.err
    assert "test_failure.py::test_connect" in printed.err
    assert "ConnectionRefusedError" in printed.err


def test_failure_diagnostics_do_not_leak_into_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    collector = EvidenceCollector()
    collector._check_evidence_item(_item("bash -c 'exit 1'"), "OMN-20663")
    result = collector._check_evidence_item(_item("bash -c 'exit 2'"), "OMN-20663")
    assert result.failure is not None
    assert result.failure.exit_code == 2
    assert result.failure.failing_test_ids == ()


def test_explicit_test_owned_database_configuration_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient-fixture-value")
    test = tmp_path / "test_explicit.py"
    test.write_text(
        "import os\n"
        "def test_database():\n"
        "    assert os.environ.get('POSTGRES_PASSWORD') == 'test-owned-fixture'\n"
    )
    ok, message = EvidenceCollector()._run_command_check(
        {
            "check_type": "test_passes",
            "check_value": f"POSTGRES_PASSWORD=test-owned-fixture python3 -m pytest {shlex.quote(str(test))} -q",
        },
        "OMN-20663",
    )
    assert ok, message


def test_non_behavior_service_commands_keep_their_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient-fixture-value")
    ok, message = EvidenceCollector()._run_command_check(
        {"check_type": "command", "check_value": "printenv POSTGRES_PASSWORD"},
        "OMN-20663",
    )
    assert ok, message
    assert "ambient-fixture-value" in message
