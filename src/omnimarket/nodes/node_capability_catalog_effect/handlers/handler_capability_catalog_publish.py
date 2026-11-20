# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capability-catalog timer, the local state files and the knowledge-base publish.

**What one run does, in order:**

1. Collect the lab inventory and the host-local tools (tools through the catalog node's command bus);
   combine both catalogs without dropping either section.
2. Render every view into ``$ONEX_STATE_DIR/capability/`` -- ``catalog.json``, ``digest.txt``,
   ``brief-part.md``, ``capability-catalog.md`` -- each written atomically (temp file then
   ``os.replace``), so a reader never sees a half-written file.
3. Compare the new catalog's :func:`structural_hash` (hosts, lanes, engines, jobs -- never a
   timestamp, an exit code, a lane note or the order the collector returned them in) against the
   last one published. Unchanged: stop, no git or GitHub call
   (``test_publish.py -k no_change_no_call``).
4. Changed: publish with one fixed branch
   (``bot/capability-catalog``) rebuilt from ``origin/main`` on every run, in a dedicated
   worktree, force-pushed, and the one open PR against it refreshed or opened.

**A collector exception never clobbers a good state.** It is caught here, written to
``failure.json`` with the reason and a traceback, and every file written in step 2 above is left
exactly as the last successful run left it (``test_publish.py -k failure_keeps_last_good``).
A run that collects successfully clears any stale ``failure.json`` from an earlier run.

