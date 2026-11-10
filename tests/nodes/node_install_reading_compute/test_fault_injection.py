# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-22956): the reading computes from the install canary and flips to NOT WORKING when the drill
deletes a root data file on a test host.

The probes here are the install canary's own: ``HandlerInstallCanary`` runs against a scratch git clone and a
host copy, and its ``ModelInstallProbe`` is mapped field for field onto the reading's fact.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_install_canary_effect.handlers.handler_install_canary import (
    HandlerInstallCanary,
    ModelInstallProbe,
)
from omnimarket.nodes.node_install_reading_compute.handlers.handler_install_reading import (
    HandlerInstallReading,
)
from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallReading,
)
from tests.nodes.node_install_reading_compute.install_support import (
    request,
)

pytestmark = pytest.mark.unit

PREFIX = "plugins/omni/skills"
ROOT_DATA = "skills/merge-drain/scripts/landing_floors.json"
TREE = {
    "plugins/omni/skills/merge-drain/SKILL.md": "# merge-drain\n",
    "plugins/omni/skills/merge-drain/scripts/landing_controller.py": "# controller\n",
    "plugins/omni/skills/merge-drain/scripts/landing_floors.json": '{"floor": 1}\n',
    "plugins/omni/scripts/landing_policy.py": "# policy\n",
}
HOSTS = ("live-host", "test-host")


def _git(repo: Path, *args: str) -> None:
    identity = {
        "GIT_AUTHOR_NAME": "lane",
        "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane",
        "GIT_COMMITTER_EMAIL": "lane@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**scrub_git_location_env(os.environ), **identity},
    )


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for rel, body in TREE.items():
        _write(repo, rel, body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


@pytest.fixture
def host_copy(tmp_path: Path) -> Path:
    root = tmp_path / "host-copy"
    for rel, body in TREE.items():
        _write(root, rel.removeprefix("plugins/omni/"), body)
    return root


def _canary() -> HandlerInstallCanary:
    return HandlerInstallCanary({"refresh_clone": lambda _path, _ref: None})


def _fact(host: str, probe: ModelInstallProbe) -> dict[str, Any]:
    """The canary's probe, field for field, plus the host and the time the caller read it."""
    return {**asdict(probe), "host": host, "probed_at": "2026-11-09T11:00:00Z"}


def _read(facts: list[dict[str, Any]]) -> ModelInstallReading:
    return HandlerInstallReading().handle(
        request(required_hosts=list(HOSTS), probes=facts)
    )


def test_fault_injection_a_daily_probe_of_a_whole_install_reads_working(
    clone: Path, host_copy: Path
) -> None:
    daily = _canary().daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    assert daily.ok
    reading = _read([_fact(host, daily) for host in HOSTS])
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()


def test_fault_injection_deleting_a_root_data_file_on_a_test_host_flips_the_reading(
    clone: Path, host_copy: Path
) -> None:
    canary = _canary()
    daily = canary.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    drill = canary.drill_install_probe(
        clone, "origin/main", host_copy, PREFIX, delete=ROOT_DATA
    )
    assert drill.missing == (ROOT_DATA,)

    before = _read([_fact("live-host", daily), _fact("test-host", daily)])
    after = _read([_fact("live-host", daily), _fact("test-host", drill)])

    assert before.verdict is V.WORKING
    assert after.verdict is V.NOT_WORKING
    assert after.reasons == (f"install-missing:test-host:1:{ROOT_DATA}",)
    assert (after.hosts_checked, after.hosts_whole) == (2, 1)
    # The drill ran on a scratch copy: the host's own install is whole, and the next daily probe reads WORKING again.
    assert (host_copy / ROOT_DATA).is_file()
    daily_again = canary.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    assert _read([_fact(h, daily_again) for h in HOSTS]).verdict is V.WORKING


def test_fault_injection_a_drill_that_does_not_see_its_fault_reads_not_working(
    clone: Path, host_copy: Path
) -> None:
    """A blind probe: the drill claims it deleted a file and still reports nothing missing."""
    daily = _canary().daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    blind = {**_fact("test-host", daily), "trigger": "drill", "fault": ROOT_DATA}
    reading = _read([_fact("live-host", daily), blind])
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"drill-fault-unseen:test-host:{ROOT_DATA}",)


def test_fault_injection_a_stale_drill_reading_does_not_hold_the_flip(
    clone: Path, host_copy: Path
) -> None:
    """The reading is of the newest probe: a later daily probe of the test host replaces the drill."""
    canary = _canary()
    daily = canary.daily_install_probe(clone, "origin/main", host_copy, PREFIX)
    drill = canary.drill_install_probe(
        clone, "origin/main", host_copy, PREFIX, delete=ROOT_DATA
    )
    later = {**_fact("test-host", daily), "probed_at": "2026-11-09T11:30:00Z"}
    reading = _read([_fact("live-host", daily), _fact("test-host", drill), later])
    assert reading.verdict is V.WORKING
