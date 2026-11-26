# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The additive binding rows session_start, agent_spawn and per_prompt (OMN-22016, plan task TC.11).

Each test drives the real ``onex-claude-code-hook`` entry on a recorded hook input, with the
contract's own rows, against a receipt-mode client double that routes the published command to the
real context-pack orchestrator (no bus), so the join between row and server list is exercised.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess
from pydantic import ValidationError

from omnimarket.models.claude_code_binding import (
    ADDITIVE_BUDGET_CAPS_MS,
    ADDITIVE_TEXT_CAP_CHARS,
    EnumAdditiveMissReason,
    EnumBindingKind,
    EnumClientMaxMode,
    ModelAdditiveClientBinding,
    ModelAdditiveMissRecord,
    ModelBindingRenderRequest,
)
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackCommand,
    ModelContextPackRequest,
)
from omnimarket.nodes.node_claude_code_binding_render_compute.handlers.handler_binding_render import (
    HandlerClaudeCodeBindingRender,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.additive_miss_journal import (
    MISS_EVENT_TYPE,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    HandlerClaudeCodeHookAdapter,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    load_client_section,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelClientSection,
    ModelHookAdapterRequest,
    ModelReceiptEvent,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.runtime_claude_code_hook_cli import (
    main,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers import (
    HandlerContextPackOrchestratorRuntime,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from tests.nodes.node_context_pack_orchestrator.conftest import FixtureSource

if TYPE_CHECKING:
    from pydantic import JsonValue

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

REPO = Path(__file__).resolve().parents[3]
PROFILES = REPO / "src" / "omnimarket" / "pack" / "profiles"
ROWS = ("session_start", "agent_spawn", "per_prompt")
EVENTS = {
    "session_start": "SessionStart",
    "agent_spawn": "SubagentStart",
    "per_prompt": "UserPromptSubmit",
}
SENTINEL = "SENTINEL-TC11-9c41-package-text"
ACCESS = ModelContextAccess(tenant_id="lab")


class OrchestratorClient:
    """A receipt-mode client that hands the published command to a real orchestrator."""

    def __init__(
        self, orchestrator: HandlerContextPackOrchestrator, *, delay_s: float = 0.0
    ) -> None:
        self.orchestrator = orchestrator
        self.delay_s = delay_s
        self.calls: list[dict[str, object]] = []

    def publish_and_await(
        self,
        *,
        topic: str,
        command_type: str,
        payload: dict[str, JsonValue],
        awaited_events: tuple[str, ...],
        deadline_ms: int,
    ) -> tuple[ModelReceiptEvent, ...]:
        self.calls.append(
            {"topic": topic, "payload": payload, "deadline_ms": deadline_ms}
        )
        if self.delay_s:
            time.sleep(self.delay_s)
        built = self.orchestrator.handle(
            ModelContextPackCommand(
                request=ModelContextPackRequest.model_validate(payload),
                access=ACCESS,
            )
        )
        return (
            ModelReceiptEvent(
                topic=awaited_events[0], payload=built.model_dump(mode="json")
            ),
        )


class SilentClient:
    """Answers nothing before the deadline, as a bus whose package never arrives does."""

    def __init__(self, *, sleep_s: float = 0.0) -> None:
        self.sleep_s = sleep_s
        self.calls: list[dict[str, object]] = []

    def publish_and_await(self, **kwargs: object) -> tuple[ModelReceiptEvent, ...]:
        self.calls.append(kwargs)
        time.sleep(self.sleep_s)
        return ()


class ReplyClient:
    """Answers fixed payloads."""

    def __init__(self, *payloads: dict[str, JsonValue]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def publish_and_await(self, **kwargs: object) -> tuple[ModelReceiptEvent, ...]:
        self.calls.append(kwargs)
        return tuple(ModelReceiptEvent(topic="t", payload=p) for p in self.payloads)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enabled_orchestrator(*profiles: str) -> HandlerContextPackOrchestrator:
    texts = {
        EnumContextFactor.SESSION_GOAL: f"goal {SENTINEL}",
        EnumContextFactor.OPERATOR_RULINGS: "2026-10-15T08:14:15Z | RULING | lane=x | r",
        EnumContextFactor.OPEN_WORK: "2026-10-17T22:36:14Z | CLAIM | lane=x | ticket=OMN-1",
        EnumContextFactor.LANE_RULES: "Squash-only.",
    }
    sources = {f: FixtureSource(f, text=t) for f, t in texts.items()}
    return HandlerContextPackOrchestratorRuntime(
        sources,
        enabled_additive_profiles=profiles,
        implementation_identity=lambda: {"fixture": "a" * 64},
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A workspace registry, an internal-library clone, an unrelated directory and a journal."""
    registry = tmp_path / "workspace"
    internal = tmp_path / "library_clone"
    unrelated = tmp_path / "elsewhere"
    journal = tmp_path / "journal"
    for directory in (registry / "omniclaude", internal / "src", unrelated):
        directory.mkdir(parents=True)
    monkeypatch.setenv("OMNI_HOME", str(registry))
    monkeypatch.setenv("LIBRARY_INTERNAL_HOME", str(internal))
    monkeypatch.setenv("ONEX_HOOK_EMIT_JOURNAL_DIR", str(journal))
    return {
        "registry": registry / "omniclaude",
        "internal": internal / "src",
        "unrelated": unrelated,
        "journal": journal,
    }


def _run(
    row: str, client: object, cwd: Path, *, stdin_extra: dict[str, object] | None = None
) -> tuple[int, str, str]:
    hook_input = {
        "session_id": "s-1",
        "hook_event_name": EVENTS[row],
        "cwd": str(cwd),
        "source": "startup",
    } | (stdin_extra or {})
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--binding", row],
        client=client,
        stdin=io.StringIO(json.dumps(hook_input)),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def _journal_records(journal: Path) -> list[dict[str, object]]:
    if not journal.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(journal.glob("*.json"))]


# --- the rows -----------------------------------------------------------------------------


def test_the_three_additive_rows_are_declared_with_their_profile_and_budget() -> None:
    rows = {b.name: b for b in load_client_section().bindings if b.name in ROWS}
    assert tuple(rows) == ROWS
    for name, row in rows.items():
        assert isinstance(row, ModelAdditiveClientBinding)
        assert row.kind is EnumBindingKind.ADDITIVE
        assert (row.hook_event, row.profile) == (EVENTS[name], name)
        assert row.latency_budget_ms == row.deadline_ms == ADDITIVE_BUDGET_CAPS_MS[name]
        profile = yaml.safe_load((PROFILES / f"{name}.yaml").read_text())
        assert profile["binding_kind"] == "additive"
        assert profile["latency_budget_ms"] == row.latency_budget_ms
    assert rows["session_start"].start_scope.value == "workspace"


def test_an_additive_row_without_a_profile_is_refused_by_the_section() -> None:
    with pytest.raises(ValidationError, match="names no profile"):
        ModelClientSection.model_validate(
            {
                "bindings": [
                    {
                        "name": "bare",
                        "kind": "additive",
                        "hook_event": "SessionStart",
                        "command_topic": "onex.cmd.omniintelligence.context-pack-requested.v1",
                        "command_type": "context_pack_requested",
                        "verdict_compute": "client_verdict",
                        "deadline_ms": 100,
                    }
                ]
            }
        )


def test_the_additive_rows_render_one_registration_each_with_the_exact_command() -> (
    None
):
    rows = tuple(b for b in load_client_section().bindings if b.name in ROWS)
    out = HandlerClaudeCodeBindingRender().handle(
        ModelBindingRenderRequest(bindings=rows, max_mode=EnumClientMaxMode.OBSERVE)
    )
    assert [(r.event, r.matcher, r.command) for r in out.registrations] == [
        (EVENTS[n], None, f"onex-claude-code-hook --binding {n}") for n in ROWS
    ]


# --- AC1 (D7) -------------------------------------------------------------------------------


def test_session_start_scope_acts_only_inside_the_registry_or_an_internal_clone(
    world: dict[str, Path],
) -> None:
    orchestrator = _enabled_orchestrator("session_start")
    sent: dict[str, OrchestratorClient] = {}
    outputs: dict[str, str] = {}
    for where in ("registry", "internal", "unrelated"):
        client = sent[where] = OrchestratorClient(orchestrator)
        code, outputs[where], _ = _run("session_start", client, world[where])
        assert code == 0
    # Unrelated directory: nothing printed, nothing sent, nothing journalled.
    assert outputs["unrelated"] == ""
    assert sent["unrelated"].calls == []
    assert _journal_records(world["journal"]) == []
    # The other two: the package is emitted, and it is the same package.
    packages = {}
    for where in ("registry", "internal"):
        assert len(sent[where].calls) == 1
        context = json.loads(outputs[where])["hookSpecificOutput"]
        assert context["hookEventName"] == "SessionStart"
        assert SENTINEL in context["additionalContext"]
        packages[where] = context["additionalContext"]
    assert packages["registry"] == packages["internal"]
    # The inline consumer (what the session-start skill calls) returns that package in all
    # three directories, whatever the hook did there, and does not need the list.
    inline = {}
    for where in ("registry", "internal", "unrelated"):
        built = _enabled_orchestrator().handle(
            ModelContextPackCommand(
                request=ModelContextPackRequest(
                    profile_name="session_start", session_id="s-1"
                ),
                access=ACCESS,
            )
        )
        assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
        inline[where] = "\n\n".join(m.text for m in built.messages)
    assert len(set(inline.values())) == 1
    assert inline["unrelated"] == packages["registry"]


def test_session_start_scope_fails_closed_with_no_registry_or_no_cwd(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = OrchestratorClient(_enabled_orchestrator("session_start"))
    code, out, _ = _run(
        "session_start", client, world["registry"], stdin_extra={"cwd": None}
    )
    assert (code, out, client.calls) == (0, "", [])
    monkeypatch.delenv("OMNI_HOME")
    code, out, _ = _run("session_start", client, world["registry"])
    assert (code, out, client.calls) == (0, "", [])
    assert _journal_records(world["journal"]) == []


# --- AC2 (D8) -------------------------------------------------------------------------------


@pytest.mark.parametrize("row", ROWS)
def test_additive_latency_budget_wait_never_exceeds_the_rows_budget(
    row: str, world: dict[str, Path]
) -> None:
    client = SilentClient()
    ticks = iter((0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1))
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--binding", row],
        client=client,
        stdin=io.StringIO(
            json.dumps({"session_id": "s", "cwd": str(world["registry"])})
        ),
        stdout=out,
        stderr=err,
        clock=lambda: next(ticks),
    )
    assert code == 0
    (call,) = client.calls
    budget = ADDITIVE_BUDGET_CAPS_MS[row]
    # 100 ms of startup were spent; the wait is what the budget leaves, with no exit reserve.
    assert call["deadline_ms"] == budget - 100
    assert call["deadline_ms"] <= budget


def test_additive_latency_budget_per_prompt_returns_within_its_500_ms(
    world: dict[str, Path],
) -> None:
    client = SilentClient(sleep_s=0.5)
    started = time.monotonic()
    code, out, _ = _run("per_prompt", client, world["registry"])
    assert (code, out) == (0, "")
    assert time.monotonic() - started < 0.5 + 0.4


@pytest.mark.parametrize("profile", ROWS)
def test_additive_latency_budget_above_the_cap_is_refused(profile: str) -> None:
    cap = ADDITIVE_BUDGET_CAPS_MS[profile]
    fields = {
        "name": profile,
        "hook_event": EVENTS[profile],
        "profile": profile,
        "command_topic": "onex.cmd.omniintelligence.context-pack-requested.v1",
        "command_type": "context_pack_requested",
        "verdict_compute": "client_verdict",
    }
    ok = ModelAdditiveClientBinding(**fields, latency_budget_ms=cap, deadline_ms=cap)
    assert ok.latency_budget_ms == cap
    with pytest.raises(ValidationError, match="above the"):
        ModelAdditiveClientBinding(
            **fields, latency_budget_ms=cap + 1, deadline_ms=cap + 1
        )
    with pytest.raises(ValidationError, match="equals its latency_budget_ms"):
        ModelAdditiveClientBinding(**fields, latency_budget_ms=cap, deadline_ms=cap - 1)
    with pytest.raises(ValidationError, match="not one of"):
        ModelAdditiveClientBinding(
            **{**fields, "profile": "unlisted"}, latency_budget_ms=1, deadline_ms=1
        )


def test_additive_latency_budget_a_package_after_the_budget_is_never_injected(
    world: dict[str, Path],
) -> None:
    row = next(b for b in load_client_section().bindings if b.name == "agent_spawn")
    text = "late package"
    reply = {"action": "inject", "text": text, "package_sha256": _sha(text)}
    ticks = iter((0.0, 3.5, 3.5))
    handler = HandlerClaudeCodeHookAdapter(
        ReplyClient(reply), clock=lambda: next(ticks)
    )
    result = handler.handle(
        ModelHookAdapterRequest(binding=row, hook_input={"session_id": "s"})
    )
    assert result.hook_output is None
    (record,) = _journal_records(world["journal"])
    assert record["payload"]["reason"] == "timeout"
    assert record["payload"]["elapsed_ms"] == 3500


# --- AC3 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", ROWS)
def test_additive_list_empty_every_row_prints_nothing(
    row: str, world: dict[str, Path]
) -> None:
    runtime = HandlerContextPackOrchestratorRuntime()
    client = OrchestratorClient(runtime)
    code, out, _ = _run(row, client, world["registry"])
    assert (code, out) == (0, "")
    assert "additionalContext" not in out
    assert len(client.calls) == 1  # the call was made and answered none
    assert _journal_records(world["journal"]) == []  # none is not a miss


def test_additive_list_empty_has_a_positive_control_when_the_list_names_the_profile(
    world: dict[str, Path],
) -> None:
    client = OrchestratorClient(_enabled_orchestrator("session_start"))
    code, out, err = _run("session_start", client, world["registry"])
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert SENTINEL in text
    assert f"sha256={_sha(text)} " in err
    # The same list leaves a row it does not name silent.
    code, out, _ = _run("agent_spawn", client, world["registry"])
    assert (code, out) == (0, "")


# --- AC5 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", ROWS)
def test_additive_forced_timeout_prints_nothing_and_journals_one_miss(
    row: str, world: dict[str, Path]
) -> None:
    client = SilentClient()
    code, out, _ = _run(row, client, world["registry"])
    assert (code, out) == (0, "")
    (record,) = _journal_records(world["journal"])
    assert record["event_type"] == MISS_EVENT_TYPE
    assert record["payload"] == {
        "profile": row,
        "reason": "timeout",
        "elapsed_ms": record["payload"]["elapsed_ms"],
    }


def test_additive_forced_timeout_exits_within_the_budget(
    world: dict[str, Path],
) -> None:
    client = SilentClient(sleep_s=0.5)
    started = time.monotonic()
    code, out, _ = _run("per_prompt", client, world["registry"])
    elapsed = time.monotonic() - started
    assert (code, out) == (0, "")
    assert elapsed < 0.5 + 0.4
    (record,) = _journal_records(world["journal"])
    assert record["payload"]["reason"] == "timeout"
    assert 400 <= record["payload"]["elapsed_ms"] <= 900


def test_additive_forced_timeout_a_partial_package_is_never_printed(
    world: dict[str, Path],
) -> None:
    chunk = {"record_id": "r1", "chunk_index": 0, "chunk_total": 2, "data": SENTINEL}
    client = ReplyClient(chunk)
    code, out, _ = _run("agent_spawn", client, world["registry"])
    assert (code, out) == (0, "")
    (record,) = _journal_records(world["journal"])
    assert record["payload"]["reason"] == "timeout"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            {"action": "inject", "text": "pkg", "package_sha256": "0" * 64},
            "unverified",
        ),
        (
            {
                "action": "inject",
                "text": "x" * (ADDITIVE_TEXT_CAP_CHARS + 1),
                "package_sha256": _sha("x" * (ADDITIVE_TEXT_CAP_CHARS + 1)),
            },
            "over_cap",
        ),
    ],
)
def test_additive_forced_timeout_a_package_that_cannot_be_injected_is_one_miss(
    reply: dict[str, JsonValue], reason: str, world: dict[str, Path]
) -> None:
    code, out, _ = _run("agent_spawn", ReplyClient(reply), world["registry"])
    assert (code, out) == (0, "")
    (record,) = _journal_records(world["journal"])
    assert record["payload"]["reason"] == reason


def test_additive_forced_timeout_a_bus_that_fails_is_one_miss_and_exit_zero(
    world: dict[str, Path],
) -> None:
    class Broken:
        def publish_and_await(self, **kwargs: object) -> tuple[ModelReceiptEvent, ...]:
            raise OSError("bus down")

    code, out, _ = _run("session_start", Broken(), world["registry"])
    assert (code, out) == (0, "")
    (record,) = _journal_records(world["journal"])
    assert record["payload"]["reason"] == "unavailable"


# --- AC7 ------------------------------------------------------------------------------------


def test_event_registry_drift_miss_record_carries_the_profile_reason_and_elapsed_only(
    world: dict[str, Path],
) -> None:
    assert set(ModelAdditiveMissRecord.model_fields) == {
        "profile",
        "reason",
        "elapsed_ms",
    }
    with pytest.raises(ValidationError):
        ModelAdditiveMissRecord(
            profile="session_start",
            reason=EnumAdditiveMissReason.TIMEOUT,
            elapsed_ms=1,
            text=SENTINEL,
        )
    with pytest.raises(ValidationError):
        ModelAdditiveMissRecord(
            profile=f"session start {SENTINEL}",
            reason=EnumAdditiveMissReason.TIMEOUT,
            elapsed_ms=1,
        )
    # A package that fails the size cap is journalled without any of its text.
    big = SENTINEL + "x" * ADDITIVE_TEXT_CAP_CHARS
    reply = {"action": "inject", "text": big, "package_sha256": _sha(big)}
    _run("agent_spawn", ReplyClient(reply), world["registry"])
    raw = "".join(p.read_text() for p in world["journal"].glob("*.json"))
    assert raw
    assert SENTINEL not in raw


def test_event_registry_drift_miss_record_is_registered_node_owned_with_its_three_fields() -> (
    None
):
    registry = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
        ).read_text()
    )["events"]
    entry = registry[MISS_EVENT_TYPE]
    assert entry["node_owned"] is True
    assert entry["required_fields"] == list(ModelAdditiveMissRecord.model_fields)
    assert entry["partition_key_field"] == "profile"
    (fan_out,) = entry["fan_out"]
    contract = yaml.safe_load(
        (
            REPO
            / "src/omnimarket/nodes/node_claude_code_hook_adapter_effect/contract.yaml"
        ).read_text()
    )
    assert [e["topic"] for e in contract["published_events"]] == [fan_out["topic"]]
    assert fan_out["topic"] == "onex.evt.omnimarket.additive-pack-missed.v1"
