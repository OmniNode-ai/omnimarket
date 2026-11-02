# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree cell of a CLAIM and of the TERMINAL that closes it.

Problem (fact). Worktree removal was only ever a timer: the merged-worktree reaper, the
morning prune and the six-hourly triage remove a tree after the fact, and nothing in a
lane's own closeout said what became of the tree it worked in. A CLAIM could omit its
worktree and a TERMINAL could say nothing about it, so the trees a lane left behind were
found again only by the next sweep (operator ruling 2026-09-28T15:08:59Z: worktree cleanup
is automatic, "the cleanup needs to be automatic").

Rule. On a NEW row in the governed ledger:

* a CLAIM carries ``worktree=<path>`` or ``worktree=none``;
* a TERMINAL carries a ``worktree=`` disposition when the CLAIM it closes named a path:
  ``removed:<path>``, ``pinned-removed:<path>@<archive>``, ``kept:<path>:<reason>`` or
  ``none`` (``none`` is refused when the closed CLAIM named a path: it is the one spelling
  that says nothing);
* a TERMINAL that says ``removed`` or ``pinned-removed`` is refused while the path still
  exists, and its path must be the one the closed CLAIM named.

The CLAIM a TERMINAL closes is ``closes-CLAIM=<stamp>`` (or ``closes=<stamp>``), else the
newest CLAIM of the same lane, preferring one on the same ticket. A CLAIM that cannot be
found (rolled into an archive) imposes nothing.

Exempt from the two REQUIRED cells, as for the TERMINAL delegation cell: a row whose
identity is ``actor=script:*``, ``actor=launchd:*`` or ``model=none``. A script lane has no
worktree of its own to dispose of. Such a row that does carry a cell is still judged.

THE HONEST LIMIT. This is shape plus one filesystem fact. ``removed:<path>`` while the path
is absent proves the tree is gone; it does not prove it was archived first. That half is
``onex-lane-closeout``, which prints the cell only after it has archived, verified and
removed the tree.

A path is ``$OMNI_HOME/...``, ``~/...``, an absolute path, or a path relative to
``$OMNI_HOME`` (or to the private data clone, whose location the ``repos.private_data_home_envs``
overlay key names). ``OMNI_HOME`` unset while a path needs it is a refusal, never a guess, and so
is an overlay that cannot name the private data clone: the refusal names the key.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_clone_home

OMNI_HOME_ENV = "OMNI_HOME"
WORKTREES_ROOT_ENV = "ONEX_WORKTREES_ROOT"
WORKTREES_DIR = "omni_worktrees"

NONE = "none"
NO_WORKTREE_WORDS = frozenset({"", "none", "n/a", "na", "-"})

REMOVED = "removed"
PINNED_REMOVED = "pinned-removed"
KEPT = "kept"

_EXEMPT_IDENTITY_RE = re.compile(r"(?:^|[\s|])(actor|model)=([^\s|]*)")
_VARIABLE_RE = re.compile(r"\$\{?OMNI_HOME\}?")

CLAIM_SHAPE = "worktree=<path> or worktree=none"
TERMINAL_SHAPE = (
    "worktree=removed:<path>, worktree=pinned-removed:<path>@<archive>, "
    "worktree=kept:<path>:<reason>, or worktree=none"
)


def exempt_identity(line: str) -> bool:
    """A script or launchd lane, or ``model=none``: no worktree of its own."""
    identities = dict(_EXEMPT_IDENTITY_RE.findall(line))
    return (
        identities.get("actor", "").startswith(("script:", "launchd:"))
        or identities.get("model") == "none"
    )


def names_a_worktree(value: str | None) -> bool:
    """Whether a CLAIM's ``worktree=`` value names a path (not none or a placeholder)."""
    return value is not None and value.strip().lower() not in NO_WORKTREE_WORDS


def path_refusal(raw: str) -> str | None:
    """Why ``raw`` is not a worktree path a reader can resolve, or None."""
    if not raw:
        return "is empty"
    if re.search(r"\s", raw):
        return "holds whitespace; a path is one token"
    if "," in raw:
        return (
            "holds a comma; name one path (the ticket directory covers several trees)"
        )
    if "<" in raw or ">" in raw:
        return "is a placeholder"
    if "/" not in raw:
        return (
            "is not a path (no '/'); write worktree=none when the lane has no worktree"
        )
    return None


def claim_refusal(values: list[str]) -> str | None:
    """The refusal for a CLAIM's ``worktree=`` cells, or None. ``values`` is every
    ``worktree=`` cell of the row."""
    if not values or not any(values):
        return f"CLAIM carries no worktree cell (OMN-20148): add {CLAIM_SHAPE}"
    if len(values) > 1:
        return "CLAIM carries more than one worktree= cell; name one path or none"
    value = values[0].strip()
    if value.lower() == NONE:
        return None
    if value.lower() in NO_WORKTREE_WORDS:
        return f"worktree={value!r} is a placeholder (OMN-20148): write {CLAIM_SHAPE}"
    why = path_refusal(value)
    if why is not None:
        return f"worktree={value!r} {why} (OMN-20148): write {CLAIM_SHAPE}"
    return None


class Disposition:
    """A parsed TERMINAL ``worktree=`` cell."""

    __slots__ = ("archive", "kind", "path", "reason")

    def __init__(
        self, kind: str, path: str = "", archive: str = "", reason: str = ""
    ) -> None:
        self.kind = kind
        self.path = path
        self.archive = archive
        self.reason = reason


