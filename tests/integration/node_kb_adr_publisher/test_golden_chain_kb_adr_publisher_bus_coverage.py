# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full I/O-boundary EFFECT coverage for node_kb_adr_publisher, driven over the
canonical in-memory bus.

OMN-13674 (cluster wave-kb-context-knowledge, archetype effect). This module
drives ``HandlerKBADRPublisher`` end to end over ``EventBusInmemory`` (via the
``integration_event_bus`` fixture + ``LocalRuntimeBusAdapter``): a
``ModelKBADRPublishRequest`` lands on the declared command topic
``onex.cmd.omnimarket.kb-adr-publish-requested.v1`` and the terminal
``ModelKBADRPublishResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.kb-adr-publish-completed.v1``. No live Kafka / ``.201``.

The git/gh subprocess boundary is replaced by a constructor-injected
``_MockRunner`` (the canonical ``_Mock*`` injection pattern) — subprocess is
NEVER monkeypatched and no real git/gh ever runs, so no prod-mutating effect is
exercised. The real ``render_adr_to_kb`` adapter runs unchanged, writing ADR
markdown into a temp clone dir (filesystem-only, no network).

EFFECT DoD covered — every outcome at the injected I/O boundary:
  * missing ``extracted_decisions.json`` → ``success=False`` (pre-boundary);
  * no decisions matching ``model_key`` → ``success=False`` (pre-boundary);
  * dry-run success → ``adr_count`` set, no PR, no subprocess;
  * full success → clone + branch + render + commit + push + PR create, with a
    real ``pr_url`` + ``branch`` off the terminal event;
  * gate-blocked / failure mode: a clone subprocess fault raises through the
    ``check=True`` seam — the adapter records the failure and NO terminal event
    is published (never a silent success);
  * idempotency: identical input yields an identical terminal event.
