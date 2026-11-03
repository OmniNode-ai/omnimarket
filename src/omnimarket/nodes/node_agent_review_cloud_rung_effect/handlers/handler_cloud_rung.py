# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cast one cloud vote: a review-only Claude cloud session reviews one pull request head.

``handle(request) -> vote`` renders the review-only brief (the pull request, its exact
head, the concise compact verdict format and the instruction to end with the review;
nothing else), runs it as a review-only cloud lane through the injected runner, emits
one delegation event (backend ``harness:<harness>:<model>``, the run and session ids,
the head) and returns the vote UNSIGNED with that event's id. The dispatch node signs
the receipt that counts it.

Fail closed: a lane that did not end ``done`` with a parsed review, a review of another
head, a run with no session URL or an event the bus did not take raises
``CloudRungRefusedError`` and casts no vote, which the dispatch counts as a missing
voter. A model or endpoint other than the voter's is returned as written and never
rewritten, so the dispatch quorum refuses to count it.

No signing key or secret reaches the session: the brief is built from the request and
the overlay only and is refused if any secret name or secret value of the environment
appears in it; the runner's child environment drops every secret-shaped name.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

import omnimarket
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.agent_review_cloud import (
    REVIEW_FILE,
    REVIEW_HEAD_PREFIX,
    REVIEW_MARKER,
    REVIEW_ONLY_KIND,
)

from ..adapters import LaneBusDelegationEventSink, RemoteLaneCloudCaller
from ..models import (
    ModelCloudLaneRun,
    ModelCloudRungConfig,
    ModelCloudRungTopics,
    ModelCloudVote,
    ModelCloudVoteFinding,
    ModelCloudVoteRequest,
)
from ..protocols import ProtocolCloudLaneCaller, ProtocolDelegationEventSink
from ..secret_scope import (
    SIGNING_SECRET_NAME,
    is_secret_name,
    runner_environment,
    secret_leaks,
)

_PACKAGE = "omnimarket.nodes.node_agent_review_cloud_rung_effect"
OVERLAY_ID = "node_agent_review_cloud_rung_effect"
_CATALOG = Path("contracts") / "review_defect_catalog" / "review_defect_catalog.v1.yaml"
_HOUSE_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
"""omnimarket HOUSE_TENANT_UUID: the cloud rung serves the operator's own work only."""


class CloudRungRefusedError(RuntimeError):
    """The cloud lane left nothing this rung may count as a vote."""


def load_contract() -> dict[str, Any]:
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise CloudRungRefusedError(f"{OVERLAY_ID} contract.yaml is not a mapping")
    return raw


def load_topics() -> ModelCloudRungTopics:
    """The topics the contract declares; the handler spells none of its own."""
    contract = load_contract()
    return ModelCloudRungTopics(
        command=contract["runtime_dispatch"]["command_topic"],
        delegation_completed=contract["delegation_event"]["topic"],
    )


def load_config(environ: Mapping[str, str] | None = None) -> ModelCloudRungConfig:
    """``<root>/node_agent_review_cloud_rung_effect/overlay.yaml``; refuses naming the missing keys."""
    return load_structured_overlay(OVERLAY_ID, ModelCloudRungConfig, environ)


def catalog_section() -> tuple[str, str]:
    """The catalog version and one line per active code, from the packaged defect catalog."""
    path = Path(omnimarket.__file__).parent / _CATALOG
    catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    lines = [
        f"- {c['code']} ({c['family']}, default {c['default_severity']}): {c['title']}"
        for c in catalog["codes"]
        if c.get("status") == "active"
    ]
    return str(catalog["catalog_version"]), "\n".join(lines)


def lane_name(config: ModelCloudRungConfig, request: ModelCloudVoteRequest) -> str:
    """A lane name per vote: the overlay's prefix, the PR, the head and a nonce."""
    return f"{config.lane_prefix}-{request.pr}-{request.head_sha[:8]}-{uuid.uuid4().hex[:6]}"


