# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic path + render/parse convention for the durable OCC observation
store (OMN-14888, resolves the OMN-14851 open storage-surface question:
Option A — git-committed append-only files in ``onex_change_control``).

Pure, zero-I/O module. Owns exactly the three ideas needed to make
:class:`~omnimarket.events.occ_observation_record.ModelOccObservationRecord`
(built, unmodified, in OMN-14851) a real file inside ``onex_change_control``:

  * :func:`occ_observation_record_relpath` — the deterministic, collision-free
    repo-relative path for one raw record, derived from the full append-only
    identity 6-tuple. One file per actual attempt, never overwritten (mirrors
    the existing ``drift/dod_receipts/<ticket>/<item>/command.yaml``
    file-per-record discipline already used in ``onex_change_control`` —
    net-negative-surface: no new path convention invented, the existing one is
    reused for a new record type under its own subtree).
  * :func:`render_occ_observation_record` — deterministic YAML bytes for one
    record (``sort_keys=True``, stable float/None representation via
    ``model_dump(mode="json")``), so two renders of the identical record are
    byte-identical (a precondition the write-EFFECT's append-only guard and the
    read-EFFECT's round-trip both rely on).
  * :func:`parse_occ_observation_record` — the exact inverse, used by the read
    side (``node_occ_observation_source_effect``) to reconstitute records from
    committed files.

WHERE these files physically live (a `onex_change_control` clone/checkout) is
decided by the caller (the write/read EFFECT nodes); this module never touches
the filesystem or network.
"""

from __future__ import annotations

import re

import yaml

from omnimarket.events.occ_observation_record import ModelOccObservationRecord

#: Root directory (repo-relative, inside `onex_change_control`) for the
#: append-only observation trail. Sibling of `drift/dod_receipts/` and
#: `contracts/` — a new subtree, not a new top-level convention.
OCC_OBSERVATIONS_ROOT = "drift/occ_observations"

#: Anything outside this set is replaced with "_" when building a path segment,
#: so a hostile/unexpected repo slug, policy version, or sha can never escape
#: the intended subtree (path traversal, extra directory levels) or collide via
#: separator confusion.
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
#: A run of 2+ dots (the path-traversal token, however it survived character
#: filtering) is collapsed separately so "../.." cannot reassemble across a
#: replaced "/" (e.g. "../../etc" -> "_.._.._etc", not "....__etc").
_DOT_RUN_RE = re.compile(r"\.{2,}")


def _safe_segment(value: str) -> str:
    """Pure: replace any character outside [A-Za-z0-9_.-] with '_', then
    collapse any surviving run of 2+ dots (path-traversal token) to '_'."""
    return _DOT_RUN_RE.sub("_", _SAFE_SEGMENT_RE.sub("_", value))


def occ_observation_record_relpath(record: ModelOccObservationRecord) -> str:
    """Pure: the deterministic, collision-free repo-relative path for one record.

    Shape: ``drift/occ_observations/<owner>__<repo>/pr-<n>/<head_sha>__<policy_version>__run<workflow_run_id>-<run_attempt>.yaml``

    Every path segment is built from the full append-only raw key (product_repo,
    product_pr_number, head_sha, policy_version, workflow_run_id, run_attempt),
    so two DIFFERENT raw attempts can never map to the same path, and the SAME
    raw attempt (re-ingested) always maps back to the SAME path (idempotent
    append-only: a re-run of the identical attempt is a no-op write, never a
    silent second row).
    """
    owner_repo = _safe_segment(record.product_repo.replace("/", "__"))
    head_sha = _safe_segment(record.head_sha)
    policy_version = _safe_segment(record.policy_version)
    filename = (
        f"{head_sha}__{policy_version}__"
        f"run{record.workflow_run_id}-{record.run_attempt}.yaml"
    )
    return (
        f"{OCC_OBSERVATIONS_ROOT}/{owner_repo}/pr-{record.product_pr_number}/{filename}"
    )


#: Line width handed to PyYAML so its wrapping decisions match the formatter that
#: gates the destination repo. `onex_change_control/.yamlfmt` sets
#: `max_line_length: 100`; PyYAML's default `width` is 80, so an unset width
#: produced files yamlfmt immediately rewrapped.
YAMLFMT_MAX_LINE_LENGTH = 100


def render_occ_observation_record(record: ModelOccObservationRecord) -> str:
    """Pure: deterministic YAML bytes for one record (stable across calls/hosts).

    The output is also YAMLFMT-STABLE against ``onex_change_control/.yamlfmt``
    (OMN-15300). These files are committed into that repo, whose pre-commit
    yamlfmt hook fails any file it would rewrite ("files were modified by this
    hook"). Two settings carry that:

      * ``explicit_start=True`` — the config sets ``include_document_start: true``,
        so a file without a leading ``---`` is rewritten on sight.
      * ``width=YAMLFMT_MAX_LINE_LENGTH`` — PyYAML defaults to 80 and yamlfmt
        wraps at 100, so every long ``reason`` string got rewrapped.

    Both are proven by ``test_render_is_yamlfmt_stable``, which runs the real
    yamlfmt binary over the rendered bytes and asserts zero modification.
    """
    payload = {"schema_version": "1.0.0", **record.model_dump(mode="json")}
    return yaml.safe_dump(
        payload,
        sort_keys=True,
        default_flow_style=False,
        explicit_start=True,
        width=YAMLFMT_MAX_LINE_LENGTH,
    )


def parse_occ_observation_record(text: str) -> ModelOccObservationRecord:
    """Pure: the exact inverse of :func:`render_occ_observation_record`.

    Ignores the injected ``schema_version`` envelope key (forward-compat: a
    reader from a future schema major can still reject on validation, not on an
    unexpected extra key at this parse boundary).
    """
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a YAML mapping, got {type(parsed).__name__}")
    body = {k: v for k, v in parsed.items() if k != "schema_version"}
    return ModelOccObservationRecord.model_validate(body)


__all__ = [
    "OCC_OBSERVATIONS_ROOT",
    "YAMLFMT_MAX_LINE_LENGTH",
    "occ_observation_record_relpath",
    "parse_occ_observation_record",
    "render_occ_observation_record",
]
