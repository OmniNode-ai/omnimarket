# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the nightly attribution records a receipt names (operation ``read_attribution_records``).

The only I/O is the repository gateway node's ``commit_check_runs``: the check run ``<prefix>:<slot date>``
on the tested head commit the receipt names for the night, with its output summary. Nothing else is read,
and no record is taken on trust: the summary must parse as a published attribution record, be the only
one for the night, and name the commit, repository, slot date and run it was published for. Everything else is a typed defect or an absent record, never a
clean night. A gateway that gives no answer raises ``GatewayReadError`` to the caller.
"""

from __future__ import annotations

from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelCheckRun,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_attribution_record_reading import (
    EnumRecordDefectKind,
    ModelAttributionRecordReading,
    ModelAttributionRecordReadRequest,
    ModelRecordDefect,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelReceiptNight,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_gateway_read import (
    ProtocolGatewayRead,
)

_Kind = EnumRecordDefectKind


class HandlerAttributionRecordRead:
    """Reads through its injected gateway; the settings come from the policy."""

    def __init__(
        self, gateway: ProtocolGatewayRead, policy: ModelSelectorPolicy | None = None
    ) -> None:
        self._gateway = gateway
        self._policy = policy or load_policy()

    def handle(
        self, request: ModelAttributionRecordReadRequest
    ) -> ModelAttributionRecordReading:
        records: list[ModelPublishedAttributionRecord] = []
        defects: list[ModelRecordDefect] = []
        for night in request.nights:
            outcome = self._read_night(request.repository, night)
            if isinstance(outcome, ModelRecordDefect):
                defects.append(outcome)
            elif outcome is not None:
                records.append(outcome)
        return ModelAttributionRecordReading(
            records=tuple(records), defects=tuple(defects)
        )

    def _read_night(
        self, repository: str, night: ModelReceiptNight
    ) -> ModelPublishedAttributionRecord | ModelRecordDefect | None:
        head = night.tested_head_sha
        name = f"{self._policy.record_check_name_prefix}:{night.slot_date.isoformat()}"
        result = self._gateway.read(
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
                repo=repository,
                head_sha=head,
                check_name=name,
                include_output=True,
                limit=MAX_LIST_LIMIT,
            )
        )
        if not isinstance(result, ModelCommitCheckRunsResult):
            raise GatewayReadError(
                f"gateway answered {result.operation!r} for commit_check_runs"
            )
        runs = [run for run in result.check_runs if run.name == name]
        if not runs:
            return None
        if len(runs) > 1:
            return _defect(
                night, _Kind.DUPLICATE, f"{len(runs)} check runs named {name}"
            )
        return _parse(repository, night, head, runs[0])


def _parse(
    repository: str, night: ModelReceiptNight, head: str, run: ModelCheckRun
) -> ModelPublishedAttributionRecord | ModelRecordDefect:
    if run.status != "completed":
        return _defect(night, _Kind.UNPARSEABLE, f"the check run is {run.status}")
    if not run.output_summary:
        return _defect(night, _Kind.UNPARSEABLE, "the check run carries no summary")
    try:
        record = ModelPublishedAttributionRecord.model_validate_json(run.output_summary)
    except ValidationError as exc:
        return _defect(
            night,
            _Kind.UNPARSEABLE,
            f"the summary is not a published attribution record ({exc.error_count()} errors)",
        )
    if record.tested_head_sha != head:
        return _defect(
            night,
            _Kind.HEAD_MISMATCH,
            "the record names another commit than its check run",
        )
    if (
        record.repository != repository
        or record.nightly_slot_date != night.slot_date
        or record.nightly_run_id != night.nightly_run_id
    ):
        return _defect(
            night,
            _Kind.IDENTITY_MISMATCH,
            "the record names another repository, slot date or nightly run than the night asked for",
        )
    return record


def _defect(
    night: ModelReceiptNight, kind: EnumRecordDefectKind, detail: str
) -> ModelRecordDefect:
    return ModelRecordDefect(
        slot_date=night.slot_date,
        nightly_run_id=night.nightly_run_id,
        kind=kind,
        detail=detail,
    )
