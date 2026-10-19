# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host's pool of Claude tokens, rotated when one account hits its usage limit (OMN-17427).

A remote lane authenticates with ``CLAUDE_CODE_OAUTH_TOKEN`` from one token file
per host, and when that account answers "You've hit your weekly limit" the whole
host is benched until the reset, although another account could carry its lanes.
This effect keeps several tokens per host instead:

* each token is an entry in the local secret store (the one ``onex secret``
  writes), under ``lab.claude-oauth.<label>``, the label naming its account;
* the pool file beside the store lists the labels in the order they were added,
  with each one's fingerprint prefix and the time its limit ends (no value);
* ``resolve`` hands the runner the first unrevoked token whose limit has passed,
  so the host stays placeable while any token has room, and ``mark_limited``
  marks the token a lane used, not the host; ``mark_revoked`` skips a token
  whose OAuth login was revoked until it is re-added;
* every operation rewrites a one-line status mirror (labels and limit times,
  never a value) that the placement probe reads, so the pool status line shows
  the tokens per host, how many are limited and the next reset, with an alarm
  when every token is limited or revoked.

The token file stays the runner's fallback: an empty pool, or one whose every
token is limited or revoked, resolves nothing and the runner keeps its old path.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import socket
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolEntry,
    ModelClaudeTokenPoolRequest,
    ModelClaudeTokenPoolResult,
)

