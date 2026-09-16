# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural proof that omnimarket's automated pushes carry the App identity (OMN-18273).

Two workflows in this repo commit and push as a bot into
``OmniNode-ai/onex_change_control``: the OCC receipt runner (the companion
publisher) and the behavior-proof backfill. Both already mint a per-run
``onexbot-occ-writer`` App token, and both already push with it — but neither
commit is attributable and neither is traceable back to the run that made it.

Every assertion here corresponds to a measured defect, not a preference:

  * ``user.email`` must carry the ``307849072+`` account-id prefix. Read live off
    App-authored commits in ``onex_change_control`` via
    ``gh api /repos/OmniNode-ai/onex_change_control/commits``, the App's real
    committer identity is
    ``307849072+onexbot-occ-writer[bot]@users.noreply.github.com``. The numeric
    prefix is what GitHub matches to link a commit to the bot account. Both
    workflows in this repo set the address WITHOUT it, so every receipt commit
    they have ever written renders as an unlinked plain-text author — the commit
    looks bot-made and is attributable to nothing.
  * the mint must FAIL CLOSED — never
    ``${{ steps.<id>.outputs.token || secrets.GITHUB_TOKEN }}``. The older
    "App-token pushes are suppressed on this org" finding was a credential
    confound: a default ``actions/checkout`` persists a basic-auth extraheader
    carrying ``GITHUB_TOKEN`` which overrides any credential in the remote URL.
    App-token pushes DO trigger push-driven CI here (probe run 35134173847 on
    omnibase_spi: actor ``onexbot-occ-writer[bot]``, event ``push``, success). A
    ``||`` fallback therefore silently downgrades the identity on exactly the
    runs where the mint failed — the runs where knowing that matters most.
  * every commit must stamp ``Onex-Workflow:`` and ``Onex-Run:`` trailers, with
    the run URL interpolated. Without them a receipt commit on a companion
    branch cannot be traced back to the run that executed the checks it claims
    to record, so the only way to audit one is to guess at run lists by
    timestamp.

REGISTRY COMPLETENESS. ``test_every_pushing_workflow_is_classified`` asserts that
the set of workflow files containing an executable ``git push`` is EXACTLY the
in-scope set plus the exempt set. A new automated pusher added later fails this
module until somebody classifies it, which is the only reason the per-job
assertions below stay meaningful.

POSITIVE CONTROL. Every scan here can return zero rows, and a zero-row scan reads
exactly like a pass. ``test_positive_control_*`` run the same extractors and the
same predicates against synthetic inputs known to produce rows and known to
violate each rule, so an extractor that silently stopped matching is reported as
a failure rather than as a clean bill of health.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The App's REAL committer identity, read live off App-authored commits via
# `gh api /repos/OmniNode-ai/onex_change_control/commits`. The `307849072+`
# prefix is the App account's numeric id and is LOAD-BEARING: without it GitHub
# does not link the commit to the bot account and it renders unlinked.
APP_COMMITTER_NAME = "onexbot-occ-writer[bot]"
APP_COMMITTER_EMAIL = "307849072+onexbot-occ-writer[bot]@users.noreply.github.com"

APP_ID_SECRET = "ONEXBOT_OCC_APP_ID"
APP_KEY_SECRET = "ONEXBOT_OCC_PRIVATE_KEY"

WORKFLOW_TRAILER = "Onex-Workflow:"
RUN_TRAILER = "Onex-Run:"

# (workflow file, job id) pairs whose pushes must carry the App identity.
IN_SCOPE: tuple[tuple[str, str], ...] = (
    ("occ-receipt-runner.yml", "occ-receipt-runner"),
    ("occ-behavior-proof-backfill.yml", "backfill"),
)

# Workflow files that push but are deliberately NOT converted, each with the
# reason stated here rather than left to be rediscovered.
EXEMPT: dict[str, str] = {
    "release-on-merge.yml": (
        "Release and main-sync path. An App-token push on a release or "
        "main-sync ref fires the production-feeding image builds downstream of "
        "a tag and of `main`, so changing the pushing identity there changes "
        "what production builds from. Its identity is separately pinned by "
        "tests/ci/test_release_on_merge_workflow.py, which requires the App "
        "token to reach the push via `actions/checkout`'s `token:` input "
        "(the only shape that survives checkout v7's persisted extraheader) "
        "rather than via a URL-embedded credential."
    ),
}


