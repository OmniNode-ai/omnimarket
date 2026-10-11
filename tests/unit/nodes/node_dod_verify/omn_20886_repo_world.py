# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20886 -- a registry root of product clones and a PR watcher state.

Shared by the repo-first lookup suite and by the recording of its
onex_change_control-only replay fixture, so it imports nothing the base commit
lacks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    DodEvidenceLocalSource,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_19428_occ_tree import (
    MODEL_RELPATH,
    REAL_MODEL_FIXTURE,
)

CHECK = "repo-evidence / dod-verify"
_FIXED_DATE = "2026-10-01T00:00:00+00:00"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        # Fixed dates keep the commit ids, and so the recorded replay, stable.
        env=scrub_git_location_env(
            {
                **os.environ,
                "GIT_AUTHOR_DATE": _FIXED_DATE,
                "GIT_COMMITTER_DATE": _FIXED_DATE,
            }
        ),
    ).stdout.strip()


def contract(ticket_id: str, item_id: str, binds: list[str]) -> str:
    return yaml.safe_dump(
        {
            "schema_version": "1.0.0",
            "ticket_id": ticket_id,
            "dod_evidence": [
                {
                    "id": item_id,
                    "description": "the bound behaviour",
                    "source": "manual",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                    "binds_ac": binds,
                }
            ],
        },
        sort_keys=False,
    )


class World:
    """A registry root of clones and the PR watcher state over them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = root / "registry"
        self.registry.mkdir()
        self.state: dict[str, Any] = {
            "schema": 1,
            "operator": "op",
            "last_tick": "2026-10-10T00:00:00Z",
            "last_full_resync": "2026-10-10T00:00:00Z",
            "repos": {},
            "prs": {},
            "merges": {},
        }
        self.check_runs: dict[tuple[str, str], list[dict[str, object]]] = {}
        self._hour = 0

    def clone(self, name: str) -> Path:
        clone = self.registry / name
        if not clone.exists():
            clone.mkdir()
            _git(clone, "init", "-q", "-b", "dev")
            _git(
                clone,
                "remote",
                "add",
                "origin",
                f"https://github.com/OmniNode-ai/{name}.git",
            )
            (clone / "README.md").write_text("base\n")
            _git(clone, "add", ".")
            _git(clone, "commit", "-q", "-m", "chore: base (#1)")
            _git(clone, "update-ref", "refs/remotes/origin/dev", "HEAD")
            self.state["repos"][name] = {"default_branch": "dev"}
        return clone

    def merge(
        self,
        repo: str,
        number: int,
        title: str,
        files: dict[str, str],
        *,
        run: str | None = "success",
    ) -> dict[str, str]:
        """Squash-merge PR ``number`` into ``repo``'s dev; ``run`` is the
        conclusion of its head's repo-evidence run (None: no run)."""
        clone = self.clone(repo)
        for rel, text in files.items():
            path = clone / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", f"{title} (#{number})")
        merge_sha = _git(clone, "rev-parse", "HEAD")
        _git(clone, "update-ref", "refs/remotes/origin/dev", merge_sha)
        self._hour += 1
        # The PR's own head: the merged tree on the pre-merge parent, an object
        # the clone holds but no branch names (as after a squash merge).
        head_sha = _git(
            clone,
            "commit-tree",
            f"{merge_sha}^{{tree}}",
            "-p",
            f"{merge_sha}^",
            "-m",
            f"head of #{number}",
        )
        self.state["merges"][f"{repo}#{number}"] = {
            "repo": repo,
            "number": number,
            "title": title,
            "head_ref": f"branch-{number}",
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "merged_at": f"2026-10-0{1 + self._hour // 24}T{self._hour % 24:02d}:00:00Z",
            "base": "dev",
        }
        if run is not None:
            self.check_runs[(f"OmniNode-ai/{repo}", head_sha)] = [
                {
                    "id": 1000 + number,
                    "name": CHECK,
                    "app_slug": "github-actions",
                    "status": "completed",
                    "conclusion": run,
                }
            ]
        return {"merge_sha": merge_sha, "head_sha": head_sha}

    def occ(self, contracts: dict[str, str]) -> Path:
        """``onex_change_control`` with ``contracts`` on a bare-upstream ``dev``."""
        upstream = self.root / "occ-upstream.git"
        upstream.mkdir()
        _git(upstream, "init", "-q", "--bare", "--initial-branch=dev")
        occ = self.registry / "onex_change_control"
        occ.mkdir()
        _git(occ, "init", "-q", "-b", "dev")
        model = occ / MODEL_RELPATH
        model.parent.mkdir(parents=True)
        shutil.copyfile(REAL_MODEL_FIXTURE, model)
        (occ / "contracts").mkdir()
        for ticket_id, text in contracts.items():
            (occ / "contracts" / f"{ticket_id}.yaml").write_text(text)
        _git(occ, "add", ".")
        _git(occ, "commit", "-q", "-m", "contracts")
        _git(occ, "remote", "add", "origin", str(upstream))
        _git(occ, "push", "-q", "-u", "origin", "dev")
        return occ

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state_path = self.root / "state.json"
        state_path.write_text(json.dumps(self.state))
        monkeypatch.setenv("OMNI_HOME", str(self.registry))
        monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(state_path))
        monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
        monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
        monkeypatch.setattr(hd_mod, "_default_local_source", DodEvidenceLocalSource)

        def no_gh(argv: list[str], timeout_s: int) -> tuple[object | None, str]:
            raise AssertionError(f"unexpected GitHub read: {argv}")

        def check_runs(
            argv: list[str], timeout_s: int
        ) -> tuple[list[dict[str, object]] | None, str]:
            endpoint = argv[3]
            assert "/check-runs?" in endpoint, argv
            repo = endpoint.removeprefix("repos/").split("/commits/")[0]
            sha = endpoint.split("/commits/")[1].split("/")[0]
            rows = self.check_runs.get((repo, sha))
            return (
                (None, "HTTP 502")
                if rows == [{"unreadable": True}]
                else (rows or [], "")
            )

        monkeypatch.setattr(hd_mod, "_gh_json", no_gh)
        monkeypatch.setattr(hd_mod, "_gh_json_lines", check_runs)


def normalise(text: str | None, world: World) -> str:
    """A message without the run's temporary root or its check timings."""
    return re.sub(
        r"\(\d+ms\)", "(<ms>)", (text or "").replace(str(world.root), "<TMP>")
    )


def snapshot(
    collector: EvidenceCollector, results: list[ModelEvidenceCheckResult], world: World
) -> dict[str, object]:
    subject = collector.contract_subject
    return {
        "results": [
            [r.evidence_id, r.status.value, normalise(r.message, world)]
            for r in results
        ],
        "subject": None
        if subject is None
        else [subject.source.value, subject.repository, subject.repo_path],
    }


def occ_only_world(world: World, case: str) -> str:
    ticket = "OMN-90203"
    if case == "product_prs_without_contract":
        world.merge(
            "omniclaude",
            2573,
            f"fix({ticket}): claim identity",
            {"src/claim.py": "x = 1\n"},
        )
    elif case == "product_contract_without_repo_evidence_run":
        world.merge(
            "omnimarket",
            71,
            f"feat({ticket}): carries a contract",
            {f"contracts/{ticket}.yaml": contract(ticket, "dod-market", ["AC1"])},
            run=None,
        )
    world.occ({ticket: contract(ticket, "dod-occ", ["AC1"])})
    return ticket
