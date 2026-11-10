# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-22956): a request the node cannot read as asked is refused by name, an install that
fails reads NOT WORKING, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same error
assertions; the mutated chain must raise AssertionError.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_install_reading_compute.handlers import (
    handler_install_reading as module,
)
from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallReadingRequest,
)
from tests.nodes.node_install_reading_compute.install_support import (
    HOST_A,
    HOST_B,
    ROOT_DATA,
    good,
    probe,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "blind-drill-accepted": (
        "error",
        "test_mutation_blind_drill_accepted_turns_the_error_chain_red",
    ),
    "empty-file-list-accepted": (
        "error",
        "test_mutation_empty_file_list_accepted_turns_the_error_chain_red",
    ),
    "missing-file-ignored": (
        "error",
        "test_mutation_missing_file_ignored_turns_the_error_chain_red",
    ),
    "missing-host-ignored": (
        "error",
        "test_mutation_missing_host_ignored_turns_the_error_chain_red",
    ),
    "probe-age-ignored": (
        "error",
        "test_mutation_probe_age_ignored_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    """Each broken request ends NOT WORKING (or is refused by name); the unreadable one ends UNKNOWN."""
    handler = resolve_handler()()
    broken: dict[str, dict[str, Any]] = {
        "missing-file": {
            "probes": [probe(HOST_A, missing=(ROOT_DATA,)), probe(HOST_B)]
        },
        "drill-deleted-root-data-file": {
            "probes": [
                probe(HOST_A),
                probe(HOST_B, trigger="drill", fault=ROOT_DATA, missing=(ROOT_DATA,)),
            ]
        },
        "blind-drill": {
            "probes": [probe(HOST_A), probe(HOST_B, trigger="drill", fault=ROOT_DATA)]
        },
        "empty-file-list": {"probes": [probe(HOST_A), probe(HOST_B, expected=0)]},
        "host-without-probe": {"probes": [probe(HOST_A)]},
        "probe-stale": {
            "probes": [probe(HOST_A), probe(HOST_B, probed_at="2026-11-07T00:00:00Z")]
        },
        "probe-future-dated": {
            "probes": [probe(HOST_A), probe(HOST_B, probed_at="2026-11-09T12:00:01Z")]
        },
    }
    for name, edits in broken.items():
        reading = handler.handle(request(**edits))
        assert reading.verdict is V.NOT_WORKING, name
        assert reading.reasons, name
    unreadable = handler.handle(
        request(probes=[], probes_unread_reason="canary-record-missing")
    )
    assert unreadable.verdict is V.UNKNOWN
    with pytest.raises(ValidationError, match="names the file it deleted"):
        request(probes=[probe(HOST_A, trigger="drill")])


def test_error_chain_a_well_formed_request_is_accepted() -> None:
    """Positive control: the refused shapes differ from this one in one field each."""
    parsed = ModelInstallReadingRequest.model_validate(good())
    assert resolve_handler()().handle(parsed).verdict is V.WORKING


def test_error_chain_broken_requests_end_not_working_or_refused() -> None:
    run_error_chain()


def _drop_failures(real: Callable[..., Any], *prefixes: str) -> Callable[..., Any]:
    def mutated(*args: Any) -> Any:
        return [f for f in real(*args) if not f.startswith(prefixes)]

    return mutated


def test_mutation_missing_file_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "install-missing")
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_blind_drill_accepted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "judge_probe",
        _drop_failures(module.judge_probe, "drill-fault-unseen"),
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_empty_file_list_accepted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "file-list-empty")
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_probe_age_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_probe", _drop_failures(module.judge_probe, "probe-stale")
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_missing_host_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.newest_probes

    def filled(req: ModelInstallReadingRequest) -> Any:
        held = real(req)
        template = next(iter(held.values()))
        return {h: held.get(h, template) for h in req.required_hosts}

    monkeypatch.setattr(module, "newest_probes", filled)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"error"}
