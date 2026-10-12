# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Audit live GitHub documents or replay a saved branch-protection snapshot.

Usage::

    python -m omnimarket.nodes.node_branch_protection_audit_effect --overlay policy.yaml
    python -m omnimarket.nodes.node_branch_protection_audit_effect --repos repo_a --snapshot snapshot.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_audit import (
    HandlerBranchProtectionAudit,
)
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_fetch import (
    HandlerBranchProtectionFetch,
)
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_policy_load import (
    BranchProtectionOverlayError,
    HandlerBranchProtectionPolicyLoad,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    DEFAULT_BRANCHES,
    DEFAULT_ORG,
    ModelBranchProtectionAuditRequest,
    ModelBranchProtectionFetchRequest,
    ModelBranchProtectionPolicyLoadRequest,
    ModelBranchProtectionSnapshot,
)


def _comma_list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _error(message: str) -> int:
    sys.stderr.write("ERROR: " + " ".join(message.splitlines()) + "\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--overlay-required", action="store_true")
    parser.add_argument("--repos", type=_comma_list)
    parser.add_argument("--branches", type=_comma_list, default=DEFAULT_BRANCHES)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--write-snapshot", type=Path)
    parser.add_argument("--jsonl", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = HandlerBranchProtectionPolicyLoad().handle(
            ModelBranchProtectionPolicyLoadRequest(
                overlay_path=args.overlay,
                overlay_required=args.overlay_required,
            )
        )
    except BranchProtectionOverlayError as exc:
        return _error(str(exc))
    if args.repos is not None:
        policy = policy.model_copy(update={"repos": cast(tuple[str, ...], args.repos)})
    if not policy.repos:
        return _error(
            "no repositories to audit: name them in the overlay or with --repos"
        )
    branches = cast(tuple[str, ...], args.branches)
    org = cast(str, args.org)
    if args.snapshot is not None:
        path = cast(Path, args.snapshot)
        try:
            snapshot = ModelBranchProtectionSnapshot.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as exc:
            return _error(f"could not read snapshot: {exc}")
    else:
        try:
            snapshot = HandlerBranchProtectionFetch().handle(
                ModelBranchProtectionFetchRequest(
                    policy=policy,
                    org=org,
                    branches=branches,
                )
            )
        except GitHubApiError as exc:
            return _error(str(exc))
    if args.write_snapshot is not None:
        try:
            cast(Path, args.write_snapshot).write_text(
                snapshot.model_dump_json() + "\n", encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            return _error(f"could not write snapshot: {exc}")
    result = HandlerBranchProtectionAudit().handle(
        ModelBranchProtectionAuditRequest(
            policy=policy,
            snapshot=snapshot,
            org=org,
            branches=branches,
            audited_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    if args.jsonl is not None:
        try:
            with cast(Path, args.jsonl).open("a", encoding="utf-8") as stream:
                for finding in result.findings:
                    record = {
                        "repo": finding.repo,
                        "branch": finding.branch,
                        "check": finding.check,
                        "status": finding.status.value,
                        "detail": finding.detail,
                    }
                    stream.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
        except (OSError, UnicodeError) as exc:
            return _error(f"could not append JSONL: {exc}")
    sys.stdout.write(result.report)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
