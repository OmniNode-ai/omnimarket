# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the rescue-patch scan."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.nodes.node_rescue_patch_review_effect.handlers import (
    HandlerRescuePatchScan,
)
from omnimarket.nodes.node_rescue_patch_review_effect.handlers.adapter_compute_reviewer import (
    review_patch_with_compute_node as _review_patch,
)
from omnimarket.nodes.node_rescue_patch_review_effect.models import (
    ModelRescuePatchScanRequest,
    ModelRescuePatchScanResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_rescue_patch_review_effect"
)


def test_golden_chain_scan_then_preserve_verdict(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    clone = registry / "widgets"
    clone.mkdir(parents=True)
    for args in (("init", "-b", "main"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            cwd=clone,
            env=scrub_git_location_env(os.environ),
            check=True,
            capture_output=True,
        )
    root = tmp_path / "rescue"
    saved = root / "saved__widgets"
    saved.mkdir(parents=True)
    (saved / "MANIFEST.json").write_text(
        json.dumps({"repo": "widgets", "base": "HEAD"})
    )
    (saved / "commits.patch").write_text("")
    request = ModelRescuePatchScanRequest(
        rescue_root=root,
        registry_root=registry,
        state_dir=tmp_path / "state",
        now="2026-10-15T06:00:00Z",
    )
    handler = HandlerRescuePatchScan(reviewer=_review_patch)
    result = handler.handle(
        ModelRescuePatchScanRequest.model_validate_json(request.model_dump_json())
    )
    assert result.reviewed == result.discarded == 1
    verdict = (saved / "VERDICT.json").read_bytes()
    assert json.loads(verdict)["verdict"] == "discard-empty"
    again = handler.handle(request.model_copy(update={"now": "2026-10-16T06:00:00Z"}))
    assert again.reviewed == 0
    assert again.already_reviewed == 1
    assert (saved / "VERDICT.json").read_bytes() == verdict
    beat = json.loads((request.state_dir / "last-run.json").read_text())
    assert beat["ran_utc"] == "2026-10-16T06:00:00Z"
    assert (
        ModelRescuePatchScanResult.model_validate_json(again.model_dump_json()) == again
    )


def test_error_chain_request_with_invalid_path_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRescuePatchScanRequest.model_validate(
            {"rescue_root": [], "registry_root": ".", "state_dir": "."}
        )


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.rescue-patch-scan-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.rescue-patch-scan-completed.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]
    assert contract["terminal_event"] == bus["publish_topics"][0]
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["purity"] == "effectful"
    assert len(contract["descriptor"]["side_effects"]) == 3
    assert contract["handler"]["class"] == HandlerRescuePatchScan.__name__
    assert contract["handler"]["module"] == HandlerRescuePatchScan.__module__
    assert (
        getattr(
            import_module(contract["handler"]["module"]), contract["handler"]["class"]
        )
        is HandlerRescuePatchScan
    )
    assert contract["input_model"] == contract["handler"]["input_model"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["operation"] == "scan_rescue_patches"
    assert route["input_model"] == contract["input_model"]
    assert route["output_model"] == contract["output_model"]
