# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-18013 topic/category/event-type closure gates.

Each gate here is proven RED on the shape that existed at the parent commit and GREEN on
the shape this change lands, so "the gate works" is a test result rather than a claim.

The parent shapes are reproduced VERBATIM in-test rather than read from git: a gate whose
red-proof depends on a revision walk stops proving anything the moment the branch is
rebased, and this is exactly the class of check that must not be able to no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.testing.publisher_contract_fixture import (
    NoDeclaredPublisherError,
    declared_publishers,
    publisher_event_type,
)
from omnimarket.validators import no_baseline_refreeze
from omnimarket.validators import no_literal_event_type_in_tests as no_literal
from omnimarket.validators.contract_topic_graph import main as contract_topic_graph_main
from omnimarket.validators.handler_event_type_source import scan_source
from omnimarket.validators.no_literal_event_type_in_tests import scan as scan_tests

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# HandlerRuntimeCloseoutOrchestrator.handle as it stood at the parent commit. Every one
# of these four branches was dead on the wire: the consume boundary stamps the alias
# `omnimarket.redeploy-completed`, which does not end in `.v1`, so every real message
# fell through to the else-branch and RESTARTED the closeout.
PARENT_HANDLER_SHAPE = '''
def handle(self, envelope):
    """Docstring mentioning event_type.endswith("redeploy-completed.v1") is not a match."""
    event_type = envelope.event_type or ""
    if event_type.endswith("closeout-preflight-completed.v1"):
        return self._on_preflight(envelope)
    elif event_type.endswith("closeout-fitness-gated.v1"):
        return self._on_fitness(envelope)
    elif event_type.endswith("redeploy-completed.v1"):
        return self._on_deploy(envelope)
    elif event_type.endswith("closeout-proof-matrix-completed.v1"):
        return self._on_proof_matrix(envelope)
    return self._on_start(envelope)
'''

# The shape this change lands: the accepted keys are derived from the node's own
# contract, so the handler cannot disagree with what the bus carries.
FIXED_HANDLER_SHAPE = """
_SUBSCRIBE = contract_subscribe_topics(_CONTRACT)
MATCH_REDEPLOY_COMPLETED = _match_keys("redeploy-completed.v1")


def handle(self, envelope):
    event_type = envelope.event_type or ""
    if event_type in MATCH_REDEPLOY_COMPLETED:
        return self._on_deploy(envelope)
    return self._on_start(envelope)
"""


class TestHandlerEventTypeSource:
    """Gate 2 — the event type a handler matches comes from the contract."""

    def test_refuses_the_parent_handler_shape(self) -> None:
        findings = scan_source(PARENT_HANDLER_SHAPE, "parent_handler.py")
        assert [f.literal for f in findings] == [
            "closeout-preflight-completed.v1",
            "closeout-fitness-gated.v1",
            "redeploy-completed.v1",
            "closeout-proof-matrix-completed.v1",
        ]

    def test_a_docstring_quoting_the_shape_is_not_a_match(self) -> None:
        """The gate reads the AST, so prose about the defect is not the defect."""
        source = '"""event_type.endswith("x.v1") — do not do this."""\nx = 1\n'
        assert scan_source(source, "prose.py") == []

    def test_accepts_the_contract_derived_shape(self) -> None:
        assert scan_source(FIXED_HANDLER_SHAPE, "fixed_handler.py") == []

    def test_a_membership_test_against_a_literal_is_still_refused(self) -> None:
        """`in ("a.v1",)` is the same hand-typing with different syntax."""
        source = 'if event_type in ("onex.evt.omnimarket.x.v1",):\n    pass\n'
        assert len(scan_source(source, "member.py")) == 1

    def test_the_live_tree_is_clean(self) -> None:
        from omnimarket.validators.handler_event_type_source import scan

        findings, files = scan(REPO_ROOT / "src" / "omnimarket")
        assert files > 400, "vacuous scan — the positive control above must have run"
        assert findings == []


class TestPublisherContractFixture:
    """Gate 4 — a golden chain builds its input from the publisher's contract."""

    def test_event_type_is_the_alias_the_bus_carries_not_the_topic(self) -> None:
        topic = "onex.evt.omnimarket.redeploy-completed.v1"
        assert publisher_event_type(topic) == "omnimarket.redeploy-completed"
        assert publisher_event_type(topic) != topic

    def test_refuses_a_topic_no_contract_publishes(self) -> None:
        with pytest.raises(NoDeclaredPublisherError) as excinfo:
            publisher_event_type("onex.evt.omnimarket.nothing-declares-this.v1")
        assert "No contract declares a publisher" in str(excinfo.value)

    def test_a_real_publisher_resolves_by_contract_name(self) -> None:
        """Positive control: the zero above is a real zero, not an empty index."""
        assert declared_publishers("onex.evt.omnimarket.redeploy-completed.v1")


