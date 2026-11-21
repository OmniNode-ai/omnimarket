# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record source that reads the published nightly records through the repository gateway node.

Golden chain: seven published records read, the receipt validated. Error chain: every absence and every
malformed or mismatched record is a typed violation, never a clean night, and an unreachable gateway keeps
``records_unavailable``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelCheckRun,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.gateway_read_in_memory import (
    GatewayReadInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_gateway import (
    SourceAttributionRecordsGateway,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_receipt_validation import (
    HandlerStageReceiptValidation,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation as Code,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelStageReceipt,
    ModelStageReceiptValidationRequest,
    ModelStageReceiptValidationResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_attribution_record_source import (
    ProtocolAttributionRecordSource,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "ci" / "fixtures" / "selection_stage"
REPO = "OmniNode-ai/omnimarket"
PREFIX = load_policy().record_check_name_prefix


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _records() -> list[ModelPublishedAttributionRecord]:
    return [
        ModelPublishedAttributionRecord.model_validate(r)
        for r in _load("records_valid.json")
    ]


def _name(record: ModelPublishedAttributionRecord) -> str:
    return f"{PREFIX}:{record.nightly_slot_date.isoformat()}"


def _published(
    records: list[ModelPublishedAttributionRecord],
) -> GatewayReadInMemory:
    gateway = GatewayReadInMemory()
    for record in records:
        gateway.publish(record.tested_head_sha, _name(record), record.model_dump_json())
    return gateway


def _validate(gateway: GatewayReadInMemory) -> ModelStageReceiptValidationResult:
    source: ProtocolAttributionRecordSource = SourceAttributionRecordsGateway(
        gateway, load_policy()
    )
    handler = HandlerStageReceiptValidation(source, load_policy())
    return handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=1,
            base_stage=0,
            receipts=(
                ModelStageReceipt(stage=0),
                ModelStageReceipt.model_validate(_load("receipt_valid.json")),
            ),
        )
    )


def _codes(result: ModelStageReceiptValidationResult) -> list[Code]:
    return [violation.code for violation in result.violations]


# -- golden chain ---------------------------------------------------------------------


def test_attribution_source_golden_chain_seven_published_records_validate() -> None:
    gateway = _published(_records())
    result = _validate(gateway)
    assert result.valid, [v.model_dump() for v in result.violations]
    # one read per night, each for the check run of that slot on the commit the receipt names
    assert len(gateway.requests) == 7
    for request, record in zip(gateway.requests, _records(), strict=True):
        assert request.operation is EnumGithubGatewayOperation.COMMIT_CHECK_RUNS
        assert request.repo == REPO
        assert request.head_sha == record.tested_head_sha
        assert request.check_name == _name(record)
        assert request.include_output is True


def test_attribution_source_makes_no_call_until_it_is_asked() -> None:
    gateway = GatewayReadInMemory()
    SourceAttributionRecordsGateway(gateway, load_policy())
    assert gateway.requests == []


# -- AC2: the count is recomputed from the parsed records ----------------------------------


@pytest.mark.parametrize(
    "dirty",
    [
        {"status": "incomplete", "incomplete_count": 1},
        {"miss_count": 1},
        {"unattributable_count": 1},
        {"pull_request_record_count": 0},
    ],
)
def test_adapter_clean_night_a_dirty_record_is_not_counted(
    dirty: dict[str, Any],
) -> None:
    records = _records()
    fields = {**records[3].model_dump(), **dirty}
    if dirty.get("status") == "incomplete":
        fields |= {"interaction_count": 0, "preexisting_count": 0}
    published = ModelPublishedAttributionRecord.model_validate(fields)
    result = _validate(_published([*records[:3], published, *records[4:]]))
    assert Code.NIGHT_NOT_CLEAN in _codes(result)
    assert Code.FIGURE_MISMATCH in _codes(
        result
    )  # the receipt claims seven clean nights
    assert not result.valid


def test_adapter_clean_night_the_receipts_claim_is_never_the_count() -> None:
    """The receipt claims seven clean nights; the published records, read through the gateway, say six."""
    records = _records()
    dirty = ModelPublishedAttributionRecord.model_validate(
        {**records[0].model_dump(), "miss_count": 2}
    )
    result = _validate(_published([dirty, *records[1:]]))
    detail = " ".join(v.detail for v in result.violations)
    assert "clean_nights 7 (recomputed 6)" in detail


# -- AC3: typed absences ---------------------------------------------------------------------


def test_adapter_absence_a_night_with_no_check_run_is_record_missing() -> None:
    result = _validate(_published(_records()[:-1]))
    assert _codes(result) == [Code.RECORD_MISSING, Code.FIGURE_MISMATCH]


def test_adapter_absence_an_unparseable_summary_is_record_mismatch() -> None:
    records = _records()
    gateway = _published(records[:-1])
    gateway.publish(records[-1].tested_head_sha, _name(records[-1]), "not json")
    result = _validate(gateway)
    assert Code.RECORD_MISMATCH in _codes(result)
    assert Code.NIGHT_NOT_CLEAN not in _codes(result)
    assert not result.valid
    assert "not json" not in " ".join(v.detail for v in result.violations)


def test_adapter_absence_a_summary_with_a_field_more_is_unparseable() -> None:
    records = _records()
    gateway = _published(records[:-1])
    last = records[-1].model_dump(mode="json") | {"free_text": "x"}
    gateway.publish(records[-1].tested_head_sha, _name(records[-1]), json.dumps(last))
    assert Code.RECORD_MISMATCH in _codes(_validate(gateway))


def test_adapter_absence_a_check_run_with_no_summary_is_record_mismatch() -> None:
    records = _records()
    gateway = _published(records[:-1])
    gateway.publish(records[-1].tested_head_sha, _name(records[-1]), None)
    assert Code.RECORD_MISMATCH in _codes(_validate(gateway))


def test_adapter_absence_a_check_run_still_running_is_record_mismatch() -> None:
    records = _records()
    gateway = _published(records[:-1])
    gateway.publish(
        records[-1].tested_head_sha,
        _name(records[-1]),
        records[-1].model_dump_json(),
        status="in_progress",
        conclusion=None,
    )
    assert Code.RECORD_MISMATCH in _codes(_validate(gateway))


def test_adapter_absence_two_records_for_one_slot_is_duplicate_record() -> None:
    records = _records()
    gateway = _published(records)
    gateway.publish(
        records[2].tested_head_sha, _name(records[2]), records[2].model_dump_json()
    )
    result = _validate(gateway)
    assert Code.DUPLICATE_RECORD in _codes(result)
    assert not result.valid


def test_adapter_absence_a_record_naming_another_commit_is_record_mismatch() -> None:
    records = _records()
    elsewhere = ModelPublishedAttributionRecord.model_validate(
        {**records[1].model_dump(), "tested_head_sha": "f" * 40}
    )
    gateway = _published([records[0], *records[2:]])
    gateway.publish(
        records[1].tested_head_sha, _name(records[1]), elsewhere.model_dump_json()
    )
    result = _validate(gateway)
    assert Code.RECORD_MISMATCH in _codes(result)
    assert not result.valid


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "OmniNode-ai/other"},
        {"nightly_run_id": 1234},
    ],
)
def test_adapter_absence_a_record_for_another_repository_or_run_is_refused(
    change: dict[str, Any],
) -> None:
    records = _records()
    other = ModelPublishedAttributionRecord.model_validate(
        {**records[1].model_dump(), **change}
    )
    gateway = _published([records[0], *records[2:]])
    gateway.publish(
        records[1].tested_head_sha, _name(records[1]), other.model_dump_json()
    )
    assert Code.RECORD_MISMATCH in _codes(_validate(gateway))


