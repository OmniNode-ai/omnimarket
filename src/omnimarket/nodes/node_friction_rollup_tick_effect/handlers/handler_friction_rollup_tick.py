# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One friction rollup tick: read the ledger, roll up each window, write the reports, append one STATUS row (OMN-22987).

A behaviour-preserving port of the main flow of the friction rollup timer. The rollup decisions are the compute node's
handler, run in process; the pure decisions of the tick are in ``friction_rollup_tick_core``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
    parse_class_owners,
    parse_class_rules,
    parse_iso8601,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_core import (
    _cell,
    compute_trend,
    file_stamp,
    log_line,
    needs_ticket,
    render_summary_markdown,
    stamp,
    status_row,
    top_classes,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_io import (
    load_settings,
    prune_reports,
    read_previous,
    read_sources,
    write_atomic,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionClassOverlay,
    ModelFrictionRollupTickRequest,
    ModelFrictionRollupTickResult,
    ModelFrictionRollupTickSettings,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    ProtocolLedgerAppender,
    SubprocessLedgerAppender,
)

NODE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OVERLAY = NODE_DIR / "overlay.example.yaml"


def _refused(message: str) -> ModelFrictionRollupTickResult:
    return ModelFrictionRollupTickResult(
        status="failure", exit_code=2, state="REFUSED", error_message=message
    )


class HandlerFrictionRollupTick:
    """Run one tick; the appender and the settings can be injected, otherwise the overlay and the real command are used."""

    def __init__(
        self,
        appender: ProtocolLedgerAppender | None = None,
        settings: ModelFrictionRollupTickSettings | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = environ
        self._settings = (
            settings if settings is not None else load_settings(DEFAULT_OVERLAY)
        )
        self._appender: ProtocolLedgerAppender = (
            appender
            if appender is not None
            else SubprocessLedgerAppender(
                self._settings.appender_command, self._settings.appender_timeout_seconds
            )
        )
        self._rollup = HandlerFrictionRollup()

    @property
    def settings(self) -> ModelFrictionRollupTickSettings:
        return self._settings

    def _class_rules(
        self,
    ) -> tuple[list[ModelFrictionClassRule], list[ModelFrictionClassOwner], str | None]:
        """The overlay home's rules, or empty lists and the note that says why they are unapplied."""
        try:
            overlay = load_structured_overlay(
                self._settings.class_overlay_id,
                ModelFrictionClassOverlay,
                self._environ,
                file_env=self._settings.class_overlay_file_env,
            )
            rules = list(overlay.class_rules)
            owners = list(overlay.class_owners)
            parse_class_rules((r.name, r.owner, r.pattern) for r in rules)
            parse_class_owners((o.class_name, o.owner) for o in owners)
        except (ImportError, OSError, UnicodeError, ValueError, ValidationError) as exc:
            return [], [], f"class-rules=unapplied:{_cell(type(exc).__name__)}"
        except Exception as exc:
            return [], [], f"class-rules=unapplied:{_cell(type(exc).__name__)}"
        return rules, owners, None

    def _window_report(
        self,
        sources: list[ModelFrictionRollupSource],
        rules: list[ModelFrictionClassRule],
        owners: list[ModelFrictionClassOwner],
        since: datetime,
        until: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        request = ModelFrictionRollupRequest(
            sources=sources,
            since=stamp(since),
            until=stamp(until),
            generated_at=stamp(now),
            bucket_hours=self._settings.bucket_hours,
            umbrella_tickets=[self._settings.umbrella_ticket],
            aliases=[],
            class_rules=rules,
            class_owners=owners,
        )
        return self._rollup.handle(request).report

    def handle(
        self, request: ModelFrictionRollupTickRequest
    ) -> ModelFrictionRollupTickResult:
        settings = self._settings
        ledger = Path(request.ledger_path)
        archive_dir = (
            Path(request.archive_dir)
            if request.archive_dir
            else ledger.parent / settings.archive_subdir
        )
        state_dir = (
            Path(request.state_dir)
            if request.state_dir
            else Path(request.registry_root) / settings.state_subdir
        )
        try:
            now = parse_iso8601(request.now) if request.now else datetime.now(UTC)
        except ValueError as exc:
            return _refused(str(exc))
        now = now.replace(microsecond=0)
        if not ledger.is_file():
            return _refused(f"friction-rollup: ledger {ledger} is absent")
        rules, owners, rules_note = self._class_rules()
        try:
            sources = read_sources(ledger, archive_dir, settings)
            current = {
                window.name: self._window_report(
                    sources,
                    rules,
                    owners,
                    now - timedelta(hours=window.hours),
                    now,
                    now,
                )
                for window in settings.windows
            }
        except (OSError, UnicodeError) as exc:
            return _refused(f"friction-rollup: input could not be read: {exc}")

        latest_json = state_dir / f"{settings.latest_stem}.json"
        previous = read_previous(latest_json, settings)
        previous_path = ""
        if previous is not None:
            previous_path = str(previous.get("report_json") or latest_json.name)
        top = top_classes(current[settings.primary.name], settings.top_n)
        flagged = needs_ticket(current[settings.primary.name])
        summary: dict[str, Any] = {
            "top_classes": top,
            "needs_ticket": flagged,
            "trend": compute_trend(
                current, top, flagged, previous, previous_path, settings
            ),
        }
        stamped = file_stamp(now)
        report_name = f"{stamped}.md"
        document: dict[str, Any] = {
            "schema": settings.schema_name,
            "ticket": settings.ticket,
            "generated_at": stamp(now),
            "report_json": f"{stamped}.json",
            "report_md": report_name,
            "windows": current,
            "summary": summary,
        }
        if rules_note is not None:
            document["class_rules"] = rules_note.removeprefix("class-rules=")
        row = status_row(now, current, summary, report_name, settings, rules_note)
        json_text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        markdown_text = render_summary_markdown(now, current, summary, settings)
        done_code = 0 if rules_note is None else 3
        done_status: Literal["success", "partial"] = (
            "success" if rules_note is None else "partial"
        )
        class_rules = (
            "applied" if rules_note is None else rules_note.removeprefix("class-rules=")
        )

        if request.dry_run:
            return ModelFrictionRollupTickResult(
                status=done_status,
                exit_code=done_code,
                state="DRY-RUN",
                status_row=row,
                log_line=log_line(
                    now, "DRY-RUN", "-", current, top, flagged, settings, rules_note
                ),
                class_rules=class_rules,
                report_json_text=json_text,
                report_markdown_text=markdown_text,
            )

        written: list[str] = []
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            for name, text in (
                (f"{stamped}.json", json_text),
                (report_name, markdown_text),
                (latest_json.name, json_text),
                (f"{settings.latest_stem}.md", markdown_text),
            ):
                write_atomic(state_dir / name, text)
                written.append(name)
            pruned = prune_reports(state_dir, now, settings.keep_days)
        except OSError as exc:
            return _refused(f"friction-rollup: output could not be written: {exc}")

        report_path = str(state_dir / report_name)
        outcome = self._appender.append(ledger, row)
        refused = not outcome.ok
        state: Literal["OK", "APPEND-FAILED"] = "APPEND-FAILED" if refused else "OK"
        return ModelFrictionRollupTickResult(
            status="failure" if refused else done_status,
            exit_code=1 if refused else done_code,
            state=state,
            status_row=row,
            log_line=log_line(
                now, state, report_path, current, top, flagged, settings, rules_note
            ),
            error_message=(
                f"friction-rollup: the STATUS row was refused: {outcome.detail}"
                if refused
                else ""
            ),
            class_rules=class_rules,
            report_path=report_path,
            report_json_text=json_text,
            report_markdown_text=markdown_text,
            written=tuple(written),
            pruned=tuple(path.name for path in pruned),
        )
