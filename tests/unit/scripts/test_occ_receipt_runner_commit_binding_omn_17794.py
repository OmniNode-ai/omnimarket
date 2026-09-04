# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17794 — the receipt must say WHICH repository its ``commit_sha`` lives in.

The defect
----------
``scripts/ci/occ_receipt_runner.py`` runs in a PRODUCT repo's CI and stamps that
product repo's head into ``commit_sha``. It then writes the receipt into
``onex_change_control``. A bare SHA carries no repo attribution, so OCC's Receipt
Hardening Gate resolves it against its own
``_DEFAULT_COMMIT_SHA_REPO = "OmniNode-ai/onex_change_control"``, does not find a
product commit there, and reports a real, pushed commit as fabricated.

Measured live on **OCC#8145** — the autobound companion for this very change
(``omnimarket#2286``) — in ``CI / Pre-commit`` job ``100860443848``::

    Receipt hardening gate: 1 violation(s):

      drift/dod_receipts/OMN-17794/dod-occ-diff-derived-behavior-proof/
      test_passes.supersede.2286.yaml: replacement [COMMIT_SHA_EXISTS]
      commit_sha '138956373a614f5e053922e25a59978b4ed42e16' does not resolve to
      a real, remote-reachable commit ... If this receipt documents a check
      against a different repo, embed a 'repos/<owner>/<repo>/...' reference in
      check_value/probe_command so the gate can resolve it there.

``138956373a`` is the real head of ``omnimarket``'s
``jonah/omn-17794-occ-receipt-yamlfmt-stable``. The gate is not wrong; the
producer simply never told it where to look.

Why the ``commits/<sha>`` form specifically
-------------------------------------------
The gate's ``_REPO_HINT_RE`` accepts two citation forms — the
``repos/<owner>/<repo>/`` URL-path form and the ``--repo <owner>/<repo>``
CLI-flag form — and only for repositories in its ``_KNOWN_REPO_HINTS``
allowlist (an unrecognised ``<owner>/<repo>`` is noise or an attack, never an
authority). When a receipt cites more than one trusted repository the gate
disambiguates in a fixed two-tier order, and **tier 1 is the repository whose
own command segment binds the receipt's ``commit_sha`` via
``repos/<owner>/<repo>/commits/<sha>``**.

Emitting that exact form is therefore strictly stronger than emitting a bare
``repos/<owner>/<repo>/`` hint: a contract whose declared check text already
names some other trusted repo resolves to the product repo anyway, instead of
refusing as ambiguous.

What must not break
-------------------
* ``probe_command == check_value``. The OMN-15459 S2 family-binding rule relies
  on it, and re-deriving one from the other is the OCC#5534 laundering channel.
  The prefix is applied identically to both, so the equality is preserved.
* The contract's declared bar is carried through **verbatim**. The receipt
  attests to the declared check; it is not rewritten, only located.
* S1 — no byte-identical ``replacement.check_value`` across different items in
  one cohort. The prefix makes values more distinct, never less.
* OMN-15710 — no machine-specific absolute path in ``check_value`` /
  ``probe_command``. A ``repos/<owner>/<repo>/commits/<sha>`` reference is a
  GitHub API path, not a filesystem path.
* OMN-17794's own first half — ``check_value`` and ``probe_command`` are now
  **multi-line**, so they are exactly the shape yamlfmt falsifies. They must be
  emitted as block scalars and survive the real formatter unchanged.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_receipt_runner as runner  # noqa: E402

# Byte-mirror of onex_change_control/.yamlfmt, as in the sibling yamlfmt module.
_OCC_YAMLFMT_CONF = """\
formatter:
  retain_line_breaks: true
  max_line_length: 100
  indent: 2
  include_document_start: true
  pad_line_comments: 2
"""

# Verbatim mirror of `_REPO_HINT_RE` in onex_change_control
# scripts/validation/check_receipt_hardening.py. Mirrored rather than imported:
# this repo has no OCC checkout, and a test that silently skips when the
# consumer is absent proves nothing.
_REPO_HINT_RE = re.compile(
    r"repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/|"
    r"--repo[= ]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b"
)

_PRODUCT_REPO = "OmniNode-ai/omnimarket"
_HEAD_SHA = "138956373a614f5e053922e25a59978b4ed42e16"
_PR_NUMBER = 2286

# The declared bar, verbatim from contracts/OMN-17794.yaml as OCC#8145 carries it.
_DECLARED_CHECK = (
    "uv run pytest tests/unit/scripts/test_occ_receipt_runner_yamlfmt_omn_17794.py -q"
)

_PROBE_STDOUT = (
    "................................................ssssssssssssssssssssssss [ 97%]\n"
    "s.                                                                       [100%]\n"
    "49 passed, 25 skipped in 0.92s"
)

_CONTRACT_DATA: dict[str, object] = {
    "ticket_id": "OMN-17794",
    "title": "OCC receipt producer emits yamlfmt-stable, repo-bound YAML",
    "dod_evidence": [
        {
            "id": "dod-occ-diff-derived-behavior-proof",
            "description": "the runner's own suite passes in the product checkout",
            "source": "generated",
            "checks": [{"check_type": "test_passes", "check_value": _DECLARED_CHECK}],
        }
    ],
}


def _executed(check_value: str = _DECLARED_CHECK) -> runner.ExecutedCheck:
    return runner.ExecutedCheck(
        ticket_id="OMN-17794",
        evidence_item_id="dod-occ-diff-derived-behavior-proof",
        check_type="test_passes",
        check_value=check_value,
        stdout=_PROBE_STDOUT,
        exit_code=0,
        duration_ms=11969,
    )


def _receipt(check_value: str = _DECLARED_CHECK) -> dict[str, object]:
    return runner.build_receipt(
        _executed(check_value),
        contract_data=_CONTRACT_DATA,
        pr_number=_PR_NUMBER,
        repo=_PRODUCT_REPO,
        head_sha=_HEAD_SHA,
        branch="jonah/omn-17794-occ-receipt-yamlfmt-stable",
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/33819148909",
    )


def _repo_hints(text: str) -> set[str]:
    """Every repository the gate's own regex would extract from ``text``."""
    return {a or b for a, b in _REPO_HINT_RE.findall(text)}


class TestTheReceiptNamesItsCommitsRepository:
    """The gate can resolve ``commit_sha`` — the OCC#8145 violation, closed."""

    @pytest.mark.parametrize("field", ["check_value", "probe_command"])
    def test_the_contract_bound_field_cites_the_product_repo(self, field: str) -> None:
        assert _repo_hints(str(_receipt()[field])) == {_PRODUCT_REPO}

    @pytest.mark.parametrize("field", ["check_value", "probe_command"])
    def test_the_citation_binds_this_receipts_own_commit_sha(self, field: str) -> None:
        """Tier 1: ``repos/<owner>/<repo>/commits/<sha>`` naming our own SHA.

        Not merely *a* SHA — the receipt's own, so the gate resolves the commit
        it is actually asserting exists.
        """
        receipt = _receipt()
        assert f"repos/{_PRODUCT_REPO}/commits/{receipt['commit_sha']}" in str(
            receipt[field]
        )

    def test_the_bound_sha_is_the_receipts_commit_sha_not_a_constant(self) -> None:
        other = "0123456789abcdef0123456789abcdef01234567"
        receipt = runner.build_receipt(
            _executed(),
            contract_data=_CONTRACT_DATA,
            pr_number=_PR_NUMBER,
            repo=_PRODUCT_REPO,
            head_sha=other,
            branch="b",
            run_url="https://example.invalid/run",
        )
        assert receipt["commit_sha"] == other
        assert f"commits/{other}" in str(receipt["check_value"])
        assert _HEAD_SHA not in str(receipt["check_value"])

    def test_the_bound_repo_is_the_product_repo_not_a_constant(self) -> None:
        receipt = runner.build_receipt(
            _executed(),
            contract_data=_CONTRACT_DATA,
            pr_number=_PR_NUMBER,
            repo="OmniNode-ai/omnibase_infra",
            head_sha=_HEAD_SHA,
            branch="b",
            run_url="https://example.invalid/run",
        )
        assert _repo_hints(str(receipt["check_value"])) == {
            "OmniNode-ai/omnibase_infra"
        }


class TestWhatMustNotBreak:
    """Every invariant the prefix could plausibly have cost."""

    def test_probe_command_still_equals_check_value(self) -> None:
        """OMN-15459 S2 / OCC#5534 — the equality is load-bearing."""
        receipt = _receipt()
        assert receipt["probe_command"] == receipt["check_value"]

    @pytest.mark.parametrize("field", ["check_value", "probe_command"])
    def test_the_declared_bar_is_carried_through_verbatim(self, field: str) -> None:
        """The receipt attests to the contract's check; it does not rewrite it."""
        assert str(_receipt()[field]).endswith(f"\n{_DECLARED_CHECK}")

    def test_distinct_declared_checks_stay_distinct(self) -> None:
        """S1 — no byte-identical ``check_value`` across items in one cohort."""
        first = _receipt("uv run pytest tests/unit/a.py -q")
        second = _receipt("uv run pytest tests/unit/b.py -q")
        assert first["check_value"] != second["check_value"]

    @pytest.mark.parametrize("field", ["check_value", "probe_command"])
    def test_no_machine_specific_absolute_path_is_introduced(self, field: str) -> None:
        """OMN-15710 — a GitHub API path is not a filesystem path."""
        value = str(_receipt()[field])
        # test-literal-ok: absence is the assertion (same rationale as the
        # _ABS_PATH_PREFIXES literals in test_occ_receipt_runner_omn_16859.py).
        assert "/Users/" not in value  # test-literal-ok: absence is the assertion
        assert "/Volumes/" not in value  # test-literal-ok: absence is the assertion
        assert not value.startswith("/")


class TestTheNowMultilineCommandSurvivesYamlfmt:
    """The prefix makes these fields multi-line — the shape yamlfmt falsifies.

    This is the join between OMN-17794's two halves: closing the commit-binding
    gap would have reopened the formatter gap if the fields were not emitted as
    block scalars.
    """

    @pytest.mark.parametrize("field", ["check_value", "probe_command"])
    def test_the_field_is_emitted_as_a_block_scalar(self, field: str) -> None:
        text = runner.render_receipt_yaml(_receipt())
        node = yaml.compose(text)
        assert isinstance(node, yaml.nodes.MappingNode)
        styles = {
            str(key.value): value.style
            for key, value in node.value
            if isinstance(value, yaml.nodes.ScalarNode)
        }
        assert styles[field] == "|", (
            f"{field} is multi-line and must be a literal block scalar; "
            f"got style {styles[field]!r}"
        )

    @pytest.mark.skipif(
        shutil.which("yamlfmt") is None, reason="yamlfmt binary not installed"
    )
    def test_the_real_formatter_changes_nothing(self, tmp_path: Path) -> None:
        conf = tmp_path / ".yamlfmt"
        conf.write_text(_OCC_YAMLFMT_CONF, encoding="utf-8")
        target = tmp_path / "test_passes.supersede.2286.yaml"
        receipt = _receipt()
        target.write_text(runner.render_receipt_yaml(receipt), encoding="utf-8")

        before = target.read_bytes()
        result = subprocess.run(
            ["yamlfmt", "-conf", str(conf), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        after = target.read_bytes()

        assert after == before, "yamlfmt rewrote a receipt it should have left alone"
        reloaded = yaml.safe_load(after.decode("utf-8"))
        assert reloaded == receipt
        assert "magic___" not in after.decode("utf-8")
