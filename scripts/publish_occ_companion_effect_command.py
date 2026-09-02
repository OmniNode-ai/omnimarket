#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# publish_occ_companion_effect_command.py
#
# Thin-publishes onex.cmd.omnimarket.occ-companion-effect-requested.v1 when a
# product PR is opened or synchronized (OMN-14941 born path). The command is
# consumed by node_occ_companion_effect (the canonical RSD-3 write-EFFECT),
# which runs the deterministic read -> compute -> write OCC-companion producer
# cycle and stamps Evidence-Source: OCC#<n> back onto the product PR. Only this
# path mints occ:machine-minted + minted_by_node=true.
#
# Payload (ModelOccCompanionEffectRequest-shaped so payload_type_match routes
# it — the OMN-13990 lesson): {repo, pr_number, mode, correlation_id} and
# NOTHING else. The model is frozen + extra='forbid'; any stray key (the legacy
# occ-autobind block_reason/ticket_id/requested_at/pr_head_sha/event_id/topic
# shape) fails validation and the command is silently DLQ'd — the seam test
# (tests/unit/nodes/node_occ_companion_effect/
# test_publish_occ_companion_effect_command.py) round-trips this publisher's
# emitted JSON through ModelOccCompanionEffectRequest.model_validate to pin it.
#
# mode MUST be "mutate": ModelOccCompanionEffectRequest defaults mode to
# dry_run (fail-safe). An omitted mode would make every published command a
# silent read+compute no-mint — the exact optional-input-silent-skip trap
# (memory feedback_optional_input_means_the_check_does_not_exist). This
# publisher IS the live trigger, so it sets mode="mutate" explicitly, always.
#
# occ_repo / runner / verifier are intentionally OMITTED so the model defaults
# apply (runner=node_occ_companion_compute != verifier=occ-evidence-source-
# autobind, OMN-12791).
#
# PUBLISHER-SIDE IDEMPOTENCY (OMN-14941; line-anchored per OMN-16710;
# ARTIFACT-RESOLVED per OMN-15615): when the product PR body already carries a
# real Evidence-Source stamp AND that stamp RESOLVES to a live OCC companion
# for THIS product PR, the companion is already bound — publishing would only
# burn a lease + a full read/compute cycle to reach the handler's own
# already-bound no-op. The publisher skips LOUDLY (exit 0) instead.
#
# The decision is a three-step pipeline, not a text test (OMN-15615):
#
#   1. strip non-canonical regions (fenced code blocks, blockquotes) — a
#      stamp-shaped line inside a fence is documentation, never a declaration;
#   2. parse the FIRST Evidence-Source line into a citation, in BOTH legal
#      forms (``OCC#<n>`` and a bare 7-40 hex commit SHA);
#   3. RESOLVE that citation against the live onex_change_control companion set
#      for this exact product PR. Skip only when it resolves to an OPEN or
#      MERGED companion on this PR's deterministic ``auto/...-occ-autobind``
#      branch AND its generated contract plus product receipt bind the current
#      product head. An OPEN stale companion is refreshed through the existing
#      deterministic effect path; a MERGED, missing, or unparseable binding
#      fails closed because immutable evidence requires append-only
#      supersession.
#
# An authority or binding-resolution failure DECLINES nonzero rather than
# publishing from event-environment data. The environment is only a trigger:
# a delayed synchronize event, malformed response, or ambiguous artifact must
# be retried from a fresh live product-PR read. A citation that is absent or
# resolves to no companion remains eligible for the existing deterministic
# effect path. See product_pr_evidence_citation / resolve_citation.
#
# BROKER / LANE RESOLUTION: identical to publish_occ_autobind_command.py
# (OMN-14801 overlay-driven lane resolution, OMN-14813 secret-free concrete dev
# broker, OMN-14639 fail-loud flush, OMN-14451 trusted-runner fail-closed).
# The helpers are REUSED from that script by file path — single source of
# truth, no second copy of the lane-resolution logic to drift.
#
# MACHINE-READABLE VERDICT (OMN-15615 AC7): every terminating path prints
# exactly one verdict marker on stdout — ``skipped_bound_to: OCC#<n>`` when the
# publish is suppressed, ``published_correlation_id: <uuid>`` when a command
# reaches the broker, or ``publish_declined: <reason>`` for the lane/broker
# no-op exits that were already green. A green publisher job carrying NONE of
# these is the vacuous-SUCCESS shape this ticket exists to retire; the caller
# workflow greps for a marker and fails the job when none is present.
#
# Ticket: OMN-14941 (defect fixed under OMN-15615)
#
# Required environment variables (when not --dry-run):
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnibase_infra
#   PR_NUMBER                 -- PR number as a string (cast to int here — GHA
#                                env is always a string)
#   PR_HEAD_SHA               -- product PR head commit SHA (log/forensics only;
#                                NOT carried on the wire — the RSD-2 read
#                                re-resolves live state)
#
# Optional:
#   PR_BODY                   -- product PR body; when it carries a canonical
#                                Evidence-Source stamp that RESOLVES to a live
#                                companion for this PR the publish is skipped
#                                loudly (already bound). A prose mention is not
#                                a stamp (OMN-16710) and a stamp that resolves
#                                to nothing is not a binding (OMN-15615).
#   GH_TOKEN / GITHUB_TOKEN   -- optional GitHub token for the citation
#                                resolution read. onex_change_control is a
#                                PUBLIC repo so the read works unauthenticated,
#                                but the shared self-hosted fleet burns the
#                                60/hr anonymous budget quickly; a token raises
#                                it. Absent/expired makes authoritative
#                                resolution unavailable and therefore declines
#                                for retry; it never authorizes a publish.
#   KAFKA_BOOTSTRAP_SERVERS   -- injected broker (fail-loud-checked against the
#                                overlay-declared lane broker when both exist)
#   KAFKA_SASL_USERNAME       -- SASL username / API key (cloud broker only)
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret (cloud broker only)
#
# Usage:
#   python scripts/publish_occ_companion_effect_command.py --lane dev [--dry-run]

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import click
import yaml


