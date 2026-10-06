# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate: every live walker path of an enrolled workflow has a chain that still holds.

The walker runs over ``src`` now, so a contract change that adds, removes or
reroutes a path shows here. Each committed chain is re-driven through the real
handlers and checked with the chain assertion helper. To regenerate after an
intended change, run this file with ``CHAIN_GEN_WRITE=1`` and commit the diff.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from enum import Enum
from functools import cache
from pathlib import Path

import pytest

from omnimarket.nodes.node_event_chain_generator_compute.handlers.handler_event_chain_generator import (
    HandlerEventChainGenerator,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_generation import (
    ModelChainExpectationSet,
    ModelChainGateRequest,
    ModelChainObligation,
    ModelGeneratedChain,
)
from tests.chains.chain_assert import assert_chain, assert_error_chain
from tests.chains.generated.driver_delegation import DRIVERS

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
H = HandlerEventChainGenerator()


def expectation_path(owner: str) -> Path:
    return HERE / f"{owner}.chains.json"


@cache
def walker_report() -> dict[str, object]:
    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"chain-gen-walk-{os.getpid()}.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "omnibase_core.validation.validator_contract_walker",
            str(REPO / "src"),
            "--json-out",
            str(out),
        ],
        check=True,
        capture_output=True,
        cwd=REPO,
    )
    loaded: dict[str, object] = json.loads(out.read_text(encoding="utf-8"))
    return loaded


def obligations(owner: str) -> tuple[ModelChainObligation, ...]:
    return H.obligations(walker_report(), owner)


def committed(owner: str) -> ModelChainExpectationSet | None:
    path = expectation_path(owner)
    if not path.exists():
        return None
    return ModelChainExpectationSet.model_validate_json(
        path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("owner", sorted(DRIVERS))
async def test_every_walker_path_has_a_chain_that_still_holds(owner: str) -> None:
    generated = await H.generate(obligations(owner), DRIVERS[owner])
    if os.environ.get("CHAIN_GEN_WRITE") == "1":
        expectation_path(owner).write_text(
            json.dumps(generated.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    result = H.gate(
        ModelChainGateRequest(
            workflow_owner=owner,
            obligations=obligations(owner),
            committed=committed(owner),
            generated=generated,
        )
    )
    assert result.passed, result.model_dump_json(indent=2)


def _chain_cases() -> list[tuple[str, ModelGeneratedChain]]:
    cases = []
    for owner in sorted(DRIVERS):
        expectations = committed(owner)
        for chain in expectations.chains if expectations else ():
            cases.append((owner, chain))
    return cases


def _typed(terminal: object, fields: dict[str, object]) -> dict[str, object]:
    typed: dict[str, object] = {}
    for name, value in fields.items():
        actual = getattr(terminal, name, None)
        typed[name] = type(actual)(value) if isinstance(actual, Enum) else value
    return typed


@pytest.mark.parametrize(
    ("owner", "chain"), _chain_cases(), ids=lambda v: getattr(v, "path_id", v)
)
async def test_generated_chain_passes_the_chain_assertion_helper(
    owner: str, chain: ModelGeneratedChain
) -> None:
    obligation = next(o for o in obligations(owner) if o.path_id == chain.path_id)
    cid, run = await DRIVERS[owner].run(obligation)
    assert tuple(state.name for state in run.states) == chain.expected_states
    check = assert_error_chain if chain.kind == "error" else assert_chain
    check(
        run.events,
        expected_event_types=chain.expected_event_types,
        terminal_fields=_typed(run.events[-1].payload, chain.terminal_fields),
        correlation_id=cid,
        bus_history_count=run.bus_history_count,
    )
