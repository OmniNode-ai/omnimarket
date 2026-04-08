"""Tests for the hardcoded-config check in NodeAislopSweep."""

from __future__ import annotations

import tempfile
from pathlib import Path

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)


def _run_check(py_content: str, checks: list[str] | None = None) -> list:
    handler = NodeAislopSweep()
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "src"
        src.mkdir()
        (src / "handler.py").write_text(py_content, encoding="utf-8")
        result = handler.handle(
            AislopSweepRequest(
                target_dirs=[tmpdir],
                checks=checks or ["hardcoded-config"],
            )
        )
    return result.findings


class TestHardcodedIp:
    def test_detects_192_168_address(self) -> None:
        findings = _run_check('BASE_URL = "http://192.168.1.10:8080/api"\n')
        assert len(findings) == 1
        assert findings[0].check == "hardcoded-config"
        assert findings[0].severity == "ERROR"

    def test_detects_10_dot_address(self) -> None:
        findings = _run_check('HOST = "10.0.0.5"\n')
        assert len(findings) == 1
        assert "hardcoded private IP" in findings[0].message

    def test_no_false_positive_on_variable_name(self) -> None:
        # A variable that contains IP-like text but not in a string literal
        findings = _run_check("ip_range = os.environ['IP_RANGE']\n")
        assert findings == []


class TestHardcodedLocalhostUrl:
    def test_detects_http_localhost(self) -> None:
        findings = _run_check('url = "http://localhost:5432"\n')
        assert len(findings) == 1
        assert "localhost" in findings[0].message

    def test_detects_https_localhost(self) -> None:
        findings = _run_check('endpoint = "https://localhost/api"\n')
        assert len(findings) == 1

    def test_detects_127_0_0_1(self) -> None:
        findings = _run_check('broker = "http://127.0.0.1:19092"\n')
        assert len(findings) == 1
        assert "loopback" in findings[0].message


class TestHardcodedPort:
    def test_detects_postgres_port(self) -> None:
        findings = _run_check('DB_PORT = ":5432"\n')
        assert len(findings) == 1
        assert findings[0].check == "hardcoded-config"

    def test_detects_kafka_port(self) -> None:
        findings = _run_check('BROKER = "redpanda:19092"\n')
        assert len(findings) == 1

    def test_detects_redis_port(self) -> None:
        findings = _run_check('cache_port = ":6379"\n')
        assert len(findings) == 1

    def test_no_false_positive_on_year(self) -> None:
        # 8000 inside a year-like context shouldn't fire — but the pattern
        # is intentionally broad; verify it only fires on :-prefixed ports
        findings = _run_check("# created in 8000 BC\n")
        # comment lines are skipped
        assert findings == []


class TestHardcodedDbConnectionString:
    def test_detects_postgres_dsn(self) -> None:
        findings = _run_check('DB_URL = "postgresql://user:pass@host:5432/mydb"\n')
        assert len(findings) >= 1
        checks = {f.check for f in findings}
        assert "hardcoded-config" in checks

    def test_detects_mysql_dsn(self) -> None:
        findings = _run_check('host = "mysql://root:secret@localhost/appdb"\n')
        assert len(findings) >= 1


class TestHardcodedDbName:
    def test_detects_db_name(self) -> None:
        findings = _run_check('DB_NAME = "production_db"\n')
        assert len(findings) == 1
        assert "database name" in findings[0].message

    def test_detects_database_equals(self) -> None:
        findings = _run_check('database = "analytics"\n')
        assert len(findings) == 1


class TestExclusions:
    def test_skips_test_files(self) -> None:
        handler = NodeAislopSweep()
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "test_handler.py").write_text(
                'BASE_URL = "http://localhost:8000"\n', encoding="utf-8"
            )
            result = handler.handle(
                AislopSweepRequest(
                    target_dirs=[tmpdir],
                    checks=["hardcoded-config"],
                )
            )
        assert result.findings == []

    def test_clean_file_produces_no_findings(self) -> None:
        findings = _run_check(
            'BASE_URL = os.environ["BASE_URL"]\nDB_NAME = settings.database_name\n'
        )
        assert findings == []

    def test_hardcoded_config_not_in_default_checks_skipped(self) -> None:
        """Verify the check is included in ALL_CHECKS."""
        assert "hardcoded-config" in NodeAislopSweep.ALL_CHECKS
