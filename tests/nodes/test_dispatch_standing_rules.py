# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing dispatch rules: controlled facts, clock, admission and effects."""

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.container.container_overlay_dependencies import (
    bind_overlay,
    load_overlay_bindings,
)
from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesDispatch,
    ModelStandingRulesRequest,
    ModelStandingRulesSnapshot,
)
from omnimarket.nodes.node_dispatch_standing_rules_compute.handlers.handler_dispatch_standing_rules import (
    HandlerDispatchStandingRules,
)
from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.handler_dispatch_standing_rules_effect import (
    HandlerDispatchStandingRulesEffect,
)
from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.standing_rules_io import (
    FileStandingRulesIO,
    MemoryStandingRulesIO,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 4, 17, tzinfo=UTC)


def snapshot(**dispatch: object) -> ModelStandingRulesSnapshot:
    registry = yaml.safe_load(
        files("omnimarket.nodes.node_dispatch_standing_rules_effect")
        .joinpath("standing_rules.yaml")
        .read_text()
    )
    return ModelStandingRulesSnapshot.model_validate(
        {
            "dispatch": {
                "request_id": "fixture-1",
                "lane": "fixture",
                "dispatch_kind": "runner",
                "model": "sonnet",
                "repo_only": True,
                "placement": "lab",
                **dispatch,
            },
            "now": NOW,
            "registry": registry,
            "rulings": [rule["source_ruling"] for rule in registry["rules"]],
            "facts": {
                "cloud_credit": {
                    "remaining": 10,
                    "expires_at": "2026-11-05T00:00:00Z",
                },
                "cloud_slot": {"available": True},
            },
        }
    )


def test_rule_cloud_first_standing_rules_refuses_explicit_lab() -> None:
    result = HandlerDispatchStandingRules().handle(snapshot())
    assert result.verdict == "refuse"
    assert "FIX: --placement cloud|auto" in result.fix


def test_rule_cloud_first_standing_rules_fixed_request_is_allowed() -> None:
    assert (
        HandlerDispatchStandingRules().handle(snapshot(placement="cloud")).verdict
        == "allow"
    )


def test_rule_cloud_first_standing_rules_auto_routes_cloud_first() -> None:
    result = HandlerDispatchStandingRules().handle(snapshot(placement="auto"))
    assert result.verdict == "route"
    assert result.route is not None
    assert result.route.order == ("cloud", "lab")


@pytest.mark.parametrize(("model", "resources"), [("codex", ()), ("sonnet", ("gpu",))])
def test_rule_cloud_first_standing_rules_exclusions(
    model: str, resources: tuple[str, ...]
) -> None:
    request = snapshot(model=model, lab_resources=resources).model_copy(
        update={"facts": {}}
    )
    assert HandlerDispatchStandingRules().handle(request).verdict == "allow"


def test_standing_rules_missing_credit_is_unread() -> None:
    request = snapshot().model_copy(
        update={"facts": {"cloud_slot": snapshot().facts["cloud_slot"]}}
    )
    result = HandlerDispatchStandingRules().handle(request)
    assert result.verdict == "UNREAD"
    assert "cloud_credit" in result.fix
    assert "read" in result.fix.lower()


def test_rule_cloud_first_standing_rules_lapsed_never_refuses() -> None:
    request = snapshot().model_copy(update={"now": datetime(2026, 11, 6, tzinfo=UTC)})
    result = HandlerDispatchStandingRules().handle(request)
    assert result.verdict == "allow"
    assert result.lapsed == ("R-A",)


def test_standing_rules_source_must_resolve() -> None:
    result = HandlerDispatchStandingRules().handle(
        snapshot().model_copy(update={"rulings": ()})
    )
    assert result.verdict == "UNREAD"
    assert "ledger" in result.fix


@pytest.mark.parametrize("field", ["source_ruling", "expires_from"])
def test_standing_rules_registry_requires_provenance_and_expiry(field: str) -> None:
    data = snapshot().model_dump(mode="json")
    del data["registry"]["rules"][0][field]
    with pytest.raises(ValidationError):
        ModelStandingRulesSnapshot.model_validate(data)


def test_standing_rules_request_is_typed() -> None:
    with pytest.raises(ValidationError):
        ModelStandingRulesDispatch.model_validate({"placement": "elsewhere"})


