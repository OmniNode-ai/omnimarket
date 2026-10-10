# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure rules of the OCC retirement S7 replay (OMN-20917).

The S7 parity bar for a repository after omnimarket (orchestrator 2026-10-10
applying the operator's rulings of the day) is a replay of its last 30 merged
PRs through both evidence paths. ``HandlerOccReplay`` gathers one
``ModelOccReplayRecord`` per merged PR; this module classifies the records with
the S5 rules and decides the replay's verdict:

* a PR whose head has no OCC run (pre-OCC, a skipped workflow) or whose OCC
  verdict could not be read is ``not_compared``: it is reported, never counted,
  and the window widens past it until ``count`` compared rows exist;
* the replay fails (exit 1) on any ``unclassified_difference`` row and any
  forbidden row (``accepted_negative_control``, ``old_behavioral_refusal``);
* a window that ran out before ``count`` compared rows passes no bar (exit 2).

``must_fail_control_line`` and its helpers port the decision the receipt
gate's "Must-fail control at the merge base" step writes to
``base-<ticket>.control.txt`` (omnibase_core receipt-gate.yml), so a replayed
control reads exactly as the live one would.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from omnimarket.nodes.node_dod_verify.models.model_occ_replay import (
    ModelOccReplayRecord,
    ModelOccReplayReport,
    ModelOccReplayRow,
    ModelOccReplaySummary,
)
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    classify,
    parse_occ_verdict,
)
from omnimarket.occ_content_probe import (
    classify_dependency_pin_only,
    classify_plugin_manifest_version_only,
    classify_workflow_core_pin_only,
    is_release_artifact_only_diff,
)

# receipt-gate.yml TEST_SIDE: the paths overlaid on the merge base.
TEST_SIDE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^tests?/|/tests?/|(^|/)conftest\.py$|(^|/)test_[^/]*\.py$|_test\.py$"
)
TICKET_PATTERN: Final[re.Pattern[str]] = re.compile(r"OMN-[0-9]+")

# receipt-gate.yml "Detect dependency-bot author": exempt unconditionally.
DEPENDENCY_BOT_AUTHORS: Final[frozenset[str]] = frozenset(
    {
        "dependabot[bot]",
        "app/dependabot",
        "dependabot",
        "renovate[bot]",
        "app/renovate",
        "renovate",
    }
)
# The OCC writer app: exempt only on a derived pin-only or release-cut diff.
OCC_WRITER_AUTHORS: Final[frozenset[str]] = frozenset(
    {"onexbot-occ-writer[bot]", "app/onexbot-occ-writer", "onexbot-occ-writer"}
)
# The receipt gate's head step looks for a missing contract in these homes.
CONTRACT_HOME_REPOSITORIES: Final[tuple[str, ...]] = (
    "omnibase_core",
    "omnibase_infra",
    "omniclaude",
    "omnimarket",
    "omnidash",
    "onex_change_control",
)
# Outcomes that fail the replay.
FAILING_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"unclassified_difference", "forbidden_difference"}
)
NEGATIVE_CONTROL_LABEL: Final[str] = "dod-negative-control"


def tickets_from_title(title: str) -> tuple[str, ...]:
    """The receipt gate's cited tickets: every OMN-<n> in the title, sorted unique."""
    return tuple(sorted(set(TICKET_PATTERN.findall(title))))


def is_test_side(path: str) -> bool:
    return TEST_SIDE_PATTERN.search(path) is not None


def is_test_only_diff(paths: Sequence[str], tickets: Sequence[str]) -> bool:
    """Every path is test-side or a cited contract, and one is test-side."""
    cited = {f"contracts/{ticket}.yaml" for ticket in tickets}
    has_tests = False
    for path in paths:
        if is_test_side(path):
            has_tests = True
        elif path not in cited:
            return False
    return has_tests


