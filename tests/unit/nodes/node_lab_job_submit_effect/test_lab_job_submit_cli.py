# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_job_submit_effect submit --spec-file``:
one validated spec, one publish keyed by job_id (OMN-20604)."""

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.delegated_test_loop.lane_bus import BusKind, LabRunBusError
from omnimarket.models.lab_job import ModelLabJobSpec
from omnimarket.nodes.node_lab_job_submit_effect import __main__ as cli
from omnimarket.nodes.node_lab_job_submit_effect.__main__ import lab_job_group
from omnimarket.nodes.node_lab_job_submit_effect.handlers.handler_lab_job_submit_effect import (
    load_lab_job_submitted_topic,
)

pytestmark = pytest.mark.unit

LAB_JOB_SUBMITTED_TOPIC_V1 = load_lab_job_submitted_topic()

JOB_ID = "lj-0123456789abcdef"
SHA = "7579a64e614156dbb1f2f29afccc36b802a46666"


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "job_id": JOB_ID,
        "kind": "lane",
        "brief": "do the work",
        "repo": "OmniNode-ai/omnimarket",
        "ref": SHA,
        "engine": "opus",
        "host_preference": ["h101"],
        "time_box_min": 50,
        "retry_policy": {
            "max_attempts": 2,
            "backoff_s": 300,
            "on_time_box": "continue",
        },
        "done_criteria": [{"kind": "terminal_row"}],
        "parent_lane": "review-session-9143",
        "ticket": "OMN-20604",
        "stall_after_min": 20,
    }
    spec.update(overrides)
    return spec


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: object = None
    ) -> None:
        self.published.append((topic, key, value))


def _stub_bus(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> tuple[_Bus, list[dict[str, object]]]:
    bus = _Bus()
    opened: list[dict[str, object]] = []

    @contextlib.asynccontextmanager
    async def open_bus(
        *,
        bus: BusKind,
        lane: str | None,
        kafka_bootstrap: str | None,
        omni_home: Path | None,
    ) -> AsyncIterator[_Bus]:
        opened.append({"bus": bus, "lane": lane, "bootstrap": kafka_bootstrap})
        if fail:
            raise LabRunBusError("lane login unavailable")
        yield bus_

    bus_ = bus
    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)
    return bus, opened


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "r.job.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_topic_comes_from_the_contract() -> None:
    assert LAB_JOB_SUBMITTED_TOPIC_V1.startswith(
        "onex.cmd.omnimarket.lab-job-submitted."
    )


def test_runner_record_publishes_its_spec_keyed_by_job_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus, opened = _stub_bus(monkeypatch)
    record = {
        "topic": LAB_JOB_SUBMITTED_TOPIC_V1,
        "spec": _spec(),
        "lane": "jobs-submit-9143",
        "run_id": "r",
        "attempt": 1,
    }
    path = _write(tmp_path, record)

    result = CliRunner().invoke(
        lab_job_group,
        ["submit", "--spec-file", str(path), "--bus-lane", "dev"],
    )

    assert result.exit_code == 0, result.output
    assert opened == [{"bus": "kafka", "lane": "dev", "bootstrap": None}]
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == LAB_JOB_SUBMITTED_TOPIC_V1
    assert key == JOB_ID.encode("utf-8")
    envelope = json.loads(value)
    assert ModelLabJobSpec.model_validate(envelope["payload"]) == (
        ModelLabJobSpec.model_validate(_spec())
    )
    assert envelope["correlation_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, JOB_ID))
    assert envelope["event_type"] == "omnimarket.lab-job-submitted"
    receipt = json.loads(result.output.strip().splitlines()[-1])
    assert receipt == {
        "status": "published",
        "job_id": JOB_ID,
        "topic": LAB_JOB_SUBMITTED_TOPIC_V1,
    }


def test_bare_spec_file_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus, _ = _stub_bus(monkeypatch)
    path = _write(tmp_path, _spec())

    result = CliRunner().invoke(lab_job_group, ["submit", "--spec-file", str(path)])

    assert result.exit_code == 0, result.output
    assert [key for _, key, _ in bus.published] == [JOB_ID.encode("utf-8")]


def test_resubmitting_the_same_job_publishes_the_same_key_and_correlation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus, _ = _stub_bus(monkeypatch)
    path = _write(tmp_path, _spec())

    for _ in range(2):
        assert (
            CliRunner()
            .invoke(lab_job_group, ["submit", "--spec-file", str(path)])
            .exit_code
            == 0
        )

    keys = {key for _, key, _ in bus.published}
    correlations = {
        json.loads(value)["correlation_id"] for _, _, value in bus.published
    }
    assert keys == {JOB_ID.encode("utf-8")}
    assert len(correlations) == 1


@pytest.mark.parametrize(
    "data",
    [
        _spec(host_preference=None),
        _spec(done_criteria=[{"kind": "pr_merged", "pr": "OmniNode-ai/omnimarket#1"}]),
        _spec(ref="dev"),
        _spec(job_id="lj-short"),
        _spec(repo=None),
        {"topic": "onex.cmd.omnimarket.other.v1", "spec": _spec()},
        ["not", "an", "object"],
    ],
    ids=[
        "host_preference_null",
        "done_criterion_not_canonical",
        "ref_not_a_sha",
        "job_id_shape",
        "repo_null",
        "other_topic",
        "not_an_object",
    ],
)
def test_a_non_canonical_record_is_refused_and_nothing_is_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: object
) -> None:
    bus, opened = _stub_bus(monkeypatch)
    path = _write(tmp_path, data)

    result = CliRunner().invoke(lab_job_group, ["submit", "--spec-file", str(path)])

    assert result.exit_code == cli.EXIT_REFUSED, result.output
    assert bus.published == []
    assert opened == []


def test_unreadable_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "r.job.json"
    path.write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(lab_job_group, ["submit", "--spec-file", str(path)])

    assert result.exit_code == cli.EXIT_REFUSED


def test_bus_unavailable_exits_69(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus, _ = _stub_bus(monkeypatch, fail=True)
    path = _write(tmp_path, _spec())

    result = CliRunner().invoke(lab_job_group, ["submit", "--spec-file", str(path)])

    assert result.exit_code == cli.EXIT_BUS_UNAVAILABLE
    assert bus.published == []
