# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_shim_scanner (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): writes synthetic Python files carrying ``@shim(...)``
annotations under ``tmp_path`` and drives ``HandlerShimScanner.handle`` in-process.
The scan is AST-based, so a fixed ``reference_date`` makes EXPIRED/EXPIRING/ACTIVE
classification fully deterministic without touching the clock.

Asserts typed result fields (``total_count``, ``expired_count``, ``expiring_count``)
and the per-finding ``status`` enum / ``days_until_expiry``.

Negative control: a shim whose ``expires_on`` precedes ``reference_date`` must
produce an EXPIRED finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_shim_scanner.handlers.handler_shim_scanner import (
    HandlerShimScanner,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_finding import EnumShimStatus
from omnimarket.nodes.node_shim_scanner.models.model_shim_scan_request import (
    ModelShimScanRequest,
)

_REFERENCE_DATE = "2026-06-27"

_NO_SHIM = "def plain():\n    return 1\n"


def _shim_func(name: str, expires: str) -> str:
    """Emit a function decorated with a deterministic @shim(...) annotation."""
    y, m, d = expires.split("-")
    return (
        "import datetime\n\n\n"
        f'@shim(ticket_id="OMN-13675", expires_on=datetime.date({int(y)}, '
        f'{int(m)}, {int(d)}), reason="legacy", replacement="node_x")\n'
        f"def {name}():\n    return 1\n"
    )


_EXPIRED = _shim_func("expired_fn", "2026-01-01")  # before reference -> EXPIRED
_EXPIRING = _shim_func("expiring_fn", "2026-07-10")  # +13d within 30d -> EXPIRING
_ACTIVE = _shim_func("active_fn", "2027-01-01")  # far future -> ACTIVE


def _write(tmp: Path, name: str, source: str) -> Path:
    p = tmp / name
    p.write_text(source, encoding="utf-8")
    return p


# (file sources, warn_days, expected_total, expected_expired, expected_expiring,
#  required_status|None)
CASES = [
    pytest.param(
        {"clean.py": _NO_SHIM},
        30,
        0,
        0,
        0,
        None,
        id="no-shims-zero-findings",
    ),
    pytest.param(
        {"old.py": _EXPIRED},
        30,
        1,
        1,
        0,
        EnumShimStatus.EXPIRED,
        id="expired-shim-negative-control",
    ),
    pytest.param(
        {"soon.py": _EXPIRING},
        30,
        1,
        0,
        1,
        EnumShimStatus.EXPIRING,
        id="expiring-within-warn-window",
    ),
    pytest.param(
        {"future.py": _ACTIVE},
        30,
        1,
        0,
        0,
        EnumShimStatus.ACTIVE,
        id="active-shim-far-future",
    ),
    pytest.param(
        # Same EXPIRING fixture (+13d) but a narrower warn window (7d) -> ACTIVE.
        {"soon.py": _EXPIRING},
        7,
        1,
        0,
        0,
        EnumShimStatus.ACTIVE,
        id="warn-window-boundary-narrows-to-active",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("files", "warn_days", "total", "expired", "expiring", "status"), CASES
)
def test_shim_scanner_multiparam(
    tmp_path: Path,
    files: dict[str, str],
    warn_days: int,
    total: int,
    expired: int,
    expiring: int,
    status: EnumShimStatus | None,
) -> None:
    for name, source in files.items():
        _write(tmp_path, name, source)

    request = ModelShimScanRequest(
        paths=[str(tmp_path)],
        reference_date=_REFERENCE_DATE,
        warn_days_before_expiry=warn_days,
    )
    result = HandlerShimScanner().handle(request)

    assert result.total_count == total
    assert result.expired_count == expired
    assert result.expiring_count == expiring
    assert len(result.findings) == total
    if status is not None:
        assert all(f.status == status for f in result.findings)
        assert all(f.ticket_id == "OMN-13675" for f in result.findings)
        if status == EnumShimStatus.EXPIRED:
            assert all(f.days_until_expiry < 0 for f in result.findings)


@pytest.mark.integration
def test_shim_scanner_multiple_paths_aggregate(tmp_path: Path) -> None:
    """A directory containing several shim files aggregates per-status counts."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _write(pkg, "a.py", _EXPIRED)
    _write(pkg, "b.py", _EXPIRING)
    _write(pkg, "c.py", _ACTIVE)
    _write(pkg, "d.py", _NO_SHIM)

    result = HandlerShimScanner().handle(
        ModelShimScanRequest(
            paths=[str(pkg)],
            reference_date=_REFERENCE_DATE,
            warn_days_before_expiry=30,
        )
    )

    assert result.total_count == 3
    assert result.expired_count == 1
    assert result.expiring_count == 1
    statuses = {f.function_name: f.status for f in result.findings}
    assert statuses["expired_fn"] == EnumShimStatus.EXPIRED
    assert statuses["expiring_fn"] == EnumShimStatus.EXPIRING
    assert statuses["active_fn"] == EnumShimStatus.ACTIVE
