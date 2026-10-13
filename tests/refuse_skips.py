# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``--refuse-skips``: a run in which a test did not execute is a failed run (OMN-19987).

A proof that needs a database skips when the database is absent, and pytest
exits 0 for a run in which every test skipped. An evidence check that runs such
a proof therefore passes with nothing proven. A check that adds
``--refuse-skips`` exits 1 unless every selected test ran and passed: a skip, a
module skipped at import and an expected failure are each refused, and the run
says which test did not run and why.

Without the option nothing here changes a run. The hooks are loaded for every
run by ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest

_OPTION = "refuse_skips"
# Outcomes the terminal reporter files a test under when it proved nothing.
_REFUSED_OUTCOMES = ("skipped", "xfailed")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--refuse-skips",
        action="store_true",
        default=False,
        dest=_OPTION,
        help=(
            "Exit 1 when any test is skipped or is an expected failure. For "
            "evidence checks, where a skipped proof must not pass."
        ),
    )


def _refused(config: pytest.Config) -> list[str] | None:
    """The tests that did not run, one line each; None when the reporter is absent."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return None
    lines: list[str] = []
    for outcome in _REFUSED_OUTCOMES:
        for report in reporter.stats.get(outcome, []):
            longrepr = getattr(report, "longrepr", None)
            if isinstance(longrepr, tuple) and len(longrepr) == 3:
                reason = str(longrepr[2])
            else:
                reason = str(getattr(report, "wasxfail", "") or longrepr or "")
            nodeid = getattr(report, "nodeid", "") or "<collection>"
            lines.append(f"{outcome}: {nodeid}: {reason}".rstrip(": "))
    return lines


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus  # the hook's signature; the session carries the value changed below
    config = session.config
    if not config.getoption(_OPTION, False):
        return
    refused = _refused(config)
    # No reporter means the outcomes cannot be read, which is not a pass either.
    if (refused is None or refused) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    if not config.getoption(_OPTION, False):
        return
    refused = _refused(config)
    if not refused:
        return
    terminalreporter.write_sep("=", "refuse-skips: tests that did not run", red=True)
    for line in refused:
        terminalreporter.write_line(line)
    terminalreporter.write_line(
        f"refuse-skips: {len(refused)} test(s) did not run, so this run is not a pass."
    )