def _load_sibling(module_name: str, filename: str) -> ModuleType:
    """Load a sibling module by file path (never import the omnimarket package).

    This thin GHA script runs on a sparse checkout with only minimal deps
    installed; importing ``omnimarket.*`` would execute the package __init__ and
    pull in the full product dependency graph. Mirrors the file-path loading
    pattern established by publish_occ_autobind_command.py.
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {filename} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Single source of truth for the OMN-14801/OMN-14813 lane-resolution +
# trusted-runner + producer-transport helpers: reuse the autobind publisher's
# implementations rather than committing a second copy that can drift.
_AUTOBIND = _load_sibling(
    "publish_occ_autobind_command_for_companion_effect",
    "publish_occ_autobind_command.py",
)

# SLF001 noqa'd deliberately: these are sibling-script internals reused as the
# single source of truth (both scripts are covered by the same unit suites);
# duplicating them here is the drift vector OMN-14801 exists to close.
_load_lane_overlay = _AUTOBIND._load_lane_overlay  # noqa: SLF001
_resolve_lane_broker = _AUTOBIND._resolve_lane_broker  # noqa: SLF001
_is_trusted_runner = _AUTOBIND._is_trusted_runner  # noqa: SLF001
_kafka_producer_config = _AUTOBIND._kafka_producer_config  # noqa: SLF001
_LANE_OVERLAY_PATH = _AUTOBIND._LANE_OVERLAY_PATH  # noqa: SLF001
_MODE_NO_LANE = _AUTOBIND._MODE_NO_LANE  # noqa: SLF001
_MODE_UNKNOWN_LANE = _AUTOBIND._MODE_UNKNOWN_LANE  # noqa: SLF001
_MODE_INMEMORY = _AUTOBIND._MODE_INMEMORY  # noqa: SLF001
_MODE_FROM_SECRET = _AUTOBIND._MODE_FROM_SECRET  # noqa: SLF001
_MODE_CONCRETE = _AUTOBIND._MODE_CONCRETE  # noqa: SLF001

# Canonical topic constant (single source of truth in omnimarket.events.topics,
# loaded by file path for the same minimal-deps reason).
_TOPICS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "events" / "topics.py"
)
_TOPICS_SPEC = importlib.util.spec_from_file_location(
    "omnimarket_events_topics_for_occ_companion_effect", _TOPICS_PATH
)
if _TOPICS_SPEC is None or _TOPICS_SPEC.loader is None:
    raise RuntimeError(f"Could not load topic registry from {_TOPICS_PATH}")
_TOPICS_MODULE = importlib.util.module_from_spec(_TOPICS_SPEC)
_TOPICS_SPEC.loader.exec_module(_TOPICS_MODULE)

TOPIC = _TOPICS_MODULE.OCC_COMPANION_EFFECT_COMMAND_TOPIC_V1

# The stamp the canonical producer writes onto a bound product PR body. When a
# real stamp LINE is present, the companion already exists — publishing again is
# pure waste.
#
# OMN-16710: this used to be a bare substring test —
# ``"Evidence-Source: OCC#" in pr_body`` — which matched the literal ANYWHERE,
# including prose that merely discusses OCC evidence. Tripping it is silent
# green on a merge-gating path: nothing is published, no companion is minted,
# no stamp lands, and the job still exits 0. The product PR then hard-fails the
# `verify / verify` receipt gate (OMN-10419) for a stamp that was never minted,
# while the job responsible for minting it reports SUCCESS. Observed live on
# omnimemory#450 during the OMN-16708 canary — a docs PR *about* OCC publisher
# routing suppressed its own companion. Same silent-green shape OMN-14639 /
# OMN-14451 hardened the OTHER branch of this publisher against (the flush
# branch, which now refuses to report success on an undelivered command).
#
# The predicate below is a deliberate line-for-line MIRROR of the authoritative
# parser, ``omnibase_compat.contracts.pr_occ_stamp.pr_occ_metadata_stamp``
# (``_parse_evidence_source`` + ``occ_stamp_authoring.product_pr_occ_binding``)
# — same regex, same IGNORECASE, same per-line ``strip()``/``fullmatch``, same
# "first Evidence-Source line decides" rule. It is copied rather than imported
# because this thin GHA script must not pull in the package dependency graph
# (see ``_load_sibling``). The copy is pinned against the real parser by
# tests/unit/nodes/node_occ_companion_effect/
# test_publish_occ_companion_effect_command.py::
# TestIdempotencyAgreesWithCanonicalParser — a publisher that decides
# "already bound" on different evidence than the handler that does the minting
# is the same defect class as the substring match itself.
_EVIDENCE_SOURCE_PREFIX = "evidence-source:"
_EVIDENCE_SOURCE_OCC_PR_RE = re.compile(
    r"^Evidence-Source:\s+OCC#(\d+)\s*$",
    re.IGNORECASE,
)
_EVIDENCE_SOURCE_SHA_RE = re.compile(
    r"^Evidence-Source:\s+([0-9a-f]{7,40})\s*$",
    re.IGNORECASE,
)

# OMN-15615 / OMN-14682: a stamp-shaped line inside a fenced code block or a
# blockquote is DOCUMENTATION, never a machine-read declaration. This is a
# line-for-line mirror of the canonical helper
# ``omnibase_core.validation.validator_receipt_gate.strip_noncanonical_regions``
# — same fence pattern, same "matching fence char closes", same blank-line
# substitution (line positions preserved), same fail-closed "an unterminated
# fence blanks to end-of-body". It is copied rather than imported for the same
# minimal-deps reason as the parser below, and pinned byte-for-byte against the
# real helper by
# tests/unit/nodes/node_occ_companion_effect/
# test_publish_occ_companion_effect_command.py::
# TestStripAgreesWithCanonicalHelper. That seam test is what makes this a reuse
# of the canonical notion of "non-canonical region" rather than a fourth
# private one.
_FENCE_LINE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")


def strip_noncanonical_regions(pr_body: str) -> str:
    """Blank out PR-body regions that cannot carry a *canonical* stamp.

    Mirror of ``validator_receipt_gate.strip_noncanonical_regions`` (OMN-14682).
    Excluded lines become empty lines rather than disappearing, so line
    positions survive and every surviving canonical line parses identically.
    An unterminated opening fence blanks everything to end-of-body — a stamp
    after malformed markup is not a trustworthy declaration.

    Idempotent: re-stripping an already-stripped body is a no-op.
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in pr_body.splitlines():
        fence = _FENCE_LINE_PATTERN.match(line)
        if fence is not None:
            marker_char = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_char = marker_char
            elif marker_char == fence_char:
                in_fence = False
                fence_char = ""
            out.append("")
            continue
        if in_fence:
            out.append("")
            continue
        if line.lstrip().startswith(">"):
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