def carried_evidence_ids(head_contract: object, base_contract: object) -> set[str]:
    """dod_evidence ids whose item is unchanged from the merge base's contract."""

    def items(contract: object) -> list[object]:
        raw = contract.get("dod_evidence") if isinstance(contract, dict) else None
        return raw if isinstance(raw, list) else []

    base_items = {
        item["id"]: item
        for item in items(base_contract)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    head_ids = [
        item["id"]
        for item in items(head_contract)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    return {
        item["id"]
        for item in items(head_contract)
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and head_ids.count(item["id"]) == 1
        and item == base_items.get(item["id"])
    }


def _ids(checks: Iterable[dict[str, object]]) -> str:
    return ", ".join(
        str(check.get("evidence_id") or "<missing evidence_id>") for check in checks
    )


def must_fail_control_line(
    base_result: object,
    head_result: object,
    *,
    carried_ids: set[str],
    test_only: bool,
    at_merge_base: bool,
) -> str:
    """The first line receipt-gate.yml writes to ``base-<ticket>.control.txt``.

    ``passed: ...`` when every bound check of this PR's own fails at the
    control tree (or a test-only diff has verified head evidence for each);
    ``refused: <why>`` otherwise.
    """
    checks = base_result.get("checks") if isinstance(base_result, dict) else None
    if not isinstance(checks, list):
        return "refused: control stdout is not one JSON verdict with checks"
    bound = [
        check for check in checks if isinstance(check, dict) and check.get("binds_ac")
    ]
    if not bound:
        return "refused: zero bound checks; nothing is bound, so nothing can fail"
    own = [check for check in bound if check.get("evidence_id") not in carried_ids]
    if not own:
        return (
            "refused: every bound check is carried unchanged from the merge "
            "base's contract; this pull request binds no evidence of its own"
        )
    verified = [check for check in own if check.get("status") == "verified"]
    other = [
        check for check in own if check.get("status") not in {"failed", "verified"}
    ]
    if test_only and at_merge_base and not other:
        head_checks = (
            head_result.get("checks")
            if isinstance(head_result, dict) and head_result.get("status") == "verified"
            else None
        )
        if isinstance(head_checks, list) and all(
            isinstance(check.get("evidence_id"), str)
            and check.get("evidence_id")
            and any(
                isinstance(head_check, dict)
                and head_check.get("evidence_id") == check.get("evidence_id")
                and head_check.get("binds_ac")
                and head_check.get("status") == "verified"
                for head_check in head_checks
            )
            for check in own
        ):
            return (
                "passed: IMPOSSIBLE TEST_ONLY_DIFF (test_only_diff); coverage-only "
                "evidence, no earlier product behaviour to control"
            )
        return (
            "refused: TEST_ONLY_DIFF requires verified head evidence for every "
            "own bound check"
        )
    if verified:
        return (
            f"refused: [{_ids(verified)}] bound test also passes at the control: "
            "the control did not fail (always-pass)"
        )
    if other:
        return f"refused: [{_ids(other)}] the control could not run"
    return "passed: every bound check failed"


def _classify_release_cut(
    paths: Sequence[str],
    *,
    pyproject_head: str | None,
    pyproject_base: str | None,
    changelog_head: str | None,
    changelog_base: str | None,
) -> tuple[bool, str]:
    """receipt-gate.yml's writer-app release-cut classifier, ported."""
    if list(paths).count("CHANGELOG.md") != 1:
        return False, "release cut requires root CHANGELOG.md"
    if not is_release_artifact_only_diff(paths):
        return False, "release cut is not a release-artifact-only diff"
    if any(path not in {"CHANGELOG.md", "pyproject.toml", "uv.lock"} for path in paths):
        return False, "release cut contains a path outside the root release allowlist"
    manifests = [path for path in paths if path in {"pyproject.toml", "uv.lock"}]
    if manifests:
        pin_only, reason = classify_dependency_pin_only(
            manifests, pyproject_head=pyproject_head, pyproject_base=pyproject_base
        )
        if not pin_only:
            return False, f"release cut manifests are not dependency-pin-only: {reason}"
    if changelog_head is None:
        return False, "release cut CHANGELOG.md is unreadable at the head"
    base = (changelog_base or "").splitlines(keepends=True)
    head = changelog_head.splitlines(keepends=True)
    inserted: list[str] = []
    for tag, _, _, head_start, head_end in difflib.SequenceMatcher(
        None, base, head, autojunk=False
    ).get_opcodes():
        if tag not in {"equal", "insert"}:
            return False, "release cut rewrites or deletes an existing changelog line"
        if tag == "insert":
            inserted.extend(head[head_start:head_end])
    headings = [line.rstrip("\r\n") for line in inserted if line.startswith("## ")]
    if len(headings) != 1:
        return False, "release cut requires exactly one inserted ## heading"
    heading = re.fullmatch(r"## v(\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)", headings[0])
    if heading is None:
        return False, "release cut heading must match ## v<version> (<date>)"
    first = next((line.rstrip("\r\n") for line in inserted if line.strip()), None)
    if first != headings[0]:
        return False, "release cut heading is not the first non-blank inserted line"
    if pyproject_head is None:
        return False, "release cut root pyproject.toml is unreadable at the head"
    try:
        project = tomllib.loads(pyproject_head).get("project", {})
        version = project.get("version") if isinstance(project, dict) else None
    except tomllib.TOMLDecodeError:
        return False, "release cut root pyproject.toml does not parse at the head"
    if version != heading.group(1):
        return False, "release cut heading version does not match head project.version"
    if any(line.startswith(f"## v{version} ") for line in base):
        return False, "release cut version already has a heading at the merge base"
    return True, f"release cut adds changelog version {version}"


def classify_writer_app_exemption(
    paths: Sequence[str],
    *,
    contents: Mapping[str, tuple[str | None, str | None]],
) -> tuple[bool, str]:
    """receipt-gate.yml's "Derive the OCC writer app's dependency-pin-only or
    release-cut verdict at the head": exempt only on a derived verdict.

    ``contents`` maps a changed path to ``(head, base)`` content; it must carry
    every changed path, and CHANGELOG.md and pyproject.toml when present.
    """
    manifests = [path for path in paths if path.rsplit("/", 1)[-1] == "pyproject.toml"]
    if len(manifests) > 1:
        return False, "more than one dependency manifest changed"
    pyproject_head, pyproject_base = (
        contents.get(manifests[0], (None, None)) if manifests else (None, None)
    )
    pin_only, pin_reason = classify_dependency_pin_only(
        paths, pyproject_head=pyproject_head, pyproject_base=pyproject_base
    )
    if pin_only:
        return True, f"dependency_pin_only: {pin_reason}"
    if paths and all(
        path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
        for path in paths
    ):
        workflow_pin, workflow_reason = classify_workflow_core_pin_only(
            paths, contents=contents
        )
        if workflow_pin:
            return True, f"dependency_pin_only: {workflow_reason}"
    if paths and all(
        path.rsplit("/", 1)[-1] in {"plugin.json", "marketplace.json"}
        and path.split("/")[-2:-1] == [".claude-plugin"]
        for path in paths
    ):
        plugin_bump, plugin_reason = classify_plugin_manifest_version_only(
            paths, contents=contents
        )
        if plugin_bump:
            return True, f"plugin_version_bump: {plugin_reason}"
    root_head, root_base = contents.get("pyproject.toml", (None, None))
    changelog_head, changelog_base = contents.get("CHANGELOG.md", (None, None))
    release_cut, reason = _classify_release_cut(
        paths,
        pyproject_head=root_head,
        pyproject_base=root_base,
        changelog_head=changelog_head,
        changelog_base=changelog_base,
    )
    if release_cut:
        return True, f"release_cut: {reason}"
    return False, f"{reason}; dependency-pin-only: {pin_reason}"


def _row(record: ModelOccReplayRecord) -> ModelOccReplayRow:
    old = parse_occ_verdict(record.occ_check_run)
    new = record.new_verdict
    if old.admitted is None or new is None:
        # No OCC verdict on this head, or the new path could not be replayed:
        # reported, never counted.
        return ModelOccReplayRow(
            pr=record.pr,
            head_sha=record.head_sha,
            merged_at=record.merged_at,
            tickets=record.tickets,
            occ_admitted=old.admitted,
            occ_conclusion=old.conclusion,
            occ_reason=old.reason,
            new_admitted=new.admitted if new is not None else None,
            new_reason=new.reason if new is not None else None,
            outcome="not_compared",
            reason_code=None,
            passed=False,
            note=record.note,
        )
    result = classify(old, new, negative_control=record.negative_control)
    return ModelOccReplayRow(
        pr=record.pr,
        head_sha=record.head_sha,
        merged_at=record.merged_at,
        tickets=record.tickets,
        occ_admitted=result.old_admitted,
        occ_conclusion=old.conclusion,
        occ_reason=result.old_reason,
        new_admitted=result.new_admitted,
        new_reason=result.new_reason,
        outcome=result.outcome,
        reason_code=result.reason_code,
        passed=result.passed,
        note=record.note,
    )


def replay_records(
    records: Iterable[ModelOccReplayRecord], *, repository: str, count: int
) -> ModelOccReplayReport:
    """Classify records newest first until ``count`` of them are compared."""
    rows: list[ModelOccReplayRow] = []
    compared = 0
    for record in records:
        if compared >= count:
            break
        row = _row(record)
        rows.append(row)
        if row.outcome != "not_compared":
            compared += 1
    compared_rows = [row for row in rows if row.outcome != "not_compared"]
    by_outcome = Counter(row.outcome for row in compared_rows)
    by_reason = Counter(
        row.reason_code.value
        if row.reason_code is not None
        else ("agree" if row.outcome == "agree" else row.outcome)
        for row in compared_rows
    )
    unclassified = by_outcome.get("unclassified_difference", 0)
    forbidden = by_outcome.get("forbidden_difference", 0)
    summary = ModelOccReplaySummary(
        repository=repository,
        target=count,
        examined=len(rows),
        compared=compared,
        not_compared=len(rows) - compared,
        target_met=compared >= count,
        unclassified=unclassified,
        forbidden=forbidden,
        passed=unclassified == 0 and forbidden == 0,
        by_outcome=dict(sorted(by_outcome.items())),
        by_reason_code=dict(sorted(by_reason.items())),
        window_newest_pr=rows[0].pr if rows else None,
        window_oldest_pr=rows[-1].pr if rows else None,
        window_oldest_merged_at=rows[-1].merged_at if rows else "",
    )
    return ModelOccReplayReport(rows=tuple(rows), summary=summary)


def replay_exit_code(report: ModelOccReplayReport) -> int:
    """1 on a failing row, 2 when the window held fewer compared rows than asked."""
    if not report.summary.passed:
        return 1
    if not report.summary.target_met:
        return 2
    return 0


def _verdict_cell(admitted: bool | None, conclusion: str | None = None) -> str:
    if admitted is None:
        return f"none ({conclusion})" if conclusion else "none"
    return "admitted" if admitted else "refused"


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_replay_table(report: ModelOccReplayReport) -> str:
    """The per-PR table, one markdown row per examined PR."""
    lines = [
        "| pr | head | ticket | OCC verdict | OCC reason | new-path verdict "
        "| new-path reason | outcome | reason code | note |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report.rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.pr),
                    row.head_sha[:12],
                    _cell(", ".join(row.tickets)),
                    _verdict_cell(row.occ_admitted, row.occ_conclusion),
                    _cell(row.occ_reason),
                    _verdict_cell(row.new_admitted),
                    _cell(row.new_reason),
                    row.outcome,
                    row.reason_code.value if row.reason_code is not None else "",
                    _cell(row.note),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "CONTRACT_HOME_REPOSITORIES",
    "DEPENDENCY_BOT_AUTHORS",
    "NEGATIVE_CONTROL_LABEL",
    "OCC_WRITER_AUTHORS",
    "carried_evidence_ids",
    "classify_writer_app_exemption",
    "is_test_only_diff",
    "is_test_side",
    "must_fail_control_line",
    "render_replay_table",
    "replay_exit_code",
    "replay_records",
    "tickets_from_title",
]
