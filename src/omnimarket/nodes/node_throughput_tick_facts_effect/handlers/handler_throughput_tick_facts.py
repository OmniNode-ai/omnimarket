# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly check's facts read as an effect (OMN-23734).

Behaviour-preserving port of what the retired throughput_tick.py read before it called the decision
compute: ``read_launchctl`` (the landing controller's launchd state and its PID's elapsed time),
``read_controller_files`` (the newest tick lines, the heartbeat and state.json), ``read_watcher_and_floors``
(the PR watcher's state, the landing floors, the logins that land like the operator),
``read_open_history`` and ``read_repo_status_units`` (the rows the record step keeps), and the goal
check's readers (the goal_scope overlay, the controller's admission rows and newest receipt reasons, the
last goal record). Every read goes through ``ProtocolTickFactsSource``; a read that fails is the error
text the script put on its request, never a raise and never an empty fact.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnimarket.models.model_throughput_goal_scope import ModelGoalScope
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.tick_facts_ports import (
    HostTickFactsSource,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelGoalFacts,
    ModelThroughputTickFacts,
    ModelThroughputTickFactsRequest,
    ModelThroughputTickFactsResult,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.protocols.protocol_tick_facts_source import (
    ProtocolTickFactsSource,
)

PID_RE = re.compile(r'"PID"\s*=\s*(\d+)\s*;')
HISTORY_FILE = "throughput-tick.open-history.json"
REPO_STATUS_FILE = "throughput-tick.repo-status.json"
GOAL_STATE_FILE = "goal-check.json"
ADMISSIONS_LOG = "admissions.jsonl"
ADMISSIONS_TAIL_BYTES = 4 * 1024 * 1024
RECEIPT_WINDOW = 1 << 16
RECEIPT_CEILING = 64 << 20
GOAL_OVERLAY_ID = "goal_scope"
CLOUD_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
GOAL_SCALARS = frozenset({"overlay_version", "ticket", "ruling"})
GOAL_LISTS = frozenset(
    {
        "in_scope_repos",
        "in_scope_prs",
        "prerequisite_prs",
        "paused_prs",
        "repo_work_kinds",
        "prerequisite_kinds",
    }
)
_GOAL_REF = re.compile(r"^[A-Za-z0-9_.-]+#\d+$")
_GOAL_NAME = re.compile(r"^(?:[A-Za-z0-9_.-]+|\*)$")


class FloorsError(Exception):
    """The floors file cannot be read as a floors file."""


def _json_dict(line: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_goal_overlay(text: str) -> dict[str, str | list[str]]:
    """``key: scalar`` lines and ``key:`` followed by ``- item`` lines (or ``key: []``); comments and blank lines allowed.
    Anything else is refused with its line number."""
    out: dict[str, str | list[str]] = {}
    current: list[str] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = (
            raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        )
        if not line.strip():
            continue
        if line.startswith("- "):
            if current is None:
                raise ValueError(
                    f"line {number}: a list item outside a list: {raw.strip()[:60]}"
                )
            current.append(line[2:].strip().strip("'\""))
            continue
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace():
            raise ValueError(
                f"line {number} is neither `key: value` nor a `- item`: {raw.strip()[:60]}"
            )
        value = value.strip().strip("'\"")
        if key.strip() in out:
            raise ValueError(f"line {number}: {key.strip()} is declared twice")
        if value == "[]" or not value:
            current = []
            out[key.strip()] = current
        else:
            out[key.strip()] = value
            current = None
    return out


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FloorsError(f"{name} must be a nonnegative finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise FloorsError(f"{name} is too large") from exc
    if not math.isfinite(number) or number < 0:
        raise FloorsError(f"{name} must be a nonnegative finite number")
    return number


class HandlerThroughputTickFacts:
    """Read what the tick needs and return it under the script's own key names."""

    def __init__(self, source: ProtocolTickFactsSource | None = None) -> None:
        self._source: ProtocolTickFactsSource = source or HostTickFactsSource()

    def handle(
        self, request: ModelThroughputTickFactsRequest
    ) -> ModelThroughputTickFactsResult:
        facts: dict[str, Any] = {}
        landing = Path(request.landing_state_dir).expanduser()
        state = Path(request.state_dir)
        self._launchctl(request, facts)
        self._controller_files(landing, facts)
        self._watcher_and_floors(request, facts)
        facts["open_history"] = self._open_history(state, request.open_basis)
        facts["repo_status"] = {
            "window_minutes": request.repo_status_window_minutes,
            "open_units": self._repo_status_units(state),
        }
        return ModelThroughputTickFactsResult(
            facts=ModelThroughputTickFacts(**facts),
            goal=self._goal(request, landing, state),
        )

    # ---- the decision compute's facts ----------------------------------------------------------------------------------

    def _launchctl(
        self, request: ModelThroughputTickFactsRequest, facts: dict[str, Any]
    ) -> None:
        try:
            loaded = self._source.run(
                [request.launchctl, "list", request.controller_label], 2
            )
        except OSError as exc:
            facts.update(launchctl="unavailable", launchctl_error=str(exc))
            return
        if loaded.returncode != 0:
            facts["launchctl"] = "not_loaded"
            return
        facts.update(launchctl="loaded", launchctl_stdout=loaded.stdout)
        match = PID_RE.search(loaded.stdout)
        if match is None:
            return
        try:
            out = self._source.run(
                [request.ps, "-o", "etime=", "-p", match.group(1)], 2
            )
        except OSError:
            return
        facts["pid_etime"] = out.stdout.strip() if out.returncode == 0 else None

    def _last_ticks(
        self, directory: Path, count: int, ceiling_bytes: int = 64 << 20
    ) -> list[dict[str, Any]]:
        """The newest ``count`` complete tick lines, newest first, whatever their length (the window doubles from 64 KiB)."""
        path = directory / "ticks.jsonl"
        size, window = self._source.read_tail(path, 0)[0], 65536
        while True:
            window = min(window, ceiling_bytes, size)
            _, data = self._source.read_tail(path, window)
            found = [
                d
                for d in map(_json_dict, reversed(data.splitlines()[size > window :]))
                if d is not None
            ][:count]
            if len(found) == count or window >= min(ceiling_bytes, size):
                return found
            window *= 2

    def _controller_files(self, landing: Path, facts: dict[str, Any]) -> None:
        try:
            facts["ticks"] = self._last_ticks(landing, 2)
        except (OSError, ValueError) as exc:
            facts.update(ticks=[], ticks_error=str(exc))
        with contextlib.suppress(OSError, ValueError):
            facts["heartbeat_text"] = self._source.read_text(landing / "heartbeat")
        with contextlib.suppress(OSError, ValueError):
            facts["state_json"] = json.loads(
                self._source.read_text(landing / "state.json")
            )

    def _floors(self, path: str | None) -> dict[str, float]:
        if path is None:
            raise FloorsError("cannot read floors: the request names no floors_path")
        try:
            data = json.loads(self._source.read_text(Path(path)))
            if not isinstance(data, dict) or not isinstance(data.get("per_repo"), dict):
                raise FloorsError("floors must be an object with a per_repo object")
            _nonnegative_number(data.get("default_per_hour"), "default_per_hour")
            per_repo: dict[str, float] = {}
            for repo, floor in data["per_repo"].items():
                if not repo:
                    raise FloorsError("per_repo must contain nonempty repository names")
                per_repo[repo] = _nonnegative_number(floor, f"per_repo.{repo}")
            return per_repo
        except (OSError, ValueError, FloorsError) as exc:
            raise FloorsError(f"cannot read floors {path}: {exc}") from exc

    def _watcher_and_floors(
        self, request: ModelThroughputTickFactsRequest, facts: dict[str, Any]
    ) -> None:
        if request.watcher_state_path is not None:
            facts["watcher_path"] = request.watcher_state_path
            try:
                facts["watcher_state"] = json.loads(
                    self._source.read_text(Path(request.watcher_state_path))
                )
            except (OSError, ValueError) as exc:
                facts["watcher_read_error"] = str(exc)
        try:
            facts["floors_per_repo"] = self._floors(request.floors_path)
        except FloorsError as exc:  # a floors file that cannot be read is the compute's UNKNOWN, never an OK
            facts["floors_error"] = str(exc)
            return
        # every author lands like the operator (RULING 2026-10-14T02:24:46Z): the floors check takes the logins that do,
        # so name every login the watcher state holds
        state = facts.get("watcher_state")
        prs = (state.get("prs") if isinstance(state, Mapping) else None) or {}
        logins = {
            str((pr.get("facts") or {}).get("author") or "") for pr in prs.values()
        }
        facts["land_like_operator"] = sorted(login for login in logins if login)

    def _open_history(self, state: Path, basis: str) -> list[dict[str, Any]]:
        """The open counts earlier runs recorded on the current basis, oldest first; an absent or unreadable file is no history."""
        try:
            points = json.loads(self._source.read_text(state / HISTORY_FILE))["history"]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        if not isinstance(points, list):
            return []
        return [
            {"at": p["at"], "open": p["open"]}
            for p in points
            if isinstance(p, dict)
            and isinstance(p.get("at"), str)
            and isinstance(p.get("open"), int)
            and p.get("basis") == basis
        ]

    def _repo_status_units(self, state: Path) -> list[dict[str, Any]]:
        """The status-check units the previous run left open; an absent or unreadable file is none."""
        try:
            units = json.loads(self._source.read_text(state / REPO_STATUS_FILE))[
                "open_units"
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        if not isinstance(units, list):
            return []
        return [
            {"repo": u["repo"], "opened_at": u["opened_at"], "verdict": u["verdict"]}
            for u in units
            if isinstance(u, dict)
            and all(isinstance(u.get(k), str) for k in ("repo", "opened_at", "verdict"))
        ]

    # ---- the goal check's facts ----------------------------------------------------------------------------------------

    def _goal(
        self, request: ModelThroughputTickFactsRequest, landing: Path, state: Path
    ) -> ModelGoalFacts:
        scope = self._goal_scope(request)
        admissions = self._admissions(landing)
        reasons = self._pr_reasons(landing)
        return ModelGoalFacts(
            scope=None if isinstance(scope, str) else scope,
            scope_unread=scope if isinstance(scope, str) else "",
            previous=self._goal_record(state),
            admissions={} if isinstance(admissions, str) else admissions,
            admissions_unread=admissions if isinstance(admissions, str) else "",
            pr_reasons=None if isinstance(reasons, str) else reasons,
            pr_reasons_unread=reasons if isinstance(reasons, str) else "",
        )

    def _goal_scope(
        self, request: ModelThroughputTickFactsRequest
    ) -> ModelGoalScope | str:
        """The declared scope, or the reason it cannot be read (the goal_scope overlay)."""
        roots = [
            *(r for r in request.overlay_roots.split(":") if r),
            *request.installed_roots,
        ]
        path = next(
            (
                c
                for root in roots
                if self._source.is_file(
                    c := Path(root) / GOAL_OVERLAY_ID / "overlay.yaml"
                )
            ),
            None,
        )
        if path is None:
            return (
                f"no {GOAL_OVERLAY_ID}/overlay.yaml under {CLOUD_ROOTS_ENV}={request.overlay_roots or 'unset'} "
                "or the installed onex-overlays plugin"
            )
        try:
            doc = parse_goal_overlay(self._source.read_text(path))
        except (OSError, UnicodeError, ValueError) as exc:
            return f"{path} is unreadable: {' '.join(str(exc).split())[:120]}"
        missing = sorted((GOAL_LISTS | {"ruling"}) - set(doc))
        unknown = sorted(set(doc) - GOAL_SCALARS - GOAL_LISTS)
        if missing or unknown:
            return f"{path} " + "; ".join(
                p
                for p in (
                    f"lacks {', '.join(missing)}" if missing else "",
                    f"carries unknown {', '.join(unknown)}" if unknown else "",
                )
                if p
            )
        if not isinstance(doc["ruling"], str):
            return f"{path}: ruling must be one line naming the goal"
        lists: dict[str, frozenset[str]] = {}
        for name in sorted(GOAL_LISTS):
            items = doc[name]
            if not isinstance(items, list):
                return f"{path}: {name} must be a list"
            pattern = _GOAL_REF if name.endswith("_prs") else _GOAL_NAME
            bad = [i for i in items if not pattern.match(i)]
            if bad:
                return f"{path}: {name} holds {len(bad)} malformed entr{'y' if len(bad) == 1 else 'ies'}: {bad[0][:40]}"
            lists[name] = frozenset(items)
        return ModelGoalScope(
            ruling=doc["ruling"],
            repos=lists["in_scope_repos"],
            prs=lists["in_scope_prs"],
            prerequisites=lists["prerequisite_prs"],
            paused=lists["paused_prs"],
            repo_work_kinds=lists["repo_work_kinds"],
            prerequisite_kinds=lists["prerequisite_kinds"],
        )

    def _admissions(self, landing: Path) -> dict[str, dict[str, Any]] | str:
        """The newest admission row the landing controller recorded per pull request (the last row of a pull request wins);
        a log not yet written is ``{}``, one that cannot be read is returned as its reason."""
        path = landing / ADMISSIONS_LOG
        try:
            _, raw = self._source.read_tail(path, ADMISSIONS_TAIL_BYTES)
            tail = raw.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            return f"{path} unreadable: {' '.join(str(exc).split())[:120]}"
        rows: dict[str, dict[str, Any]] = {}
        for line in tail.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(row, dict)
                and row.get("kind") == "admission"
                and row.get("event") in ("admitted", "kept_out")
                and row.get("pr")
            ):
                rows[str(row["pr"])] = row
        return rows

    def _pr_reasons(self, landing: Path) -> dict[str, str] | str:
        """The newest ticks.jsonl receipt's ``pr_reasons`` (``repo#n`` -> reason), or the reason it cannot be read."""
        path = landing / "ticks.jsonl"
        try:
            size, window = self._source.read_tail(path, 0)[0], RECEIPT_WINDOW
            while True:
                window = min(window, RECEIPT_CEILING, size)
                _, data = self._source.read_tail(path, window)
                lines = data.splitlines()[size > window :]
                for raw in reversed(lines):
                    try:
                        receipt = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(receipt, dict):
                        reasons = receipt.get("pr_reasons")
                        if not isinstance(reasons, Mapping):
                            return (
                                f"{path.name}: the newest receipt carries no pr_reasons"
                            )
                        return {
                            str(k).rsplit("/", 1)[-1]: str(v)
                            for k, v in reasons.items()
                        }
                if window >= min(RECEIPT_CEILING, size):
                    return f"{path.name}: no complete receipt in it"
                window *= 2
        except OSError as exc:
            return f"{path}: {exc.strerror or exc}"

    def _goal_record(self, state: Path) -> dict[str, Any] | None:
        try:
            doc = json.loads(self._source.read_text(state / GOAL_STATE_FILE))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None


__all__ = ["HandlerThroughputTickFacts", "parse_goal_overlay"]
