# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cost accounting of a compaction: four lines, each with its provenance (OMN-21966)."""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_cost_compute.handlers.handler_compaction_cost import (
    HandlerCompactionCost,
)
from omnimarket.nodes.node_compaction_cost_compute.models.model_compaction_cost import (
    EnumProvenance,
    EnumRatioStatus,
    EnumSafeguard,
    EnumUsageKind,
    ModelArmCost,
    ModelArmInput,
    ModelBoundary,
    ModelCompactionCostRequest,
    ModelCompactionCostResult,
    ModelCostLine,
    ModelRecoveryPattern,
    ModelToolCall,
    ModelTurn,
    ModelUsageRecord,
)
from omnimarket.nodes.node_compaction_cost_compute.node import (
    NodeCompactionCostCompute,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_compaction_cost_compute"
SUMMARY = "x" * 4000  # 1000 estimated output tokens at four characters per token
LEDGER = "docs/tracking/ROLLING_WORK_LEDGER.md"


def skill_call(name: str) -> ModelToolCall:
    return ModelToolCall(
        tool_name="Skill",
        target=name,
        result_input_tokens=100,
        provenance=EnumProvenance.MEASURED,
    )


def read(path: str, tokens: int | None, provenance: str = "measured") -> ModelToolCall:
    return ModelToolCall(
        tool_name="Read",
        target=path,
        result_input_tokens=tokens,
        provenance=EnumProvenance(provenance),
    )


def turns(
    calls: dict[int, list[ModelToolCall]], count: int = 25
) -> tuple[ModelTurn, ...]:
    return tuple(
        ModelTurn(index=i, tool_calls=tuple(calls.get(i, ()))) for i in range(count)
    )


def stock_arm(
    *,
    pre: int | None = 100_000,
    post: int | None = 10_000,
    summary: str | None = SUMMARY,
    records: tuple[ModelUsageRecord, ...] = (),
    calls: dict[int, list[ModelToolCall]] | None = None,
    count: int = 25,
) -> ModelArmInput:
    return ModelArmInput(
        session_id="s-stock",
        boundary=ModelBoundary(uuid="b-1", pre_tokens=pre, post_tokens=post),
        summary_text=summary,
        usage_records=records,
        turns=turns(
            calls
            if calls is not None
            else {
                2: [read(LEDGER, 1000)],
                9: [read(LEDGER, 2000)],
                20: [read(LEDGER, 3000)],
                5: [read("src/app.py", 777)],
            },
            count,
        ),
    )


def mod_arm(
    *,
    records: tuple[ModelUsageRecord, ...] | None = None,
    recovery: int = 3000,
    pre: int | None = 100_000,
    post: int | None = 8_000,
) -> ModelArmInput:
    if records is None:
        records = (
            ModelUsageRecord(
                request_id="pack-1",
                kind=EnumUsageKind.PACK_BUILD,
                input_tokens=20_000,
                output_tokens=2_000,
                source="pack_receipt",
            ),
        )
    return ModelArmInput(
        session_id="s-mod",
        boundary=ModelBoundary(uuid="b-2", pre_tokens=pre, post_tokens=post),
        usage_records=records,
        turns=turns({4: [read(LEDGER, recovery)]}),
    )


def run(request: ModelCompactionCostRequest) -> ModelCompactionCostResult:
    return HandlerCompactionCost().handle(request)


def lines(arm: ModelArmCost) -> list[ModelCostLine]:
    return [
        arm.compaction_input,
        arm.compaction_output,
        arm.compaction,
        arm.post_size,
        arm.recovery,
        arm.net,
        arm.cost,
    ]


def test_boundary_and_25_turns_yield_four_lines_with_provenance() -> None:
    arm = run(ModelCompactionCostRequest(stock=stock_arm())).stock
    assert arm is not None
    assert arm.compaction.value == 100_000 + 1_000
    assert arm.compaction.provenance is EnumProvenance.ESTIMATED
    assert arm.post_size.value == 10_000
    assert arm.post_size.provenance is EnumProvenance.MEASURED
    assert arm.recovery.value == 6_000
    assert arm.recovery.provenance is EnumProvenance.MEASURED
    assert len(arm.counted_calls) == 3
    assert arm.net.value == 90_000
    assert arm.net.provenance is EnumProvenance.MEASURED
    assert arm.cost.value == 101_000 + 6_000
    assert arm.cost.provenance is EnumProvenance.ESTIMATED
    assert arm.window_complete is True


def test_every_cost_line_carries_provenance_and_summarizer_output_is_estimated() -> (
    None
):
    arm = run(ModelCompactionCostRequest(stock=stock_arm())).stock
    assert arm is not None
    for line in lines(arm):
        assert isinstance(line.provenance, EnumProvenance)
        assert line.basis
    assert arm.compaction_output.value == math.ceil(len(SUMMARY) / 4)
    assert arm.compaction_output.provenance is EnumProvenance.ESTIMATED


def test_pretokens_is_an_estimated_proxy_never_measured_summarizer_usage() -> None:
    arm = run(ModelCompactionCostRequest(stock=stock_arm())).stock
    assert arm is not None
    assert arm.compaction_input.value == 100_000
    assert arm.compaction_input.provenance is EnumProvenance.ESTIMATED
    assert "proxy" in arm.compaction_input.basis
    assert all(
        line.provenance is not EnumProvenance.MEASURED
        for line in (arm.compaction_input, arm.compaction_output, arm.compaction)
    )


def test_summarizer_usage_record_replaces_the_proxy_and_is_measured() -> None:
    record = ModelUsageRecord(
        request_id="req-9",
        kind=EnumUsageKind.SUMMARIZER_CALL,
        input_tokens=88_000,
        output_tokens=1_234,
        source="transcript_message_usage",
    )
    arm = run(ModelCompactionCostRequest(stock=stock_arm(records=(record,)))).stock
    assert arm is not None
    assert arm.compaction_input.value == 88_000
    assert arm.compaction_input.provenance is EnumProvenance.MEASURED
    assert arm.compaction_output.value == 1_234
    assert arm.compaction_output.provenance is EnumProvenance.MEASURED
    assert arm.compaction.provenance is EnumProvenance.MEASURED
    assert "transcript_message_usage" in arm.compaction_input.basis


def test_recovery_list_is_data_and_a_read_outside_it_is_not_counted() -> None:
    calls = {
        1: [read(LEDGER, 1000)],
        2: [read("docs/tracking/other_ledger_notes.md", 5000)],
        3: [read("src/ledger.py", 5000)],
        4: [
            ModelToolCall(
                tool_name="Bash",
                target="cat /tmp/ROLLING_WORK_LEDGER.md.bak",
                result_input_tokens=5000,
                provenance=EnumProvenance.MEASURED,
            )
        ],
    }
    arm = run(ModelCompactionCostRequest(stock=stock_arm(calls=calls))).stock
    assert arm is not None
    assert [c.target for c in arm.counted_calls] == [LEDGER]
    assert arm.recovery.value == 1000


def test_default_pattern_list_covers_each_recovery_kind() -> None:
    calls = {
        0: [read(LEDGER, 100)],
        1: [read(".onex_state/checkpoints/checkpoint.json", 100)],
        2: [skill_call("omni:ledger-ask")],
        3: [skill_call("omni:ledger-catchup")],
        4: [skill_call("omni:session-resume")],
        5: [read("docs/handoff/HANDOFF.md", 100)],
        6: [skill_call("omni:way-of-working")],
    }
    arm = run(ModelCompactionCostRequest(stock=stock_arm(calls=calls))).stock
    assert arm is not None
    kinds = {c.kind.value for c in arm.counted_calls}
    assert kinds == {
        "ledger_read",
        "checkpoint_read",
        "ledger_ask",
        "catchup_skill",
        "resume_skill",
        "handoff_read",
    }
    assert arm.recovery.value == 600


def test_request_pattern_list_replaces_the_default() -> None:
    patterns = (
        ModelRecoveryPattern(
            pattern_id="only-src",
            kind="handoff_read",
            tool_names=("Read",),
            target_regex=r"^src/",
        ),
    )
    request = ModelCompactionCostRequest(stock=stock_arm(), recovery_patterns=patterns)
    arm = run(request).stock
    assert arm is not None
    assert [c.pattern_id for c in arm.counted_calls] == ["only-src"]
    assert arm.recovery.value == 777


def test_turns_after_the_window_are_not_counted() -> None:
    calls = {0: [read(LEDGER, 10)], 24: [read(LEDGER, 20)], 25: [read(LEDGER, 4000)]}
    request = ModelCompactionCostRequest(
        stock=stock_arm(calls=calls, count=30), window_turns=25
    )
    arm = run(request).stock
    assert arm is not None
    assert arm.recovery.value == 30


def test_short_window_is_flagged_incomplete() -> None:
    arm = run(ModelCompactionCostRequest(stock=stock_arm(count=10))).stock
    assert arm is not None
    assert arm.window_complete is False
    assert arm.turns_observed == 10


def test_no_value_is_written_to_the_savings_table() -> None:
    table = "savings" + "_estimates"
    writes = re.compile(rf"(?i)(insert|update|upsert|delete)[^;\n]*{table}")
    hits = []
    for base in (NODE, Path(__file__).parent):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".sql"}:
                text = path.read_text()
                if writes.search(text) or (base == NODE and table in text):
                    hits.append(str(path))
    assert hits == []
    handler = (NODE / "handlers/handler_compaction_cost.py").read_text()
    for forbidden in ("psycopg", "asyncpg", "sqlalchemy", "open(", "requests"):
        assert forbidden not in handler