# The two fixtures below assemble the FORBIDDEN shape at runtime instead of spelling it.
# Spelling it would make this file — the test OF gate 4 — the last remaining violation of
# gate 4, and the only ways out of that would be an exemption list (which is the baseline
# the operator ruling of 2026-09-06 exists to remove) or leaving the gate unwired. Neither
# is acceptable, and neither is needed: the lint matches an `event_type` assignment whose
# value is a topic LITERAL on the same source line, so a value composed from a name is not
# a match here while the file it writes is still byte-for-byte the shape under test.
_PUBLISHED_TOPIC = "onex.evt.omnimarket.redeploy-completed.v1"
_COMMAND_TOPIC = "onex.cmd.omnimarket.redeploy-start.v1"


def _hand_typed_kwarg(topic: str) -> str:
    """`event_type="<topic>"` as a keyword argument — the shape gate 4 refuses."""
    return "envelope = E(event_type=" + f'"{topic}"' + ")\n"


def _hand_typed_dict(topic: str) -> str:
    """`"event_type": "<topic>"` as a dict entry — the same defect, dict spelling."""
    return 'payload = {"event_type": ' + f'"{topic}"' + "}\n"


class TestNoLiteralEventTypeInTests:
    """Gate 4's lint — the shape the converted tests must not regress to."""

    def test_refuses_a_hand_typed_event_type(self, tmp_path: Path) -> None:
        (tmp_path / "test_x.py").write_text(_hand_typed_kwarg(_PUBLISHED_TOPIC))
        findings, files = scan_tests(tmp_path)
        assert files == 1
        assert [f.topic for f in findings] == [
            "onex.evt.omnimarket.redeploy-completed.v1"
        ]

    def test_refuses_the_dict_spelling_too(self, tmp_path: Path) -> None:
        (tmp_path / "test_x.py").write_text(_hand_typed_dict(_COMMAND_TOPIC))
        findings, _ = scan_tests(tmp_path)
        assert len(findings) == 1

    def test_accepts_the_fixture_form(self, tmp_path: Path) -> None:
        (tmp_path / "test_x.py").write_text(
            "envelope = E(event_type=publisher_event_type("
            '"onex.evt.omnimarket.redeploy-completed.v1"))\n'
        )
        findings, _ = scan_tests(tmp_path)
        assert findings == []

    def test_a_bare_topic_mention_is_not_a_match(self, tmp_path: Path) -> None:
        """Only an ASSIGNMENT trips it — a subscribe list or constant does not."""
        (tmp_path / "test_x.py").write_text(
            'TOPIC = "onex.evt.omnimarket.redeploy-completed.v1"\n'
            'topics = ["onex.cmd.omnimarket.redeploy-start.v1"]\n'
        )
        findings, _ = scan_tests(tmp_path)
        assert findings == []


class TestNoBaselineRefreeze:
    """Gate 5 — a burned-down baseline may not come back."""

    def test_the_live_tree_passes(self) -> None:
        assert no_baseline_refreeze.check(REPO_ROOT) == []

    def test_refuses_a_recreated_baseline(self, tmp_path: Path) -> None:
        for relative, _ in no_baseline_refreeze.DELETED_BASELINES:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("accepted: []\n")
        for relative, (_, rows) in no_baseline_refreeze.PEER_FENCED_BASELINES.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump({"rows": [{"contract": c, "topic": k} for c, k in rows]})
            )
        problems = no_baseline_refreeze.check(tmp_path)
        assert len(problems) == len(no_baseline_refreeze.DELETED_BASELINES)
        assert all("is BACK" in p for p in problems)

    def test_refuses_a_peer_fenced_baseline_that_grew(self, tmp_path: Path) -> None:
        for relative, (_, rows) in no_baseline_refreeze.PEER_FENCED_BASELINES.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            grown = [{"contract": c, "topic": k} for c, k in rows]
            grown.append({"contract": "some_new_node", "topic": "onex.evt.x.y.v1"})
            path.write_text(yaml.safe_dump({"rows": grown}))
        problems = no_baseline_refreeze.check(tmp_path)
        assert len(problems) == len(no_baseline_refreeze.PEER_FENCED_BASELINES)
        assert all("is NOT peer-fenced" in p for p in problems)


