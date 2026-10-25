# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compact helper cases adapted from test_lane_brief_compact.py."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_brief_compact_compute.handlers import (
    handler_brief_compact as compact,
)
from omnimarket.nodes.node_brief_compact_compute.models.model_brief_compact import (
    ModelBriefCompactRequest,
)

pytestmark = pytest.mark.unit

DISPATCH = (
    "Premise: every factual premise is marked `verified by: <command> => <observation>` or `UNVERIFIED — falsify "
    "first`. Re-check every UNVERIFIED premise before writing code. Open your report with `Premises falsified: "
    "<premise> — <evidence>` or `Premises falsified: none`.",
    "Report to file: write the full report to `.claude_scratch/reports/<lane>.md` and return at most 12 lines.",
    "Mint authority: never create a ticket on your own initiative. The literal line `Mint authority: delegated by the "
    "orchestrator for exactly one ticket` grants exactly one ticket.",
    "Canonical shape: new capability is a node, never a standalone module. A node is a `contract.yaml`, a declarative "
    "node and a handler with the canonical signature. Integrate before building: before you write a new file, find the "
    "existing node or handler that already does the job and wire it.",
)
RULES_HEAD = "# Standing rules for every dispatched lane (fixture of realistic size)"


def rules_fixture(size: int = 30000) -> str:
    """A rules block of realistic size: the heading, the four dispatch paragraphs, then numbered standing rules."""
    parts = [RULES_HEAD, "Fixture block. Every dispatched lane reads it.", *DISPATCH]
    n = 0
    while len("\n\n".join(parts)) < size:
        n += 1
        parts.append(
            f"Standing rule {n}: a lab lane records its step {n} in the ledger through the mutex, reads its "
            f"inbox before the step, and names the receipt of step {n} in its TERMINAL row; a lane that "
            "cannot reach the ledger stops and says so in its report."
        )
    return "\n\n".join(parts) + "\n"


CLAIM = "| lane=fixture-lane | ticket=OMN-990012 | band=b2 |"


def delegation(**kwargs: object) -> str:
    values = {
        "output_contract": "Use only supplied facts.",
        "glm_first_marker": "GLM first",
        "codex_command": "codex run",
        **kwargs,
    }
    return compact.delegation_body("OMN-990012", "fixture-lane", "b2", **values)


def render(name: str, cells: Mapping[str, str]) -> str:
    return {
        "rules": rules_fixture(),
        "landing-fixed": "Fixed landing step.\n" * 1100,
        "delegation": delegation(knowledge_base="kb_repo_example"),
        "heavy-work": compact.HEAVY_WORK,
    }[name]


def full_brief() -> str:
    return "\n\n".join(
        [
            "## Task\nFix the fixture.",
            *(render(name, {}) for name in compact.COMPACT_PARAMS),
            CLAIM,
            "## Ticket\nticket-text: OMN-990012\n" + "Fixture ticket detail.\n" * 600,
            "## Report\nReturn the findings.",
        ]
    )


def compress(brief: str, **kwargs: object) -> str:
    return (
        compact.HandlerBriefCompact()
        .handle(
            ModelBriefCompactRequest(
                operation="compact",
                brief=brief,
                render=render,
                claim=CLAIM,
                rules_head=RULES_HEAD,
                dispatch_rules=DISPATCH,
                **kwargs,
            )
        )
        .brief
    )


def test_fit_a_real_shaped_brief_fits_after_compact_and_check_cloud_tells_them_apart() -> (
    None
):
    full = full_brief()
    brief = compress(full)
    assert len(full) > 70000
    assert compact.cloud_fit_problem(len(full)) is not None
    assert compact.cloud_fit_problem(len(brief)) is None
    assert len(brief) <= compact.CLOUD_PROMPT_MAX - compact.CLOUD_HEAD_RESERVE
    assert RULES_HEAD in brief
    for paragraph in DISPATCH:
        assert paragraph in brief
    for text in ("## Task", "## Ticket", "ticket-text: OMN-990012", "## Report", CLAIM):
        assert text in brief


def test_inverse_round_trip_is_exact_and_compact_is_idempotent() -> None:
    full = full_brief()
    brief = compress(full)
    assert compact.expand_brief(brief, render) == full
    assert compress(brief) == brief
    assert compact.expand_brief(full, render) == full
    assert (
        compact.HandlerBriefCompact()
        .handle(
            ModelBriefCompactRequest(operation="expand", brief=brief, render=render)
        )
        .brief
        == full
    )


@pytest.mark.parametrize("damage", ["stale", "unpaired"])
def test_intact_a_weakened_compact_brief_is_refused_for_that_reason(
    damage: str,
) -> None:
    brief = compress(full_brief())
    if damage == "stale":
        brief = re.sub(
            r"(begin landing-fixed sha256=)(.)",
            lambda m: m[1] + ("0" if m[2] != "0" else "1"),
            brief,
        )
    else:
        brief = brief.replace("<!-- compact-ref end heavy-work -->", "")
    with pytest.raises(compact.CompactRefusedError, match=damage):
        compact.expand_brief(brief, render)


def test_rules_compact_refuses_a_rules_block_that_is_not_the_committed_one() -> None:
    edited = full_brief().replace("Standing rule 7:", "Standing rule seven:")
    with pytest.raises(
        compact.CompactRefusedError,
        match="the fresh rules rendering is not in the brief verbatim",
    ):
        compress(edited)


