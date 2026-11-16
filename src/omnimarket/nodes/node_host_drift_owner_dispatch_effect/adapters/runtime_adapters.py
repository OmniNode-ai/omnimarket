# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime backend: the checker's state file, the ledger read over the bus, files for the record and briefs, a detached runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelBriefPrepared,
    ModelDriftOpenItem,
    ModelDriftState,
    ModelLiveLanes,
    ModelPlaceOutcome,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelBriefSettings,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    DriftStateUnreadableError,
    LiveLanesUnreadableError,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class FileDriftOpenItems:
    """Reads the checker's ``state.json``: ``{"host": str, "open": [{key, kind, declared, observed, first_seen_at, bound_seconds, verdict}]}``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> ModelDriftState:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            host = str(document["host"])
            items = tuple(
                ModelDriftOpenItem(host=host, **{k: row[k] for k in _ITEM_FIELDS})
                for row in document["open"]
            )
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise DriftStateUnreadableError(
                f"{self._path}: {type(exc).__name__}"
            ) from exc
        return ModelDriftState(host=host, items=items)


_ITEM_FIELDS = (
    "key",
    "kind",
    "declared",
    "observed",
    "first_seen_at",
    "bound_seconds",
    "verdict",
)


class FileDispatchRecord:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, str]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {str(k): str(v) for k, v in document.items()}

    def save(self, record: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            temp = Path(handle.name)
        temp.replace(self._path)


_REFUSED_LINE = re.compile(r"^REFUSED (.+)$", re.MULTILINE)
_ROUTE_LINE = re.compile(r"^ROUTE: band=.*$", re.MULTILINE)
_REGENERATE = "regenerate 3a"
#: What a check prints as a refusal when it reached no verdict on the brief (no lab host could run it): not the brief's fault.
_NO_VERDICT = (
    "the check did not run on a lab host",
    "lab host could not check",
    "lab check could not be prepared",
    "check queue: all",
)


class LaneBriefPreparer:
    """Completes a task text into a lane brief through the lane-brief path and checks it, as the lab fill's briefs are.

    The standing rules block, the ticket's text and the 3a constraints are read with the plugin's own scripts
    (``lane_rules_block.py``, ``lane_brief.py ticket-text`` and ``constraints``), the brief is written, and
    ``lane_brief.py check`` runs on the written bytes. A passing check leaves the stamp, keyed by the brief's
    sha256, that the remote-lane runner reads before it places anything. A command that timed out, did not start or
    reached no verdict is a brief that did not pass, with its reason.
    """

    def __init__(
        self,
        directory: Path,
        plugin_root: Path,
        settings: ModelBriefSettings,
        *,
        model: str,
        ticket: str,
        environ: Mapping[str, str],
        run: Run = subprocess.run,
    ) -> None:
        self._directory = directory
        self._root = plugin_root
        self._settings = settings
        self._model = model
        self._ticket = ticket
        self._environ = dict(environ)
        self._run = run

    def _command(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._settings.command_timeout_seconds,
                stdin=subprocess.DEVNULL,
                env=self._environ,
            )
        except subprocess.TimeoutExpired:
            return (
                -2,
                "",
                f"timed out after {self._settings.command_timeout_seconds:g} s",
            )
        except OSError as exc:
            return -1, "", f"did not start: {exc}"
        return done.returncode, done.stdout, done.stderr

    def _unread(self, what: str, rc: int, out: str, err: str) -> str:
        said = _last_line(err or out or "")
        return f"{what} exited {rc}: {said}" if rc >= 0 else f"{what} {said}"

    def _constraints(self, lane: str, lane_brief: list[str]) -> list[str]:
        settings = self._settings
        score = sum(int(digit) for digit in settings.route_dims[1::2])
        return [
            *lane_brief,
            "constraints",
            "--lane",
            lane,
            "--parent",
            settings.parent,
            "--landing-lane",
            settings.landing_lane,
            "--landing-ruling-lane",
            settings.landing_ruling_lane,
            "--landing-ruling",
            settings.landing_ruling,
            "--ticket",
            self._ticket,
            "--model",
            self._model,
            "--effort",
            settings.effort,
            "--repo",
            settings.repo,
            "--out-of-scope",
            settings.out_of_scope,
            "--band",
            settings.route_band,
            "--score",
            str(score),
            "--dims",
            settings.route_dims,
        ]

    def prepare(self, *, lane: str, name: str, text: str) -> ModelBriefPrepared:
        settings = self._settings
        path = self._directory / name
        unwritten = ModelBriefPrepared(
            path=str(path), passed=False, sha256="", detail=""
        )
        ledger = self._environ.get(settings.ledger_env, "")
        if not ledger:
            return unwritten.model_copy(
                update={
                    "detail": f"{settings.ledger_env} is not set: the lane-brief reads the ledger"
                }
            )
        lane_brief = [
            sys.executable,
            "-B",
            str(self._root / settings.lane_brief_script),
            "--ledger",
            ledger,
        ]
        rc, rules, err = self._command(
            [sys.executable, "-B", str(self._root / settings.rules_block_script)]
        )
        if rc != 0:
            return unwritten.model_copy(
                update={"detail": self._unread("lane_rules_block.py", rc, rules, err)}
            )
        rc, ticket_text, err = self._command(
            [*lane_brief, "ticket-text", "--ticket", self._ticket]
        )
        if rc != 0:
            return unwritten.model_copy(
                update={
                    "detail": self._unread(
                        f"lane_brief.py ticket-text {self._ticket}",
                        rc,
                        ticket_text,
                        err,
                    )
                }
            )
        reasons = "the lane-brief check ran no attempt"
        digest = ""
        for _ in range(settings.regenerate_attempts):
            rc, block, err = self._command(self._constraints(lane, lane_brief))
            if rc != 0:
                return unwritten.model_copy(
                    update={
                        "detail": self._unread(
                            "lane_brief.py constraints", rc, block, err
                        )
                    }
                )
            body = text.rstrip()
            route = _ROUTE_LINE.search(block)
            rows = body.splitlines()
            first = next(
                (i for i, row in enumerate(rows) if row.startswith("ROUTE: ")), None
            )
            if route and first is not None:
                rows[first] = route.group(0)
                body = "\n".join(rows)
            whole = (
                f"{body}\n\n## 3. Rules (committed standing block, verbatim)\n\n{rules.strip()}\n\n"
                f"{ticket_text.strip()}\n\n{block.strip()}\n"
            )
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
                path.write_text(whole, encoding="utf-8")
            except OSError as exc:
                return unwritten.model_copy(
                    update={"detail": f"brief not written: {exc}"}
                )
            digest = hashlib.sha256(whole.encode("utf-8")).hexdigest()
            rc, out, err = self._command(
                [
                    *lane_brief,
                    "check",
                    str(path),
                    "--recompute",
                    "--lab",
                    "--where",
                    settings.check_where,
                ]
            )
            if rc == 0:
                return ModelBriefPrepared(
                    path=str(path), passed=True, sha256=digest, detail=""
                )
            if rc < 0:
                reasons = self._unread("lane_brief.py check", rc, out, err)
                break
            refused = _REFUSED_LINE.findall(out) or [_last_line(err or out)]
            reasons = "; ".join(refused)
            if any(marker in reasons for marker in _NO_VERDICT):
                reasons = f"no verdict on the brief: {reasons}"
                break
            if not any(_REGENERATE in line for line in refused):
                break
        return ModelBriefPrepared(
            path=str(path), passed=False, sha256=digest, detail=reasons[:2000]
        )


class LedgerQueryLiveLanes:
    """Reads the lanes live for a cause from the rolling ledger over the bus (``onex ledger query``).

    A lane covers a cause when a CLAIM row of the lookback window names the cause key and no TERMINAL row
    of that lane follows. The host's own ledger file is not read: on a lab host it is a stale copy.
    """

    def __init__(
        self,
        query_command: tuple[str, ...],
        lookback_hours: float,
        row_limit: int,
        timeout_seconds: float,
        clock: Callable[[], datetime],
        run: Run = subprocess.run,
    ) -> None:
        self._command = query_command
        self._lookback = timedelta(hours=lookback_hours)
        self._limit = row_limit
        self._timeout = timeout_seconds
        self._clock = clock
        self._run = run

    def _rows(self, *flags: str) -> list[dict[str, str]]:
        since = (self._clock() - self._lookback).astimezone(UTC).strftime(_STAMP)
        argv = [
            *self._command,
            "--query",
            "rows",
            "--since",
            since,
            "--limit",
            str(self._limit),
            *flags,
        ]
        try:
            done = self._run(
                argv, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LiveLanesUnreadableError(
                f"ledger query: {type(exc).__name__}"
            ) from exc
        payload = next(
            (
                line
                for line in reversed(done.stdout.splitlines())
                if line.startswith("{")
            ),
            None,
        )
        if done.returncode != 0 or payload is None:
            reason = _last_line(done.stderr or done.stdout or "")
            raise LiveLanesUnreadableError(
                f"ledger query exited {done.returncode}: {reason}"
            )
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise LiveLanesUnreadableError("ledger query answered no JSON") from exc
        if answer.get("error") or answer.get("refusal"):
            raise LiveLanesUnreadableError(
                f"ledger query refused: {answer.get('error') or answer.get('refusal')}"
            )
        return [
            {
                "lane": str(row.get("row_lane") or ""),
                "type": str(row.get("row_type") or ""),
                "text": str(row.get("text") or ""),
            }
            for row in answer.get("rows") or []
        ]

    def read(self, cause_keys: tuple[str, ...]) -> ModelLiveLanes:
        causes: list[str] = []
        lanes: list[str] = []
        for key in cause_keys:
            for row in self._rows("--kind", "CLAIM", "--term", key):
                if row["type"] != "CLAIM" or key not in row["text"] or not row["lane"]:
                    continue
                if self._rows("--kind", "TERMINAL", "--lane", row["lane"]):
                    continue
                causes.append(key)
                lanes.append(row["lane"])
        return ModelLiveLanes(
            covered_causes=tuple(dict.fromkeys(causes)),
            covered_lanes=tuple(dict.fromkeys(lanes)),
        )


class SubprocessLanePlacer:
    """Starts one lane detached; a runner still running after the settle window is a lane that started."""

    def __init__(self, settle_seconds: float, log_directory: Path) -> None:
        self._settle = settle_seconds
        self._logs = log_directory

    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        try:
            self._logs.mkdir(parents=True, exist_ok=True)
            log = (
                self._logs / f"{Path(argv[argv.index('--lane') + 1]).name}-dispatch.log"
            )
            with log.open("ab") as output:
                runner = subprocess.Popen(
                    list(argv),
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    env=dict(os.environ),
                )
            try:
                code = runner.wait(timeout=self._settle)
            except subprocess.TimeoutExpired:
                return ModelPlaceOutcome(
                    started=True, detail="running past the settle window"
                )
            if code == 0:
                return ModelPlaceOutcome(started=True, detail="runner exited 0")
            return ModelPlaceOutcome(
                started=False, detail=f"runner exited {code}: {_last_log_line(log)}"
            )
        except (OSError, ValueError) as exc:
            return ModelPlaceOutcome(
                started=False,
                detail=f"dispatch failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}",
            )


def _last_line(text: str) -> str:
    """The last non-empty line of a command's output, the command's own reason for a refusal."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def _last_log_line(log: Path) -> str:
    try:
        lines = [
            line
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return "no log"
    return lines[-1][:200] if lines else "no output"


__all__ = [
    "FileDispatchRecord",
    "FileDriftOpenItems",
    "LaneBriefPreparer",
    "LedgerQueryLiveLanes",
    "SubprocessLanePlacer",
]
