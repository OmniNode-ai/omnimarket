# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-equivalence proof for the RSD-fill def-B flip (OMN-14839).

Class-B Tier-1 canonical-rewrite fan-out (epic OMN-14355). ``HandlerRsdFill.handle``
moved from ``handle(correlation_id, scored_tickets, max_tickets) -> ModelRsdFillOutput``
(def-A, three positional params -> non-canonical, RuntimeLocal-undispatchable) to the
canonical ``handle(request: ModelRsdFillInput) -> ModelRsdFillOutput`` (def-B, single
typed-payload positional; the fields are read from ``request``). The selection logic is
UNCHANGED: it was extracted verbatim into ``HandlerRsdFill._select_top_n`` (proven
byte-identical across the flip by the git-re-derived hand-flip proof
``scripts/ci/adequacy_receipts/omnimarket.nodes.node_rsd_fill_compute.handflip.json``).

The goldens under ``tests/fixtures/golden/node_rsd_fill_compute/*.json`` capture
byte-canonical ``handle`` output + a sha256 fingerprint for a reviewed scenario pool.
This durable regression drives the SAME recorded inputs through the live def-B
``handle(request)`` and asserts byte/fingerprint-identical output — so a future edit to
this handler cannot silently change behavior without failing here.

``test_fingerprint_discriminates`` proves the oracle is not a vacuous green: a
one-field perturbation of a recorded output changes the fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_input import (
    ModelRsdFillInput,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_output import (
    ModelRsdFillOutput,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN_DIR = _REPO_ROOT / "tests" / "fixtures" / "golden" / "node_rsd_fill_compute"
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_rsd_fill_compute"
    / "contract.yaml"
)

# Recorded from the pre-flip def-A selection logic; the reviewed scenario pool size.
_EXPECTED_GOLDEN_COUNT = 8


def _fingerprint(output_json: dict[str, Any]) -> str:
    canonical = json.dumps(output_json, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _golden_files() -> list[Path]:
    files = sorted(_GOLDEN_DIR.glob("*.json"))
    assert files, f"no golden fixtures found under {_GOLDEN_DIR}"
    return files


@pytest.mark.asyncio
@pytest.mark.parametrize("golden_path", _golden_files(), ids=lambda p: p.stem)
async def test_handle_reproduces_recorded_golden(golden_path: Path) -> None:
    """def-B ``handle(request)`` on the recorded input == the recorded output."""
    golden: dict[str, Any] = json.loads(golden_path.read_text(encoding="utf-8"))
    request = ModelRsdFillInput.model_validate(golden["input"])

    output = await HandlerRsdFill().handle(request)

    assert isinstance(output, ModelRsdFillOutput)
    fresh_json = output.model_dump(mode="json")
    assert fresh_json == golden["output"], (
        f"{golden_path.name}: def-B output structure diverged from recorded def-A"
    )
    assert _fingerprint(fresh_json) == golden["output_sha256"], (
        f"{golden_path.name}: def-B output fingerprint diverged from recorded def-A"
    )


def test_golden_fixture_count_matches_expected_pool() -> None:
    """Regression guard: the recorded scenario pool has a known, reviewed size."""
    assert len(_golden_files()) == _EXPECTED_GOLDEN_COUNT


def test_fingerprint_discriminates() -> None:
    """The fingerprint oracle is not vacuous: a 1-field perturbation diverges."""
    golden = json.loads(_golden_files()[0].read_text(encoding="utf-8"))
    recorded_fp = golden["output_sha256"]
    assert _fingerprint(golden["output"]) == recorded_fp  # sanity: recompute matches

    perturbed = json.loads(json.dumps(golden["output"]))
    perturbed["total_selected"] = int(perturbed["total_selected"]) + 1
    assert _fingerprint(perturbed) != recorded_fp, (
        "fingerprint failed to detect a perturbed output — oracle is vacuous"
    )


def test_terminal_events_declared_and_bound() -> None:
    """State-coverage: both declared terminal topics are bound to their literals.

    ``HandlerRsdFill.handle`` is total for a valid ``ModelRsdFillInput`` (pure top-N
    selection, no internal raise path), so the ``failure`` terminal is synthesized by
    the runtime only on an upstream envelope/validation error, never by the compute
    core. This asserts the contract binding of both declared terminals.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    terminals = contract["runtime_dispatch"]["terminal_events"]
    assert terminals["success"] == "onex.evt.omnimarket.rsd-fill-completed.v1"
    assert terminals["failure"] == "onex.evt.omnimarket.rsd-fill-failed.v1"