def test_adapter_absence_an_unreachable_gateway_keeps_records_unavailable() -> None:
    gateway = _published(_records())
    gateway.unreachable = True
    result = _validate(gateway)
    assert _codes(result) == [Code.RECORDS_UNAVAILABLE]


def test_adapter_absence_a_gateway_that_answers_another_operation_is_unavailable() -> (
    None
):
    class Wrong:
        def read(self, request: ModelGithubGatewayRequest) -> Any:
            from omnimarket.models.model_github_gateway_io import (
                ModelWorkflowRunsResult,
            )

            return ModelWorkflowRunsResult(repo=request.repo, truncated=False)

    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsGateway(Wrong(), load_policy()), load_policy()
    )
    result = handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=1,
            base_stage=0,
            receipts=(
                ModelStageReceipt(stage=0),
                ModelStageReceipt.model_validate(_load("receipt_valid.json")),
            ),
        )
    )
    assert _codes(result) == [Code.RECORDS_UNAVAILABLE]


# -- the gateway's own read carries the summary only when asked ----------------------------------


class _Transport:
    """Serves one check run list the way GitHub does, with an output block."""

    def __init__(self, summary: str | None) -> None:
        self._summary = summary

    def send_sync(self, request: Any) -> Any:
        from omnimarket.github_landing.model_github_http_exchange import (
            ModelGithubHttpResponse,
        )

        body = {
            "total_count": 1,
            "check_runs": [
                {
                    "id": 5,
                    "name": "nightly-attribution:2026-12-01",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "x"},
                    "check_suite": {"head_branch": "dev"},
                    "output": {"title": "t", "summary": self._summary},
                }
            ],
        }
        return ModelGithubHttpResponse(status=200, body=body, headers={})


def test_attribution_source_gateway_read_returns_the_summary_only_with_include_output() -> (
    None
):
    from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
        read_commit_check_runs,
    )

    def run(include: bool) -> ModelCheckRun:
        result = read_commit_check_runs(
            cast(Any, _Transport("{}")),
            ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
                repo=REPO,
                head_sha="a" * 40,
                include_output=include,
            ),
        )
        assert isinstance(result, ModelCommitCheckRunsResult)
        return result.check_runs[0]

    assert run(True).output_summary == "{}"
    assert run(False).output_summary is None


# -- the policy holds the settings --------------------------------------------------------------


def test_attribution_source_settings_live_in_policy_yaml() -> None:
    policy_path = (
        REPO_ROOT
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_test_selection_selector_compute"
        / "policy.yaml"
    )
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert raw["record_check_name_prefix"] == load_policy().record_check_name_prefix
    assert "#   record_check_name_prefix " in policy_path.read_text(encoding="utf-8")


def test_attribution_source_wire_shape_is_unchanged_unless_the_summary_is_asked_for() -> (
    None
):
    plain = ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.COMMIT_CHECK_RUNS,
        repo=REPO,
        head_sha="a" * 40,
    )
    asked = plain.model_copy(update={"include_output": True})
    assert "include_output" not in plain.model_dump(mode="json")
    assert asked.model_dump(mode="json")["include_output"] is True
    assert ModelGithubGatewayRequest.model_validate(
        asked.model_dump(mode="json")
    ).include_output
    bare = ModelCheckRun(id=1, name="n", status="completed")
    assert "output_summary" not in bare.model_dump(mode="json")
    with_summary = bare.model_copy(update={"output_summary": "{}"})
    assert (
        ModelCheckRun.model_validate(
            with_summary.model_dump(mode="json")
        ).output_summary
        == "{}"
    )