class Citation(NamedTuple):
    """The FIRST canonical ``Evidence-Source`` value on a product PR body.

    ``kind`` is ``"occ_pr"`` (value is the decimal OCC PR number) or ``"sha"``
    (value is a lowercased 7-40 hex commit SHA). Both are legal canonical forms
    — ``omnibase_compat.contracts.pr_occ_stamp`` parses both, and
    ``omniclaude/scripts/ci/check_occ_companion_merged.py`` resolves both.
    Recognising only the ``OCC#`` form was Mode B of OMN-15615: a PR bound by
    the SHA form looked unbound, so the publisher re-minted after its own
    companion had merged (live: omniclaude#1969 -> the duplicate OCC#5795).
    """

    kind: str
    value: str


def product_pr_evidence_citation(pr_body: str) -> Citation | None:
    """Parse the canonical Evidence-Source citation, in either legal form.

    Non-canonical regions are stripped FIRST (OMN-15615 AC1), then the
    canonical rules apply:

    * only a LINE whose stripped form is entirely the stamp counts — a mid-line
      mention, a ``> ``-quoted line, a list item, or an ``OCC#<n>`` placeholder
      with no digits does not;
    * the FIRST surviving line beginning ``Evidence-Source:`` decides; a
      malformed value there yields ``None`` (unparsable -> publish) even when a
      later line names a well-formed one.
    """
    for line in strip_noncanonical_regions(pr_body).splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith(_EVIDENCE_SOURCE_PREFIX):
            continue
        if occ_match := _EVIDENCE_SOURCE_OCC_PR_RE.fullmatch(stripped):
            return Citation(kind="occ_pr", value=occ_match.group(1))
        if sha_match := _EVIDENCE_SOURCE_SHA_RE.fullmatch(stripped):
            return Citation(kind="sha", value=sha_match.group(1).lower())
        return None
    return None


def product_pr_occ_binding(pr_body: str) -> int | None:
    """Return the cited OCC PR number, or ``None`` when none is cited.

    Retained as the narrow ``OCC#``-form view of
    :func:`product_pr_evidence_citation` for the seam test that pins this thin
    script against the canonical compat parser. It answers "what does the body
    SAY", never "is this PR bound" — the second question needs
    :func:`resolve_citation`, because a citation that resolves to nothing is
    not a binding (OMN-15615 AC2).
    """
    citation = product_pr_evidence_citation(pr_body)
    if citation is None or citation.kind != "occ_pr":
        return None
    return int(citation.value)


# --- Citation resolution (OMN-15615 AC2/AC3/AC4) ---------------------------
#
# The OCC repository holding every evidence companion. PUBLIC, so the read
# below needs no privileged credential.
_OCC_REPO = "OmniNode-ai/onex_change_control"
_GITHUB_API = "https://api.github.com"  # url-authority-ok: GitHub control-plane host for the onex_change_control companion read; matches node_prod_promotion_grant_resolver's _GITHUB_API_BASE, and this thin GHA script cannot import the routing authority
_RESOLUTION_TIMEOUT_SECONDS = 20


def companion_branch(repo: str, pr_number: int) -> str:
    """Return the deterministic OCC companion branch for a product PR.

    ``auto/<repo-slug-lower>-pr-<pr_number>-occ-autobind``. Mirrors
    ``handler_occ_companion_compute`` and ``OccCompanionVerifier._expected_branch``
    exactly — one OCC branch per product PR. This branch is what makes
    resolution "for THIS product PR" rather than "some companion exists
    somewhere".
    """
    return f"auto/{repo.replace('/', '-').lower()}-pr-{pr_number}-occ-autobind"


class Resolution(NamedTuple):
    """Outcome of resolving a citation against the live OCC companion set.

    ``bound`` is True ONLY when the citation names an OPEN or MERGED companion
    on this product PR's deterministic branch whose generated contract and
    product receipt bind the exact product head. ``fail_closed`` distinguishes
    unsafe evidence (missing, malformed, or stale merged provenance) from a
    safe refresh of an OPEN stale bot companion.
    """

    bound: bool
    reason: str
    occ_pr_number: int | None
    fail_closed: bool = False


class _Companion(NamedTuple):
    number: int
    open_or_merged: bool
    merged: bool
    head_sha: str
    shas: tuple[str, ...]


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_MAX_GITHUB_PAGES = 100


class _ProductPr(NamedTuple):
    repo: str
    number: int
    body: str
    head_sha: str


class _Binding(NamedTuple):
    repo: str
    pr_number: int
    head_sha: str
    evidence_item_id: str


def _canonical_repo(value: object) -> str | None:
    """Return an owner/repository slug only when it is structurally exact."""
    if not isinstance(value, str):
        return None
    pieces = value.split("/")
    if len(pieces) != 2 or not all(pieces):
        return None
    if any(
        not part.replace("-", "").replace("_", "").replace(".", "").isalnum()
        for part in pieces
    ):
        return None
    return "/".join(pieces)


def _normalised_full_sha(value: object) -> str | None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        return None
    return value.lower()


def _decode_github_content(payload: object) -> str | None:
    """Decode one GitHub contents response, rejecting every ambiguous shape."""
    if not isinstance(payload, dict):
        return None
    encoded = payload.get("content")
    if (
        payload.get("type") != "file"
        or payload.get("encoding") != "base64"
        or not isinstance(encoded, str)
    ):
        return None
    try:
        # GitHub wraps its canonical base64 with LF line endings. Remove ONLY
        # those transport line endings; validate=True rejects every other
        # whitespace/alphabet ambiguity instead of silently accepting it.
        decoded = base64.b64decode(encoded.replace("\n", ""), validate=True)
        return decoded.decode("utf-8")
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def _parse_product_ref_check(value: object) -> tuple[str, str] | None:
    """Parse the one generated ``gh api ...contents...?ref=<sha>`` check.

    ``check_value`` is a shell command, not a structured string to search.
    Tokenise it, then parse the API path/query as data. The accepted grammar is
    deliberately closed: ``gh api <contents-path>?ref=<full-sha> --jq
    .content`` (with an optional shell comment).  A proof is authority, so an
    otherwise-valid path paired with an unsupported ``gh`` option must not be
    accepted merely because that option is unrelated to the path parser.
    """
    if not isinstance(value, str):
        return None
    try:
        # Honour shell comments.  In particular, the canonical generated
        # command carries a trailing ``# pulls/<number>`` annotation.  A quoted
        # ``'#'`` remains an argument and is rejected by the closed grammar.
        tokens = shlex.split(value, comments=True)
    except ValueError:
        return None
    if len(tokens) != 5 or tokens[:2] != ["gh", "api"]:
        return None
    if tokens[3:] != ["--jq", ".content"]:
        return None
    api_path = urllib.parse.urlsplit(tokens[2])
    if api_path.scheme or api_path.netloc or api_path.fragment:
        return None
    path_parts = api_path.path.split("/")
    if len(path_parts) < 5 or path_parts[0] != "repos":
        return None
    # ``repos/<owner>/<repo>/contents/<path>`` has no empty path components.
    if path_parts[3] != "contents" or not all(path_parts[4:]):
        return None
    repo = _canonical_repo(f"{path_parts[1]}/{path_parts[2]}")
    query = urllib.parse.parse_qs(api_path.query, keep_blank_values=True)
    if repo is None or set(query) != {"ref"} or len(query["ref"]) != 1:
        return None
    head_sha = _normalised_full_sha(query["ref"][0])
    if head_sha is None:
        return None
    return repo, head_sha


