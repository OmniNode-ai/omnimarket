# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15382: fail-closed hardening for ``_run_command_check``.

Two independent vacuous-verdict mechanisms shared one root cause — running
``check_value`` verbatim via ``subprocess.run(cmd, shell=True)`` (POSIX
``sh -c``, no ``pipefail``) and judging success on exit code alone:

* ``"Recorded product receipt: docker compose ... | sha256sum"`` exits 0
  under plain ``sh -c`` — the first pipeline stage ("Recorded" — not a real
  command) fails with 127, but a non-``pipefail`` shell's pipeline exit code
  is the LAST stage's, and ``sha256sum`` happily hashes empty stdin and exits
  0. Vacuous GREEN on evidence that never actually ran the intended command.
* The same prose with no pipe (``"Recorded product receipt: uv run pytest
  x"``) exits 127 under plain ``sh -c`` — RED, but for the wrong reason:
  command-not-found is indistinguishable from a real check failure.

These tests prove BOTH are now closed WITHOUT a blanket "empty stdout is RED"
rule (which would break legitimate quiet checks like ``grep -q``):

1. list-form execution via ``["bash", "-o", "pipefail", "-c", cmd]`` instead
   of ``shell=True`` — a failing first pipeline stage now fails the whole
   check for genuinely command-shaped pipelines;
2. a pre-execution shape guard rejects prose before it is ever shelled out,
   with a distinct reason (``INVALID_CHECK_VALUE_NOT_A_COMMAND``);
3. a real quiet command (``grep -q``) still passes — proving no blanket
   empty-stdout-is-RED rule was added.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-15382",
    dod_evidence: list[dict] | None = None,
) -> Path:
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": dod_evidence or [],
    }
    p = tmp_path / f"{ticket_id}.yaml"
    p.write_text(yaml.dump(contract), encoding="utf-8")
    return p


def _run_single_check(tmp_path: Path, check_value: str) -> object:
    _write_contract(
        tmp_path,
        dod_evidence=[
            {
                "id": "dod-001",
                "description": "OMN-15382 command-check hardening",
                "checks": [{"check_type": "command", "check_value": check_value}],
            }
        ],
    )
    collector = EvidenceCollector()
    results = collector.collect(
        "OMN-15382",
        contract_path=str(tmp_path / "OMN-15382.yaml"),
    )
    return results[0]


@pytest.mark.unit
class TestPipefailClosesVacuousGreenPipe:
    def test_failing_first_pipeline_stage_now_fails_the_check(
        self, tmp_path: Path
    ) -> None:
        """RED-first proof: under the pre-fix ``sh -c`` (no pipefail), a
        pipeline's exit code is the LAST stage's. ``false`` is a genuinely
        resolvable command (passes the shape guard) that always fails, but
        ``sha256sum`` downstream of it always succeeds (hashing empty
        stdin) — so under the OLD implementation this check was vacuously
        GREEN even though the real first stage failed. Confirmed against the
        pre-fix code (git stash of this lane's diff): the identical
        ``check_value`` returned VERIFIED before this fix. Must be FAILED now.
        """
        result = _run_single_check(tmp_path, "false | sha256sum")
        assert result.status == EnumEvidenceCheckStatus.FAILED

    def test_passing_pipeline_still_passes(self, tmp_path: Path) -> None:
        """Non-regression: a genuinely all-green pipeline still passes under
        pipefail — this is not a blanket "any pipe fails" rule."""
        result = _run_single_check(tmp_path, "true | cat")
        assert result.status == EnumEvidenceCheckStatus.VERIFIED


