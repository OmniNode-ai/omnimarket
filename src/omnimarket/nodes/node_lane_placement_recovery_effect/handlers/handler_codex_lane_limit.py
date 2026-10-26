# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bench a host after a proven zero-turn Codex 429, without retaining engine text."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..models.model_codex_lane_limit import (
    ModelCodexLaneLimitRequest,
    ModelCodexLaneLimitResult,
)

CODEX_LIMITED_PREFIX = ".codex-limited-"
_HTTP_429 = re.compile(
    r"(?:last status[:=]?\s*429\b|HTTP(?:/\S+)?[:=]?\s+429\b|(?:http_status|status(?:_code)?)[\"']?\s*[:=]\s*429\b|429\s+Too Many Requests)",
    re.I,
)
_RESET = re.compile(
    r"(?:resets?_at|rate_limit_reset_at|x-ratelimit-reset)[\"']?\s*[:=]\s*[\"']?([0-9T:.+Z-]+)",
    re.I,
)
_RETRY = re.compile(r"retry-after[\"']?\s*[:=]\s*([^\r\n;\"}]+)", re.I)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return stamp.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic(path: Path, body: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".codex-limit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(body, stream, sort_keys=True)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _error_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return (
            json.dumps(value)
            + "\n"
            + "\n".join(_error_text(item) for item in value.values())
        )
    return ""


def _errors(path: Path, data: dict[str, Any]) -> str:
    # Raw tool output and prompts can quote 429s; only terminal engine events count.
    texts = [str(data.get("result") or ""), str(data.get("error") or "")]
    for log in (path.with_suffix(".out"), path.with_suffix(".log.jsonl")):
        try:
            with log.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        if re.match(r"^(?:Error: )?turn\.failed\b", line):
                            texts.append(line)
                        continue
                    if isinstance(event, dict) and event.get("type") in {
                        "turn.failed",
                        "error",
                    }:
                        texts.append(
                            _error_text(
                                event.get("error") or event.get("message") or ""
                            )
                        )
        except OSError:
            continue
    return "\n".join(texts)


def _reset(text: str, ended: datetime) -> datetime:
    candidates = []
    for match in _RESET.finditer(text):
        value = match.group(1)
        try:
            stamp = (
                datetime.fromtimestamp(float(value), UTC)
                if value.replace(".", "", 1).isdigit()
                else _time(value)
            )
            candidates.append(stamp)
        except (ValueError, OverflowError, OSError):
            continue
    for match in _RETRY.finditer(text):
        value = match.group(1).strip()
        try:
            stamp = (
                ended + timedelta(seconds=float(value))
                if value.replace(".", "", 1).isdigit()
                else parsedate_to_datetime(value)
            )
            if stamp.tzinfo is not None:
                candidates.append(stamp)
        except (ValueError, OverflowError, TypeError):
            continue
    return max(candidates) if candidates else ended + timedelta(hours=1)


class HandlerCodexLaneLimit:
    """Observe one final receipt; the marker and sanitized failure are idempotent."""

    def handle(self, request: ModelCodexLaneLimitRequest) -> ModelCodexLaneLimitResult:
        path = Path(request.receipt_path)
        data = _read(path)
        host = data.get("host")
        if (
            data.get("engine", data.get("model")) != "codex"
            or not (data.get("final") or request.completed_attempt)
            or not isinstance(host, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host)
        ):
            return ModelCodexLaneLimitResult()
        if (
            data.get("status") in {"done", "handed-off"}
            and not data.get("is_error")
            and not data.get("exit_code")
        ):
            return ModelCodexLaneLimitResult()
        text = _errors(path, data)
        if not _HTTP_429.search(text):
            return ModelCodexLaneLimitResult()
        now = request.now or datetime.now(UTC)
        try:
            ended = _time(str(data["ended_at"]))
        except (KeyError, ValueError):
            return ModelCodexLaneLimitResult()
        until = _reset(text, ended)
        failure = {
            "kind": "codex-429",
            "http_status": 429,
            "retryable": False,
            "avoid_hosts": [host],
            "until": _stamp(until),
            "reason": "Codex provider refused the lane with HTTP 429",
        }
        data.update(failure=failure)
        if type(data.get("num_turns")) is not int or data["num_turns"] != 0:
            _atomic(path, data)
            return ModelCodexLaneLimitResult(host=host, observed_429=True)
        if until <= now:
            _atomic(path, data)
            return ModelCodexLaneLimitResult(host=host, observed_429=True)
        directory = Path(request.placement_directory)
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / f"{CODEX_LIMITED_PREFIX}{host}"
        state = directory / ".limit-state" / f"{CODEX_LIMITED_PREFIX}{host}"
        state.mkdir(parents=True, exist_ok=True)
        with (state / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prior = _read(marker)
            with contextlib.suppress(KeyError, ValueError):
                until = max(until, _time(str(prior["until"])))
            _atomic(
                marker,
                {
                    "host": host,
                    "at": _stamp(ended),
                    "until": _stamp(until),
                    "reason": "codex-429",
                    "run_id": data.get("run_id"),
                },
            )
        failure["until"] = _stamp(until)
        _atomic(path, data)
        return ModelCodexLaneLimitResult(
            host=host, limited_until=_stamp(until), observed_429=True
        )