class TestPeerFencedBaselinesHoldOnlyPeerRows:
    """The two baselines that survive carry ONLY rows owned outside this change.

    Ledger CLAIM docs/tracking/ROLLING_WORK_LEDGER.md:3904 (lane dev-lane-fsm-residuals,
    OMN-16939 / OMN-17888). Both files are deleted, and --baseline dropped from the hook
    and the CI job, the moment that lane lands its rows.
    """

    def test_subscriber_dispatcher_baseline_is_only_the_fenced_rows(self) -> None:
        """5 peer-owned redeploy-FSM rows + 2 node_e2e_orchestrator rows, nothing else.

        node_e2e_orchestrator is not peer-owned: it needs a handler class extracted from
        its standalone consumer.py, which is a node refactor rather than a category or
        alias fix. Deleting its subscribe declarations instead was tried and rejected --
        the node-orphan-graph gate then classifies the node PRODUCER_ONLY and hard-fails.
        """
        path = (
            REPO_ROOT
            / "config/validation/subscriber_dispatcher_resolution_baseline.yaml"
        )
        rows = yaml.safe_load(path.read_text())["known_unresolved_subscriptions"]
        assert {r["contract"] for r in rows} == {
            "node_redeploy_orchestrator",
            "node_redeploy_deploy_effect",
            "node_e2e_orchestrator",
        }
        assert len(rows) == 7

    def test_mixed_category_baseline_is_the_one_peer_row(self) -> None:
        path = (
            REPO_ROOT
            / "config/validation/mixed_category_routing_omnimarket_baseline.yaml"
        )
        rows = yaml.safe_load(path.read_text())["known_mixed_category_entries"]
        assert [r["contract"] for r in rows] == ["node_redeploy_deploy_effect"]


class TestContractTopicGraphScope:
    """Gate 3 — --scope is HARD mode: per-repo, zero baseline, no softening flag."""

    def test_scope_refuses_write_baseline(self) -> None:
        """HARD mode may not freeze what it is not allowed to accept."""
        with pytest.raises(SystemExit) as excinfo:
            contract_topic_graph_main(["--scope", "omnimarket", "--write-baseline"])
        assert excinfo.value.code == 2

    def test_scope_refuses_an_unknown_package(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            contract_topic_graph_main(["--scope", "not_a_package"])
        assert excinfo.value.code == 2

    def test_scope_flag_exists_and_is_repeatable(self) -> None:
        """The flag is what makes per-repo landing possible at all: without it no repo
        can turn the gate hard until every repo has."""
        import inspect

        from omnimarket.validators import contract_topic_graph

        source = inspect.getsource(contract_topic_graph.main)
        assert '"--scope"' in source
        assert 'action="append"' in source
        # HARD mode consults no baseline -- OMN-18013 went further and DELETED the
        # baseline file, so the gate no longer has a --baseline flag to consult.
        assert "NO baseline exists" in source
        assert "--baseline" not in source


class TestGate4FenceCannotRot:
    """The fence in gate 4 is a pinned pair list, not a baseline.

    A baseline grows when a new defect appears and silently keeps entries after they are
    fixed. This asserts the two properties that make the fence the opposite of that: a
    fenced entry that no longer occurs is a HARD FAILURE, and a fenced FILE is not a
    licence — a new topic in the same file still fails.
    """

    def test_every_fenced_pair_is_still_present_in_the_tree(self) -> None:
        """Positive control: the fence is describing the real tree, not a memory of it."""
        findings, files = scan_tests(Path("tests"))
        assert files > no_literal.DEFAULT_MIN_EXPECTED_FILES
        observed = {(no_literal._norm(f.path), f.topic) for f in findings}
        missing = sorted(no_literal._FENCED_PAIRS - observed)
        assert missing == [], (
            "fenced pair(s) no longer occur — delete them from _FENCED rather than "
            f"leaving the fence to rot: {missing}"
        )

    def test_a_stale_fence_entry_fails_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A fixed site still listed FAILS, so the list cannot become an exemption list."""
        (tmp_path / "test_clean.py").write_text("x = 1\n")
        monkeypatch.setattr(
            no_literal,
            "_FENCED_PAIRS",
            frozenset({("tests/test_already_fixed.py", "onex.evt.omnimarket.gone.v1")}),
        )
        assert no_literal.main([str(tmp_path), "0"]) == 1

    def test_a_fenced_file_is_not_a_licence_for_a_new_topic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fence is keyed on the (path, topic) PAIR, never on the path alone."""
        fenced_path = "test_redeploy_input_boundary.py"
        (tmp_path / fenced_path).write_text(
            _hand_typed_kwarg("onex.evt.omnimarket.brand-new-topic.v1")
        )
        monkeypatch.setattr(
            no_literal,
            "_FENCED_PAIRS",
            frozenset({(fenced_path, "onex.cmd.omnimarket.redeploy-start.v1")}),
        )
        # The fenced pair is absent from this tree, so the stale check fires first —
        # which is itself the point: neither route lets the NEW topic through.
        assert no_literal.main([str(tmp_path), "0"]) == 1

    def test_the_gate_is_green_on_the_real_tree(self) -> None:
        """Enforcement, not detection: this is what the wired CI job asserts."""
        assert no_literal.main(["tests"]) == 0
