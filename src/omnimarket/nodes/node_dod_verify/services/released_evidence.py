# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Released-is-Done evidence: a merge is not shipped until it is released.

OMN-18010 deliverable 2. Until this module existed, "Done" was measured at
**merge**. A release was a manual, ticket-driven tag push with no merge
trigger, and nothing audited dev-vs-tag distance — so merged work sat
unreleased indefinitely and closed anyway. The measured precedent the operator
named on 2026-09-06: ``omnimarket#2304`` merged 2026-09-05 and sat unreleased
with its own release ticket in Backlog; ``#2334`` likewise; PRD scoring found
five landed-not-deployed items.

What this module decides
------------------------
Given the PR/merge-commit citations a ticket's durable evidence already
produces, for every citation that lands in a **publishing** repository:

1. the merge sha must be contained in a release tag (``git tag --contains``
   semantics, i.e. the sha is an ancestor of — or is — the tagged commit), and
2. the package index must actually serve that tag's version, with **both** a
   wheel and an sdist.

Both legs are required. A tag with no published distribution is a tag, not a
release: the consumer's ``pip install`` still cannot reach the change. This is
the same both-legs shape the OMN-18010 probe specifies.

Fail-closed, in both directions
-------------------------------
A probe that cannot answer returns ``None``, and ``None`` is
:attr:`EnumReleasedOutcome.INDETERMINATE` — never RELEASED. A clone that has no
tags fetched, an unreachable index, a missing ``git``, a network timeout: all
of them are refusals to certify, not certifications. This matters more here
than in most checks, because the failure mode being closed is precisely
"looked green, shipped nothing".

Purity
------
Everything in this module except the two ``*_probe`` production wirings at the
bottom is pure logic over injected :class:`typing.Protocol` probes, so the unit
tests run with recorded fixtures and no network at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# The publishing-repo registry
# --------------------------------------------------------------------------- #

# ``<owner>/<repo>`` -> the PEP 503 normalized distribution name that repo
# publishes to the package index. Derived from each repo's ``release.yml``
# publish step and its ``pyproject.toml`` ``[project].name`` — a repo is on
# this map if and only if a release of it produces something a consumer can
# install.
#
# Deliberately NOT a wildcard over the org: the whole point of the check is
# that a merge into a repo on this map is not shipped until the index serves
# it, and a repo that publishes nothing can never satisfy that. Repos with no
# ``release.yml`` publish step (``onex_change_control``, ``omnidash``,
# ``omniweb``, ``omni_home``, ``knowledge-base``) are NOT_APPLICABLE, which is
# a pass — see :func:`is_publishing_repo`.
PUBLISHING_REPO_DISTRIBUTIONS: Final[dict[str, str]] = {
    "OmniNode-ai/omnibase_core": "omnibase-core",
    "OmniNode-ai/omnibase_infra": "omnibase-infra",
    "OmniNode-ai/omnibase_spi": "omnibase-spi",
    "OmniNode-ai/omnibase_compat": "omnibase-compat",
    "OmniNode-ai/omnimarket": "omnimarket",
    "OmniNode-ai/omnimemory": "omninode-memory",
    "OmniNode-ai/omniintelligence": "omninode-intelligence",
    "OmniNode-ai/omniclaude": "omninode-claude",
}

# The distribution files a version must serve to count as released. A wheel
# alone leaves source-installing consumers unable to reach the change; an sdist
# alone leaves every ordinary ``pip install`` building from source.
REQUIRED_INDEX_PACKAGE_TYPES: Final[frozenset[str]] = frozenset(
    {"bdist_wheel", "sdist"}
)

# Release tags in every publishing repo are ``vX.Y.Z``. Anything else (a
# ``-rc1`` prerelease, a ``lane-*`` marker, a bare ``X.Y.Z``) is not a release
# tag for this check's purpose and is ignored rather than guessed at.
_RELEASE_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^v(\d+\.\d+\.\d+)$")

# Git short SHA is 7 hex chars; full is 40.
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{7,40}$")

# OMN-18010, stated rather than assumed: there is NO ``customer-facing`` label,
# type, or field anywhere in the dod_verify probe registry or in
# ``ModelTicketContract`` — grep over ``src/`` and ``tests/`` finds the phrase
# only in unrelated routing prose. So the trigger for this check is NOT a
# label: it is **any evidence PR in a publishing repo**. That is deliberately
# broader than "customer-facing" and cannot be gamed by omitting a label. If a
# real label is introduced later it belongs here, as a widening of the trigger,
# never as a narrowing.
CUSTOMER_FACING_LABEL: Final[str] = "customer-facing"


