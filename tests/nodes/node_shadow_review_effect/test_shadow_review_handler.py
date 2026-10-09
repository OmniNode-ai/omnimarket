# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store behaviour, the secret scan and the arm split of the shadow tick (OMN-20422)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_shadow_review_effect.handlers import shadow_effects
from omnimarket.nodes.node_shadow_review_effect.handlers.handler_shadow_review import (
    HandlerShadowReview,
)
from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_effects import (
    LabShadowReviewer,
    scan_for_secrets,
)
from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    EnumShadowArm,
    EnumShadowArmStatus,
    EnumShadowStratum,
    ModelShadowArmResult,
    ModelShadowReviewCandidate,
    ModelShadowReviewPolicy,
    ModelShadowReviewRequest,
)

DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
SECRET_DIFF = DIFF + "+key = 'AKIA" + "ABCDEFGHIJKLMNOP'\n"


def pr(repo: str, n: int) -> ModelShadowReviewCandidate:
    return ModelShadowReviewCandidate(
        repo=repo,
        number=n,
        created_at="2026-10-09T11:00:00Z",
        head_sha="b" * 40,
        head_ref=f"br{n}",
        base="dev",
        title="feat: x",
    )


class FakeDiffs:
    def __init__(self, diffs: dict[str, str | None]):
        self.diffs = diffs
        self.calls: list[str] = []

    def diff(self, c: ModelShadowReviewCandidate) -> tuple[str | None, str]:
        self.calls.append(c.key)
        d = self.diffs.get(c.key)
        return (d, "ok") if d is not None else (None, "head-unavailable")


class FakeReviewer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, EnumShadowStratum]] = []

    def review(
        self,
        c: ModelShadowReviewCandidate,
        diff: str,
        stratum: EnumShadowStratum,
        work_dir: Path,
    ) -> tuple[ModelShadowArmResult, ...]:
        self.calls.append((c.key, stratum))
        return (
            ModelShadowArmResult(
                arm=EnumShadowArm.CODEX, status=EnumShadowArmStatus.OK, wall_s=1.0
            ),
        )


def request(
    tmp: Path, *cands: ModelShadowReviewCandidate, dry_run: bool = False
) -> ModelShadowReviewRequest:
    return ModelShadowReviewRequest(
        candidates=cands,
        policy=ModelShadowReviewPolicy(
            window_start="2026-10-09T10:00:00Z", max_reviews_per_tick=5
        ),
        store_root=tmp / "store",
        dry_run=dry_run,
    )


def test_tick_writes_one_record_per_pr_and_never_repeats(tmp_path: Path) -> None:
    diffs = FakeDiffs({"omnimarket#1": DIFF, "omnibase_internal#2": DIFF})
    reviewer = FakeReviewer()
    handler = HandlerShadowReview(diffs, reviewer, clock=lambda: "2026-10-09T12:00:00Z")
    req = request(tmp_path, pr("omnimarket", 1), pr("omnibase_internal", 2))
    first = handler.handle(req)
    # equal created_at: ordered by key
    assert [r.key for r in first.records] == ["omnibase_internal#2", "omnimarket#1"]
    assert first.public_reviewed_total == 1
    rec = json.loads((tmp_path / "store/records/omnimarket__1.json").read_text())
    assert rec["head_sha"] == "b" * 40
    assert rec["posted"] is False
    assert (tmp_path / "store/artifacts/omnimarket__1/diff.patch").read_text() == DIFF
    second = handler.handle(req)
    assert second.records == ()
    assert {s.reason for s in second.selections} == {"already-in-store"}
    assert len(reviewer.calls) == 2
    ticks = (tmp_path / "store/ticks.jsonl").read_text().splitlines()
    assert len(ticks) == 2
    assert json.loads(ticks[1])["public_reviewed_total"] == 1


def test_secret_pattern_drops_the_pr_before_any_reviewer_call(tmp_path: Path) -> None:
    reviewer = FakeReviewer()
    handler = HandlerShadowReview(FakeDiffs({"omnimarket#1": SECRET_DIFF}), reviewer)
    result = handler.handle(request(tmp_path, pr("omnimarket", 1)))
    assert reviewer.calls == []
    assert [(d.key, d.reason) for d in result.dropped] == [
        ("omnimarket#1", "secret-pattern:aws-access-key")
    ]
    drop = json.loads((tmp_path / "store/drops/omnimarket__1.json").read_text())
    assert "AKIA" not in json.dumps(drop)
    assert not (tmp_path / "store/artifacts/omnimarket__1").exists()


