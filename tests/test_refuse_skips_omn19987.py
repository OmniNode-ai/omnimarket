# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``--refuse-skips`` turns a run in which a test did not execute into a failure (OMN-19987).

An evidence check that runs a database proof passes today when the database is
absent: every test skips and pytest exits 0. A check that asks for
``--refuse-skips`` cannot pass that way.

Failure modes enumerated before the option existed, each with a case below:

* every test skips (no database) and the run exits 0;
* one test skips among tests that pass and the run exits 0;
* a whole module skips at import beside a module that passes, and the run exits 0;
* an expected failure is counted as proof;
* the option hides a real failure, or changes a clean run;
* the option changes any run that did not ask for it;
* the refusal does not say which test did not run, or why.

Each case runs pytest in a child process on a file under ``tmp_path``, with an
empty configuration file, so nothing in this repository's own test setup
decides the outcome.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_ALL_SKIP = """
import pytest

def test_a():
    pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")

def test_b():
    pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")
"""

_ONE_SKIP = """
import pytest

def test_passes():
    assert True

def test_skips():
    pytest.skip("no reachable Postgres")
"""

_MODULE_SKIP = """
import pytest

pytest.skip("module needs a database", allow_module_level=True)

def test_never_collected():
    assert True
"""

_XFAIL = """
import pytest

def test_passes():
    assert True

@pytest.mark.xfail(reason="known gap")
def test_expected_failure():
    assert False
"""

_ALL_PASS = """
def test_a():
    assert True

def test_b():
    assert True
"""

_ONE_FAILS = """
def test_passes():
    assert True

def test_fails():
    assert False
"""


def _run(
    tmp_path: Path, source: str, *options: str
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_case.py").write_text(source, encoding="utf-8")
    # A second module that always passes: a module skipped at import is then
    # the only thing wrong with the run, as a skipped proof is beside green tests.
    (tmp_path / "test_other_module.py").write_text(
        "def test_other_module_passes():\n    assert True\n", encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "tests.refuse_skips",
            "-p",
            "no:cacheprovider",
            "-q",
            *options,
            str(tmp_path),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("source", "named"),
    [
        (_ALL_SKIP, "INTEGRATION_POSTGRES_PASSWORD not set"),
        (_ONE_SKIP, "no reachable Postgres"),
        (_MODULE_SKIP, "module needs a database"),
        (_XFAIL, "test_expected_failure"),
    ],
    ids=["every_test_skips", "one_test_skips", "module_skips", "expected_failure"],
)
def test_a_run_in_which_a_test_did_not_prove_anything_is_refused(
    tmp_path: Path, source: str, named: str
) -> None:
    refused = _run(tmp_path, source, "--refuse-skips")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "refuse-skips" in refused.stdout
    assert named in refused.stdout


@pytest.mark.parametrize(
    "source",
    [_ALL_SKIP, _ONE_SKIP, _MODULE_SKIP, _XFAIL],
    ids=["every_test_skips", "one_test_skips", "module_skips", "expected_failure"],
)
def test_the_same_run_without_the_option_is_unchanged(
    tmp_path: Path, source: str
) -> None:
    # The control: this is the exit 0 the option exists to refuse.
    plain = _run(tmp_path, source)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "refuse-skips" not in plain.stdout


def test_a_run_in_which_every_test_passes_still_passes(tmp_path: Path) -> None:
    clean = _run(tmp_path, _ALL_PASS, "--refuse-skips")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "refuse-skips" not in clean.stdout


def test_a_real_failure_stays_a_failure(tmp_path: Path) -> None:
    failed = _run(tmp_path, _ONE_FAILS, "--refuse-skips")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "test_fails" in failed.stdout


def test_a_run_that_selects_no_test_does_not_pass(tmp_path: Path) -> None:
    empty = _run(tmp_path, _ALL_PASS, "--refuse-skips", "-k", "no_such_test")
    assert empty.returncode != 0, empty.stdout + empty.stderr


def test_the_shared_conftest_offers_the_option(request: pytest.FixtureRequest) -> None:
    # The bound check runs plain ``uv run pytest ... --refuse-skips`` with no
    # ``-p``: the option has to come from this repository's own test setup.
    # This session is such a run, so its own configuration must know the option.
    from tests import conftest, refuse_skips

    assert request.config.getoption("refuse_skips") is False
    assert conftest.pytest_addoption is refuse_skips.pytest_addoption
    assert conftest.pytest_sessionfinish is refuse_skips.pytest_sessionfinish
    assert conftest.pytest_terminal_summary is refuse_skips.pytest_terminal_summary