def _executable_lines(raw: str) -> str:
    """The file with every comment line removed.

    Both YAML comments and shell comments inside ``run:`` blocks start with
    ``#`` after optional whitespace, and neither class executes. This module's
    subject workflows carry long prose comments that name the very shapes being
    forbidden, so scanning raw text would report a workflow's own explanation of
    a defect as the defect.
    """
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )


def _load(workflow_file: str) -> dict[str, Any]:
    parsed = yaml.safe_load((WORKFLOWS_DIR / workflow_file).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{workflow_file} must parse as a YAML mapping"
    return parsed


def _job(workflow_file: str, job_id: str) -> dict[str, Any]:
    jobs = _load(workflow_file)["jobs"]
    assert job_id in jobs, (
        f"{workflow_file} has no job {job_id!r}; jobs are {list(jobs)}"
    )
    job = jobs[job_id]
    assert isinstance(job, dict)
    return job


def _job_shell(job: dict[str, Any]) -> str:
    """Every ``run:`` script in a job, comments stripped, concatenated."""
    return _executable_lines(
        "\n".join(str(step["run"]) for step in job.get("steps", []) if "run" in step)
    )


def _job_text(job: dict[str, Any]) -> str:
    """The whole job serialised, so ``${{ }}`` expressions in `env:`/`with:` are scanned too."""
    return json.dumps(job)


def _git_commit_invocations(shell: str) -> list[str]:
    """Every ``git commit`` command in a shell script, continuation lines folded in.

    Both subject workflows spell their commits across several backslash-joined
    lines on purpose (a continuation at column 0 would terminate the YAML block
    scalar), so a line-at-a-time scan sees only the bare ``git commit \\`` and
    would report a commit with no message and no trailers as having neither
    problem nor content.
    """
    invocations: list[str] = []
    lines = shell.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if re.match(r"^git\s+commit\b", stripped):
            parts = [stripped]
            while parts[-1].endswith("\\") and i + 1 < len(lines):
                i += 1
                parts.append(lines[i].strip())
            invocations.append(" ".join(part.rstrip("\\").strip() for part in parts))
        i += 1
    return invocations


def _trailer_values(invocation: str, trailer: str) -> list[str]:
    """Values passed as ``--trailer "<trailer> <value>"`` in one commit invocation."""
    pattern = re.compile(
        r"--trailer\s+[\"']" + re.escape(trailer) + r"\s*(.*?)[\"']",
    )
    return [match.strip() for match in pattern.findall(invocation)]


# ---------------------------------------------------------------------------
# Registry completeness — a new pusher must be classified before it can ship
# ---------------------------------------------------------------------------


def _workflow_files_that_push() -> set[str]:
    pushing: set[str] = set()
    for path in sorted(WORKFLOWS_DIR.iterdir()):
        if path.suffix not in {".yml", ".yaml"} or not path.is_file():
            continue
        if re.search(
            r"^\s*git\s+push\b",
            _executable_lines(path.read_text(encoding="utf-8")),
            re.MULTILINE,
        ):
            pushing.add(path.name)
    return pushing


def test_every_pushing_workflow_is_classified() -> None:
    """The pushing set is exactly (in-scope U exempt), so a new pusher fails here.

    Without this, the per-job assertions below silently narrow over time: adding
    a workflow that pushes as ``github-actions[bot]`` would leave every other
    test in this module green.
    """
    classified = {name for name, _ in IN_SCOPE} | set(EXEMPT)
    observed = _workflow_files_that_push()
    unclassified = observed - classified
    assert not unclassified, (
        "these workflows run `git push` but are neither in IN_SCOPE nor listed "
        f"in EXEMPT with a stated reason: {sorted(unclassified)}"
    )
    stale = classified - observed
    assert not stale, (
        f"these workflows are classified as pushers but no longer push: {sorted(stale)}"
    )


def test_every_exemption_states_a_reason() -> None:
    for name, reason in EXEMPT.items():
        assert len(reason.split()) >= 12, (
            f"the exemption for {name} must state WHY, in prose a reader can "
            f"adjudicate; got {reason!r}"
        )


# ---------------------------------------------------------------------------
# The mint: real App credentials, and no fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("workflow_file", "job_id"), IN_SCOPE)
def test_the_job_mints_a_real_app_token(workflow_file: str, job_id: str) -> None:
    job = _job(workflow_file, job_id)
    mints = [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
    ]
    assert mints, (
        f"{workflow_file}:{job_id} pushes to onex_change_control but never mints "
        "an App token via actions/create-github-app-token"
    )
    for mint in mints:
        with_block = str(mint.get("with", {}))
        assert APP_ID_SECRET in with_block, (
            f"{workflow_file}:{job_id} mint does not read secrets.{APP_ID_SECRET}"
        )
        assert APP_KEY_SECRET in with_block, (
            f"{workflow_file}:{job_id} mint does not read secrets.{APP_KEY_SECRET}"
        )


@pytest.mark.parametrize(("workflow_file", "job_id"), IN_SCOPE)
def test_the_mint_fails_closed_with_no_workflow_token_fallback(
    workflow_file: str, job_id: str
) -> None:
    """No ``outputs.token || secrets.GITHUB_TOKEN`` anywhere in the job.

    A fallback degrades the pushing identity precisely on the runs where the
    mint failed, and it does so silently: the push still succeeds, as
    ``github-actions[bot]``, and the receipt it writes is indistinguishable from
    an App-authored one except in the commit header nobody reads.
    """
    text = _job_text(_job(workflow_file, job_id))
    offenders = re.findall(r"outputs\.token\s*\|\|[^\"}]*", text)
    assert not offenders, (
        f"{workflow_file}:{job_id} falls back off the App token: {offenders}. "
        "The mint must fail closed."
    )
    fallbacks = re.findall(r"\|\|\s*secrets\.GITHUB_TOKEN", text)
    assert not fallbacks, (
        f"{workflow_file}:{job_id} carries a secrets.GITHUB_TOKEN fallback: {fallbacks}"
    )


# ---------------------------------------------------------------------------
# The identity the commit is authored as
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("workflow_file", "job_id"), IN_SCOPE)
def test_the_commit_identity_is_the_app_account(
    workflow_file: str, job_id: str
) -> None:
    shell = _job_shell(_job(workflow_file, job_id))

    names = re.findall(r"git config (?:--\S+ )*user\.name\s+[\"']([^\"']+)[\"']", shell)
    assert names, f"{workflow_file}:{job_id} never sets git config user.name"
    assert set(names) == {APP_COMMITTER_NAME}, (
        f"{workflow_file}:{job_id} commits as {sorted(set(names))}, "
        f"expected {APP_COMMITTER_NAME!r}"
    )

    emails = re.findall(
        r"git config (?:--\S+ )*user\.email\s+[\"']([^\"']+)[\"']", shell
    )
    assert emails, f"{workflow_file}:{job_id} never sets git config user.email"
    assert set(emails) == {APP_COMMITTER_EMAIL}, (
        f"{workflow_file}:{job_id} commits as {sorted(set(emails))}, expected "
        f"{APP_COMMITTER_EMAIL!r}. The `307849072+` account-id prefix is what "
        "GitHub matches to link the commit to the bot account; without it the "
        "commit renders as unlinked plain text and is attributable to nothing."
    )


