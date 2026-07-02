# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/validate_state_coverage.py.

Regression coverage:
- FSM state extraction from both ``fsm.states`` (list[str]) and
  ``state_machine.states`` (list[dict] with ``state_name``) shapes.
- Output-class/event-type extraction for compute/effect/reducer contracts.
- Coverage detection via quoted-string and bare-attribute matches.
- Baseline WARN-vs-FAIL gating: baselined gaps stay WARN unless strict=True.
- Empty declared-state contracts are skipped, not FAILed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_state_coverage.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "validate_state_coverage", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_state_coverage"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scc_module() -> object:
    return _load_module()


@pytest.mark.unit
def test_extract_fsm_states_from_fsm_block(scc_module: object) -> None:
    """Orchestrator-style ``fsm.states: [str, ...]`` shape is extracted."""
    contract = {
        "node_type": "orchestrator",
        "fsm": {"states": ["COLLECTING", "SCORING", "COMPLETE", "BLOCKED"]},
    }
    ds = scc_module.declared_states(contract)  # type: ignore[attr-defined]
    assert ds.kind == "fsm"
    assert ds.states == ["COLLECTING", "SCORING", "COMPLETE", "BLOCKED"]


@pytest.mark.unit
def test_extract_fsm_states_from_state_machine_block(scc_module: object) -> None:
    """Reducer-style ``state_machine.states: [{state_name: ...}, ...]`` shape."""
    contract = {
        "node_type": "reducer",
        "state_machine": {
            "states": [
                {"state_name": "idle", "description": "no state yet"},
                {"state_name": "active", "description": "active"},
                {"state_name": "ended", "is_terminal": True},
            ]
        },
    }
    ds = scc_module.declared_states(contract)  # type: ignore[attr-defined]
    assert ds.kind == "fsm"
    assert ds.states == ["idle", "active", "ended"]


@pytest.mark.unit
def test_extract_output_states_for_compute_node(scc_module: object) -> None:
    """No FSM block -> declared states fall back to outputs + publish_topics."""
    contract = {
        "node_type": "compute",
        "outputs": {
            "overall_status": {"type": "string"},
            "backend_statuses": {"type": "array"},
        },
        "event_bus": {
            "publish_topics": ["onex.evt.omnimarket.knowledge-health-classified.v1"]
        },
        "terminal_event": "onex.evt.omnimarket.knowledge-health-classified.v1",
    }
    ds = scc_module.declared_states(contract)  # type: ignore[attr-defined]
    assert ds.kind == "outputs"
    # terminal_event de-dups against the identical publish_topics entry.
    assert ds.states == [
        "overall_status",
        "backend_statuses",
        "onex.evt.omnimarket.knowledge-health-classified.v1",
    ]


@pytest.mark.unit
def test_declared_states_empty_when_no_states_declared(scc_module: object) -> None:
    ds = scc_module.declared_states({"node_type": "effect"})  # type: ignore[attr-defined]
    assert ds.states == []


@pytest.mark.unit
def test_state_covered_by_quoted_string(scc_module: object) -> None:
    covered = scc_module._state_covered(  # type: ignore[attr-defined]
        "active", 'assert new_state.current_phase == "active"'
    )
    assert covered is True


@pytest.mark.unit
def test_state_covered_by_bare_attribute(scc_module: object) -> None:
    covered = scc_module._state_covered(  # type: ignore[attr-defined]
        "overall_status", "assert result.overall_status == 'healthy'"
    )
    assert covered is True


@pytest.mark.unit
def test_state_not_covered(scc_module: object) -> None:
    covered = scc_module._state_covered(  # type: ignore[attr-defined]
        "blocked", "assert result.status == 'ok'"
    )
    assert covered is False


@pytest.mark.unit
def test_topic_state_requires_exact_quoted_match_not_attribute(
    scc_module: object,
) -> None:
    """Topic-shaped states (contain '.') must not false-positive on attribute regex."""
    state = "onex.evt.omnimarket.foo-completed.v1"
    # Attribute-style access would never legitimately assert a topic string,
    # so a bare mention without quotes must not count as coverage.
    covered = scc_module._state_covered(  # type: ignore[attr-defined]
        state, "result.onex.evt.omnimarket.foo-completed.v1.something"
    )
    assert covered is False
    covered_quoted = scc_module._state_covered(  # type: ignore[attr-defined]
        state, f'publish_topic == "{state}"'
    )
    assert covered_quoted is True


