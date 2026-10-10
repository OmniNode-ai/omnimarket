# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B PR claim registry: the cross-run mutual-exclusion lock for PR mutations.

Ported without a behaviour change from the omniclaude hook library module
``pr_claim_registry``. One JSON file per PR under the claims directory is the
shared lock; a claim is acquired by an atomic create-if-absent (write a unique
temp file, then ``os.link``), and an expired or malformed claim is reaped
through a unique tomb path that is revalidated, so a racing live claim is
preserved.

A claim is expired only when BOTH hold: the last heartbeat is more than
HEARTBEAT_STALE_MINUTES old and the claim itself is more than
CLAIMED_AT_STALE_HOURS old.

The handler reads no clock, environment variable or state directory: the
request carries the time, the claims directory and the instance id file. What
the old module printed is returned in ``messages``. Two deliberate differences
from the module: directory scans (cleanup, list) visit files in name order
where the module used filesystem order, and ``get_claim`` returns nothing for a
claim file that is not a JSON object where the module returned the value.
"""

import json
import os
import socket
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.pr_claim.model_pr_claim_registry_request import (
    require_valid_pr_key,
)
from omnimarket.nodes.node_pr_claim_registry_effect.models import (
    EnumPrClaimOperation,
    ModelPrClaim,
    ModelPrClaimRegistryRequest,
    ModelPrClaimRegistryResult,
)

HEARTBEAT_STALE_MINUTES = 30
CLAIMED_AT_STALE_HOURS = 2
_REAP_ATTEMPTS = 4
_TAG = "[claim-registry]"

_Claim = dict[str, object]


def canonical_pr_key(org: str, repo: str, number: int | str) -> str:
    """Build the canonical PR key ``<lowercase-org>/<lowercase-repo>#<number>``."""
    return f"{org.lower()}/{repo.lower()}#{number}"