def render_review_brief(
    request: ModelCloudVoteRequest, lane: str, config: ModelCloudRungConfig
) -> str:
    """The review-only brief: the pull request and its exact head, the concise format, the final message. Nothing else."""
    version, codes = catalog_section()
    example = json.dumps(
        {
            "v": version,
            "f": [{"c": "T02", "p": "src/pkg/module.py", "l": 42, "s": "M"}],
            "more": False,
            "done": True,
        }
    )
    return "\n".join(
        [
            f"# Lane {lane}: review {request.repo}#{request.pr} at head {request.head_sha} ({config.ticket})",
            "",
            f"Lane kind: {REVIEW_ONLY_KIND}",
            f"{REVIEW_HEAD_PREFIX} {request.head_sha}",
            "",
            "## Task",
            "",
            f"Review pull request {request.repo}#{request.pr} at exactly head {request.head_sha}. Fetch it with "
            f"`git fetch origin pull/{request.pr}/head` and check out {request.head_sha}; if the fetched head is another "
            "commit, review nothing and end with that fact. Review only what the pull request changes against the merge "
            "base with its base branch. Report defects only: security, correctness, missing error handling, untested "
            "behaviour changes, contract breaks. Do not praise and do not restate the change.",
            "",
            "Write nothing to GitHub: no commit, branch, push, pull request, comment or review.",
            "",
            "## Output",
            "",
            f"Write the review to {REVIEW_FILE}, then end your final message with the same two parts and nothing after them:",
            "",
            f"{REVIEW_MARKER} head={request.head_sha}",
            example,
            "",
            "The second part is one JSON object, the v2 compact verdict: `v` the catalog version "
            f"{version}; `f` the findings, at most 12; `more` true only if you found more than you listed; `done` true. "
            "Each finding: `c` a catalog code below, or `X` when none fits; `p` the repository-relative path; `l` the "
            "line (0 for the whole file); `s` the severity, `C` critical, `M` major, `m` minor, `n` nit; `y` the family "
            "(architecture, security, performance, correctness, completeness, feasibility, testing, style), only with "
            "`X`; `n` a note of at most 120 characters, required with `X`. No other key. With no defects, `f` is [].",
            "",
            f"Catalog {version}:",
            codes,
            "",
        ]
    )