def _parse_contract_binding(content: str) -> _Binding | None:
    """Read exactly one generated product evidence declaration from a contract."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict):
        return None
    evidence = document.get("dod_evidence")
    if not isinstance(evidence, list):
        return None

    candidates: list[tuple[str, str, str]] = []
    for item in evidence:
        if not isinstance(item, dict) or item.get("source") != "generated":
            continue
        item_id = item.get("id")
        checks = item.get("checks")
        if not isinstance(item_id, str) or not item_id or not isinstance(checks, list):
            return None
        for check in checks:
            if not isinstance(check, dict) or check.get("check_type") != "command":
                continue
            parsed = _parse_product_ref_check(check.get("check_value"))
            if parsed is not None:
                candidates.append((item_id, *parsed))
    if len(candidates) != 1:
        return None
    evidence_item_id, repo, head_sha = candidates[0]
    return _Binding(repo, 0, head_sha, evidence_item_id)


def _parse_receipt_binding(content: str) -> tuple[str, int, str, str] | None:
    """Parse a receipt's typed fields and its canonical product PR probe."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict):
        return None
    evidence_item_id = document.get("evidence_item_id")
    pr_number = document.get("pr_number")
    head_sha = _normalised_full_sha(document.get("commit_sha"))
    branch = document.get("branch")
    probe_command = document.get("probe_command")
    if (
        not isinstance(evidence_item_id, str)
        or not evidence_item_id
        or not isinstance(pr_number, int)
        or pr_number <= 0
        or head_sha is None
        or not isinstance(branch, str)
        or not isinstance(probe_command, str)
    ):
        return None
    try:
        # See _parse_product_ref_check: parse comments as a shell would, while
        # retaining a quoted '#' as an ordinary (and unsupported) argument.
        tokens = shlex.split(probe_command, comments=True)
    except ValueError:
        return None
    if len(tokens) < 7 or tokens[:3] != ["gh", "pr", "view"]:
        return None
    if tokens[3] != str(pr_number):
        return None

    # Closed ``gh pr view`` proof grammar.  The selector appears exactly once
    # in any supported gh spelling: ``--repo value``, ``--repo=value``, ``-R
    # value``, or compact ``-Rvalue``.  We intentionally do not admit other
    # gh flags (notably ``--hostname``), so parsing a receipt cannot silently
    # bless a command that changes its authority boundary.
    repo_values: list[str] = []
    json_values: list[str] = []
    index = 4
    while index < len(tokens):
        token = tokens[index]
        if token in {"--repo", "-R"}:
            index += 1
            if index >= len(tokens):
                return None
            repo_values.append(tokens[index])
        elif token.startswith("--repo="):
            repo_values.append(token.removeprefix("--repo="))
        elif token.startswith("-R") and len(token) > 2:
            compact_value = token[2:]
            repo_values.append(compact_value.removeprefix("="))
        elif token == "--json":
            index += 1
            if index >= len(tokens):
                return None
            json_values.append(tokens[index])
        elif token.startswith("--json="):
            json_values.append(token.removeprefix("--json="))
        else:
            return None
        index += 1

    if len(repo_values) != 1 or len(json_values) != 1:
        return None
    repo = _canonical_repo(repo_values[0])
    if repo is None or not json_values[0] or json_values[0].startswith("-"):
        return None
    return evidence_item_id, pr_number, head_sha, branch + "\n" + repo


def _list_changed_paths(
    companion: _Companion,
    *,
    token: str,
    fetch: Callable[[str, str], object | None],
) -> tuple[str, ...] | None:
    """Fully enumerate the changed file set; any pagination uncertainty fails."""
    paths: list[str] = []
    for page in range(1, _MAX_GITHUB_PAGES + 1):
        payload = fetch(
            f"{_GITHUB_API}/repos/{_OCC_REPO}/pulls/{companion.number}/files"
            f"?per_page=100&page={page}",
            token,
        )
        if not isinstance(payload, list):
            return None
        for item in payload:
            path = item.get("filename") if isinstance(item, dict) else None
            if not isinstance(path, str) or not path or path in paths:
                return None
            paths.append(path)
        if len(payload) < 100:
            return tuple(paths)
    return None


