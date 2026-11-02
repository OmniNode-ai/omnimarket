# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction verdict compute: synthetic corpora with known truths."""

from __future__ import annotations

import importlib
import random
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_compaction_verdict_compute.handlers.handler_compaction_verdict import (
    HandlerCompactionVerdict,
    HandlerLiveBlockInterval,
)
from omnimarket.nodes.node_compaction_verdict_compute.handlers.joint_power import (
    simulate_joint_power,
)
from omnimarket.nodes.node_compaction_verdict_compute.handlers.thresholds_loader import (
    load_thresholds,
)
from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CompactionThresholdsError,
    CompactionVerdictRefusedError,
    EnumCompactionVerdict,
    EnumRunLabel,
    EnumSafeguardStatus,
    ModelArmCell,
    ModelCompactionVerdictRequest,
    ModelLiveBlock,
    ModelLiveIntervalRequest,
    ModelLiveSession,
    ModelMetricCount,
    ModelPairedBoundary,
)

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "thresholds.yaml"
_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_compaction_verdict_compute"
)

_STOCK = {
    "ledger_recall": 0.40,
    "acted_share": 0.40,
    "contradiction": 0.05,
    "planted_recall": 0.60,
}


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


def _corpus(
    *,
    sessions: int,
    mod: dict[str, float],
    stock: dict[str, float],
    seed: int,
    boundaries: int = 4,
    items: int = 5,
    session_sd: float = 0.05,
    fallbacks: int = 0,
) -> list[ModelPairedBoundary]:
    rng = random.Random(seed)
    cells: list[ModelPairedBoundary] = []
    for s in range(sessions):
        shift = rng.gauss(0.0, session_sd)
        for b in range(boundaries):
            mod_metrics: dict[str, ModelMetricCount] = {}
            stock_metrics: dict[str, ModelMetricCount] = {}
            for metric in stock:
                mod_hits = sum(
                    rng.random() < _clip(mod[metric] + shift) for _ in range(items)
                )
                stock_hits = sum(
                    rng.random() < _clip(stock[metric] + shift) for _ in range(items)
                )
                mod_metrics[metric] = ModelMetricCount(successes=mod_hits, trials=items)
                stock_metrics[metric] = ModelMetricCount(
                    successes=stock_hits, trials=items
                )
            index = s * boundaries + b
            cells.append(
                ModelPairedBoundary(
                    session_id=f"s{s:03d}",
                    boundary_id=f"s{s:03d}-b{b}",
                    mod=ModelArmCell(
                        metrics=mod_metrics,
                        cost=1.1,
                        fell_back=index < fallbacks,
                    ),
                    stock=ModelArmCell(metrics=stock_metrics, cost=1.0),
                )
            )
    return cells


def _request(
    boundaries: list[ModelPairedBoundary],
    *,
    confirmatory: bool,
    thresholds: Path = _FIXTURE,
) -> ModelCompactionVerdictRequest:
    return ModelCompactionVerdictRequest(
        run_id="synthetic",
        thresholds_path=str(thresholds),
        confirmatory=confirmatory,
        boundaries=tuple(boundaries),
    )


def _helps_corpus() -> list[ModelPairedBoundary]:
    mod = dict(_STOCK, ledger_recall=0.80, acted_share=0.75)
    return _corpus(sessions=40, mod=mod, stock=_STOCK, seed=11)


def _aa_corpus() -> list[ModelPairedBoundary]:
    return _corpus(sessions=60, mod=_STOCK, stock=_STOCK, seed=12)


def _harm_corpus() -> list[ModelPairedBoundary]:
    mod = dict(_STOCK, contradiction=0.50)
    return _corpus(sessions=40, mod=mod, stock=_STOCK, seed=13)


def _small_zero_event_corpus() -> list[ModelPairedBoundary]:
    rates = dict(_STOCK, contradiction=0.0)
    return _corpus(sessions=6, mod=rates, stock=rates, seed=14, session_sd=0.0)


def _write_thresholds(tmp_path: Path, **changes: object) -> Path:
    data = yaml.safe_load(_FIXTURE.read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key)
        else:
            data[key] = value
    path = tmp_path / "thresholds.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# AC1: the four synthetic corpora.