@pytest.mark.unit
class TestShapeGuardRejectsProse:
    def test_prose_with_pipe_is_red_not_vacuously_green(self, tmp_path: Path) -> None:
        """The literal OMN-15382 root-cause example: prose piped into a real
        command. Was GREEN pre-fix (sha256sum hashes empty stdin after the
        prose "command" fails silently in the pipe); must be RED now — the
        shape guard rejects it before it is ever shelled out at all, so the
        pipefail mechanism is never even reached for this string."""
        result = _run_single_check(
            tmp_path, "Recorded product receipt: anything | sha256sum"
        )
        assert result.status == EnumEvidenceCheckStatus.FAILED

    def test_prose_without_pipe_gets_distinct_invalid_reason(
        self, tmp_path: Path
    ) -> None:
        """Was RED pre-fix too, but for the wrong reason (bash: Recorded:
        command not found — indistinguishable from a real check failure).
        Must now be RED with the distinct INVALID_CHECK_VALUE_NOT_A_COMMAND
        reason, proving the collector recognized this as prose rather than
        treating a command-not-found exit code as a legitimate check result.
        """
        result = _run_single_check(
            tmp_path, "Recorded product receipt: uv run pytest x"
        )
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")

    def test_var_assignment_prefix_does_not_defeat_the_guard(
        self, tmp_path: Path
    ) -> None:
        """Leading VAR=VAL tokens are stripped before inspecting the first
        real token — prose after an assignment is still caught."""
        result = _run_single_check(tmp_path, "FOO=bar Recorded product receipt: x")
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")


@pytest.mark.unit
class TestShapeGuardIsCwdAware:
    def test_relative_script_with_declared_cwd_is_not_false_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verifier-flagged regression: the shape guard used to run BEFORE
        ``cwd:`` was resolved and judged the first token via a bare
        ``shutil.which()`` — which only ever inspects this process's actual
        cwd/PATH, never a check's declared ``cwd:``. A legitimate, pre-existing
        supported shape (OMN-10078 relative-script-plus-cwd) — e.g.
        ``check_value: "./verify.sh"`` with ``cwd: "${OMNI_HOME}/sub"`` where
        ``verify.sh`` genuinely exists and is executable at that cwd — was
        false-RED with INVALID_CHECK_VALUE_NOT_A_COMMAND even though the
        script is real. The guard must resolve relative path-like first
        tokens against the check's OWN declared cwd, not the process cwd.
        """
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        sub = tmp_path / "sub"
        sub.mkdir()
        script = sub / "verify.sh"
        script.write_text("#!/bin/sh\necho verified\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "relative script + cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "./verify.sh",
                            "cwd": "${OMNI_HOME}/sub",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-15382",
            contract_path=str(tmp_path / "OMN-15382.yaml"),
        )
        result = results[0]
        assert result.status == EnumEvidenceCheckStatus.VERIFIED, result.message
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" not in (result.message or "")

    def test_relative_script_that_does_not_exist_at_cwd_is_still_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-regression: a relative first token that genuinely does not
        resolve at the declared cwd is still caught by the shape guard."""
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))
        sub = tmp_path / "sub"
        sub.mkdir()

        _write_contract(
            tmp_path,
            dod_evidence=[
                {
                    "id": "dod-001",
                    "description": "missing relative script + cwd",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": "./does-not-exist.sh",
                            "cwd": "${OMNI_HOME}/sub",
                        }
                    ],
                }
            ],
        )
        collector = EvidenceCollector()
        results = collector.collect(
            "OMN-15382",
            contract_path=str(tmp_path / "OMN-15382.yaml"),
        )
        result = results[0]
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")


@pytest.mark.unit
class TestQuietCommandsStillPassNoBlanketEmptyStdoutRule:
    def test_grep_q_on_fixture_file_still_passes(self, tmp_path: Path) -> None:
        """A real quiet command (``grep -q``, no stdout at all on match)
        must still VERIFY — proves no blanket "empty stdout is RED" rule was
        added; pipefail + the shape guard close both reported mechanisms
        without that overbroad rule (see module docstring)."""
        fixture = tmp_path / "fixture.txt"
        fixture.write_text("needle in a haystack\n", encoding="utf-8")
        result = _run_single_check(tmp_path, f"grep -q needle {fixture}")
        assert result.status == EnumEvidenceCheckStatus.VERIFIED
        # Genuinely quiet — grep -q produces no stdout on a match.
        assert result.message is not None
