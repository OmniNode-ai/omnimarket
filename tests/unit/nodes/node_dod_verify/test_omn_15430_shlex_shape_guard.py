# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15430: shlex-aware tokenization for the OMN-15382 shape guard.

``_invalid_check_value_reason`` (the OMN-15382 pre-execution prose guard)
tokenized ``check_value`` with a naive ``cmd_str.strip().split()`` after
stripping a leading ``VAR=`` token. On a check_value shaped like

    body="$(gh api ... | base64 -d)" && printf '%s' "$body" | grep -qF '<marker>'

(the OMN-15411 SIGPIPE-safe rewrite of the OMN-15170 evidence check) the
naive split breaks apart *inside* the quoted command substitution and
inspects an inner word (``api``) as the "first token" — a false
``INVALID_CHECK_VALUE_NOT_A_COMMAND`` on a command that actually runs
correctly (``bash -o pipefail -c '<same string>'`` exits 0).

The fix tokenizes with ``shlex.split(cmd_str, posix=True)`` (quote-respecting),
strips leading ``VAR=VAL`` assignment tokens AND leading shell control
operators (``&&``, ``||``, ``;``, ``|``, ...), then resolves the first real
command token. On a genuine shlex parse failure (unbalanced quotes), the
guard stays fail-closed (INVALID) — a check_value bash itself cannot parse
is genuinely invalid.

Test groups:
  1. RED-first: the exact OMN-15170 shape, proven INVALID under the
     pre-fix naive-split guard via a stash-replay of the pre-fix source,
     then proven to pass (not rejected) under the post-fix guard.
  2. Non-regression: bare prose, first-token-ends-with-colon,
     ``./script.sh`` + declared cwd (the round-1 fix), and a genuinely
     unparseable string (unbalanced quote) all still hard-INVALID.
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
    _invalid_check_value_reason,
)

# The exact OMN-15170 sigpipe-safe shape (OMN-15411, contracts/OMN-15170.yaml
# dod-steel220-omn15170-sigpipe-safe entry) that false-REDs under a naive
# ``str.split()`` shape guard because it splits inside the quoted
# ``$(...)`` command substitution.
OMN_15170_SHAPE = (
    'body="$(gh api repos/jonahgabriel/steel_onslaught/contents/tests/live/'  # onex-allow-test-fixture OMN-15430 reason="literal reproduction of the real OMN-15170/OMN-15411 check_value evidence string, required verbatim for the RED/GREEN shape-guard regression proof"
    "test_omn15170_live_driver.py?ref=24f3f5174ee47d26e0c9abe564c6da58e23497f2 "
    '--jq .content | base64 -d)" && printf \'%s\' "$body" | grep -qF '
    "'def test_live_match_produces_real_kafka_terminal_event_via_delegation_pin'"
)


def _write_contract(
    tmp_path: Path,
    ticket_id: str = "OMN-15430",
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
                "description": "OMN-15430 shlex shape-guard hardening",
                "checks": [{"check_type": "command", "check_value": check_value}],
            }
        ],
    )
    collector = EvidenceCollector()
    results = collector.collect(
        "OMN-15430",
        contract_path=str(tmp_path / "OMN-15430.yaml"),
    )
    return results[0]


@pytest.mark.unit
class TestOmn15170ShapeIsAcceptedPostFix:
    """RED-first proof for this ticket was performed via a manual
    stash-replay (stash the src fix, run this class against the pre-fix
    naive-split guard, confirm INVALID; unpop, confirm GREEN) — see the PR
    description for the transcript. These tests assert the required
    post-fix (GREEN) behavior permanently; they are not themselves the
    RED-first mechanism (a permanent test that execs historical git blobs
    would be fragile against shallow CI checkouts and repo history rewrites)."""

    def test_post_fix_guard_accepts_the_omn_15170_shape(self) -> None:
        """Post-fix: the shlex-aware guard tokenizes the same string
        correctly and does not reject it."""
        reason = _invalid_check_value_reason(OMN_15170_SHAPE, cwd=None)
        assert reason is None, reason

    def test_post_fix_end_to_end_check_is_not_invalid_shape(
        self, tmp_path: Path
    ) -> None:
        """End-to-end through the collector: the OMN-15170 shape must not
        be rejected by the shape guard (INVALID_CHECK_VALUE_NOT_A_COMMAND).
        It may still legitimately FAIL for unrelated reasons in a hermetic
        test environment (no network / no ``gh`` auth) — this test proves
        only that the guard itself no longer misclassifies the shape as
        prose, which is the bug in scope."""
        result = _run_single_check(tmp_path, OMN_15170_SHAPE)
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" not in (result.message or "")


@pytest.mark.unit
class TestNonRegressionStillInvalid:
    def test_bare_prose_still_invalid(self, tmp_path: Path) -> None:
        result = _run_single_check(
            tmp_path, "Recorded product receipt: uv run pytest x"
        )
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")

    def test_first_token_ends_with_colon_still_invalid(self) -> None:
        reason = _invalid_check_value_reason("note: do the thing", cwd=None)
        assert reason is not None
        assert "ends with ':'" in reason

    def test_relative_script_with_declared_cwd_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-regression for the round-1 (OMN-15382) fix: a legitimate
        relative-script + declared-cwd shape must still pass under the new
        shlex tokenization."""
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
            "OMN-15430",
            contract_path=str(tmp_path / "OMN-15430.yaml"),
        )
        result = results[0]
        assert result.status == EnumEvidenceCheckStatus.VERIFIED, result.message
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" not in (result.message or "")

    def test_unbalanced_quote_is_unparseable_and_still_invalid(self) -> None:
        """A genuinely unparseable check_value (shlex.split raises
        ValueError on the unbalanced quote) must stay fail-closed INVALID —
        never silently passed through as a valid command shape."""
        reason = _invalid_check_value_reason("echo 'unbalanced", cwd=None)
        assert reason is not None
        assert "could not be parsed as shell syntax" in reason

    def test_unbalanced_quote_end_to_end_is_invalid(self, tmp_path: Path) -> None:
        result = _run_single_check(tmp_path, "echo 'unbalanced")
        assert result.status == EnumEvidenceCheckStatus.FAILED
        assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in (result.message or "")

    def test_var_assignment_prefix_still_stripped(self) -> None:
        """Non-regression: a leading VAR=VAL assignment is still stripped
        before inspecting the first real token, now via shlex tokens."""
        reason = _invalid_check_value_reason("FOO=bar echo hi", cwd=None)
        assert reason is None, reason

    def test_leading_operator_only_with_no_command_is_invalid(self) -> None:
        """Edge case surfaced by control-operator skipping: a check_value
        that is only assignments/operators with no real command token stays
        fail-closed INVALID."""
        reason = _invalid_check_value_reason("FOO=bar &&", cwd=None)
        assert reason is not None
