# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Swapping the model behind a local endpoint never turns a delegation probe red (OMN-26628).

Every probe of this repository that sends a task (C13, C14) or reads the customer surface (C17) takes the responder's
context window, capabilities and price from the chain's live catalog, at the time of the check, and runs through the
responder chain. The fixture catalog below holds one entry per local endpoint; each test swaps the model behind each
endpoint (a vLLM-shaped listing, a llama.cpp-shaped listing, a larger and a smaller window, another capability set) and
shows each probe grades PASS with the swapped model's own window, capabilities and price on its record. A swap to a model
that declares no window reads NOT_MEASURED, never green.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderReading,
    reading_from_models_body,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
    InMemoryCustomerMachine,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from tests.nodes.node_delegation_check_probe_effect.test_delegation_check_probe_node import (
    good_delegation,
    good_routing,
    good_surface,
)

ROOT = Path(__file__).resolve().parents[2].parent
ENDPOINT_A = "http://127.0.0.11:8111"
ENDPOINT_B = "http://127.0.0.12:8000"
LOCAL_ENDPOINTS = (ENDPOINT_A, ENDPOINT_B)
CHAIN = (
    ModelDelegationCheckResponder(tier="local", base_url=ENDPOINT_A),
    ModelDelegationCheckResponder(tier="local", base_url=ENDPOINT_B),
    ModelDelegationCheckResponder(tier="openrouter"),
    ModelDelegationCheckResponder(tier="claude_api"),
)
TASK_AND_SURFACE_CHECKS: tuple[CheckId, ...] = ("C13", "C14", "C17")


class LiveCatalogReader:
    """The chain's live catalog as a test double: what each endpoint answers at the time of the check.

    ``serving`` is mutable on purpose: a test swaps the model behind an endpoint between two checks, exactly as the lab
    swaps a model on a server, and the next ``read`` answers with the new model.
    """

    def __init__(self, serving: Mapping[str, ModelResponderReading]) -> None:
        self.serving = dict(serving)
        self.reads: list[str] = []

    def read(self, base_url: str) -> ModelResponderReading:
        self.reads.append(base_url)
        return self.serving.get(
            base_url, ModelResponderReading(base_url=base_url, answered=False)
        )


def served(
    base: str,
    model: str,
    *,
    window: int | None,
    shape: str,
    capabilities: dict[str, str] | None = None,
    price: Decimal | None = Decimal(0),
) -> ModelResponderReading:
    kwargs: dict[str, Any] = {
        "base_url": base,
        "answered": True,
        "served_model": model,
        "capabilities": capabilities or {},
        "input_usd_per_million": price,
        "output_usd_per_million": price,
    }
    kwargs["max_model_len" if shape == "vllm" else "n_ctx"] = window
    return ModelResponderReading(**kwargs)


# Models a lab host can serve behind an endpoint; each is a synthetic listing entry, none is a recorded one.
SWAPS = {
    "vllm-large": {
        "model": "model-vllm-large",
        "window": 262144,
        "shape": "vllm",
        "capabilities": {"Tools": "yes", "Reason": "yes"},
    },
    "vllm-small": {
        "model": "model-vllm-small",
        "window": 4096,
        "shape": "vllm",
        "capabilities": {"Tools": "no"},
    },
    "llamacpp-mid": {
        "model": "model-llamacpp-mid",
        "window": 131072,
        "shape": "llamacpp",
        "capabilities": {"Tools": "yes", "Vision": "no"},
    },
    "llamacpp-tiny": {
        "model": "model-llamacpp-tiny",
        "window": 2048,
        "shape": "llamacpp",
        "capabilities": {},
    },
}


def fixture_catalog() -> dict[str, ModelResponderReading]:
    return {
        ENDPOINT_A: served(
            ENDPOINT_A,
            "model-before-swap-a",
            window=65536,
            shape="vllm",
            capabilities={"Tools": "yes"},
        ),
        ENDPOINT_B: served(
            ENDPOINT_B,
            "model-before-swap-b",
            window=131072,
            shape="llamacpp",
            capabilities={"Tools": "yes"},
        ),
    }


def probe(
    reader: LiveCatalogReader, machine: InMemoryCustomerMachine
) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=CHAIN,
        reader=reader,
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
    )


def machine_for(base: str, model: str) -> InMemoryCustomerMachine:
    return InMemoryCustomerMachine(
        delegation=good_delegation(base, model),
        routing=good_routing(),
        surface=good_surface(),
    )


def run(
    handler: HandlerDelegationCheckProbe, check: CheckId
) -> Any:  # ModelDelegationCheckResult
    return handler.handle(
        ModelDelegationCheckRequest(correlation_id=f"swap-{check}", check_id=check)
    )