Typed result fields are asserted off the terminal event — never a bare
"returned without raising".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    EnumAdrPublicationClassification,
    EnumAdrSourceVisibility,
    ModelAdrSourceProvenance,
)
from omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher import (
    HandlerKBADRPublisher,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_result import (
    ModelKBADRPublishResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.kb-adr-publish-requested.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.kb-adr-publish-completed.v1"

_PR_URL = "https://github.com/OmniNode-ai/knowledge-base/pull/42"
_SOURCE_PROVENANCE = ModelAdrSourceProvenance(
    source_repository="OmniNode-ai/omnimarket",
    source_visibility="public",
    publication_classification="public",
)


def _make_decision(
    model_id: str = "qwen3-coder-local",
    *,
    source_provenance: ModelAdrSourceProvenance = _SOURCE_PROVENANCE,
    kb_destination: EnumAdrKBDestination = EnumAdrKBDestination.public,
) -> dict[str, Any]:
    return {
        "draft": {
            "status": "Proposed",
            "date": "2026-05-23T10:00:00+00:00",
            "title": f"Use Pydantic for wire DTOs ({model_id})",
            "context": "Inconsistent DTO definitions across repos.",
            "decision": "All wire DTOs must be Pydantic BaseModel subclasses.",
            "consequences": "Easier validation; heavier import overhead.",
            "alternatives_considered": ["dataclasses", "TypedDict"],
            "supersedes": [],
            "source_evidence": ["seg-abc123"],
            "extraction_metadata": {
                "model_id": model_id,
                "confidence": 0.87,
                "pipeline_version": "1.0.0",
                "prompt_template_id": "adr-extraction-v3",
                "prompt_template_version": "3.0.1",
                "canary_run_id": "canary-2026-05-23-001",
                "extracted_at": "2026-05-23T10:00:00+00:00",
            },
        },
        "source_provenance": source_provenance.model_dump(mode="json"),
        "kb_destination": kb_destination.value,
        "source_documents": [
            {
                "source_path": "docs/plans/adr-publisher-plan.md",
                "source_content_sha256": "a" * 64,
            }
        ],
    }


def _command(
    run_dir: Path,
    *,
    model_key: str = "qwen3-coder-local",
    dry_run: bool = False,
    kb_destination: EnumAdrKBDestination = EnumAdrKBDestination.public,
    source_provenance: ModelAdrSourceProvenance | None = _SOURCE_PROVENANCE,
) -> ModelKBADRPublishRequest:
    return ModelKBADRPublishRequest(
        canary_run_dir=str(run_dir),
        model_key=model_key,
        dry_run=dry_run,
        kb_destination=kb_destination,
        source_provenance=source_provenance,
    )


def _canary_dir(tmp_path: Path, *, decisions: list[dict[str, Any]] | None) -> Path:
    run_dir = tmp_path / "canary-2026-05-23-001"
    run_dir.mkdir(parents=True)
    if decisions is not None:
        (run_dir / "extracted_decisions.json").write_text(
            json.dumps(decisions), encoding="utf-8"
        )
    return run_dir


class _MockRunner:
    """Constructor-injected git/gh seam — no real subprocess runs."""

    def __init__(self, *, pr_url: str = _PR_URL, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._pr_url = pr_url
        self._fail_on = fail_on  # "clone" | "commit" | "push" | "pr" | None

    @staticmethod
    def _token(cmd: list[str]) -> str:
        if "clone" in cmd:
            return "clone"
        if "checkout" in cmd:
            return "checkout"
        if "pr" in cmd and "create" in cmd:
            return "pr"
        if "commit" in cmd:
            return "commit"
        if "push" in cmd:
            return "push"
        if "add" in cmd:
            return "add"
        return "other"

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        token = self._token(cmd)
        if self._fail_on == token:
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, stderr=f"{token} boom"
            )
        stdout = f"{self._pr_url}\n" if token == "pr" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


async def _drive(
    bus: Any,
    command: ModelKBADRPublishRequest,
    runner: _MockRunner,
    *,
    on_error: Any | None = None,
) -> tuple[list[Any], _MockRunner]:
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerKBADRPublisher(run=runner),
        handler_name="kb-adr-publisher",
        input_model_cls=ModelKBADRPublishRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
        on_error=on_error,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-kb-adr-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=command.model_dump_json().encode("utf-8")
    )
    completed = await bus.get_event_history(topic=TOPIC_COMPLETED)
    return completed, runner


def _result(completed: list[Any]) -> ModelKBADRPublishResult:
    assert len(completed) == 1, f"expected exactly one terminal event, got {completed}"
    assert completed[-1].topic == "onex.evt.omnimarket.kb-adr-publish-completed.v1"
    return ModelKBADRPublishResult.model_validate(json.loads(completed[-1].value))


# ---------------------------------------------------------------------------
# Pre-boundary failure — missing extracted_decisions.json.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_missing_decisions_file_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        run_dir = _canary_dir(tmp_path, decisions=None)
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(run_dir),
            runner,
        )
        result = _result(completed)
        assert result.success is False
        assert result.error is not None
        assert "extracted_decisions.json" in result.error
        assert result.adr_count == 0
        assert runner.calls == []  # never reached the subprocess boundary
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Pre-boundary failure — no decisions match the model_key.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_matching_decisions_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        run_dir = _canary_dir(tmp_path, decisions=[_make_decision("deepseek-r1-local")])
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(run_dir),
            runner,
        )
        result = _result(completed)
        assert result.success is False
        assert result.error is not None
        assert "qwen3-coder-local" in result.error
        assert runner.calls == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# dry-run success — filtered count, no PR, no subprocess.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dry_run_success_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        run_dir = _canary_dir(
            tmp_path,
            decisions=[
                _make_decision("qwen3-coder-local"),
                _make_decision("qwen3-coder-local"),
                _make_decision("deepseek-r1-local"),
            ],
        )
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(run_dir, dry_run=True),
            runner,
        )
        result = _result(completed)
        assert result.success is True
        assert result.adr_count == 2  # filtered to the matching model_key
        assert result.pr_url is None
        assert result.branch is None
        assert runner.calls == []  # dry-run never crosses the boundary
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# full success — clone + branch + render + commit + push + PR create.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_full_publish_success_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        run_dir = _canary_dir(
            tmp_path,
            decisions=[
                _make_decision("qwen3-coder-local"),
                _make_decision("qwen3-coder-local"),
            ],
        )
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(run_dir),
            runner,
        )
        result = _result(completed)
        assert result.success is True
        assert result.adr_count == 2
        assert result.pr_url == _PR_URL
        assert result.branch is not None
        assert "canary-2026-05-23-001" in result.branch
        assert "qwen3-coder-local" in result.branch
        tokens = [_MockRunner._token(c) for c in runner.calls]
        assert tokens.count("clone") == 1
        assert tokens.count("pr") == 1
        assert "checkout" in tokens
        assert "push" in tokens
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# gate-blocked / failure mode — a clone fault raises through check=True; the
# adapter records the failure and NO terminal event is published.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_clone_fault_yields_no_terminal_event_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    errors: list[int] = []
    try:
        run_dir = _canary_dir(tmp_path, decisions=[_make_decision("qwen3-coder-local")])
        runner = _MockRunner(fail_on="clone")
        completed, runner = await _drive(
            bus,
            _command(run_dir),
            runner,
            on_error=lambda: errors.append(1),
        )
        # The uncaught CalledProcessError means the effect never claims success.
        assert completed == []
        assert errors == [1]
        assert [_MockRunner._token(c) for c in runner.calls] == ["clone"]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Publication policy — reject unsafe provenance before the subprocess seam.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("source_visibility", "classification", "error_fragment"),
    [
        (
            EnumAdrSourceVisibility.private,
            EnumAdrPublicationClassification.private,
            "private source provenance",
        ),
        (
            EnumAdrSourceVisibility.public,
            EnumAdrPublicationClassification.restricted,
            "restricted publication classification",
        ),
        (
            EnumAdrSourceVisibility.public,
            EnumAdrPublicationClassification.needs_review,
            "needs_review publication classification",
        ),
    ],
)
async def test_public_destination_policy_rejection_over_bus_has_no_subprocess(
    integration_event_bus: Any,
    tmp_path: Path,
    source_visibility: EnumAdrSourceVisibility,
    classification: EnumAdrPublicationClassification,
    error_fragment: str,
) -> None:
    """Unsafe source policy publishes a typed failure without invoking git/gh."""
    bus = integration_event_bus
    await bus.start()
    try:
        source = ModelAdrSourceProvenance(
            source_repository="OmniNode-ai/policy-source",
            source_visibility=source_visibility,
            publication_classification=classification,
        )
        run_dir = _canary_dir(
            tmp_path,
            decisions=[_make_decision(source_provenance=source)],
        )
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(run_dir, source_provenance=source),
            runner,
        )
        result = _result(completed)
        assert result.success is False
        assert result.error_code == "PUBLICATION_POLICY_REJECTED"
        assert result.error is not None
        assert error_fragment in result.error
        assert runner.calls == []
    finally:
        await bus.close()