def test_unobtainable_head_is_dropped_and_counted_once(tmp_path: Path) -> None:
    diffs = FakeDiffs({})
    handler = HandlerShadowReview(diffs, FakeReviewer())
    handler.handle(request(tmp_path, pr("omnimarket", 1)))
    handler.handle(request(tmp_path, pr("omnimarket", 1)))
    assert diffs.calls == ["omnimarket#1"]


def test_boot_resolved_handler_without_arms_refuses_before_any_io(
    tmp_path: Path,
) -> None:
    # The runtime boot resolver builds the handler from the injectable
    # container alone; such a handler has no arms and must not run a tick.
    handler = HandlerShadowReview(container=object())
    with pytest.raises(RuntimeError, match="lab-host entry point"):
        handler.handle(request(tmp_path, pr("omnimarket", 1)))
    assert not (tmp_path / "store").exists()


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    reviewer = FakeReviewer()
    result = HandlerShadowReview(FakeDiffs({"omnimarket#1": DIFF}), reviewer).handle(
        request(tmp_path, pr("omnimarket", 1), dry_run=True)
    )
    assert result.selections[0].decision.value == "review"
    assert reviewer.calls == []
    assert not (tmp_path / "store").exists()


def test_scan_reads_added_lines_only() -> None:
    assert scan_for_secrets(DIFF) == ()
    removed = DIFF + "-old = '-----BEGIN RSA PRIVATE KEY-----'\n"
    assert scan_for_secrets(removed) == ()
    assert scan_for_secrets(DIFF + "+-----BEGIN OPENSSH PRIVATE KEY-----\n") == (
        "private-key-block",
    )
    assert scan_for_secrets(DIFF + "+API_KEY = 'abcdefghijklmnopqrstuvwx'\n") == (
        "assigned-secret",
    )
    assert scan_for_secrets(DIFF + "+token = os.environ['X']\n") == ()


@pytest.mark.parametrize(
    ("stratum", "arms"),
    [(EnumShadowStratum.PUBLIC, ["A1", "A2"]), (EnumShadowStratum.PRIVATE, ["A1"])],
)
def test_glm_arm_runs_on_public_prs_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stratum: EnumShadowStratum,
    arms: list[str],
) -> None:
    reviewer = LabShadowReviewer(
        omniintelligence_dir=tmp_path,
        venv_dir=tmp_path,
        harness_script=tmp_path / "h.py",
        codex_timeout_s=1,
        glm_call_budget_s=1,
    )
    ran: list[str] = []

    def arm(name: EnumShadowArm) -> Callable[..., ModelShadowArmResult]:
        def run(*_a: object) -> ModelShadowArmResult:
            ran.append(name.value)
            return ModelShadowArmResult(
                arm=name, status=EnumShadowArmStatus.OK, wall_s=0
            )

        return run

    monkeypatch.setattr(reviewer, "_codex", arm(EnumShadowArm.CODEX))
    monkeypatch.setattr(reviewer, "_glm", arm(EnumShadowArm.GLM))
    out = reviewer.review(pr("omnimarket", 1), DIFF, stratum, tmp_path)
    assert [a.arm.value for a in out] == arms == ran


def test_effects_never_name_a_posting_command() -> None:
    source = Path(shadow_effects.__file__).read_text()
    for verb in ('"gh"', "gh pr comment", "gh api", "create_check", "pulls/comments"):
        assert verb not in source


@pytest.mark.parametrize("var", ["GITHUB_ACTIONS", "CI"])
def test_entry_point_refuses_to_run_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    from omnimarket.nodes.node_shadow_review_effect.__main__ import main

    state = tmp_path / "state.json"
    state.write_text('{"schema": 1, "prs": {}}')
    monkeypatch.setenv(var, "true")
    rc = main(
        [
            "--watcher-state",
            str(state),
            "--store",
            str(tmp_path / "s"),
            "--dry-run",
            "--omni-home",
            str(tmp_path),
        ]
    )
    assert rc == 2
    assert not (tmp_path / "s").exists()