@pytest.mark.parametrize("check", list(TASK_AND_SURFACE_CHECKS))
@pytest.mark.parametrize("swap", sorted(SWAPS))
@pytest.mark.parametrize("endpoint", LOCAL_ENDPOINTS)
def test_delegation_probe_model_swap_leaves_every_probe_green(
    endpoint: str, swap: str, check: CheckId
) -> None:
    """Swap the model behind one local endpoint; the probe still grades PASS on that model's own catalog values."""
    catalog = fixture_catalog()
    reader = LiveCatalogReader(catalog)
    # The other endpoint goes down so the swapped endpoint is the one the chain must choose.
    other = ENDPOINT_B if endpoint == ENDPOINT_A else ENDPOINT_A
    reader.serving.pop(other)
    spec = dict(SWAPS[swap])
    swapped = served(endpoint, **spec)  # type: ignore[arg-type]
    reader.serving[endpoint] = swapped
    machine = machine_for(endpoint, swapped.served_model or "")
    result = run(probe(reader, machine), check)

    assert result.verdict is EnumCheckVerdict.PASS, [
        (c.name, c.detail) for c in result.clauses if not c.ok
    ]
    assert result.chosen_responder == endpoint
    assert result.served_model == spec["model"]
    # Window, capabilities and price are the swapped model's own, read from the catalog at check time.
    assert result.window == spec["window"]
    assert result.capabilities == spec["capabilities"]
    assert result.input_usd_per_million == Decimal(0)
    assert result.output_usd_per_million == Decimal(0)
    # The probe ran through the chain: it read the catalog and recorded the chain's attempts.
    assert endpoint in reader.reads
    assert any(a.outcome == "chosen" for a in result.chain)


@pytest.mark.parametrize("check", list(TASK_AND_SURFACE_CHECKS))
def test_delegation_probe_model_swap_between_two_checks_is_read_each_time(
    check: CheckId,
) -> None:
    """The window is read when the check runs: the same handler, the model swapped between two runs, two readings."""
    reader = LiveCatalogReader(fixture_catalog())
    reader.serving.pop(ENDPOINT_B)
    handler = probe(reader, machine_for(ENDPOINT_A, "model-before-swap-a"))
    first = run(handler, check)
    assert first.window == 65536
    reader.serving[ENDPOINT_A] = served(ENDPOINT_A, **SWAPS["llamacpp-mid"])  # type: ignore[arg-type]
    handler = probe(reader, machine_for(ENDPOINT_A, "model-llamacpp-mid"))
    second = run(handler, check)
    assert (second.served_model, second.window) == ("model-llamacpp-mid", 131072)
    assert second.verdict is EnumCheckVerdict.PASS


@pytest.mark.parametrize("check", ["C13", "C14"])
@pytest.mark.parametrize("endpoint", LOCAL_ENDPOINTS)
def test_delegation_probe_model_swap_to_a_windowless_model_is_never_green(
    endpoint: str, check: CheckId
) -> None:
    """A swap to a model that declares no window reads NOT_MEASURED with the chain it tried, never PASS."""
    reader = LiveCatalogReader(
        {
            endpoint: ModelResponderReading(
                base_url=endpoint, answered=True, served_model="model-no-window"
            )
        }
    )
    result = run(probe(reader, machine_for(endpoint, "model-no-window")), check)
    assert result.verdict is EnumCheckVerdict.NOT_MEASURED
    assert result.conclusion == "failure"
    assert any(a.outcome == "no-window" for a in result.chain)


def test_delegation_probe_model_swap_a_probe_without_a_chain_call_is_refused() -> None:
    """A probe grades only after the chain was called: a record with clauses and an empty chain cannot exist."""
    reader = LiveCatalogReader(fixture_catalog())
    for check in TASK_AND_SURFACE_CHECKS:
        before = len(reader.reads)
        result = run(
            probe(reader, machine_for(ENDPOINT_A, "model-before-swap-a")), check
        )
        assert len(reader.reads) > before, f"{check} graded without reading the catalog"
        assert result.chain, f"{check} graded with no chain call recorded"


RECORDED_LISTING = (
    Path(__file__).parent / "fixtures" / "llamacpp_models_listing_pc2.json"
)


