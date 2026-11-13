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
  whose OAuth login was revoked until it is re-added, with the mark naming
  whether the login was revoked or only answered a 403 challenge page
  (``challenged``: out of rotation like a revoke, and the pool tick re-tests
  it and clears the mark when it answers);
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
    ModelClaudeTokenPoolRefusal,
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
        lane_cap: int | None = None,
        lane_cap_read: bool = True,
    ) -> None:
        #: The overlay's lanes-per-label cap (``None``: no cap), and whether the overlay holding it was read
        #: on this host. The runtime reads both from the overlay; no cap is spelled here.
        self._lane_cap = lane_cap
        self._lane_cap_read = lane_cap_read
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
            cleared: tuple[str, ...] = ()
            limit_effect: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = (
                (),
                (),
                (),
            )
            revoke_effect: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = (
                (),
                (),
                (),
            )
            if request.operation == "add":
                self._add(pool, request)
            elif request.operation == "mark_limited":
                self._mark(pool, request)
            elif request.operation == "mark_revoked":
                alert = self._revoke(pool, request)
            elif request.operation == "reorder":
                self._reorder(pool, request)
            elif request.operation == "record_use":
                self._record_use(pool, request)
            elif request.operation == "pin":
                self._pin(pool, request)
            elif request.operation == "release":
                pool.pop("pinned", None)
                pool.pop("pinned_by", None)
            elif request.operation == "clear_revokes":
                cleared = self._clear_revokes(pool, request)
            elif request.operation == "apply_limit_rechecks":
                limit_effect = self._apply_limit_rechecks(pool, request)
            elif request.operation == "apply_revoke_rechecks":
                revoke_effect = self._apply_revoke_rechecks(pool, request)
            synced: tuple[str, ...] = ()
            if request.operation == "sync_marks":
                synced = self._sync_marks(pool, request)
            chosen, token = (None, None)
            refusal: ModelClaudeTokenPoolRefusal | None = None
            fallback = False
            if request.operation == "resolve":
                chosen, token, refusal, fallback = self._resolve(pool, request)
            if request.operation != "status" and not request.dry_run:
                _write_private(self._pool_path, pool)
            if request.operation == "export":
                token = self._export(pool, request)
            result = self._result(
                request.operation, pool, chosen, token, alert, synced, cleared
            ).model_copy(
                update={
                    "refusal": refusal,
                    "drained_fallback": fallback,
                    "limits_cleared": limit_effect[0],
                    "limits_replaced": limit_effect[1],
                    "limits_kept": limit_effect[2],
                    "revoke_rechecks_cleared": revoke_effect[0],
                    "revoke_rechecks_confirmed": revoke_effect[1],
                    "revoke_rechecks_kept": revoke_effect[2],
                }
            )
            if not request.dry_run:
                _write_private(
                    self._status_path,
                    {
                        "host": self._host,
                        "at": _stamp(self._now()),
                        "tokens": [
                            {
                                "label": e.label,
                                "until": e.until,
                                "revoked": e.revoked,
                                "fingerprint": e.fingerprint,
                                "revoked_at": e.revoked_at,
                                "limited_at": e.limited_at,
                                "probed_at": e.probed_at,
                                "revoke_cause": e.revoke_cause,
                            }
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
            "limited_at": None,
            "revoke_cause": None,
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
        # A notice that names its own reset (``exact``) is newer than any stamp the entry holds and replaces it, even
        # with an earlier time, so a stamp taken from a wrong or since-moved reset cannot outlast the account's real
        # one. A backoff estimate (no reset named) still never shortens a stamp.
        entry["until"] = (
            until if request.exact else max(until, entry.get("until") or "")
        )
        entry["limited_at"] = _stamp(self._now())
        if request.exact:
            entry["probed_at"] = None

    def _record_use(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> None:
        """A lane on this host started under ``label``: count the use and when it was seen."""
        label = self._label(request)
        entry = next((t for t in pool["tokens"] if t["label"] == label), None)
        if entry is None:
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        self._count_use(entry)

    def _count_use(self, entry: dict[str, Any]) -> None:
        entry["uses"] = entry.get("uses", 0) + 1
        entry["last_used_at"] = _stamp(self._now())

    def _pin(self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest) -> None:
        """Hold ``label`` first in the draw until released: ``resolve`` and every later ``reorder`` keep it first."""
        label = self._label(request)
        entry = next((t for t in pool["tokens"] if t["label"] == label), None)
        if entry is None:
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        if entry.get("revoked_at"):
            raise ClaudeTokenPoolRefusedError(
                f"{label} is revoked; re-add it with --force"
            )
        if (entry.get("until") or "") > _stamp(self._now()):
            raise ClaudeTokenPoolRefusedError(
                f"{label} is limited until {entry['until']}"
            )
        pool["pinned"] = label
        pool["pinned_by"] = request.pinned_by
        self._pinned_first(pool)

    @staticmethod
    def _pinned_first(pool: dict[str, Any]) -> None:
        """Move the pinned token, when the pool still holds it, to the head of the draw order."""
        head = pool.get("pinned")
        held = [t for t in pool["tokens"] if t["label"] == head]
        if held:
            pool["tokens"] = held + [t for t in pool["tokens"] if t["label"] != head]

    def _reorder(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> None:
        wanted = list(request.labels)
        if len(set(wanted)) != len(wanted):
            raise ClaudeTokenPoolRefusedError("reorder names a label twice")
        by_label = {t["label"]: t for t in pool["tokens"]}
        missing = [label for label in wanted if label not in by_label]
        if missing:
            raise ClaudeTokenPoolRefusedError(
                f"{', '.join(missing)} not in this host's pool"
            )
        rest = [t for t in pool["tokens"] if t["label"] not in set(wanted)]
        pool["tokens"] = [by_label[label] for label in wanted] + rest
        # a pin outlives every reorder: the rotation tick resets the order every few minutes
        self._pinned_first(pool)

    def _sync_marks(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> tuple[str, ...]:
        """Make the marks of every named label equal the source's; return the labels that changed.

        A local limit or revocation observed after the source wrote its snapshot (``marks_at``) is newer
        than the snapshot and is kept, so a host that recorded a real limit the source has not seen yet
        never loses it; a local mark older than the snapshot, or one with no recorded time, is cleared.
        """
        now = _stamp(self._now())
        snapshot = _stamp(request.marks_at) if request.marks_at else None
        observed = (
            snapshot or now
        )  # a mark taken from the source was observed when the source wrote it
        by_label = {t["label"]: t for t in pool["tokens"]}
        changed: list[str] = []
        for mark in request.marks:
            entry = by_label.get(mark.label)
            if entry is None:
                continue
            until = mark.until if mark.until and mark.until > now else None
            was_until = entry.get("until") if (entry.get("until") or "") > now else None
            was_revoked = bool(entry.get("revoked_at"))
            local_limit_newer = (
                snapshot is not None
                and was_until is not None
                and (entry.get("limited_at") or "") > snapshot
            )
            local_revoke_newer = (
                snapshot is not None
                and was_revoked
                and (entry.get("revoked_at") or "") > snapshot
            )
            if local_limit_newer:
                until = max(until or "", was_until or "") or None
            revoked = mark.revoked or local_revoke_newer
            if until != was_until or revoked != was_revoked:
                changed.append(mark.label)
            entry["until"] = until
            if until is not None and until != was_until:
                entry["limited_at"] = max(
                    mark.limited_at or observed, entry.get("limited_at") or ""
                )
            entry["revoked_at"] = (
                (entry.get("revoked_at") or mark.revoked_at or observed)
                if revoked
                else None
            )
            # The mark's cause travels with it. A local revoke newer than the snapshot keeps the
            # cause it was marked with (an entry without one, from before the field, stays without:
            # it reads as revoked); when revoked ends, so does the cause.
            mark_cause = getattr(mark, "revoke_cause", None)
            if revoked:
                if not local_revoke_newer:
                    entry["revoke_cause"] = mark_cause or "revoked"
            else:
                entry["revoke_cause"] = None
        return tuple(changed)

    def _clear_revokes(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> tuple[str, ...]:
        """Execute the staleness compute's typed clear commands; return the labels whose revoked mark was cleared.

        A command is executed only when the pool still holds the command's fingerprint under the label and the
        revoked stamp it holds is exactly the one the decision was made on, so a newer revoke, a re-added token
        or an already cleared mark is left alone.
        """
        by_label = {t["label"]: t for t in pool["tokens"]}
        cleared: list[str] = []
        for command in request.clear_commands:
            entry = by_label.get(command.label)
            if (
                entry is None
                or entry.get("fingerprint") != command.fingerprint
                or entry.get("revoked_at") != _stamp(command.expected_revoked_at)
            ):
                continue
            entry["revoked_at"] = None
            entry["revoke_cause"] = None
            cleared.append(command.label)
        return tuple(cleared)

    def _apply_limit_rechecks(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Execute the limit recheck compute's typed commands; return the labels cleared, replaced and kept.

        A command is executed only when the pool still holds the command's fingerprint under the label and the
        limited-until stamp it holds is exactly the one the decision was made on, so a newer limit notice, a
        re-added token or an already cleared mark is left alone (and is in none of the three results).
        """
        by_label = {t["label"]: t for t in pool["tokens"]}
        cleared: list[str] = []
        replaced: list[str] = []
        kept: list[str] = []
        for command in request.limit_commands:
            entry = by_label.get(command.label)
            if (
                entry is None
                or entry.get("fingerprint") != command.fingerprint
                or entry.get("until") != _stamp(command.expected_until)
            ):
                continue
            entry["probed_at"] = _stamp(command.probed_at)
            if command.action == "clear":
                entry["until"] = None
                entry["limited_at"] = None
                cleared.append(command.label)
            elif command.action == "replace" and command.new_until is not None:
                entry["until"] = _stamp(command.new_until)
                entry["limited_at"] = _stamp(command.probed_at)
                replaced.append(command.label)
            else:
                kept.append(command.label)
        return tuple(cleared), tuple(replaced), tuple(kept)

    def _apply_revoke_rechecks(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Execute the revoke recheck compute's typed commands; return the labels cleared, confirmed and kept.

        A command is executed only when the pool still holds the command's fingerprint under the label and the
        revoked stamp it holds is exactly the one the decision was made on, so a newer revoke, a re-added token
        or an already cleared mark is left alone (and is in none of the three results).
        """
        by_label = {t["label"]: t for t in pool["tokens"]}
        cleared: list[str] = []
        confirmed: list[str] = []
        kept: list[str] = []
        for command in request.revoke_commands:
            entry = by_label.get(command.label)
            if (
                entry is None
                or entry.get("fingerprint") != command.fingerprint
                or entry.get("revoked_at") != _stamp(command.expected_revoked_at)
            ):
                continue
            entry["probed_at"] = _stamp(command.probed_at)
            if command.action == "clear":
                entry["revoked_at"] = None
                entry["revoke_cause"] = None
                if command.new_until is not None:
                    # the probe answered ok but found the account limited: out until that reset
                    entry["until"] = _stamp(command.new_until)
                    entry["limited_at"] = _stamp(command.probed_at)
                cleared.append(command.label)
            elif command.action == "confirm":
                entry["revoke_cause"] = "revoked"
                confirmed.append(command.label)
            else:
                kept.append(command.label)
        return tuple(cleared), tuple(confirmed), tuple(kept)

    def _export(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> SecretStr:
        """The stored token of ``request.label`` for the caller's pipe; refused for a label this pool does not hold."""
        label = self._label(request)
        if not any(t["label"] == label for t in pool["tokens"]):
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        value = self.secret(label)
        if value is None:
            raise ClaudeTokenPoolRefusedError(
                f"{label} is in this host's pool but its stored token is missing"
            )
        return value

    def secret(self, label: str) -> SecretStr | None:
        """The stored token of ``label`` for the rotation tick to write to the host's login, never to a result."""
        value = asyncio.run(self._store.get_secret(SECRET_REF_PREFIX + label))
        return SecretStr(value) if value else None

    def _revoke(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> str | None:
        label = self._label(request)
        entry = next((t for t in pool["tokens"] if t["label"] == label), None)
        if entry is None:
            raise ClaudeTokenPoolRefusedError(f"{label} is not in this host's pool")
        if entry.get("revoked_at"):
            # A mark whose cause reads challenged (absent reads revoked) is a 403 challenge page,
            # not a real revoke: a later real revoke upgrades the cause and keeps the mark's time.
            if entry.get("revoke_cause") == "challenged" and request.cause == "revoked":
                entry["revoke_cause"] = "revoked"
                return self._revoked_alert(label)
            return None
        entry["revoked_at"] = _stamp(self._now())
        entry["revoke_cause"] = request.cause
        if request.cause == "challenged":
            return (
                f"ALERT claude-pool host={self._host} challenged={label}: a 403 challenge "
                "ended a lane on it; the pool tick re-tests it and clears the mark "
                "when it answers"
            )
        return self._revoked_alert(label)

    def _revoked_alert(self, label: str) -> str:
        return (
            f"ALERT claude-pool host={self._host} revoked={label}: its OAuth token "
            "was revoked; re-add it with `python -m "
            f"omnimarket.nodes.node_claude_token_pool_effect add --label {label} --force`"
        )

    def _resolve(
        self, pool: dict[str, Any], request: ModelClaudeTokenPoolRequest
    ) -> tuple[str | None, SecretStr | None, ModelClaudeTokenPoolRefusal | None, bool]:
        """The token a NEW lane start runs on: the label, its value, a refusal, and whether it is a drained fallback.

        A limited or revoked label is never returned. Of the rest, a label at the overlay's lane cap is
        out; a label in ``drain_labels`` is skipped while another label is left and is used (as a
        fallback) when it is the only one. Draw order decides among equals, as before.
        """
        now = _stamp(self._now())
        usable = [
            entry
            for entry in pool["tokens"]
            if not entry.get("revoked_at") and (entry.get("until") or "") <= now
        ]
        if not usable:
            # Nothing usable is the answer it always was (an empty pool, or every token limited or
            # revoked), never a refusal: a refusal says a usable token exists.
            return None, None, None, False
        counted = request.lane_counts
        if counted is not None and not self._lane_cap_read:
            return (
                None,
                None,
                ModelClaudeTokenPoolRefusal(
                    code="lane_cap_unread",
                    detail="the caller passed lane counts and this host's pool overlay, which holds the lane cap, was not read",
                ),
                False,
            )
        cap = self._lane_cap
        if cap is not None and counted is None:
            return (
                None,
                None,
                ModelClaudeTokenPoolRefusal(
                    code="lane_counts_missing",
                    cap=cap,
                    detail=f"the overlay caps a label at {cap} lanes and the caller passed no lane counts",
                ),
                False,
            )
        lanes = {row.label: row.lanes for row in counted or ()}
        open_labels = [
            entry
            for entry in usable
            if cap is None or lanes.get(entry["label"], 0) < cap
        ]
        drained = frozenset(request.drain_labels)
        ordered = [e for e in open_labels if e["label"] not in drained] + [
            e for e in open_labels if e["label"] in drained
        ]
        for entry in ordered:
            value = asyncio.run(
                self._store.get_secret(SECRET_REF_PREFIX + entry["label"])
            )
            if value:
                pool["current"] = entry["label"]
                # every draw is a lane about to start under the token: count it, so "unused" means undrawn
                self._count_use(entry)
                return (
                    entry["label"],
                    SecretStr(value),
                    None,
                    entry["label"] in drained,
                )
        if not open_labels and cap is not None:
            at_cap = tuple(e["label"] for e in usable)
            return (
                None,
                None,
                ModelClaudeTokenPoolRefusal(
                    code="label_lane_cap",
                    cap=cap,
                    labels=at_cap,
                    detail=f"every usable label already runs {cap} lanes or more: {', '.join(at_cap)}",
                ),
                False,
            )
        return None, None, None, False

    def _result(
        self,
        operation: Any,
        pool: dict[str, Any],
        chosen: str | None,
        token: SecretStr | None,
        alert: str | None = None,
        synced: tuple[str, ...] = (),
        cleared: tuple[str, ...] = (),
    ) -> ModelClaudeTokenPoolResult:
        now = _stamp(self._now())
        entries = tuple(
            ModelClaudeTokenPoolEntry(
                label=t["label"],
                fingerprint=t["fingerprint"],
                added_at=t["added_at"],
                until=t.get("until") if (t.get("until") or "") > now else None,
                limited=(t.get("until") or "") > now and not t.get("revoked_at"),
                revoked=bool(t.get("revoked_at")),
                uses=t.get("uses", 0),
                last_used_at=t.get("last_used_at"),
                revoked_at=t.get("revoked_at"),
                limited_at=t.get("limited_at"),
                probed_at=t.get("probed_at"),
                # an entry without a cause, from before the field, reads as revoked
                revoke_cause=(t.get("revoke_cause") or "revoked")
                if t.get("revoked_at")
                else None,
            )
            for t in pool["tokens"]
        )
        revoked = tuple(e.label for e in entries if e.revoked)
        challenged = tuple(
            e.label for e in entries if e.revoked and e.revoke_cause == "challenged"
        )
        return ModelClaudeTokenPoolResult(
            operation=operation,
            host=self._host,
            tokens=entries,
            chosen=chosen,
            pinned=pool.get("pinned"),
            pinned_by=pool.get("pinned_by"),
            token=token,
            revoked=revoked,
            challenged=challenged,
            synced=synced,
            cleared=cleared,
            alert=alert,
            **pool_summary(
                self._host,
                [(e.label, e.until) for e in entries],
                now,
                # the line names only the real revokes; the challenged labels follow separately
                revoked=tuple(
                    e.label
                    for e in entries
                    if e.revoked and e.revoke_cause == "revoked"
                ),
                challenged=challenged,
            ),
        )


def pool_summary(
    host: str,
    tokens: list[tuple[str, str | None]],
    now: str,
    *,
    revoked: Sequence[str] = (),
    challenged: Sequence[str] = (),
) -> dict[str, Any]:
    """Tokens, limits at ``now``, the next reset and the status line, including out-of-rotation labels.

    A revoked token is out of rotation for good, not until a reset, so it is named in ``revoked`` and is
    neither counted in ``limited`` nor offered as the ``next_reset``, even when it still carries a limit time.
    A challenged one (a 403 challenge page) is out the same way until the recheck clears it: it is named in
    ``challenged``, after ``revoked``, and is excluded from the limited counts and the ``next_reset`` alike.
    """
    gone = frozenset(revoked) | frozenset(challenged)
    untils = sorted(u for label, u in tokens if u and u > now and label not in gone)
    total, limited = len(tokens), len(untils)
    every = total > 0 and all((u or "") > now or label in gone for label, u in tokens)
    line = (
        f"claude-pool host={host} tokens={total} limited={limited} "
        f"next_reset={untils[0] if untils else 'none'}"
    )
    if revoked:
        line += f" revoked={','.join(revoked)}"
    if challenged:
        line += f" challenged={','.join(challenged)}"
    if every:
        # a challenged label is out of rotation exactly like a revoked one
        tail = " or revoked" if revoked or challenged else ""
        line = f"ALARM {line} every token limited{tail}"
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