SECRET_REF_PREFIX = "lab.claude-oauth."
POOL_FILE_NAME = "claude_token_pool.json"
STATUS_MIRROR = Path.home() / ".cache" / "omni" / "claude-token-pool.json"
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# A long-lived token as ``claude setup-token`` prints it. A terminal wraps a long
# line, so a run of token characters alone on the next line continues it.
_TOKEN_START = re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_-]+")
_TOKEN_CONT = re.compile(r"^[A-Za-z0-9_-]+$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MIN_TOKEN_LEN = 40


class ClaudeTokenPoolRefusedError(Exception):
    """A request the pool refuses. Its text names the label, never a token."""


def fingerprint(value: str) -> str:
    """The first 8 hex characters of sha256(value): tells tokens apart, cannot recover one."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def extract_token(text: str) -> str:
    """The one token in pasted ``claude setup-token`` output (or a bare token), joined across terminal wraps."""
    lines = _ANSI.sub("", text).replace("\r", "").split("\n")
    found: list[str] = []
    for i, line in enumerate(lines):
        for match in _TOKEN_START.finditer(line):
            token = match.group(0)
            if match.end() == len(line.rstrip()):
                for following in lines[i + 1 :]:
                    piece = following.strip()
                    if not piece or not _TOKEN_CONT.match(piece):
                        break
                    token += piece
            found.append(token)
    distinct = sorted(set(found))
    if not distinct:
        raise ClaudeTokenPoolRefusedError(
            "no Claude token (sk-ant-oat...) in the pasted output; paste everything "
            "`claude setup-token` printed after it finished"
        )
    if len(distinct) > 1:
        raise ClaudeTokenPoolRefusedError(
            f"{len(distinct)} different tokens in the pasted output; paste one at a time"
        )
    if len(distinct[0]) < _MIN_TOKEN_LEN:
        raise ClaudeTokenPoolRefusedError(
            "the token in the pasted output is cut short; paste it whole"
        )
    return distinct[0]


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _write_private(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, sort_keys=True) + "\n")
    os.replace(tmp, path)


class HandlerClaudeTokenPool:
    """Adds, resolves, marks and reads this host's Claude tokens."""

    def __init__(
        self,
        *,
        store: LocalByokCredentialStore | None = None,
        pool_path: Path | None = None,
        status_path: Path | None = None,
        host: str | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store or LocalByokCredentialStore()
        self._pool_path = pool_path or self._store.db_path.parent / POOL_FILE_NAME
        self._status_path = status_path or STATUS_MIRROR
        self._host = host or socket.gethostname().split(".")[0]
        self._now = now

    def handle(
        self, request: ModelClaudeTokenPoolRequest
    ) -> ModelClaudeTokenPoolResult:
        with self._locked() as pool:
            alert = None
            if request.operation == "add":
                self._add(pool, request)
            elif request.operation == "mark_limited":
                self._mark(pool, request)
            elif request.operation == "mark_revoked":
                alert = self._revoke(pool, request)
            chosen, token = (None, None)
            if request.operation == "resolve":
                chosen, token = self._resolve(pool)
            if request.operation != "status":
                _write_private(self._pool_path, pool)
            result = self._result(request.operation, pool, chosen, token, alert)
            _write_private(
                self._status_path,
                {
                    "host": self._host,
                    "at": _stamp(self._now()),
                    "tokens": [
                        {"label": e.label, "until": e.until, "revoked": e.revoked}
                        for e in result.tokens
                    ],
                },
            )
        return result

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self._pool_path.parent.mkdir(parents=True, exist_ok=True)
        with self._pool_path.with_name(self._pool_path.name + ".lock").open(
            "a"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                body = json.loads(self._pool_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                body = {}
            except ValueError as bad:
                raise ClaudeTokenPoolRefusedError(
                    f"{self._pool_path} is not JSON: {bad}"
                ) from None
            if not isinstance(body.get("tokens"), list):
                body["tokens"] = []
            yield body

    def _label(self, request: ModelClaudeTokenPoolRequest) -> str:
        label = request.label or ""
        if not LABEL_RE.match(label):
            raise ClaudeTokenPoolRefusedError(
                f"label {label!r} must be lower case letters, digits and hyphens, naming the account"
            )
        return label

    def _add(self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest) -> None:
        label = self._label(request)
        pasted = (
            request.setup_token_output.get_secret_value()
            if request.setup_token_output
            else ""
        )
        token = extract_token(pasted)
        known = next((t for t in pool["tokens"] if t["label"] == label), None)
        if known is not None and not request.force:
            raise ClaudeTokenPoolRefusedError(
                f"{label} is already in the pool (fingerprint {known['fingerprint']}); pass --force to replace it"
            )
        digest = fingerprint(token)
        clash = next(
            (
                t
                for t in pool["tokens"]
                if t["fingerprint"] == digest and t["label"] != label
            ),
            None,
        )
        if clash is not None:
            raise ClaudeTokenPoolRefusedError(
                f"this token is already in the pool as {clash['label']}"
            )
        asyncio.run(self._store.set_secret(SECRET_REF_PREFIX + label, token))
        entry = {
            "label": label,
            "fingerprint": digest,
            "added_at": _stamp(self._now()),
            "until": None,
            "revoked_at": None,
        }
        if known is not None:
            known.update(entry)
        else:
            pool["tokens"].append(entry)

    def _mark(self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest) -> None:
        label = self._label(request)
        if request.seconds is None:
            raise ClaudeTokenPoolRefusedError(
                "mark_limited needs the seconds until the limit resets"
            )
        entry = next((t for t in pool["tokens"] if t["label"] == label), None)
        if entry is None:
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        until = _stamp(self._now() + timedelta(seconds=request.seconds))
        entry["until"] = max(until, entry.get("until") or "")

    def _revoke(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> str | None:
        label = self._label(request)
        entry = next((t for t in pool["tokens"] if t["label"] == label), None)
        if entry is None:
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        if entry.get("revoked_at"):
            return None
        entry["revoked_at"] = _stamp(self._now())
        return (
            f"ALERT claude-pool host={self._host} revoked={label}: its OAuth token "
            "was revoked; re-add it with `python -m "
            f"omnimarket.nodes.node_claude_token_pool_effect add --label {label} --force`"
        )

    def _resolve(self, pool: dict[str, Any]) -> tuple[str | None, SecretStr | None]:
        now = _stamp(self._now())
        for entry in pool["tokens"]:
            if entry.get("revoked_at") or (entry.get("until") or "") > now:
                continue
            value = asyncio.run(
                self._store.get_secret(SECRET_REF_PREFIX + entry["label"])
            )
            if value:
                pool["current"] = entry["label"]
                return entry["label"], SecretStr(value)
        return None, None

    def _result(
        self,
        operation: Any,
        pool: dict[str, Any],
        chosen: str | None,
        token: SecretStr | None,
        alert: str | None = None,
    ) -> ModelClaudeTokenPoolResult:
        now = _stamp(self._now())
        entries = tuple(
            ModelClaudeTokenPoolEntry(
                label=t["label"],
                fingerprint=t["fingerprint"],
                added_at=t["added_at"],
                until=t.get("until") if (t.get("until") or "") > now else None,
                limited=(t.get("until") or "") > now,
                revoked=bool(t.get("revoked_at")),
            )
            for t in pool["tokens"]
        )
        revoked = tuple(e.label for e in entries if e.revoked)
        return ModelClaudeTokenPoolResult(
            operation=operation,
            host=self._host,
            tokens=entries,
            chosen=chosen,
            token=token,
            revoked=revoked,
            alert=alert,
            **pool_summary(
                self._host, [(e.label, e.until) for e in entries], now, revoked=revoked
            ),
        )


def pool_summary(
    host: str,
    tokens: list[tuple[str, str | None]],
    now: str,
    *,
    revoked: Sequence[str] = (),
) -> dict[str, Any]:
    """Tokens, limits at ``now``, the next reset and the status line, including revoked labels."""
    untils = sorted(u for _, u in tokens if u and u > now)
    total, limited = len(tokens), len(untils)
    every = total > 0 and all(
        (u or "") > now or label in revoked for label, u in tokens
    )
    line = (
        f"claude-pool host={host} tokens={total} limited={limited} "
        f"next_reset={untils[0] if untils else 'none'}"
    )
    if revoked:
        line += f" revoked={','.join(revoked)}"
    if every:
        line = f"ALARM {line} every token limited{' or revoked' if revoked else ''}"
    return {
        "limited": limited,
        "all_limited": every,
        "next_reset": untils[0] if untils else None,
        "status_line": line,
    }


__all__ = [
    "SECRET_REF_PREFIX",
    "ClaudeTokenPoolRefusedError",
    "HandlerClaudeTokenPool",
    "extract_token",
    "fingerprint",
    "pool_summary",
]