def test_known_gain_returns_helps() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_helps_corpus(), confirmatory=True)
    )
    assert report.verdict is EnumCompactionVerdict.HELPS
    assert report.run_label is EnumRunLabel.CONFIRMATORY
    assert any(p.rejected and p.lower > 0 for p in report.primaries)
    assert all(s.status is EnumSafeguardStatus.PASSED for s in report.safeguards)


def test_aa_corpus_returns_no_difference() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_aa_corpus(), confirmatory=True)
    )
    assert report.verdict is EnumCompactionVerdict.NO_DIFFERENCE
    assert not any(p.rejected for p in report.primaries)


def test_contradiction_excess_returns_harm() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_harm_corpus(), confirmatory=True)
    )
    assert report.verdict is EnumCompactionVerdict.HARM
    contradiction = next(s for s in report.safeguards if s.name == "contradiction")
    assert contradiction.status is EnumSafeguardStatus.HARM
    assert contradiction.lower > contradiction.margin


def test_small_aa_corpus_returns_inconclusive_not_no_difference() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_small_zero_event_corpus(), confirmatory=False)
    )
    assert report.verdict is EnumCompactionVerdict.INCONCLUSIVE
    assert report.run_label is EnumRunLabel.EXPLORATORY


def test_fallback_over_limit_is_hold_first() -> None:
    mod = dict(_STOCK, contradiction=0.50)
    corpus = _corpus(sessions=40, mod=mod, stock=_STOCK, seed=13, fallbacks=40)
    report = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    assert report.verdict is EnumCompactionVerdict.HOLD
    assert report.hold_reason == "MOD_FALLBACK"


# AC2: boundary order is irrelevant and the resample unit is the session.


def test_shuffling_boundary_order_changes_nothing() -> None:
    corpus = _helps_corpus()
    shuffled = list(corpus)
    random.Random(99).shuffle(shuffled)
    first = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    second = HandlerCompactionVerdict().handle(_request(shuffled, confirmatory=False))
    assert first.model_dump() == second.model_dump()


def test_resampling_is_by_session() -> None:
    corpus = _helps_corpus()
    merged: dict[str, ModelPairedBoundary] = {}
    for cell in corpus:
        prior = merged.get(cell.session_id)
        if prior is None:
            merged[cell.session_id] = cell.model_copy(
                update={"boundary_id": cell.session_id}
            )
            continue

        def _sum(a: ModelArmCell, b: ModelArmCell) -> ModelArmCell:
            return ModelArmCell(
                metrics={
                    k: ModelMetricCount(
                        successes=a.metrics[k].successes + b.metrics[k].successes,
                        trials=a.metrics[k].trials + b.metrics[k].trials,
                    )
                    for k in a.metrics
                },
                cost=a.cost + b.cost,
            )

        merged[cell.session_id] = prior.model_copy(
            update={
                "mod": _sum(prior.mod, cell.mod),
                "stock": _sum(prior.stock, cell.stock),
            }
        )
    split = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    whole = HandlerCompactionVerdict().handle(
        _request(list(merged.values()), confirmatory=False)
    )
    assert split.resample_unit == "session"
    assert split.sessions == whole.sessions == 40
    for a, b in zip(split.primaries, whole.primaries, strict=True):
        assert (a.estimate, a.lower, a.upper) == pytest.approx(
            (b.estimate, b.lower, b.upper)
        )
    for sa, sb in zip(split.safeguards, whole.safeguards, strict=True):
        assert (sa.lower, sa.upper) == pytest.approx((sb.lower, sb.upper))


# AC3: joint power simulation and the confirmatory refusal.


def test_report_carries_joint_power_from_the_data_file() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_helps_corpus(), confirmatory=True)
    )
    power = report.power
    assert power.simulations == 30
    assert power.minimum_power == 0.5
    assert power.minimum_sessions == 30
    assert power.sessions == 40
    assert power.models_session_clustering
    assert power.models_boundary_pairing
    assert power.models_holm
    assert set(power.safeguards_modelled) == {
        "contradiction",
        "planted_recall",
        "cost_ratio",
    }
    assert 0.0 <= power.simulated_power <= 1.0
    assert power.simulated_power >= power.minimum_power


