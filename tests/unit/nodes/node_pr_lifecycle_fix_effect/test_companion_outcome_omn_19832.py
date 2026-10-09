# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The producer publishes the typed companion outcome with its command_id (OMN-19832).

Wave-2 task T10 of the PR landing workflow (epic OMN-19822, plan revision 1,
F5): ``node_pr_lifecycle_fix_effect`` answers every autobind command it consumes
with one ``ModelPrLandingCompanionOutcome`` on
``onex.evt.omnimarket.pr-landing-companion-outcome.v1``, echoing the command's
``command_id`` so the landing reducer can drop an answer to any other command.

Three layers are pinned here: the seam fields, the pure mapping of one fix run
onto one outcome (checked against the T5 marker corpus so the bus surface and
the check-run surface classify a decline identically), and the real runtime
dispatch seam, so the outcome is proven to reach its own topic and not the
terminal one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_pr_lifecycle_fix_effect as fix_effect_pkg
from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOp,
    EnumPrLandingCompanionOutcomeKind,
    ModelPrLandingCompanionOutcome,
)
from omnimarket.events.topics import PR_LANDING_COMPANION_OUTCOME_TOPIC_V1
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.companion_outcome import (
    companion_outcome_for_fix_run,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome import (
    EnumAutobindOutcome,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    companion_outcome_from_autobind_marker,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
    ModelPrLifecycleFixResult,
)

_REPO = "OmniNode-ai/omnimarket"
_HEAD = "a" * 40
_CONTRACT = Path(fix_effect_pkg.__file__).resolve().parent / "contract.yaml"
_CORPUS = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing"
    / "companion_outcome"
    / "marker_lines_2026-09-26.json"
)
_AUTHORED = (
    "authored OCC companion Evidence-Source: OCC#11400 for OMN-19832 on "
    f"{_REPO}#2990 (product head {_HEAD}, branch "
    "auto/omninode-ai-omnimarket-pr-2990-occ-autobind)"
)
_UNVERIFIED = (
    " | OCC companion NOT verified: no OCC companion verifier wired; fail-closed "
    "(cannot prove the companion was pushed)"
)


def _command(
    *,
    op: EnumPrLandingCompanionOp = EnumPrLandingCompanionOp.DERIVE,
    command_id: str | None = "landing-cmd-1",
    block_reason: EnumPrBlockReason = EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=UUID("11111111-2222-3333-4444-555555555555"),
        pr_number=2990,
        repo=_REPO,
        block_reason=block_reason,
        ticket_id="OMN-19832",
        op=op,
        command_id=command_id,
        requested_at=datetime(2026, 9, 27, 7, 0, tzinfo=UTC),
    )


class _Adapter:
    """Records the op it was asked for and returns a fixed producer reason."""

    def __init__(self, action: str = _AUTHORED, *, raises: bool = False) -> None:
        self._action = action
        self._raises = raises
        self.ops: list[object] = []

    async def autobind_evidence_source(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None = None,
        *,
        batch_mode: object = None,
        op: object = None,
    ) -> str:
        del repo, pr_number, ticket_id, batch_mode
        self.ops.append(op)
        if self._raises:
            raise RuntimeError("secret store unreachable: GITHUB_TOKEN")
        return self._action


class _Verifier:
    def __init__(self, *, verified: bool) -> None:
        self._verified = verified

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        del repo, pr_number, ticket_id
        return ModelOccCompanionVerification(verified=self._verified, detail="test")


class _Reporter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


@pytest.fixture
def reporter(monkeypatch: pytest.MonkeyPatch) -> _Reporter:
    recording = _Reporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        recording,
    )
    return recording


def _runtime(
    adapter: _Adapter, *, head: str | None = _HEAD
) -> HandlerPrLifecycleFixRuntime:
    return HandlerPrLifecycleFixRuntime(
        occ_autobind_adapter=adapter,
        outcome_token_resolver=lambda: "ghs_test_token",
        head_sha_resolver=lambda *_args: head,
    )


