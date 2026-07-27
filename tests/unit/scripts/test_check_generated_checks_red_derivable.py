# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the OMN-15247 static RED-derivability grammar gate.

The gate is layer 2 of OMN-15247's acceptance bar ("for every generated check,
the same check run against the PR's merge-base must return non-zero"). It is
structural: it proves a check's SHAPE is capable of RED, never that it went RED
— that is layer 1 (mint-time execution, in the emitter).

The gate is driven over the REAL producer's rendered output, not a hand-written
YAML string, so a producer change that emits an un-vetted shape fails here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_companion_contract,
)
from omnimarket.occ_content_probe import build_content_read_check

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "ci"
    / "check_generated_checks_red_derivable.py"
)


def _load_gate():
    spec = importlib.util.spec_from_file_location("red_derivable_gate", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["red_derivable_gate"] = module
    spec.loader.exec_module(module)
    return module


_GATE = _load_gate()

_CONTENT_BOUND = build_content_read_check(
    repo="OmniNode-ai/omnimarket",
    path="src/omnimarket/handlers/handler_probe.py",
    kind="class",
    symbol="HandlerContentBoundProbe",
    head_sha="b" * 40,
)


@pytest.mark.unit
class TestClassifyCheck:
    def test_a_well_formed_content_bound_check_is_accepted(self) -> None:
        classification, reason = _GATE.classify_check(_CONTENT_BOUND)
        assert classification == "content_bound"
        assert reason is None

    @pytest.mark.parametrize(
        "value",
        [
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json files",
            "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only | grep -qiE 'nodes/'",
            "grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/"
            "OMN-9999/dod-x/command.yaml",
        ],
    )
    def test_the_shipped_default_forms_are_allowlisted(self, value: str) -> None:
        classification, reason = _GATE.classify_check(value)
        assert classification == "allowlisted"
        assert reason is None

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            (f"{_CONTENT_BOUND} || true", "swallows its exit code"),
            (f"{_CONTENT_BOUND} 2>/dev/null", "swallows its exit code"),
            # Looks like a content read, but no terminal grep => not falsifiable.
            (
                "gh api repos/o/r/contents/x.py?ref=" + "b" * 40 + " --jq '.content'",
                "does not match the RED-derivable grammar",
            ),
            # Empty needle: grep -c '' matches every line, can never go RED.
            (
                "gh api repos/o/r/contents/x.py?ref="
                + "b" * 40
                + " --jq '.content' | base64 -d | grep -c ''",
                "does not match the RED-derivable grammar",
            ),
            # A placeholder ref: the compliance runner has no ${SHA} token, so
            # this would run literally and could never resolve.
            (
                "gh api repos/o/r/contents/x.py?ref=${SHA} --jq '.content' "
                "| base64 -d | grep -c 'class X'",
                "does not match the RED-derivable grammar",
            ),
            ("", "empty check_value"),
            ("true", "matches no known generated-check form"),
        ],
    )
    def test_non_falsifiable_or_unvetted_shapes_are_rejected(
        self, value: str, fragment: str
    ) -> None:
        _classification, reason = _GATE.classify_check(value)
        assert reason is not None
        assert fragment in reason


@pytest.mark.unit
class TestCheckContract:
    def test_the_real_producers_default_contract_passes(self, tmp_path: Path) -> None:
        """Driven over the REAL rendering seam, not a hand-written fixture."""
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
            )
        )
        assert _GATE.check_contract(contract) == []

    def test_the_real_producers_content_bound_contract_passes(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
                downstream_check_value=_CONTENT_BOUND,
            )
        )
        assert _GATE.check_contract(contract) == []

    def test_a_smuggled_inert_check_is_reported_with_its_item_id(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            "---\n"
            'schema_version: "1.0.0"\n'
            "dod_evidence:\n"
            '  - id: "dod-smuggled"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            '        check_value: "gh pr view 1 --repo o/r --json number || true"\n'
        )
        violations = _GATE.check_contract(contract)
        assert len(violations) == 1
        assert violations[0]["item"] == "dod-smuggled"
        assert "swallows its exit code" in violations[0]["reason"]

    def test_main_exits_nonzero_on_a_violation(self, tmp_path: Path) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            "---\n"
            "dod_evidence:\n"
            '  - id: "x"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            '        check_value: "true"\n'
        )
        assert _GATE.main([str(contract)]) == 1

    def test_main_exits_zero_on_a_clean_contract(self, tmp_path: Path) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
            )
        )
        assert _GATE.main([str(contract)]) == 0
