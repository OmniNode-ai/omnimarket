# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The arch graph ticket contracts bind only what a hosted test proves.

OMN-22403 refuses a ``binds_ac`` on a ``local_done_gate`` item, so each criterion
left in ``acceptance`` is bound to a hosted ``uv run pytest`` on an existing test
file, and each criterion the hosted runner cannot prove is stated in ``summary``
with where it is carried. The local checks stay as unbound local evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "contracts"


def _load(ticket: str) -> dict[str, object]:
    return yaml.safe_load(  # type: ignore[no-any-return]
        (_CONTRACTS / f"{ticket}.yaml").read_text()
    )


def _items(ticket: str) -> list[dict[str, object]]:
    return list(_load(ticket)["dod_evidence"])  # type: ignore[call-overload]


def _listed(ticket: str) -> list[str]:
    return [
        ac["id"]
        for req in _load(ticket)["requirements"]  # type: ignore[attr-defined]
        for ac in req["acceptance"]
    ]


def _checks(item: dict[str, object]) -> list[str]:
    return [str(chk["check_value"]) for chk in item["checks"]]  # type: ignore[attr-defined]


def _local(ticket: str) -> list[dict[str, object]]:
    return [
        item
        for item in _items(ticket)
        if item.get("execution_scope") == "local_done_gate"
    ]


def _hosted_test_file(value: str) -> Path | None:
    """The existing test file a ``uv run pytest`` check names, or None."""
    if not value.startswith("uv run pytest"):
        return None
    for token in value.split():
        if token.startswith("tests/") and token.endswith(".py"):
            path = _ROOT / token
            return path if path.is_file() else None
    return None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ticket", "expected"),
    [("OMN-24687", ["AC2"]), ("OMN-24715", ["AC3", "AC4"])],
)
def test_arch_graph_bolt_contract_lists_only_hosted_provable_criteria(
    ticket: str, expected: list[str]
) -> None:
    assert _listed(ticket) == expected


@pytest.mark.unit
@pytest.mark.parametrize("ticket", ["OMN-24687", "OMN-24715"])
def test_arch_graph_bolt_no_local_done_gate_item_binds_a_criterion(
    ticket: str,
) -> None:
    local = _local(ticket)
    assert local, "the local evidence items are kept, not deleted"
    for item in local:
        assert "binds_ac" not in item, item["id"]


@pytest.mark.unit
@pytest.mark.parametrize("ticket", ["OMN-24687", "OMN-24715"])
def test_arch_graph_bolt_each_listed_criterion_has_a_hosted_pytest_binding(
    ticket: str,
) -> None:
    for ac_id in _listed(ticket):
        bound = [item for item in _items(ticket) if ac_id in item.get("binds_ac", [])]  # type: ignore[operator]
        assert bound, ac_id
        for item in bound:
            assert item.get("execution_scope") != "local_done_gate"
            values = _checks(item)
            assert values
            for value in values:
                assert _hosted_test_file(value) is not None, value
                assert "OmniNode-ai/overlays" not in value


@pytest.mark.unit
def test_arch_graph_bolt_host_overlay_test_binds_ac2_only() -> None:
    bound = [
        item
        for item in _items("OMN-24687")
        if any("test_arch_graph_bolt_host_overlay" in v for v in _checks(item))
        and "binds_ac" in item
    ]
    assert len(bound) == 1
    assert bound[0]["binds_ac"] == ["AC2"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ticket", "dropped"),
    [
        ("OMN-24687", ["AC1", "AC3", "AC4", "AC5"]),
        ("OMN-24715", ["AC1", "AC2"]),
    ],
)
def test_arch_graph_bolt_summary_states_each_criterion_no_longer_listed(
    ticket: str, dropped: list[str]
) -> None:
    summary = str(_load(ticket)["summary"])
    for ac_id in dropped:
        assert f"{ac_id} (" in summary, ac_id
    assert "carried" in summary


@pytest.mark.unit
def test_arch_graph_bolt_summary_names_where_ticket_24687_criteria_are_carried() -> (
    None
):
    summary = str(_load("OMN-24687")["summary"])
    # AC1, AC4 and AC5: the ticket itself; AC3: the overlays test
    assert "carried by the ticket OMN-24687" in summary
    assert "tests/test_arch_graph_bolt_overlay.py of overlays#577" in summary


@pytest.mark.unit
def test_arch_graph_bolt_summary_names_where_ticket_24715_criteria_are_carried() -> (
    None
):
    summary = str(_load("OMN-24715")["summary"])
    assert "overlays ci.yml node pin check" in summary
    assert "tests/test_arch_graph_bolt_overlay.py in the overlays repository" in summary


@pytest.mark.unit
def test_arch_graph_bolt_unmet_criteria_check_is_unbound_and_reads_a_tcp_connect() -> (
    None
):
    unmet = [item for item in _local("OMN-24687") if "ac1-ac4-ac5" in str(item["id"])]
    assert len(unmet) == 1
    # reads red until a Bolt service answers: a TCP connect, never a printed string
    assert any("create_connection" in value for value in _checks(unmet[0]))


@pytest.mark.unit
@pytest.mark.parametrize("host", ["h051", "h053"])
def test_arch_graph_bolt_integration_proof_is_pinned_to_each_lab_host(
    host: str,
) -> None:
    checks = [
        value
        for item in _items("OMN-24687")
        if "integration" in str(item["id"])
        for value in _checks(item)
    ]
    pinned = [value for value in checks if f"--host {host} " in value]
    assert len(pinned) == 1
    assert "grep -q ^bolt" in pinned[0]
    assert "ARCH_GRAPH_BOLT_URI" in pinned[0]


@pytest.mark.unit
def test_arch_graph_bolt_no_integration_check_is_unpinned() -> None:
    for item in _items("OMN-24687"):
        if "integration" not in str(item["id"]):
            continue
        for value in _checks(item):
            assert "--host h0" in value


@pytest.mark.unit
@pytest.mark.parametrize("ticket", ["OMN-24687", "OMN-24715"])
def test_arch_graph_bolt_overlays_sha_is_read_from_main_of_the_declared_clone(
    ticket: str,
) -> None:
    values = [
        value
        for item in _local(ticket)
        for value in _checks(item)
        if "OmniNode-ai/overlays" in value
    ]
    assert values
    for value in values:
        # the declared clone sits beside the registry root, and the branch ref goes on merge
        assert 'git -C "$OMNI_HOME/../overlays" rev-parse origin/main' in value
        assert "omn-24687-arch-graph-bolt-host-overlay" not in value