def _mixed(
    safeguard_upper_bound: float | None = None,
) -> ModelCompactionCostRequest:
    return ModelCompactionCostRequest(
        stock=stock_arm(),
        mod=mod_arm(),
        safeguard_upper_bound=safeguard_upper_bound,
    )


def test_ratio_with_estimated_stock_and_measured_mod_is_flagged_estimated() -> None:
    result = run(_mixed(safeguard_upper_bound=0.5))
    ratio = result.ratio
    assert ratio is not None
    assert result.stock is not None
    assert result.mod is not None
    assert result.stock.cost.value == 107_000
    assert result.mod.cost.value == 25_000
    assert ratio.ratio == pytest.approx(25_000 / 107_000)
    assert ratio.status is EnumRatioStatus.ESTIMATED
    assert ratio.perturbation_fraction == 0.5
    assert set(ratio.estimated_lines) == {
        "stock.compaction_input",
        "stock.compaction_output",
    }
    # stock estimated lines halved, measured recovery unchanged
    assert ratio.perturbed_upper_ratio == pytest.approx(25_000 / (50_500 + 6_000))
    assert ratio.safeguard is EnumSafeguard.ESTABLISHED


def test_safeguard_is_not_established_when_the_perturbed_bound_fails() -> None:
    result = run(_mixed(safeguard_upper_bound=0.3))
    ratio = result.ratio
    assert ratio is not None
    assert ratio.ratio == pytest.approx(0.2336, abs=1e-3)
    assert ratio.perturbed_upper_ratio is not None
    assert ratio.ratio is not None
    assert ratio.ratio <= 0.3 < ratio.perturbed_upper_ratio
    assert ratio.safeguard is EnumSafeguard.NOT_ESTABLISHED


