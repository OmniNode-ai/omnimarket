# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit proof for the PyPI pin-resolvability gate (OMN-18034).

WHAT FAILED, MEASURED 2026-09-07
--------------------------------
``.github/workflows/release-on-merge.yml`` (OMN-18010) made a release automatic.
It did not make it *verified*: ``uv build`` packages a wheel from local source
and never touches the index, and local dev/test resolution is short-circuited by
``[tool.uv.sources]`` git-rev overrides that plain ``pip`` never reads. So a
PyPI-facing pin can be unsatisfiable while every local signal stays green.

It was. Four consecutive automatic releases -- omnimarket 0.4.21, 0.4.22, 0.4.23
and 0.4.24 -- are ALL unresolvable from the real index::

    omnimarket>=0.4.21 requires omnibase-core>=0.47.5 and
    omnibase-infra>=0.38.19,<0.39.0; the only published omnibase-infra in that
    range is 0.38.19, which pins omnibase-core==0.47.4 exactly.

0.4.20 is the last release a downstream user can install. Automating the release
removed the human who would have noticed, which is precisely why the gate has to
be mechanical.

WHAT THIS MODULE PINS
---------------------
Every leg below is a distinct way the gate could report the wrong thing:

  * an unresolvable pin set exits 1 and says so;
  * a TIMEOUT also exits 1 but must NOT be reported as a broken pin -- it says
    nothing about the pins, only that the fleet was slow (OMN-16047). Conflating
    the two sends a release engineer to edit a correct pin;
  * ``dist/`` holding zero or two wheels is a failure, never a pass by vacuity;
  * a bad invocation exits 2, distinct from every substantive failure;
  * an index/network fault is a FAILURE. A gate that passes when it could not
    reach the index is worse than no gate: it reports proof it does not have.

The red-first leg is
:func:`test_the_recorded_published_triple_is_refused`. It replays a transcript
recorded from this exact script run against the REAL published
``omnimarket==0.4.24`` wheel (see the fixture header for the command and the
index heads at recording time) and asserts refusal -- so the gate is demonstrated
FAILING on live-broken input before it is demonstrated passing on good input.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.verify_pypi_pin_resolvability import (
    _INSTALL_TIMEOUT_ENV_VAR,
    PinResolveTimeoutError,
    find_single_wheel,
    main,
    verify_pin_resolvability,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECORDED_UNRESOLVABLE = FIXTURES / "omn_18034_recorded_unresolvable_0_4_24.txt"

_BEGIN = "---- BEGIN RECORDED uv OUTPUT ----"
_END = "---- END RECORDED uv OUTPUT ----"

#: Substrings the failure report must carry so a release engineer reading only
#: the run log can tell WHICH failure happened without opening the source.
_UNRESOLVABLE_REPORT = "do not resolve"
_TIMEOUT_REPORT = "THROUGHPUT failure"


def _recorded_uv_output() -> str:
    """The verbatim uv transcript from the fixture, without its prose header."""
    text = RECORDED_UNRESOLVABLE.read_text(encoding="utf-8")
    body = text.split(_BEGIN, 1)[1].split(_END, 1)[0]
    return body.strip("\n")


def _wheel(tmp_path: Path, name: str = "omnimarket-0.4.24-py3-none-any.whl") -> Path:
    """A dist/ directory holding one placeholder wheel.

    The gate never reads wheel BYTES -- it hands the path to ``uv pip install``
    -- so a placeholder is sufficient everywhere the subprocess is faked.
    """
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    wheel = dist / name
    wheel.write_bytes(b"not a real wheel")
    return wheel


class _FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess`` with only what is read."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    venv: _FakeCompleted | Exception,
    install: _FakeCompleted | Exception,
) -> None:
    """Drive both subprocess legs of the gate without touching the network.

    ``uv venv`` runs first and ``uv pip install`` second; dispatching on the
    argv rather than on call order keeps the fake honest if the script ever
    reorders them.
    """
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> _FakeCompleted:
        calls.append(argv)
        outcome = install if "install" in argv else venv
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", run)