def test_power_simulation_models_session_clustering(tmp_path: Path) -> None:
    corpus = _helps_corpus()
    loose = load_thresholds(
        str(
            _write_thresholds(
                tmp_path,
                power_session_sd=0.0,
                power_simulations=40,
                primaries=[
                    {
                        "name": "H1",
                        "metric": "ledger_recall",
                        "power_assumed_gain": 0.05,
                    },
                    {"name": "H2", "metric": "acted_share", "power_assumed_gain": 0.05},
                ],
            )
        )
    )
    clustered = loose.model_copy(update={"power_session_sd": 0.3})
    flat = simulate_joint_power(corpus, loose)
    heavy = simulate_joint_power(corpus, clustered)
    assert flat.session_sd == 0.0
    assert heavy.session_sd == 0.3
    assert heavy.simulated_power + 0.2 < flat.simulated_power


def test_confirmatory_run_below_minimum_sessions_is_refused() -> None:
    corpus = _corpus(
        sessions=10, mod=dict(_STOCK, ledger_recall=0.9), stock=_STOCK, seed=3
    )
    with pytest.raises(CompactionVerdictRefusedError, match="minimum_sessions"):
        HandlerCompactionVerdict().handle(_request(corpus, confirmatory=True))


def test_confirmatory_run_below_minimum_power_is_refused(tmp_path: Path) -> None:
    path = _write_thresholds(
        tmp_path,
        minimum_power=0.999,
        primaries=[
            {"name": "H1", "metric": "ledger_recall", "power_assumed_gain": 0.01},
            {"name": "H2", "metric": "acted_share", "power_assumed_gain": 0.01},
        ],
    )
    with pytest.raises(CompactionVerdictRefusedError, match="minimum_power"):
        HandlerCompactionVerdict().handle(
            _request(_small_power_corpus(), confirmatory=True, thresholds=path)
        )


def _small_power_corpus() -> list[ModelPairedBoundary]:
    return _corpus(sessions=30, mod=_STOCK, stock=_STOCK, seed=21, items=1)


def test_non_confirmatory_run_below_minimum_is_labelled_not_powered() -> None:
    corpus = _corpus(sessions=2, mod=_STOCK, stock=_STOCK, seed=4, boundaries=1)
    path_report = HandlerCompactionVerdict().handle(
        _request(corpus, confirmatory=False)
    )
    assert path_report.run_label in (EnumRunLabel.NOT_POWERED, EnumRunLabel.EXPLORATORY)
    assert path_report.verdict in (
        EnumCompactionVerdict.INCONCLUSIVE,
        EnumCompactionVerdict.HARM,
    )
    assert path_report.power.simulations == 30


def test_missing_threshold_field_is_refused_by_name(tmp_path: Path) -> None:
    path = _write_thresholds(tmp_path, minimum_power=None)
    with pytest.raises(CompactionThresholdsError, match="minimum_power"):
        load_thresholds(str(path))


def test_missing_safeguard_margin_is_refused_by_name(tmp_path: Path) -> None:
    path = _write_thresholds(
        tmp_path,
        safeguards=[
            {
                "name": "contradiction",
                "metric": "contradiction",
                "harm_direction": "higher",
            }
        ],
    )
    with pytest.raises(CompactionThresholdsError, match="margin"):
        load_thresholds(str(path))


# AC4: zero events in both arms.


def test_zero_event_safeguard_is_not_established_when_bound_exceeds_margin() -> None:
    report = HandlerCompactionVerdict().handle(
        _request(_small_zero_event_corpus(), confirmatory=False)
    )
    contradiction = next(s for s in report.safeguards if s.name == "contradiction")
    assert contradiction.zero_event
    assert contradiction.bound_rule == "rule_of_three"
    assert contradiction.upper == pytest.approx(3 / 6)
    assert contradiction.status is EnumSafeguardStatus.NOT_ESTABLISHED
    assert report.verdict is not EnumCompactionVerdict.NO_DIFFERENCE
    assert report.verdict is not EnumCompactionVerdict.HELPS