def test_estimated_mod_lines_are_inflated_in_the_unfavourable_direction() -> None:
    mod = mod_arm()
    call = ModelToolCall(
        tool_name="Read",
        target=LEDGER,
        result_input_tokens=4000,
        provenance=EnumProvenance.ESTIMATED,
    )
    mod = mod.model_copy(update={"turns": turns({1: [call]})})
    result = run(ModelCompactionCostRequest(stock=stock_arm(), mod=mod))
    ratio = result.ratio
    assert ratio is not None
    assert ratio.status is EnumRatioStatus.ESTIMATED
    assert "mod.recovery" in ratio.estimated_lines
    assert ratio.perturbed_upper_ratio == pytest.approx(
        (22_000 + 6_000) / (50_500 + 6_000)
    )


def test_all_measured_ratio_is_measured_and_unperturbed() -> None:
    record = ModelUsageRecord(
        request_id="req-9",
        kind=EnumUsageKind.SUMMARIZER_CALL,
        input_tokens=80_000,
        output_tokens=2_000,
        source="transcript_message_usage",
    )
    request = ModelCompactionCostRequest(
        stock=stock_arm(records=(record,)), mod=mod_arm(), safeguard_upper_bound=0.5
    )
    ratio = run(request).ratio
    assert ratio is not None
    assert ratio.status is EnumRatioStatus.MEASURED
    assert ratio.estimated_lines == ()
    assert ratio.perturbed_upper_ratio == pytest.approx(ratio.ratio)
    assert ratio.safeguard is EnumSafeguard.ESTABLISHED