# ---------------------------------------------------------------------------
# The trailers that make a commit traceable to its run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("workflow_file", "job_id"), IN_SCOPE)
def test_every_commit_stamps_both_provenance_trailers(
    workflow_file: str, job_id: str
) -> None:
    invocations = _git_commit_invocations(_job_shell(_job(workflow_file, job_id)))
    assert invocations, f"{workflow_file}:{job_id} authors no commit"

    for invocation in invocations:
        workflow_values = _trailer_values(invocation, WORKFLOW_TRAILER)
        assert len(workflow_values) == 1, (
            f"{workflow_file}:{job_id} commit does not stamp exactly one "
            f"`{WORKFLOW_TRAILER}` trailer: {invocation}"
        )
        assert "GITHUB_WORKFLOW" in workflow_values[0], (
            f"{workflow_file}:{job_id} `{WORKFLOW_TRAILER}` trailer is a literal "
            f"rather than the run's own workflow name: {workflow_values[0]!r}"
        )

        run_values = _trailer_values(invocation, RUN_TRAILER)
        assert len(run_values) == 1, (
            f"{workflow_file}:{job_id} commit does not stamp exactly one "
            f"`{RUN_TRAILER}` trailer: {invocation}"
        )
        assert "GITHUB_RUN_ID" in run_values[0], (
            f"{workflow_file}:{job_id} `{RUN_TRAILER}` trailer does not "
            f"interpolate the run id: {run_values[0]!r}"
        )
        assert "/actions/runs/" in run_values[0], (
            f"{workflow_file}:{job_id} `{RUN_TRAILER}` trailer is not a run URL: "
            f"{run_values[0]!r}"
        )