**Single writer.** This module does not itself check the hostname. The timer is installed only
on the declared orchestrator (``sources.yaml``'s ``orchestrator_host``), so at most one host ever
runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models.model_capability_catalog import (
    SCHEMA_ID,
    Catalog,
    Host,
    Lane,
    ModelServer,
)
from ..models.model_capability_catalog_publish_request import (
    ModelCapabilityCatalogPublishRequest,
)
from ..models.model_capability_catalog_publish_result import (
    ModelCapabilityCatalogPublishResult,
)
from ..models.model_capability_catalog_sources import Sources
from .handler_capability_catalog_cli import dispatch_tools
from .handler_capability_catalog_collect_lab import (
    collect_lab,
    load_sources,
)
from .handler_capability_catalog_render import (
    load_static_facts,
    render_brief_part,
    render_digest,
    render_full_markdown,
)

REPO_SLUG = "OmniNode-ai/docs_repo"
BASE = "main"
BRANCH = "bot/capability-catalog"
TARGET_REL = "reference/capability-catalog.md"
TICKET_ENV = "ONEX_CAPABILITY_CATALOG_PR_TICKET"
GENERATOR = (
    "src/omnimarket/nodes/node_capability_catalog_effect/handlers/"
    "handler_capability_catalog_publish.py"
)
WORKTREE_REL = "omni_worktrees/capability-catalog-publish/docs_repo"
#: The PR watcher state is read for the open PR; a state older than this is unreadable, not a
#: reason to read GitHub (lanes do not read GitHub, and the shim refuses the read).
PR_STATE_MAX_AGE = timedelta(minutes=8)
PR_STATE_REL = ".claude_scratch/pr-watcher/state.json"
#: The header the docs-repo checker reads. Its body-sha256 covers everything
#: after this header's own two lines.
HEADER_MARKER = "generated-by: onex-capability-catalog"


class PublishError(Exception):
    """A git or GitHub step failed. Exit 1."""


CollectFn = Callable[..., tuple[list[Host], list[Lane], list[ModelServer]]]
Clock = Callable[[], str]


def _default_clock() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def capability_state_dir(onex_state_dir: Path) -> Path:
    return onex_state_dir / "capability"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# Readings, not structure: a timestamp, an exit code, a lane's note (live container
# counts such as "28 containers, 23 up", or "ssh exit 124"), a launchd job's last exit code and a
# skill's last use. Each moves with a re-probe of the same reality, so none may reach the hash.
_VOLATILE_KEYS = frozenset(
    {
        "read_at",
        "exit_code",
        "generated_at",
        "note",
        "last_exit_code",
        "last_used_at",
        "delegation_runs",
        "ledger_usage",
    }
)


def _canonical(value: Any) -> Any:
    """The structure of `value`: readings dropped, a probe reduced to its state, every list in a
    fixed order, so two collections of the same reality hash the same."""
    if isinstance(value, dict):
        return {
            k: (
                {"state": v["state"]}
                if k == "probe" and isinstance(v, dict)
                else _canonical(v)
            )
            for k, v in value.items()
            if k not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return sorted(
            (_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
        )
    return value


def structural_hash(catalog: Catalog) -> str:
    payload = json.dumps(_canonical(catalog.to_dict()), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_with_header(body: str) -> str:
    """Wrap `body` with the header the docs-repo checker validates."""
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = f"<!-- {HEADER_MARKER}\n     body-sha256: {body_hash} -->\n"
    return header + body


# --------------------------------------------------------------------------
# git / GitHub


def scrubbed_git_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    ):
        env.pop(key, None)
    return env


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=scrubbed_git_env(),
    )
    if check and done.returncode != 0:
        raise PublishError(
            f"git {' '.join(args)} exited {done.returncode}: "
            f"{(done.stderr or done.stdout).strip()[:800]}"
        )
    return done


GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


@dataclass
class GitHub:
    run: GhRunner = _run_gh
    repo: str = REPO_SLUG

    def _api(self, *args: str) -> tuple[dict[str, str], str]:
        done = self.run(["api", "-i", *args])
        if done.returncode != 0:
            raise PublishError(
                f"gh api {' '.join(args)[:200]} failed: {done.stderr.strip()[:400]}"
            )
        head, _, body = done.stdout.replace("\r\n", "\n").partition("\n\n")
        headers: dict[str, str] = {}
        for line in head.splitlines()[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        return headers, body

    def create_pr(self, branch: str, title: str, body: str) -> int:
        _, out = self._api(
            "-X",
            "POST",
            f"repos/{self.repo}/pulls",
            "-f",
            f"title={title}",
            "-f",
            f"head={branch}",
            "-f",
            f"base={BASE}",
            "-f",
            f"body={body}",
        )
        return int(json.loads(out)["number"])

    def update_pr(self, number: int, title: str, body: str) -> None:
        self._api(
            "-X",
            "PATCH",
            f"repos/{self.repo}/pulls/{number}",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        )


def open_pr_number(
    state_path: Path, repo: str, branch: str, *, now: datetime | None = None
) -> int | None:
    """The open PR of ``branch`` in ``repo`` from the PR watcher's state, or None when it has none.

    A missing, unparseable or stale state raises: the publish stops and retries on the next tick
    rather than guess whether a PR is open (a wrong guess opens a duplicate).
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ticked = datetime.strptime(
            str(state["last_tick"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        records = state["prs"].values()
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise PublishError(
            f"PR watcher state {state_path} is unreadable: {exc}"
        ) from exc
    age = (now or datetime.now(UTC)) - ticked
    if age > PR_STATE_MAX_AGE:
        raise PublishError(
            f"PR watcher state {state_path} is stale: last tick {int(age.total_seconds())}s ago "
            f"(max {int(PR_STATE_MAX_AGE.total_seconds())}s)"
        )
    numbers = sorted(
        int(facts["number"])
        for record in records
        if isinstance(record, dict)
        and isinstance(facts := record.get("facts"), dict)
        and facts.get("repo") == repo
        and facts.get("head_ref") == branch
        and facts.get("state") == "OPEN"
    )
    return numbers[0] if numbers else None


def ensure_worktree(kb_repo: Path, worktree: Path) -> None:
    if (worktree / ".git").exists():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(kb_repo, "worktree", "prune")
    git(kb_repo, "worktree", "add", "--detach", str(worktree), f"origin/{BASE}")


def pr_text(new_hash: str) -> tuple[str, str]:
    ticket = os.environ.get(TICKET_ENV, "").strip()
    scope = f"docs({ticket})" if ticket else "docs"
    title = f"{scope}: regenerate the capability catalog"
    body = "\n".join(
        [
            f"Regenerated by the omnimarket capability catalog node `{GENERATOR}`.",
            "",
            f"Structural hash: `{new_hash}`.",
            "",
            f"`{TARGET_REL}` cannot be hand-edited: its header carries this body's "
            "sha256, and a docs-repo check fails any PR whose body does not "
            "match it. The branch is rebuilt from origin/main on every run, so a later run "
            "replaces this head with a newer snapshot.",
        ]
    )
    return title, body


# --------------------------------------------------------------------------
# The run


def run(
    *,
    registry_root: Path,
    onex_state_dir: Path,
    kb_repo: Path,
    sources_path: Path | None = None,
    worktree: Path | None = None,
    pr_state: Path | None = None,
    branch: str = BRANCH,
    dry_run: bool = False,
    push: bool = True,
    clock: Clock = _default_clock,
    collect: CollectFn = collect_lab,
    collect_tools: Callable[..., Catalog | None] | None = None,
    github: GitHub | None = None,
    log: Callable[[str], None] = print,
) -> int:
    out_dir = capability_state_dir(onex_state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = clock()

    sources: Sources = load_sources(sources_path)
    try:
        hosts, lanes, model_servers = collect(
            sources, registry_root=registry_root, now=now
        )
        catalog = Catalog(
            schema=SCHEMA_ID,
            generated_at=now,
            hosts=tuple(hosts),
            lanes=tuple(lanes),
            model_servers=tuple(model_servers),
        )
        if collect_tools is not None:
            tools_catalog = collect_tools(
                registry_root=registry_root, home=Path.home(), now=now
            )
            if tools_catalog is None:
                raise PublishError("tool collection did not return a terminal catalog")
            catalog = replace(
                tools_catalog,
                hosts=catalog.hosts,
                lanes=catalog.lanes,
                model_servers=catalog.model_servers,
            )
    except Exception as exc:
        _write_atomic(
            out_dir / "failure.json",
            json.dumps(
                {
                    "failed_at": now,
                    "reason": str(exc),
                    "traceback": traceback.format_exc(),
                },
                indent=2,
                sort_keys=True,
            ),
        )
        log(f"capability_catalog publish: FAILED to collect: {exc}")
        return 1

    static_facts = load_static_facts()
    new_hash = structural_hash(catalog)
    hash_path = out_dir / "last_published_hash.txt"
    previous_hash = (
        hash_path.read_text(encoding="utf-8").strip() if hash_path.is_file() else None
    )

    _write_atomic(
        out_dir / "catalog.json",
        json.dumps(catalog.to_dict(), indent=2, sort_keys=True),
    )
    _write_atomic(out_dir / "digest.txt", render_digest(catalog, static_facts))
    _write_atomic(out_dir / "brief-part.md", render_brief_part(catalog, static_facts))
    body = render_full_markdown(catalog, static_facts)
    rendered = render_with_header(body)
    _write_atomic(out_dir / "capability-catalog.md", rendered)
    failure_path = out_dir / "failure.json"
    if failure_path.is_file():
        failure_path.unlink()

    log(
        f"capability_catalog publish: hosts={len(hosts)} lanes={len(lanes)} "
        f"hash={new_hash[:12]} previous={previous_hash[:12] if previous_hash else 'none'}"
    )

    if new_hash == previous_hash:
        log("capability_catalog publish: unchanged structural hash, nothing to publish")
        return 0
    if dry_run:
        log(
            f"capability_catalog publish: would publish (hash {previous_hash} -> {new_hash})"
        )
        return 0

    worktree = worktree or registry_root / WORKTREE_REL
    git(kb_repo, "fetch", "--quiet", "origin", BASE)
    ref = f"origin/{BASE}"
    ensure_worktree(kb_repo, worktree)
    git(worktree, "checkout", "--quiet", "--force", "-B", branch, ref)
    target = worktree / TARGET_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    git(worktree, "add", "--", TARGET_REL)
    status = git(worktree, "status", "--porcelain", "--", TARGET_REL)
    if not status.stdout.strip():
        log(
            "capability_catalog publish: rendered file identical to origin/main HEAD; no commit"
        )
        _write_atomic(hash_path, new_hash)
        return 0
    title, pr_body = pr_text(new_hash)
    git(worktree, "commit", "--quiet", "-m", title, "-m", pr_body)
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    log(f"capability_catalog publish: committed {head[:12]} on {branch}")
    if not push:
        return 0

    gh = github or GitHub()
    number = open_pr_number(
        pr_state or registry_root / PR_STATE_REL, REPO_SLUG.split("/")[1], branch
    )
    git(worktree, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")
    if number is None:
        number = gh.create_pr(branch, title, pr_body)
        log(f"capability_catalog publish: opened {REPO_SLUG}#{number} head {head[:12]}")
    else:
        gh.update_pr(number, title, pr_body)
        log(
            f"capability_catalog publish: refreshed {REPO_SLUG}#{number} head {head[:12]}"
        )
    _write_atomic(hash_path, new_hash)
    return 0


class HandlerCapabilityCatalogPublish:
    """Collect, render and publish the catalog to the knowledge base when its structure changed."""

    def handle(
        self, request: ModelCapabilityCatalogPublishRequest
    ) -> ModelCapabilityCatalogPublishResult:
        return ModelCapabilityCatalogPublishResult(
            exit_code=run(
                registry_root=request.registry_root,
                onex_state_dir=request.onex_state_dir,
                kb_repo=request.kb_repo,
                sources_path=request.sources_path,
                worktree=request.worktree,
                pr_state=request.pr_state,
                branch=request.branch,
                dry_run=request.dry_run,
                push=request.push,
                collect_tools=dispatch_tools,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument(
        "--omni-home",
        dest="registry_root",
        metavar="OMNI_HOME",
        type=Path,
        default=None,
    )
    parser.add_argument("--kb-repo", type=Path, default=None, help="docs-repo clone")
    parser.add_argument("--onex-state-dir", type=Path, default=None)
    parser.add_argument("--sources", type=Path, default=None)
    parser.add_argument("--worktree", type=Path, default=None)
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)

    registry_root = (args.registry_root or Path(os.environ["OMNI_HOME"])).resolve()
    kb_repo = (args.kb_repo or registry_root / "docs_repo").resolve()
    onex_state_dir = (
        args.onex_state_dir or Path(os.environ["ONEX_STATE_DIR"])
    ).resolve()
    pr_state = (
        Path(os.environ["ONEX_PR_WATCHER_STATE"])
        if os.environ.get("ONEX_PR_WATCHER_STATE")
        else None
    )
    print(
        f"capability_catalog publish: run registry_root={registry_root} kb_repo={kb_repo}"
    )
    try:
        result = HandlerCapabilityCatalogPublish().handle(
            ModelCapabilityCatalogPublishRequest(
                registry_root=registry_root,
                onex_state_dir=onex_state_dir,
                kb_repo=kb_repo,
                sources_path=args.sources,
                worktree=args.worktree,
                pr_state=pr_state,
                branch=args.branch,
                dry_run=args.dry_run,
                push=not args.no_push,
            )
        )
        return result.exit_code
    except PublishError as exc:
        print(f"capability_catalog publish: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
