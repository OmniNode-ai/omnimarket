# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_check_run_effect (OMN-22950): AC1 a check posted on the exact head with the attempt identity inside the wait
bound; AC2 a superseded attempt's check invalidated and a missing check read as missing.

Golden chain: a post reaches the API, is read back on the exact head and the chain closes with a start and a success.
Error chain: every refusal and failure closes the chain with a start and one typed failure code. Four named injected
breaks show the readings can fail for real: ``BrokenHeadApi`` posts on another head, ``LeakyHeadApi`` lists runs of
every head, ``DeafInvalidationApi`` accepts an invalidation and changes nothing, ``DownApi`` refuses every call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink, JsonlEmissionSink
from omnimarket.nodes.node_lab_check_run_effect.adapters.github_check_run_api import (
    GithubCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_effect import (
    HandlerLabCheckRunEffect,
    load_topics,
)
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    EnumLabCheckRunConclusion,
    EnumLabCheckRunOperation,
    EnumLabCheckRunOutcome,
    ModelCheckRunRecord,
    ModelLabCheckRunRequest,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay import (
    ModelGithubSource,
    ModelLabCheckRunOverlay,
)
from omnimarket.nodes.node_lab_check_run_effect.protocols.protocol_check_run_api import (
    CheckRunApiError,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    EnumLabCheckVerdict,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lab_check_run_effect"
)
NOW = datetime(2026, 11, 8, 13, 0, 0, tzinfo=UTC)
REPO = "example-org/example-private-repo"
PUBLIC_REPO = "example-org/example-public-repo"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
CHECK = "lab-tests / run"
CURRENT = "dispatch-41:order-7:overlay-a1"
OLD = "dispatch-40:order-7:overlay-a1"


def overlay(**override: Any) -> ModelLabCheckRunOverlay:
    raw = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    raw["settings"].update(override)
    return ModelLabCheckRunOverlay.model_validate(raw)


def record(
    run_id: int,
    attempt: str,
    status: str = "completed",
    conclusion: str | None = "success",
    head: str = HEAD,
) -> ModelCheckRunRecord:
    return ModelCheckRunRecord(
        check_run_id=run_id,
        repository=REPO,
        name=CHECK,
        head_sha=head,
        attempt_id=attempt,
        status=status,
        conclusion=conclusion,
    )


def request(
    *,
    operation: EnumLabCheckRunOperation = EnumLabCheckRunOperation.POST,
    attempt: str | None = CURRENT,
    conclusion: EnumLabCheckRunConclusion | None = EnumLabCheckRunConclusion.SUCCESS,
    repository: str = REPO,
    head: str = HEAD,
    dispatched_ago: int = 120,
    cid: str = "cid-1",
) -> ModelLabCheckRunRequest:
    return ModelLabCheckRunRequest(
        correlation_id=cid,
        operation=operation,
        repository=repository,
        head_sha=head,
        current_attempt_id=CURRENT,
        dispatched_at=NOW - timedelta(seconds=dispatched_ago),
        now=NOW,
        attempt_id=attempt,
        conclusion=conclusion,
        detail="lab host h051, lane labcheck",
    )


class Rig:
    def __init__(
        self, api: Any = None, ov: ModelLabCheckRunOverlay | None = None
    ) -> None:
        self.api = api if api is not None else InMemoryCheckRunApi()
        self.sink = InMemoryEmissionSink()
        self.handler = HandlerLabCheckRunEffect(
            overlay=ov or overlay(), api=self.api, sink=self.sink, clock=lambda: NOW
        )

    def phases(self) -> list[tuple[str, str | None]]:
        return [(e.phase, e.failure_code) for e in self.sink.events]


# ------------------------------------------------------------------------------------------------ injected breaks


class BrokenHeadApi(InMemoryCheckRunApi):
    """Break: a poster that posts on another head than the one it was asked for."""

    def create_run(self, **kwargs: Any) -> ModelCheckRunRecord:
        kwargs["head_sha"] = OLD_HEAD
        return super().create_run(**kwargs)


class DeafInvalidationApi(InMemoryCheckRunApi):
    """Break: an API that accepts an invalidation and changes nothing."""

    def update_run(self, **kwargs: Any) -> ModelCheckRunRecord:
        self._calls += 1
        return next(r for r in self.runs if r.check_run_id == kwargs["check_run_id"])


class LeakyHeadApi(InMemoryCheckRunApi):
    """Break: a list call that ignores the head and returns every run of the check name."""

    def list_runs(self, **kwargs: Any) -> tuple[ModelCheckRunRecord, ...]:
        self._calls += 1
        return tuple(r for r in self.runs if r.name == kwargs["check_name"])


class DownApi(InMemoryCheckRunApi):
    """Break: the check-run API refuses every call."""

    def list_runs(self, **kwargs: Any) -> tuple[ModelCheckRunRecord, ...]:
        raise CheckRunApiError("GET answered 503")


# ------------------------------------------------------------------------------------------------ AC1 golden


def test_post_on_exact_head_golden_chain() -> None:
    rig = Rig()
    pending = rig.handler.handle(
        request(conclusion=EnumLabCheckRunConclusion.PENDING, cid="g-1")
    )
    assert pending.outcome is EnumLabCheckRunOutcome.POSTED
    assert pending.verdict is EnumLabCheckVerdict.PENDING
    done = rig.handler.handle(request(cid="g-2"))
    assert done.outcome is EnumLabCheckRunOutcome.POSTED
    assert done.within_wait_bound is True
    assert done.verdict is EnumLabCheckVerdict.PASS
    assert done.posted is not None
    assert (done.posted.head_sha, done.posted.attempt_id) == (HEAD, CURRENT)
    assert (done.posted.status, done.posted.conclusion) == ("completed", "success")
    # the pending run was updated, not duplicated
    assert [r.check_run_id for r in rig.api.runs] == [done.posted.check_run_id]
    assert rig.phases() == [
        ("start", None),
        ("success", None),
        ("start", None),
        ("success", None),
    ]
    assert {e.piece for e in rig.sink.events} == {8}


def test_post_on_exact_head_leaves_a_run_of_another_head_alone() -> None:
    other = record(1, CURRENT, head=OLD_HEAD)
    rig = Rig(InMemoryCheckRunApi([other]))
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    assert result.invalidated == ()
    assert other in rig.api.runs
    on_head = [r for r in rig.api.runs if r.head_sha == HEAD]
    assert len(on_head) == 1
    assert on_head[0].attempt_id == CURRENT


def test_post_on_exact_head_failure_posts_a_failed_run_that_reads_failed() -> None:
    rig = Rig()
    result = rig.handler.handle(request(conclusion=EnumLabCheckRunConclusion.FAILURE))
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    assert result.verdict is EnumLabCheckVerdict.FAILED


def test_post_on_exact_head_is_idempotent_for_one_attempt() -> None:
    rig = Rig()
    rig.handler.handle(request(cid="i-1"))
    again = rig.handler.handle(request(cid="i-2"))
    assert again.outcome is EnumLabCheckRunOutcome.POSTED
    assert len(rig.api.runs) == 1


def test_post_on_exact_head_late_is_posted_and_reported_late() -> None:
    rig = Rig()
    result = rig.handler.handle(request(dispatched_ago=901))
    assert result.outcome is EnumLabCheckRunOutcome.POSTED_LATE
    assert result.within_wait_bound is False
    assert result.posted is not None
    assert rig.phases() == [("start", None), ("failure", "lab_check_posted_late")]


def test_post_on_exact_head_inside_the_bound_at_the_bound_edge() -> None:
    rig = Rig()
    assert (
        rig.handler.handle(request(dispatched_ago=900)).outcome
        is EnumLabCheckRunOutcome.POSTED
    )


def test_post_on_exact_head_break_an_api_listing_other_heads_neither_passes_nor_invalidates_them() -> (
    None
):
    elsewhere = record(1, OLD, head=OLD_HEAD)
    rig = Rig(LeakyHeadApi([elsewhere, record(2, CURRENT, head=OLD_HEAD)]))
    read = rig.handler.handle(
        request(
            operation=EnumLabCheckRunOperation.READ,
            attempt=None,
            conclusion=None,
            dispatched_ago=901,
        )
    )
    assert read.verdict is EnumLabCheckVerdict.MISSING, (
        "a pass on another head never reads as this head's"
    )
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    assert result.invalidated == ()
    assert elsewhere in rig.api.runs, "a run of another head is left alone"


# ------------------------------------------------------------------------------------------------ AC1 error chain


def test_post_on_exact_head_refuses_a_repository_that_is_not_lab_bound() -> None:
    rig = Rig()
    result = rig.handler.handle(request(repository=PUBLIC_REPO))
    assert result.outcome is EnumLabCheckRunOutcome.REFUSED_REPOSITORY_NOT_LAB_BOUND
    assert rig.api.calls == 0, "refused before any call"
    assert rig.api.runs == ()
    assert rig.phases() == [
        ("start", None),
        ("failure", "lab_check_repository_not_lab_bound"),
    ]


def test_post_on_exact_head_break_a_poster_on_another_head_is_caught_by_the_readback() -> (
    None
):
    rig = Rig(BrokenHeadApi())
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.FAILED_READBACK_MISSING
    assert result.posted is None
    assert result.verdict is not EnumLabCheckVerdict.PASS
    assert rig.phases() == [("start", None), ("failure", "lab_check_readback_missing")]


def test_post_on_exact_head_api_down_ends_in_a_typed_failure() -> None:
    rig = Rig(DownApi())
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.FAILED_API
    assert "503" in result.detail
    assert rig.phases() == [("start", None), ("failure", "lab_check_api_failed")]
    topic, payload = rig.handler.event_for(result)
    assert topic == load_topics()["check_run_failed"]
    assert payload["outcome"] == "failed_api"


def test_post_on_exact_head_golden_event_goes_to_the_completed_topic() -> None:
    rig = Rig()
    topic, _ = rig.handler.event_for(rig.handler.handle(request()))
    assert topic == load_topics()["check_run_completed"]


# ------------------------------------------------------------------------------------------------ AC2


def test_superseded_and_missing_a_new_attempt_invalidates_the_old_pass() -> None:
    rig = Rig(InMemoryCheckRunApi([record(1, OLD)]))
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    assert result.invalidated == (1,)
    old = next(r for r in rig.api.runs if r.check_run_id == 1)
    assert (old.status, old.conclusion) == ("completed", "cancelled")
    counting = [r for r in rig.api.runs if r.conclusion == "success"]
    assert [r.attempt_id for r in counting] == [CURRENT]


def test_superseded_and_missing_a_superseded_attempt_posts_nothing_and_is_invalidated() -> (
    None
):
    rig = Rig(InMemoryCheckRunApi([record(1, OLD, "in_progress", None)]))
    result = rig.handler.handle(request(attempt=OLD))
    assert result.outcome is EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT
    assert result.posted is None
    assert result.invalidated == (1,)
    assert [r.check_run_id for r in rig.api.runs] == [1], "nothing was posted"
    assert rig.api.runs[0].conclusion == "cancelled"
    assert rig.phases() == [
        ("start", None),
        ("failure", "lab_check_attempt_superseded"),
    ]


def test_superseded_and_missing_a_late_pass_from_a_superseded_attempt_never_reads_pass() -> (
    None
):
    rig = Rig(InMemoryCheckRunApi([record(1, CURRENT, "in_progress", None)]))
    rig.handler.handle(request(attempt=OLD))
    read = rig.handler.handle(
        request(operation=EnumLabCheckRunOperation.READ, attempt=None, conclusion=None)
    )
    assert read.verdict is EnumLabCheckVerdict.PENDING
    assert [r.attempt_id for r in rig.api.runs] == [CURRENT]


def test_superseded_and_missing_a_head_with_only_another_attempt_reads_superseded() -> (
    None
):
    rig = Rig(InMemoryCheckRunApi([record(1, OLD)]))
    read = rig.handler.handle(
        request(operation=EnumLabCheckRunOperation.READ, attempt=None, conclusion=None)
    )
    assert read.outcome is EnumLabCheckRunOutcome.READ
    assert read.verdict is EnumLabCheckVerdict.SUPERSEDED
    assert rig.phases() == [("start", None), ("success", None)]


@pytest.mark.parametrize(
    ("dispatched_ago", "verdict"),
    [
        (120, EnumLabCheckVerdict.PENDING),
        (900, EnumLabCheckVerdict.PENDING),
        (901, EnumLabCheckVerdict.MISSING),
    ],
)
def test_superseded_and_missing_a_missing_check_reads_missing_not_passing(
    dispatched_ago: int, verdict: EnumLabCheckVerdict
) -> None:
    rig = Rig()
    read = rig.handler.handle(
        request(
            operation=EnumLabCheckRunOperation.READ,
            attempt=None,
            conclusion=None,
            dispatched_ago=dispatched_ago,
        )
    )
    assert read.verdict is verdict
    assert read.verdict is not EnumLabCheckVerdict.PASS


def test_superseded_and_missing_a_pass_on_another_head_does_not_make_this_head_pass() -> (
    None
):
    rig = Rig(InMemoryCheckRunApi([record(1, CURRENT, head=OLD_HEAD)]))
    read = rig.handler.handle(
        request(
            operation=EnumLabCheckRunOperation.READ,
            attempt=None,
            conclusion=None,
            dispatched_ago=901,
        )
    )
    assert read.verdict is EnumLabCheckVerdict.MISSING


def test_superseded_and_missing_break_an_api_that_ignores_invalidation_fails_the_post() -> (
    None
):
    rig = Rig(DeafInvalidationApi([record(1, OLD)]))
    result = rig.handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ
    assert rig.phases() == [
        ("start", None),
        ("failure", "lab_check_invalidation_not_read"),
    ]


def test_superseded_and_missing_break_a_deaf_api_fails_a_superseded_refusal_too() -> (
    None
):
    rig = Rig(DeafInvalidationApi([record(1, OLD)]))
    result = rig.handler.handle(request(attempt=OLD))
    assert result.outcome is EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ


def test_superseded_and_missing_an_old_failure_is_left_alone() -> None:
    rig = Rig(InMemoryCheckRunApi([record(1, OLD, conclusion="failure")]))
    result = rig.handler.handle(request())
    assert result.invalidated == ()
    assert rig.api.runs[0].conclusion == "failure"


# ------------------------------------------------------------------------------------------------ overlay and adapter


def test_overlay_refuses_a_superseded_conclusion_that_counts_as_a_pass() -> None:
    with pytest.raises(ValueError, match="superseded conclusion"):
        overlay(superseded_conclusion="failure", passing_conclusions=["failure"])


def test_overlay_has_no_defaults_an_absent_key_refuses() -> None:
    raw = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    del raw["settings"]["wait_bound_seconds"]
    with pytest.raises(ValueError, match="wait_bound_seconds"):
        ModelLabCheckRunOverlay.model_validate(raw)


def test_a_journal_emission_keeps_the_chain_for_the_floor(tmp_path: Path) -> None:
    ov = overlay()
    emission = ov.emission.model_copy(
        update={"backend": "journal", "journal_path": str(tmp_path / "j.jsonl")}
    )
    handler = HandlerLabCheckRunEffect(
        overlay=ov.model_copy(update={"emission": emission}),
        api=InMemoryCheckRunApi(),
        clock=lambda: NOW,
    )
    handler.handle(request())
    events = JsonlEmissionSink(tmp_path / "j.jsonl").read(NOW - timedelta(days=1))
    assert [e.phase for e in events] == ["start", "success"]


class FakeGithub:
    """A recording stand-in for the HTTP transport that answers like the check-run API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.runs: list[dict[str, Any]] = []

    def request(
        self, method: str, url: str, headers: Any, body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        import json

        self.calls.append((method, url, dict(headers), body))
        if method == "GET":
            return 200, json.dumps({"check_runs": self.runs}).encode()
        payload = json.loads(body or b"{}")
        if method == "POST":
            row = {
                "id": 100 + len(self.runs),
                **{k: v for k, v in payload.items() if k != "output"},
            }
            self.runs.append(row)
            return 201, json.dumps(row).encode()
        run_id = int(url.rsplit("/", 1)[1])
        row = next(r for r in self.runs if r["id"] == run_id)
        row.update({k: v for k, v in payload.items() if k != "output"})
        return 200, json.dumps(row).encode()


def github_source() -> ModelGithubSource:
    raw = yaml.safe_load(
        (NODE_DIR / "overlay.github.example.yaml").read_text(encoding="utf-8")
    )
    return ModelGithubSource.model_validate(raw["source"])


def test_github_adapter_posts_the_attempt_as_external_id_on_the_exact_head() -> None:
    http = FakeGithub()
    source = github_source()
    api = GithubCheckRunApi(source, http, {source.token_env: "synthetic-token"})
    ov = overlay().model_copy(update={"source": source})
    handler = HandlerLabCheckRunEffect(
        overlay=ov, api=api, sink=InMemoryEmissionSink(), clock=lambda: NOW
    )
    result = handler.handle(request())
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    post = next(c for c in http.calls if c[0] == "POST")
    import json

    body = json.loads(post[3] or b"{}")
    assert (body["head_sha"], body["external_id"], body["name"]) == (
        HEAD,
        CURRENT,
        CHECK,
    )
    assert (body["status"], body["conclusion"]) == ("completed", "success")
    assert post[2]["Authorization"] == "Bearer synthetic-token"


def test_github_adapter_names_a_missing_token_without_a_value() -> None:
    source = github_source()
    api = GithubCheckRunApi(source, FakeGithub(), {})
    with pytest.raises(CheckRunApiError, match=source.token_env):
        api.list_runs(repository=REPO, head_sha=HEAD, check_name=CHECK)


def test_github_adapter_stops_at_the_call_budget() -> None:
    http = FakeGithub()
    source = github_source().model_copy(update={"max_calls": 1})
    api = GithubCheckRunApi(source, http, {source.token_env: "synthetic-token"})
    api.list_runs(repository=REPO, head_sha=HEAD, check_name=CHECK)
    with pytest.raises(CheckRunApiError, match="call budget"):
        api.list_runs(repository=REPO, head_sha=HEAD, check_name=CHECK)
    assert len(http.calls) == 1
