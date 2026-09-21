# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key-grain gate, proven on synthetic trees and on the real one.

Every refusal here is driven by a tree built in ``tmp_path`` carrying the exact
shape under test, and every one is paired with a control that must stay green.
A gate proven only on its violating case cannot be told apart from a gate that
refuses everything, which is the failure mode this ticket is closing rather
than reproducing.

Related Tickets:
    - OMN-18908: this gate (epic OMN-18906 AC-2)
    - OMN-18013: the ancestor gate whose floor and fence semantics it copies
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.validators import key_grain_declared as gate

pytestmark = pytest.mark.unit


def _contract(
    nodes_root: Path,
    node: str,
    topic: str,
    *,
    key_grain: str | None,
    writer_source_offset: str | None = None,
    test_body: str | None = None,
) -> None:
    """Materialise one synthetic projection node.

    Args:
        key_grain: the declared grain, or None to omit the field entirely.
        writer_source_offset: the literal text passed as ``source_offset`` by
            the node's writer, or None to give the node no writer at all.
        test_body: an in-package test module's source, or None for no tests.
    """
    node_dir = nodes_root / node
    node_dir.mkdir(parents=True, exist_ok=True)
    grain_line = f'      key_grain: "{key_grain}"\n' if key_grain is not None else ""
    (node_dir / "contract.yaml").write_text(
        "name: " + node + "\n"
        "projection_api:\n"
        "  expose: true\n"
        "  exposures:\n"
        f"    - topic: {topic}\n"
        f"{grain_line}"
        "      table: some_rows\n"
    )
    if writer_source_offset is not None:
        (node_dir / "handler.py").write_text(
            "def publish(row):\n"
            "    return encode_snapshot_delta(\n"
            "        row=row,\n"
            f"        source_offset={writer_source_offset},\n"
            "    )\n"
        )
    if test_body is not None:
        tests_dir = node_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_node.py").write_text(test_body)


def _run(tmp_path: Path, minimum: int = 0) -> int:
    nodes = tmp_path / "nodes"
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    return gate.main([str(nodes), str(tests), str(minimum)])


# --------------------------------------------------------------------------
# AC1 -- an undeclared grain is refused rather than defaulted
# --------------------------------------------------------------------------


def test_ac1_an_exposure_with_no_key_grain_is_refused(tmp_path: Path) -> None:
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain=None)
    assert _run(tmp_path) == 1


def test_ac1_the_refusal_names_the_exposure(tmp_path: Path) -> None:
    """A refusal an operator cannot act on is a refusal they will suppress."""
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain=None)
    findings = gate.evaluate(
        gate.collect_exposures(tmp_path / "nodes"),
        nodes_root=tmp_path / "nodes",
        tests_root=tmp_path / "tests",
    )
    assert [(f.node, f.topic, f.code) for f in findings] == [
        ("node_projection_x", "t.x.v1", "undeclared_key_grain")
    ]


def test_ac1_negative_control_a_declared_mutable_exposure_passes(
    tmp_path: Path,
) -> None:
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain="mutable")
    assert _run(tmp_path) == 0


def test_ac1_positive_control_the_real_tree_is_fully_declared() -> None:
    """Every exposure on this branch declares a grain. The whole point."""
    exposures = gate.collect_exposures(gate.DEFAULT_NODES_ROOT)
    undeclared = [e for e in exposures if e.key_grain is None]
    assert undeclared == [], f"undeclared exposures: {undeclared}"


# --------------------------------------------------------------------------
# AC2 -- a mutable-grain writer cannot publish a constant source coordinate
# --------------------------------------------------------------------------


def test_ac2_a_mutable_writer_passing_a_literal_zero_is_refused(
    tmp_path: Path,
) -> None:
    _contract(
        tmp_path / "nodes",
        "node_projection_x",
        "t.x.v1",
        key_grain="mutable",
        writer_source_offset="0",
    )
    assert _run(tmp_path) == 1


def test_ac2_the_refusal_names_the_exposure_and_the_field(tmp_path: Path) -> None:
    _contract(
        tmp_path / "nodes",
        "node_projection_x",
        "t.x.v1",
        key_grain="mutable",
        writer_source_offset="0",
    )
    findings = gate.evaluate(
        gate.collect_exposures(tmp_path / "nodes"),
        nodes_root=tmp_path / "nodes",
        tests_root=tmp_path / "tests",
    )
    assert len(findings) == 1
    assert findings[0].code == "mutable_grain_constant_coordinate"
    assert "source_offset" in findings[0].detail
    assert findings[0].topic == "t.x.v1"


