# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay resolver node: path rule, hyphen mapping, schema refusal, contract binding.

Every overlay and schema here is a synthetic fixture written under ``tmp_path``. The node ships no overlay
data; the roots come from ``ONEX_SKILL_OVERLAY_ROOTS`` or from the request.
"""

from __future__ import annotations

import importlib
import json
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_overlay_resolver_effect as node_package
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_overlay_resolver_effect.handlers.handler_overlay_resolver import (
    HandlerOverlayResolver,
)
from omnimarket.nodes.node_overlay_resolver_effect.models.model_overlay_resolver import (
    ModelOverlayResolveRequest,
    ModelOverlayResolveResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.overlay-resolve-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.overlay-resolve-completed.v1"
ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
SCHEMA = {
    "type": "object",
    "required": ["window_minutes", "label"],
    "properties": {
        "window_minutes": {"type": "integer", "minimum": 1},
        "label": {"type": "string"},
    },
    "additionalProperties": False,
}
VALID = {"window_minutes": 15, "label": "synthetic"}


def write_overlay(
    root: Path,
    overlay_id: str,
    document: Any,
    schema: dict[str, Any] | str | None = SCHEMA,
) -> Path:
    directory = root / overlay_id
    directory.mkdir(parents=True)
    (directory / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    if schema is not None:
        (directory / "overlay.schema.json").write_text(
            schema if isinstance(schema, str) else json.dumps(schema),
            encoding="utf-8",
        )
    return directory


def resolve(
    environ: dict[str, str] | None = None, **fields: Any
) -> ModelOverlayResolveResult:
    return HandlerOverlayResolver(environ=environ).handle(
        ModelOverlayResolveRequest.model_validate(fields)
    )


def test_overlay_resolver_refuses_missing_names_the_key(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    write_overlay(root, "merge_drain", VALID)
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="ghost_skill")
    message = str(refused.value)
    assert "ghost_skill" in message
    assert "ghost_skill/overlay.yaml" in message
    assert ROOTS_ENV in message


def test_overlay_resolver_refuses_missing_when_no_root_is_configured() -> None:
    with pytest.raises(NodeOverlayError) as refused:
        resolve({}, skill="merge-drain")
    assert "merge_drain" in str(refused.value)
    assert ROOTS_ENV in str(refused.value)


def test_overlay_resolver_refuses_missing_returns_no_default(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    with pytest.raises(NodeOverlayError):
        resolve(roots=[str(root)], skill="merge_drain")


def test_overlay_resolver_resolves_from_the_first_root_that_holds_it(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    write_overlay(second, "merge_drain", VALID)
    result = resolve(
        {ROOTS_ENV: os.pathsep.join((str(first), str(second)))}, skill="merge_drain"
    )
    assert result.overlay_id == "merge_drain"
    assert result.document == VALID
    assert Path(result.overlay_path) == second / "merge_drain" / "overlay.yaml"
    assert Path(result.schema_path) == second / "merge_drain" / "overlay.schema.json"


def test_overlay_resolver_roots_in_the_request_win_over_the_environment(
    tmp_path: Path,
) -> None:
    env_root, request_root = tmp_path / "env", tmp_path / "request"
    write_overlay(env_root, "merge_drain", {"window_minutes": 1, "label": "env"})
    write_overlay(
        request_root, "merge_drain", {"window_minutes": 2, "label": "request"}
    )
    result = resolve(
        {ROOTS_ENV: str(env_root)}, skill="merge_drain", roots=[str(request_root)]
    )
    assert result.document["label"] == "request"


@pytest.mark.parametrize(
    ("skill", "overlay_id"),
    [("merge-drain", "merge_drain"), ("session-start", "session_start")],
)
def test_overlay_resolver_hyphen_to_underscore(
    tmp_path: Path, skill: str, overlay_id: str
) -> None:
    root = tmp_path / "roots"
    write_overlay(root, overlay_id, VALID)
    result = resolve({ROOTS_ENV: str(root)}, skill=skill)
    assert result.overlay_id == overlay_id
    assert Path(result.overlay_path) == root / overlay_id / "overlay.yaml"


def test_overlay_resolver_hyphen_to_underscore_ignores_a_hyphenated_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "roots"
    write_overlay(root, "merge-drain", VALID)
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge-drain")
    assert "merge_drain" in str(refused.value)


@pytest.mark.parametrize("skill", ["", "../merge_drain", "merge/drain", "Merge Drain"])
def test_overlay_resolver_refuses_a_skill_name_that_is_not_an_id(skill: str) -> None:
    with pytest.raises(ValidationError):
        ModelOverlayResolveRequest.model_validate({"skill": skill})


def test_overlay_resolver_schema_refusal_names_the_schema_path(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    directory = write_overlay(
        root, "merge_drain", {"window_minutes": 0, "label": "PRIVATE-VALUE"}
    )
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge-drain")
    message = str(refused.value)
    assert str(directory / "overlay.schema.json") in message
    assert "window_minutes" in message
    assert "PRIVATE-VALUE" not in message


def test_overlay_resolver_schema_refusal_on_an_unknown_key(tmp_path: Path) -> None:
    root = tmp_path / "roots"
    directory = write_overlay(root, "merge_drain", {**VALID, "surprise": 1})
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge_drain")
    assert str(directory / "overlay.schema.json") in str(refused.value)


def test_overlay_resolver_schema_refusal_when_the_schema_is_absent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "roots"
    directory = write_overlay(root, "merge_drain", VALID, schema=None)
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge_drain")
    assert str(directory / "overlay.schema.json") in str(refused.value)


@pytest.mark.parametrize("schema", ["{not json", json.dumps({"type": "no-such-type"})])
def test_overlay_resolver_schema_refusal_when_the_schema_is_broken(
    tmp_path: Path, schema: str
) -> None:
    root = tmp_path / "roots"
    directory = write_overlay(root, "merge_drain", VALID, schema=schema)
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge_drain")
    assert str(directory / "overlay.schema.json") in str(refused.value)


def test_overlay_resolver_schema_refusal_when_the_overlay_is_unreadable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "roots"
    directory = write_overlay(root, "merge_drain", VALID)
    (directory / "overlay.yaml").write_text("a: [unterminated", encoding="utf-8")
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge_drain")
    assert str(directory / "overlay.yaml") in str(refused.value)


def test_overlay_resolver_contract_bound_handler_and_models() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerOverlayResolver
    assert issubclass(node_package.NodeOverlayResolverEffect, handler_type)
    routed = contract["handler_routing"]["default_handler"]
    module_name, _, class_name = routed.partition(":")
    assert getattr(importlib.import_module(module_name), class_name) is handler_type
    for side, model in (
        ("input_model", ModelOverlayResolveRequest),
        ("output_model", ModelOverlayResolveResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_overlay_resolver_effect"].load() is node_package


def test_overlay_resolver_contract_bound_topics() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC


def test_overlay_resolver_contract_bound_metadata_registers_the_node() -> None:
    metadata = dict(yaml.safe_load((NODE_DIR / "metadata.yaml").read_text()))
    assert metadata["name"] == "node_overlay_resolver_effect"
    assert metadata["entry_points"]["onex.nodes"]["node_overlay_resolver_effect"] == (
        "omnimarket.nodes.node_overlay_resolver_effect"
    )


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_overlay_resolver_contract_bound_golden_chain_over_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "roots"
    write_overlay(root, "session_start", VALID)
    monkeypatch.setenv(ROOTS_ENV, str(root))
    runtime = await _run(tmp_path, {"skill": "session-start"}, "resolve")
    result = runtime.handler_result
    assert isinstance(result, ModelOverlayResolveResult)
    assert result.overlay_id == "session_start"
    assert result.document == VALID
    assert (
        ModelOverlayResolveResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_overlay_resolver_contract_bound_error_chain_over_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "roots"
    root.mkdir()
    monkeypatch.setenv(ROOTS_ENV, str(root))
    absent = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=tmp_path / "absent.json",
        state_root=tmp_path / "state-absent",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    (tmp_path / "absent.json").write_text(json.dumps({"skill": "ghost-skill"}))
    assert await absent.run_async() is EnumWorkflowResult.FAILED
    assert absent.handler_result is None


@pytest.mark.parametrize(
    "ref", ["#/$defs/absent", "no-such-schema.json", "https://example.invalid/s.json"]
)
def test_overlay_resolver_schema_refusal_when_a_schema_reference_is_unresolvable(
    tmp_path: Path, ref: str
) -> None:
    root = tmp_path / "roots"
    schema = {"type": "object", "properties": {"label": {"$ref": ref}}}
    directory = write_overlay(root, "merge_drain", VALID, schema=schema)
    with pytest.raises(NodeOverlayError) as refused:
        resolve({ROOTS_ENV: str(root)}, skill="merge_drain")
    assert str(directory / "overlay.schema.json") in str(refused.value)