def test_standing_rules_closed_check_names() -> None:
    data = snapshot().model_dump(mode="json")
    data["registry"]["rules"][0]["check"] = "exec"
    with pytest.raises(ValidationError):
        ModelStandingRulesSnapshot.model_validate(data)


@pytest.mark.parametrize("now", ["2026-11-05T00:00:00Z", "2026-11-05T00:00:01Z"])
def test_standing_rules_expiry_boundary_does_not_read_slot(now: str) -> None:
    req = snapshot().model_copy(
        update={
            "now": datetime.fromisoformat(now.replace("Z", "+00:00")),
            "facts": {"cloud_credit": snapshot().facts["cloud_credit"]},
        }
    )
    result = HandlerDispatchStandingRules().handle(req)
    assert result.verdict == "allow"
    assert result.lapsed == ("R-A",)


def test_standing_rules_absolute_expiry() -> None:
    data = snapshot().model_dump(mode="json")
    data["registry"]["rules"][0]["expires_from"] = {
        "kind": "time",
        "at": "2026-11-04T16:00:00Z",
    }
    data["facts"] = {}
    result = HandlerDispatchStandingRules().handle(
        ModelStandingRulesSnapshot.model_validate(data)
    )
    assert result.verdict == "allow"
    assert result.lapsed == ("R-A",)


@pytest.mark.parametrize(("remaining", "available"), [(0, True), (10, False)])
def test_rule_cloud_first_standing_rules_allows_lab_without_credit_or_offer(
    remaining: int, available: bool
) -> None:
    data = snapshot().model_dump(mode="json")
    data["facts"]["cloud_credit"]["remaining"] = remaining
    data["facts"]["cloud_slot"]["available"] = available
    assert (
        HandlerDispatchStandingRules()
        .handle(ModelStandingRulesSnapshot.model_validate(data))
        .verdict
        == "allow"
    )


def effect_fixture(
    tmp_path: Path, profile: str
) -> tuple[HandlerDispatchStandingRulesEffect, object, Path]:
    package = files("omnimarket.nodes.node_dispatch_standing_rules_effect")
    registry = snapshot().registry.model_dump(mode="json")
    registry["audit_path"] = str(tmp_path / "audit.jsonl")
    documents = {
        registry["ledger"][
            "path"
        ]: "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | fixture authority\n",
        registry["facts"]["cloud_credit"]["path"]: json.dumps(
            {"remaining": 10, "expires_at": "2026-11-05T00:00:00Z"}
        ),
        registry["facts"]["cloud_slot"]["path"]: json.dumps({"available": True}),
    }
    bindings = yaml.safe_load(package.joinpath(f"overlay.{profile}.yaml").read_text())
    registry_path = tmp_path / "standing_rules.yaml"
    if profile == "redpanda":
        for _name, binding in [
            ("ledger", registry["ledger"]),
            *registry["facts"].items(),
        ]:
            old_path = binding["path"]
            path = tmp_path / old_path
            path.write_text(documents[old_path])
            binding["path"] = str(path)
        registry_path.write_text(yaml.safe_dump(registry))
        bindings["dependencies"][0]["config"] = {"standing_rules": str(registry_path)}
        io: object = FileStandingRulesIO()
    else:
        bindings["dependencies"][0]["config"] = {
            "standing_rules": yaml.safe_dump(registry)
        }
        bindings["dependencies"][1]["config"] = documents
        io = MemoryStandingRulesIO(documents)
    overlay_path = tmp_path / "overlay.yaml"
    overlay_path.write_text(yaml.safe_dump(bindings))
    bind_overlay(load_overlay_bindings(overlay_path))
    return HandlerDispatchStandingRulesEffect(), io, tmp_path / "audit.jsonl"


@pytest.mark.parametrize("profile", ["inmemory", "redpanda"])
def test_standing_rules_effect_overlay_refusal_and_missing_fact(
    tmp_path: Path, profile: str
) -> None:
    handler, _, audit = effect_fixture(tmp_path, profile)
    try:
        request = ModelStandingRulesRequest(dispatch=snapshot().dispatch, now=NOW)
        result = handler.handle(request)
        assert result.verdict == "refuse"
        assert handler.handle(request).verdict == "refuse"
        assert handler._io is not None
        if profile == "inmemory":
            assert isinstance(handler._io, MemoryStandingRulesIO)
            assert len(handler._io.rows[str(audit)]) == 1
            del handler._io.documents["cloud-credit.json"]
        else:
            assert len(audit.read_text().splitlines()) == 1
            (tmp_path / "cloud-credit.json").unlink()
        unread = handler.handle(request)
        assert unread.verdict == "UNREAD"
        assert "read the configured cloud credit record" in unread.fix
    finally:
        bind_overlay(None)


