# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_shadow_review_effect (OMN-20422).

A schema-1 watcher state with an in-window public PR, a private PR, a
companion and a knowledge-base-internal PR runs through the contract-declared
handler: the two in-scope PRs get records (public: A1 and A2; private: A1),
the others are skipped with their pre-registered reasons, and the contract
declares the command and terminal topics on the bus.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import yaml

from omnimarket.nodes.node_shadow_review_effect.handlers.source_watcher_state import (
    candidates_from_watcher_state,
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

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_shadow_review_effect"
)


def facts(repo: str, n: int, **kw: object) -> dict[str, object]:
    f: dict[str, object] = {
        "repo": repo,
        "number": n,
        "state": "OPEN",
        "created_at": "2026-10-09T11:00:00Z",
        "head_sha": "c" * 40,
        "head_ref": f"b{n}",
        "base": "dev",
        "title": "fix: y",
        "author": "someone",
        "author_is_bot": False,
        "draft": False,
        "evidence_companion": None,
        "body": "never carried",
    }
    f.update(kw)
    return f


def test_golden_chain_watcher_state_to_store(tmp_path: Path) -> None:
    state = {
        "schema": 1,
        "prs": {
            "omnimarket#10": {"cls": "pending", "facts": facts("omnimarket", 10)},
            "omnibase_internal#11": {
                "cls": "pending",
                "facts": facts("omnibase_internal", 11),
            },
            "onex_change_control#12": {
                "cls": "orphan-companion",
                "facts": facts("onex_change_control", 12),
            },
            "knowledge-base-internal#13": {
                "cls": "pending",
                "facts": facts("knowledge-base-internal", 13),
            },
            "omnimarket#14": {
                "cls": "pending",
                "facts": facts("omnimarket", 14, state="CLOSED"),
            },
            "omnimarket#15": {
                "cls": "green-unarmed",
                "facts": facts(
                    "omnimarket", 15, state="MERGED", created_at="2026-10-09T09:00:00Z"
                ),
            },
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    candidates = candidates_from_watcher_state(path)
    # closed-unmerged PRs are not candidates; merged ones are
    assert sorted(c.key for c in candidates) == [
        "knowledge-base-internal#13",
        "omnibase_internal#11",
        "omnimarket#10",
        "omnimarket#15",
        "onex_change_control#12",
    ]

    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    mod = importlib.import_module(contract["handler"]["module"])
    handler_cls = getattr(mod, contract["handler"]["class"])

    class Diffs:
        def diff(self, c: ModelShadowReviewCandidate) -> tuple[str | None, str]:
            return "+x = 1\n", "ok"

    class Reviewer:
        def review(
            self,
            c: ModelShadowReviewCandidate,
            diff: str,
            stratum: EnumShadowStratum,
            work_dir: Path,
        ) -> tuple[ModelShadowArmResult, ...]:
            arms = [EnumShadowArm.CODEX] + (
                [EnumShadowArm.GLM] if stratum is EnumShadowStratum.PUBLIC else []
            )
            return tuple(
                ModelShadowArmResult(arm=a, status=EnumShadowArmStatus.OK, wall_s=2.0)
                for a in arms
            )

    result = handler_cls(
        Diffs(), Reviewer(), clock=lambda: "2026-10-09T12:00:00Z"
    ).handle(
        ModelShadowReviewRequest(
            candidates=candidates,
            policy=ModelShadowReviewPolicy(window_start="2026-10-09T10:00:00Z"),
            store_root=tmp_path / "store",
        )
    )
    reasons = {s.key: s.reason for s in result.selections}
    assert reasons == {
        "omnimarket#10": "qualifying",
        "omnibase_internal#11": "qualifying",
        "onex_change_control#12": "change-control-companion",
        "knowledge-base-internal#13": "excluded-repository",
        "omnimarket#15": "created-before-window",
    }
    arms = {r.key: [a.arm.value for a in r.arms] for r in result.records}
    assert arms == {"omnimarket#10": ["A1", "A2"], "omnibase_internal#11": ["A1"]}
    assert (
        "never carried"
        not in (tmp_path / "store/records/omnimarket__10.json").read_text()
    )


def test_contract_declares_bus_topics_and_never_posts() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    rd = contract["runtime_dispatch"]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.shadow-review-tick-completed.v1"
    )
    assert rd["terminal_events"]["success"] == contract["terminal_event"]
    assert rd["command_topic"] in contract["event_bus"]["subscribe_topics"]
    assert rd["terminal_events"]["success"] in contract["event_bus"]["publish_topics"]
    assert contract["side_effects"]["posts_to_github"] is False
    assert "ci-runner" in contract["host_requirements"]["never_in"]
    assert contract["reviewer_arms"]["A2"]["strata"] == ["public"]


def test_watcher_evidence_companion_may_be_a_number(tmp_path: Path) -> None:
    state = {
        "schema": 1,
        "prs": {
            "onex_change_control#20": {
                "cls": "pending",
                "facts": facts("onex_change_control", 20, evidence_companion=13024),
            }
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    (candidate,) = candidates_from_watcher_state(path)
    assert candidate.evidence_companion == "13024"
