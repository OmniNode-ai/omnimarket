"""The disk watermark tick ends within its own interval and runs under a declared CPU quota (OMN-24901).

Measured on h051: the unit fires every 300 s but allowed 600 s, and the class scan walks whole caches, so one tick ran
243 s at 100% of a core while the next was due. The contract now declares a start timeout below the interval (the class
scan's budget is its share of it, OMN-26097) and a CPU quota, and an install whose overlay interval the run cannot end
within is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    load_schedule,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkSchedule,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.watermark_schedule import (
    render_units,
)

from .host_disk_inputs import make_host, overlay_document, write_overlay

pytestmark = pytest.mark.unit


def _schedule_fields(**changes: Any) -> dict[str, Any]:
    return {**load_schedule().model_dump(), **changes}


def test_the_declared_run_ends_within_the_interval_even_when_every_bound_is_spent() -> (
    None
):
    """The scan spends its share of the start timeout, then the two docker reads (60 s each in the overlay) follow."""
    schedule = load_schedule()
    scan = schedule.scan_budget_percent / 100 * schedule.timeout_start_seconds
    assert schedule.timeout_start_seconds < schedule.interval_seconds
    assert scan + 2 * 60 < schedule.timeout_start_seconds


@pytest.mark.parametrize(
    "changes",
    [
        {
            "timeout_start_seconds": 300
        },  # equal to the interval: a tick may overlap the next
        {"timeout_start_seconds": 600},  # the value the unit carried
        {"cpu_quota_percent": 0},
    ],
)
def test_a_schedule_that_can_outlast_its_interval_is_refused(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelLabDiskWatermarkSchedule(**_schedule_fields(**changes))


def test_an_overlay_interval_not_above_the_start_timeout_is_refused_at_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    timeout = load_schedule().timeout_start_seconds
    overlay = write_overlay(
        tmp_path,
        overlay_document(make_host(tmp_path), interval_seconds=timeout),
    )
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "pool-a",
            "--source-root",
            str(tmp_path),
            "--overlay",
            str(overlay),
            "--workspace-root",
            str(tmp_path),
            "--workspace-config-root",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert code == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def _render(schedule: ModelLabDiskWatermarkSchedule, tmp_path: Path) -> str:
    service, _timer = render_units(
        schedule,
        "pool-a",
        python=Path("/usr/bin/python3"),
        source_root=Path("/srv/omnimarket"),
        overlay_file=Path("/srv/overlay.yaml"),
        home=tmp_path,
        workspace_root=Path("/srv/workspace"),
        workspace_config_root=Path("/srv/config"),
    )
    return service


def test_the_unit_renders_the_declared_cpu_quota_and_start_timeout(
    tmp_path: Path,
) -> None:
    schedule = load_schedule()
    service = _render(schedule, tmp_path)
    assert f"\nCPUQuota={schedule.cpu_quota_percent}%\n" in service
    assert f"\nTimeoutStartSec={schedule.timeout_start_seconds}\n" in service
    other = ModelLabDiskWatermarkSchedule(
        **_schedule_fields(cpu_quota_percent=schedule.cpu_quota_percent + 50)
    )
    assert f"\nCPUQuota={other.cpu_quota_percent}%\n" in _render(other, tmp_path)
