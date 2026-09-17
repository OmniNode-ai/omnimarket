# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing-fit gate must resolve a model the way the DISPATCHER does (OMN-18568).

RED-first. Before the fix, ``routing_input_model_fit`` read ``entry["input_model"]`` and
skipped every entry that did not declare one. Measured over ``src/omnimarket`` at the
commit this test was written against: 403 contracts, **19** entries declaring entry-level
``input_model``, **163** declaring ``event_model``, **373** declaring neither. The gate
was blind to 163 of the 182 declarations it exists to check and resolved no fallback at
all, so it reported ``OK: 403 contracts scanned`` while
``node_swarm_fanout_orchestrator`` dead-lettered 120 escalation events in 21 minutes on
the ``.201`` stability lane.

The two tests that matter here are about RESOLUTION, not about the rule. The rule ("one
model may not serve two message categories") was already right. What was wrong was the
answer to "which model does this entry actually hand the handler?", and the only correct
answer is the one the runtime computes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.validators.routing_input_model_fit import (
    PEER_FENCED_CONTRACTS,
    effective_entry_model,
    findings_for_contract_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "src" / "omnimarket"
_SWARM_CONTRACT = (
    _SCAN_ROOT / "nodes" / "node_swarm_fanout_orchestrator" / "contract.yaml"
)

_SWARM_HANDLER = {
    "name": "HandlerSwarmFanout",
    "module": (
        "omnimarket.nodes.node_swarm_fanout_orchestrator.handlers.handler_swarm_fanout"
    ),
}

_ESCALATION_TOPICS = (
    "onex.evt.omnimarket.delegation-call-completed.v1",
    "onex.evt.omnimarket.delegation-escalation-triggered.v1",
    "onex.evt.omnimarket.delegation-all-tiers-failed.v1",
)


def _pre_fix_swarm_contract() -> dict[str, object]:
    """The contract exactly as it stood when the stability lane dead-lettered.

    Written out here rather than read from git so the fixture cannot drift with the
    file it is pinning: this is the shape the gate MUST call a finding, forever.
    """
    return {
        "name": "node_swarm_fanout_orchestrator",
        "input_model": {
            "name": "ModelSwarmFanoutRequest",
            "module": (
                "omnimarket.nodes.node_swarm_fanout_orchestrator.models"
                ".model_swarm_fanout_request"
            ),
        },
        "handler": dict(_SWARM_HANDLER),
        "handler_routing": {
            "routing_strategy": "topic_match",
            "handlers": [
                {
                    "topic": "onex.cmd.omnimarket.swarm-fanout.v1",
                    "message_category": "command",
                    "operation": "fanout",
                    "event_model": {
                        "name": "ModelSwarmFanoutRequest",
                        "module": (
                            "omnimarket.nodes.node_swarm_fanout_orchestrator.models"
                            ".model_swarm_fanout_request"
                        ),
                    },
                    "handler": dict(_SWARM_HANDLER),
                },
                *(
                    {
                        "topic": topic,
                        "message_category": "event",
                        "operation": "fanout",
                        "handler": dict(_SWARM_HANDLER),
                    }
                    for topic in _ESCALATION_TOPICS
                ),
            ],
        },
    }


@pytest.mark.unit
def test_pre_fix_swarm_fanout_contract_is_a_finding(tmp_path: Path) -> None:
    """AC2. The contract that dead-lettered 120 events must be reported as a finding.

    The three ``.evt.`` entries declare no ``event_model``, so the runtime coerces each
    event into ``HandlerSwarmFanout.handle``'s own annotation -- the start-COMMAND model
    -- and validation fails before the handler is entered. One model, two categories.
    """
    node_dir = tmp_path / "nodes" / "node_swarm_fanout_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(_pre_fix_swarm_contract()))

    findings, scanned = findings_for_contract_tree(tmp_path)

    assert scanned == 1
    assert len(findings) == 1, (
        f"expected one finding, got {[f.render() for f in findings]}"
    )
    finding = findings[0]
    assert finding.handler == "HandlerSwarmFanout"
    assert finding.effective_model == "ModelSwarmFanoutRequest"
    assert finding.categories == ("command", "event")
    for topic in _ESCALATION_TOPICS:
        assert topic in finding.topics, f"{topic} missing from {finding.topics}"


@pytest.mark.unit
def test_effective_model_mirrors_the_runtime_dispatcher() -> None:
    """AC3. Pin the gate's resolution against the dispatcher's own helpers.

    ``_make_dispatch_callback`` resolves an entry that declares no ``event_model`` from
    the handler's ``handle()`` annotation, NOT from the contract-level ``input_model``.
    If this gate ever disagrees with that, it is answering a different question than the
    one that decides whether a message survives -- so the mirror is asserted against the
    runtime functions themselves rather than restated.
    """
    import importlib

    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _handler_declares_typed_event_envelope,
        _resolve_def_b_input_model_type,
    )

    handler_cls = getattr(
        importlib.import_module(_SWARM_HANDLER["module"]), _SWARM_HANDLER["name"]
    )
    handle = handler_cls.handle

    # The runtime's own answer for this handler, computed here, not asserted from memory.
    assert not _handler_declares_typed_event_envelope(handle)
    runtime_target = _resolve_def_b_input_model_type(handle)
    assert runtime_target is not None
    assert runtime_target.__name__ == "ModelSwarmFanoutRequest"

    untyped_entry = {"topic": _ESCALATION_TOPICS[0], "handler": dict(_SWARM_HANDLER)}
    assert effective_entry_model(untyped_entry) == runtime_target.__name__

    # A declared event_model always wins over the signature fallback.
    typed_entry = {
        "topic": _ESCALATION_TOPICS[0],
        "handler": dict(_SWARM_HANDLER),
        "event_model": {"name": "ModelSomethingElse", "module": "does.not.matter"},
    }
    assert effective_entry_model(typed_entry) == "ModelSomethingElse"


@pytest.mark.unit
@pytest.mark.parametrize(
    "node_name",
    [
        # handle(self, envelope: ModelEventEnvelope[Any]) -- materialized, never coerced.
        "node_redeploy_orchestrator",
        # handle(self, request: A | B | Mapping) -- a union is not a concrete BaseModel.
        "node_readiness_gate_orchestrator",
        # handle(self, input_data: dict[str, object]) -- no model to coerce into.
        "node_evidence_dashboard_effect",
        "node_projection_live_events",
        # async handle(self, *args: Any, ...) -- var-positional, no annotation to read.
        "node_pr_lifecycle_state_reducer",
    ],
)
def test_envelope_and_untyped_handlers_are_not_findings(node_name: str) -> None:
    """AC5 (labelled criterion). The fallback is the SIGNATURE, not the contract model.

    Resolving an untyped entry to the contract-level ``input_model`` instead reports all
    five of these as offenders -- including ``node_redeploy_orchestrator``, which sits on
    the prod-promotion path and is CORRECT: its handler takes an envelope and branches on
    ``event_type``. A gate that reports five false positives to find one defect is a
    broken probe, not a finding.
    """
    contract = _SCAN_ROOT / "nodes" / node_name / "contract.yaml"
    assert contract.is_file(), f"{contract} moved; update this pin"

    findings, scanned = findings_for_contract_tree(contract.parent)

    assert scanned == 1
    assert findings == (), f"false positive: {[f.render() for f in findings]}"


@pytest.mark.unit
def test_repo_scan_reports_only_the_peer_fenced_contract() -> None:
    """AC4. Over the whole tree the gate is clean apart from its one pinned row.

    ``node_content_ingestion_effect`` carries the same class, but its ``.evt.`` topic is
    its DECLARED primary input (``subscribe_topic_metadata`` names the crawler's
    ``ModelContentDiscoveredEvent``) and its ``.cmd.`` topic is its declared
    ``runtime_dispatch.command_topic``. Both entries are legitimate; the handler is what
    has to change, which is a rearchitecture owned by OMN-14613. Pinned, not exempted:
    the gate fails if this contract becomes clean and the pin is left behind.
    """
    findings, scanned = findings_for_contract_tree(_SCAN_ROOT)

    assert scanned > 300, f"collapsed discovery: only {scanned} contracts scanned"
    unpinned = [f for f in findings if f.peer_fenced_key is None]
    assert unpinned == [], f"unpinned findings: {[f.render() for f in unpinned]}"

    pinned = {f.peer_fenced_key for f in findings if f.peer_fenced_key}
    assert pinned == set(PEER_FENCED_CONTRACTS), (
        "the peer-fenced pin and the live findings disagree; a pin whose contract is "
        f"clean must be deleted. pinned={sorted(PEER_FENCED_CONTRACTS)} live={sorted(pinned)}"
    )


@pytest.mark.unit
def test_fixed_swarm_fanout_contract_declares_no_unservable_subscription() -> None:
    """AC1. The three event topics are gone from BOTH blocks of the live contract."""
    document = yaml.safe_load(_SWARM_CONTRACT.read_text())

    subscribed = document["event_bus"]["subscribe_topics"]
    routed = [e.get("topic") for e in document["handler_routing"]["handlers"]]

    assert "onex.cmd.omnimarket.swarm-fanout.v1" in subscribed
    assert "onex.cmd.omnimarket.swarm-fanout.v1" in routed
    for topic in _ESCALATION_TOPICS:
        assert topic not in subscribed, f"{topic} still a durable subscription"
        assert topic not in routed, (
            f"{topic} still routed to a handler that cannot serve it"
        )


@pytest.mark.unit
def test_swarm_handler_still_polls_the_same_three_completion_topics() -> None:
    """AC1's other half. Removing the subscriptions must not empty the inline poll.

    ``completion_topics`` used to be a filter over ``event_bus.subscribe_topics``, so
    deleting the three topics there would have turned a loud validation failure into a
    silent collection failure. They are now resolved from the node that PUBLISHES them.
    """
    from omnimarket.nodes.node_swarm_fanout_orchestrator.handlers.handler_swarm_fanout import (
        resolve_completion_topics,
    )

    assert set(resolve_completion_topics()) == set(_ESCALATION_TOPICS)


@pytest.mark.unit
def test_stale_pin_failure_text_names_the_ticket_to_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale pin must tell the reader which ticket to close, not just which file.

    A pin only stops being a silent exemption if the message a reader gets when it goes
    stale is actionable on its own. Naming the file says what changed; naming the ticket
    says what to do about it.
    """
    from omnimarket.validators.routing_input_model_fit import main, peer_fence_key_for

    pin_key = next(iter(PEER_FENCED_CONTRACTS))
    node_dir = tmp_path / Path(pin_key).parent
    node_dir.mkdir(parents=True)
    # A contract that is CLEAN under the rule: one entry, so no category conflict.
    (node_dir / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "node_content_ingestion_effect",
                "handler_routing": {
                    "handlers": [
                        {
                            "topic": "onex.cmd.omnimarket.content-ingestion-start.v1",
                            "message_category": "command",
                            "handler": dict(_SWARM_HANDLER),
                        }
                    ]
                },
            }
        )
    )
    assert peer_fence_key_for(node_dir / "contract.yaml") == pin_key

    assert main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "OMN-14613" in err, err
    assert "delete the pin and close" in err, err