def test_ac2_negative_control_a_derived_coordinate_passes(tmp_path: Path) -> None:
    """The control that keeps this from flagging every writer it can see."""
    _contract(
        tmp_path / "nodes",
        "node_projection_x",
        "t.x.v1",
        key_grain="mutable",
        writer_source_offset="meta.offset",
    )
    assert _run(tmp_path) == 0


def test_ac2_a_constant_partition_beside_a_moving_offset_is_not_a_defect(
    tmp_path: Path,
) -> None:
    """The first draft of this gate refused this shape, and was wrong.

    The cache orders on the offset; the partition selects the series. A live
    writer publishes partition 0 with a strictly increasing database write
    token and is correct, so refusing it would be the gate manufacturing a
    defect out of a working design.
    """
    node_dir = tmp_path / "nodes" / "node_projection_x"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        "projection_api:\n"
        "  expose: true\n"
        "  exposures:\n"
        "    - topic: t.x.v1\n"
        '      key_grain: "mutable"\n'
    )
    (node_dir / "handler.py").write_text(
        "def publish(row):\n"
        "    return encode_snapshot_delta(\n"
        "        source_partition=0,\n"
        "        source_offset=_write_ordering_token(row),\n"
        "    )\n"
    )
    assert _run(tmp_path) == 0


def test_ac2_a_multiline_call_is_still_found(tmp_path: Path) -> None:
    """Syntax tree, not regex.

    Nearly every publish call in this tree spans several lines. The sibling
    gate measured a line-oriented scan finding 2 sites where the tree walk
    found 24, so a regex implementation would report green over the defect.
    """
    node_dir = tmp_path / "nodes" / "node_projection_x"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        "projection_api:\n"
        "  expose: true\n"
        "  exposures:\n"
        "    - topic: t.x.v1\n"
        '      key_grain: "mutable"\n'
    )
    (node_dir / "handler.py").write_text(
        "def publish(row):\n"
        "    return encode_snapshot_delta(\n"
        "        row=row,\n"
        "        source_topic=(\n"
        '            "t.x.v1"\n'
        "        ),\n"
        "        source_offset=(\n"
        "            0\n"
        "        ),\n"
        "    )\n"
    )
    assert _run(tmp_path) == 1


def test_ac2_a_writer_under_tests_is_not_read_as_production(tmp_path: Path) -> None:
    """A fixture passing zero is not a writer publishing a constant."""
    node_dir = tmp_path / "nodes" / "node_projection_x"
    (node_dir / "tests").mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        "projection_api:\n"
        "  expose: true\n"
        "  exposures:\n"
        "    - topic: t.x.v1\n"
        '      key_grain: "mutable"\n'
    )
    (node_dir / "tests" / "test_x.py").write_text(
        "def test_x():\n    encode_snapshot_delta(source_offset=0)\n"
    )
    assert _run(tmp_path) == 0


# --------------------------------------------------------------------------
# AC3 -- an immutable declaration must be backed by evidence
# --------------------------------------------------------------------------


def test_ac3_an_immutable_declaration_with_no_evidence_is_refused(
    tmp_path: Path,
) -> None:
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain="immutable")
    assert _run(tmp_path) == 1


def test_ac3_a_content_addressing_assertion_satisfies_it(tmp_path: Path) -> None:
    _contract(
        tmp_path / "nodes",
        "node_projection_x",
        "t.x.v1",
        key_grain="immutable",
        test_body=(
            "def test_every_distinct_source_event_owns_its_own_key():\n"
            "    assert derive_key(a) != derive_key(b)\n"
        ),
    )
    assert _run(tmp_path) == 0


def test_ac3_a_correctly_named_stub_does_not_satisfy_it(tmp_path: Path) -> None:
    """The name is not the evidence. Without an assertion it proves nothing."""
    _contract(
        tmp_path / "nodes",
        "node_projection_x",
        "t.x.v1",
        key_grain="immutable",
        test_body=(
            "def test_every_distinct_source_event_owns_its_own_key():\n    pass\n"
        ),
    )
    assert _run(tmp_path) == 1


def test_ac3_positive_control_both_live_exemptions_pass_untouched() -> None:
    """Session replay and work events were not written for this gate.

    They already carry the assertion, which is the whole basis on which their
    constant coordinate is an exemption rather than the defect.
    """
    exposures = [
        e
        for e in gate.collect_exposures(gate.DEFAULT_NODES_ROOT)
        if e.key_grain == "immutable"
    ]
    assert {e.node for e in exposures} == {
        "node_projection_session_replay",
        "node_projection_work_events",
    }
    findings = gate.evaluate(
        exposures,
        nodes_root=gate.DEFAULT_NODES_ROOT,
        tests_root=gate.DEFAULT_TESTS_ROOT,
    )
    assert findings == []