def filesystem_key(pr_key: str) -> str:
    """Convert a canonical PR key to a safe filename stem.

    Rejects any key that is not ``<org>/<repo>#<number>`` so a caller-supplied
    key cannot escape the registry directory.
    """
    return require_valid_pr_key(pr_key).replace("/", "--").replace("#", "--")


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.rstrip("Z"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _minutes_since(ts: str, now: str) -> float:
    return (_parse_utc(now) - _parse_utc(ts)).total_seconds() / 60.0


def is_active(claim: Mapping[str, object], now: str) -> bool:
    """True while the claim has not expired; a malformed claim has."""
    heartbeat = claim.get("last_heartbeat_at", claim.get("claimed_at", ""))
    claimed = claim.get("claimed_at", "")
    if not heartbeat or not claimed:
        return False
    heartbeat_stale = _minutes_since(str(heartbeat), now) > HEARTBEAT_STALE_MINUTES
    claimed_old = _minutes_since(str(claimed), now) / 60.0 > CLAIMED_AT_STALE_HOURS
    return not (heartbeat_stale and claimed_old)


def _is_own_claim(
    claim: Mapping[str, object],
    run_id: str,
    lane_id: str | None,
    session_id: str | None = None,
) -> bool:
    """A live claim is the caller's when run, session and (if both name one) lane agree."""
    if claim.get("claimed_by_run") != run_id:
        return False
    held_session = claim.get("claimed_by_session")
    if held_session and session_id is not None and held_session != session_id:
        return False
    held_lane = claim.get("lane_id")
    return not held_lane or not lane_id or held_lane == lane_id


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _to_claim(raw: object) -> ModelPrClaim | None:
    if not isinstance(raw, dict):
        return None
    return ModelPrClaim(
        **{key: _text(raw.get(key)) for key in ModelPrClaim.model_fields}
    )


class HandlerPrClaimRegistry:
    def handle(
        self, request: ModelPrClaimRegistryRequest
    ) -> ModelPrClaimRegistryResult:
        run = _Run(request)
        match request.operation:
            case EnumPrClaimOperation.ACQUIRE:
                return run.result(run.acquire())
            case EnumPrClaimOperation.RELEASE:
                return run.result(run.release())
            case EnumPrClaimOperation.HEARTBEAT:
                return run.result(run.heartbeat())
            case EnumPrClaimOperation.CLEANUP_STALE_OWN_CLAIMS:
                keys = run.cleanup_stale_own_claims()
                return run.result(bool(keys), pr_keys=tuple(keys))
            case EnumPrClaimOperation.LIST_ACTIVE:
                claims = run.list_active_claims()
                return run.result(bool(claims), claims=claims)
            case EnumPrClaimOperation.HAS_ACTIVE:
                return run.result(run.has_active_claim())
            case EnumPrClaimOperation.GET_CLAIM:
                claim = run.get_claim()
                return run.result(claim is not None, claim=claim)


class _Run:
    """One operation's working state: the request, its directory and its messages."""

    def __init__(self, request: ModelPrClaimRegistryRequest) -> None:
        self._request = request
        self._dir = Path(request.claims_dir)
        self._messages: list[str] = []

    def result(
        self,
        succeeded: bool,
        *,
        pr_keys: tuple[str, ...] = (),
        claims: tuple[ModelPrClaim, ...] = (),
        claim: ModelPrClaim | None = None,
    ) -> ModelPrClaimRegistryResult:
        return ModelPrClaimRegistryResult(
            succeeded=succeeded,
            messages=tuple(self._messages),
            pr_keys=pr_keys,
            claims=claims,
            claim=claim,
        )

    def _say(self, message: str) -> None:
        self._messages.append(f"{_TAG} {message}")

    def _claim_path(self) -> Path:
        return self._dir / f"{filesystem_key(self._request.pr_key)}.json"

    def _active(self, claim: _Claim) -> bool:
        return is_active(claim, self._request.now)

    def _instance_id(self) -> str:
        raw = self._request.instance_id_path
        if not raw:
            return ""
        path = Path(raw)
        if path.exists():
            return path.read_text().strip()
        instance_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(instance_id)
        tmp.rename(path)
        return instance_id

    def _reap_inactive_claim(
        self, claim_file: Path, pr_key: str
    ) -> tuple[bool, _Claim | None]:
        """Move a presumed-inactive claim aside and revalidate it.

        Returns ``(True, None)`` when the path is clear for a new claim,
        ``(False, claim)`` when the tomb holds a live claim, and
        ``(False, None)`` when the reap could not be completed.
        """
        tomb_path = self._dir / f".{claim_file.name}.{uuid.uuid4().hex}.tomb"
        try:
            claim_file.rename(tomb_path)
        except FileNotFoundError:
            # Another acquirer reaped it first; the create below picks the winner.
            return True, None
        except OSError as e:
            self._say(f"Warning: could not reap claim for {pr_key}: {e}")
            return False, None

        try:
            try:
                tomb_claim: _Claim | None = json.loads(tomb_path.read_text())
            except json.JSONDecodeError:
                tomb_claim = None
            except OSError as e:
                # Moved but unverifiable: restore rather than delete a live claim.
                try:
                    os.link(tomb_path, claim_file)
                except FileExistsError:
                    pass
                except OSError as restore_error:
                    self._say(
                        f"Warning: could not restore unreadable claim for {pr_key}: "
                        f"{restore_error}"
                    )
                    return False, None
                tomb_path.unlink(missing_ok=True)
                self._say(
                    f"Warning: could not verify claim for {pr_key} while reaping: {e}"
                )
                return False, None

            if tomb_claim is not None and self._active(tomb_claim):
                # The pathname changed after the earlier read: put the live
                # record back unless another contender already installed one.
                try:
                    os.link(tomb_path, claim_file)
                except FileExistsError:
                    pass
                except OSError as e:
                    self._say(
                        f"Warning: could not restore live claim for {pr_key}: {e}"
                    )
                    return False, None
                tomb_path.unlink(missing_ok=True)
                return False, tomb_claim

            tomb_path.unlink()
            return True, None
        except OSError as e:
            self._say(f"Warning: could not finish reaping claim for {pr_key}: {e}")
            return False, None

    def acquire(self) -> bool:
        req = self._request
        pr_key, run_id, lane_id, session_id = (
            req.pr_key,
            req.run_id,
            req.lane_id,
            req.session_id,
        )
        host = req.host or socket.gethostname()
        instance_id = self._instance_id()
        if not host:
            raise ValueError("claimed_by_host is required but could not be resolved")
        if not instance_id:
            raise ValueError(
                "claimed_by_instance_id is required but could not be resolved"
            )
        if req.dry_run:
            return True

        self._dir.mkdir(parents=True, exist_ok=True)
        claim_file = self._claim_path()
        claim_data = {
            "pr_key": pr_key,
            "claimed_by_run": run_id,
            "claimed_by_host": host,
            "claimed_by_instance_id": instance_id,
            "claimed_at": req.now,
            "last_heartbeat_at": req.now,
            "action": req.action,
            "lane_id": lane_id,
            "claimed_by_session": session_id,
        }

        saw_create_collision = False
        for _attempt in range(_REAP_ATTEMPTS):
            existing: _Claim | None = None
            needs_reap = False
            try:
                existing = json.loads(claim_file.read_text())
                needs_reap = True
            except FileNotFoundError:
                pass
            except (json.JSONDecodeError, OSError) as e:
                needs_reap = True
                self._say(
                    f"Warning: could not read existing claim for {pr_key}: {e}. "
                    "Proceeding to reap."
                )

            if existing is not None:
                if self._active(existing):
                    if _is_own_claim(existing, run_id, lane_id, session_id):
                        return True  # idempotent re-acquire
                    collision = " concurrently" if saw_create_collision else ""
                    self._say(
                        f"PR {pr_key} is actively claimed{collision} by run "
                        f"{existing.get('claimed_by_run', 'unknown')} "
                        f"(lane: {existing.get('lane_id') or 'unknown'}) "
                        f"(action: {existing.get('action', 'unknown')}). Skipping."
                    )
                    return False
                self._say(
                    f"Expired claim for {pr_key} "
                    f"(run: {existing.get('claimed_by_run', 'unknown')}, "
                    f"heartbeat: {existing.get('last_heartbeat_at', 'unknown')}). "
                    "Proceeding to reap."
                )

            if needs_reap:
                reaped, raced_live_claim = self._reap_inactive_claim(claim_file, pr_key)
                if raced_live_claim is not None:
                    if _is_own_claim(raced_live_claim, run_id, lane_id, session_id):
                        return True
                    self._say(
                        f"PR {pr_key} is actively claimed by run "
                        f"{raced_live_claim.get('claimed_by_run', 'unknown')} "
                        f"(lane: {raced_live_claim.get('lane_id') or 'unknown'}) "
                        f"(action: {raced_live_claim.get('action', 'unknown')}). "
                        "Skipping."
                    )
                    return False
                if not reaped:
                    return False

            tmp_path = self._dir / f".tmp-{uuid.uuid4().hex}.json"
            try:
                tmp_path.write_text(json.dumps(claim_data, indent=2))
                try:
                    os.link(tmp_path, claim_file)
                except FileExistsError:
                    # A contender won between inspection and create: loop through
                    # the same active/expired checks.
                    saw_create_collision = True
                    continue
                return True
            except OSError as e:
                self._say(f"Warning: could not write claim for {pr_key}: {e}")
                return False
            finally:
                tmp_path.unlink(missing_ok=True)

        self._say(
            f"Warning: could not write or reap claim for {pr_key} after repeated "
            "concurrent changes"
        )
        return False

    def release(self) -> bool:
        req = self._request
        if req.dry_run:
            return False
        claim_file = self._claim_path()
        if not claim_file.exists():
            return False
        try:
            existing = json.loads(claim_file.read_text())
            if existing.get("claimed_by_run") != req.run_id:
                return False  # someone else's claim: never delete
            # Lanes in one session share the run id, so a matching run id alone
            # does not prove a live claim is the caller's (OMN-19696): when the
            # claim and the caller both name a lane, the lanes must match. An
            # expired claim is released on run id alone; acquire would reap it.
            if self._active(existing) and not _is_own_claim(
                existing, req.run_id, req.lane_id
            ):
                self._say(
                    f"Refusing to release live claim for {req.pr_key}: held by "
                    f"lane {existing.get('lane_id')}, caller lane {req.lane_id}."
                )
                return False
            claim_file.unlink()
            return True
        except FileNotFoundError:
            return False
        except (AttributeError, OSError, TypeError, ValueError) as e:
            self._say(
                f"Warning: could not release claim for {req.pr_key}: {e}. "
                "Heartbeat expiry will clean it up."
            )
            return False

    def heartbeat(self) -> bool:
        req = self._request
        if req.dry_run:
            return False
        claim_file = self._claim_path()
        if not claim_file.exists():
            return False
        try:
            existing = json.loads(claim_file.read_text())
            if existing.get("claimed_by_run") != req.run_id:
                return False
            existing["last_heartbeat_at"] = req.now
            tmp_path = claim_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(existing, indent=2))
            tmp_path.rename(claim_file)
            return True
        except (json.JSONDecodeError, OSError) as e:
            self._say(f"Warning: heartbeat failed for {req.pr_key}: {e}")
            return False

    def _claim_files(self) -> list[Path]:
        return sorted(self._dir.glob("*.json")) if self._dir.exists() else []

    def cleanup_stale_own_claims(self) -> list[str]:
        req = self._request
        deleted: list[str] = []
        for claim_file in self._claim_files():
            try:
                claim_data = json.loads(claim_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if claim_data.get("claimed_by_run") != req.run_id:
                continue
            if self._active(claim_data):
                continue  # may be in use
            pr_key = claim_data.get("pr_key", claim_file.stem)
            deleted.append(pr_key)
            if not req.dry_run:
                try:
                    claim_file.unlink(missing_ok=True)
                    self._say(
                        f"Cleaned up stale claim for {pr_key} (run: {req.run_id})"
                    )
                except OSError as e:
                    self._say(
                        f"Warning: could not clean up stale claim for {pr_key}: {e}"
                    )
        return deleted

    def list_active_claims(self) -> tuple[ModelPrClaim, ...]:
        active: list[ModelPrClaim] = []
        for claim_file in self._claim_files():
            try:
                claim_data = json.loads(claim_file.read_text())
                claim = _to_claim(claim_data) if self._active(claim_data) else None
            except (json.JSONDecodeError, OSError):
                continue
            if claim is not None:
                active.append(claim)
        return tuple(active)

    def has_active_claim(self) -> bool:
        claim_file = self._claim_path()
        if not claim_file.exists():
            return False
        try:
            return self._active(json.loads(claim_file.read_text()))
        except (json.JSONDecodeError, OSError):
            return False

    def get_claim(self) -> ModelPrClaim | None:
        claim_file = self._claim_path()
        if not claim_file.exists():
            return None
        try:
            return _to_claim(json.loads(claim_file.read_text()))
        except (json.JSONDecodeError, OSError):
            return None