@pytest.mark.parametrize(
    ("brief", "error"),
    [
        (
            "<!-- compact-ref begin unknown sha256=x chars=1 -->\n<!-- compact-ref end unknown -->",
            "unknown reference",
        ),
        (
            "<!-- compact-ref begin delegation sha256=x chars=1 -->\n<!-- compact-ref end delegation -->",
            "lacks ticket, lane, band",
        ),
        ("<!-- compact-ref end rules -->", "unpaired or malformed"),
        ("<!-- compact-ref invalid -->", "unpaired or malformed"),
    ],
)
def test_malformed_references_are_always_refused(brief: str, error: str) -> None:
    with pytest.raises(compact.CompactRefusedError, match=error):
        compact.expand_brief(brief, render, verify=False)


def test_stale_reference_can_be_expanded_without_verification() -> None:
    brief = compress(full_brief())

    def changed(name, cells):
        return render(name, cells) + "changed"

    with pytest.raises(compact.CompactRefusedError, match="stale"):
        compact.expand_brief(brief, changed)
    assert "changed" in compact.expand_brief(brief, changed, verify=False)


def test_compact_cells_and_missing_claim() -> None:
    assert compact.compact_cells(CLAIM) == {
        "lane": "fixture-lane",
        "ticket": "OMN-990012",
        "band": "b2",
    }
    assert compact.compact_cells("| lane=fixture-lane | ticket=OMN-990012 |") == {
        "lane": "fixture-lane",
        "ticket": "OMN-990012",
        "band": "none",
    }
    assert compact.compact_cells("") is None
    with pytest.raises(
        compact.CompactRefusedError, match=r"needs the 3a\.8 CLAIM template"
    ):
        compact.compact_brief(
            render("delegation", {}),
            render,
            claim="",
            names=("delegation",),
            rules_head=RULES_HEAD,
            dispatch_rules=DISPATCH,
        )


def test_cloud_fit_boundary_and_prompt_preserves_the_whole_brief() -> None:
    limit = compact.CLOUD_PROMPT_MAX - compact.CLOUD_HEAD_RESERVE
    assert compact.cloud_fit_problem(limit) is None
    assert f"1 over the cloud brief limit of {limit}" in compact.cloud_fit_problem(
        limit + 1
    )
    args = {
        "lane": "fixture-lane",
        "ticket": "OMN-990012",
        "owner": "example-org",
        "repo": "example-repo",
        "landing_branch": "main",
        "branch": "feature",
        "pr_heads": (),
        "run_id": "fixture-run",
        "timeout_min": 60,
    }
    prompt = compact.cloud_prompt("Report stays whole.", **args)
    assert prompt.endswith("Report stays whole.")
    assert "open one pull request" in prompt
    assert "Stop at 45 minutes" in prompt
    head_size = len(compact.cloud_prompt("", **args))
    assert (
        len(compact.cloud_prompt("x" * (compact.CLOUD_PROMPT_MAX - head_size), **args))
        == compact.CLOUD_PROMPT_MAX
    )
    with pytest.raises(
        compact.CloudPromptTooLargeError, match="1 over the 60000-character cap"
    ):
        compact.cloud_prompt("x" * (compact.CLOUD_PROMPT_MAX - head_size + 1), **args)
    args["pr_heads"] = (
        {"pr": "example#12", "head_ref": "existing", "head_sha": "head"},
    )
    args["timeout_min"] = 1
    prompt = compact.cloud_prompt("Brief", **args)
    assert "Do not open a second pull request." in prompt
    assert "existing (head head)" in prompt
    assert "Stop at 10 minutes" in prompt


def test_delegation_uses_explicit_deployment_values_and_neutral_server_text() -> None:
    body = delegation(
        knowledge_base="kb_repo_example", registry_root_var="EXAMPLE_REGISTRY"
    )
    assert "over any repository but kb_repo_example" in body
    assert (
        "the step refuses kb_repo_example (and its hyphenated alias kb-repo-example)"
        in body
    )
    assert "$EXAMPLE_REGISTRY/" in body
    assert body.count('--out-dir "$EXAMPLE_REGISTRY/.onex_state/harness-delegate"') == 2
    assert "REGISTRY_ROOT_VALUE" not in body
    assert (
        "It defaults to the lab's default model server; pass `--base-url` or set `LAB_MODEL_URL` to run it against another lab host with headroom, and do not point it at a model server whose slot holds under 65536 tokens."
        in body
    )


def test_missing_overlay_names_the_setting() -> None:
    with pytest.raises(NodeOverlayError, match="knowledge_base"):
        delegation(environ={})


def test_overlay_resolves_repository(tmp_path: Path) -> None:
    overlay = tmp_path / "node_brief_compact_compute" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text("knowledge_base: kb_repo_example\n")
    body = delegation(environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert "over any repository but kb_repo_example" in body
    assert "over any repository but override_repo" in delegation(
        environ={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
            "ONEX_KNOWLEDGE_BASE_REPO": "override_repo",
        }
    )


def test_unknown_operation_keeps_the_source_error() -> None:
    with pytest.raises(
        ValueError, match="unknown operation 'other'; use compact or expand"
    ):
        compact.HandlerBriefCompact().handle(
            ModelBriefCompactRequest(operation="other", brief="", render=render)
        )
