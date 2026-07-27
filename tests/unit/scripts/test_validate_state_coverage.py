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
def test_validate_node_baseline_stays_warn_when_strict_and_deprecated(
    scc_module: object, tmp_path: Path
) -> None:
    """A node explicitly marked lifecycle: deprecated (OMN-14151) is exempt
    from strict-mode baseline promotion — a hard-gated legacy surface neuters
    itself rather than needing new test investment for the states it will
    stop emitting."""
    node_dir = tmp_path / "node_example_reducer"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: reducer\n"
        "lifecycle: deprecated\n"
        "state_machine:\n  states:\n    - state_name: idle\n"
    )
    baseline = {("node_example_reducer", "idle")}

    result = scc_module.validate_node(  # type: ignore[attr-defined]
        node_dir, baseline=baseline, strict=True, test_corpus=[]
    )
    assert result.passed is True, (
        "deprecated node's baselined gap must stay WARN even in strict mode"
    )
    assert result.baselined_uncovered == ["idle"]


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


@pytest.mark.unit
def test_changed_nodes_uses_exact_segment_not_prefix_substring(
    scc_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test file under a longer node's directory must NOT flag a shorter node
    whose name is a prefix of it.

    Regression: ``node_projection_delegation`` is a prefix of
    ``node_projection_delegation_inference_response``. The old substring match
    (``candidate.name in Path(f).as_posix()``) spuriously flagged the shorter
    node as directly-modified, which in strict mode promoted its baselined WARN
    to a FAIL even though it was untouched.
    """
    import subprocess
    import types

    long_node = "node_projection_delegation_inference_response"
    short_node = "node_projection_delegation"
    # Both must be real node dirs for the iterdir()-based association to see them.
    assert (scc_module.NODES_DIR / long_node).is_dir()  # type: ignore[attr-defined]
    assert (scc_module.NODES_DIR / short_node).is_dir()  # type: ignore[attr-defined]

    diff = f"tests/integration/{long_node}/test_bus_coverage.py\n"

    def _fake_run(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=0, stdout=diff, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _nodes, directly_modified, _contract_touched = scc_module._get_changed_nodes(  # type: ignore[attr-defined]
        "origin/dev"
    )
    assert long_node in directly_modified
    assert short_node not in directly_modified


# ---------------------------------------------------------------------------
# OMN-14009: missing_handler_routing ratchet fixes collide with the
# state-coverage-gate grandfather clause.
#
# A purely-additive handler_routing fix (adding a nested handler: sibling to
# make the node's canonical handler reachable) touches contract.yaml but
# leaves the node's declared FSM states / output classes / published topics
# completely unchanged. Strict mode must not promote that node's unrelated,
# pre-existing baselined state-coverage debt to FAIL just because the two
# ratchets happen to share a file.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_declared_state_shape_changed_false_for_handler_routing_only_diff(
    scc_module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A handler_routing-only contract edit must NOT register as a declared-
    state-shape change (OMN-14009 concrete repro: node_code_embedding_effect)."""
    import subprocess
    import types

    node_name = "node_example_effect"
    node_dir = tmp_path / node_name
    node_dir.mkdir()
    after_contract = (
        "node_type: effect\n"
        "handler:\n"
        "  module: omnimarket.nodes.node_example_effect.handlers.handler_example\n"
        "  name: HandlerExample\n"
        "handler_routing:\n"
        "  handlers:\n"
        "    - operation: do_thing\n"
        "      handler:\n"
        "        module: omnimarket.nodes.node_example_effect.handlers.handler_example\n"
        "        name: HandlerExample\n"
        "outputs:\n"
        "  result: {type: string}\n"
    )
    before_contract = (
        "node_type: effect\n"
        "handler:\n"
        "  module: omnimarket.nodes.node_example_effect.handlers.handler_example\n"
        "  name: HandlerExample\n"
        "outputs:\n"
        "  result: {type: string}\n"
    )
    (node_dir / "contract.yaml").write_text(after_contract)

    monkeypatch.setattr(scc_module, "NODES_DIR", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(scc_module, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]

    def _fake_run(args: list[str], **_kwargs: object) -> types.SimpleNamespace:
        assert args[:2] == ["git", "show"]
        return types.SimpleNamespace(returncode=0, stdout=before_contract, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert (
        scc_module._declared_state_shape_changed(node_name, "origin/dev")  # type: ignore[attr-defined]
        is False
    )


@pytest.mark.unit
def test_read_contract_at_ref_uses_rev_path_object_syntax_no_delimiter(
    scc_module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``git show <ref>:<path>`` (no ``--`` delimiter) is the correct invocation.

    OMN-15005 regression: a prior revision inserted ``--`` before the combined
    ``<ref>:<path>`` argument. Git's ``rev:path`` object-notation is ONLY
    recognized when NOT preceded by ``--`` -- with the delimiter present, git
    instead treats the whole string as a literal pathspec, which never matches
    an on-disk file and silently returns empty output (exit 0) instead of
    erroring. That silently defeated ``_declared_state_shape_changed``'s
    before/after comparison for EVERY call (``before`` was always parsed as
    ``{}``), which broke the OMN-14009 "purely-additive handler_routing edit
    stays strict-exempt" guarantee for every node with real declared outputs.

    A ref that looks like an option flag (e.g. ``-untrusted-ref``) still fails
    safely without the delimiter: git rejects it with a non-zero exit
    ("unrecognized argument"), which the caller already treats as unreadable
    and maps to ``None`` (fail-safe -> shape treated as changed). No delimiter
    is needed to preserve that fail-safe property.
    """
    import subprocess
    import types

    before_contract = "node_type: effect\noutputs:\n  result: {type: string}\n"
    seen_args: list[str] = []

    monkeypatch.setattr(scc_module, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]

    def _fake_run(args: list[str], **_kwargs: object) -> types.SimpleNamespace:
        seen_args.extend(args)
        return types.SimpleNamespace(returncode=0, stdout=before_contract, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = scc_module._read_contract_at_ref(  # type: ignore[attr-defined]
        "src/omnimarket/nodes/node_example_effect/contract.yaml",
        "origin/dev",
    )

    assert seen_args == [
        "git",
        "show",
        "origin/dev:src/omnimarket/nodes/node_example_effect/contract.yaml",
    ]
    assert result == {"node_type": "effect", "outputs": {"result": {"type": "string"}}}


@pytest.mark.unit
def test_read_contract_at_ref_returns_none_on_unrecognized_ref_like_flag(
    scc_module: object,
) -> None:
    """A ref shaped like an option flag hits the REAL git binary (no mock) and
    fails closed: non-zero exit -> ``None`` -> caller treats shape as changed."""
    result = scc_module._read_contract_at_ref(  # type: ignore[attr-defined]
        "src/omnimarket/nodes/node_agent_coordinator_orchestrator/contract.yaml",
        "-untrusted-ref",
    )
    assert result is None


@pytest.mark.unit
def test_declared_state_shape_changed_true_for_real_output_change(
    scc_module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contract edit that genuinely adds/removes a declared output/state
    DOES register as a shape change and keeps the node strict-eligible."""
    import subprocess
    import types

    node_name = "node_example_effect"
    node_dir = tmp_path / node_name
    node_dir.mkdir()
    after_contract = "node_type: effect\noutputs:\n  result: {type: string}\n  extra: {type: string}\n"
    before_contract = "node_type: effect\noutputs:\n  result: {type: string}\n"
    (node_dir / "contract.yaml").write_text(after_contract)

    monkeypatch.setattr(scc_module, "NODES_DIR", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(scc_module, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]

    def _fake_run(args: list[str], **_kwargs: object) -> types.SimpleNamespace:
        assert args[:2] == ["git", "show"]
        return types.SimpleNamespace(returncode=0, stdout=before_contract, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert (
        scc_module._declared_state_shape_changed(node_name, "origin/dev")  # type: ignore[attr-defined]
        is True
    )


@pytest.mark.unit
def test_declared_state_shape_changed_true_when_ref_unreadable(
    scc_module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A brand-new contract (unreadable at the diff-base ref) fails safe —
    stays strict-eligible rather than being silently exempted."""
    import subprocess
    import types

    node_name = "node_example_effect"
    node_dir = tmp_path / node_name
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "node_type: effect\noutputs:\n  result: {type: string}\n"
    )

    monkeypatch.setattr(scc_module, "NODES_DIR", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(scc_module, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]

    def _fake_run(args: list[str], **_kwargs: object) -> types.SimpleNamespace:
        assert args[:2] == ["git", "show"]
        return types.SimpleNamespace(
            returncode=1, stdout="", stderr="fatal: path does not exist"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert (
        scc_module._declared_state_shape_changed(node_name, "origin/dev")  # type: ignore[attr-defined]
        is True
    )


@pytest.mark.unit
def test_resolve_strict_eligible_excludes_handler_routing_only_contract_touch(
    scc_module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMN-14009 core fix: a node whose ONLY diff is a handler_routing-only
    contract.yaml edit is excluded from strict eligibility, so its unrelated
    pre-existing baselined state-coverage debt stays WARN, not FAIL."""
    import subprocess
    import types

    node_name = "node_example_effect"
    node_dir = tmp_path / node_name
    node_dir.mkdir()
    after_contract = (
        "node_type: effect\n"
        "handler:\n  module: pkg.handlers.handler_example\n  name: HandlerExample\n"
        "handler_routing:\n"
        "  handlers:\n"
        "    - operation: do_thing\n"
        "      handler:\n        module: pkg.handlers.handler_example\n        name: HandlerExample\n"
        "outputs:\n  result: {type: string}\n"
    )
    before_contract = (
        "node_type: effect\n"
        "handler:\n  module: pkg.handlers.handler_example\n  name: HandlerExample\n"
        "outputs:\n  result: {type: string}\n"
    )
    (node_dir / "contract.yaml").write_text(after_contract)

    monkeypatch.setattr(scc_module, "NODES_DIR", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(scc_module, "REPO_ROOT", tmp_path)  # type: ignore[attr-defined]

    def _fake_run(args: list[str], **_kwargs: object) -> types.SimpleNamespace:
        assert args[:2] == ["git", "show"]
        return types.SimpleNamespace(returncode=0, stdout=before_contract, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    eligible = scc_module._resolve_strict_eligible(  # type: ignore[attr-defined]
        {node_name}, {node_name}, "origin/dev"
    )
    assert node_name not in eligible


@pytest.mark.unit
def test_resolve_strict_eligible_keeps_test_only_touch_strict_eligible(
    scc_module: object,
) -> None:
    """A node flagged via its OWN test files (not contract_touched) keeps its
    strict eligibility unchanged — this fix only narrows the contract.yaml
    handler_routing collision, not test-driven coverage investment."""
    node_name = "node_untouched_contract"
    eligible = scc_module._resolve_strict_eligible(  # type: ignore[attr-defined]
        {node_name}, set(), "origin/dev"
    )
    assert node_name in eligible


@pytest.mark.unit
def test_get_changed_nodes_reports_contract_touched_files(
    scc_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_get_changed_nodes`` reports which directly-modified nodes were
    touched via their OWN contract.yaml (third return value, OMN-14009)."""
    import subprocess
    import types

    node_name = "node_code_embedding_effect"
    assert (scc_module.NODES_DIR / node_name).is_dir()  # type: ignore[attr-defined]

    diff = f"src/omnimarket/nodes/{node_name}/contract.yaml\n"

    def _fake_run(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=0, stdout=diff, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _nodes, directly_modified, contract_touched = scc_module._get_changed_nodes(  # type: ignore[attr-defined]
        "origin/dev"
    )
    assert node_name in directly_modified
    assert node_name in contract_touched


@pytest.mark.unit
def test_get_changed_nodes_test_file_diff_not_contract_touched(
    scc_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node flagged via a test-file diff (not its own contract.yaml) must
    NOT appear in ``contract_touched`` — only the file path determines that,
    matching the ``handler_routing``-collision distinction this ticket fixes."""
    import subprocess
    import types

    node_name = "node_code_embedding_effect"
    assert (scc_module.NODES_DIR / node_name).is_dir()  # type: ignore[attr-defined]

    diff = f"tests/integration/{node_name}/test_code_embedding_state_coverage.py\n"

    def _fake_run(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=0, stdout=diff, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _nodes, directly_modified, contract_touched = scc_module._get_changed_nodes(  # type: ignore[attr-defined]
        "origin/dev"
    )
    assert node_name in directly_modified
    assert node_name not in contract_touched
