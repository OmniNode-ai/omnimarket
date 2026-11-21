# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabAdmission -- read live host state before a lane is placed (OMN-26214).

Four checks, run in order over what the caller read, each one a refusal with its evidence:

1. allowed models per host: a model of the gated engine (Codex) is refused unless the brief carries ``codexOk`` and the
   latest RULING allows it, and then only on a host whose table row has ``codex``; a lane that names no model
   gets the lane model policy's ceiling (sonnet).
2. parity dry-run: a pinned host whose dry-run is not ``ok`` is dropped and the lane is placed unpinned (a pin the
   brief requires is refused instead); a lane whose task repairs a host's parity is never pinned to it nor
   placed on it.
3. reconcile lock probe: a host whose lane-sandbox probe read one of the rules' errnos (30, the read-only
   registry) takes no lane. A host whose probe did not run is not judged by it.
4. same errno twice: a host or a lane class with ``repeat_threshold`` or more FRICTION rows naming the same errno in
   the last ``repeat_window_minutes`` takes no lane until a MERGE row for it, newer than those rows, clears it.

The handler is pure: the table rows, the parity state, the probe readings and the ledger rows arrive in the
request, and the rules are the contract's ``admission`` block.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    NODE_PACKAGE,
    HostTableError,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_admission import (
    AdmissionCheck,
    ModelAdmissionHostVerdict,
    ModelAdmissionRefusal,
    ModelAdmissionRequest,
    ModelAdmissionResult,
    ModelAdmissionRules,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    load_policy,
)

_ERRNO_RE = re.compile(r"\berrno[\s:=]*(\d+)\b", re.IGNORECASE)
_LANE_TAG_RE = re.compile(r"-\d{4}$")


def load_admission_rules() -> ModelAdmissionRules:
    """Read and check ``admission`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("admission")
    if not isinstance(block, dict):
        raise HostTableError("the lab placement contract declares no admission rules")
    try:
        return ModelAdmissionRules.model_validate(block)
    except ValidationError as exc:
        raise HostTableError(
            f"the lab admission rules are invalid: {exc.errors()[0]['msg']}"
        ) from None


def lane_family(lane: str) -> str:
    """The lane's class when none is declared: its name without the trailing ``-<HHMM>`` tag."""
    return _LANE_TAG_RE.sub("", lane)


@dataclass(frozen=True)
class LedgerRow:
    """One rolling-ledger row: ``<ts> | <TYPE> | key=value | ... | text``."""

    ts: datetime
    kind: str
    cells: dict[str, str]
    text: str
    raw: str


def parse_ledger_row(line: str) -> LedgerRow | None:
    """The row, or None for a line that is not one (a blank, a heading, a row with no readable timestamp)."""
    parts = [part.strip() for part in line.strip().split(" | ")]
    if len(parts) < 2:
        return None
    try:
        ts = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    cells: dict[str, str] = {}
    text: list[str] = []
    for part in parts[2:]:
        key, eq, value = part.partition("=")
        if eq and re.fullmatch(r"[a-z_]+", key):
            cells.setdefault(key, value)
        else:
            text.append(part)
    return LedgerRow(ts, parts[1], cells, " | ".join(text), line.strip())


def _errno_of(row: LedgerRow) -> int | None:
    match = _ERRNO_RE.search(row.text)
    return int(match.group(1)) if match else None


def _is_merge(row: LedgerRow) -> bool:
    """A MERGE row: the row type, or the STATUS row the ledger grammar files a merge under (``merged=`` cell)."""
    return row.kind == "MERGE" or (row.kind == "STATUS" and "merged" in row.cells)


