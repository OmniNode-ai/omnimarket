# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the compaction manifest compute (OMN-19998)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.handlers.context_compaction_bundle import (
    read_bundle_files,
    write_bundle_files,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers import (
    HandlerReplayBundleVerifier,
    HandlerReplayManifestBuilder,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    ModelBuildManifestRequest,
    ModelCaseSpec,
    ModelVerifyBundleRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_context_compaction_manifest_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
COMMAND_TOPIC = "onex.cmd.omnimarket.context-compaction-manifest-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.context-compaction-manifest-completed.v1"


def test_golden_chain_build_write_read_verify(tmp_path: Path) -> None:
    """A synthetic request produces pins that reproduce from its on-disk bundle."""
    prompt = "please keep the merge queue decision"
    request = ModelBuildManifestRequest.model_validate(
        {
            "specs": [
                {
                    "case_id": "case-a",
                    "finding": "synthetic repeated instruction",
                    "census_status": "CONFIRMED",
                    "census_ref": "synthetic census",
                    "session_id": "session-a",
                    "boundary_pre_event_ids": ["pre-a"],
                    "expected_statements": [
                        {
                            "statement_id": "statement-a",
                            "prompt_event_id": "prompt-a",
                            "ledger_stamp": "2025-01-01T00:01:00Z",
                            "quote": prompt,
                        }
                    ],
                    "post_boundary_evidence": [
                        {
                            "event_id": "repeat-a",
                            "kind": "RESTATED",
                            "required_spans": [prompt],
                        }
                    ],
                    "question": "Which decision should the replay retain?",
                    "expected_answer": prompt,
                }
            ],
            "events": [
                {
                    "event_id": "pre-a",
                    "session_id": "session-a",
                    "hook_event_name": "PreCompact",
                    "emitted_at": "2025-01-01T00:02:00Z",
                    "trigger": "manual",
                },
                {
                    "event_id": "post-a",
                    "session_id": "session-a",
                    "hook_event_name": "PostCompact",
                    "emitted_at": "2025-01-01T00:03:00Z",
                    "trigger": "manual",
                },
            ],
            "records": [
                {
                    "event_id": event_id,
                    "session_id": "session-a",
                    "content_kind": "operator_prompt",
                    "emitted_at": emitted_at,
                    "content": prompt,
                }
                for event_id, emitted_at in (
                    ("prompt-a", "2025-01-01T00:00:00Z"),
                    ("repeat-a", "2025-01-01T00:04:00Z"),
                )
            ],
            "ledger_files": [
                {
                    "name": "synthetic-ledger.md",
                    "content": f'2025-01-01T00:01:00Z | RULING | "{prompt}"\n'.encode(),
                }
            ],
            "census_ref": "synthetic census",
        }
    )
    result = HandlerReplayManifestBuilder().handle(request)
    assert result.case_ids == ("case-a",)
    assert result.refusals == ()
    write_bundle_files(tmp_path, result.bundle_files)
    verdict = HandlerReplayBundleVerifier().handle(
        ModelVerifyBundleRequest(
            manifest_yaml=result.manifest_yaml,
            bundle_files=read_bundle_files(tmp_path),
        )
    )
    assert verdict.ok
    assert verdict.cases_checked == 1
    assert verdict.failures == ()


def test_error_chain_case_without_boundaries_is_refused() -> None:
    """Missing case pins are refused before the handler runs."""
    with pytest.raises(ValidationError, match="boundary_pre_event_ids"):
        ModelCaseSpec.model_validate(
            {
                "case_id": "case-a",
                "finding": "synthetic incomplete case",
                "census_status": "PLAUSIBLE",
                "census_ref": "synthetic census",
                "session_id": "session-a",
                "expected_statements": [],
                "post_boundary_evidence": [],
                "question": "Which decision should the replay retain?",
                "expected_answer": "the merge queue decision",
            }
        )


def test_contract_loads_and_declares_its_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["metadata"]["ticket"] == "OMN-19998"
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [route["operation"] for route in routing["handlers"]] == [
        "pair_compaction_boundaries",
        "classify_prompt_window",
        "classify_repeat",
        "build_replay_manifest",
        "verify_replay_bundle",
    ]


@pytest.mark.parametrize(
    ("operation", "handler_name"),
    [
        ("pair_compaction_boundaries", "HandlerBoundaryPairing"),
        ("classify_prompt_window", "HandlerPromptWindow"),
        ("classify_repeat", "HandlerRepeatClassifier"),
        ("build_replay_manifest", "HandlerReplayManifestBuilder"),
        ("verify_replay_bundle", "HandlerReplayBundleVerifier"),
    ],
)
def test_contract_handlers_and_models_resolve(
    operation: str, handler_name: str
) -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    route = next(
        route
        for route in contract["handler_routing"]["handlers"]
        if route["operation"] == operation
    )
    declaration = route["handler"]
    assert declaration["name"] == handler_name
    assert declaration["module"].startswith(f"{PACKAGE}.handlers.")
    handler_class = getattr(
        importlib.import_module(declaration["module"]), handler_name
    )
    assert handler_class.__module__ == declaration["module"]
    assert callable(handler_class().handle)
    for key in ("input_model", "output_model"):
        model_path = route[key]
        assert model_path.startswith(f"{PACKAGE}.models.")
        module_name, class_name = model_path.rsplit(".", 1)
        assert issubclass(
            getattr(importlib.import_module(module_name), class_name), BaseModel
        )


def test_entry_point_registered_and_loads() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text(encoding="utf-8"))
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert "entry_points" not in contract
    registered = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    assert registered[NODE_NAME].value == PACKAGE
    assert registered[NODE_NAME].load() is importlib.import_module(PACKAGE)
    assert metadata["pack"] == "core"
    assert metadata["node_role"] == "compute"
    assert metadata["capabilities"]["requires_repo"] is False
    assert metadata["capabilities"]["side_effect_class"] == "read_only"
