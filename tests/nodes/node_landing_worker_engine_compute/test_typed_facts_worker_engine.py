# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worker-engine choice over typed facts, with no placement module and no env switch (T-A5, OMN-21958).

The caller supplies each engine's availability (the pick a host can take, or why none can) and the node's
config; the Codex engine id and the two Codex routes come from the node's contract and its overlay. The
legacy ``placement`` and ``env`` inputs stay for callers not yet moved, and the golden-chain tests keep
exercising them unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_landing_worker_engine_compute import (
    HandlerLandingWorkerEngine,
)
from omnimarket.nodes.node_landing_worker_engine_compute.handlers.worker_engine_config import (
    load_worker_engine_config,
)
from omnimarket.nodes.node_landing_worker_engine_compute.models import (
    ModelEngineAvailability,
    ModelLandingWorkerEngineRequest,
    ModelWorkerEngineConfig,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import facts, stop

pytestmark = pytest.mark.unit

NODE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_landing_worker_engine_compute"
)
CLAUDE = "claude_opus"
CODEX = "codex_high"
CONFIG = ModelWorkerEngineConfig(
    codex_engine=CODEX, codex_mechanical=True, codex_fallback=True
)


def pick_of(engine: str, host: str) -> Any:
    return SimpleNamespace(engine=engine, host=SimpleNamespace(name=host))


def availability(
    *, claude: Any = None, codex: Any = None, reason: str = "LIMITED"
) -> tuple[ModelEngineAvailability, ...]:
    return (
        ModelEngineAvailability(
            engine=CLAUDE, pick=claude, skips={"host-a": reason, "host-d": reason}
        ),
        ModelEngineAvailability(
            engine=CODEX, pick=codex, skips={"host-a": reason, "host-b": reason}
        ),
    )


def request(**over: Any) -> ModelLandingWorkerEngineRequest:
    return ModelLandingWorkerEngineRequest(
        **{
            "requested_engine": CLAUDE,
            "codex_first": False,
            "cause": False,
            "config": CONFIG,
            "availability": availability(),
            **over,
        }
    )


def test_typed_facts_need_no_placement_module_and_no_env() -> None:
    claude = pick_of(CLAUDE, "host-a")
    result = HandlerLandingWorkerEngine().handle(
        request(availability=availability(claude=claude))
    )
    assert result.pick is claude
    assert result.record == {}
    assert "placement" not in ModelLandingWorkerEngineRequest.model_fields or (
        ModelLandingWorkerEngineRequest.model_fields["placement"].default is None
    )


def test_typed_facts_codex_first_pick_keeps_the_record() -> None:
    codex = pick_of(CODEX, "host-b")
    result = HandlerLandingWorkerEngine().handle(
        request(codex_first=True, availability=availability(codex=codex))
    )
    assert result.pick is codex
    assert result.record == {"codex_first": True}


@pytest.mark.parametrize("reason", ["CODEX-LIMITED", "LOAD-OVER-BAR"])
def test_typed_facts_no_codex_slot_falls_back_to_the_requested_claude_engine(
    reason: str,
) -> None:
    claude = pick_of(CLAUDE, "host-d")
    result = HandlerLandingWorkerEngine().handle(
        request(
            codex_first=True, availability=availability(claude=claude, reason=reason)
        )
    )
    assert result.pick is claude
    assert result.record["codex_first"] is True
    assert result.record["codex_fallback"] == {
        "host-a": reason,
        "host-b": reason,
    }


def test_typed_facts_claude_limited_pool_places_a_codex_worker() -> None:
    codex = pick_of(CODEX, "host-b")
    result = HandlerLandingWorkerEngine().handle(
        request(availability=availability(codex=codex))
    )
    assert result.pick is codex
    assert result.record["claude_no_headroom"] == {
        "host-a": "LIMITED",
        "host-d": "LIMITED",
    }


def test_typed_facts_cause_brief_still_waits_for_claude() -> None:
    result = HandlerLandingWorkerEngine().handle(
        request(cause=True, availability=availability(codex=pick_of(CODEX, "host-b")))
    )
    assert result.pick is None