class HandlerLabAdmission:
    """COMPUTE handler: admit one lane against the live state read for its candidate hosts."""

    def __init__(self, rules: ModelAdmissionRules | None = None) -> None:
        self._rules = rules or load_admission_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    @property
    def rules(self) -> ModelAdmissionRules:
        return self._rules

    def _same_errno_refusals(
        self, request: ModelAdmissionRequest, lane_class: str
    ) -> list[tuple[str, str, ModelAdmissionRefusal]]:
        """(kind, subject, refusal) for every host or class with enough same-errno rows and no clearing MERGE row."""
        rules = self._rules
        floor = request.now - timedelta(minutes=rules.repeat_window_minutes)
        rows = [r for r in map(parse_ledger_row, request.ledger_rows) if r is not None]
        merges = [r for r in rows if _is_merge(r)]
        # (kind, subject, errno) -> the FRICTION rows in the window that name it
        groups: dict[tuple[str, str, int], list[LedgerRow]] = defaultdict(list)
        hosts = {host.name for host in request.hosts}
        for row in rows:
            code = _errno_of(row)
            if (
                row.kind != "FRICTION"
                or code is None
                or not floor <= row.ts <= request.now
            ):
                continue
            host = row.cells.get("host", "")
            if host in hosts:
                groups[("host", host, code)].append(row)
            family = lane_family(row.cells.get("lane", ""))
            if family == lane_class:
                groups[("class", lane_class, code)].append(row)
        out: list[tuple[str, str, ModelAdmissionRefusal]] = []
        for (kind, subject, code), hits in sorted(groups.items()):
            if len(hits) < rules.repeat_threshold:
                continue
            newest = max(row.ts for row in hits)
            cleared = any(
                m.ts > newest
                and subject
                in (m.cells.get("host"), m.cells.get("class"), m.cells.get("clears"))
                for m in merges
            )
            if cleared:
                continue
            lanes = ",".join(sorted({row.cells.get("lane", "?") for row in hits}))
            out.append(
                (
                    kind,
                    subject,
                    ModelAdmissionRefusal(
                        check="same_errno",
                        subject=subject,
                        reason=(
                            f"{len(hits)} FRICTION rows naming Errno {code} for {kind} {subject} in the "
                            f"last {rules.repeat_window_minutes} minutes (lanes {lanes}) and no MERGE "
                            "row since the newest"
                        ),
                        evidence=tuple(row.raw for row in hits),
                    ),
                )
            )
        return out

    def handle(self, request: ModelAdmissionRequest) -> ModelAdmissionResult:
        rules = self._rules
        policy = load_policy()
        model = request.model or policy.ceiling
        gated = (
            policy.engines.get(policy.aliases.get(model, model)) == rules.gated_engine
        )
        lane_class = request.lane_class or lane_family(request.lane)
        refusals: list[ModelAdmissionRefusal] = []
        # per host: the (check, reason) pairs that keep it from taking the lane
        blocks: dict[str, list[tuple[AdmissionCheck, str]]] = {
            host.name: [] for host in request.hosts
        }

        # 1. allowed models per host
        if gated:
            if not (request.brief_codex_ok and request.ruling_allows_codex):
                refusals.append(
                    ModelAdmissionRefusal(
                        check="allowed_models",
                        subject="lane",
                        reason=(
                            f"model {model} needs the brief's codexOk and a RULING that allows it "
                            f"(codexOk={request.brief_codex_ok}, ruling_allows_codex="
                            f"{request.ruling_allows_codex}, ruling={request.ruling_ref or 'none cited'})"
                        ),
                    )
                )
            for host in request.hosts:
                if not host.codex:
                    blocks[host.name].append(
                        (
                            "allowed_models",
                            f"model {model} is not allowed on {host.name} (host table codex: false)",
                        )
                    )

        # 2. parity dry-run: a lane that repairs a host's parity is never placed on it
        repairs = set(request.repairs_parity_of)
        for host in request.hosts:
            if host.name in repairs:
                blocks[host.name].append(
                    (
                        "parity_dry_run",
                        f"the lane repairs {host.name}'s parity, so it is never placed on it",
                    )
                )
            elif host.parity == "modified":
                blocks[host.name].append(
                    (
                        "parity_dry_run",
                        f"parity dry-run failed on {host.name}: installed plugin differs from its shipped revision",
                    )
                )

        # 3. reconcile lock probe
        for host in request.hosts:
            if (
                host.lock_probe_read
                and host.lock_probe_errno in rules.refused_lock_errnos
            ):
                blocks[host.name].append(
                    (
                        "reconcile_lock_probe",
                        f"reconcile lock probe read Errno {host.lock_probe_errno} on {host.name}",
                    )
                )

        # 4. same errno twice
        class_refused = False
        for kind, subject, refusal in self._same_errno_refusals(request, lane_class):
            refusals.append(refusal)
            if kind == "host":
                blocks[subject].append(("same_errno", refusal.reason))
            else:
                class_refused = True

        # The pin: dropped (the lane is placed unpinned) when its host fails a check or its parity dry-run was not
        # read; refused when the brief requires the host.
        pin_dropped: str | None = None
        pin = request.pin
        if pin is not None:
            pinned = next((h for h in request.hosts if h.name == pin), None)
            why = [reason for _, reason in blocks.get(pin, [])]
            if pinned is None:
                why.append(f"{pin} is not a candidate host")
            elif pinned.parity == "unread":
                why.append(f"the parity dry-run on {pin} was not read")
            if why:
                if request.pin_required:
                    refusals.append(
                        ModelAdmissionRefusal(
                            check="parity_dry_run",
                            subject=pin,
                            reason=f"the brief requires {pin}: {'; '.join(why)}",
                        )
                    )
                else:
                    pin_dropped = (
                        f"{pin} dropped, lane placed unpinned: {'; '.join(why)}"
                    )
                pin = None
        for host_name, items in blocks.items():
            for check, reason in items:
                if (
                    check != "same_errno"
                ):  # a same-errno refusal was recorded with its rows above
                    refusals.append(
                        ModelAdmissionRefusal(
                            check=check, subject=host_name, reason=reason
                        )
                    )
        verdicts = tuple(
            ModelAdmissionHostVerdict(
                host=host.name,
                eligible=not blocks[host.name],
                reasons=tuple(reason for _, reason in blocks[host.name]),
            )
            for host in request.hosts
        )
        eligible = tuple(v.host for v in verdicts if v.eligible)
        pin_refused = request.pin_required and request.pin is not None and pin is None
        lane_refused = (
            class_refused or pin_refused or any(r.subject == "lane" for r in refusals)
        )
        admitted = not lane_refused and bool(eligible)
        return ModelAdmissionResult(
            admitted=admitted,
            model=model if admitted else None,
            pin=pin if admitted else None,
            pin_dropped=pin_dropped,
            eligible_hosts=eligible if admitted else (),
            hosts=verdicts,
            refusals=tuple(refusals),
        )


__all__ = [
    "HandlerLabAdmission",
    "LedgerRow",
    "lane_family",
    "load_admission_rules",
    "parse_ledger_row",
]
