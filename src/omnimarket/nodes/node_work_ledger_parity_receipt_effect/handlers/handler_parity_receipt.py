# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily work-ledger parity receipt writer (OMN-23251), moved from the ledger-publish unit, whose ``parity_receipt`` function it replaces (OMN-20536).

Once a UTC day, from the not-before time on, it runs the parity check for the previous UTC day with ``--receipt`` and
appends the one STATUS row it prints. A day whose newest receipt is clean is skipped; a day with none, or whose newest
receipt shows ``missing`` or ``unexplained`` above 0, gets a fresh receipt (OMN-23251), so a fixed projection can turn the
day green without waiting for the file to roll.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    ProtocolLedgerAppender,
    SubprocessLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.parity_receipt_core import (
    build_dsn,
    receipt_day,
    receipt_owed,
    row_pattern,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.models.model_parity_receipt import (
    ModelParityReceiptRequest,
    ModelParityReceiptResult,
    ModelParityReceiptSettings,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.protocols.protocol_parity_runner import (
    ProtocolParityRunner,
    SubprocessParityRunner,
)

NODE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OVERLAY = NODE_DIR / "overlay.example.yaml"


def load_settings(path: Path) -> ModelParityReceiptSettings:
    import yaml

    return ModelParityReceiptSettings.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _result(
    state: str, day: str = "", reason: str = "", error: str = "", row: str = ""
) -> ModelParityReceiptResult:
    failed = state in ("NOT-CONFIGURED", "FAILED")
    line = f"parity receipt: {state}" + (f" day={day}" if day else "")
    if reason:
        line += f" reason={reason}"
    if error:
        line += f": {error}"
    return ModelParityReceiptResult.model_validate(
        {
            "status": "failure" if failed else "success",
            "exit_code": 1 if failed else 0,
            "state": state,
            "day": day,
            "reason": reason,
            "receipt_row": row,
            "log_line": line,
            "error_message": error,
        }
    )


class HandlerParityReceipt:
    """Write the owed receipt; the runner, the appender and the settings can be injected."""

    def __init__(
        self,
        runner: ProtocolParityRunner | None = None,
        appender: ProtocolLedgerAppender | None = None,
        settings: ModelParityReceiptSettings | None = None,
    ) -> None:
        self._settings = (
            settings if settings is not None else load_settings(DEFAULT_OVERLAY)
        )
        self._runner: ProtocolParityRunner = (
            runner if runner is not None else SubprocessParityRunner()
        )
        self._appender: ProtocolLedgerAppender = (
            appender
            if appender is not None
            else SubprocessLedgerAppender(
                self._settings.appender_command, self._settings.appender_timeout_seconds
            )
        )

    @property
    def settings(self) -> ModelParityReceiptSettings:
        return self._settings

    def handle(self, request: ModelParityReceiptRequest) -> ModelParityReceiptResult:
        settings = self._settings
        try:
            now = (
                datetime.fromisoformat(request.now.replace("Z", "+00:00"))
                if request.now
                else datetime.now(UTC)
            )
        except ValueError as exc:
            return _result("FAILED", error=f"cannot read now: {exc}")
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        day = receipt_day(now, settings)
        if day is None:
            return _result("NOT-YET")
        ledger = Path(request.ledger_path)
        try:
            text = ledger.read_text(encoding="utf-8")
        except OSError as exc:
            return _result("FAILED", day, error=f"cannot read the ledger: {exc}")
        owed = receipt_owed(text, settings.lane, day)
        if owed == "newest-clean":
            return _result("PRESENT", day, owed)
        if request.dry_run:
            return _result("DRY-RUN", day, owed)
        unset = [
            name
            for name, value in (
                ("parity_python", request.parity_python),
                ("dsn_overlay_path", request.dsn_overlay_path),
                ("dsn_env_file", request.dsn_env_file),
                ("pg_port", request.pg_port),
            )
            if not value
        ]
        if unset:
            return _result(
                "NOT-CONFIGURED", day, owed, error=f"{', '.join(unset)} unset"
            )
        try:
            dsn = build_dsn(
                Path(request.dsn_overlay_path).read_text(encoding="utf-8"),
                Path(request.dsn_env_file).read_text(encoding="utf-8"),
                request.host_environment,
                int(request.pg_port),
                settings,
            )
        except (OSError, KeyError, ValueError) as exc:
            return _result(
                "FAILED", day, owed,
                error=f"cannot build the projection DSN: {type(exc).__name__}: {exc}",
            )  # fmt: skip
        command = [
            request.parity_python, "-m", settings.parity_module,
            "--ledger", str(ledger),
            "--archive-dir", str(ledger.parent / settings.archive_subdir),
            "--utc-day", day, "--dsn-env", settings.dsn_env_name, "--receipt",
            "--state-dir", str(Path(request.registry_root) / settings.state_subdir),
        ]  # fmt: skip
        try:
            done = self._runner.run(
                command, {settings.dsn_env_name: dsn}, settings.parity_timeout_seconds
            )
        except (OSError, TimeoutError) as exc:
            return _result(
                "FAILED", day, owed, error=f"parity check did not run: {exc}"
            )
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        if done.returncode not in (0, 1):
            return _result(
                "FAILED", day, owed,
                error=f"parity check exited {done.returncode}: {done.stderr.strip()[-400:]}",
            )  # fmt: skip
        match = row_pattern(settings.lane).match(lines[0]) if len(lines) == 1 else None
        if match is None or match.group("day") != day:
            return _result(
                "FAILED", day, owed,
                error=f"parity check printed no receipt row for {day}: {done.stdout[:300]!r}",
            )  # fmt: skip
        outcome = self._appender.append(ledger, lines[0])
        if not outcome.ok:
            return _result(
                "FAILED", day, owed,
                error=f"onex-ledger refused the receipt ({outcome.detail})",
            )  # fmt: skip
        return _result("APPENDED", day, owed, row=lines[0])
