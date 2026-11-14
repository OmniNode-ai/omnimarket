# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error-chain tests for the seam defect derivator (OMN-19998)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_seam_graph_defects import (
    EnumSeamDefectClass,
    ModelSeamDefectGroup,
    ModelSeamGraphCounts,
    ModelSeamGraphDefectReport,
    ModelSeamGraphFacts,
    ModelSeamGraphRequest,
    ModelSeamGraphRule,
    ModelSeamImportFact,
    ModelSeamNodeFact,
    ModelSeamRepoRule,
)
from omnimarket.nodes.node_seam_graph_defects_compute.handlers.handler_seam_graph_defects import (
    HandlerSeamGraphDefects,
    derive_seam_defects,
)

pytestmark = pytest.mark.unit

T1 = "onex.evt.t1.v1"
T2 = "onex.evt.t2.v1"
T_CMD = "onex.cmd.t0.v1"
T_R = "onex.evt.r.v1"


def _rule(**overrides: object) -> ModelSeamGraphRule:
    fields: dict[str, object] = {
        "topic_pattern": r"^onex\.(evt|cmd|dlq|snapshot)\.[a-z0-9._-]+\.v\d+$",
        "command_topic_pattern": r"^onex\.cmd\.[a-z0-9._-]+\.v\d+$",
        "first_n": 2,
        "repos": (
            ModelSeamRepoRule(
                name="repo_a",
                package="pkg_a",
                source_dir="src/pkg_a",
                contract_glob="src/pkg_a/nodes/*/contract.yaml",
                caller_scan_dirs=("src",),
                test_dirs=("tests",),
            ),
        ),
        "overlay_repo": "overlays",
        "overlay_row_glob": "overlays/{node}/overlay.yaml",
        "scanned_extensions": (".py",),
        "excluded_dir_names": ("tests",),
        "sync_io_markers": (r"\bopen\(",),
        "ledger_reader_markers": ("LEDGER",),
        "replay_node_markers": ("replay",),
        "shared_topic_min_consumers": 2,
        "catalog_file_node_threshold": 5,
    }
    fields.update(overrides)
    return ModelSeamGraphRule.model_validate(fields)


def _node(**overrides: object) -> ModelSeamNodeFact:
    fields: dict[str, object] = {
        "repo": "repo_a",
        "name": "node_x",
        "contract_path": "src/pkg_a/nodes/node_x/contract.yaml",
        "node_type": "compute",
        "subscribe_topics": (),
        "publish_topics": (),
        "external_consumed_topics": (),
        "external_produced_topics": (),
        "command_topics": (),
        "needs_overlay": False,
        "overlay_row_present": False,
        "overlay_required_keys": (),
        "overlay_row_keys": (),
        "sync_io_hits": (),
        "ledger_reader_hits": (),
        "referenced_by": ("repo_a:src/caller.py",),
    }
    fields.update(overrides)
    return ModelSeamNodeFact.model_validate(fields)


def _import(**overrides: object) -> ModelSeamImportFact:
    fields: dict[str, object] = {
        "importer_repo": "repo_a",
        "importer_path": "src/pkg_a/nodes/node_x/handlers/handler_x.py",
        "module": "pkg_other.mod",
        "owner_repo": "repo_other",
        "has_seam_test": False,
    }
    fields.update(overrides)
    return ModelSeamImportFact.model_validate(fields)


def _facts(
    nodes: tuple[ModelSeamNodeFact, ...],
    imports: tuple[ModelSeamImportFact, ...] = (),
) -> ModelSeamGraphFacts:
    return ModelSeamGraphFacts(
        nodes=nodes,
        imports=imports,
        contracts_read=len(nodes),
        contracts_unreadable=(),
    )


def _group(
    report: ModelSeamGraphDefectReport, defect_class: EnumSeamDefectClass
) -> ModelSeamDefectGroup:
    return next(g for g in report.groups if g.defect_class == defect_class)


def _golden_nodes() -> tuple[ModelSeamNodeFact, ...]:
    return (
        _node(name="node_a", subscribe_topics=(T1,)),
        _node(name="node_b", publish_topics=(T2,)),
        _node(name="node_c", referenced_by=(), subscribe_topics=(T_CMD,)),
        _node(name="node_d", needs_overlay=True),
        _node(
            name="node_e",
            needs_overlay=True,
            overlay_row_present=True,
            overlay_required_keys=("a", "b"),
            overlay_row_keys=("a",),
        ),
        _node(name="node_f", sync_io_hits=("src/h.py: open\\(",)),
        _node(name="node_g", ledger_reader_hits=("src/g.py: LEDGER",)),
        _node(name="node_topic_replay_effect", publish_topics=(T_R,)),
        _node(name="node_r1", subscribe_topics=(T_R,)),
        _node(name="node_r2", subscribe_topics=(T_R,)),
    )