def test_no_bound_means_no_safeguard_verdict() -> None:
    ratio = run(_mixed()).ratio
    assert ratio is not None
    assert ratio.safeguard is None
    assert ratio.safeguard_upper_bound is None


def test_missing_pack_build_record_is_unavailable_not_zero() -> None:
    arm = run(ModelCompactionCostRequest(mod=mod_arm(records=()))).mod
    assert arm is not None
    for line in (arm.compaction_input, arm.compaction_output, arm.compaction, arm.cost):
        assert line.provenance is EnumProvenance.UNAVAILABLE
        assert line.value is None
    assert arm.post_size.value == 8_000
    assert arm.recovery.value == 3_000


def test_record_without_token_counts_is_unavailable_not_zero() -> None:
    pack = ModelUsageRecord(
        request_id="pack-1",
        kind=EnumUsageKind.PACK_BUILD,
        input_tokens=20_000,
        output_tokens=2_000,
        source="pack_receipt",
    )
    routed = ModelUsageRecord(
        request_id="route-1",
        kind=EnumUsageKind.ROUTED_CALL,
        input_tokens=None,
        output_tokens=None,
        source="hook_event",
    )
    arm = run(ModelCompactionCostRequest(mod=mod_arm(records=(pack, routed)))).mod
    assert arm is not None
    assert arm.compaction.value is None
    assert arm.compaction.provenance is EnumProvenance.UNAVAILABLE
    assert arm.cost.value is None


def test_mod_compaction_sums_pack_build_and_routed_calls_as_measured() -> None:
    pack = ModelUsageRecord(
        request_id="pack-1",
        kind=EnumUsageKind.PACK_BUILD,
        input_tokens=20_000,
        output_tokens=2_000,
        source="pack_receipt",
    )
    routed = ModelUsageRecord(
        request_id="route-1",
        kind=EnumUsageKind.ROUTED_CALL,
        input_tokens=500,
        output_tokens=50,
        source="hook_event",
    )
    arm = run(ModelCompactionCostRequest(mod=mod_arm(records=(pack, routed)))).mod
    assert arm is not None
    assert arm.compaction_input.value == 20_500
    assert arm.compaction_output.value == 2_050
    assert arm.compaction.value == 22_550
    assert arm.compaction.provenance is EnumProvenance.MEASURED
    assert "pack_receipt" in arm.compaction_input.basis
    assert "hook_event" in arm.compaction_input.basis


def test_stock_with_nothing_to_go_on_is_unavailable() -> None:
    arm = run(
        ModelCompactionCostRequest(stock=stock_arm(pre=None, post=None, summary=None))
    ).stock
    assert arm is not None
    for line in (
        arm.compaction_input,
        arm.compaction_output,
        arm.post_size,
        arm.net,
        arm.cost,
    ):
        assert line.provenance is EnumProvenance.UNAVAILABLE
        assert line.value is None


def test_recovery_call_without_a_count_makes_recovery_unavailable() -> None:
    calls = {1: [read(LEDGER, 1000)], 2: [read(LEDGER, None)]}
    arm = run(ModelCompactionCostRequest(stock=stock_arm(calls=calls))).stock
    assert arm is not None
    assert arm.recovery.provenance is EnumProvenance.UNAVAILABLE
    assert arm.recovery.value is None
    assert arm.cost.value is None


def test_no_observed_turns_make_recovery_unavailable_not_zero() -> None:
    arm = run(ModelCompactionCostRequest(stock=stock_arm(count=0))).stock
    assert arm is not None
    assert arm.recovery.provenance is EnumProvenance.UNAVAILABLE