# ---------------------------------------------------------------------------
# AST hardening (OMN-13816): vacuous / tautological forms must NOT count as
# coverage; genuine assertions against non-constant output still must.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_literal_assert_is_not_covered(scc_module: object) -> None:
    """`assert "<state>"` proves nothing — no comparison against output."""
    assert scc_module._state_covered("active", 'assert "active"') is False  # type: ignore[attr-defined]
    topic = "onex.evt.omnimarket.foo.v1"
    assert scc_module._state_covered(topic, f'assert "{topic}"') is False  # type: ignore[attr-defined]


@pytest.mark.unit
def test_literal_self_equality_is_not_covered(scc_module: object) -> None:
    """Constant-vs-constant tautology (`"active" == "active"`) is vacuous."""
    assert (
        scc_module._state_covered("active", 'assert "active" == "active"')  # type: ignore[attr-defined]
        is False
    )


@pytest.mark.unit
def test_name_self_equality_is_not_covered(scc_module: object) -> None:
    """`declared_topics == declared_topics` — the human-caught squash-shipped
    self-equality — must not count."""
    assert (
        scc_module._state_covered(  # type: ignore[attr-defined]
            "declared_topics", "assert declared_topics == declared_topics"
        )
        is False
    )


@pytest.mark.unit
def test_attribute_self_equality_is_not_covered(scc_module: object) -> None:
    """`result.overall_status == result.overall_status` — attribute-shaped
    self-tautology — must not count even though `.overall_status` appears."""
    assert (
        scc_module._state_covered(  # type: ignore[attr-defined]
            "overall_status",
            "assert result.overall_status == result.overall_status",
        )
        is False
    )


@pytest.mark.unit
def test_docstring_only_mention_is_not_covered(scc_module: object) -> None:
    """A state named only in a docstring / bare string statement is not covered."""
    source = '''
def test_thing() -> None:
    """active is the running phase."""
    assert something_else == 1
'''
    assert scc_module._state_covered("active", source) is False  # type: ignore[attr-defined]


@pytest.mark.unit
def test_comment_only_mention_is_not_covered(scc_module: object) -> None:
    """Comments are absent from the AST, so a comment mention is not coverage."""
    source = "# transitions through the 'active' phase\nassert other == 2\n"
    assert scc_module._state_covered("active", source) is False  # type: ignore[attr-defined]


@pytest.mark.unit
def test_import_path_mention_is_not_covered(scc_module: object) -> None:
    """An import module path (`from omnimarket.review... import`) must not
    false-positive as coverage of a `review` state — the exploit the old
    regex's `.review` match admitted."""
    source = "from omnimarket.review.pr_review_fsm import advance\nassert x == 1\n"
    assert scc_module._state_covered("review", source) is False  # type: ignore[attr-defined]


@pytest.mark.unit
def test_genuine_comparison_is_covered(scc_module: object) -> None:
    """`assert result.state == "active"` (literal vs non-constant output) counts."""
    assert (
        scc_module._state_covered("active", 'assert result.state == "active"')  # type: ignore[attr-defined]
        is True
    )


@pytest.mark.unit
def test_enum_attribute_in_for_loop_iterable_is_covered(scc_module: object) -> None:
    """The common enum idiom — states listed in a for-loop iterable that feeds
    an assert — is genuine coverage (`EnumX.INVENTORYING`)."""
    source = (
        "for state in (EnumX.IDLE, EnumX.INVENTORYING, EnumX.MERGING):\n"
        "    assert state not in terminal\n"
    )
    assert scc_module._state_covered("INVENTORYING", source) is True  # type: ignore[attr-defined]


@pytest.mark.unit
def test_assert_equal_call_is_covered(scc_module: object) -> None:
    """unittest-style `assertEqual(result.state, "active")` counts."""
    assert (
        scc_module._state_covered(  # type: ignore[attr-defined]
            "active", 'self.assertEqual(result.state, "active")'
        )
        is True
    )


