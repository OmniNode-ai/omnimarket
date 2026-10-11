# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B Linear-comments sweep: typed command in, typed terminal out.

Collects every comment addressed to the operator since an exact cutoff, asks the
decision compute node which of them get a draft, drafts each through the
delegation orchestrator, refuses a draft that cites outside its sources, has the
verifier check what is left against the facts table, then writes the report and
the metrics rows. Model calls and decisions cross the injected bus gateways; the
deployment's briefs, directories and standing rules arrive through a strict
overlay, and the phase schemas are packaged with the node. Read-only on Linear.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from importlib.resources import files
from pathlib import Path
from typing import cast

import yaml
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import JsonValue, ValidationError

from ..models.model_linear_comments_sweep import (
    ModelLinearCommentsSweepRequest,
    ModelLinearCommentsSweepResult,
    ModelSweepCollected,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
    ModelSweepVerdict,
    ModelSweepWritten,
)
from ..models.model_sweep_overlay import ModelSweepOverlay
from ..protocols.protocol_sweep_gateways import (
    ProtocolSweepDecisionGateway,
    ProtocolSweepPhaseGateway,
)

PACKAGE = "omnimarket.nodes.node_linear_comments_sweep_orchestrator"
NODE_NAME = "node_linear_comments_sweep_orchestrator"
OVERLAY_ENV = "OMNIMARKET_LINEAR_COMMENTS_SWEEP_OVERLAY"
ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
# The retry after a response cut off by its token limit names this one larger limit.
RETRY_MAX_TOKENS = 16384
DRAFT_MAX_TOKENS = 2048
FENCE = re.compile(r"\A\s*```[a-z]*\s*\n?(.*?)\n?```\s*\Z", re.S | re.I)


class LinearCommentsSweepConfigurationError(ValueError):
    """Missing or invalid deployment overlay."""


