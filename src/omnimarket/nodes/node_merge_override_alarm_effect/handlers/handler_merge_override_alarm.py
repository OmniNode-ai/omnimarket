# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge in, verdict out: every merge is judged once against the landing receipts and the override rulings."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_request import (
    ModelPrLandingGithubRequest,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref, contract_topic
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.classify_merge import (
    classify_merge,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.override_row_reader import (
    PostgresOverrideRowReader,
    resolve_dsn,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.store_merge_override import (
    MergeOverrideStore,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideOutcome,
    EnumMergeOverrideVerdict,
    ModelMergeObservation,
    ModelMergeOverrideClassification,
    ModelMergeOverrideFinding,
    ModelMergeOverrideInput,
    ModelMergeOverrideReceipt,
    ModelMergeOverrideResult,
    ModelMergeOverrideSettings,
    ModelMergeOverrideSlackCommand,
    ModelMergeOverrideSweepTrigger,
    ModelMergeOverrideVerdictEvent,
)
from omnimarket.nodes.node_merge_override_alarm_effect.overlay import load_settings
from omnimarket.nodes.node_merge_override_alarm_effect.protocols.protocol_override_row_reader import (
    ProtocolOverrideRowReader,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"
_SLACK_SUFFIX = "slack-publish.v1"
_FINDING_SUFFIX = "merge-override-finding.v1"
_VERDICT_SUFFIX = "merge-override-verdict.v1"
_READ_SUFFIX = "pr-landing-github-requested.v1"
_RECEIPT_OPERATIONS = frozenset(
    {
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
    }
)


class HandlerMergeOverrideAlarm:
    """Reads the overlay, the receipts and the ledger at evaluation time, never at wiring time."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        reader: ProtocolOverrideRowReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._slack_topic = contract_topic(
            self._contract_path, "publish_topics", _SLACK_SUFFIX
        )
        self._finding_topic = contract_topic(
            self._contract_path, "publish_topics", _FINDING_SUFFIX
        )
        self._verdict_topic = contract_topic(
            self._contract_path, "publish_topics", _VERDICT_SUFFIX
        )
        self._read_topic = contract_topic(
            self._contract_path, "publish_topics", _READ_SUFFIX
        )
        source = yaml.safe_load(self._contract_path.read_text(encoding="utf-8"))
        self._reads_dsn_env: str = source["reads_source"]["dsn_env"]
        self._writes_dsn_env: str = source["writes_source"]["dsn_env"]
        self._writes_schema: str = source["writes_source"]["schema"]
        self._publisher = event_publisher
        self._database = database
        self._reader = reader
        self._clock = clock or time.time
        self._store: MergeOverrideStore | None = None
        self._last_sweep_at: float | None = None

    def handle(self, request: ModelMergeOverrideInput) -> ModelMergeOverrideResult:
        try:
            settings = load_settings()
        except NodeOverlayError as exc:
            logger.error("Merge override alarm refused: %s", exc)
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.REFUSED, reason=str(exc)
            )
        now = datetime.fromtimestamp(self._clock(), tz=UTC)
        try:
            if isinstance(request, ModelMergeOverrideSweepTrigger):
                return self._sweep(settings, now)
            if isinstance(request, ModelPrLandingGithubCompleted):
                return self._on_completed(request, settings, now)
            if isinstance(request, ModelPrLandingGithubFailed):
                return self._on_failed(request, settings, now)
            if isinstance(request, ModelPrMergedEvent):
                return self._on_merge(
                    ModelMergeObservation(
                        repository=request.repo,
                        pr_number=request.pr_number,
                        source="pr-merged",
                    ),
                    now,
                )
            if request.state != "merged":
                return ModelMergeOverrideResult(
                    outcome=EnumMergeOverrideOutcome.IGNORED,
                    reason=f"{request.repo}#{request.pr_number} is {request.state}, not merged",
                )
            return self._on_merge(
                ModelMergeObservation(
                    repository=request.repo,
                    pr_number=request.pr_number,
                    merge_sha=request.merge_sha,
                    source="repo-merged",
                ),
                now,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.error("Merge override alarm could not handle its input: %s", reason)
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.REFUSED, error=reason
            )

    # -- inputs ---------------------------------------------------------------

    def _on_completed(
        self,
        event: ModelPrLandingGithubCompleted,
        settings: ModelMergeOverrideSettings,
        now: datetime,
    ) -> ModelMergeOverrideResult:
        if event.operation in _RECEIPT_OPERATIONS:
            if event.mode is not EnumPrLandingGithubMode.ENFORCE:
                return ModelMergeOverrideResult(
                    outcome=EnumMergeOverrideOutcome.IGNORED,
                    reason=f"a {event.mode.value} {event.operation.value} is not a landing receipt",
                )
            assert event.head_sha is not None  # the completed model requires it
            self._get_store().record_receipt(
                ModelMergeOverrideReceipt(
                    repository=event.repository,
                    pr_number=event.pr_number,
                    head_sha=event.head_sha,
                    operation=event.operation.value,
                    observed_at=now,
                    correlation_id=event.correlation_id,
                )
            )
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.RECEIPT_RECORDED
            )
        if event.operation is not EnumPrLandingGithubOperation.READ_PR_STATE:
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.IGNORED,
                reason=f"{event.operation.value} is not a receipt or a read",
            )
        pending = self._get_store().pending_by_correlation(event.correlation_id)
        if pending is None:
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.IGNORED,
                reason="no pending merge read for this correlation id",
            )
        observation = _observation(pending)
        state = event.pr_state
        if state is None or event.not_modified or not state.merged:
            return self._settle_unreadable(
                observation,
                UUID(str(pending["correlation_id"])),
                now,
                "the GitHub read did not show the pull request merged",
            )
        head = state.head_sha
        commit = state.merge_commit_sha or observation.merge_sha
        correlation = UUID(str(pending["correlation_id"]))
        try:
            receipt = self._get_store().newest_receipt(
                observation.repository, observation.pr_number
            )
            classification = classify_merge(
                repository=observation.repository,
                pr_number=observation.pr_number,
                head_sha=head,
                merger=state.merged_by,
                receipt=receipt,
                override_rows=(),
                settings=settings,
            )
            if classification.verdict is not EnumMergeOverrideVerdict.LANDING:
                # Only a merge the receipts do not explain needs the ledger.
                classification = classify_merge(
                    repository=observation.repository,
                    pr_number=observation.pr_number,
                    head_sha=head,
                    merger=state.merged_by,
                    receipt=receipt,
                    override_rows=self._get_reader().read_override_rows(
                        observation.repository, observation.pr_number
                    ),
                    settings=settings,
                )
        except Exception as exc:
            return self._settle_unreadable(
                observation,
                correlation,
                now,
                f"{type(exc).__name__}: {exc}",
                head_sha=head,
                merge_commit_sha=commit,
                merger=state.merged_by,
            )
        return self._settle(
            observation,
            correlation,
            now,
            classification,
            head_sha=head,
            merge_commit_sha=commit,
            merger=state.merged_by,
            receipt_seen=receipt is not None,
        )

    def _on_failed(
        self,
        event: ModelPrLandingGithubFailed,
        settings: ModelMergeOverrideSettings,
        now: datetime,
    ) -> ModelMergeOverrideResult:
        del settings
        pending = self._get_store().pending_by_correlation(event.correlation_id)
        if pending is None:
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.IGNORED,
                reason="no pending merge read for this correlation id",
            )
        return self._settle_unreadable(
            _observation(pending),
            UUID(str(pending["correlation_id"])),
            now,
            f"the GitHub read failed: {event.reason}",
        )

    def _on_merge(
        self, observation: ModelMergeObservation, now: datetime
    ) -> ModelMergeOverrideResult:
        store = self._get_store()
        if store.seen(observation.repository, observation.pr_number):
            return ModelMergeOverrideResult(
                outcome=EnumMergeOverrideOutcome.DUPLICATE,
                reason=f"{observation.repository}#{observation.pr_number} already has a verdict or a pending read",
            )
        correlation = uuid4()
        store.begin_pending(observation, correlation, now)
        try:
            if self._publisher is None:
                raise RuntimeError("no event_publisher was injected")
            command = ModelPrLandingGithubRequest(
                correlation_id=correlation,
                operation=EnumPrLandingGithubOperation.READ_PR_STATE,
                mode=EnumPrLandingGithubMode.ENFORCE,
                repository=observation.repository,
                pr_number=observation.pr_number,
            )
            self._publisher(self._read_topic, command.model_dump_json().encode())
        except Exception as exc:
            return self._settle_unreadable(
                observation,
                correlation,
                now,
                f"the GitHub read could not be requested: {type(exc).__name__}: {exc}",
            )
        return ModelMergeOverrideResult(outcome=EnumMergeOverrideOutcome.READ_REQUESTED)

    def _sweep(
        self, settings: ModelMergeOverrideSettings, now: datetime
    ) -> ModelMergeOverrideResult:
        epoch = now.timestamp()
        last = self._last_sweep_at
        if last is not None and 0 <= epoch - last < settings.sweep_interval_seconds:
            return ModelMergeOverrideResult(outcome=EnumMergeOverrideOutcome.THROTTLED)
        self._last_sweep_at = epoch
        cutoff = now - timedelta(seconds=settings.pending_read_timeout_seconds)
        swept = 0
        for row in self._get_store().pending_older_than(cutoff):
            self._settle_unreadable(
                _observation(row),
                UUID(str(row["correlation_id"])),
                now,
                f"the GitHub read for the merge was not answered in {settings.pending_read_timeout_seconds} s",
            )
            swept += 1
        return ModelMergeOverrideResult(
            outcome=EnumMergeOverrideOutcome.SWEPT, swept=swept
        )

    # -- verdicts -------------------------------------------------------------

    def _settle_unreadable(
        self,
        observation: ModelMergeObservation,
        correlation: UUID,
        now: datetime,
        reason: str,
        *,
        head_sha: str | None = None,
        merge_commit_sha: str | None = None,
        merger: str | None = None,
    ) -> ModelMergeOverrideResult:
        logger.error(
            "Merge override source unreadable for %s#%s: %s",
            observation.repository,
            observation.pr_number,
            reason,
        )
        return self._settle(
            observation,
            correlation,
            now,
            ModelMergeOverrideClassification(
                verdict=EnumMergeOverrideVerdict.DETECTOR_UNREADABLE, reason=reason
            ),
            head_sha=head_sha,
            merge_commit_sha=merge_commit_sha or observation.merge_sha,
            merger=merger,
            receipt_seen=False,
        )

    def _settle(
        self,
        observation: ModelMergeObservation,
        correlation: UUID,
        now: datetime,
        classification: ModelMergeOverrideClassification,
        *,
        head_sha: str | None,
        merge_commit_sha: str | None,
        merger: str | None,
        receipt_seen: bool,
    ) -> ModelMergeOverrideResult:
        """Persist and emit one verdict; a finding adds its error event, command and open row.

        Each step is tried on its own so one failed publish never hides the others.
        """
        verdict = ModelMergeOverrideVerdictEvent(
            repository=observation.repository,
            pr_number=observation.pr_number,
            verdict=classification.verdict,
            head_sha=head_sha,
            merge_commit_sha=merge_commit_sha,
            merger=merger,
            source=observation.source,
            receipt_seen=receipt_seen,
            ruling_row_id=classification.ruling_row_id,
            reason=classification.reason,
            correlation_id=correlation,
            decided_at=now,
        )
        errors: list[str] = []
        finding_published = command_published = False
        self._attempt(
            errors, "verdict row", lambda: self._get_store().record_verdict(verdict)
        )
        self._attempt(
            errors,
            "verdict event",
            lambda: self.publish_event(self._verdict_topic, verdict.model_dump_json()),
        )
        if classification.verdict.is_finding:
            finding = _finding(verdict)
            finding_published = self._attempt(
                errors,
                "finding event",
                lambda: self.publish_event(
                    self._finding_topic, finding.model_dump_json()
                ),
            )
            command_published = self._attempt(
                errors, "slack command", lambda: self._publish_slack(finding)
            )
            self._attempt(
                errors,
                "open finding row",
                lambda: self._get_store().open_finding(finding, now),
            )
        error = "; ".join(errors) or None
        if error is not None:
            logger.error(
                "Merge override verdict NOT fully delivered: %s#%s error=%s",
                observation.repository,
                observation.pr_number,
                error,
            )
        return ModelMergeOverrideResult(
            outcome=EnumMergeOverrideOutcome.VERDICT,
            verdict=verdict,
            finding_published=finding_published,
            command_published=command_published,
            error=error,
        )

    @staticmethod
    def _attempt(errors: list[str], step: str, action: Callable[[], object]) -> bool:
        try:
            action()
        except Exception as exc:
            errors.append(f"{step}: {type(exc).__name__}: {exc}")
            return False
        return True

    def publish_event(self, topic: str, payload_json: str) -> None:
        if self._publisher is None:
            raise RuntimeError("no event_publisher was injected; nothing was published")
        self._publisher(topic, payload_json.encode())

    def _publish_slack(self, finding: ModelMergeOverrideFinding) -> None:
        ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
        secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(f"secret ref {ref!r} resolved to None")
        command = ModelMergeOverrideSlackCommand(
            channel=secret.get_secret_value(),
            text=finding.text,
            idempotency_key=finding.idempotency_key,
            correlation_id=finding.correlation_id,
        )
        self.publish_event(self._slack_topic, command.model_dump_json())

    # -- boundaries -----------------------------------------------------------

    def _get_store(self) -> MergeOverrideStore:
        if self._store is None:
            if self._database is None:
                # Resolved at first use, so an absent DSN becomes a detector_unreadable
                # verdict or a refusal, never a wiring failure.
                from omnimarket.projection.postgres_sync_database import (
                    PostgresSyncProjectionAdapter,
                )

                self._database = PostgresSyncProjectionAdapter(
                    resolve_dsn(self._writes_dsn_env), schema=self._writes_schema
                )
            self._store = MergeOverrideStore(self._database)
        return self._store

    def _get_reader(self) -> ProtocolOverrideRowReader:
        if self._reader is None:
            self._reader = PostgresOverrideRowReader(
                dsn=resolve_dsn(self._reads_dsn_env)
            )
        return self._reader


def _observation(pending: dict[str, object]) -> ModelMergeObservation:
    source = str(pending["source"])
    merge_sha = pending.get("merge_sha")
    return ModelMergeObservation(
        repository=str(pending["repository"]),
        pr_number=int(str(pending["pr_number"])),
        merge_sha=str(merge_sha) if merge_sha else None,
        source="repo-merged" if source == "repo-merged" else "pr-merged",
    )


def _finding(verdict: ModelMergeOverrideVerdictEvent) -> ModelMergeOverrideFinding:
    key = (
        f"merge-override:{verdict.repository}#{verdict.pr_number}:"
        f"{verdict.head_sha or 'unreadable'}"
    )
    where = f"{verdict.repository}#{verdict.pr_number}"
    if verdict.verdict is EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE:
        text = (
            f"Merge without override: {where} head {verdict.head_sha} "
            f"merge commit {verdict.merge_commit_sha} merged by {verdict.merger}. "
            f"{verdict.reason}."
        )
    else:
        text = (
            f"Merge override detector unreadable for {where} "
            f"(head {verdict.head_sha}, merge commit {verdict.merge_commit_sha}): {verdict.reason}"
        )
    return ModelMergeOverrideFinding(
        verdict=verdict.verdict,
        repository=verdict.repository,
        pr_number=verdict.pr_number,
        head_sha=verdict.head_sha,
        merge_commit_sha=verdict.merge_commit_sha,
        merger=verdict.merger,
        source=verdict.source,
        reason=verdict.reason,
        text=text,
        idempotency_key=key,
        correlation_id=verdict.correlation_id,
    )


__all__ = ["HandlerMergeOverrideAlarm"]
