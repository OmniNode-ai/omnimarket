# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven deterministic sampling without customer or holdout leakage."""

import hashlib
import importlib
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_eval_sample_compute.handlers import (
    handler_delegation_eval_sample as sampler,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_gate_outcome import (
    EnumDelegationEvalGateOutcome,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_candidate import (
    ModelDelegationEvalCandidate,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_key import (
    ModelDelegationEvalKey,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sample_request import (
    ModelDelegationEvalSampleRequest,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sampling_config import (
    ModelDelegationEvalSamplingConfig,
)

pytestmark = pytest.mark.unit


def _load_sampling_config() -> ModelDelegationEvalSamplingConfig:
    contract_path = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_delegation_eval_sample_compute/contract.yaml"
    )
    config = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["config"]
    return ModelDelegationEvalSamplingConfig.model_validate(
        {
            **config,
            "quotas": tuple(
                {"name": name, "count": count}
                for name, count in sorted(config["quotas"].items())
            ),
        }
    )


def _candidates(
    count: int, *, holdout: bool = False
) -> tuple[ModelDelegationEvalCandidate, ...]:
    rows = []
    index = 0
    while len(rows) < count:
        correlation_id = f"correlation-{index}"
        index += 1
        if (
            int(hashlib.sha256(correlation_id.encode()).hexdigest(), 16) % 10 == 0
        ) != holdout:
            continue
        rows.append(
            ModelDelegationEvalCandidate(
                correlation_id=correlation_id,
                attempt_index=0,
                tenant_id="house",
                task_class="summarization",
                gate_outcome=EnumDelegationEvalGateOutcome.ACCEPTED,
            )
        )
    return tuple(rows)


def _request(
    candidates: tuple[ModelDelegationEvalCandidate, ...],
    imported_keys: tuple[ModelDelegationEvalKey, ...] = (),
    *,
    sampling: ModelDelegationEvalSamplingConfig | None = None,
) -> ModelDelegationEvalSampleRequest:
    return ModelDelegationEvalSampleRequest(
        seed="fixed-seed",
        window_start="2026-09-01T00:00:00Z",
        window_end="2026-09-28T00:00:00Z",
        query_text="SELECT committed identifiers",
        house_tenant_id="house",
        sampling=_load_sampling_config() if sampling is None else sampling,
        candidates=candidates,
        imported_keys=imported_keys,
    )


def _key(row: ModelDelegationEvalCandidate) -> ModelDelegationEvalKey:
    return ModelDelegationEvalKey(
        correlation_id=row.correlation_id, attempt_index=row.attempt_index
    )


def _order(row: ModelDelegationEvalCandidate) -> tuple[str, str, int]:
    return (
        hashlib.sha256(
            f"fixed-seed{row.correlation_id}{row.attempt_index}".encode()
        ).hexdigest(),
        row.correlation_id,
        row.attempt_index,
    )


def test_same_seed_same_manifest() -> None:
    request = _request(_candidates(130))
    first = sampler.HandlerDelegationEvalSample().handle(request)
    assert first == sampler.HandlerDelegationEvalSample().handle(request)
    assert [item.key for item in first.items] == [
        _key(row) for row in sorted(request.candidates, key=_order)[:100]
    ]
    assert first.seed == request.seed
    assert first.window_start == request.window_start
    assert first.window_end == request.window_end
    assert first.query_text == request.query_text
    canonical = {
        "seed": first.seed,
        "window_start": first.window_start,
        "window_end": first.window_end,
        "query_text": first.query_text,
        "quotas": [row.model_dump(mode="json") for row in first.quotas],
        "item_keys": [item.key.model_dump(mode="json") for item in first.items],
    }
    assert (
        first.manifest_id
        == hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    for field in ("seed", "window_start", "window_end", "query_text"):
        changed = request.model_copy(
            update={field: getattr(request, field) + "changed"}
        )
        assert (
            sampler.HandlerDelegationEvalSample().handle(changed).manifest_id
            != first.manifest_id
        )


def test_same_seed_same_manifest_with_reordered_quotas() -> None:
    request = _request(_candidates(130))
    reordered = request.model_copy(
        update={
            "sampling": request.sampling.model_copy(
                update={"quotas": tuple(reversed(request.sampling.quotas))}
            )
        }
    )
    handler = sampler.HandlerDelegationEvalSample()
    first = handler.handle(request)
    second = handler.handle(reordered)
    assert first.items == second.items
    assert first.manifest_id == second.manifest_id
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("count", [3, 110])
def test_same_seed_same_manifest_with_colliding_stratum_labels(count: int) -> None:
    # Both classes display as code_generation/accepted/refused, but they have
    # different gate outcomes and must retain their own quotas and shortfalls.
    rows = _candidates(count)
    candidates = tuple(
        row.model_copy(
            update={"task_class": "code_generation", "deciding_path": "refused"}
        )
        for row in rows
    ) + tuple(
        row.model_copy(
            update={
                "task_class": "code_generation/accepted",
                "gate_outcome": EnumDelegationEvalGateOutcome.REFUSED,
                "attempt_index": 1,
            }
        )
        for row in rows
    )
    handler = sampler.HandlerDelegationEvalSample()
    request = _request(candidates)
    first = handler.handle(request)
    second = handler.handle(
        request.model_copy(update={"candidates": tuple(reversed(candidates))})
    )

    assert first.model_dump_json() == second.model_dump_json()
    assert Counter(item.task_class for item in first.items) == {
        "code_generation": min(count, 100),
        "code_generation/accepted": min(count, 60),
    }
    if count == 3:
        assert {item.key for item in first.items} == set(map(_key, candidates))
        assert [(row.quota, row.available, row.taken) for row in first.shortfalls] == [
            (100, 3, 3),
            (60, 3, 3),
        ]
    else:
        assert first.shortfalls == ()


def test_holdout_bucket_never_drawn() -> None:
    blocked = _candidates(3, holdout=True)
    result = sampler.HandlerDelegationEvalSample().handle(
        _request(blocked, tuple(map(_key, blocked)))
    )
    assert result.items == ()
    assert result.shortfalls == ()
    assert result.excluded_holdout_bucket == 3
    assert {row.reason for row in result.rejected_imports} == {"holdout_bucket"}
    assert {row.key for row in result.rejected_imports} == set(map(_key, blocked))


def test_customer_tenant_never_drawn() -> None:
    blocked = tuple(
        row.model_copy(update={"tenant_id": "customer"}) for row in _candidates(3)
    )
    result = sampler.HandlerDelegationEvalSample().handle(
        _request(blocked, tuple(map(_key, blocked)))
    )
    assert result.items == ()
    assert result.excluded_customer_tenant == 3
    assert {row.reason for row in result.rejected_imports} == {"customer_tenant"}


def test_short_stratum_reported() -> None:
    result = sampler.HandlerDelegationEvalSample().handle(_request(_candidates(4)))
    assert len(result.items) == 4
    assert [row.model_dump() for row in result.shortfalls] == [
        {"stratum": "summarization/accepted", "quota": 100, "available": 4, "taken": 4}
    ]
    empty = sampler.HandlerDelegationEvalSample().handle(_request(()))
    assert empty.items == empty.shortfalls == ()


def test_imported_labelled_id_kept() -> None:
    candidates = _candidates(130)
    last = sorted(candidates, key=_order)[-1]
    unknown = ModelDelegationEvalKey(correlation_id="missing", attempt_index=99)
    result = sampler.HandlerDelegationEvalSample().handle(
        _request(candidates, (_key(last), _key(last), unknown))
    )
    assert len(result.items) == 100
    assert len({item.key for item in result.items}) == 100
    assert [item.key for item in result.items if item.source == "imported"] == [
        _key(last)
    ]
    assert {item.key for item in result.items if item.source == "drawn"} == {
        _key(row) for row in sorted(candidates, key=_order)[:99]
    }
    assert [(row.key, row.reason) for row in result.rejected_imports] == [
        (unknown, "unknown_key")
    ]


def test_quotas_read_from_contract() -> None:
    config = _load_sampling_config()
    assert {row.name: row.count for row in config.quotas} == {
        "accepted": 100,
        "refused": 60,
        "undetermined": 30,
    }
    assert config.holdout_buckets == 10
    assert config.reserved_bucket == 0
    assert config.order_key == "sha256(seed + correlation_id + attempt_index)"
    quotas = {"accepted": 2, "refused": 3, "undetermined": 1}
    sampling = ModelDelegationEvalSamplingConfig.model_validate(
        config.model_dump()
        | {"quotas": [{"name": name, "count": count} for name, count in quotas.items()]}
    )
    candidates = tuple(
        row.model_copy(update={"gate_outcome": outcome, "attempt_index": index})
        for index, outcome in enumerate(EnumDelegationEvalGateOutcome)
        for row in _candidates(5)
    )
    result = sampler.HandlerDelegationEvalSample().handle(
        _request(candidates, sampling=sampling)
    )
    assert Counter(item.stratum for item in result.items) == {
        "summarization/accepted": 2,
        "summarization/refused": 3,
        "summarization/undetermined": 1,
    }
    assert {row.name: row.count for row in result.quotas} == quotas
    assert result.shortfalls == ()


def test_code_stratum_by_deciding_path() -> None:
    paths = ("execution", "deterministic_checks_only", "no_target_refusal", None)
    candidates = tuple(
        row.model_copy(
            update={
                "task_class": "code_generation",
                "deciding_path": path,
                "attempt_index": index,
            }
        )
        for index, path in enumerate(paths)
        for row in _candidates(101)
    )
    result = sampler.HandlerDelegationEvalSample().handle(_request(candidates))
    assert Counter(item.stratum for item in result.items) == {
        f"code_generation/accepted/{path or 'none'}": 100 for path in paths
    }
    assert result.shortfalls == ()
    noncode = tuple(
        row.model_copy(update={"task_class": "summarization"}) for row in candidates
    )
    assert (
        len(sampler.HandlerDelegationEvalSample().handle(_request(noncode)).items)
        == 100
    )


def test_order_independent_of_input_order() -> None:
    candidates = _candidates(130) + _candidates(2, holdout=True)
    imports = (
        *map(_key, candidates[-5:]),
        ModelDelegationEvalKey(correlation_id="missing", attempt_index=0),
    )
    handler = sampler.HandlerDelegationEvalSample()
    first = handler.handle(_request(candidates, imports))
    assert first == handler.handle(
        _request(tuple(reversed(candidates)), tuple(reversed(imports)))
    )
    assert first == handler.handle(_request(candidates + candidates, imports + imports))


def test_conflicting_duplicate_keys_rejected_independent_of_order() -> None:
    candidate = _candidates(1)[0]
    customer = candidate.model_copy(update={"tenant_id": "customer"})
    for candidates in ((candidate, customer), (customer, candidate)):
        with pytest.raises(ValueError, match="Conflicting candidate"):
            sampler.HandlerDelegationEvalSample().handle(
                _request(candidates, (_key(candidate),))
            )


def test_imports_over_quota_follow_seed_order() -> None:
    candidates = _candidates(130)
    result = sampler.HandlerDelegationEvalSample().handle(
        _request(candidates, tuple(map(_key, reversed(candidates))))
    )
    assert [item.key for item in result.items] == [
        _key(row) for row in sorted(candidates, key=_order)[:100]
    ]
    assert {item.source for item in result.items} == {"imported"}


def test_handle_performs_no_file_io(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(_candidates(1))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("handler attempted file I/O")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    importlib.reload(sampler)
    assert sampler.HandlerDelegationEvalSample().handle(request).items


def test_models_frozen_and_forbid_content() -> None:
    candidate = _candidates(1)[0]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelDelegationEvalCandidate.model_validate(
            candidate.model_dump() | {"prompt": "content"}
        )
    with pytest.raises(ValidationError, match="frozen_instance"):
        candidate.tenant_id = "customer"
