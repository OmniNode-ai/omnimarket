# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn every raw lane work result into a named lane work event, or an acknowledgement.

Pure: no clock, no I/O beyond reading the overlay, no model. Worker text (guard,
hook, commit and push output) is mapped by the overlay's ordered pattern table,
and the first rule found decides; a text no rule finds is ``unclassified``, which
is never a success. Typed results (ledger query, PR observation, lab work unit,
ledger append, GitHub write, PR handoff answer, delegation harness result) are
mapped by field and by the action id they answer. A PR observation made before the
workflow's last push or head change is dropped, whatever head it names. A ledger
append completion is an acknowledgement and returns ``None``, never an event. A
delegation answer that does not parse into the help answer model, or whose attempt
record names another backend or model than the tier, is ``help_failed``. The red
kind of a log read comes from ``node_ci_red_kind_rule_compute``. Class names and
patterns are overlay data; nothing here names one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.models.lane_work import (
    ModelLaneHelpAnswer,
    ModelLaneHelpTier,
    ModelLaneWorkEvent,
    ModelLaneWorkState,
)
from omnibase_core.models.lane_work.model_lane_work_action import build_action_id
from pydantic import AwareDatetime, TypeAdapter, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallResult,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_answer import (
    ModelCiRedKindRuleAnswer,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_answer import (
    ModelLaneWorkClassifyAnswer,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_overlay import (
    ModelLaneWorkClassifyOverlay,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_request import (
    RAW_TEXT,
    ModelLaneWorkClassifyRequest,
)

OVERLAY_NODE_NAME = "node_lane_work_event_classify_compute"
OVERLAY_FILE_ENV = "LANE_WORK_EVENT_CLASSIFY_OVERLAY"
_NODE_DIR = Path(__file__).resolve().parents[1]
_HEX_TOKEN = re.compile(r"\b[0-9a-f]{7,40}\b")
_AWARE = TypeAdapter(AwareDatetime)
_E = EnumLaneWorkEvent


def load_lane_work_classify_overlay() -> ModelLaneWorkClassifyOverlay:
    """The bound overlay; when none is bound, the bootstrap file beside this node."""
    try:
        return load_structured_overlay(
            OVERLAY_NODE_NAME,
            ModelLaneWorkClassifyOverlay,
            file_env=OVERLAY_FILE_ENV,
        )
    except NodeOverlayNotFoundError:
        return ModelLaneWorkClassifyOverlay.model_validate(
            yaml.safe_load((_NODE_DIR / "overlay.example.yaml").read_text())
        )


def _same_head(left: str, right: str) -> bool:
    """Heads compare by prefix, since observations and receipts abbreviate."""
    left, right = left.lower(), right.lower()
    return min(len(left), len(right)) >= 7 and (
        left.startswith(right) or right.startswith(left)
    )


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _AWARE.validate_python(value)
    except ValidationError:
        return None


class HandlerLaneWorkEventClassify:
    """Classify one raw or typed result into a ``ModelLaneWorkEvent`` against a workflow's state."""

    _ACK_KIND = "ledger_append_result"

    def __init__(
        self,
        load_overlay: Callable[
            [], ModelLaneWorkClassifyOverlay
        ] = load_lane_work_classify_overlay,
        red_kind_rule: HandlerCiRedKindRule | None = None,
    ) -> None:
        self._load_overlay = load_overlay
        self._red_kind_rule = red_kind_rule or HandlerCiRedKindRule()
        self._typed: dict[
            str,
            Callable[
                [Mapping[str, Any], ModelLaneWorkState, str | None],
                ModelLaneWorkEvent | None,
            ],
        ] = {
            "ledger_query_result": self._ledger_query,
            self._ACK_KIND: self._acknowledge,
            "pr_observation": self._pr_observation,
            "lab_work_unit_result": self._lab_work_unit,
            "github_write_result": self._github_write,
            "handoff_answer": self._handoff,
            "delegation_harness_result": self._delegation,
        }

    def handle(
        self, request: ModelLaneWorkClassifyRequest
    ) -> ModelLaneWorkClassifyAnswer:
        """The node's canonical entry: one request, one answer."""
        event: ModelLaneWorkEvent | None
        if request.result_kind == RAW_TEXT:
            event = self.classify(
                str(request.raw_result), request.state, request.input_id
            )
        else:
            event = self.classify_typed(
                request.result_kind,
                request.payload or {},
                request.state,
                request.input_id,
            )
        if event is not None:
            return ModelLaneWorkClassifyAnswer(outcome="event", event=event)
        outcome = "acknowledged" if request.result_kind == self._ACK_KIND else "dropped"
        return ModelLaneWorkClassifyAnswer(outcome=outcome)

    # ------------------------------------------------------------------ text

    def classify(
        self,
        raw_result: str,
        state: ModelLaneWorkState,
        input_id: str | None = None,
    ) -> ModelLaneWorkEvent:
        """Name worker text by the first overlay rule found; no rule found is ``unclassified``.

        ``input_id`` defaults to a digest of the workflow id, its ``seq`` and the text, so a
        redelivery of one fold's input dedupes and the same refusal in a later fold counts again.
        """
        overlay = self._load_overlay()
        key = input_id or self._digest(state, raw_result)
        evidence = (raw_result[: overlay.evidence_limit],)
        stamp = (
            re.compile(overlay.log_timestamp_pattern)
            if overlay.log_timestamp_pattern
            else None
        )
        first_line = raw_result.lstrip().split("\n", 1)[0]
        is_log = bool(stamp and stamp.match(first_line))
        lines = (
            "\n".join(stamp.sub("", line, count=1) for line in raw_result.split("\n"))
            if stamp
            else raw_result
        )
        for rule in overlay.rules:
            if rule.source is not None and (rule.source == "ci_job_log") is not is_log:
                continue
            flags = re.MULTILINE | (re.IGNORECASE if rule.ignore_case else 0)
            if not re.search(rule.pattern, lines, flags):
                continue
            extra: dict[str, Any] = {}
            if rule.event is _E.HEAD_MOVED:
                head = state.expected_head or self._first_head(raw_result)
                if head is None:
                    return self._unclassified(
                        state, key, evidence, "head_moved names no head"
                    )
                extra["head_sha"] = head
            elif rule.event is _E.LOG_READ:
                red_kind = rule.red_kind or self._red_kind(
                    rule.red_kind_check_name, raw_result
                )
                if red_kind is None:
                    return self._unclassified(
                        state, key, evidence, "no red kind rule names the log"
                    )
                extra["red_kind"] = red_kind
            if rule.event_class is not None:
                extra["event_class"] = rule.event_class
            return self._build(rule.event, state, key, evidence, **extra)
        return self._unclassified(state, key, evidence, "no rule matches")

    # ----------------------------------------------------------------- typed

    def classify_typed(
        self,
        kind: str,
        payload: Mapping[str, Any],
        state: ModelLaneWorkState,
        input_id: str | None = None,
    ) -> ModelLaneWorkEvent | None:
        """Map a typed result by its kind and fields; ``None`` is an acknowledgement or a dropped stale read."""
        key = input_id or self._digest(
            state, json.dumps(payload, sort_keys=True, default=str) + kind
        )
        mapper = self._typed.get(kind)
        if mapper is None:
            return self._unclassified(
                state, key, (f"typed result kind {kind!r}",), "unknown typed result"
            )
        return mapper(payload, state, key)

    def _acknowledge(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> None:
        """A ledger append completion clears its outstanding action; it is never an event."""
        return

    def _ledger_query(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent:
        pr_ref = f"{state.change.repo}#{state.change.pr_number}"
        lane = payload.get("lane") or state.worker_lane
        for claim in payload.get("claims") or ():
            if not claim.get("open"):
                continue
            if lane is not None and claim.get("lane") != lane:
                continue
            names_ticket = (
                state.change.ticket is not None
                and claim.get("ticket") == state.change.ticket
            )
            names_pr = str(claim.get("pr", "")).endswith(pr_ref)
            if names_ticket or names_pr:
                return self._build(_E.CLAIM_CONFIRMED, state, str(key), (str(claim),))
        return self._build(
            _E.CLAIM_MISSING, state, str(key), (f"no open claim names {pr_ref}",)
        )

    def _pr_observation(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent | None:
        observed_at = _when(payload.get("observed_at"))
        ci_read_at = _when(payload.get("ci_read_at"))
        if self._stale(payload, state, observed_at, ci_read_at):
            return None
        head = str(payload.get("head_sha", "")).lower()
        evidence = (f"pr_observation {payload.get('pr', '')} head {head}",)
        if state.expected_head is not None and not _same_head(
            head, state.expected_head
        ):
            return self._build(_E.HEAD_MOVED, state, str(key), evidence, head_sha=head)
        overlay = self._load_overlay()
        event = overlay.ci_verdict_events.get(str(payload.get("ci_verdict")))
        if event is None:
            return self._unclassified(
                state, str(key), evidence, "ci_verdict names no check event"
            )
        return self._build(
            event,
            state,
            str(key),
            evidence,
            head_sha=head,
            observed_at=observed_at,
            ci_read_at=ci_read_at,
        )

    @staticmethod
    def _stale(
        payload: Mapping[str, Any],
        state: ModelLaneWorkState,
        observed_at: datetime | None,
        ci_read_at: datetime | None,
    ) -> bool:
        """Made before the last push or head change; proof of freshness is owed once one exists."""
        if payload.get("observed_after_last_push") is False:
            return True
        last = state.last_head_change_at
        if last is None:
            return False
        stamps = [t for t in (observed_at, ci_read_at) if t is not None]
        if payload.get("observed_after_last_push") is True and not stamps:
            return False
        return not stamps or any(t <= last for t in stamps)

    def _lab_work_unit(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent:
        head = str(payload.get("head_sha") or state.expected_head or "")
        extra: dict[str, Any] = {
            "action_id": self._action_id(payload, state),
            "head_sha": head or None,
        }
        evidence = (
            f"lab work unit {payload.get('status')} exit {payload.get('exit_code')}",
        )
        if payload.get("status") == "completed" and payload.get("exit_code") == 0:
            return self._build(_E.PROOF_PASSED, state, str(key), evidence, **extra)
        proof_class = "tests" if payload.get("status") == "completed" else "infra"
        return self._build(
            _E.PROOF_FAILED,
            state,
            str(key),
            evidence,
            event_class=proof_class,
            action_id=extra["action_id"],
        )

    def _github_write(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent:
        kind = (
            _E.PROOF_POSTED
            if payload.get("status") == "completed"
            else _E.PROOF_POST_FAILED
        )
        return self._build(
            kind,
            state,
            str(key),
            (f"github write {payload.get('status')}",),
            action_id=self._action_id(payload, state),
        )

    def _handoff(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent:
        outcome = payload.get("outcome")
        evidence = (f"handoff {outcome}",)
        kinds = {"handed_off": _E.HANDOFF_WRITTEN, "failed": _E.HANDOFF_FAILED}
        if outcome not in kinds:
            return self._unclassified(
                state,
                str(key),
                evidence,
                "handoff outcome is neither handed_off nor failed",
            )
        return self._build(
            kinds[outcome],
            state,
            str(key),
            evidence,
            action_id=self._action_id(payload, state),
            head_sha=payload.get("head_sha"),
            request_id=payload.get("request_id"),
        )

    def _delegation(
        self, payload: Mapping[str, Any], state: ModelLaneWorkState, key: str | None
    ) -> ModelLaneWorkEvent:
        action_id = self._action_id(payload, state)
        rest = {k: v for k, v in payload.items() if k != "action_id"}

        def failed(why: str) -> ModelLaneWorkEvent:
            return self._build(
                _E.HELP_FAILED, state, str(key), (why,), action_id=action_id
            )

        try:
            result = ModelHarnessBackendCallResult.model_validate(rest)
        except ValidationError:
            return failed("the delegation result does not parse")
        if result.status != "completed" or not result.ok:
            return failed(f"the delegation ended {result.status}: {result.detail}")
        tier = self._tier(state)
        if tier is None:
            return failed("the workflow state holds no help tier for this climb")
        attempt = self._answering_attempt(result)
        if attempt is None:
            return failed("the result carries no attempt record")
        if attempt.backend_id != tier.backend_id or attempt.model_name != tier.model:
            return failed(
                f"attempt ran {attempt.backend_id}/{attempt.model_name}, "
                f"the tier names {tier.backend_id}/{tier.model}"
            )
        try:
            answer = ModelLaneHelpAnswer.model_validate(json.loads(result.content))
        except (ValueError, ValidationError):
            return failed("the answer does not parse into the help answer model")
        evidence = tuple(
            text for text in (answer.cause, answer.correction) if text is not None
        ) or (answer.kind,)
        return self._build(
            _E(answer.kind),
            state,
            str(key),
            evidence,
            action_id=action_id,
            help_origin=answer.help_origin,
            reason=answer.reason,
            decision_needed=answer.decision_needed,
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _answering_attempt(
        result: ModelHarnessBackendCallResult,
    ) -> ModelHarnessAttemptRecord | None:
        accepted = [a for a in result.attempts if a.accepted]
        if accepted:
            return accepted[-1]
        return result.attempts[-1] if result.attempts else None

    @staticmethod
    def _tier(state: ModelLaneWorkState) -> ModelLaneHelpTier | None:
        if state.help_tier_index < len(state.help_tiers):
            return state.help_tiers[state.help_tier_index]
        return None

    @staticmethod
    def _action_id(payload: Mapping[str, Any], state: ModelLaneWorkState) -> str:
        """The action the result answers: named by it, else the one outstanding, else the one this fold issued."""
        named = payload.get("action_id")
        if named:
            return str(named)
        if len(state.outstanding_action_ids) == 1:
            return state.outstanding_action_ids[0]
        return build_action_id(state.workflow_id, state.seq, 0)

    @staticmethod
    def _first_head(text: str) -> str | None:
        found = _HEX_TOKEN.search(text)
        return found.group(0) if found else None

    @staticmethod
    def _digest(state: ModelLaneWorkState, text: str) -> str:
        material = f"{state.workflow_id}|{state.seq}|{text}"
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    def _red_kind(self, check_name: str | None, log: str) -> str | None:
        request = ModelCiRedKindRuleRequest(
            check_name=check_name or "worker-log-read", log_excerpt=log
        )
        answer: ModelCiRedKindRuleAnswer = self._red_kind_rule.handle(request)
        if not answer.ok:
            # The red kind node has no overlay bound: use the bootstrap rules this overlay carries.
            bootstrap = self._load_overlay().red_kind_bootstrap
            if bootstrap is None:
                return None
            answer = HandlerCiRedKindRule(lambda: bootstrap).handle(request)
        return answer.kind if answer.matched else None

    def _build(
        self,
        kind: EnumLaneWorkEvent,
        state: ModelLaneWorkState,
        key: str,
        evidence: tuple[str, ...],
        **fields: Any,
    ) -> ModelLaneWorkEvent:
        fields = {k: v for k, v in fields.items() if v is not None}
        try:
            return ModelLaneWorkEvent(
                kind=kind,
                workflow_id=state.workflow_id,
                input_id=key,
                change=state.change,
                evidence=evidence,
                **fields,
            )
        except ValidationError as exc:
            return self._unclassified(
                state,
                key,
                evidence,
                f"{kind.value} is not a valid event: {exc.error_count()} field error(s)",
            )

    @staticmethod
    def _unclassified(
        state: ModelLaneWorkState, key: str, evidence: tuple[str, ...], why: str
    ) -> ModelLaneWorkEvent:
        return ModelLaneWorkEvent(
            kind=_E.UNCLASSIFIED,
            workflow_id=state.workflow_id,
            input_id=key,
            change=state.change,
            evidence=(*evidence, why),
        )


__all__: list[str] = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_NODE_NAME",
    "HandlerLaneWorkEventClassify",
    "load_lane_work_classify_overlay",
]
