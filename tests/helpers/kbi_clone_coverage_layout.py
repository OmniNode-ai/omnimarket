# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real git layouts for the declared-clone refresh tests (OMN-22149), built from the recorded fixture."""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    ModelTransportResult,
    TransportUnreachableError,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "kbi_clone_coverage"
    / "h101_layout.yaml"
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**scrub_git_location_env(os.environ), **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class HomesTransport:
    """Runs a host's command on this machine with that host's own HOME: the path forms of the
    table (`$HOME/...`) resolve per host, exactly as they do over ssh."""

    def __init__(
        self, homes: dict[str, Path], unreachable: frozenset[str] = frozenset()
    ):
        self.homes = homes
        self.unreachable = unreachable

    def run(
        self, host: ModelLabHostRow, argv: Sequence[str], timeout_seconds: float
    ) -> ModelTransportResult:
        if host.name in self.unreachable:
            raise TransportUnreachableError(f"{host.name}: no route")
        done = subprocess.run(
            list(argv),
            env={
                **scrub_git_location_env(os.environ),
                **_GIT_ENV,
                "HOME": str(self.homes[host.name]),
            },
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return ModelTransportResult(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


@dataclass
class Layout:
    spec: dict[str, Any]
    origin: Path
    homes: dict[str, Path]
    table: ModelLabHostsTable
    origin_tip: str
    start: dict[str, str] = field(default_factory=dict)

    @property
    def repo_dir(self) -> str:
        return str(self.spec["repo_dir"])

    def clone_path(self, host: str) -> Path:
        return self.homes[host] / str(self.spec["hosts"][host]["clone_rel"])

    def head(self, host: str) -> str:
        return git(self.clone_path(host), "rev-parse", "HEAD")

    def scan_based_refresh(self, host: str) -> list[Path]:
        """The scan-based behaviour: fast-forward each clone found directly under a registry root."""
        moved: list[Path] = []
        for root in self.spec["registry_roots"]:
            candidate = self.homes[host] / root / self.repo_dir
            if (candidate / ".git").is_dir():
                git(candidate, "pull", "--ff-only", "--quiet")
                moved.append(candidate)
        return moved


def build_layout(tmp_path: Path) -> Layout:
    spec = yaml.safe_load(FIXTURE.read_text())
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "-b", spec["ref"])
    commits: list[str] = []
    for n in range(4):
        (seed / "f.txt").write_text(f"{n}\n")
        git(seed, "add", "f.txt")
        git(seed, "commit", "--quiet", "-m", f"c{n}")
        commits.append(git(seed, "rev-parse", "HEAD"))
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(origin))
    homes: dict[str, Path] = {}
    rows = []
    start: dict[str, str] = {}
    for host, row in spec["hosts"].items():
        home = tmp_path / "homes" / host
        (home / spec["registry_roots"][0]).mkdir(parents=True)
        homes[host] = home
        clone = home / row["clone_rel"]
        clone.parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "clone", "--quiet", str(origin), str(clone))
        git(clone, "reset", "--quiet", "--hard", commits[3 - row["behind"]])
        start[host] = commits[3 - row["behind"]]
        rows.append(
            ModelLabHostRow.model_validate(
                {
                    "name": host,
                    "target": f"{host}.invalid",
                    "clones": [
                        {
                            "repo": spec["origin_slug"],
                            "ref": spec["ref"],
                            "path": f"$HOME/{row['clone_rel']}",
                        }
                    ],
                }
            )
        )
    return Layout(
        spec=spec,
        origin=origin,
        homes=homes,
        table=ModelLabHostsTable(hosts=tuple(rows)),
        origin_tip=commits[3],
        start=start,
    )


async def assert_node_covers_the_clone_outside_the_scan(
    bus: Any, layout: Layout
) -> None:
    """The one test body both bus profiles run: scan-based behaviour leaves the h101 clone behind,
    the node (request over the bus, receipt over the bus) brings every declared clone to origin."""
    from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.adapter_declared_clone_refresh_host import (
        run_declared_refresh,
    )
    from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
        HandlerDeclaredCloneRefresh,
    )
    from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
        EnumDeclaredCloneState,
        ModelDeclaredCloneRefreshRequest,
    )

    assert layout.scan_based_refresh("h101") == []
    assert layout.head("h101") != layout.origin_tip

    handler = HandlerDeclaredCloneRefresh(
        layout.table, layout.repo_dir, HomesTransport(layout.homes)
    )
    receipt = await run_declared_refresh(
        bus,
        handler,
        ModelDeclaredCloneRefreshRequest(hosts=("h051", "h053", "h101")),
        group_name="test-host",
        wait_seconds=60.0,
    )
    assert not receipt.refused
    states = {o.host: o.state for o in receipt.outcomes}
    assert states == {
        "h051": EnumDeclaredCloneState.UP_TO_DATE,
        "h053": EnumDeclaredCloneState.REFRESHED,
        "h101": EnumDeclaredCloneState.REFRESHED,
    }
    for outcome in receipt.outcomes:
        assert outcome.after_sha == outcome.origin_sha == layout.origin_tip
        assert layout.head(outcome.host) == layout.origin_tip