@pytest.mark.unit
def test_per_file_sources_survive_duplicate_future_imports(
    scc_module: object,
) -> None:
    """`_state_covered` accepts a per-file list; two files each carrying
    `from __future__ import annotations` must not break parsing (a single
    concatenated `ast.parse` would raise SyntaxError)."""
    file_a = "from __future__ import annotations\nassert x == 1\n"
    file_b = 'from __future__ import annotations\nassert result.state == "active"\n'
    assert scc_module._state_covered("active", [file_a, file_b]) is True  # type: ignore[attr-defined]


@pytest.mark.unit
def test_validate_node_fails_on_uncovered_declared_state(
    scc_module: object, tmp_path: Path
) -> None:
    node_dir = tmp_path / "node_example_reducer"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: reducer\n"
        "state_machine:\n"
        "  states:\n"
        "    - state_name: idle\n"
        "    - state_name: done\n"
    )
    result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=set(), strict=False, test_corpus=[]
    )
    assert result.passed is False
    assert set(result.uncovered) == {"idle", "done"}


@pytest.mark.unit
def test_validate_node_passes_when_states_covered_in_corpus(
    scc_module: object, tmp_path: Path
) -> None:
    node_dir = tmp_path / "node_example_reducer"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: reducer\n"
        "state_machine:\n"
        "  states:\n"
        "    - state_name: idle\n"
        "    - state_name: done\n"
    )
    corpus = [
        (
            "tests/test_node_example_reducer.py",
            'assert new_state.phase == "idle"\nassert new_state.phase == "done"\n',
        )
    ]
    result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=set(), strict=False, test_corpus=corpus
    )
    assert result.passed is True
    assert result.uncovered == []


@pytest.mark.unit
def test_validate_node_baseline_stays_warn_when_not_strict(
    scc_module: object, tmp_path: Path
) -> None:
    node_dir = tmp_path / "node_example_reducer"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: reducer\nstate_machine:\n  states:\n    - state_name: idle\n"
    )
    baseline = {("node_example_reducer", "idle")}

    warn_result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=baseline, strict=False, test_corpus=[]
    )
    assert warn_result.passed is True, "baselined gap must stay WARN when not strict"
    assert warn_result.baselined_uncovered == ["idle"]


@pytest.mark.unit
def test_validate_node_baseline_promoted_to_fail_when_strict(
    scc_module: object, tmp_path: Path
) -> None:
    node_dir = tmp_path / "node_example_reducer"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: reducer\nstate_machine:\n  states:\n    - state_name: idle\n"
    )
    baseline = {("node_example_reducer", "idle")}

    fail_result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=baseline, strict=True, test_corpus=[]
    )
    assert fail_result.passed is False, (
        "directly-modified node must FAIL on its own baselined gap in strict mode"
    )
    assert fail_result.uncovered == ["idle"]


@pytest.mark.unit
def test_validate_node_no_contract_returns_empty_kind(
    scc_module: object, tmp_path: Path
) -> None:
    node_dir = tmp_path / "node_no_contract"
    node_dir.mkdir()
    result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=set(), strict=False, test_corpus=[]
    )
    assert result.kind == "none"
    assert result.passed is True


@pytest.mark.unit
def test_load_baseline_parses_pairs(
    scc_module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_file = tmp_path / "baseline.txt"
    baseline_file.write_text(
        "# comment line\n"
        "\n"
        "node_ab_compare_reducer compared\n"
        "node_two_strike_arbiter onex.evt.omnimarket.two-strike-arbiter.v1\n"
    )
    monkeypatch.setattr(scc_module, "BASELINE_PATH", baseline_file)
    pairs = scc_module._load_baseline()  # type: ignore[attr-defined]
    assert pairs == {
        ("node_ab_compare_reducer", "compared"),
        ("node_two_strike_arbiter", "onex.evt.omnimarket.two-strike-arbiter.v1"),
    }


@pytest.mark.unit
def test_load_baseline_missing_file_returns_empty(
    scc_module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scc_module, "BASELINE_PATH", tmp_path / "does-not-exist.txt")
    assert scc_module._load_baseline() == set()  # type: ignore[attr-defined]
