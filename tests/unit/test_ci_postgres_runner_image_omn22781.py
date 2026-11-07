# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A job that installs Postgres 16 from apt runs on the image whose archive carries it (OMN-22802).

At about 22:11Z on 2026-11-06 the hosted ``ubuntu-latest`` image became Ubuntu 26.04 (apt suite ``resolute``). Its archive
has no ``postgresql-16``, so the step that installs it failed with ``E: Unable to locate package postgresql-16`` (exit
100) in all 20 full-suite shards and in the integration guard, and nothing that runs the whole tree could land. The install
was written for the 24.04 image, so each job that holds it names that image instead of a moving label.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
INSTALL = "postgresql-16"
IMAGE = "ubuntu-24.04"


def installing_jobs() -> list[tuple[str, str, Any]]:
    """Every (workflow file, job key, runs-on) whose steps apt-install Postgres 16."""
    found: list[tuple[str, str, Any]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key, job in (document.get("jobs") or {}).items():
            if any(
                INSTALL in str(step.get("run", "")) for step in job.get("steps", [])
            ):
                found.append((path.name, key, job.get("runs-on")))
    return found


def test_the_jobs_that_install_postgres_16_are_the_three_this_change_pins() -> None:
    # A job that starts to install it is a job this test has to be told about, not one it skips.
    assert sorted((f, k) for f, k, _ in installing_jobs()) == [
        ("ci.yml", "integration-guard"),
        ("ci.yml", "test"),
        ("nightly-full-suite.yml", "shard"),
    ]


def test_every_job_that_installs_postgres_16_names_the_24_04_image() -> None:
    assert sorted((f, k, image) for f, k, image in installing_jobs()) == [
        ("ci.yml", "integration-guard", IMAGE),
        ("ci.yml", "test", IMAGE),
        ("nightly-full-suite.yml", "shard", IMAGE),
    ]