# ---------------------------------------------------------------------------
# RED FIRST — the recorded, live-measured failure
# ---------------------------------------------------------------------------


def test_the_recorded_published_triple_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate refuses the exact triple that is live-broken on PyPI today.

    This is the whole point of the ticket: had this step existed, omnimarket
    0.4.21 would never have been tagged, and 0.4.22-0.4.24 would not have
    compounded it.
    """
    _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=_FakeCompleted(0),
        install=_FakeCompleted(1, stderr=_recorded_uv_output()),
    )

    assert main([str(tmp_path / "dist")]) == 1

    out = capsys.readouterr().out
    assert _UNRESOLVABLE_REPORT in out
    # The resolver's own explanation must survive into the report; a bare
    # "pins did not resolve" sends the reader back to re-run it by hand.
    assert "omnibase-core==0.47.4" in out
    assert "omnibase-core>=0.47.5" in out
    assert _TIMEOUT_REPORT not in out


def test_the_recorded_fixture_still_describes_an_unresolvable_result() -> None:
    """Guard the fixture itself.

    A fixture silently edited into a resolvable transcript would turn the
    red-first test above green while proving nothing.
    """
    recorded = _recorded_uv_output()
    assert "No solution found when resolving dependencies" in recorded
    assert "omnimarket==0.4.24" in recorded


# ---------------------------------------------------------------------------
# Timeout is NOT a broken pin
# ---------------------------------------------------------------------------


def test_a_timeout_exits_one_but_reports_throughput_not_a_broken_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=_FakeCompleted(0),
        install=subprocess.TimeoutExpired(
            cmd=["uv", "pip", "install"], timeout=900, output=b"partial progress"
        ),
    )

    assert main([str(tmp_path / "dist")]) == 1

    out = capsys.readouterr().out
    assert _TIMEOUT_REPORT in out
    # OMN-16047: the two failures exit the same way, so the REPORT is the only
    # thing that tells them apart. A timeout must never accuse the pins.
    assert _UNRESOLVABLE_REPORT not in out
    # The remedy must be named, or the next reader edits a correct pin.
    assert _INSTALL_TIMEOUT_ENV_VAR in out
    assert "partial progress" in out


def test_the_venv_leg_has_its_own_budget_and_reports_its_own_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hung ``uv venv`` must not be reported as a slow index install.

    It also must not consume the install budget -- that is why the script gives
    it a separate, much smaller allowance.
    """
    _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=subprocess.TimeoutExpired(cmd=["uv", "venv"], timeout=120),
        install=_FakeCompleted(0),
    )

    assert main([str(tmp_path / "dist")]) == 1
    out = capsys.readouterr().out
    assert "uv venv" in out
    assert _UNRESOLVABLE_REPORT not in out


def test_timeout_error_keeps_the_step_and_budget_it_failed_on() -> None:
    exc = PinResolveTimeoutError(
        "uv pip install",
        900,
        subprocess.TimeoutExpired(cmd=["uv"], timeout=900),
        "prior",
    )
    assert exc.step == "uv pip install"
    assert exc.budget_seconds == 900
    assert exc.prior_output == "prior"


# ---------------------------------------------------------------------------
# An index/network fault is a FAILURE, never a pass
# ---------------------------------------------------------------------------