def _companion_product_head_binding(
    companion: _Companion,
    *,
    repo: str,
    pr_number: int,
    product_head_sha: str,
    token: str,
    fetch: Callable[[str, str], object | None],
) -> str:
    """Classify generated OCC provenance for one cited companion.

    The companion's branch identifies a product PR but does not identify the
    product *revision*.  Only the generated contract probe and the matching
    product receipt do that.  Both must parse and bind the exact event head;
    a missing or malformed artifact is deliberately not treated as historical
    evidence that happens to be good enough.
    """
    if not _FULL_SHA_RE.fullmatch(product_head_sha):
        return "product_head_invalid"

    paths = _list_changed_paths(companion, token=token, fetch=fetch)
    if paths is None:
        return "binding_unparseable_or_truncated_files"
    contract_paths = tuple(
        path
        for path in paths
        if path.startswith("contracts/") and path.endswith(".yaml")
    )
    receipt_paths = tuple(
        path
        for path in paths
        if path.startswith("drift/dod_receipts/") and path.endswith(".yaml")
    )
    if len(contract_paths) != 1 or not receipt_paths:
        return "binding_artifact_cardinality_mismatch"

    content_by_path: dict[str, str] = {}
    for path in [*contract_paths, *receipt_paths]:
        content_url = (
            f"{_GITHUB_API}/repos/{_OCC_REPO}/contents/"
            f"{urllib.parse.quote(path, safe='/')}?ref={companion.head_sha}"
        )
        content = _decode_github_content(fetch(content_url, token))
        if content is None:
            return "binding_unparseable_content_envelope"
        content_by_path[path] = content

    contract = _parse_contract_binding(content_by_path[contract_paths[0]])
    if contract is None:
        return "binding_unparseable_contract"
    contract_evidence_id, contract_repo, contract_head = (
        contract.evidence_item_id,
        contract.repo,
        contract.head_sha,
    )

    receipts: list[tuple[str, int, str, str, str]] = []
    for path in receipt_paths:
        parsed = _parse_receipt_binding(content_by_path[path])
        if parsed is None:
            return "binding_unparseable_receipt"
        evidence_item_id, receipt_pr, receipt_head, branch_and_repo = parsed
        receipt_branch, receipt_repo = branch_and_repo.split("\n", maxsplit=1)
        if evidence_item_id == contract_evidence_id:
            receipts.append(
                (path, receipt_pr, receipt_head, receipt_branch, receipt_repo)
            )
    if len(receipts) != 1:
        return "binding_receipt_cardinality_mismatch"

    _, receipt_pr, receipt_head, receipt_branch, receipt_repo = receipts[0]
    normalized_head = product_head_sha.lower()
    if (
        contract_repo != repo
        or receipt_repo != repo
        or receipt_pr != pr_number
        or receipt_branch != companion_branch(repo, pr_number)
    ):
        return "binding_product_identity_mismatch"
    if contract_head != receipt_head:
        return "binding_conflicting_heads"
    if contract_head != normalized_head:
        return "binding_stale_product_head"
    return "binding_matches_product_head"


def _github_get_json(url: str, token: str) -> object | None:
    """GET ``url`` as JSON. Returns ``None`` on ANY failure — the caller reads
    that as INDETERMINATE and publishes (never as "not bound, but confidently").
    """
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "occ-companion-effect-publisher")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=_RESOLUTION_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                return None
            decoded: object = json.loads(response.read().decode("utf-8"))
            return decoded
    except (
        http.client.HTTPException,
        urllib.error.URLError,
        OSError,
        ValueError,
        TimeoutError,
    ):
        return None


def _read_live_product_pr(
    repo: str,
    pr_number: int,
    *,
    token: str,
    fetch: Callable[[str, str], object | None],
) -> _ProductPr | None:
    """Read the product PR authority; environment values are only a trigger."""
    payload = fetch(f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}", token)
    if not isinstance(payload, dict) or payload.get("number") != pr_number:
        return None
    body = payload.get("body")
    head = payload.get("head")
    base = payload.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    base_repo_name = base_repo.get("full_name") if isinstance(base_repo, dict) else None
    head_sha = _normalised_full_sha(head.get("sha") if isinstance(head, dict) else None)
    if (
        not isinstance(body, (str, type(None)))
        or head_repo_name != repo
        or base_repo_name != repo
        or head_sha is None
    ):
        return None
    return _ProductPr(repo, pr_number, body or "", head_sha)


def _live_product_pr_is_unchanged(
    snapshot: _ProductPr,
    *,
    token: str,
    fetch: Callable[[str, str], object | None],
) -> bool:
    """A second authoritative read closes the synchronize/event TOCTOU gap."""
    return (
        _read_live_product_pr(snapshot.repo, snapshot.number, token=token, fetch=fetch)
        == snapshot
    )