def is_publishing_repo(repo: str) -> bool:
    """True when ``<owner>/<repo>`` publishes to a package index. Pure."""
    return repo in PUBLISHING_REPO_DISTRIBUTIONS


def distribution_for_repo(repo: str) -> str | None:
    """Return the distribution ``repo`` publishes, or ``None``. Pure."""
    return PUBLISHING_REPO_DISTRIBUTIONS.get(repo)


def version_from_release_tag(tag: str) -> str | None:
    """Return the version a ``vX.Y.Z`` release tag names, else ``None``. Pure."""
    match = _RELEASE_TAG_RE.match(tag.strip())
    if match is None:
        return None
    return match.group(1)


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


class EnumReleasedOutcome(StrEnum):
    """What the released check concluded about one citation, or a whole set."""

    # No citation lands in a publishing repo. The check does not apply and
    # passes: a change to a repo that publishes nothing cannot be unreleased.
    NOT_APPLICABLE = "not_applicable"

    # Every publishing-repo merge sha is contained in a release tag whose
    # version the index serves with both a wheel and an sdist.
    RELEASED = "released"

    # The distinct, actionable non-closing state this whole module exists to
    # name: the PR is genuinely MERGED, and no release tag contains it. The
    # work landed and shipped nowhere.
    MERGED_UNRELEASED = "merged_unreleased"

    # A release tag contains the sha, but the index does not serve that
    # version with both required distribution files. A tag is not a release.
    RELEASED_TAG_NOT_ON_INDEX = "released_tag_not_on_index"

    # A probe could not answer — no tags fetched, index unreachable, git
    # missing, timeout. Fail-closed: never certified as released.
    INDETERMINATE = "indeterminate"


# Worst-first. The aggregate outcome of a citation set is the first member of
# this tuple that any citation reported, so the headline a closer reports is
# the actionable one (MERGED_UNRELEASED) rather than an incidental probe
# failure that happened to be evaluated first.
RELEASED_OUTCOME_PRECEDENCE: Final[tuple[EnumReleasedOutcome, ...]] = (
    EnumReleasedOutcome.MERGED_UNRELEASED,
    EnumReleasedOutcome.RELEASED_TAG_NOT_ON_INDEX,
    EnumReleasedOutcome.INDETERMINATE,
    EnumReleasedOutcome.RELEASED,
    EnumReleasedOutcome.NOT_APPLICABLE,
)

# The only two outcomes that permit closure.
CLOSING_RELEASED_OUTCOMES: Final[frozenset[EnumReleasedOutcome]] = frozenset(
    {EnumReleasedOutcome.RELEASED, EnumReleasedOutcome.NOT_APPLICABLE}
)


def is_closing_outcome(outcome: EnumReleasedOutcome) -> bool:
    """True when ``outcome`` permits a Done transition. Pure."""
    return outcome in CLOSING_RELEASED_OUTCOMES


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


class ReleaseTagsContainingProbe(Protocol):
    """Probe: which release tags of ``repo`` contain ``commit_sha``?

    Returns the tag names (e.g. ``("v0.47.4", "v0.47.5")``) that contain the
    commit — ``git tag --list 'v*' --contains <sha>`` semantics, so a sha that
    IS the tagged commit counts as contained. An EMPTY tuple is a definite
    "no release tag contains this commit" (merged-unreleased). ``None`` is
    INDETERMINATE: the clone was missing, its tags were not fetched, the sha
    was unknown to it, or git failed. The two are never conflated — that
    conflation is exactly how an unfetched clone would certify a release.
    """

    def __call__(self, repo: str, commit_sha: str) -> tuple[str, ...] | None: ...


class IndexReleaseFilesProbe(Protocol):
    """Probe: which distribution file types does the index serve for a version?

    Returns e.g. ``frozenset({"bdist_wheel", "sdist"})``. An EMPTY frozenset is
    a definite "the index does not serve this version" (a 404). ``None`` is
    INDETERMINATE — the index was unreachable or answered unparseably.
    """

    def __call__(self, distribution: str, version: str) -> frozenset[str] | None: ...


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #


