# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each effect operation runs through its binding (OMN-20677)."""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.ledger_reconcile import (
    ModelAppendRowsRequest,
    ModelPlannedAppend,
    ModelPrRef,
    ModelReadSourcesRequest,
    ModelReconcileWanted,
    ModelShaRef,
    ModelVerifyEvidenceRequest,
)

from .fakes import NOW, FakeAppender, FakeClock, FakeGit, FakeGitHub, FakeHost

NAME = "node_ledger_reconcile_effect"
OPERATIONS = (
    "read_ledger_reconcile_sources",
    "verify_ledger_reconcile_evidence",
    "append_ledger_reconcile_rows",
)


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _resolve(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    types = []
    for key in ("input_model", "output_model"):
        module, _, name = str(entry[key]).rpartition(".")
        types.append(getattr(importlib.import_module(module), name))
    return handler_type, types[0], types[1]


def test_contract_declares_the_bus_route_and_effect_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["descriptor"]["node_archetype"] == "effect"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.ledger-reconcile-effect-requested.v1"
    )
    assert set(contract["runtime_dispatch"]["terminal_events"].values()) == {
        "onex.evt.omnimarket.ledger-reconcile-effect-completed.v1",
        "onex.evt.omnimarket.ledger-reconcile-effect-failed.v1",
    }
    # Hosted on the ledger host by a serve process; a shared runtime must never attach it.
    assert "event_bus" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert tuple(e["operation"] for e in routing["handlers"]) == OPERATIONS


def test_every_binding_resolves_to_a_definition_b_handler() -> None:
    for entry in _contract()["handler_routing"]["handlers"]:
        handler_type, request_type, result_type = _resolve(entry)
        assert callable(handler_type.handle), entry["operation"]
        assert asyncio.iscoroutinefunction(handler_type.handle), entry["operation"]
        assert request_type.model_config["frozen"]
        assert result_type.model_config["frozen"]


def test_the_three_operations_run_through_the_bindings() -> None:
    by_op = {
        e["operation"]: _resolve(e) for e in _contract()["handler_routing"]["handlers"]
    }
    host = FakeHost(
        ["2026-09-22T10:00:00Z | CLAIM | lane=a-lane | ticket=OMN-1 | scope=x"]
    )

    read_type, read_request, read_result = by_op["read_ledger_reconcile_sources"]
    read = asyncio.run(
        read_type(host=host, clock=FakeClock()).handle(
            ModelReadSourcesRequest(live_lanes=("zeta", "alpha"))
        )
    )
    assert isinstance(read, read_result)
    assert read_request is ModelReadSourcesRequest
    assert read.error == ""
    assert read.sources is not None
    assert read.sources.live.name == "ledger.md"
    assert read.sources.registry_name == "registry_root"
    assert read.sources.live_lanes == ("alpha", "zeta")
    assert read.sources.clones == ("omnimarket",)
    assert read.sources.read_at == NOW

    verify_type, verify_request, verify_result = by_op[
        "verify_ledger_reconcile_evidence"
    ]
    github = FakeGitHub({11: "2026-09-22T11:00:00Z"})
    git = FakeGit({"abcdef12": ("omnimarket", "2026-09-22T12:00:00+00:00", ("OMN-1",))})
    verified = asyncio.run(
        verify_type(github=github, git=git, host=host).handle(
            ModelVerifyEvidenceRequest(
                wanted=ModelReconcileWanted(
                    prs=(ModelPrRef(repo="omnimarket", number=11),),
                    shas=(
                        ModelShaRef(
                            sha="abcdef12", candidates=("registry_root", "omnimarket")
                        ),
                    ),
                    pushes=(ModelPrRef(repo="omnimarket", number=12),),
                ),
                github_org="Example-Org",
                registry_name="registry_root",
            )
        )
    )
    assert isinstance(verified, verify_result)
    assert verify_request is ModelVerifyEvidenceRequest
    assert verified.error == ""
    assert [(f.number, f.state) for f in verified.facts.prs] == [(11, "MERGED")]
    assert verified.facts.shas[0].found_in == "omnimarket"
    assert verified.facts.shas[0].landed
    assert [(f.number, f.committed_at) for f in verified.facts.pushes] == [(12, "")]
    assert github.lookups == [("Example-Org", "omnimarket", 11)]
    assert git.probes[0].candidates == ("registry_root", "omnimarket")

    append_type, append_request, append_result = by_op["append_ledger_reconcile_rows"]
    appender = FakeAppender(errors={1: "ledger_lock exit 5: busy"})
    appended = asyncio.run(
        append_type(appender=appender, host=host).handle(
            ModelAppendRowsRequest(
                rows=(
                    ModelPlannedAppend(index=0, kind="terminal", row="row zero"),
                    ModelPlannedAppend(index=1, kind="attention", row="row one"),
                    ModelPlannedAppend(index=2, kind="release", row="row two"),
                ),
                ledger_path=str(Path("/elsewhere/ledger.md")),
            )
        )
    )
    assert isinstance(appended, append_result)
    assert append_request is ModelAppendRowsRequest
    assert [(o.index, o.error) for o in appended.outcomes] == [
        (0, ""),
        (1, "ledger_lock exit 5: busy"),
        (2, ""),
    ]
    assert appender.rows == ["row zero", "row one", "row two"]
    assert appender.ledgers == [Path("/elsewhere/ledger.md")] * 3