# --------------------------------------------------------------------------
# AC4 -- the two live exemptions are behaviourally unchanged
# --------------------------------------------------------------------------


def test_ac4_neither_exemption_writer_line_was_edited() -> None:
    """Both handlers still publish their fixed coordinate, as they should.

    This change declares the grain; it does not touch the two sites, because
    both are correct. If a later change repoints one of them, this goes red
    and the exemption has to be re-argued rather than quietly dropped.
    """
    session = Path(
        "src/omnimarket/nodes/node_projection_session_replay/handlers/"
        "handler_projection_session_replay.py"
    ).read_text()
    work = Path(
        "src/omnimarket/nodes/node_projection_work_events/handlers/"
        "handler_projection_work_events.py"
    ).read_text()
    assert "source_offset=0," in session
    assert "source_offset=0," in work


# --------------------------------------------------------------------------
# AC5 -- the scan proves it ran
# --------------------------------------------------------------------------


def test_ac5_a_collapsed_scan_fails_on_the_floor(tmp_path: Path) -> None:
    """A gate over a collapsed set proves nothing, so it is read as broken."""
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain="mutable")
    assert _run(tmp_path, minimum=55) == 1


def test_ac5_the_real_tree_clears_the_declared_floor() -> None:
    exposures = gate.collect_exposures(gate.DEFAULT_NODES_ROOT)
    assert len(exposures) >= gate.DEFAULT_MIN_EXPECTED_EXPOSURES


def test_ac5_the_gate_is_green_on_the_real_tree() -> None:
    """The literal assertion the pre-commit hook and the CI job make."""
    assert gate.main([]) == 0


# --------------------------------------------------------------------------
# The fence is a fence, not a baseline
# --------------------------------------------------------------------------


def test_the_fence_is_empty_because_every_exposure_is_declared() -> None:
    """Shipping the mechanism empty is the point.

    A fence added later, under the pressure of a failing gate, is a baseline
    wearing the word fence.
    """
    assert gate._FENCED == ()


def test_a_stale_fence_row_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fenced row that no longer occurs must be deleted, not left to rot."""
    _contract(tmp_path / "nodes", "node_projection_x", "t.x.v1", key_grain="mutable")
    monkeypatch.setattr(
        gate, "_FENCED_PAIRS", frozenset({("node_projection_gone", "t.gone.v1")})
    )
    assert _run(tmp_path) == 1


def test_a_fenced_exposure_does_not_license_a_second_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fencing one finding must not silence the next one beside it."""
    nodes = tmp_path / "nodes"
    _contract(nodes, "node_projection_x", "t.x.v1", key_grain=None)
    _contract(nodes, "node_projection_y", "t.y.v1", key_grain=None)
    monkeypatch.setattr(
        gate, "_FENCED_PAIRS", frozenset({("node_projection_x", "t.x.v1")})
    )
    assert _run(tmp_path) == 1


# --------------------------------------------------------------------------
# Shape coverage -- a gate blind to half the tree reads as coverage
# --------------------------------------------------------------------------


def test_both_contract_shapes_are_enumerated(tmp_path: Path) -> None:
    """The tree carries a legacy inline exposure and an exposures list.

    A gate that understood only one would report green over the other, which
    is the blindness this epic exists to remove.
    """
    nodes = tmp_path / "nodes"
    legacy = nodes / "node_projection_legacy"
    legacy.mkdir(parents=True)
    (legacy / "contract.yaml").write_text(
        "projection_api:\n"
        "  expose: true\n"
        "  topic: t.legacy.v1\n"
        '  key_grain: "mutable"\n'
    )
    _contract(nodes, "node_projection_listed", "t.listed.v1", key_grain="mutable")

    topics = {e.topic for e in gate.collect_exposures(nodes)}
    assert topics == {"t.legacy.v1", "t.listed.v1"}


def test_an_unexposed_contract_is_not_enumerated(tmp_path: Path) -> None:
    """``expose: false`` declares no serving surface, so there is no grain."""
    nodes = tmp_path / "nodes"
    node = nodes / "node_projection_hidden"
    node.mkdir(parents=True)
    (node / "contract.yaml").write_text(
        "projection_api:\n  expose: false\n  topic: t.hidden.v1\n"
    )
    assert gate.collect_exposures(nodes) == []
