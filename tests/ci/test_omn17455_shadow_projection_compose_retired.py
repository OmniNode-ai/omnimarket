"""The retired, ungoverned projection compose lane must not return (OMN-17455 AC5).

``docker-compose.projection.yml`` (OMN-7610) declared seven standalone
projection writers on compose project ``omnimarket``, entirely outside the
governed dev-lane overlay: not in the lane census, not rebuilt by any
governed refresh, invisible to every lane-boundary attestation. Its only
live instance on .201 (``omnimarket-projection-registration``) was a shadow
copy of the same handler the governed ``projection-registration-writer``
now runs in the dev lane. This test guards against the file, or a live
pointer to it, coming back.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ungoverned_projection_compose_file_is_absent() -> None:
    """The shadow compose file itself must not exist."""
    assert not (REPO_ROOT / "docker-compose.projection.yml").exists()


def test_no_live_reference_to_the_retired_compose_file() -> None:
    """No tracked file may point at the retired compose file as if it still
    exists. Historical mentions that explicitly say it was retired (this
    test, the OMN-16146 migration comment, the OMN-14513 seam-test docstring)
    are allowed; a bare, undated ``docker-compose.projection.yml`` mention
    elsewhere means something still assumes the shadow lane is live.
    """
    result = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-F",
            "docker-compose.projection.yml",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    hits = {line for line in result.stdout.splitlines() if line}
    allowed = {
        "tests/ci/test_omn17455_shadow_projection_compose_retired.py",
        "tests/test_omn14513_baselines_seam.py",
        "src/omnimarket/nodes/node_projection_registration/migrations/"
        "0005_create_projection_watermarks.sql",
    }
    assert hits <= allowed, (
        f"unexpected live reference(s) to the retired compose file: {hits - allowed}"
    )