def delegation_payload(
    request: ModelCloudVoteRequest,
    run: ModelCloudLaneRun,
    config: ModelCloudRungConfig,
    lane: str,
    brief: str,
    event_id: str,
) -> dict[str, Any]:
    """One delegate-skill terminal for the cloud session, in the harness-delegate shape: the backend
    ``harness:<harness>:<model>``, the run and session ids and the head in the response."""
    assert run.review is not None
    response = json.dumps(
        {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "session_url": run.session_url,
            "repo": request.repo,
            "pr": request.pr,
            "head_sha": request.head_sha,
            "identity": request.identity,
            "review": run.review.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    backend = f"harness:{config.harness}:{run.model or 'unknown'}"
    return {
        "status": "completed",
        "correlation_id": event_id,
        "task_type": config.task_type,
        "provider": f"harness:{config.harness}",
        "model_name": run.model or "unknown",
        "backend_id": backend,
        "host": None,
        "prompt_text": brief,
        "prompt_chars": len(brief),
        "prompt_sha256": hashlib.sha256(brief.encode()).hexdigest(),
        "response": response,
        "response_chars": len(response),
        "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "error_message": "",
        "metrics": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "latency_ms": 0,
            "cost_usd": 0.0,
        },
        "attempts": [
            {
                "tier": "harness",
                "backend_id": backend,
                "model_id": run.model or "unknown",
                "quality_gate_passed": True,
                "host": None,
                "acceptance_decision": "accept",
                "acceptance_reason": "quality_bar_met",
                "failure_class": None,
                "error_message": "",
            }
        ],
        "attempts_count": 1,
        "escalation_count": 0,
        "session_id": event_id,
        "delegated_to": run.model or "unknown",
        "caller_lane": lane,
        "ticket_id": config.ticket,
        "tenant_id": _HOUSE_TENANT,
        "repo_name": request.repo.split("/", 1)[1],
        "emitted_at": datetime.now(UTC).isoformat(),
    }


def vote_from_run(
    request: ModelCloudVoteRequest, run: ModelCloudLaneRun, event_id: str
) -> ModelCloudVote:
    """The unsigned vote the run supports. The model and endpoint are the session's own, as written."""
    assert run.review is not None
    return ModelCloudVote(
        identity=request.identity,
        endpoint=urlsplit(run.session_url).hostname or "",
        model=run.model,
        delegation_event_id=event_id,
        findings=tuple(
            ModelCloudVoteFinding.model_validate(f.model_dump())
            for f in run.review.findings
        ),
    )


def refusal(request: ModelCloudVoteRequest, run: ModelCloudLaneRun) -> str | None:
    """Why the run supports no vote, or None."""
    if run.status != "done" or run.review is None:
        return f"the cloud lane ended {run.status or 'unknown'}: {run.refusal or 'no review'}"
    if run.review.head_sha != request.head_sha:
        return f"the review is of head {run.review.head_sha}, not {request.head_sha}"
    if not urlsplit(run.session_url).hostname:
        return "the run names no session URL"
    return None


class HandlerAgentReviewCloudRung:
    """One cloud vote per request; the runner, the event sink, the config and the environment are injected."""

    def __init__(
        self,
        *,
        runner: ProtocolCloudLaneCaller | None = None,
        sink: ProtocolDelegationEventSink | None = None,
        config: ModelCloudRungConfig | None = None,
        environ: Mapping[str, str] | None = None,
        topics: ModelCloudRungTopics | None = None,
    ) -> None:
        """Every port defaults to its production binding, built at the first call: the remote-lane runner, the
        lane bus the overlay names, the overlay and the process environment."""
        self._runner = runner
        self._sink = sink
        self._config = config
        self._environ = environ
        self._topics = topics

    def _bound(
        self,
    ) -> tuple[
        ProtocolCloudLaneCaller,
        ProtocolDelegationEventSink,
        ModelCloudRungConfig,
        Mapping[str, str],
        ModelCloudRungTopics,
    ]:
        environ: Mapping[str, str] = (
            os.environ if self._environ is None else self._environ
        )
        config = self._config or load_config(environ)
        runner = self._runner or RemoteLaneCloudCaller(environ)
        sink = self._sink or LaneBusDelegationEventSink(config)
        return runner, sink, config, environ, self._topics or load_topics()

    async def handle(self, request: ModelCloudVoteRequest) -> ModelCloudVote:
        runner, sink, config, environ, topics = self._bound()
        lane = lane_name(config, request)
        brief = render_review_brief(request, lane, config)
        leaks = secret_leaks(brief, environ)
        if leaks:
            raise CloudRungRefusedError(
                f"the brief holds secret material: {', '.join(leaks)}"
            )
        run = await runner.run(brief, lane, request, config)
        why = refusal(request, run)
        if why is not None:
            raise CloudRungRefusedError(why)
        event_id = str(uuid.uuid4())
        payload = delegation_payload(request, run, config, lane, brief, event_id)
        receipt = await sink.emit(topics.delegation_completed, event_id, payload)
        if not receipt.published or receipt.event_id != event_id:
            raise CloudRungRefusedError(
                f"the bus took no delegation event: {receipt.detail or 'not published'}"
            )
        return vote_from_run(request, run, event_id)


__all__ = [
    "OVERLAY_ID",
    "SIGNING_SECRET_NAME",
    "CloudRungRefusedError",
    "HandlerAgentReviewCloudRung",
    "catalog_section",
    "delegation_payload",
    "is_secret_name",
    "lane_name",
    "load_config",
    "load_topics",
    "refusal",
    "render_review_brief",
    "runner_environment",
    "secret_leaks",
    "vote_from_run",
]
