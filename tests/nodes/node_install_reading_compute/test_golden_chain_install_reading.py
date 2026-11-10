# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-22956): the packaged contract resolves to the handler, a good request reads
WORKING with the topics the contract declares, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden
assertions; the mutated chain must raise AssertionError, which shows the assertion can fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_install_reading_compute.handlers import (
    handler_install_reading as module,
)
from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallReading,
    ModelInstallReadingRequest,
)
from tests.nodes.node_install_reading_compute.install_support import (
    HOST_A,
    HOST_B,
    contract,
    probe,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

#: mutation name -> (the chain it turns red, the test that shows it).
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "newest-probe-ignored": (
        "golden",
        "test_mutation_newest_probe_ignored_turns_the_golden_chain_red",
    ),
    "probes-ignored": (
        "golden",
        "test_mutation_probes_ignored_turns_the_golden_chain_red",
    ),
    "unrequired-host-counted": (
        "golden",
        "test_mutation_unrequired_host_counted_turns_the_golden_chain_red",
    ),
}


def run_golden_chain() -> None:
    """The golden assertions: contract routing, the good request, a newer good probe over an older bad one,
    and a probe of a host nobody requires."""
    bus = contract()["event_bus"]
    assert bus["subscribe_topics"] == ["onex.cmd.omnimarket.install-read-requested.v1"]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.install-read.v1"]
    assert contract()["terminal_event"] in bus["publish_topics"]
    handler = resolve_handler()()
    reading = handler.handle(request())
    assert isinstance(reading, ModelInstallReading)
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.hosts_checked, reading.hosts_whole) == (2, 2)
    repaired = handler.handle(
        request(
            probes=[
                probe(HOST_A, probed_at="2026-11-09T01:00:00Z", missing=("a.json",)),
                probe(HOST_A, probed_at="2026-11-09T09:00:00Z"),
                probe(HOST_B),
            ]
        )
    )
    assert repaired.verdict is V.WORKING
    ignored = handler.handle(
        request(
            probes=[probe(HOST_A), probe(HOST_B), probe("host-z", missing=("a.json",))]
        )
    )
    assert ignored.verdict is V.WORKING
    assert ignored.reasons == ()


def test_contract_declares_a_pure_compute_node_with_one_operation() -> None:
    declared = contract()
    assert declared["name"] == "node_install_reading_compute"
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["side_effects"] == []
    [entry] = declared["handler_routing"]["handlers"]
    assert declared["handler_routing"]["routing_strategy"] == "operation_match"
    assert entry["operation"] == "install_reading"
    assert resolve_handler() is module.HandlerInstallReading
    assert (
        declared["handler"]["input_model"].rpartition(".")[2]
        == ModelInstallReadingRequest.__name__
    )


def test_golden_chain_good_request_reads_working_through_the_contract_handler() -> None:
    run_golden_chain()


def test_mutation_probes_ignored_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "newest_probes", lambda _req: {})
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_newest_probe_ignored_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def oldest(req: ModelInstallReadingRequest) -> Any:
        held: dict[str, Any] = {}
        for p in sorted(req.probes, key=lambda p: p.probed_at):
            held.setdefault(p.host, p)
        return {h: p for h, p in held.items() if h in req.required_hosts}

    monkeypatch.setattr(module, "newest_probes", oldest)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_unrequired_host_counted_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.HandlerInstallReading.handle

    def counted(self: Any, req: ModelInstallReadingRequest) -> Any:
        widened = req.model_copy(
            update={
                "required_hosts": tuple(
                    dict.fromkeys((*req.required_hosts, *(p.host for p in req.probes)))
                )
            }
        )
        return real(self, widened)

    monkeypatch.setattr(module.HandlerInstallReading, "handle", counted)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden"}