def test_an_index_fault_fails_and_is_never_treated_as_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable index must not read as "the pins are fine".

    Rule 16 of the workspace doctrine in one test: an empty/errored result is
    not evidence of absence. A gate that passes when it could not reach PyPI
    reports proof it does not have.
    """
    _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=_FakeCompleted(0),
        install=_FakeCompleted(
            2, stderr="error sending request for url (https://pypi.org/simple/)"
        ),
    )

    assert main([str(tmp_path / "dist")]) == 1
    assert _UNRESOLVABLE_REPORT in capsys.readouterr().out


def test_a_failed_venv_creation_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=_FakeCompleted(1, stderr="uv venv: permission denied"),
        install=_FakeCompleted(0),
    )

    ok, log = verify_pin_resolvability(wheel)
    assert ok is False
    assert "permission denied" in log


# ---------------------------------------------------------------------------
# dist/ shape — never a pass by vacuity
# ---------------------------------------------------------------------------


def test_an_empty_dist_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        find_single_wheel(dist)
    assert "no wheel" in str(excinfo.value)


def test_two_wheels_are_refused_rather_than_arbitrarily_chosen(
    tmp_path: Path,
) -> None:
    """Picking one of two wheels would verify an artifact that may not ship."""
    _wheel(tmp_path, "omnimarket-0.4.24-py3-none-any.whl")
    _wheel(tmp_path, "omnimarket-0.4.25-py3-none-any.whl")
    with pytest.raises(SystemExit) as excinfo:
        find_single_wheel(tmp_path / "dist")
    assert "exactly one wheel" in str(excinfo.value)


def test_a_missing_dist_directory_is_a_failure(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        find_single_wheel(tmp_path / "no-such-dist")


def test_the_sdist_is_ignored_when_selecting_the_wheel(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    (tmp_path / "dist" / "omnimarket-0.4.24.tar.gz").write_bytes(b"sdist")
    assert find_single_wheel(tmp_path / "dist") == wheel


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["dist/", "extra"]])
def test_a_bad_invocation_exits_two_distinctly(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 is reserved for "you called this wrong".

    Sharing exit 1 with a substantive failure would let a workflow typo read as
    a broken pin and block a release for the wrong reason.
    """
    assert main(argv) == 2
    assert "usage:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The green leg, last
# ---------------------------------------------------------------------------


def test_a_resolvable_wheel_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wheel(tmp_path)
    _fake_runner(
        monkeypatch,
        venv=_FakeCompleted(0),
        install=_FakeCompleted(0, stdout="Installed 178 packages"),
    )

    assert main([str(tmp_path / "dist")]) == 0
    assert "resolve cleanly" in capsys.readouterr().out


def test_the_install_runs_from_a_scratch_dir_with_no_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole premise of the gate.

    ``[tool.uv.sources]`` git-rev overrides are what hid the broken pin locally.
    They are only unreachable if the install runs from a directory with no
    project config AND through the ``uv pip`` interface (never ``uv sync``/
    ``uv add``/``uv lock``, which read a project's pyproject.toml).
    """
    wheel = _wheel(tmp_path)
    seen: list[tuple[list[str], Path]] = []

    def run(argv: list[str], **kwargs: Any) -> _FakeCompleted:
        seen.append((argv, Path(kwargs["cwd"])))
        return _FakeCompleted(0)

    monkeypatch.setattr(subprocess, "run", run)
    verify_pin_resolvability(wheel)

    install_argv, cwd = next((a, c) for a, c in seen if "install" in a)
    assert install_argv[1:3] == ["pip", "install"], (
        "must use the pip-compatible interface; uv sync/add/lock read "
        "[tool.uv.sources] and would defeat the check"
    )
    assert "--no-cache" in install_argv
    assert not (cwd / "pyproject.toml").exists()
    assert not (cwd / "uv.toml").exists()
    # It must install the COPY in the scratch dir, not the path inside the repo
    # checkout -- an install run against dist/ would sit next to pyproject.toml.
    assert str(cwd) in install_argv[-1]


def test_the_configured_budget_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow sets this; if it stopped being read the budget would be a lie."""
    monkeypatch.setenv(_INSTALL_TIMEOUT_ENV_VAR, "123")
    import importlib

    import scripts.ci.verify_pypi_pin_resolvability as module

    reloaded = importlib.reload(module)
    try:
        assert reloaded._INSTALL_TIMEOUT_SECONDS == 123
    finally:
        monkeypatch.delenv(_INSTALL_TIMEOUT_ENV_VAR, raising=False)
        importlib.reload(module)
