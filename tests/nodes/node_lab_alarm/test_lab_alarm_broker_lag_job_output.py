# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one run of the scheduled broker-lag verb prints, read the way the job log is read.

The scheduled unit runs the ``broker-lag`` verb and its log is the verb's stdout. A group whose consumers commit
slower than the topic is produced keeps growing, so the verb ends ALARM and exits 2 on every tick. That exit does
not hide the other lines a reader looks for in the same log: the manifest fallback note rides in the same
``BROKER-LAG`` line as the alarm lines, a manifest that answered prints no fallback note at all, and the verb
prints no effects-held reading, which only the effects-held verbs read.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryManifest,
    InMemoryManifestCache,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import EnumLabAlarmFault
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import run
from tests.nodes.node_lab_alarm.test_lab_alarm_chain_events import (
    _TOPICS,
    _flat,
    _group,
    _manifest,
)
from tests.nodes.node_lab_alarm.test_lab_alarm_manifest_fallback import (
    _down,
    _good_cache,
    _handler,
    _overlay,
)

pytestmark = pytest.mark.unit

_GROWING = _group("1.2.0", _TOPICS[0])


def _growing_lags() -> dict[str, int]:
    lags = _flat(5)
    lags[_GROWING] = 9
    return lags


def _run_verb(
    *, manifest: InMemoryManifest, cache: InMemoryManifestCache | None
) -> tuple[int, str]:
    handler, _records, _samples = _handler(
        lags=_growing_lags(), previous=_flat(5), manifest=manifest, cache=cache
    )
    printed: list[str] = []
    code = run(
        "broker-lag",
        EnumLabAlarmFault.NONE,
        overlay=_overlay(),
        handler=handler,
        out=printed.append,
    )
    return code, "".join(printed)


def test_golden_chain_growth_exits_2_and_names_the_group_and_its_topic() -> None:
    code, out = _run_verb(manifest=InMemoryManifest(_manifest()), cache=None)

    assert code == 2
    assert "BROKER-LAG outcome=ALARM" in out
    assert f"ALARM {_GROWING}: lag grew 5->9" in out
    assert _TOPICS[0] in _GROWING  # the group name carries its topic after ``.__t.``


def test_error_chain_the_fallback_note_and_the_alarm_lines_print_in_one_run() -> None:
    code, out = _run_verb(
        manifest=_down(), cache=_good_cache(int(timedelta(minutes=2).total_seconds()))
    )

    assert code == 2
    broker_lag_line = next(
        line for line in out.splitlines() if line.startswith("BROKER-LAG ")
    )
    assert "manifest-stale:" in broker_lag_line
    assert f"ALARM {_GROWING}: lag grew 5->9" in out


def test_a_manifest_that_answered_prints_no_fallback_note_while_the_alarm_fires() -> (
    None
):
    code, out = _run_verb(
        manifest=InMemoryManifest(_manifest()), cache=_good_cache(120)
    )

    assert code == 2
    assert "manifest-stale" not in out
    assert "manifest-unreachable" not in out


def test_the_broker_lag_verb_prints_no_effects_held_reading() -> None:
    _code, out = _run_verb(manifest=InMemoryManifest(_manifest()), cache=None)

    assert "EFFECTS-HELD" not in out
