# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-22958): a request the node cannot read as asked is refused by name, a source that
fails reads NOT WORKING, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same error
assertions; the mutated chain must raise AssertionError.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReadingRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers import (
    handler_dev_deploy_reading as module,
)
from tests.nodes.node_dev_deploy_reading_compute.dev_deploy_support import (
    SHA_A,
    SHA_B,
    SHA_C,
    good,
    lineage_fact,
    merge,
    probe,
    receipt,
    request,
    resolve_handler,
    rule,
)

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "fail-receipt-accepted-as-pass": (
        "error",
        "test_mutation_fail_receipt_accepted_as_pass_turns_the_error_chain_red",
    ),
    "late-receipt-accepted": (
        "error",
        "test_mutation_late_receipt_accepted_turns_the_error_chain_red",
    ),
    "other-lane-receipt-accepted": (
        "error",
        "test_mutation_other_lane_receipt_accepted_turns_the_error_chain_red",
    ),
    "probe-age-ignored": (
        "error",
        "test_mutation_probe_age_ignored_turns_the_error_chain_red",
    ),
    "probe-sha-ignored": (
        "error",
        "test_mutation_probe_sha_ignored_turns_the_error_chain_red",
    ),
    "probe-missing-ignored": (
        "error",
        "test_mutation_probe_missing_ignored_turns_the_error_chain_red",
    ),
    "covering-receipt-always": (
        "error",
        "test_mutation_covering_receipt_always_turns_the_error_chain_red",
    ),
    "probe-sha-never-fails": (
        "error",
        "test_mutation_probe_sha_never_fails_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    """Each broken request ends NOT WORKING (or is refused by name); the unreadable one ends UNKNOWN."""
    handler = resolve_handler()()
    broken: dict[str, dict[str, Any]] = {
        "no-pass-receipt": {"receipts": [receipt(SHA_B, "2026-11-08T09:20:00Z")]},
        "fail-receipt": {
            "receipts": [
                receipt(SHA_A, "2026-11-08T08:20:00Z", result="FAIL"),
                receipt(SHA_B, "2026-11-08T09:20:00Z"),
            ]
        },
        "late-receipt": {
            "receipts": [
                receipt(SHA_A, "2026-11-08T09:30:00Z"),
                receipt(SHA_B, "2026-11-08T09:20:00Z"),
            ]
        },
        "other-lane-receipt": {
            "receipts": [
                receipt(SHA_A, "2026-11-08T08:20:00Z", lane="lane-b"),
                receipt(SHA_B, "2026-11-08T09:20:00Z"),
            ]
        },
        "probe-missing": {"probes": []},
        "probe-stale": {"probes": [probe(probed_at="2026-11-07T00:00:00Z")]},
        "probe-failed": {"probes": [probe(ok=False)]},
        "probe-sha": {"probes": [probe(expected=SHA_B, observed=SHA_A)]},
    }
    for name, edits in broken.items():
        reading = handler.handle(request(**edits))
        assert reading.verdict is V.NOT_WORKING, name
        assert reading.reasons, name
    unreadable = handler.handle(request(receipts_unread_reason="artifact-api-503"))
    assert unreadable.verdict is V.UNKNOWN
    with pytest.raises(ValidationError, match="not an exact sha"):
        request(merges=[merge("abc", "2026-11-08T08:00:00Z")])


def test_error_chain_a_well_formed_request_is_accepted() -> None:
    """Positive control: the refused shapes differ from this one in one field each."""
    parsed = ModelDevDeployReadingRequest.model_validate(good())
    assert resolve_handler()().handle(parsed).verdict is V.WORKING


def test_error_chain_broken_requests_end_not_working_or_refused() -> None:
    run_error_chain()


def _rewrite_receipts(
    real: Callable[..., Any], change: Callable[[Any], Any]
) -> Callable[..., Any]:
    def mutated(req: ModelDevDeployReadingRequest) -> Any:
        return real(
            req.model_copy(update={"receipts": tuple(change(r) for r in req.receipts)})
        )

    return mutated


def _drop_failures(
    real: Callable[..., Any], prefix: str, index: int
) -> Callable[..., Any]:
    def mutated(req: ModelDevDeployReadingRequest) -> Any:
        out = list(real(req))
        out[index] = [f for f in out[index] if not f.startswith(prefix)]
        return tuple(out)

    return mutated


def test_mutation_fail_receipt_accepted_as_pass_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "judge_receipts",
        _rewrite_receipts(
            module.judge_receipts, lambda r: r.model_copy(update={"result": "PASS"})
        ),
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_late_receipt_accepted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "judge_receipts",
        _drop_failures(module.judge_receipts, "receipt-late", 0),
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_other_lane_receipt_accepted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "judge_receipts",
        _rewrite_receipts(
            module.judge_receipts,
            lambda r: r.model_copy(update={"lane": "lane-a"}),
        ),
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_probe_age_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "probe-stale", 0)
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_probe_sha_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "probe-sha", 0)
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_probe_missing_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "probe-missing", 0)
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def run_lineage_error_chain() -> None:
    """The lineage rule (OMN-23603): a non-descendant is NOT WORKING, an unread pair UNKNOWN, a runtime commit between NOT WORKING."""
    handler = resolve_handler()()
    not_ancestor = handler.handle(
        request(
            merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
            receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
            lineage_rule=rule(receipt_covers_descendant=True),
            lineage=[lineage_fact(SHA_A, SHA_B, relation="not-ancestor")],
        )
    )
    assert not_ancestor.verdict is V.NOT_WORKING
    unread = handler.handle(
        request(
            merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
            receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
            lineage_rule=rule(receipt_covers_descendant=True),
            lineage=[
                lineage_fact(
                    SHA_A, SHA_B, relation="unread", unread_reason="clone-api-503"
                )
            ],
        )
    )
    assert unread.verdict is V.UNKNOWN
    runtime = handler.handle(
        request(
            probes=[probe(expected=SHA_B, observed=SHA_C)],
            lineage_rule=rule(probe_accepts_descendant=True),
            lineage=[lineage_fact(SHA_B, SHA_C, runtime_affecting_between=(SHA_A,))],
        )
    )
    assert runtime.verdict is V.NOT_WORKING


def test_lineage_error_chain_broken_requests_end_not_working_or_unknown() -> None:
    run_lineage_error_chain()


def test_mutation_covering_receipt_always_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_covering_receipt", lambda *_a, **_k: (True, None))
    with pytest.raises(AssertionError):
        run_lineage_error_chain()


def test_mutation_probe_sha_never_fails_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_probe_sha_failures", lambda *_a, **_k: [])
    with pytest.raises(AssertionError):
        run_lineage_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_error_chain()
    run_lineage_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"error"}