@pytest.mark.integration
async def test_private_source_can_publish_to_private_destination_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """The closed private destination is the sole allowed route for private sources."""
    bus = integration_event_bus
    await bus.start()
    try:
        private_source = ModelAdrSourceProvenance(
            source_repository="OmniNode-ai/private-source",
            source_visibility="private",
            publication_classification="private",
        )
        run_dir = _canary_dir(
            tmp_path,
            decisions=[
                _make_decision(
                    source_provenance=private_source,
                    kb_destination=EnumAdrKBDestination.private,
                )
            ],
        )
        runner = _MockRunner()
        completed, runner = await _drive(
            bus,
            _command(
                run_dir,
                kb_destination=EnumAdrKBDestination.private,
                source_provenance=private_source,
            ),
            runner,
        )
        result = _result(completed)
        assert result.success is True
        assert result.kb_destination is EnumAdrKBDestination.private
        assert result.kb_repository == "OmniNode-ai/knowledge-base-internal"
        assert ["gh", "repo", "clone", "OmniNode-ai/knowledge-base-internal"] in [
            call[:4] for call in runner.calls
        ]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Idempotency — identical input yields an identical terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_deterministic_identical_input_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus_factory = type(integration_event_bus)
    run_dir = _canary_dir(tmp_path, decisions=[_make_decision("qwen3-coder-local")])
    command = _command(run_dir)
    payloads: list[str] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            completed, _ = await _drive(bus, command, _MockRunner())
            payloads.append(_result(completed).model_dump_json())
        finally:
            await bus.close()
    assert payloads[0] == payloads[1]