def parse_disposition(value: str) -> tuple[Disposition | None, str | None]:
    """The disposition, or (None, why it is malformed)."""
    text = value.strip()
    if text.lower() == NONE:
        return Disposition(NONE), None
    kind, sep, rest = text.partition(":")
    kind = kind.lower()
    if not sep or kind not in {REMOVED, PINNED_REMOVED, KEPT}:
        return None, f"worktree={text!r} is not a disposition; write {TERMINAL_SHAPE}"
    if kind == REMOVED:
        why = path_refusal(rest)
        if why is not None:
            return None, f"worktree=removed:{rest!r} path {why}"
        return Disposition(REMOVED, path=rest), None
    if kind == PINNED_REMOVED:
        path, at, archive = rest.partition("@")
        if not at or not archive.strip():
            return None, (
                "worktree=pinned-removed needs the archive it was pinned to: "
                "pinned-removed:<path>@<archive>"
            )
        why = path_refusal(path)
        if why is not None:
            return None, f"worktree=pinned-removed:{path!r} path {why}"
        if re.search(r"\s", archive):
            return (
                None,
                "worktree=pinned-removed archive holds whitespace; it is one token",
            )
        return Disposition(PINNED_REMOVED, path=path, archive=archive), None
    path, colon, reason = rest.partition(":")
    why = path_refusal(path)
    if why is not None:
        return None, f"worktree=kept:{path!r} path {why}"
    if not colon or not reason.strip():
        return None, "worktree=kept needs the reason it was kept: kept:<path>:<reason>"
    return Disposition(KEPT, path=path, reason=reason.strip()), None


def terminal_shape_refusal(values: list[str]) -> str | None:
    """Shape of a TERMINAL's ``worktree=`` cells when it carries any, else None."""
    if not values:
        return None
    if len(values) > 1:
        return "TERMINAL carries more than one worktree= cell; one disposition"
    if not values[0].strip():
        return f"TERMINAL worktree= cell is empty (OMN-20148): write {TERMINAL_SHAPE}"
    _, why = parse_disposition(values[0])
    return why


def resolve(
    raw: str, env: Mapping[str, str] | None = None
) -> tuple[list[Path], str | None]:
    """Every location ``raw`` can mean, or ([], why it cannot be resolved)."""
    source = os.environ if env is None else env
    omni_raw = source.get(OMNI_HOME_ENV, "").strip()
    text = raw
    if _VARIABLE_RE.search(text):
        if not omni_raw:
            return (
                [],
                f"{OMNI_HOME_ENV} is not set, so {raw!r} cannot be checked (no default)",
            )
        text = _VARIABLE_RE.sub(omni_raw, text)
    if "$" in text:
        return [], f"{raw!r} holds a variable other than $OMNI_HOME"
    if text.startswith("~"):
        text = os.path.expanduser(text)
    path = Path(text)
    if path.is_absolute():
        return [path], None
    if not omni_raw:
        return (
            [],
            f"{OMNI_HOME_ENV} is not set, so the relative path {raw!r} cannot be checked",
        )
    registry_root = Path(omni_raw)
    found = [registry_root / path]
    try:
        private_data = resolve_clone_home(
            "repos.private_data_home_envs", environ=source, registry_root=registry_root
        )
    except NodeOverlayError as exc:
        return [], f"{exc}, so the relative path {raw!r} cannot be checked"
    found.append(Path(private_data) / path)
    worktrees_root = source.get(WORKTREES_ROOT_ENV, "").strip()
    if worktrees_root and path.parts[:1] == (WORKTREES_DIR,):
        found.append(Path(worktrees_root).joinpath(*path.parts[1:]))
    return found, None


def exists(raw: str, env: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """(whether any location ``raw`` can mean exists, why it cannot be checked)."""
    locations, why = resolve(raw, env)
    if why is not None:
        return False, why
    return any(os.path.lexists(p) for p in locations), None


def same_worktree(a: str, b: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether two spellings name one worktree."""
    if a.rstrip("/") == b.rstrip("/"):
        return True
    first, why_a = resolve(a, env)
    second, why_b = resolve(b, env)
    if why_a is not None or why_b is not None:
        return False
    normal = {os.path.normpath(p) for p in first}
    return any(os.path.normpath(p) in normal for p in second)


def disposition_refusal(
    disposition: Disposition, claimed: str | None, env: Mapping[str, str] | None = None
) -> str | None:
    """The state-dependent refusal for a parsed TERMINAL cell, or None. ``claimed`` is the
    path the closed CLAIM named, when it named one."""
    if disposition.kind == NONE:
        if claimed is not None:
            return (
                f"TERMINAL says worktree=none but the CLAIM it closes named {claimed} "
                f"(OMN-20148): account for it with removed:{claimed}, "
                f"pinned-removed:{claimed}@<archive> or kept:{claimed}:<reason>"
            )
        return None
    if claimed is not None and not same_worktree(disposition.path, claimed, env):
        return (
            f"TERMINAL worktree={disposition.kind}:{disposition.path} is not the worktree the "
            f"CLAIM it closes named ({claimed}) (OMN-20148)"
        )
    if disposition.kind in {REMOVED, PINNED_REMOVED}:
        present, why = exists(disposition.path, env)
        if why is not None:
            return f"TERMINAL says worktree={disposition.kind} but {why} (OMN-20148)"
        if present:
            return (
                f"TERMINAL says worktree={disposition.kind}:{disposition.path} but that path "
                "still exists (OMN-20148): run onex-lane-closeout --lane <lane> --worktree "
                "<path> and paste the cell it prints, or say worktree=kept:<path>:<reason>"
            )
    return None


def required_terminal_refusal(claimed: str) -> str:
    return (
        f"TERMINAL carries no worktree cell but the CLAIM it closes named {claimed} "
        f"(OMN-20148): add worktree=removed:{claimed}, worktree=pinned-removed:{claimed}@"
        f"<archive> or worktree=kept:{claimed}:<reason> (onex-lane-closeout prints the cell)"
    )