@pytest.mark.parametrize("profile", ["inmemory", "redpanda"])
def test_standing_rules_effect_lapsed_append_once_across_dispatches(
    tmp_path: Path, profile: str
) -> None:
    handler, _, audit = effect_fixture(tmp_path, profile)
    try:
        for index in range(2):
            request = ModelStandingRulesRequest(
                dispatch=snapshot(request_id=f"expired-{index}").dispatch,
                now=datetime(2026, 11, 6, tzinfo=UTC),
            )
            result = handler.handle(request)
            assert result.verdict == "allow"
            assert result.lapsed == ("R-A",)
        if profile == "inmemory":
            assert isinstance(handler._io, MemoryStandingRulesIO)
            rows = list(handler._io.rows[str(audit)].values())
        else:
            rows = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len(rows) == 1
        assert (rows[0].kind if profile == "inmemory" else rows[0]["kind"]) == "LAPSED"
    finally:
        bind_overlay(None)


def test_standing_rules_effect_unbound_is_unread() -> None:
    bind_overlay(None)
    result = HandlerDispatchStandingRulesEffect().handle(
        ModelStandingRulesRequest(dispatch=snapshot().dispatch, now=NOW)
    )
    assert result.verdict == "UNREAD"
    assert "dependency bindings" in result.fix


@pytest.mark.parametrize("entry", ["agent", "workflow", "runner", "lab"])
def test_standing_rules_entries_share_one_answer(tmp_path: Path, entry: str) -> None:
    handler, _, _ = effect_fixture(tmp_path, "inmemory")
    try:
        result = handler.handle(
            ModelStandingRulesRequest(
                dispatch=snapshot(dispatch_kind=entry).dispatch, now=NOW
            )
        )
        assert result == HandlerDispatchStandingRules().handle(
            snapshot(dispatch_kind=entry)
        )
    finally:
        bind_overlay(None)


def test_standing_rules_file_audit_is_atomic(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from omnimarket.models.standing_rules.model_standing_rules import (
        ModelStandingRulesAudit,
    )

    audit = tmp_path / "audit.jsonl"
    row = ModelStandingRulesAudit(at=NOW, kind="LAPSED", rule="R-A", request_id="race")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: FileStandingRulesIO().append_once(
                    str(audit), "LAPSED:R-A", row
                ),
                range(8),
            )
        )
    assert len(audit.read_text().splitlines()) == 1


async def test_standing_rules_installed_runtime_bus_roundtrip(tmp_path: Path) -> None:
    """Discover and wire the packaged handler; the event carries its typed verdict."""
    import asyncio
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    node = "node_dispatch_standing_rules_effect"
    contracts = tuple(c for c in discover_contracts().contracts if c.name == node)
    assert len(contracts) == 1
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
    )
    command = contract["runtime_dispatch"]["command_topic"]
    completed = contract["runtime_dispatch"]["terminal_events"]["success"]
    bus = EventBusInmemory(environment="local", group="standing-rules-proof")
    answers: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await bus.start()
    effect_fixture(tmp_path, "inmemory")

    async def collect(message: object) -> None:
        await answers.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed, group_id="standing-rules-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            node: "wired"
        }
        engine.freeze()
        for entry in ("agent", "workflow", "runner", "lab"):
            correlation_id = uuid4()
            request = ModelStandingRulesRequest(
                dispatch=snapshot(dispatch_kind=entry).dispatch, now=NOW
            )
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation_id,
                event_type="omnimarket.dispatch-standing-rules-effect-requested",
            )
            await bus.publish(command, None, envelope.model_dump_json().encode(), None)
            answer = await asyncio.wait_for(answers.get(), timeout=5)
            assert answer["correlation_id"] == str(correlation_id)
            assert answer["payload"] == HandlerDispatchStandingRules().handle(
                snapshot(dispatch_kind=entry)
            ).model_dump(mode="json")
    finally:
        bind_overlay(None)
        await bus.close()