# ---------------------------------------------------------------------------
# 1. The seam fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSeamFields:
    def test_command_carries_command_id_on_the_companion_route(self) -> None:
        assert _command().command_id == "landing-cmd-1"
        assert _command(command_id=None).command_id is None

    def test_command_id_off_the_companion_route_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="command_id"):
            _command(block_reason=EnumPrBlockReason.CI_FAILURE)

    def test_empty_command_id_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _command(command_id="")

    def test_legacy_payload_without_command_id_still_parses(self) -> None:
        payload = _command(command_id=None).model_dump(mode="json")
        payload.pop("command_id")
        assert ModelPrLifecycleFixCommand.model_validate(payload).command_id is None

    def test_outcome_carries_command_id(self) -> None:
        outcome = ModelPrLandingCompanionOutcome(
            kind=EnumPrLandingCompanionOutcomeKind.MINTED,
            repository=_REPO,
            pr_number=1,
            head_sha=_HEAD,
            occ_pr=7,
            command_id="c1",
        )
        assert outcome.command_id == "c1"
        assert (
            ModelPrLandingCompanionOutcome.model_validate_json(
                outcome.model_dump_json()
            )
            == outcome
        )


# ---------------------------------------------------------------------------
# 2. One fix run -> one outcome
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutcomeMapping:
    def test_error_run_is_error_with_the_command_id(self) -> None:
        outcome = companion_outcome_for_fix_run(
            _command(op=EnumPrLandingCompanionOp.REGENERATE),
            head_sha=_HEAD,
            fix_action="failed: boom",
            error="boom",
            companion_verified=False,
        )
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.ERROR
        assert outcome.error_reason == "boom"
        assert outcome.command_id == "landing-cmd-1"
        assert outcome.op is EnumPrLandingCompanionOp.REGENERATE
        assert outcome.correlation_id == UUID("11111111-2222-3333-4444-555555555555")

    def test_unverified_authored_mint_is_minted_with_an_unknown_stamp(self) -> None:
        """The marker says DECLINED here; the typed outcome says what happened."""
        outcome = companion_outcome_for_fix_run(
            _command(),
            head_sha=_HEAD,
            fix_action=_AUTHORED + _UNVERIFIED,
            error=None,
            companion_verified=False,
        )
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.MINTED
        assert outcome.occ_pr == 11400
        assert outcome.stamped is None
        assert outcome.armed is None
        assert outcome.conflicting is None

    def test_verified_mint_is_stamped(self) -> None:
        outcome = companion_outcome_for_fix_run(
            _command(),
            head_sha=_HEAD,
            fix_action=_AUTHORED,
            error=None,
            companion_verified=True,
        )
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.MINTED
        assert outcome.stamped is True

    @pytest.mark.parametrize(
        ("action", "code", "occ_pr", "stamped"),
        [
            (
                f"no-op: {_REPO}#2990 already bound to OCC#55 (Evidence-Source "
                "already an OCC source)",
                EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
                55,
                True,
            ),
            (
                f"skip:LEASE_HELD — {_REPO}#2990@aaaaaaaa companion already being "
                "minted by another producer (OMN-14793 / OMN-14783)",
                EnumPrLandingCompanionDeclineCode.LEASE_HELD,
                None,
                None,
            ),
            (
                "skip:TICKET_LEASE_HELD — OMN-19832 batch companion lease remained "
                "held by another producer",
                EnumPrLandingCompanionDeclineCode.TICKET_LEASE_HELD,
                None,
                None,
            ),
            (
                "something this seam has never seen",
                EnumPrLandingCompanionDeclineCode.UNCLASSIFIED,
                None,
                None,
            ),
        ],
    )
    def test_every_other_return_is_a_typed_decline(
        self,
        action: str,
        code: EnumPrLandingCompanionDeclineCode,
        occ_pr: int | None,
        stamped: bool | None,
    ) -> None:
        outcome = companion_outcome_for_fix_run(
            _command(),
            head_sha=_HEAD,
            fix_action=action,
            error=None,
            companion_verified=False,
        )
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
        assert outcome.decline_code is code
        assert outcome.occ_pr == occ_pr
        assert outcome.stamped is stamped
        assert outcome.command_id == "landing-cmd-1"

    def test_decline_codes_agree_with_the_t5_marker_reader_over_the_corpus(
        self,
    ) -> None:
        """Both surfaces classify one producer reason identically.

        Every marker line recorded on 2026-09-26 is replayed through the typed
        builder. The single deliberate difference is the unverified mint, which
        the marker posts as DECLINED/AUTHORED_UNVERIFIED and the bus posts as
        MINTED; every other line must agree on kind, code, occ_pr and stamp.
        """
        records = json.loads(_CORPUS.read_text(encoding="utf-8"))["records"]
        assert len(records) > 100  # positive control: the corpus loaded
        compared = minted = 0
        for record in records:
            marker = companion_outcome_from_autobind_marker(
                record["marker_line"], head_sha=record["head_sha"]
            )
            reason = record["marker_line"].split(" reason=", 1)[1]
            typed = companion_outcome_for_fix_run(
                _command(),
                head_sha=record["head_sha"],
                fix_action=reason,
                error=(
                    reason
                    if marker.kind is EnumPrLandingCompanionOutcomeKind.ERROR
                    else None
                ),
                companion_verified=False,
            )
            if (
                marker.decline_code
                is EnumPrLandingCompanionDeclineCode.AUTHORED_UNVERIFIED
            ):
                minted += 1
                assert typed.kind is EnumPrLandingCompanionOutcomeKind.MINTED
                assert typed.occ_pr == marker.occ_pr
                continue
            compared += 1
            assert typed.kind is marker.kind, record["marker_line"]
            assert typed.decline_code is marker.decline_code, record["marker_line"]
            assert typed.occ_pr == marker.occ_pr, record["marker_line"]
            assert typed.stamped == marker.stamped, record["marker_line"]
        assert compared > 0
        assert minted > 0