@pytest.mark.parametrize("switch", ["codex_fallback", "codex_mechanical"])
def test_typed_facts_a_route_off_in_the_config_turns_the_fallback_off(
    switch: str,
) -> None:
    config = CONFIG.model_copy(update={switch: False})
    result = HandlerLandingWorkerEngine().handle(
        request(config=config, availability=availability(codex=pick_of(CODEX, "h")))
    )
    assert result.pick is None


def test_typed_facts_an_engine_with_no_availability_refuses_naming_it() -> None:
    only_claude = (ModelEngineAvailability(engine=CLAUDE, pick=None),)
    with pytest.raises(ValueError, match=CODEX):
        HandlerLandingWorkerEngine().handle(request(availability=only_claude))


def test_typed_facts_a_request_with_neither_facts_nor_placement_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLandingWorkerEngineRequest(
            requested_engine=CLAUDE, codex_first=False, cause=False
        )


def test_typed_facts_stop_in_an_enforce_class_keeps_the_engine_out_of_the_choice(
    tmp_path: Path,
) -> None:
    claude, codex = pick_of(CLAUDE, "host-a"), pick_of(CODEX, "host-b")
    stopped = stop(key=CLAUDE)
    result = HandlerLandingWorkerEngine().handle(
        request(
            availability=availability(claude=claude, codex=codex),
            stop_gate=facts(tmp_path, "enforce", stopped),
        )
    )
    assert result.pick is codex
    assert stopped.id in result.record["engine_refused"][CLAUDE]


def test_typed_facts_a_stop_on_every_engine_leaves_no_pick(tmp_path: Path) -> None:
    claude, codex = pick_of(CLAUDE, "host-a"), pick_of(CODEX, "host-b")
    result = HandlerLandingWorkerEngine().handle(
        request(
            availability=availability(claude=claude, codex=codex),
            stop_gate=facts(tmp_path, "enforce", stop(key=CLAUDE), stop(key=CODEX)),
        )
    )
    assert result.pick is None
    assert set(result.record["engine_refused"]) == {CLAUDE, CODEX}


def test_typed_facts_observe_mode_refuses_nothing(tmp_path: Path) -> None:
    claude, codex = pick_of(CLAUDE, "host-a"), pick_of(CODEX, "host-b")
    stopped = stop(key=CLAUDE)
    result = HandlerLandingWorkerEngine().handle(
        request(
            availability=availability(claude=claude, codex=codex),
            stop_gate=facts(tmp_path, "observe", stopped),
        )
    )
    assert result.pick is claude
    assert "engine_refused" not in result.record
    assert stopped.id in result.record["stop_observed"][CLAUDE]


def test_typed_facts_the_config_comes_from_the_contract() -> None:
    contract = yaml.safe_load((NODE_ROOT / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["config"]["codex_engine"] == CODEX
    assert contract["config"]["codex_routes"] == {"mechanical": True, "fallback": True}
    assert load_worker_engine_config({}) == CONFIG


def test_typed_facts_the_overlay_turns_a_route_off(tmp_path: Path) -> None:
    folder = tmp_path / "landing_worker_engine"
    folder.mkdir()
    (folder / "overlay.yaml").write_text("codex_routes:\n  fallback: false\n")
    config = load_worker_engine_config({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert config.codex_fallback is False
    assert config.codex_mechanical is True
    assert config.codex_engine == CODEX


def test_typed_facts_a_broken_overlay_refuses_naming_the_key(tmp_path: Path) -> None:
    folder = tmp_path / "landing_worker_engine"
    folder.mkdir()
    (folder / "overlay.yaml").write_text("codex_routes:\n  everything: false\n")
    with pytest.raises(NodeOverlayError, match="codex_routes"):
        load_worker_engine_config({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_typed_facts_the_handler_reads_no_env_switch_and_no_threshold_literal() -> None:
    handler = (NODE_ROOT / "handlers/handler_landing_worker_engine.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in handler.splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    code = re.sub(r"[A-Z_]+ = \"\"\".*?\"\"\"", "", code, flags=re.S)
    assert "LANDING_CODEX" not in code
    assert "environ" not in code
    assert "request.env" not in code
    assert "request.placement" not in code