@pytest.mark.parametrize("check", list(TASK_AND_SURFACE_CHECKS))
def test_delegation_probe_model_swap_recorded_listing_of_the_second_gpu_host(
    check: CheckId,
) -> None:
    """The listing the second lab GPU host answered on 2026-11-24 (llama.cpp: no max_model_len) carries a probe to PASS.

    The model it names is whatever the host served that day; the probe reads it, the window and the capabilities from the
    listing, never from a pin, and its record carries them.
    """
    body = json.loads(RECORDED_LISTING.read_text(encoding="utf-8"))
    reading = reading_from_models_body(ENDPOINT_B, body).model_copy(
        update={
            "capabilities": {"Tools": "unknown"},
            "input_usd_per_million": Decimal(0),
            "output_usd_per_million": Decimal(0),
        }
    )
    listed = body["data"][0]
    reader = LiveCatalogReader({ENDPOINT_B: reading})
    result = run(probe(reader, machine_for(ENDPOINT_B, listed["id"])), check)
    assert result.verdict is EnumCheckVerdict.PASS
    assert result.served_model == listed["id"]
    assert result.window == listed["meta"]["n_ctx"]
    assert result.window != listed["meta"]["n_ctx_train"]
    assert result.capabilities == {"Tools": "unknown"}


@pytest.mark.parametrize("check", list(TASK_AND_SURFACE_CHECKS))
def test_delegation_probe_model_swap_a_machine_without_a_collector_still_records_the_chain(
    check: CheckId,
) -> None:
    """A probe whose collector is absent reads PRECONDITION-ABSENT and still carries the live catalog it read."""
    reader = LiveCatalogReader(fixture_catalog())
    result = run(probe(reader, InMemoryCustomerMachine()), check)
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.chain
    assert result.window == 65536
    assert result.served_model == "model-before-swap-a"


# ----- the sources name no model and hold no window or price constant ---------------------------------------------

SCANNED = (
    ROOT / "src/omnimarket/nodes/node_delegation_check_probe_effect",
    ROOT / "src/omnimarket/nodes/node_build_delegation_checks",
    ROOT / "src/omnimarket/handlers/routing/delegation_check_responder_chain.py",
)
MODEL_NAME = re.compile(
    r"inkling|qwen|gpt-?[0-9o]|glm-?[0-9]|gemini-?[0-9]|mistral|claude-(?:opus|sonnet|haiku)|llama-?[0-9]",
    re.IGNORECASE,
)
# A literal that is a context window (a power of two from 2048 up), or a nonzero number on a line that names a price.
WINDOW = re.compile(
    r"(?<![\w.])(?:2048|4096|8192|16384|32768|65536|131072|262144|524288|1048576)(?![\w.])"
)
PRICE_LINE = re.compile(r"price|usd|cost|per_million", re.IGNORECASE)
NONZERO_NUMBER = re.compile(r"(?<![\w.])(?!0+(?:\.0+)?(?![\w.]))\d+(?:\.\d+)?(?![\w.])")


def _sources() -> list[Path]:
    found: list[Path] = []
    for source in SCANNED:
        paths = sorted(source.rglob("*")) if source.is_dir() else [source]
        found.extend(
            p
            for p in paths
            if p.suffix in {".py", ".yaml"}
            and "__pycache__" not in p.parts
            and p.name != "overlay.example.yaml"
        )
    return found


def _constants(path: Path) -> list[str]:
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    hits = [m.group(0) for ln in lines for m in WINDOW.finditer(ln)]
    hits += [
        m.group(0)
        for ln in lines
        if PRICE_LINE.search(ln)
        for m in NONZERO_NUMBER.finditer(ln)
    ]
    return hits


def _constants_of(text: str) -> list[str]:
    scratch = Path(tempfile.mkdtemp()) / "probe.py"
    scratch.write_text(text, encoding="utf-8")
    return _constants(scratch)


def test_delegation_probe_model_swap_the_scan_can_fire() -> None:
    assert MODEL_NAME.search("served = 'Qwen3.8-27B'")
    assert _constants_of("window = 131072") == ["131072"]
    assert _constants_of("input_usd_per_million = 0.5") == ["0.5"]
    assert _constants_of("input_usd_per_million = Decimal(0)") == []
    assert _constants_of("cost_usd: float = 0.0") == []
    assert _constants_of("timeout_ms: 3600000") == []
    assert len(_sources()) >= 12


def test_delegation_probe_model_swap_no_probe_source_names_a_model() -> None:
    offenders = {
        str(p.relative_to(ROOT)): MODEL_NAME.findall(p.read_text(encoding="utf-8"))
        for p in _sources()
        if MODEL_NAME.search(p.read_text(encoding="utf-8"))
    }
    assert offenders == {}


def test_delegation_probe_model_swap_no_probe_source_holds_a_window_or_price() -> None:
    offenders = {
        str(p.relative_to(ROOT)): hits for p in _sources() if (hits := _constants(p))
    }
    assert offenders == {}
