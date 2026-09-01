# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Module entrypoint for the contractor integration-note node (OMN-17277).

This is the surface a GitHub Actions shim invokes. It holds no logic of its own
beyond argument parsing and overlay loading: it builds the typed request, wires
the two effect adapters, and dispatches the node's handler.

The PR facts arrive as ONE JSON document in GitHub's own pull-request shape,
from either ``github.event.pull_request`` or ``gh api repos/<repo>/pulls/<n>``.
Both produce the same fields, so the merge path and the backfill path share a
parser instead of drifting. Passing them as a file rather than argv keeps
attacker-controlled strings (title, body) off every command line.

Usage:
    python -m omnimarket.nodes.node_contractor_integration_note_effect.cli \
        --repo OmniNode-ai/omnibase_infra \
        --pr-json pr.json \
        --repo-path . \
        --roster config/contractor_roster.yaml

Exit codes:
    0 — a note was posted, or no note was owed (the reason is printed).
    1 — a note WAS owed and delivery failed. Never silent: an undelivered note
        that exits 0 is the failure this node exists to end.
    2 — usage or configuration error (unreadable overlay, malformed PR JSON,
        unset credential).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_contractor_integration_note_effect.handlers.handler_contractor_integration_note import (
    HandlerContractorIntegrationNote,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelContractorRoster,
    ModelIntegrationNoteRequest,
    ModelMergedPullRequest,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    ModelIntegrationNoteResult,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.adapters import (
    GitReleaseStateProbe,
    LinearGraphqlNoteBoundary,
    ProtocolLinearNoteBoundary,
    ProtocolReleaseStateProbe,
    resolve_linear_api_key,
)

_log = logging.getLogger(__name__)


def load_roster(path: Path) -> ModelContractorRoster:
    """Load the contractor roster overlay.

    Fail-closed on a malformed overlay. A silently-ignored roster reads as
    "no contractors configured", which delivers nothing and looks identical to
    a quiet week — the exact ambiguity this node removes.
    """
    if not path.exists():
        raise FileNotFoundError(f"contractor roster not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"contractor roster must be a mapping: {path}")
    return ModelContractorRoster.model_validate(raw)


def parse_pull_request(repo: str, payload: dict[str, Any]) -> ModelMergedPullRequest:
    """Build the typed merge facts from GitHub's pull-request JSON shape.

    Refuses an unmerged PR outright. A closed-unmerged PR carries a null merge
    SHA, and a note about a commit that does not exist is worse than no note.
    """
    if payload.get("merged") is not True:
        raise ValueError(
            f"PR #{payload.get('number')} in {repo} is not merged; "
            "refusing to compose an integration note for it."
        )
    merge_sha = payload.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not merge_sha:
        raise ValueError(f"PR #{payload.get('number')} carries no merge_commit_sha")
    merged_at = payload.get("merged_at")
    if not isinstance(merged_at, str) or not merged_at:
        raise ValueError(f"PR #{payload.get('number')} carries no merged_at")
    base = payload.get("base") or {}
    return ModelMergedPullRequest(
        repo=repo,
        number=int(payload["number"]),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        merge_sha=merge_sha,
        merged_at=datetime.fromisoformat(merged_at.replace("Z", "+00:00")),
        base_ref=str(base.get("ref") or "") or "unknown",
        html_url=str(payload.get("html_url") or f"https://github.com/{repo}"),
    )


def run(
    request: ModelIntegrationNoteRequest,
    linear: ProtocolLinearNoteBoundary,
    releases: ProtocolReleaseStateProbe,
) -> ModelIntegrationNoteResult:
    """Dispatch the node handler. Separated so tests can inject fake adapters."""
    return HandlerContractorIntegrationNote(linear, releases).handle(request)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Post one integration note on the Linear ticket a merged PR cites, "
            "when that ticket is assigned to a configured contractor (OMN-17277)."
        )
    )
    parser.add_argument("--repo", required=True, help="GitHub repo slug (owner/repo).")
    parser.add_argument(
        "--pr-json",
        required=True,
        type=Path,
        help="File holding GitHub's pull-request JSON for the merged PR.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        type=Path,
        help=(
            "Checkout of --repo, with tags fetched, used to answer "
            "'is this merge in a released tag'. Required: an inferred repo root "
            "would silently answer about the wrong tree."
        ),
    )
    parser.add_argument(
        "--roster",
        required=True,
        type=Path,
        help="Path to the contractor roster overlay YAML.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose and print the note without writing it to Linear.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the typed result as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args(argv)

    try:
        roster = load_roster(args.roster)
        payload = json.loads(args.pr_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{args.pr_json} must contain a JSON object")
        pull_request = parse_pull_request(args.repo, payload)
        request = ModelIntegrationNoteRequest(
            pull_request=pull_request, roster=roster, dry_run=args.dry_run
        )
        # A dry run still READS Linear (assignee, posted keys) to reach a
        # truthful decision, so it wants the key too — but an unset key must
        # not turn a rehearsal into a hard configuration error. A real run
        # fails closed here instead.
        api_key = _dry_run_key_or_empty() if args.dry_run else resolve_linear_api_key()
    except (OSError, ValueError, ValidationError, RuntimeError) as exc:
        _log.error("%s", exc)
        return 2

    linear: ProtocolLinearNoteBoundary = LinearGraphqlNoteBoundary(api_key)
    releases: ProtocolReleaseStateProbe = GitReleaseStateProbe(args.repo_path)

    try:
        result = run(request, linear, releases)
    except (OSError, RuntimeError) as exc:
        _log.error("integration note delivery failed: %s", exc)
        return 1

    if args.json:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    elif result.posted:
        sys.stdout.write(
            f"posted integration note on {result.decision.ticket_identifier} "
            f"for {result.decision.recipient_display_name} "
            f"({result.decision.note_key}, {result.decision.reachability})\n"
        )
    else:
        sys.stdout.write(
            f"no note written for {result.decision.note_key}: "
            f"{result.decision.skip_reason or 'dry_run'}\n"
        )
        if args.dry_run and result.decision.note_body:
            sys.stdout.write("\n" + result.decision.note_body + "\n")
    if result.decision.redacted_fields:
        _log.warning(
            "fields withheld for internal references: %s",
            ", ".join(result.decision.redacted_fields),
        )
    return 0


def _dry_run_key_or_empty() -> str:
    """Best-effort credential for a dry run; empty is tolerated."""
    try:
        return resolve_linear_api_key()
    except RuntimeError:
        _log.warning(
            "LINEAR_API_KEY unset — dry run will fail to resolve the ticket and "
            "will report that, rather than assuming no note was owed."
        )
        return ""


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