def _golden_imports() -> tuple[ModelSeamImportFact, ...]:
    return (
        _import(importer_path="src/pkg_a/nodes/node_a/handlers/h_a.py"),
        _import(importer_path="src/pkg_a/nodes/node_b/handlers/h_b.py"),
        _import(
            importer_path="src/pkg_a/nodes/node_c/handlers/h_c.py", has_seam_test=True
        ),
    )


def test_golden_all_nine_classes() -> None:
    report = derive_seam_defects(_facts(_golden_nodes(), _golden_imports()), _rule())

    assert [group.defect_class for group in report.groups] == list(EnumSeamDefectClass)
    expected: dict[EnumSeamDefectClass, tuple[int, str]] = {
        EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER: (1, T1),
        EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER: (1, T2),
        EnumSeamDefectClass.NODE_NO_CALLER: (1, "node_c"),
        EnumSeamDefectClass.NODE_OVERLAY_ROW_MISSING: (1, "node_d"),
        EnumSeamDefectClass.NODE_OVERLAY_KEYS_MISSING: (1, "node_e"),
        EnumSeamDefectClass.CROSS_REPO_IMPORT_NO_SEAM_TEST: (1, "pkg_other.mod"),
        EnumSeamDefectClass.SYNC_IO_HANDLER: (1, "node_f"),
        EnumSeamDefectClass.FILE_LEDGER_READER: (1, "node_g"),
        EnumSeamDefectClass.SHARED_TOPIC_REPLAY: (1, "node_topic_replay_effect"),
    }
    for group in report.groups:
        count, subject = expected[group.defect_class]
        assert group.count == count, group.defect_class
        assert [d.subject for d in group.first] == [subject], group.defect_class
        assert group.first[0].repo == "repo_a", group.defect_class

    assert report.counts == ModelSeamGraphCounts(
        nodes=10,
        topics=4,
        publish_edges=2,
        subscribe_edges=4,
        overlay_edges=1,
        caller_edges=9,
        import_edges=3,
    )
    assert report.contracts_read == 10
    assert report.contracts_unreadable == ()


_FactsBuilder = Callable[[], ModelSeamGraphFacts]

_CONTROL_CASES: tuple[tuple[EnumSeamDefectClass, _FactsBuilder, _FactsBuilder], ...] = (
    (
        EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER,
        lambda: _facts((_node(subscribe_topics=(T1,)),)),
        lambda: _facts(
            (
                _node(subscribe_topics=(T1,)),
                _node(name="node_publisher", publish_topics=(T1,)),
            )
        ),
    ),
    (
        EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER,
        lambda: _facts((_node(publish_topics=(T2,)),)),
        lambda: _facts(
            (
                _node(publish_topics=(T2,)),
                _node(name="node_subscriber", subscribe_topics=(T2,)),
            )
        ),
    ),
    (
        EnumSeamDefectClass.NODE_NO_CALLER,
        lambda: _facts((_node(referenced_by=()),)),
        lambda: _facts((_node(),)),
    ),
    (
        EnumSeamDefectClass.NODE_OVERLAY_ROW_MISSING,
        lambda: _facts((_node(needs_overlay=True),)),
        lambda: _facts((_node(needs_overlay=True, overlay_row_present=True),)),
    ),
    (
        EnumSeamDefectClass.NODE_OVERLAY_KEYS_MISSING,
        lambda: _facts(
            (
                _node(
                    needs_overlay=True,
                    overlay_row_present=True,
                    overlay_required_keys=("a", "b"),
                    overlay_row_keys=("a",),
                ),
            )
        ),
        lambda: _facts(
            (
                _node(
                    needs_overlay=True,
                    overlay_row_present=True,
                    overlay_required_keys=("a", "b"),
                    overlay_row_keys=("a", "b"),
                ),
            )
        ),
    ),
    (
        EnumSeamDefectClass.CROSS_REPO_IMPORT_NO_SEAM_TEST,
        lambda: _facts((_node(),), (_import(),)),
        lambda: _facts((_node(),), (_import(has_seam_test=True),)),
    ),
    (
        EnumSeamDefectClass.SYNC_IO_HANDLER,
        lambda: _facts((_node(sync_io_hits=("src/h.py: open\\(",)),)),
        lambda: _facts((_node(),)),
    ),
    (
        EnumSeamDefectClass.FILE_LEDGER_READER,
        lambda: _facts((_node(ledger_reader_hits=("src/g.py: LEDGER",)),)),
        lambda: _facts((_node(),)),
    ),
    (
        EnumSeamDefectClass.SHARED_TOPIC_REPLAY,
        lambda: _facts(
            (
                _node(name="node_replay_x", publish_topics=(T_R,)),
                _node(name="node_reader_a", subscribe_topics=(T_R,)),
                _node(name="node_reader_b", subscribe_topics=(T_R,)),
            )
        ),
        lambda: _facts(
            (
                _node(name="node_replay_x", publish_topics=(T_R,)),
                _node(name="node_reader_a", subscribe_topics=(T_R,)),
            )
        ),
    ),
)