def _parse_companions(
    payload: object, expected_branch: str
) -> tuple[_Companion, ...] | None:
    """Normalise the GitHub pulls payload. ``None`` == malformed (indeterminate)."""
    if not isinstance(payload, list):
        return None
    companions: list[_Companion] = []
    numbers: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            return None
        number = item.get("number")
        state = item.get("state")
        if (
            not isinstance(number, int)
            or number <= 0
            or number in numbers
            or not isinstance(state, str)
            or state.lower() not in {"open", "closed"}
        ):
            return None
        merged_at = item.get("merged_at")
        if merged_at is not None and not isinstance(merged_at, str):
            return None
        merged = merged_at is not None
        head = item.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        normalized_head_sha = _normalised_full_sha(head_sha)
        head_ref = head.get("ref") if isinstance(head, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        head_repo_name = (
            head_repo.get("full_name") if isinstance(head_repo, dict) else None
        )
        if (
            normalized_head_sha is None
            or head_ref != expected_branch
            or head_repo_name != _OCC_REPO
        ):
            return None
        merge_sha = item.get("merge_commit_sha")
        if merge_sha is not None and _normalised_full_sha(merge_sha) is None:
            return None
        shas_list: list[str] = []
        for value in (head_sha, merge_sha):
            normalized_sha = _normalised_full_sha(value)
            if normalized_sha is not None:
                shas_list.append(normalized_sha)
        companions.append(
            _Companion(
                number=number,
                open_or_merged=(state.lower() == "open") or merged,
                merged=merged,
                head_sha=normalized_head_sha,
                shas=tuple(shas_list),
            )
        )
        numbers.add(number)
    return tuple(companions)


def resolve_citation(
    citation: Citation,
    repo: str,
    pr_number: int,
    product_head_sha: str,
    token: str = "",
    fetch: Callable[[str, str], object | None] | None = None,
) -> Resolution:
    """Resolve ``citation`` against the live OCC companions of this product PR.

    ONE read: the onex_change_control pull requests whose head ref is this
    product PR's deterministic companion branch, in any state. That single
    query answers both halves of AC2 at once — "does the cited artifact exist"
    and "is it this PR's companion" — because a companion for a DIFFERENT
    product PR is simply not in the returned set. A body that quotes some other
    PR's ``OCC#<n>`` therefore resolves to nothing and publishes.

    A missing companion or a closed-unmerged companion remains eligible for a
    fresh deterministic effect. A cited open/merged companion must additionally
    prove its generated provenance binds ``product_head_sha``; without that
    proof the publisher never silently skips.
    """
    fetcher = fetch if fetch is not None else _github_get_json
    owner = _OCC_REPO.split("/")[0]
    branch = companion_branch(repo, pr_number)
    all_companions: list[_Companion] = []
    companion_numbers: set[int] = set()
    for page in range(1, _MAX_GITHUB_PAGES + 1):
        url = (
            f"{_GITHUB_API}/repos/{_OCC_REPO}/pulls"
            f"?head={owner}:{branch}&state=all&per_page=100&page={page}"
        )
        payload = fetcher(url, token)
        if payload is None:
            return Resolution(False, "resolution_unavailable", None, fail_closed=True)
        companions = _parse_companions(payload, branch)
        if companions is None:
            return Resolution(
                False, "resolution_malformed_payload", None, fail_closed=True
            )
        if any(companion.number in companion_numbers for companion in companions):
            return Resolution(
                False, "resolution_malformed_payload", None, fail_closed=True
            )
        companion_numbers.update(companion.number for companion in companions)
        all_companions.extend(companions)
        if len(companions) < 100:
            break
    else:
        return Resolution(
            False, "resolution_truncated_companion_set", None, fail_closed=True
        )
    companions = tuple(all_companions)
    if not companions:
        return Resolution(False, "no_companion_exists_for_this_pr", None)

    if citation.kind == "occ_pr":
        cited = int(citation.value)
        for companion in companions:
            if companion.number != cited:
                continue
            if companion.open_or_merged:
                binding = _companion_product_head_binding(
                    companion,
                    repo=repo,
                    pr_number=pr_number,
                    product_head_sha=product_head_sha,
                    token=token,
                    fetch=fetcher,
                )
                if binding == "binding_matches_product_head":
                    return Resolution(
                        True, "cited_companion_open_or_merged_same_head", cited
                    )
                if binding == "binding_stale_product_head":
                    if companion.merged:
                        return Resolution(
                            False,
                            "cited_merged_companion_stale_product_head",
                            cited,
                            fail_closed=True,
                        )
                    # An OPEN bot-owned companion is safe to refresh only by
                    # republishing the existing deterministic effect command.
                    return Resolution(
                        False, "cited_open_companion_stale_product_head", cited
                    )
                return Resolution(False, binding, cited, fail_closed=True)
            # The OMN-15214 incident state: a companion CLOSED without merging
            # is destroyed evidence, not a binding. Re-mint.
            return Resolution(False, "cited_companion_closed_unmerged", None)
        return Resolution(False, "citation_is_not_a_companion_of_this_pr", None)

    # SHA form. The producer stamps the companion's MERGE commit (live:
    # omniclaude#1969 carried 64174b87…, the merge_commit_sha of OCC#5793 on
    # branch auto/omninode-ai-omniclaude-pr-1969-occ-autobind). Head sha is
    # accepted too; both are compared as prefixes because the canonical stamp
    # grammar admits 7-40 hex.
    for companion in companions:
        if not companion.merged:
            continue
        if any(sha.startswith(citation.value) for sha in companion.shas):
            binding = _companion_product_head_binding(
                companion,
                repo=repo,
                pr_number=pr_number,
                product_head_sha=product_head_sha,
                token=token,
                fetch=fetcher,
            )
            if binding == "binding_matches_product_head":
                return Resolution(
                    True,
                    "cited_sha_is_a_merged_companion_of_this_pr_same_head",
                    companion.number,
                )
            if binding == "binding_stale_product_head":
                return Resolution(
                    False,
                    "cited_merged_companion_stale_product_head",
                    companion.number,
                    fail_closed=True,
                )
            return Resolution(False, binding, companion.number, fail_closed=True)
    return Resolution(False, "sha_is_not_a_merged_companion_of_this_pr", None)


# Wire literal for the command mode. MUST equal the "mutate" member of
# ModelOccCompanionEffectRequest.mode — asserted by the seam test rather than
# imported here (importing the node's model package into this thin GHA-runner
# script would pull in far more than the minimal deps the workflow installs).
_MODE_MUTATE = "mutate"


def build_payload(
    repo: str,
    pr_number: int,
    correlation_id: str,
    allow_merged_replay: bool = False,
) -> dict[str, object]:
    """Return a companion-effect command payload shaped as ModelOccCompanionEffectRequest.

    The runtime consumes this off
    ``onex.cmd.omnimarket.occ-companion-effect-requested.v1`` and the contract's
    routing validates it against ``ModelOccCompanionEffectRequest``
    (``frozen=True, extra='forbid'``) before dispatching to
    ``HandlerOccCompanionEffect``. The keys here MUST be exactly (a subset of)
    the command model's fields — the occ-autobind born-path bug was a payload
    that never matched its consumer model, so the command was silently DLQ'd and
    the effect never fired (OMN-13990). Specifically:

    * ``mode`` is ALWAYS ``"mutate"``: the model defaults to ``dry_run``
      (fail-safe), so omitting it would silently never mint (the
      optional-input-silent-skip trap).
    * ``pr_number`` is an ``int`` (cast from the GHA string env by the caller).
    * ``occ_repo``/``runner``/``verifier`` are omitted so the model defaults
      apply (runner != verifier, OMN-12791).
    * ``allow_merged_replay`` (OMN-16665) is emitted ONLY when True. It is the
      deliberately-scoped F-17 override that authors the companion for a PR
      which MERGED without one — the merge/queue-latency race in which this
      publisher's job is held long enough that the PR merges before the command
      reaches compute (live: omnimemory#447, published ~90s after the merge).
      Emitting the key unconditionally would be harmless to the model but would
      put a False in every born-path payload; omitting it keeps the born-path
      wire shape byte-identical to pre-16665.
    * NO legacy occ-autobind fields (block_reason/ticket_id/requested_at/
      pr_head_sha/event_id/topic) — ``extra='forbid'`` rejects every one.
    """
    payload: dict[str, object] = {
        "repo": repo,
        "pr_number": pr_number,
        "mode": _MODE_MUTATE,
        "correlation_id": correlation_id,
    }
    if allow_merged_replay:
        payload["allow_merged_replay"] = True
    return payload


def publish_occ_companion_effect_command(
    bootstrap_servers: str,
    username: str,
    password: str,
    repo: str,
    pr_number: int,
) -> str:
    """Publish the companion-effect command to Kafka. Returns the correlation_id."""
    from confluent_kafka import Producer  # type: ignore[import-untyped,unused-ignore]

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        correlation_id=correlation_id,
    )

    producer = Producer(_kafka_producer_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(payload, default=str).encode("utf-8")
    key = f"occ-companion-effect/{repo}/{pr_number}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    # OMN-14639: flush() returns the number of messages STILL in the producer
    # queue when the timeout elapses. A broker that refuses the connection
    # leaves the message queued and unacked; librdkafka's per-message delivery
    # timeout is far larger than this 30s flush window, so `_on_delivery` never
    # fires and `delivery_error` stays None. A non-zero remaining count means
    # the command did NOT reach the broker — a hard delivery failure, never
    # success (the "runs green while publishing nothing" class).
    remaining = producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    if remaining and remaining > 0:
        raise RuntimeError(
            f"Kafka delivery timed out: {remaining} message(s) still undelivered "
            f"to {bootstrap_servers} after a 30s flush (broker unreachable / "
            "connection refused). Refusing to report success on an undelivered "
            "occ-companion-effect command (OMN-14639)."
        )

    return correlation_id


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print payload without publishing to Kafka",
)
@click.option(
    "--lane",
    default=None,
    help=(
        "CI bus lane id (e.g. 'dev') resolved against config/ci_bus_lanes.yaml "
        "to decide the bus target and to fail-loud-check any injected "
        "KAFKA_BOOTSTRAP_SERVERS against the lane's declared broker (OMN-14801). "
        "Required on the trusted self-hosted runner for an authoring PR."
    ),
)
def main(dry_run: bool, lane: str | None) -> None:
    """Publish onex.cmd.omnimarket.occ-companion-effect-requested.v1 for a product PR.

    All inputs are read from environment variables injected by the GHA workflow:
    PR_REPO, PR_NUMBER, PR_HEAD_SHA, PR_BODY (optional, idempotency skip),
    ALLOW_MERGED_REPLAY (optional, OMN-16665 merged-PR recovery). The bus target
    is resolved from ``--lane`` against config/ci_bus_lanes.yaml.
    """
    repo = os.environ.get("PR_REPO", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")
    pr_head_sha = os.environ.get("PR_HEAD_SHA", "")
    # OMN-16665: only the exact literal "true" enables the override. A loose
    # truthiness read ("false" is a non-empty string) would silently arm the
    # F-17 override on every born-path publish that set the variable at all.
    allow_merged_replay = (
        os.environ.get("ALLOW_MERGED_REPLAY", "").strip().lower() == "true"
    )

    if not repo or not pr_number_str or not pr_head_sha:
        click.echo(
            "ERROR: PR_REPO, PR_NUMBER, and PR_HEAD_SHA must all be set",
            err=True,
        )
        sys.exit(1)

    repo = _canonical_repo(repo) or ""
    try:
        # GHA env values are ALWAYS strings; the consumer model requires int.
        pr_number = int(pr_number_str)
    except ValueError:
        click.echo(
            f"ERROR: PR_NUMBER must be an integer, got: {pr_number_str!r}", err=True
        )
        sys.exit(1)
    if pr_number <= 0 or _normalised_full_sha(pr_head_sha) is None or not repo:
        click.echo(
            "ERROR: PR_REPO, PR_NUMBER, and PR_HEAD_SHA must be canonical "
            "owner/repo, positive integer, and full 40-hex SHA values",
            err=True,
        )
        sys.exit(1)

    token = (
        os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    ).strip()
    fetcher = _github_get_json

    # A dry run neither skips nor publishes a command; it is intentionally
    # offline so validation jobs can exercise the frozen wire shape.
    if dry_run:
        correlation_id = str(uuid.uuid4())
        payload = build_payload(
            repo=repo,
            pr_number=pr_number,
            correlation_id=correlation_id,
            allow_merged_replay=allow_merged_replay,
        )
        click.echo(
            f"occ-companion-effect command: repo={repo} pr={pr_number} "
            f"head={pr_head_sha} lane={lane!r} "
            f"allow_merged_replay={allow_merged_replay}"
        )
        click.echo("publish_declined: dry_run")
        click.echo("(dry-run: skipping Kafka publish)")
        click.echo(json.dumps(payload, indent=2))
        sys.exit(0)

    live_pr = _read_live_product_pr(repo, pr_number, token=token, fetch=fetcher)
    if live_pr is None:
        click.echo("publish_declined: live_product_pr_unavailable")
        click.echo(
            f"ERROR: cannot authoritatively read {repo}#{pr_number}; retry rather "
            "than using event environment evidence.",
            err=True,
        )
        sys.exit(1)
    if live_pr.head_sha != pr_head_sha.lower():
        click.echo("publish_declined: product_head_changed")
        click.echo(
            f"ERROR: event head {pr_head_sha} differs from live {live_pr.head_sha}; "
            "retry from the current synchronized PR.",
            err=True,
        )
        sys.exit(1)

    # Publisher-side idempotency (OMN-14941): an already-bound product PR needs
    # no companion — skip loudly, exit 0. This is a cheap pre-filter; the
    # handler's compute no-ops on the same condition as the authoritative check.
    # OMN-16710 made it line-anchored; OMN-15615 makes it artifact-resolved —
    # a citation that does not resolve to a live companion of THIS PR is not a
    # binding, and an answer we could not obtain is never a binding.
    citation = product_pr_evidence_citation(live_pr.body)
    if citation is None:
        resolution = Resolution(False, "no_evidence_source_citation", None)
    else:
        resolution = resolve_citation(
            citation,
            repo=repo,
            pr_number=pr_number,
            product_head_sha=live_pr.head_sha,
            token=token,
            fetch=fetcher,
        )
    if resolution.fail_closed:
        # A cited companion with absent, ambiguous, or stale generated
        # provenance is not evidence for this head.  An OPEN companion can be
        # refreshed through the normal effect path, but a MERGED companion is
        # immutable append-only evidence and must be superseded explicitly.
        click.echo(f"publish_declined: {resolution.reason}")
        click.echo(
            f"ERROR: {repo}#{pr_number} cites OCC#{resolution.occ_pr_number}, but "
            f"its generated provenance is not valid for product head {live_pr.head_sha}. "
            "Refusing to suppress or replay the publisher; create an append-only "
            "superseding companion when the cited evidence is merged.",
            err=True,
        )
        sys.exit(1)
    if resolution.bound:
        if not _live_product_pr_is_unchanged(live_pr, token=token, fetch=fetcher):
            click.echo("publish_declined: live_product_pr_changed_before_skip")
            click.echo(
                "ERROR: product PR changed while companion evidence was resolved; retry.",
                err=True,
            )
            sys.exit(1)
        # AC7: the machine-readable verdict names the RESOLVED companion. A
        # skip that cannot name one is the vacuous-SUCCESS shape itself.
        click.echo(f"skipped_bound_to: OCC#{resolution.occ_pr_number}")
        click.echo(
            f"SKIP: {repo}#{pr_number} is already bound to "
            f"OCC#{resolution.occ_pr_number} on branch "
            f"{companion_branch(repo, pr_number)} "
            f"({resolution.reason}) — not publishing a redundant "
            "occ-companion-effect command (publisher-side idempotency, "
            "OMN-14941; artifact-resolved per OMN-15615). Exiting 0."
        )
        sys.exit(0)

    click.echo(
        f"publish_reason: {resolution.reason}"
        + (
            ""
            if citation is None
            else f" (citation {citation.kind}={citation.value} did not resolve)"
        )
    )

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        correlation_id=correlation_id,
        allow_merged_replay=allow_merged_replay,
    )

    click.echo(
        f"occ-companion-effect command: repo={repo} pr={pr_number} "
        f"head={pr_head_sha} lane={lane!r} "
        f"allow_merged_replay={allow_merged_replay}"
    )

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")

    # Read once; a wiring gap (RUNNER_IS_TRUSTED unset/invalid) fails fast here
    # rather than silently choosing the permissive branch (OMN-14451).
    trusted = _is_trusted_runner()

    overlay = _load_lane_overlay()
    mode, declared_broker = _resolve_lane_broker(overlay, lane)

    def _publish_or_die(target_broker: str) -> None:
        if not _live_product_pr_is_unchanged(live_pr, token=token, fetch=fetcher):
            click.echo("publish_declined: live_product_pr_changed_before_publish")
            click.echo(
                "ERROR: product PR changed while companion evidence was resolved; retry.",
                err=True,
            )
            sys.exit(1)
        try:
            published_id = publish_occ_companion_effect_command(
                bootstrap_servers=target_broker,
                username=username,
                password=password,
                repo=repo,
                pr_number=pr_number,
            )
        except Exception as exc:
            click.echo(f"Delivery error: {exc}", err=True)
            sys.exit(1)
        # AC7: the verdict marker a green publish MUST carry.
        click.echo(f"published_correlation_id: {published_id}")
        click.echo(f"Published {TOPIC} event_id={published_id}")

    # --- OMN-14801: overlay-driven lane -> bus-target resolution (same branch
    # semantics as publish_occ_autobind_command.py; helpers reused above) ------
    if mode in (_MODE_NO_LANE, _MODE_UNKNOWN_LANE):
        if trusted:
            lanes_obj = overlay.get("lanes")
            declared = sorted(lanes_obj) if isinstance(lanes_obj, dict) else []
            reason = (
                "--lane was not supplied"
                if mode == _MODE_NO_LANE
                else f"lane {lane!r} is not declared"
            )
            click.echo(
                f"ERROR: {reason} on the TRUSTED self-hosted runner for an "
                "occ-companion-effect-eligible PR. The publishing lane MUST "
                "resolve to a declared bus lane in "
                f"config/{_LANE_OVERLAY_PATH.name} (declared lanes: {declared}). "
                "Refusing to guess the bus lane / silently no-op (OMN-14801).",
                err=True,
            )
            sys.exit(1)
        click.echo(f"publish_declined: no_bus_lane_resolved_mode_{mode}")
        click.echo(
            f"WARNING: no bus lane resolved (mode={mode}, lane={lane!r}) -- "
            "skipping occ-companion-effect publish (expected on a fork/cloud "
            "runner). Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if mode == _MODE_INMEMORY:
        click.echo("publish_declined: lane_declares_in_memory_bus")
        click.echo(
            f"lane={lane!r} declares the in-memory bus (config-as-data default "
            f"in config/{_LANE_OVERLAY_PATH.name}): no cross-process broker to "
            "publish to. Skipping cross-process publish (no-op). Exiting 0."
        )
        sys.exit(0)

    if mode == _MODE_FROM_SECRET:
        if not bootstrap_servers:
            if trusted:
                click.echo(
                    "ERROR: KAFKA_BOOTSTRAP_SERVERS is not set on the TRUSTED "
                    f"self-hosted runner for lane={lane!r} (declared "
                    "'from-secret'). The broker MUST be resolvable here. Failing "
                    "loudly instead of silently skipping (OMN-14451).",
                    err=True,
                )
                sys.exit(1)
            click.echo("publish_declined: no_broker_on_untrusted_runner")
            click.echo(
                "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set -- skipping "
                "occ-companion-effect publish (expected on a fork/cloud runner "
                "with no broker provisioned). Exiting 0.",
                err=True,
            )
            sys.exit(0)
        _publish_or_die(bootstrap_servers)
        return

    # mode == _MODE_CONCRETE: the overlay declares an authoritative host:port
    # for this lane. Prefer the committed value AND fail-loud-check any injected
    # secret against it (the OMN-14800 silent-drift guard). The dev lane is
    # secret-free (OMN-14813): normally no KAFKA_BOOTSTRAP_SERVERS is injected
    # at all and the committed overlay value is used directly.
    if not bootstrap_servers:
        if trusted:
            click.echo(
                "KAFKA_BOOTSTRAP_SERVERS is not injected; publishing to the "
                f"overlay-declared broker for lane={lane!r}: {declared_broker} "
                f"(config/{_LANE_OVERLAY_PATH.name})."
            )
            _publish_or_die(declared_broker)
            return
        click.echo("publish_declined: no_broker_on_untrusted_runner")
        click.echo(
            "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set on a fork/cloud runner "
            f"-- skipping occ-companion-effect publish for lane={lane!r}. "
            "Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if bootstrap_servers != declared_broker:
        if trusted:
            click.echo(
                "ERROR: LANE BUS DRIFT -- the injected KAFKA_BOOTSTRAP_SERVERS "
                "(GH-masked in logs) does not match the broker declared for "
                f"lane={lane!r} in config/{_LANE_OVERLAY_PATH.name} (declared: "
                f"{declared_broker}). This is the silent dev->stability repoint "
                "class (OMN-14800): refusing to publish the occ-companion-effect "
                "command to an undeclared broker. Reconcile the injected secret "
                "with the checked-in lane overlay (OMN-14801).",
                err=True,
            )
            sys.exit(1)
        click.echo("publish_declined: lane_bus_drift_on_untrusted_runner")
        click.echo(
            "WARNING: injected KAFKA_BOOTSTRAP_SERVERS diverges from the "
            f"overlay-declared broker for lane={lane!r} on a fork/cloud runner "
            "-- skipping occ-companion-effect publish. Exiting 0.",
            err=True,
        )
        sys.exit(0)

    _publish_or_die(declared_broker)


if __name__ == "__main__":
    main()