# ---------------------------------------------------------------------------
# 3. The handler: one run, one marker, one typed outcome, one head
# ---------------------------------------------------------------------------


def _events(output: object) -> list[object]:
    return list(cast("Any", output).events)


@pytest.mark.unit
class TestRuntimeHandlerEmitsTheOutcome:
    async def test_regenerate_reaches_the_producer_and_is_answered_by_command_id(
        self, reporter: _Reporter
    ) -> None:
        adapter = _Adapter(_AUTHORED + _UNVERIFIED)
        output = await _runtime(adapter).handle(
            _command(op=EnumPrLandingCompanionOp.REGENERATE)
        )

        assert adapter.ops == [EnumPrLandingCompanionOp.REGENERATE]
        events = _events(output)
        assert [type(e) for e in events] == [
            ModelPrLifecycleFixResult,
            ModelPrLandingCompanionOutcome,
        ]
        outcome = cast("ModelPrLandingCompanionOutcome", events[1])
        assert outcome.command_id == "landing-cmd-1"
        assert outcome.op is EnumPrLandingCompanionOp.REGENERATE
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.MINTED
        assert outcome.head_sha == _HEAD

        # The check-run marker is still posted, on the same head.
        assert len(reporter.calls) == 1
        assert reporter.calls[0]["head_sha"] == _HEAD
        assert reporter.calls[0]["outcome"] is EnumAutobindOutcome.MINTED

    async def test_a_raising_producer_is_answered_with_error(
        self, reporter: _Reporter
    ) -> None:
        output = await _runtime(_Adapter(raises=True)).handle(_command())
        outcome = cast("ModelPrLandingCompanionOutcome", _events(output)[1])
        assert outcome.kind is EnumPrLandingCompanionOutcomeKind.ERROR
        assert outcome.command_id == "landing-cmd-1"
        assert "secret store unreachable" in (outcome.error_reason or "")
        assert reporter.calls[0]["outcome"] is EnumAutobindOutcome.ERROR

    async def test_a_push_driven_command_is_answered_with_no_command_id(
        self, reporter: _Reporter
    ) -> None:
        output = await _runtime(_Adapter()).handle(_command(command_id=None))
        outcome = cast("ModelPrLandingCompanionOutcome", _events(output)[1])
        assert outcome.command_id is None
        assert outcome.op is EnumPrLandingCompanionOp.DERIVE

    async def test_no_head_means_no_typed_outcome_but_the_marker_still_posts(
        self, reporter: _Reporter
    ) -> None:
        output = await _runtime(_Adapter(), head=None).handle(_command())
        assert [type(e) for e in _events(output)] == [ModelPrLifecycleFixResult]
        assert len(reporter.calls) == 1
        assert reporter.calls[0]["head_sha"] is None

    async def test_a_non_companion_command_has_no_outcome_and_no_marker(
        self, reporter: _Reporter
    ) -> None:
        output = await _runtime(_Adapter()).handle(
            _command(block_reason=EnumPrBlockReason.CI_FAILURE, command_id=None)
        )
        assert [type(e) for e in _events(output)] == [ModelPrLifecycleFixResult]
        assert reporter.calls == []

    async def test_the_merge_sweep_path_is_unchanged(self, reporter: _Reporter) -> None:
        """``HandlerPrLifecycleFix.handle`` still returns the bare result."""
        handler = HandlerPrLifecycleFix(
            occ_autobind_adapter=_Adapter(),
            occ_companion_verifier=_Verifier(verified=True),
            outcome_token_resolver=lambda: "ghs_test_token",
        )
        result = await handler.handle(_command())
        assert isinstance(result, ModelPrLifecycleFixResult)
        assert reporter.calls[0]["outcome"] is EnumAutobindOutcome.MINTED
        assert reporter.calls[0]["head_sha"] is None  # the reporter reads it itself