@pytest.mark.parametrize(
    ("defect_class", "bad", "fixed"),
    _CONTROL_CASES,
    ids=[case[0].value for case in _CONTROL_CASES],
)
def test_failing_control_clears_the_cause(
    defect_class: EnumSeamDefectClass, bad: _FactsBuilder, fixed: _FactsBuilder
) -> None:
    assert _group(derive_seam_defects(bad(), _rule()), defect_class).count == 1
    assert _group(derive_seam_defects(fixed(), _rule()), defect_class).count == 0


def test_command_topic_never_flagged_by_topic_classes() -> None:
    facts = _facts(
        (
            _node(subscribe_topics=(T_CMD,)),
            _node(name="node_dispatch", publish_topics=("onex.cmd.t9.v1",)),
        )
    )
    report = derive_seam_defects(facts, _rule())

    assert _group(report, EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER).count == 0
    assert _group(report, EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER).count == 0


def test_external_declarations_clear_topic_defects() -> None:
    facts = _facts(
        (
            _node(subscribe_topics=(T1,), external_produced_topics=(T1,)),
            _node(publish_topics=(T2,), external_consumed_topics=(T2,)),
        )
    )
    report = derive_seam_defects(facts, _rule())

    assert _group(report, EnumSeamDefectClass.TOPIC_SUBSCRIBED_NO_PUBLISHER).count == 0
    assert _group(report, EnumSeamDefectClass.TOPIC_PUBLISHED_NO_SUBSCRIBER).count == 0


def test_first_n_truncates_and_count_is_total() -> None:
    nodes = tuple(
        _node(name=f"node_h{i}", sync_io_hits=(f"src/h{i}.py: open\\(",))
        for i in range(1, 6)
    )
    report = derive_seam_defects(_facts(nodes), _rule())
    group = _group(report, EnumSeamDefectClass.SYNC_IO_HANDLER)

    assert group.count == 5
    assert len(group.first) == 2
    assert [d.subject for d in group.first] == ["node_h1", "node_h2"]


def test_report_is_deterministic_in_input_order() -> None:
    report = derive_seam_defects(_facts(_golden_nodes(), _golden_imports()), _rule())
    reversed_report = derive_seam_defects(
        _facts(tuple(reversed(_golden_nodes())), tuple(reversed(_golden_imports()))),
        _rule(),
    )

    assert reversed_report == report


def test_error_chain_vacuous_graph_refuses() -> None:
    with pytest.raises(ValueError, match="vacuous seam graph"):
        derive_seam_defects(_facts(()), _rule())


def test_error_chain_invalid_command_pattern_refuses() -> None:
    with pytest.raises(ValueError, match="is not a valid regex"):
        derive_seam_defects(_facts((_node(),)), _rule(command_topic_pattern="("))


def test_error_chain_handler_refuses_repo_without_checkout() -> None:
    request = ModelSeamGraphRequest(rule=_rule(), repo_roots={})

    with pytest.raises(ValueError, match="repo_a"):
        HandlerSeamGraphDefects().handle(request)


def test_error_chain_extra_field_refused() -> None:
    with pytest.raises(ValidationError):
        ModelSeamNodeFact(**_node().model_dump(), surprise=1)


def test_shape_example_overlay_validates() -> None:
    path = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_seam_graph_defects_compute/overlay.example.yaml"
    )
    rule = ModelSeamGraphRule.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )

    assert rule.overlay_repo == "overlays"
    assert [repo.name for repo in rule.repos] == ["repo_a"]