def load_sweep_overlay() -> ModelSweepOverlay:
    """Read the explicit pointer, otherwise the first existing overlay in root order.

    The node cannot run without its overlay: an absent one is a hard stop, never
    a run against empty briefs. An explicit pointer never falls through.
    """
    if OVERLAY_ENV in os.environ:
        pointer = os.environ[OVERLAY_ENV]
        if not pointer:
            raise LinearCommentsSweepConfigurationError(
                f"{OVERLAY_ENV}: empty overlay pointer"
            )
        path: Path | None = Path(pointer)
        source = OVERLAY_ENV
    else:
        path = None
        source = ROOTS_ENV
        for root in os.environ.get(ROOTS_ENV, "").split(os.pathsep):
            if not root:
                continue
            candidate = Path(root) / NODE_NAME / "overlay.yaml"
            if candidate.exists():
                path = candidate
                break
    if path is None:
        raise LinearCommentsSweepConfigurationError(
            f"no linear comments sweep overlay: set {OVERLAY_ENV} to an overlay.yaml, "
            f"or put {NODE_NAME}/overlay.yaml under a root in {ROOTS_ENV}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("overlay must be a mapping")
        briefs = path.parent / str(raw.get("briefs", ""))
        if not raw.get("briefs") or not briefs.is_file():
            raise ValueError("overlay briefs file is not readable")
        return ModelSweepOverlay.from_documents(
            {k: v for k, v in raw.items() if k != "briefs"},
            briefs.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise LinearCommentsSweepConfigurationError(
            f"{source}: invalid linear comments sweep overlay: {exc}"
        ) from None


def parse_draft(content: str) -> dict[str, JsonValue] | None:
    """The delegated response as the requested JSON object; a fence is stripped, nothing is edited."""
    text = content.strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return cast(dict[str, JsonValue], decoded) if isinstance(decoded, dict) else None


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


class HandlerLinearCommentsSweep:
    """Coordinate the existing delegation pipeline and decision node, preserving the sweep's gates."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None,
        gateway: ProtocolSweepPhaseGateway | None = None,
        decisions: ProtocolSweepDecisionGateway | None = None,
        overlay: ModelSweepOverlay | None = None,
    ) -> None:
        from .handler_sweep_bus import HandlerSweepBus

        # OMN-17427: the overlay is a deployment fact of a RUN. Auto-wiring builds
        # this handler inside the effects runtime, and a constructor that raises
        # there takes every other node's runtime down with it, so nothing here
        # touches the deployment: an absent overlay is refused when a run starts.
        self._overlay = overlay
        bus = (
            HandlerSweepBus(event_bus) if gateway is None or decisions is None else None
        )
        self.gateway = gateway or cast(HandlerSweepBus, bus)
        self.decisions = decisions or cast(HandlerSweepBus, bus)
        self.schemas = cast(
            dict[str, dict[str, JsonValue]],
            json.loads(
                files(PACKAGE).joinpath("models/phase_schemas.json").read_text()
            ),
        )

    @property
    def overlay(self) -> ModelSweepOverlay:
        """The deployment overlay, read on first use; absent or invalid is a refusal."""
        if self._overlay is None:
            self._overlay = load_sweep_overlay()
        return self._overlay

    async def handle(
        self, request: ModelLinearCommentsSweepRequest
    ) -> ModelLinearCommentsSweepResult:
        # Refuse a run without its overlay before any phase or decision runs.
        overlay = self.overlay
        slots = {
            **overlay.values,
            "since": request.since,
            "facts": request.facts,
            "report_path": self._report_path(request),
        }

        collected = await self._collect(request, slots)
        decided = await self.decisions.decide(
            request, {"kind": "decide_items", "items": list(collected.items)}
        )
        decisions = cast(list[dict[str, JsonValue]], decided["decisions"])
        selected = set(cast(list[str], decided["selected_comment_ids"]))
        needing = [i for i in collected.items if i.get("comment_id") in selected]
        refused = [d for d in decisions if not d["draft"]]
        refused_by_class = dict(Counter(_text(d["reason_class"]) for d in refused))

        outcomes = await asyncio.gather(
            *(self._draft_one(request, slots, item) for item in needing)
        )
        good = [o["draft"] for o in outcomes if o.get("draft") is not None]
        metrics = [o["metrics"] for o in outcomes if o.get("metrics") is not None]
        held_raw = [o["held"] for o in outcomes if o.get("held") is not None]
        classes = (
            cast(
                list[str],
                (
                    await self.decisions.decide(
                        request,
                        {
                            "kind": "classify_held",
                            "held": [
                                {
                                    "stage": h["stage"],
                                    "reason": h["reason"],
                                    "truncated": h["truncated"],
                                }
                                for h in cast(list[dict[str, JsonValue]], held_raw)
                            ],
                        },
                    )
                )["held_classes"],
            )
            if held_raw
            else []
        )
        held = [
            {**cast(dict[str, JsonValue], h), "reason_class": c}
            for h, c in zip(held_raw, classes, strict=True)
        ]
        tally = await self.decisions.decide(
            request,
            {
                "kind": "tally",
                "held": [
                    {
                        "stage": h["stage"],
                        "reason": h["reason"],
                        "truncated": h["truncated"],
                        "reason_class": h["reason_class"],
                    }
                    for h in held
                ],
                "needing_count": len(needing),
                "accepted_count": len(good),
            },
        )
        written = await self._write(
            request,
            slots,
            collected,
            good,
            held,
            refused,
            metrics,
        )
        return ModelLinearCommentsSweepResult(
            correlation_id=request.correlation_id,
            tenant_id=request.tenant_id,
            linear_file=slots["report_path"],
            metrics_file=overlay.values["metrics_path"],
            since=request.since,
            collected=len(collected.items),
            needing=len(needing),
            refused=len(refused),
            refused_by_class=refused_by_class,
            accepted=len(good),
            held=len(held),
            held_by_class=cast(dict[str, int], tally["held_by_class"]),
            degraded=cast(bool, tally["degraded"]),
            control=collected.positive_control,
            written=written,
        )

    def _report_path(self, request: ModelLinearCommentsSweepRequest) -> str:
        stamp = request.since.replace(":", "")
        return f"{self.overlay.values['report_dir']}/linear-comment-replies-{stamp}.md"

    def _schema(self, name: str) -> dict[str, JsonValue]:
        return self.schemas[name]

    @staticmethod
    def _render(text: str, values: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match[1]
            if key not in values:
                raise ValueError(f"unknown linear comments prompt field: {key}")
            return values[key]

        # Replace in one pass: substitutions can contain literal shell syntax.
        return re.sub(r"@@(\w+)@@", replace, text)

    async def _collect(
        self, request: ModelLinearCommentsSweepRequest, slots: dict[str, str]
    ) -> ModelSweepCollected:
        reply = await self.gateway.run(
            request,
            ModelSweepPhaseRequest(
                label="collect:linear",
                phase="Collect",
                task_type="agent_delegation",
                model="sonnet",
                effort="medium",
                prompt=self._render(
                    self.overlay.templates["linear-comments-collect"], slots
                ),
                timeout_seconds=3600,
                schema_definition=self._schema("COLLECT"),
            ),
        )
        if not reply.ok:
            raise ValueError(f"collect phase failed: {reply.failure}")
        try:
            return ModelSweepCollected.model_validate_json(reply.content)
        except ValidationError as exc:
            raise ValueError(
                "collect phase result is off its schema or names no positive control: "
                f"{exc.error_count()} problem(s)"
            ) from None

    async def _draft_call(
        self,
        request: ModelLinearCommentsSweepRequest,
        item: dict[str, JsonValue],
        prompt: str,
        max_tokens: int,
        suffix: str,
    ) -> ModelSweepPhaseReply:
        return await self.gateway.run(
            request,
            ModelSweepPhaseRequest(
                label=f"delegate:{_text(item.get('ticket'))}:{_text(item.get('comment_id'))}{suffix}",
                phase="Draft",
                task_type=request.task_type,
                prompt=prompt,
                max_tokens=max_tokens,
                timeout_seconds=300,
                schema_definition=self._schema("DRAFT"),
            ),
        )

    async def _cut_off(
        self, request: ModelLinearCommentsSweepRequest, reply: ModelSweepPhaseReply
    ) -> list[str]:
        answer = await self.decisions.decide(
            request,
            {
                "kind": "detect_truncation",
                "outcome": {
                    "finish_reason": reply.finish_reason,
                    "truncated": reply.truncated,
                    "response_head": reply.content[:40],
                    "response_tail": reply.content[-200:],
                },
            },
        )
        truncation = cast(dict[str, JsonValue], answer["truncation"])
        return (
            [str(s) for s in cast(list[JsonValue], truncation["signals"])]
            if truncation["truncated"]
            else []
        )

    async def _draft_one(
        self,
        request: ModelLinearCommentsSweepRequest,
        slots: dict[str, str],
        item: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """One item through delegate, source check and verifier; a loss is a held entry, never a gap."""

        def lost(
            stage: str, reason: str, truncated: bool = False
        ) -> dict[str, JsonValue]:
            return {
                "held": {
                    "ticket": _text(item.get("ticket")),
                    "comment_id": _text(item.get("comment_id")),
                    "comment_url": _text(item.get("comment_url")),
                    "author_role": _text(item.get("author_role")),
                    "stage": stage,
                    "reason": reason,
                    "truncated": truncated,
                    "asks": cast(list[JsonValue], item.get("asks") or []),
                }
            }

        try:
            prompt = _text(
                (
                    await self.decisions.decide(
                        request,
                        {"kind": "build_prompt", "item": item, "facts": request.facts},
                    )
                )["prompt"]
            )
            reply = await self._draft_call(request, item, prompt, DRAFT_MAX_TOKENS, "")
            signals = await self._cut_off(request, reply)
            if signals:
                reply = await self._draft_call(
                    request, item, prompt, RETRY_MAX_TOKENS, ":retry"
                )
                signals = await self._cut_off(request, reply)
                if signals:
                    return lost(
                        "delegate",
                        f"the delegated response was cut off by its token limit "
                        f"({', '.join(signals)}) and again on the retry with "
                        f"--max-tokens {RETRY_MAX_TOKENS}; there is no complete draft",
                        True,
                    )
            if not reply.ok:
                return lost("delegate", reply.failure)
            draft = parse_draft(reply.content)
            if draft is None:
                return lost(
                    "delegate",
                    "the delegated response did not parse as the requested JSON object",
                )
            citation = cast(
                dict[str, JsonValue],
                (
                    await self.decisions.decide(
                        request,
                        {
                            "kind": "check_draft",
                            "draft": draft,
                            "item": item,
                            "facts": request.facts,
                        },
                    )
                )["citation"],
            )
            if not citation["ok"]:
                return lost(
                    "source_check",
                    f"the draft was refused before verification: {_text(citation['reason'])}",
                )
            verdict = await self._verify(request, slots, item, draft)
            if not verdict.accepted:
                return lost(
                    "verify",
                    verdict.rejection_reason
                    or "the verifier rejected the draft and gave no reason",
                )
            row = await self.decisions.decide(
                request,
                {"kind": "metrics_row", "receipt": self._receipt(reply, True)},
            )
            return {"draft": verdict.draft, "metrics": self._metrics(item, row)}
        except Exception as exc:
            return lost("pipeline", f"{type(exc).__name__}: {exc}")

    async def _verify(
        self,
        request: ModelLinearCommentsSweepRequest,
        slots: dict[str, str],
        item: dict[str, JsonValue],
        draft: dict[str, JsonValue],
    ) -> ModelSweepVerdict:
        reply = await self.gateway.run(
            request,
            ModelSweepPhaseRequest(
                label=f"verify:{_text(item.get('ticket'))}:{_text(item.get('comment_id'))}",
                phase="Draft",
                task_type="agent_delegation",
                model="opus",
                effort="high",
                prompt=self._render(
                    self.overlay.templates["linear-comments-verify"],
                    {
                        **slots,
                        "ticket": _text(item.get("ticket")),
                        "comment_id": _text(item.get("comment_id")),
                        "author_role": _text(item.get("author_role")),
                        "comment_text": _text(item.get("text")),
                        "draft_json": json.dumps(draft),
                    },
                ),
                timeout_seconds=1800,
                schema_definition=self._schema("VERIFY"),
            ),
        )
        if not reply.ok:
            raise ValueError(f"verifier returned nothing: {reply.failure}")
        return ModelSweepVerdict.model_validate_json(reply.content)

    @staticmethod
    def _receipt(reply: ModelSweepPhaseReply, accepted: bool) -> dict[str, JsonValue]:
        """The delegation terminal in the shape the decision node's metrics row reads."""
        attempt: dict[str, JsonValue] = {
            "model_id": reply.model_used,
            "acceptance_decision": "accept" if accepted else "reject",
        }
        return {
            "receipt": {
                "duration_ms": reply.latency_ms,
                "result": {
                    "model_name": reply.model_used,
                    "metrics": {
                        "total_tokens": reply.total_tokens,
                        "input_tokens": reply.prompt_tokens,
                        "output_tokens": reply.completion_tokens,
                        "latency_ms": reply.latency_ms,
                    },
                    "attempts": [attempt],
                    "attempts_count": reply.attempts or 1,
                    "escalation_count": reply.escalations or 0,
                },
            }
        }

    @staticmethod
    def _metrics(
        item: dict[str, JsonValue], row: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return {
            "ticket": _text(item.get("ticket")),
            "stage": "draft",
            **cast(dict[str, JsonValue], row["metrics"]),
        }

    async def _write(
        self,
        request: ModelLinearCommentsSweepRequest,
        slots: dict[str, str],
        collected: ModelSweepCollected,
        good: list[JsonValue],
        held: list[dict[str, JsonValue]],
        refused: list[dict[str, JsonValue]],
        metrics: list[JsonValue],
    ) -> ModelSweepWritten:
        payload = {
            "positive_control": collected.positive_control,
            "items": list(collected.items),
            "drafts": good,
            "held": held,
            "refused": refused,
        }
        reply = await self.gateway.run(
            request,
            ModelSweepPhaseRequest(
                label="write:linear-file",
                phase="Finalize",
                task_type="agent_delegation",
                model="sonnet",
                effort="medium",
                prompt=self._render(
                    self.overlay.templates["linear-comments-write"],
                    {
                        **slots,
                        "metrics_path": self.overlay.values["metrics_path"],
                        "payload_json": json.dumps(payload),
                        "metrics_json": json.dumps(metrics),
                    },
                ),
                timeout_seconds=1800,
                schema_definition=self._schema("WRITE"),
            ),
        )
        if not reply.ok:
            raise ValueError(f"write phase failed: {reply.failure}")
        try:
            written = ModelSweepWritten.model_validate_json(reply.content)
        except ValidationError as exc:
            raise ValueError(
                f"write phase result is off its schema: {exc.error_count()} problem(s)"
            ) from None
        if written.report_path != slots["report_path"]:
            raise ValueError(
                f"write phase wrote {written.report_path}, not {slots['report_path']}"
            )
        return written
