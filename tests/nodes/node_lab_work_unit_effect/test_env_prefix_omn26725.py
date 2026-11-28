"""``env NAME=value ... <command>`` is parsed, not refused for the word ``env``.

The recorded OMN-24028 item sends ``env PYTHONDONTWRITEBYTECODE=1 uv run ...``
to a lab host. The host used to refuse it because ``env`` is not allowed; the
allowlist must judge the command after the assignments, and ``env`` must never
become a way to run a program the allowlist does not hold.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
    split_env_prefix,
)

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-24028.yaml"


class _RecordingRunner:
    def __init__(self) -> None:
        self.ran: list[ModelLabWorkUnitRequest] = []

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.ran.append(request)
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=0,
        )


def _request(argv: list[str]) -> ModelLabWorkUnitRequest:
    return ModelLabWorkUnitRequest(
        work_unit_id="lw-test-26725",
        target_host="h1",
        repo="OmniNode-ai/omnimarket",
        commit_sha="a" * 40,
        argv=argv,
        lane="lane-x",
    )


def _recorded_item_argv() -> list[str]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    item = next(
        e
        for e in document["dod_evidence"]
        if e["id"] == "dod-omn24028-integration-proof"
    )
    words = shlex.split(item["checks"][0]["check_value"])
    return words[words.index("--") + 1 :]


def test_recorded_omn24028_item_starts_with_env() -> None:
    assert _recorded_item_argv()[0] == "env"


@pytest.mark.asyncio
async def test_recorded_omn24028_item_is_placed_and_run() -> None:
    runner = _RecordingRunner()
    handler = HandlerLabWorkUnitEffect(host_name="h1", runner=runner)
    receipt = await handler.handle(_request(_recorded_item_argv()))
    assert receipt.status == EnumLabWorkUnitStatus.COMPLETED
    assert len(runner.ran) == 1


def test_split_reads_assignments_and_command() -> None:
    assignments, command = split_env_prefix(["env", "A=1", "B=x=y", "uv", "run", "C=3"])
    assert assignments == {"A": "1", "B": "x=y"}
    assert command == ["uv", "run", "C=3"]


def test_split_leaves_a_plain_argv_alone() -> None:
    assert split_env_prefix(["uv", "run"]) == ({}, ["uv", "run"])


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "A=1", "bash", "-c", "id"],
        ["env", "bash"],
        ["env", "A=1", "env", "B=2", "bash"],
        ["env", "-i", "uv", "run"],
        ["env", "A=1"],
        ["env"],
    ],
)
@pytest.mark.asyncio
async def test_env_followed_by_a_disallowed_or_missing_command_is_refused(
    argv: list[str],
) -> None:
    runner = _RecordingRunner()
    handler = HandlerLabWorkUnitEffect(host_name="h1", runner=runner)
    receipt = await handler.handle(_request(argv))
    assert receipt.status == EnumLabWorkUnitStatus.REFUSED
    assert "nothing was fetched or run" in receipt.detail
    assert runner.ran == []