def test_ratio_is_unavailable_when_a_cost_is_unavailable() -> None:
    request = ModelCompactionCostRequest(
        stock=stock_arm(), mod=mod_arm(records=()), safeguard_upper_bound=0.5
    )
    ratio = run(request).ratio
    assert ratio is not None
    assert ratio.status is EnumRatioStatus.UNAVAILABLE
    assert ratio.ratio is None
    assert ratio.safeguard is None


def test_ratio_is_unavailable_when_stock_cost_is_zero() -> None:
    record = ModelUsageRecord(
        request_id="r",
        kind=EnumUsageKind.SUMMARIZER_CALL,
        input_tokens=0,
        output_tokens=0,
        source="transcript_message_usage",
    )
    request = ModelCompactionCostRequest(
        stock=stock_arm(records=(record,), calls={}), mod=mod_arm()
    )
    ratio = run(request).ratio
    assert ratio is not None
    assert ratio.status is EnumRatioStatus.UNAVAILABLE
    assert ratio.ratio is None


def test_no_ratio_with_one_arm() -> None:
    assert run(ModelCompactionCostRequest(stock=stock_arm())).ratio is None


def test_estimator_is_injectable() -> None:
    class Flat:
        def estimate_tokens(self, text: str) -> int:
            return 7

    request = ModelCompactionCostRequest(stock=stock_arm())
    arm = HandlerCompactionCost(estimator=Flat()).handle(request).stock
    assert arm is not None
    assert arm.compaction_output.value == 7
    assert arm.compaction_output.provenance is EnumProvenance.ESTIMATED


@pytest.mark.parametrize(
    "build",
    [
        lambda: ModelCompactionCostRequest(),
        lambda: ModelCompactionCostRequest(stock=stock_arm(), window_turns=0),
        lambda: ModelCompactionCostRequest(stock=stock_arm(), safeguard_upper_bound=0),
        lambda: ModelCompactionCostRequest(
            stock=stock_arm(
                records=(
                    ModelUsageRecord(
                        request_id="p",
                        kind=EnumUsageKind.PACK_BUILD,
                        input_tokens=1,
                        output_tokens=1,
                        source="pack_receipt",
                    ),
                )
            )
        ),
        lambda: ModelCompactionCostRequest(
            mod=mod_arm(
                records=(
                    ModelUsageRecord(
                        request_id="s",
                        kind=EnumUsageKind.SUMMARIZER_CALL,
                        input_tokens=1,
                        output_tokens=1,
                        source="transcript_message_usage",
                    ),
                )
            )
        ),
        lambda: ModelCompactionCostRequest(
            mod=mod_arm(
                records=(
                    ModelUsageRecord(
                        request_id="dup",
                        kind=EnumUsageKind.PACK_BUILD,
                        input_tokens=1,
                        output_tokens=1,
                        source="pack_receipt",
                    ),
                    ModelUsageRecord(
                        request_id="dup",
                        kind=EnumUsageKind.ROUTED_CALL,
                        input_tokens=1,
                        output_tokens=1,
                        source="hook_event",
                    ),
                )
            )
        ),
        lambda: ModelRecoveryPattern(
            pattern_id="bad", kind="ledger_read", tool_names=("Read",), target_regex="("
        ),
        lambda: ModelToolCall(
            tool_name="Read",
            result_input_tokens=1,
            provenance=EnumProvenance.UNAVAILABLE,
        ),
    ],
)
def test_invalid_requests_are_refused(build: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        build()


def test_node_contract_is_offline_pure_and_registered() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["side_effects"] == []
    assert contract["runtime_profiles"] == []
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    handler = contract["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    assert issubclass(NodeCompactionCostCompute, NodeCompute)
    assert any(e.name == NODE.name for e in entry_points(group="onex.nodes"))
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text())
    assert metadata["capabilities"]["requires_network"] is False
    assert metadata["capabilities"]["side_effect_class"] == "none"


def test_handler_source_has_no_topic_literals() -> None:
    for path in (NODE / "handlers").glob("*.py"):
        assert "onex.cmd" not in path.read_text()
        assert "onex.evt" not in path.read_text()
