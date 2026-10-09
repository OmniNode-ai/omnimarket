# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR claim registry effect matches the registry module it replaces, case for case."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_claim_registry_effect.handlers import (
    HandlerPrClaimRegistry,
    canonical_pr_key,
    filesystem_key,
    is_active,
)
from omnimarket.nodes.node_pr_claim_registry_effect.models import (
    EnumPrClaimOperation,
    ModelPrClaim,
    ModelPrClaimRegistryRequest,
)

pytestmark = pytest.mark.unit

GOLDEN: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "pr_claim_registry_golden.json").read_text()
)
NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_claim_registry_effect"
)


def _write(path: Path, content: object) -> None:
    path.write_text(content if isinstance(content, str) else json.dumps(content))


def _parsed(path: Path) -> object:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _present(claim: ModelPrClaim) -> dict[str, object]:
    return {k: v for k, v in claim.model_dump().items() if v is not None}


def _as_recorded(claim: dict[str, Any]) -> dict[str, object]:
    return {k: v for k, v in claim.items() if v is not None}


def _request(
    case: dict[str, Any], claims: Path, instance: Path
) -> ModelPrClaimRegistryRequest:
    recorded = dict(case["request"])
    return ModelPrClaimRegistryRequest(
        claims_dir=str(claims),
        instance_id_path=str(instance),
        now=GOLDEN["now"],
        host=GOLDEN["host"],
        **recorded,
    )


def _seed(case: dict[str, Any], claims: Path) -> dict[str, object]:
    seed: dict[str, str] = {}
    if GOLDEN["states"][case["state"]] is not None:
        seed[GOLDEN["target_file"]] = GOLDEN["states"][case["state"]]
    if case["others"]:
        seed.update(GOLDEN["others"])
    for name, text in seed.items():
        _write(claims / name, text)
    return {name: _parsed(claims / name) for name in seed}


@pytest.mark.parametrize(
    "case",
    GOLDEN["cases"],
    ids=[f"case-{i}" for i in range(len(GOLDEN["cases"]))],
)
def test_matches_the_registry_golden(case: dict[str, Any], tmp_path: Path) -> None:
    claims = tmp_path / "claims"
    claims.mkdir()
    instance = tmp_path / "instance_id"
    instance.write_text(GOLDEN["instance_id"])
    seeded = _seed(case, claims)

    result = HandlerPrClaimRegistry().handle(_request(case, claims, instance))

    expected = case["expected"]
    assert result.succeeded == expected["succeeded"]
    # The module scanned in filesystem order and the node scans in name order,
    # so the golden holds a cleanup's lines sorted.
    reported = (
        sorted(result.messages)
        if case["request"]["operation"] == "cleanup_stale_own_claims"
        else list(result.messages)
    )
    assert reported == expected["messages"]
    assert sorted(result.pr_keys) == expected["pr_keys"]
    assert [
        _present(c) for c in sorted(result.claims, key=lambda c: c.pr_key or "")
    ] == [_as_recorded(c) for c in expected["claims"]]
    if expected["claim"] is None:
        assert result.claim is None
    else:
        assert result.claim is not None
        assert _present(result.claim) == _as_recorded(expected["claim"])

    after = {p.name: _parsed(p) for p in sorted(claims.iterdir())}
    assert not [name for name in after if name.startswith(".")], (
        "temp or tomb file left"
    )
    changed = {n: v for n, v in after.items() if seeded.get(n, "<absent>") != v}
    assert changed == expected["changed"]
    assert sorted(set(seeded) - set(after)) == expected["removed"]


def test_golden_covers_every_operation_and_both_outcomes() -> None:
    cases = GOLDEN["cases"]
    assert len(cases) == 841
    seen = {(c["request"]["operation"], c["expected"]["succeeded"]) for c in cases}
    for operation in EnumPrClaimOperation:
        assert (operation.value, True) in seen, operation
    assert {
        "acquire",
        "release",
        "heartbeat",
        "has_active",
    } <= {op for op, ok in seen if not ok}
    assert any(c["expected"]["messages"] for c in cases)
    assert any(c["state"] == "malformed-json" for c in cases)
    assert any(c["state"] == "expired-r1-laneA" for c in cases)


def test_acquire_resolves_the_host_when_the_request_names_none(tmp_path: Path) -> None:
    claims = tmp_path / "claims"
    instance = tmp_path / "instance_id"
    result = HandlerPrClaimRegistry().handle(
        ModelPrClaimRegistryRequest(
            operation=EnumPrClaimOperation.ACQUIRE,
            claims_dir=str(claims),
            instance_id_path=str(instance),
            now="2026-10-09T12:00:00Z",
            pr_key="omninode-ai/omniclaude#1",
            run_id="R1",
            action="merge",
        )
    )
    assert result.succeeded
    stored = json.loads((claims / "omninode-ai--omniclaude--1.json").read_text())
    assert stored["claimed_by_host"]
    assert stored["claimed_by_instance_id"] == instance.read_text()


def test_an_acquire_without_an_instance_id_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="claimed_by_instance_id"):
        HandlerPrClaimRegistry().handle(
            ModelPrClaimRegistryRequest(
                operation=EnumPrClaimOperation.ACQUIRE,
                claims_dir=str(tmp_path),
                now="2026-10-09T12:00:00Z",
                host="h",
                pr_key="omninode-ai/omniclaude#1",
                run_id="R1",
                action="merge",
            )
        )


def test_the_key_helpers_match_the_registry() -> None:
    assert (
        canonical_pr_key("OmniNode-ai", "OmniClaude", 247)
        == "omninode-ai/omniclaude#247"
    )
    assert (
        filesystem_key("omninode-ai/omniclaude#247") == "omninode-ai--omniclaude--247"
    )


def test_expiry_needs_both_a_stale_heartbeat_and_an_old_claim() -> None:
    now = "2026-10-09T12:00:00Z"
    both = {
        "claimed_at": "2026-10-09T09:00:00Z",
        "last_heartbeat_at": "2026-10-09T11:00:00Z",
    }
    stale_only = {
        "claimed_at": "2026-10-09T11:30:00Z",
        "last_heartbeat_at": "2026-10-09T11:00:00Z",
    }
    assert not is_active(both, now)
    assert is_active(stale_only, now)
    assert not is_active({"pr_key": "k"}, now)


@pytest.mark.parametrize(
    "updates",
    [{"unknown_field": "x"}, {"operation": "steal"}],
)
def test_a_malformed_request_is_refused_at_the_model(
    updates: dict[str, object],
) -> None:
    base: dict[str, object] = {
        "operation": "get_claim",
        "claims_dir": "/tmp/claims",
        "now": "2026-10-09T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        ModelPrClaimRegistryRequest.model_validate(base | updates)


def test_contract_declares_its_topics_and_the_definition_b_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.pr-claim-registry-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-claim-registry-completed.v1"
    ]
    assert contract["terminal_event"] == contract["event_bus"]["publish_topics"][0]
    handler = contract["handler"]
    assert handler["class"] == "HandlerPrClaimRegistry"
    assert handler["input_model"] == contract["input_model"]


@pytest.mark.parametrize(
    "bad_key",
    [
        "../../etc/passwd#1",
        "a/../b#1",
        "../x#1",
        "org/repo",
        "org/repo#1/..",
        "",
        "./.#1",
    ],
)
def test_filesystem_key_rejects_keys_that_could_escape_the_directory(
    bad_key: str,
) -> None:
    with pytest.raises(ValueError, match="invalid PR key"):
        filesystem_key(bad_key)