# ---------------------------------------------------------------------------
# 4. The contract and the real dispatch seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_the_outcome_topic_and_routes_both_events() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    publish_topics = contract["event_bus"]["publish_topics"]
    assert PR_LANDING_COMPANION_OUTCOME_TOPIC_V1 in publish_topics
    assert contract["terminal_event"] in publish_topics
    routes = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert routes == {
        "PrLifecycleFixResult": contract["terminal_event"],
        "PrLandingCompanionOutcome": PR_LANDING_COMPANION_OUTCOME_TOPIC_V1,
    }


@pytest.mark.unit
async def test_real_dispatch_seam_publishes_the_outcome_on_its_own_topic(
    reporter: _Reporter,
) -> None:
    """engine.dispatch -> the production adapter -> handle -> applier.apply.

    The same seam the effects runtime wires from this contract. RED before
    OMN-19832: nothing reached the outcome topic.
    """
    from omnibase_core.enums.enum_node_kind import EnumNodeKind
    from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
    from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
    from omnibase_infra.protocols import ProtocolEventBusLike
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.event_bus_subcontract_wiring import (
        load_published_events_map,
    )
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entry = contract["handler_routing"]["handlers"][0]
    command_topic = "onex.cmd.omnimarket.occ-autobind.v1"
    assert command_topic in contract["event_bus"]["subscribe_topics"]
    published_map = load_published_events_map(_CONTRACT)

    handler = _runtime(_Adapter(_AUTHORED + _UNVERIFIED))
    dispatcher = _make_dispatch_callback(
        handler,
        ModelHandlerRef(
            name=entry["event_model"]["name"],
            module=entry["event_model"]["module"],
        ),
        handler_node_kind=EnumNodeKind.EFFECT,
        published_event_names=frozenset(published_map),
    )
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="node_pr_lifecycle_fix_effect.autobind",
        dispatcher=dispatcher,
        category=EnumMessageCategory.COMMAND,
        message_types={entry["event_model"]["name"]},
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="node_pr_lifecycle_fix_effect.autobind.route",
            topic_pattern=command_topic,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="node_pr_lifecycle_fix_effect.autobind",
        )
    )
    engine.freeze()

    correlation_id = uuid4()
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=_command(op=EnumPrLandingCompanionOp.REGENERATE),
        correlation_id=correlation_id,
        event_type=entry["event_model"]["name"],
    )
    dispatch_result = await engine.dispatch(topic=command_topic, envelope=envelope)
    assert dispatch_result.status == EnumDispatchStatus.SUCCESS, (
        dispatch_result.error_message
    )

    published: list[tuple[str, Any]] = []

    class _Bus:
        async def publish_envelope(
            self, *, envelope: object, topic: str, key: bytes | None = None
        ) -> None:
            del key
            published.append((topic, envelope))

    applier = DispatchResultApplier(
        event_bus=cast("ProtocolEventBusLike", _Bus()),
        output_topic=contract["terminal_event"],
        output_topic_map=published_map,
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )
    await applier.apply(dispatch_result, correlation_id)

    by_topic = dict(published)
    assert sorted(by_topic) == sorted(
        [contract["terminal_event"], PR_LANDING_COMPANION_OUTCOME_TOPIC_V1]
    ), published
    payload = by_topic[PR_LANDING_COMPANION_OUTCOME_TOPIC_V1].payload
    assert isinstance(payload, ModelPrLandingCompanionOutcome)
    assert payload.command_id == "landing-cmd-1"
    assert payload.op is EnumPrLandingCompanionOp.REGENERATE
    assert isinstance(
        by_topic[contract["terminal_event"]].payload, ModelPrLifecycleFixResult
    )