# ---------------------------------------------------------------------------
# Positive controls — a zero-row scan must not read as a pass
# ---------------------------------------------------------------------------


_SYNTHETIC_GOOD = """
git config user.name "onexbot-occ-writer[bot]"
git config user.email "307849072+onexbot-occ-writer[bot]@users.noreply.github.com"
git commit \\
  -m "evidence(OMN-18273): synthetic" \\
  --trailer "Onex-Workflow: ${GITHUB_WORKFLOW}" \\
  --trailer "Onex-Run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
"""

_SYNTHETIC_BAD = """
git config user.name "github-actions[bot]"
git config user.email "onexbot-occ-writer[bot]@users.noreply.github.com"
git commit -m "evidence(OMN-18273): synthetic with no trailers"
"""


def test_positive_control_the_commit_extractor_finds_a_folded_invocation() -> None:
    """The extractor returns rows for a known-good input.

    Without this, a regex that stopped matching would make every
    ``assert invocations`` above vacuous in the direction that reads as green.
    """
    good = _git_commit_invocations(_SYNTHETIC_GOOD)
    assert len(good) == 1
    assert "--trailer" in good[0]
    assert _trailer_values(good[0], WORKFLOW_TRAILER) == ["${GITHUB_WORKFLOW}"]
    assert _trailer_values(good[0], RUN_TRAILER) == [
        "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
    ]

    bad = _git_commit_invocations(_SYNTHETIC_BAD)
    assert len(bad) == 1
    assert _trailer_values(bad[0], WORKFLOW_TRAILER) == []
    assert _trailer_values(bad[0], RUN_TRAILER) == []


def test_positive_control_the_identity_predicate_rejects_the_unprefixed_address() -> (
    None
):
    """The defect this module exists to catch is actually caught by the predicate.

    The unprefixed address is the shape both workflows shipped with, and it is
    one character class away from the correct one — exactly the kind of near-miss
    a substring assertion would wave through.
    """
    emails = re.findall(
        r"git config (?:--\S+ )*user\.email\s+[\"']([^\"']+)[\"']", _SYNTHETIC_BAD
    )
    assert emails == ["onexbot-occ-writer[bot]@users.noreply.github.com"]
    assert set(emails) != {APP_COMMITTER_EMAIL}

    names = re.findall(
        r"git config (?:--\S+ )*user\.name\s+[\"']([^\"']+)[\"']", _SYNTHETIC_BAD
    )
    assert names == ["github-actions[bot]"]
    assert set(names) != {APP_COMMITTER_NAME}

    good_emails = re.findall(
        r"git config (?:--\S+ )*user\.email\s+[\"']([^\"']+)[\"']", _SYNTHETIC_GOOD
    )
    assert set(good_emails) == {APP_COMMITTER_EMAIL}


def test_positive_control_the_push_scan_finds_the_known_pushers() -> None:
    """The registry scan returns rows, so an empty `observed` cannot read as clean."""
    observed = _workflow_files_that_push()
    assert observed, (
        "the git-push scan of .github/workflows returned zero files. Three are "
        "known to push; an empty result means the scan broke, not that the "
        "pushers went away."
    )
    for known in ("occ-receipt-runner.yml", "release-on-merge.yml"):
        assert known in observed, f"{known} pushes but the scan missed it"


def test_positive_control_the_fallback_predicate_catches_a_fallback() -> None:
    """The fail-closed predicate matches the shape it forbids."""
    offending = json.dumps(
        {
            "env": {
                "OCC_TOKEN": "${{ steps.occ-app-token.outputs.token || secrets.GITHUB_TOKEN }}"
            }
        }
    )
    assert re.findall(r"outputs\.token\s*\|\|[^\"}]*", offending)
    assert re.findall(r"\|\|\s*secrets\.GITHUB_TOKEN", offending)
