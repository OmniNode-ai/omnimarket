# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerShimScanner (OMN-4419)."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_shim_scanner.handlers.handler_shim_scanner import (
    HandlerShimScanner,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_finding import (
    EnumShimStatus,
)
from omnimarket.nodes.node_shim_scanner.models.model_shim_scan_request import (
    ModelShimScanRequest,
)


def _write_py(tmp_path: Path, name: str, source: str) -> Path:
    f = tmp_path / name
    f.write_text(source)
    return f


@pytest.mark.unit
class TestHandlerShimScanner:
    _HANDLER = HandlerShimScanner()
    _REF = datetime.date(2026, 6, 1)

    def _run(
        self,
        paths: list[str],
        reference_date: datetime.date = _REF,
        warn_days: int = 30,
    ) -> object:
        req = ModelShimScanRequest(
            paths=paths,
            reference_date=reference_date.isoformat(),
            warn_days_before_expiry=warn_days,
        )
        return self._HANDLER.handle(req)

    def test_detects_expired_shim(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "legacy.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(
    ticket_id="OMN-1",
    expires_on=datetime.date(2026, 5, 1),
    reason="old compat",
    replacement="NewClass",
)
def old_fn():
    pass
""",
        )
        result = self._run([str(f)])
        assert result.expired_count == 1
        assert result.total_count == 1
        assert result.findings[0].status == EnumShimStatus.EXPIRED
        assert result.findings[0].ticket_id == "OMN-1"
        assert result.findings[0].function_name == "old_fn"

    def test_detects_expiring_shim(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "soon.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(
    ticket_id="OMN-2",
    expires_on=datetime.date(2026, 6, 15),
    reason="soon",
    replacement="Better",
)
def almost_expired():
    pass
""",
        )
        result = self._run([str(f)], warn_days=30)
        assert result.expiring_count == 1
        assert result.findings[0].status == EnumShimStatus.EXPIRING

    def test_detects_active_shim(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "active.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(
    ticket_id="OMN-3",
    expires_on=datetime.date(2027, 1, 1),
    reason="not yet",
    replacement="FutureClass",
)
def still_fine():
    pass
""",
        )
        result = self._run([str(f)])
        assert result.total_count == 1
        assert result.findings[0].status == EnumShimStatus.ACTIVE

    def test_empty_file_returns_no_findings(self, tmp_path: Path) -> None:
        f = _write_py(tmp_path, "empty.py", "")
        result = self._run([str(f)])
        assert result.total_count == 0

    def test_no_shim_decorator_skipped(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "plain.py",
            """\
def plain():
    pass
""",
        )
        result = self._run([str(f)])
        assert result.total_count == 0

    def test_directory_scan(self, tmp_path: Path) -> None:
        _write_py(
            tmp_path,
            "a.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(ticket_id="OMN-10", expires_on=datetime.date(2026, 5, 1), reason="r", replacement="x")
def fn_a(): pass
""",
        )
        _write_py(
            tmp_path,
            "b.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(ticket_id="OMN-11", expires_on=datetime.date(2026, 5, 1), reason="r", replacement="x")
def fn_b(): pass
""",
        )
        result = self._run([str(tmp_path)])
        assert result.total_count == 2
        assert result.expired_count == 2

    def test_syntax_error_file_skipped_gracefully(self, tmp_path: Path) -> None:
        f = _write_py(tmp_path, "bad.py", "def broken(: pass")
        result = self._run([str(f)])
        assert result.total_count == 0

    def test_nonexistent_path_produces_no_findings(self) -> None:
        result = self._run(["/nonexistent/path/that/does/not/exist.py"])
        assert result.total_count == 0

    def test_finding_fields_populated(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "check_fields.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(
    ticket_id="OMN-99",
    expires_on=datetime.date(2026, 5, 15),
    reason="field check",
    replacement="omnibase_compat.NewDto",
)
def check_me():
    pass
""",
        )
        result = self._run([str(f)])
        assert result.total_count == 1
        finding = result.findings[0]
        assert finding.ticket_id == "OMN-99"
        assert finding.reason == "field check"
        assert finding.replacement == "omnibase_compat.NewDto"
        assert finding.expires_on == datetime.date(2026, 5, 15)
        assert finding.line_number > 0
        assert finding.file_path == str(f)

    def test_multiple_shims_in_one_file(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "multi.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim(ticket_id="OMN-A", expires_on=datetime.date(2026, 5, 1), reason="r", replacement="x")
def first(): pass

@shim(ticket_id="OMN-B", expires_on=datetime.date(2027, 1, 1), reason="r", replacement="y")
def second(): pass
""",
        )
        result = self._run([str(f)])
        assert result.total_count == 2
        assert result.expired_count == 1
        assert result.expiring_count == 0

    def test_positional_args_supported(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            "positional.py",
            """\
import datetime
from omnibase_core.decorators import shim

@shim("OMN-POS", datetime.date(2026, 5, 1), "positional reason", "ReplacementClass")
def positional_fn(): pass
""",
        )
        result = self._run([str(f)])
        assert result.total_count == 1
        assert result.findings[0].ticket_id == "OMN-POS"
