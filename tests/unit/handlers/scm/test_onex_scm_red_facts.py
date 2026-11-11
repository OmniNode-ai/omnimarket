# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm red-facts`` (OMN-23832): the failing step and lines of a red head, from the filler's record.

The verb is local: it opens no bus (zero gateway publishes), so it spends no budget unit. A bus that
fails the test when it is opened stands in for "no publish, no GitHub request". The error chain: a
head that moved reads ``not_filled`` with none of the old head's text; an overlay that cannot be read
is unanswered (exit 3) and never falls back to the gateway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.handler_scm_dispatch import SCM_LOCAL_VERBS
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_red_facts import (
    RedFactsStore,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_red_facts import (
    ModelRedFactsRecord,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
OLD_TEXT = "FAILED tests/nodes/x.py::test_old_head_only - AssertionError"


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    publishes: list[str] = []

    def refuse(_: dict[str, object]) -> NoReturn:
        publishes.append("opened the bus")
        raise AssertionError("red-facts opened the bus")

    monkeypatch.setattr(scm_cli, "_open_bus", refuse)
    overlay = tmp_path / "filler-overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "github_owner": "OmniNode-ai",
                "watcher_state_env": "UNUSED_WATCHER_STATE",
                "pr_classes": ["red"],
                "always_job_names": [],
                "max_reads_per_pass": 1,
                "reader_concurrency": 1,
                "reply_tail_bytes": 4096,
                "reply_timeout_seconds": 5,
                "reprobe_seconds": 1800,
                "red_facts_state_env": "RED_FACTS_STATE_DIR",
                "red_facts_subdir": "red-facts",
                "red_facts_tail_bytes": 65536,
                "red_facts_max_lines": 12,
                "red_facts_line_chars": 240,
                "default_profile": "inmemory",
                "profiles": {"inmemory": {"kind": "inmemory"}},
            }
        )
    )
    monkeypatch.setenv("ONEX_JOB_LOG_FILLER_OVERLAY", str(overlay))
    monkeypatch.setenv("RED_FACTS_STATE_DIR", str(tmp_path / "state"))
    store = RedFactsStore(tmp_path / "state" / "red-facts")
    store.write(
        ModelRedFactsRecord(
            repo=REPO,
            pr_number=77,
            head_sha=HEAD,
            check="Tests Gate",
            job_id=9001,
            failing_step="Run pytest",
            digest_lines=(OLD_TEXT,),
            digest_kind="log_lines",
            cache_path="/cache",
            log_total_bytes=10,
            recorded_at="2026-11-11T16:00:00Z",
        )
    )
    return {"publishes": publishes, "tmp": tmp_path}


def _watcher(tmp_path: Path, *, head: str) -> Path:
    tick = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "schema": 1,
        "last_tick": tick,
        "prs": {
            "omnimarket#77": {
                "cls": "red",
                "ci": {"read_at": tick, "red": ["Tests Gate"], "detail": []},
                "facts": {"number": 77, "state": "OPEN", "head_sha": head},
            }
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _run(lane: dict[str, Any], head: str):
    state = _watcher(lane["tmp"], head=head)
    return CliRunner().invoke(
        scm_cli.scm_group,
        ["red-facts", f"{REPO}#77", "--watcher-state", str(state)],
        catch_exceptions=False,
    )


def test_red_facts_is_a_local_verb_with_no_node_operation() -> None:
    assert "red-facts" in {v.verb for v in SCM_LOCAL_VERBS}


def test_red_facts_answers_from_the_record_and_opens_no_bus(lane) -> None:
    result = _run(lane, HEAD)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    assert answer["verb"] == "red-facts"
    assert answer["result"]["status"] == "found"
    [record] = answer["result"]["records"]
    assert (record["check"], record["job_id"], record["failing_step"]) == (
        "Tests Gate",
        9001,
        "Run pytest",
    )
    assert record["digest_lines"] == [OLD_TEXT]
    assert lane["publishes"] == []  # zero gateway publishes, so zero budget charges


def test_error_a_head_that_moved_reads_not_filled_and_none_of_the_old_text(
    lane,
) -> None:
    result = _run(lane, NEW_HEAD)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout.strip().splitlines()[-1])["result"]
    assert answer["status"] == "not_filled"
    assert answer["head_sha"] == NEW_HEAD
    assert answer["records"] == []
    assert isinstance(answer["head_age_seconds"], int)
    assert "test_old_head_only" not in result.stdout
    assert lane["publishes"] == []


def test_error_an_unreadable_overlay_is_unanswered_and_never_asks_the_gateway(
    lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RED_FACTS_STATE_DIR")
    result = _run(lane, HEAD)
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "RED_FACTS_STATE_DIR" in result.output
    assert lane["publishes"] == []


def test_error_a_pr_the_watcher_does_not_hold_is_unanswered(lane) -> None:
    state = _watcher(lane["tmp"], head=HEAD)
    result = CliRunner().invoke(
        scm_cli.scm_group,
        ["red-facts", f"{REPO}#78", "--watcher-state", str(state)],
        catch_exceptions=False,
    )
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert lane["publishes"] == []
