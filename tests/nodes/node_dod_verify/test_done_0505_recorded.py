# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

import os
import shlex
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_state import EnumEvidenceCheckStatus
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
    _repo_contract_subject,
)

pytestmark = pytest.mark.unit
_FIXTURES = Path(__file__).with_name("fixtures")


def test_done_0505_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = yaml.safe_load((_FIXTURES / "done-0505-contract.yaml").read_text())
    refusal, failing_check = (
        (_FIXTURES / "done-0505-refusal.txt").read_text().split("\n", 1)
    )
    assert (
        "status='failed', total_checks=9, verified_count=5, failed_count=3" in refusal
    )
    usage_error = failing_check.split("first_error=", 1)[1].rstrip()
    assert usage_error.endswith("rootdir: /app")
    recorded = [
        item
        for item in contract["dod_evidence"]
        if any(check["check_type"] == "test_passes" for check in item["checks"])
    ]
    assert [check["cwd"] for item in recorded for check in item["checks"]] == ["."] * 3

    app = tmp_path / "app"
    product = app / "omnibase_infra"
    (product / "tests" / "ci").mkdir(parents=True)
    test_path = "tests/ci/test_dev_runtime_scope_clones_omn25709.py"
    (product / test_path).touch()
    calls = tmp_path / "calls"
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$PWD" >> {shlex.quote(str(calls))}\n'
        f"if test -f {test_path}; then\n"
        f'  case " $* " in *" {test_path} "*) ;; *) exit 2 ;; esac\n'
        "  printf '1 passed in 0.01s\\n'\n"
        "else\n"
        f"  printf '%s\\n' {shlex.quote(usage_error)} >&2\n"
        "  exit 4\n"
        "fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OMNI_HOME", str(app))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.chdir(app)

    results = EvidenceCollector()._collect_impl(
        contract["ticket_id"],
        None,
        EnumDodVerifyExecutionAudience.HOSTED,
        loaded_contract=(
            {**contract, "dod_evidence": recorded},
            _repo_contract_subject(
                "OmniNode-ai/omnibase_infra",
                "f2789cb4e0e8bc8a95dfea3295337f778c24307e",
                contract["ticket_id"],
            ),
        ),
    )
    assert [result.status for result in results] == [
        EnumEvidenceCheckStatus.VERIFIED
    ] * 3, [result.message for result in results]
    assert calls.read_text().splitlines() == [str(product)] * 3
