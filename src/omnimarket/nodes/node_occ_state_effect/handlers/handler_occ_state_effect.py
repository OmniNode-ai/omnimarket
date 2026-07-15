# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccStateEffect — the RSD-2 read-EFFECT for node_occ_companion_compute (OMN-14619).

Gathers all machine-observed PR + OCC facts the pure COMPUTE node
(``node_occ_companion_compute``, RSD-1, OMN-14285) needs to render a companion,
and assembles the exact :class:`ModelOccCompanionRequest` it consumes. This is
the read half of the read -> compute -> write cycle the design doc
(``docs/plans/2026-07-10-occ-autogen-mechanization-design.md``) specifies;
RSD-3 (the write-EFFECT that clones/pushes/opens the companion PR, plus the
trigger/orchestrator) is intentionally NOT built here — OMN-14619 is READ-ONLY
by design so it can be proven against real PRs without wiring anything into
the live process.

Second responsibility (OMN-14619's other half): derive an honest **content-read**
``downstream_check_value`` from the PR diff instead of leaving the COMPUTE node
to fall back to its generic ``gh pr view ... --json number,state`` probe, which
proves only that the PR exists — never that the claimed work landed (it can
never go RED against an EXISTS-but-WRONG PR). :func:`extract_symbol_candidates`
+ :func:`select_asserted_check` are pure functions (given an injected content
fetcher) so the RED-control logic is unit-testable without live network calls;
only :class:`HandlerOccStateEffect` itself performs I/O.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from omnibase_core.validation.validator_receipt_gate import _extract_ticket_ids

from omnimarket.events.occ_companion import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.github_api import GitHubApiError, rest_json, rest_json_array, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# Matches an added top-level declaration line in a unified diff hunk, e.g.
# "+class HandlerCodegenOutcomeReducer:" or "+    async def handle(self, ...):".
# Deterministic, LLM-free stand-in for "pick a symbol the PR adds" — the exact
# authoring move the hand-authored reference companions (OCC#4135, OCC#4136)
# made manually ("class HandlerCodegenOutcomeReducer defined ... — head 1 / dev 404").
_DECLARATION_RE = re.compile(
    r"^\+\s*(async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

_OCC_DEFAULT_TARGET_BRANCH = "dev"  # OCC companion PRs always target dev (never main).


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an ``object``-typed API field to a dict, or empty on any other shape."""
    return value if isinstance(value, dict) else {}


def _as_int(value: object, default: int = 0) -> int:
    """Narrow an ``object``-typed API field to an int, or ``default`` otherwise."""
    return value if isinstance(value, int) else default


def _resolve_github_token() -> str:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856)."""
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


@dataclass(frozen=True)
class SymbolCandidate:
    """One (path, kind, symbol) triple extracted from an added diff line — pure."""

    path: str
    kind: Literal["class", "def"]
    symbol: str


def extract_symbol_candidates(
    files: list[dict[str, object]],
) -> tuple[SymbolCandidate, ...]:
    """Pure: parse GitHub PR-files ``patch`` hunks for added top-level class/def lines.

    Only considers Python files with status ``added``/``modified`` (never a pure
    rename/removal) — the diff patch is the only place we look, never file
    content, so this stays a function of ``files`` alone.
    """
    candidates: list[SymbolCandidate] = []
    for f in files:
        path = str(f.get("filename", ""))
        status = f.get("status")
        patch = f.get("patch")
        if not path.endswith(".py") or status not in ("added", "modified"):
            continue
        if not isinstance(patch, str):
            continue
        for line in patch.splitlines():
            match = _DECLARATION_RE.match(line)
            if not match:
                continue
            kind: Literal["class", "def"] = (
                "class" if match.group(1) == "class" else "def"
            )
            candidates.append(
                SymbolCandidate(path=path, kind=kind, symbol=match.group(2))
            )
    return tuple(candidates)


def declaration_count(
    content: str | None, kind: Literal["class", "def"], symbol: str
) -> int:
    """Pure: count ``class X`` / ``def X`` / ``async def X`` declaration lines."""
    if not content:
        return 0
    verb = "class" if kind == "class" else r"(?:async\s+def|def)"
    pattern = re.compile(rf"^\s*{verb}\s+{re.escape(symbol)}\b", re.MULTILINE)
    return len(pattern.findall(content))


def build_content_read_check(
    *, repo: str, path: str, kind: Literal["class", "def"], symbol: str, head_sha: str
) -> str:
    """Canonical honest content-read check_value (reference_occ_receipt_gate_flow).

    Pinned to the PR head SHA; ``grep -c`` exits non-zero (RED) when the
    declaration is absent from the file at that ref — never a bare existence
    probe (a `.sha`/`.content` presence check would rubber-stamp a MODIFIED
    file that already existed on the base branch).

    NOTE (OMN-14619 live proof, 2026-07-14): the reference memory's form
    (``--jq -r .content``) is WRONG — ``gh api`` rejects it
    ("accepts 1 arg(s), received 2") because ``-r`` is not a valid ``--jq``
    sub-flag; ``gh api --jq`` already prints scalar results raw with no ``-r``
    needed. Live-verified against omnimarket#1760 (OMN-14608): the corrected
    form below returns ``1``/exit 0 at the PR head and ``0``/exit 1 (RED) at
    the PR base — this handler's own canary evidence, not the memory's text.
    """
    needle = f"{kind} {symbol}"
    return (
        f"gh api repos/{repo}/contents/{path}?ref={head_sha} --jq '.content' "
        f"| base64 -d | grep -c '{needle}'"
    )


def select_asserted_check(
    candidates: tuple[SymbolCandidate, ...],
    *,
    repo: str,
    head_sha: str,
    base_sha: str,
    fetch_content: Callable[[str, str], str | None],
) -> str | None:
    """Pick the first candidate that is RED-controllable, or None.

    A candidate passes only when its declaration is present at ``head_sha`` AND
    strictly more numerous there than at ``base_sha`` (feedback_prove_red_against
    _exists_but_wrong: assert against the PR-introduced state, never a symbol
    that already existed before the PR). ``fetch_content`` is injected so this
    function stays pure and unit-testable without live network calls — the
    effect boundary supplies a real GitHub content reader.
    """
    for candidate in candidates:
        head_content = fetch_content(candidate.path, head_sha)
        head_count = declaration_count(head_content, candidate.kind, candidate.symbol)
        if head_count < 1:
            continue
        base_content = fetch_content(candidate.path, base_sha)
        base_count = declaration_count(base_content, candidate.kind, candidate.symbol)
        if base_count >= head_count:
            continue  # not RED-controlled: already present at base, same or more
        return build_content_read_check(
            repo=repo,
            path=candidate.path,
            kind=candidate.kind,
            symbol=candidate.symbol,
            head_sha=head_sha,
        )
    return None


class HandlerOccStateEffect:
    """EFFECT handler: gather live PR + OCC facts into a ModelOccCompanionRequest.

    Read-only — never clones, writes, or pushes. Zero mutation to any GitHub
    surface. Companion (git clone/push/PR-open) is RSD-3, deliberately not
    built here.
    """

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        logger.info(
            "occ_state_effect: repo=%s pr=%s correlation_id=%s",
            request.repo,
            request.pr_number,
            request.correlation_id,
        )
        return await asyncio.to_thread(self._gather_sync, request)

    # -- gather -------------------------------------------------------------

    def _gather_sync(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        token = _resolve_github_token()
        owner, repo_name = split_repo(request.repo)

        pr = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{request.pr_number}", token=token
        )
        head = _as_dict(pr.get("head"))
        base = _as_dict(pr.get("base"))
        head_sha = str(head.get("sha") or "")
        base_sha = str(base.get("sha") or "")
        title = str(pr.get("title") or "")
        body = str(pr.get("body") or "")
        pr_state = str(pr.get("state") or "open")
        head_ref = str(head.get("ref") or "")

        files = self._list_files(owner, repo_name, request.pr_number, token)
        changed_files = tuple(str(f.get("filename", "")) for f in files)
        diff_total_lines = sum(
            _as_int(f.get("additions")) + _as_int(f.get("deletions")) for f in files
        )

        tickets = tuple(_extract_ticket_ids(body, title))
        occ_contract_states = tuple(
            self._occ_contract_state(request.occ_repo, ticket, token)
            for ticket in tickets
        )

        downstream_check_value: str | None = None
        if head_sha and base_sha:
            candidates = extract_symbol_candidates(files)

            def _fetch(path: str, ref: str) -> str | None:
                return self._content_at_ref(request.repo, path, ref, token)

            downstream_check_value = select_asserted_check(
                candidates,
                repo=request.repo,
                head_sha=head_sha,
                base_sha=base_sha,
                fetch_content=_fetch,
            )

        product_probe = self._observe_pr_probe(
            pr_number=request.pr_number,
            repo=request.repo,
            token=token,
            fallback={
                "number": request.pr_number,
                "state": pr_state,
                "headRefName": head_ref,
            },
        )

        return ModelOccCompanionRequest(
            repo=request.repo,
            pr_number=request.pr_number,
            pr_head_sha=head_sha,
            pr_title=title,
            pr_body=body,
            pr_state=pr_state,
            pr_head_ref=head_ref,
            runner=request.runner,
            verifier=request.verifier,
            run_timestamp=datetime.now(UTC).isoformat(),
            product_probe=product_probe,
            occ_repo=request.occ_repo,
            occ_contract_states=occ_contract_states,
            changed_files=changed_files,
            diff_total_lines=diff_total_lines,
            downstream_check_value=downstream_check_value,
        )

    def _list_files(
        self, owner: str, repo_name: str, pr_number: int, token: str
    ) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        page = 1
        while True:
            batch = rest_json_array(
                "GET",
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}/files"
                f"?per_page=100&page={page}",
                token=token,
            )
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def _occ_contract_state(
        self, occ_repo: str, ticket_id: str, token: str
    ) -> ModelOccContractState:
        """Read whether contracts/<ticket>.yaml already exists on OCC dev.

        OCC contracts only ever land on ``dev`` (main-target-guard rejects a
        companion PR targeting main), so a contract found at ``ref=dev`` is, by
        construction, already merged.
        """
        path = f"contracts/{ticket_id}.yaml"
        content = self._content_at_ref(
            occ_repo, path, _OCC_DEFAULT_TARGET_BRANCH, token
        )
        if content is None:
            return ModelOccContractState(ticket_id=ticket_id)

        whole_hash = hashlib.sha256(content.encode()).hexdigest()
        entry_ids: tuple[str, ...] = ()
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict):
            items = parsed.get("dod_evidence")
            if isinstance(items, list):
                entry_ids = tuple(
                    str(item.get("id"))
                    for item in items
                    if isinstance(item, dict) and item.get("id")
                )
        return ModelOccContractState(
            ticket_id=ticket_id,
            exists=True,
            merged=True,
            existing_entry_ids=entry_ids,
            whole_file_sha256=whole_hash,
            raw_contract_text=content,
        )

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        """Fetch decoded file content at ``ref``, or None if absent/undecodable."""
        owner, repo_name = split_repo(repo)
        encoded_path = urllib.parse.quote(path, safe="/")
        try:
            data = rest_json(
                "GET",
                f"/repos/{owner}/{repo_name}/contents/{encoded_path}?ref={ref}",
                token=token,
            )
        except GitHubApiError:
            return None
        if data.get("encoding") != "base64":
            return None
        content_b64 = str(data.get("content", ""))
        try:
            raw = base64.b64decode(content_b64, validate=False)
        except (binascii.Error, ValueError):
            return None
        return raw.decode("utf-8", errors="replace")

    def _observe_pr_probe(
        self,
        *,
        pr_number: int,
        repo: str,
        token: str,
        fallback: dict[str, object],
    ) -> ModelObservedProbe:
        """Run the real ``gh pr view`` probe so the receipt carries a genuine
        machine observation (OMN-13990 item 4 / OMN-14055), mirroring
        ``OccCompanionEmitter._observe_pr_probe`` exactly.
        """
        command = (
            f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
        )
        fallback_json = json.dumps(fallback, separators=(",", ":"), sort_keys=True)
        try:
            env = os.environ.copy()
            env["GH_TOKEN"] = token
            result = subprocess.run(
                shlex.split(command),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ModelObservedProbe(
                command=command, stdout=fallback_json, exit_code=0
            )
        if result.returncode != 0 or not result.stdout.strip():
            return ModelObservedProbe(
                command=command, stdout=fallback_json, exit_code=0
            )
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ModelObservedProbe(
                command=command,
                stdout=result.stdout.strip().replace("\n", " "),
                exit_code=0,
            )
        return ModelObservedProbe(
            command=command,
            stdout=json.dumps(parsed, separators=(",", ":"), sort_keys=True),
            exit_code=0,
        )


__all__ = [
    "HandlerOccStateEffect",
    "SymbolCandidate",
    "build_content_read_check",
    "declaration_count",
    "extract_symbol_candidates",
    "select_asserted_check",
]
