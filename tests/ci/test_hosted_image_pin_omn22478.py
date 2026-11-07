# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A job that installs a versioned system package must not float its hosted image (OMN-22478).

On 2026-11-06 about 22:11Z GitHub's ``ubuntu-latest`` became Ubuntu 26.04, which
ships no ``postgresql-16`` package, and every full-suite shard that ran
``sudo apt-get install --yes postgresql-16`` failed. The jobs were built for
Ubuntu 24.04. The check refuses a floating hosted label (``ubuntu-latest``,
``macos-latest``, ``windows-latest`` and the bare family names) in a job whose
steps install a package named with a version, and it names file, job and label.

The fixtures below are the recorded incident and its neighbours; the last class
runs the check over this repository's real workflows.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.ci.check_lane_job_placement import check_hosted_image_pins

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _check(tmp_path: Path, body: str) -> list[str]:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text(textwrap.dedent(body), encoding="utf-8")
    return check_hosted_image_pins(tmp_path)


def _job(runs_on: str, run: str, extra: str = "") -> str:
    return (
        "name: fixture\n"
        "on: push\n"
        "jobs:\n"
        "  shard:\n"
        f"    runs-on: {runs_on}\n"
        f"{extra}"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: install\n"
        "        run: |\n" + "".join(f"          {line}\n" for line in run.split("\n"))
    )


class TestRecordedIncident:
    def test_floating_ubuntu_with_postgresql_16_is_refused(
        self, tmp_path: Path
    ) -> None:
        violations = _check(
            tmp_path,
            _job("ubuntu-latest", "sudo apt-get install -y postgresql-16"),
        )
        assert len(violations) == 1
        message = violations[0]
        assert ".github/workflows/fixture.yml" in message
        assert "'shard'" in message
        assert "ubuntu-latest" in message
        assert "postgresql-16" in message

    def test_same_job_pinned_to_ubuntu_2404_passes(self, tmp_path: Path) -> None:
        assert (
            _check(
                tmp_path,
                _job("ubuntu-24.04", "sudo apt-get install -y postgresql-16"),
            )
            == []
        )


class TestOtherFloatingShapes:
    @pytest.mark.parametrize(
        "label", ["ubuntu-latest", "macos-latest", "windows-latest", "ubuntu"]
    )
    def test_every_floating_alias_is_refused(self, tmp_path: Path, label: str) -> None:
        violations = _check(
            tmp_path, _job(label, "sudo apt-get install -y postgresql-16")
        )
        assert len(violations) == 1
        assert label in violations[0]

    def test_label_list_with_a_floating_member_is_refused(self, tmp_path: Path) -> None:
        violations = _check(
            tmp_path,
            _job('[ubuntu-latest, "x64"]', "sudo apt-get install -y postgresql-16"),
        )
        assert len(violations) == 1

    def test_expression_with_a_floating_fallback_is_refused(
        self, tmp_path: Path
    ) -> None:
        violations = _check(
            tmp_path,
            _job(
                "${{ fromJSON(vars.RUNS_ON_JSON || '[\"ubuntu-latest\"]') }}",
                "sudo apt-get install -y postgresql-16",
            ),
        )
        assert len(violations) == 1
        assert "ubuntu-latest" in violations[0]

    def test_matrix_runs_on_with_a_floating_value_is_refused(
        self, tmp_path: Path
    ) -> None:
        violations = _check(
            tmp_path,
            _job(
                "${{ matrix.os }}",
                "sudo apt-get install -y postgresql-16",
                extra=(
                    "    strategy:\n"
                    "      matrix:\n"
                    "        os: [ubuntu-24.04, ubuntu-latest]\n"
                ),
            ),
        )
        assert len(violations) == 1
        assert "ubuntu-latest" in violations[0]

    def test_matrix_value_with_a_step_guarded_to_another_leg_passes(
        self, tmp_path: Path
    ) -> None:
        body = (
            "name: fixture\n"
            "on: push\n"
            "jobs:\n"
            "  canary:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        os: [ubuntu-latest, macos-15-intel]\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    steps:\n"
            "      - if: matrix.os == 'macos-15-intel'\n"
            "        run: brew install openssl@3\n"
        )
        assert _check(tmp_path, body) == []

    def test_matrix_value_with_an_unguarded_versioned_install_is_refused(
        self, tmp_path: Path
    ) -> None:
        body = (
            "name: fixture\n"
            "on: push\n"
            "jobs:\n"
            "  canary:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        os: [macos-latest]\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    steps:\n"
            "      - run: brew install openssl@3\n"
        )
        violations = _check(tmp_path, body)
        assert len(violations) == 1
        assert "macos-latest" in violations[0]
        assert "openssl@3" in violations[0]

    @pytest.mark.parametrize(
        "run",
        [
            "sudo apt-get update && sudo apt-get install -y python3.12-venv",
            "sudo apt install gcc=4:13.2.0-7ubuntu1",
            "sudo add-apt-repository ppa:deadsnakes/ppa",
            "sudo apt-get install -y \\\n  libpq-dev \\\n  postgresql-client-16",
        ],
    )
    def test_other_versioned_installs_are_refused(
        self, tmp_path: Path, run: str
    ) -> None:
        violations = _check(tmp_path, _job("ubuntu-latest", run))
        assert len(violations) == 1, run


class TestPassingShapes:
    def test_floating_job_installing_only_unversioned_packages_passes(
        self, tmp_path: Path
    ) -> None:
        run = "sudo apt-get install -y --no-install-recommends libpq-dev build-essential jq"
        assert _check(tmp_path, _job("ubuntu-latest", run)) == []

    def test_option_values_are_not_package_versions(self, tmp_path: Path) -> None:
        run = "sudo apt-get -o Acquire::Retries=5 install -y -qq postgresql-client"
        assert _check(tmp_path, _job("ubuntu-latest", run)) == []

    def test_floating_job_with_no_install_passes(self, tmp_path: Path) -> None:
        assert _check(tmp_path, _job("ubuntu-latest", "make test")) == []

    def test_self_hosted_label_passes(self, tmp_path: Path) -> None:
        assert (
            _check(
                tmp_path,
                _job(
                    '[self-hosted, omnibase-ci, "arch-amd64"]',
                    "sudo apt-get install -y postgresql-16",
                ),
            )
            == []
        )

    def test_reusable_workflow_call_job_has_no_runs_on_and_passes(
        self, tmp_path: Path
    ) -> None:
        body = (
            "name: fixture\n"
            "on: push\n"
            "jobs:\n"
            "  call:\n"
            "    uses: OmniNode-ai/omnibase_core/.github/workflows/x.yml@v1\n"
        )
        assert _check(tmp_path, body) == []

    def test_unparseable_workflow_is_a_violation_not_a_pass(
        self, tmp_path: Path
    ) -> None:
        violations = _check(tmp_path, "jobs: [unclosed\n")
        assert len(violations) == 1
        assert "fixture.yml" in violations[0]


class TestThisRepository:
    def test_no_job_floats_its_hosted_image_while_installing_a_versioned_package(
        self,
    ) -> None:
        assert check_hosted_image_pins(REPO_ROOT) == []