def test_zero_event_safeguard_passes_when_rule_of_three_is_inside_margin() -> None:
    rates = dict(_STOCK, contradiction=0.0)
    corpus = _corpus(sessions=60, mod=rates, stock=rates, seed=15, session_sd=0.0)
    report = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    contradiction = next(s for s in report.safeguards if s.name == "contradiction")
    assert contradiction.zero_event
    assert contradiction.upper == pytest.approx(3 / 60)
    assert contradiction.status is EnumSafeguardStatus.PASSED


# AC5: the live block-paired interval.


def _live_blocks(
    rng: random.Random, *, blocks: int, delta: float, items: int
) -> list[ModelLiveBlock]:
    out: list[ModelLiveBlock] = []
    for b in range(blocks):
        base = 0.45 + rng.gauss(0.0, 0.08)
        mod_hits = sum(rng.random() < _clip(base + delta) for _ in range(items))
        stock_hits = sum(rng.random() < _clip(base) for _ in range(items))
        out.append(
            ModelLiveBlock(
                block_id=f"blk{b}",
                mod=ModelLiveSession(
                    session_id=f"m{b}",
                    metric=ModelMetricCount(successes=mod_hits, trials=items),
                ),
                stock=ModelLiveSession(
                    session_id=f"s{b}",
                    metric=ModelMetricCount(successes=stock_hits, trials=items),
                ),
            )
        )
    return out


def test_live_block_interval_coverage() -> None:
    rng = random.Random(2026)
    delta = 0.10
    handler = HandlerLiveBlockInterval()
    covered = 0
    experiments = 1000
    for index in range(experiments):
        blocks = _live_blocks(rng, blocks=30, delta=delta, items=10)
        interval = handler.handle(
            ModelLiveIntervalRequest(
                metric="acted_share",
                blocks=tuple(blocks),
                confidence=0.90,
                resamples=300,
                seed=index,
            )
        )
        assert interval.resample_unit == "block"
        covered += interval.low <= delta <= interval.high
    coverage = covered / experiments
    assert 0.85 <= coverage <= 0.95, coverage


def test_live_block_rejects_a_block_with_more_than_one_session_per_arm() -> None:
    with pytest.raises(ValueError, match="mod"):
        ModelLiveBlock.model_validate(
            {
                "block_id": "b",
                "mod": [
                    {"session_id": "a", "metric": {"successes": 1, "trials": 2}},
                    {"session_id": "c", "metric": {"successes": 1, "trials": 2}},
                ],
                "stock": {"session_id": "d", "metric": {"successes": 1, "trials": 2}},
            }
        )


# Display and shape.


def test_per_arm_wilson_is_display_only_and_shared() -> None:
    from omnimarket.handlers import stats_wilson
    from omnimarket.nodes.node_delegation_tier_comparison_compute.handlers import (
        comparison_stats,
    )

    assert comparison_stats.wilson_interval is stats_wilson.wilson_interval
    report = HandlerCompactionVerdict().handle(
        _request(_aa_corpus(), confirmatory=False)
    )
    row = report.arm_display[0]
    assert (row.wilson_low, row.wilson_high) == stats_wilson.wilson_interval(
        row.successes, row.trials
    )
    assert row.display_only


def test_unscored_cell_is_removed_from_both_arms() -> None:
    corpus = _aa_corpus()
    first = corpus[0]
    corpus[0] = first.model_copy(
        update={
            "mod": first.mod.model_copy(
                update={"scored": False, "unscored_reason": "UNSTABLE"}
            )
        }
    )
    report = HandlerCompactionVerdict().handle(_request(corpus, confirmatory=False))
    assert report.boundaries_used == len(corpus) - 1
    removals = {(r.arm, r.reason): r.count for r in report.removals}
    assert removals == {("mod", "UNSTABLE"): 1, ("stock", "matched_removal"): 1}


def test_contract_declares_no_topics_and_resolves_handlers() -> None:
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["descriptor"]["purity"] == "pure"
    for entry in contract["handler_routing"]["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        assert hasattr(module, entry["handler"]["name"])