class ModelReleasedCitation(BaseModel):
    """One PR/merge-sha citation, and what the released check made of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1, description="GitHub <owner>/<repo>.")
    pr_number: int | None = Field(
        default=None, description="PR number, when the citation carries one."
    )
    merge_sha: str = Field(
        ..., min_length=7, description="The merge sha whose release state was probed."
    )
    distribution: str | None = Field(
        default=None,
        description="Distribution the repo publishes; None for a non-publishing repo.",
    )
    containing_tags: tuple[str, ...] = Field(
        default=(), description="Release tags found to contain merge_sha."
    )
    released_version: str | None = Field(
        default=None,
        description="The version proven served by the index, when one was.",
    )
    outcome: EnumReleasedOutcome = Field(...)
    detail: str = Field(..., min_length=1, description="Human-readable evidence line.")


class ModelReleasedEvidenceResult(BaseModel):
    """Aggregate verdict over a citation set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumReleasedOutcome = Field(...)
    citations: tuple[ModelReleasedCitation, ...] = Field(default=())
    message: str = Field(..., min_length=1)

    @property
    def passed(self) -> bool:
        """True when the verdict permits closure."""
        return is_closing_outcome(self.outcome)


class ModelReleasedCitationInput(BaseModel):
    """A citation handed to :func:`evaluate_released`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    merge_sha: str = Field(..., min_length=7)
    pr_number: int | None = Field(default=None)


# --------------------------------------------------------------------------- #
# The evaluator
# --------------------------------------------------------------------------- #


def evaluate_citation(
    citation: ModelReleasedCitationInput,
    *,
    release_tags_containing: ReleaseTagsContainingProbe,
    index_release_files: IndexReleaseFilesProbe,
) -> ModelReleasedCitation:
    """Decide the released state of ONE citation. Pure over its probes."""
    repo = citation.repo
    sha = citation.merge_sha
    distribution = distribution_for_repo(repo)

    if distribution is None:
        return ModelReleasedCitation(
            repo=repo,
            pr_number=citation.pr_number,
            merge_sha=sha,
            outcome=EnumReleasedOutcome.NOT_APPLICABLE,
            detail=(
                f"{repo} publishes no distribution — a merge there cannot be "
                "unreleased, so the released check does not apply."
            ),
        )

    if not _SHA_RE.match(sha):
        return ModelReleasedCitation(
            repo=repo,
            pr_number=citation.pr_number,
            merge_sha=sha,
            distribution=distribution,
            outcome=EnumReleasedOutcome.INDETERMINATE,
            detail=(
                f"{repo}: cited merge sha {sha!r} is not a 7-40 char hex sha, so "
                "no containing-tag lookup can be performed (fail-closed)."
            ),
        )

    tags = release_tags_containing(repo, sha)
    if tags is None:
        return ModelReleasedCitation(
            repo=repo,
            pr_number=citation.pr_number,
            merge_sha=sha,
            distribution=distribution,
            outcome=EnumReleasedOutcome.INDETERMINATE,
            detail=(
                f"{repo}: the containing-tag lookup for {sha} could not be "
                "resolved (clone missing, tags not fetched, sha unknown to the "
                "clone, or git failed). Fail-closed — this is NOT evidence that "
                "the commit is released."
            ),
        )

    release_tags = tuple(t for t in tags if version_from_release_tag(t) is not None)
    if not release_tags:
        rejected = (
            f" ({len(tags)} non-release tag(s) ignored: {sorted(tags)})" if tags else ""
        )
        return ModelReleasedCitation(
            repo=repo,
            pr_number=citation.pr_number,
            merge_sha=sha,
            distribution=distribution,
            containing_tags=tuple(tags),
            outcome=EnumReleasedOutcome.MERGED_UNRELEASED,
            detail=(
                f"{repo}: merge {sha} is contained in NO vX.Y.Z release tag"
                f"{rejected}. The change landed and shipped nowhere — cut a "
                f"release carrying it before closing."
            ),
        )

    # Any one containing release tag whose version the index actually serves is
    # enough: the change is reachable by a consumer.
    indeterminate_detail: str | None = None
    unserved: list[str] = []
    for tag in sorted(release_tags):
        version = version_from_release_tag(tag)
        if version is None:  # unreachable: filtered above, kept for mypy
            continue
        served = index_release_files(distribution, version)
        if served is None:
            indeterminate_detail = (
                f"{repo}: {tag} contains {sha}, but the index read for "
                f"{distribution} {version} could not be resolved. Fail-closed — "
                "an unreachable index is not proof of publication."
            )
            continue
        missing = REQUIRED_INDEX_PACKAGE_TYPES - served
        if not missing:
            return ModelReleasedCitation(
                repo=repo,
                pr_number=citation.pr_number,
                merge_sha=sha,
                distribution=distribution,
                containing_tags=release_tags,
                released_version=version,
                outcome=EnumReleasedOutcome.RELEASED,
                detail=(
                    f"{repo}: merge {sha} is contained in {tag}, and the index "
                    f"serves {distribution} {version} with "
                    f"{sorted(REQUIRED_INDEX_PACKAGE_TYPES)}."
                ),
            )
        unserved.append(
            f"{tag} -> {distribution} {version} served {sorted(served)}, "
            f"missing {sorted(missing)}"
        )

    if indeterminate_detail is not None and not unserved:
        return ModelReleasedCitation(
            repo=repo,
            pr_number=citation.pr_number,
            merge_sha=sha,
            distribution=distribution,
            containing_tags=release_tags,
            outcome=EnumReleasedOutcome.INDETERMINATE,
            detail=indeterminate_detail,
        )

    suffix = (
        f" Also indeterminate: {indeterminate_detail}" if indeterminate_detail else ""
    )
    return ModelReleasedCitation(
        repo=repo,
        pr_number=citation.pr_number,
        merge_sha=sha,
        distribution=distribution,
        containing_tags=release_tags,
        outcome=EnumReleasedOutcome.RELEASED_TAG_NOT_ON_INDEX,
        detail=(
            f"{repo}: merge {sha} is contained in {sorted(release_tags)}, but no "
            f"containing tag's version is served by the index with both a wheel "
            f"and an sdist — {'; '.join(unserved)}. A tag is not a release."
            f"{suffix}"
        ),
    )


def aggregate_outcome(
    citations: tuple[ModelReleasedCitation, ...],
) -> EnumReleasedOutcome:
    """Roll a citation set up to one outcome, worst-first. Pure."""
    if not citations:
        return EnumReleasedOutcome.NOT_APPLICABLE
    present = {c.outcome for c in citations}
    for candidate in RELEASED_OUTCOME_PRECEDENCE:
        if candidate in present:
            return candidate
    return EnumReleasedOutcome.INDETERMINATE


def evaluate_released(
    citations: tuple[ModelReleasedCitationInput, ...],
    *,
    release_tags_containing: ReleaseTagsContainingProbe,
    index_release_files: IndexReleaseFilesProbe,
) -> ModelReleasedEvidenceResult:
    """Decide whether every publishing-repo citation is actually released.

    Pure over its two probes — the unit suite injects recorded fixtures and
    performs no I/O of any kind.
    """
    if not citations:
        return ModelReleasedEvidenceResult(
            outcome=EnumReleasedOutcome.NOT_APPLICABLE,
            citations=(),
            message=(
                "No merged-PR citation was supplied, so no publishing repo is "
                "implicated and the released check does not apply."
            ),
        )

    evaluated = tuple(
        evaluate_citation(
            citation,
            release_tags_containing=release_tags_containing,
            index_release_files=index_release_files,
        )
        for citation in citations
    )
    outcome = aggregate_outcome(evaluated)
    blocking = [c for c in evaluated if not is_closing_outcome(c.outcome)]

    if not blocking:
        publishing = [c for c in evaluated if c.outcome is EnumReleasedOutcome.RELEASED]
        if not publishing:
            message = (
                f"None of the {len(evaluated)} cited PR(s) lands in a publishing "
                "repo, so the released check does not apply."
            )
        else:
            message = (
                f"All {len(publishing)} publishing-repo merge(s) are contained in "
                "a release tag whose version the index serves: "
                + "; ".join(c.detail for c in publishing)
            )
        return ModelReleasedEvidenceResult(
            outcome=outcome, citations=evaluated, message=message
        )

    return ModelReleasedEvidenceResult(
        outcome=outcome,
        citations=evaluated,
        message=(
            f"{outcome.value.upper()}: {len(blocking)} of {len(evaluated)} cited "
            "merge(s) are not proven released — "
            + "; ".join(c.detail for c in blocking)
        ),
    )


# --------------------------------------------------------------------------- #
# The declared-check surface
# --------------------------------------------------------------------------- #

# ``check_value`` for a ``check_type: released`` evidence check: one or more
# ``<owner>/<repo>@<merge-sha>`` citations, separated by commas or whitespace.
# The MERGE sha, not the PR head sha: these repos are squash-merge-only, so a
# head sha has no ancestry to any tag and would read as unreleased forever.
_RELEASED_CITATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-fA-F]{7,40})$"
)


def parse_released_check_value(
    check_value: str,
) -> tuple[tuple[ModelReleasedCitationInput, ...], str | None]:
    """Parse a ``check_type: released`` ``check_value``. Pure.

    Returns ``(citations, None)`` on success, or ``((), error)`` on any
    malformed token. Fail-closed by construction: a partially parseable value
    is an error, never a shorter citation list — silently dropping an
    unparseable citation would drop exactly the merge nobody released.
    """
    tokens = [t for t in re.split(r"[,\s]+", check_value.strip()) if t]
    if not tokens:
        return (), (
            "Empty check_value for check_type 'released'. Expected one or more "
            "'<owner>/<repo>@<merge-sha>' citations."
        )
    citations: list[ModelReleasedCitationInput] = []
    for token in tokens:
        match = _RELEASED_CITATION_RE.match(token)
        if match is None:
            return (), (
                f"Malformed released citation {token!r}. Expected "
                "'<owner>/<repo>@<merge-sha>' with a 7-40 char hex sha "
                "(the SQUASH MERGE commit, not the PR head sha)."
            )
        citations.append(
            ModelReleasedCitationInput(
                repo=match.group("repo"), merge_sha=match.group("sha")
            )
        )
    return tuple(citations), None


# --------------------------------------------------------------------------- #
# Production probe wirings (the only I/O in this module)
# --------------------------------------------------------------------------- #

_DEFAULT_GIT_TIMEOUT_S: Final[float] = 120.0
_DEFAULT_INDEX_TIMEOUT_S: Final[float] = 20.0

# The package index this org publishes to. Read from one place so a test can
# assert the URL shape without a network call.
PACKAGE_INDEX_JSON_URL: Final[str] = (
    # url-authority-ok: the public PyPI JSON API is a GOVERNANCE-plane read for
    # this verification probe, exactly as api.github.com is in
    # node_prod_promotion_grant_resolver_effect. It carries no model routing
    # authority, it is never a runtime dependency of any node, and it is the
    # index these repos' own release.yml publishes to (`uv publish --check-url
    # https://pypi.org/simple/`). Resolving it from a routing contract would
    # make the released check depend on the very control plane whose contents
    # it is auditing.
    "https://pypi.org/pypi/{distribution}/{version}/json"  # url-authority-ok: governance-plane index read, no routing authority
)


def git_release_tags_containing(
    clone_root: Path,
    commit_sha: str,
    *,
    timeout_s: float = _DEFAULT_GIT_TIMEOUT_S,
) -> tuple[str, ...] | None:
    """``git tag --list 'v*' --contains <sha>`` against a staged clone.

    Returns the containing tag names, ``()`` when the sha is known to the clone
    but no tag contains it, and ``None`` when the lookup could not be resolved
    at all — a missing clone, an unknown sha (tags or objects not fetched), a
    git failure or a timeout. The caller treats ``None`` as INDETERMINATE.

    The distinction is load-bearing: a clone whose tags were never fetched
    would otherwise report "no containing tag" and be read as
    merged-unreleased, converting a probe failure into a finding.
    """
    if not clone_root.is_dir():
        return None
    try:
        # Prove the object exists in this clone first. Without this an unknown
        # sha makes ``git tag --contains`` fail, and a caller that only read
        # stdout would see an empty list.
        known = subprocess.run(
            [
                "git",
                "-C",
                str(clone_root),
                "cat-file",
                "-e",
                f"{commit_sha}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        if known.returncode != 0:
            return None
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(clone_root),
                "tag",
                "--list",
                "v*",
                "--contains",
                commit_sha,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())


def pypi_release_files(
    distribution: str,
    version: str,
    *,
    timeout_s: float = _DEFAULT_INDEX_TIMEOUT_S,
) -> frozenset[str] | None:
    """Read the distribution file types the index serves for one version.

    ``frozenset()`` on a definite 404 (the index does not have that version);
    ``None`` on any other failure, which is INDETERMINATE and never certifies.
    """
    url = PACKAGE_INDEX_JSON_URL.format(distribution=distribution, version=version)
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return frozenset() if exc.code == 404 else None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return None
    types = {
        entry["packagetype"]
        for entry in urls
        if isinstance(entry, dict) and isinstance(entry.get("packagetype"), str)
    }
    return frozenset(types)


__all__: list[str] = [
    "CLOSING_RELEASED_OUTCOMES",
    "CUSTOMER_FACING_LABEL",
    "PACKAGE_INDEX_JSON_URL",
    "PUBLISHING_REPO_DISTRIBUTIONS",
    "RELEASED_OUTCOME_PRECEDENCE",
    "REQUIRED_INDEX_PACKAGE_TYPES",
    "EnumReleasedOutcome",
    "IndexReleaseFilesProbe",
    "ModelReleasedCitation",
    "ModelReleasedCitationInput",
    "ModelReleasedEvidenceResult",
    "ReleaseTagsContainingProbe",
    "aggregate_outcome",
    "distribution_for_repo",
    "evaluate_citation",
    "evaluate_released",
    "git_release_tags_containing",
    "is_closing_outcome",
    "is_publishing_repo",
    "parse_released_check_value",
    "pypi_release_files",
    "version_from_release_tag",
]
