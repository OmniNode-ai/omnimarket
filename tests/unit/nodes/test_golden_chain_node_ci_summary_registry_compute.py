# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_ci_summary_registry_compute (OMN-23505): parity, refusals, runtime and a failing control.

Shape follows tests/test_golden_chain_node_review_finding_disposition_compute.py. The
shipped sweep-exclusion file is read by the old loader's own expression and by the
node; the two must agree. Each refusal test fails when the handler's validation is
removed (test_control_*).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_ci_summary_registry_compute as _node_pkg
from omnimarket.nodes.node_ci_summary_registry_compute.handlers import (
    handler_ci_summary_registry as handler_module,
)
from omnimarket.nodes.node_ci_summary_registry_compute.handlers.handler_ci_summary_registry import (
    HandlerCiSummaryRegistry,
)
from omnimarket.nodes.node_ci_summary_registry_compute.models.model_ci_summary_registry import (
    ModelCiSummaryRegistry,
    ModelCiSummaryRegistryRequest,
)
from omnimarket.nodes.node_ci_summary_registry_compute.runtime_ci_summary_registry import (
    main,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SHIPPED_EXCLUSIONS = REPO / "scripts" / "ci" / "ci_summary_sweep_exclusions.json"
CONTRACT = {
    "contract": "ci_summary_external_check_modes",
    "version": 1,
    "modes": {"advisory": ["neutral"], "blocking": []},
    "entry_fields": ["mode", "reason"],
}
GOOD = {
    "mode": "advisory",
    "reason": "the reviewer reports neutral as its normal outcome",
}


def _resolve(**fields: object) -> ModelCiSummaryRegistry:
    return HandlerCiSummaryRegistry().handle(
        ModelCiSummaryRegistryRequest.model_validate(fields)
    )


def test_golden_shipped_exclusions_match_the_old_loader() -> None:
    raw = json.loads(SHIPPED_EXCLUSIONS.read_text(encoding="utf-8"))
    result = _resolve(
        sweep_exclusions=raw, check_modes_contract=CONTRACT, check_modes_overlay={}
    )
    assert result.refusals == []
    assert set(result.exclusions) == set(raw)
    for name, fields in raw.items():
        assert result.exclusions[name].model_dump() == fields


def test_golden_declared_modes_add_their_conclusions() -> None:
    overlay = {
        "Tessl Reviewer": GOOD,
        "Hard Gate": {"mode": "blocking", "reason": "blocks"},
    }
    result = _resolve(
        sweep_exclusions={}, check_modes_contract=CONTRACT, check_modes_overlay=overlay
    )
    assert result.check_modes == {"Tessl Reviewer": ["neutral"], "Hard Gate": []}
    assert result.refusals == []


def test_parity_resolution_is_deterministic() -> None:
    raw = json.loads(SHIPPED_EXCLUSIONS.read_text(encoding="utf-8"))
    one = _resolve(
        sweep_exclusions=raw,
        check_modes_contract=CONTRACT,
        check_modes_overlay={"A": GOOD},
    )
    two = _resolve(
        sweep_exclusions=raw,
        check_modes_contract=CONTRACT,
        check_modes_overlay={"A": GOOD},
    )
    assert one == two


@pytest.mark.parametrize(
    "entry",
    [
        {"mode": "advisory"},
        {"mode": "advisory", "reason": "  "},
        {"mode": "bogus", "reason": "x"},
        "not-a-dict",
    ],
    ids=["missing-reason", "blank-reason", "undeclared-mode", "non-dict"],
)
def test_error_malformed_overlay_entry_refuses_and_admits_nothing(
    entry: object,
) -> None:
    result = _resolve(
        sweep_exclusions={},
        check_modes_contract=CONTRACT,
        check_modes_overlay={"Bad": entry, "Good": GOOD},
    )
    assert "Bad" not in result.check_modes
    assert result.check_modes == {"Good": ["neutral"]}
    assert result.refusals == [
        "check-modes: Bad: needs non-empty text mode, reason and a declared mode"
    ]


def test_error_malformed_contract_refuses() -> None:
    result = _resolve(
        sweep_exclusions={},
        check_modes_contract={"entry_fields": []},
        check_modes_overlay={"A": GOOD},
    )
    assert result.check_modes == {}
    assert result.refusals[0].startswith("check-modes: unreadable or malformed")


def test_error_unreadable_file_refuses_both_registries() -> None:
    result = _resolve(
        sweep_exclusions_error="x.json: missing", check_modes_error="y.json: missing"
    )
    assert result.exclusions == {}
    assert result.check_modes == {}
    assert [r.split(":")[0] for r in result.refusals] == [
        "sweep-exclusions",
        "check-modes",
    ]


def test_error_malformed_exclusion_entry_refuses() -> None:
    result = _resolve(
        sweep_exclusions={"X": {"reason": "untyped"}},
        check_modes_contract=CONTRACT,
        check_modes_overlay={},
    )
    assert result.exclusions == {}
    assert result.refusals[0].startswith("sweep-exclusions: unreadable or malformed")


def test_control_without_validation_the_refusal_test_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing control: with the overlay validation removed, a malformed entry is admitted."""

    def lax(
        spec: object, decls: object, error: str | None
    ) -> tuple[dict[str, list[str]], list[str]]:
        assert isinstance(decls, dict)
        return {n: ["neutral"] for n in decls}, []

    monkeypatch.setattr(handler_module, "_resolve_check_modes", lax)
    with pytest.raises(AssertionError):
        test_error_malformed_overlay_entry_refuses_and_admits_nothing(
            {"mode": "advisory"}
        )


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main([])
    return code, capsys.readouterr().out


def test_runtime_cli_resolves_a_request_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = {
        "sweep_exclusions": {},
        "check_modes_contract": CONTRACT,
        "check_modes_overlay": {"A": GOOD},
    }
    code, out = _run_cli(monkeypatch, capsys, json.dumps(request))
    assert code == 0
    assert json.loads(out) == {
        "exclusions": {},
        "check_modes": {"A": ["neutral"]},
        "refusals": [],
    }


def test_runtime_cli_names_a_caller_reported_unreadable_file_and_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = {
        "sweep_exclusions": {},
        "check_modes_error": "ci_summary_external_check_modes.json: absent",
    }
    code, out = _run_cli(monkeypatch, capsys, json.dumps(request))
    written = json.loads(out)
    assert code == 0
    assert written["check_modes"] == {}
    assert written["refusals"][0].startswith(
        "check-modes: unreadable or malformed: ci_summary"
    )


def test_error_runtime_cli_refuses_a_stdin_that_is_not_a_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_cli(monkeypatch, capsys, "not json")
    assert code == 2
    assert out == ""


def test_contract_declares_a_compute_node_with_an_importable_handler() -> None:
    contract = yaml.safe_load(
        (Path(_node_pkg.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    assert contract["node_type"] == "compute"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.ci-summary-registry-requested.v1"
    )
    assert contract["handler"]["class"] == HandlerCiSummaryRegistry.__name__


def test_contract_publishes_the_resolved_and_failed_terminal_events() -> None:
    contract = yaml.safe_load(
        (Path(_node_pkg.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    terminal = contract["runtime_dispatch"]["terminal_events"]
    assert terminal["success"] == "onex.evt.omnimarket.ci-summary-registry-resolved.v1"
    assert terminal["failure"] == "onex.evt.omnimarket.ci-summary-registry-failed.v1"
    assert sorted(contract["event_bus"]["publish_topics"]) == sorted(
        [
            "onex.evt.omnimarket.ci-summary-registry-resolved.v1",
            "onex.evt.omnimarket.ci-summary-registry-failed.v1",
        ]
    )
