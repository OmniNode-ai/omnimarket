# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick run in-process must not leave its process-ending deadline timer armed (OMN-23693).

The tick entrypoints arm ``faulthandler.dump_traceback_later(deadline, exit=True)`` at the top
of ``main`` so a hung tick ends its own process instead of blocking its caller. Run in-process
(by a test, or any in-process caller), a tick that returns without cancelling that timer leaves
it armed for the host process, which then ends with ``_exit(1)`` seconds later, silently, on
whatever is running at the time. These tests run each tick's ``main`` through its overlay
refusal path (no overlay configured, so nothing touches a broker) with the arm and cancel
calls recorded, and assert the recorded events start with ``arm`` and end with ``cancel``.
No module's ``main`` raises on the refusal path it takes here: each returns its own refusal
code, so no exception handling is needed in the test.
"""

from __future__ import annotations

import faulthandler
import importlib
from pathlib import Path

import pytest

import omnimarket

pytestmark = pytest.mark.unit

TICK_MODULES = (
    "omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_tick",
    "omnimarket.nodes.node_overlay_store_load_effect.runtime_overlay_store_load_tick",
    "omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick",
    "omnimarket.nodes.node_c32_auth_boundary_probe_effect.runtime_c32_probe_tick",
    "omnimarket.nodes.node_job_log_filler_effect.runtime_job_log_filler_tick",
)

# Every overlay source the five loaders consult. With all of these unset (and no overlay file
# under any root) each loader refuses before any bus or broker is opened.
_OVERLAY_ENV = (
    "ONEX_SKILL_OVERLAY_ROOTS",
    "ONEX_C32_PROBE_OVERLAY",
    "ONEX_JOB_LOG_FILLER_OVERLAY",
)


@pytest.mark.parametrize("module_name", TICK_MODULES)
def test_tick_main_disarms_its_deadline_timer(
    module_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _OVERLAY_ENV:
        monkeypatch.delenv(name, raising=False)
    events: list[str] = []

    def arm(*args: object, **kwargs: object) -> None:
        events.append("arm")

    def cancel(*args: object, **kwargs: object) -> bool:
        events.append("cancel")
        return True

    monkeypatch.setattr(faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", cancel)
    main = importlib.import_module(module_name).main
    main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")])
    assert events, f"{module_name}: main() armed no deadline timer at all"
    assert events[0] == "arm"
    assert events[-1] == "cancel", (
        f"{module_name}: main() returned with the process-ending timer still armed: "
        f"{events!r}"
    )


def test_every_module_that_arms_a_deadline_timer_is_covered() -> None:
    """A sixth tick that arms the timer must join ``TICK_MODULES``, so it is held to the same rule."""
    source_root = Path(omnimarket.__file__).resolve().parent
    armed = {
        path
        for path in source_root.rglob("*.py")
        if "faulthandler.dump_traceback_later(" in path.read_text(encoding="utf-8")
    }
    covered = {
        Path(str(importlib.import_module(name).__file__)).resolve()
        for name in TICK_MODULES
    }
    assert armed == covered
