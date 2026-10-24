# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release and pin guard decision (OMN-18594). Pure: no file, git, clock or env read.

The omni plugin's PreToolUse hook runs a standard-library copy of the decision
functions below (src/omni/nodes/node_release_pin_guard_compute); this module is
their canonical home and adds the typed handler.

RULING 2026-10-23T17:13:55Z: releases are created by the automated overnight
release train only; a session or lane cannot create a release, a version tag
or a pin. "One-off releases can be created with a grant from me or [the cloud owner]." A
grant is an OPERATOR-CONSENT row on the rolling work ledger with
``approved_by=operator`` or ``approved_by=cloud-owner``, one or more
``grant=<release|pin>:<repo>@<version>`` cells and an ``expires=<UTC>`` cell
that has not passed.

Every decision is a function of the tool call, the grant
rows and the time, all passed in.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from omnimarket.models.model_release_pin_guard import (
    ModelReleasePinGuardRequest,
    ModelReleasePinGuardResult,
)

RULING = "RULING 2026-10-23T17:13:55Z"
TICKET = "OMN-18594"
GRANT_APPROVERS = frozenset({"operator", "cloud-owner"})

# Internal siblings: every OmniNode package and repository name the registry
# ships (omnibase_*, omni*, onex_change_control), in either separator form.
_SIBLING = r"(?:omni[a-z0-9]*(?:[-_][a-z0-9]+)*|onex[-_]change[-_]control)"
_SHA = r"[0-9a-f]{40}"
_VERSION = r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[.\-+][0-9A-Za-z.\-+]*)?"

_RELEASE_SKILLS = frozenset({"omni:release-cut", "release-cut", "onex:release"})
_RELEASE_WORKFLOW = re.compile(
    r"(release|train|cascade|pin[-_]?(refresh|bump|cascade)|publish|dev[-_]merge[-_]tag)",
    re.IGNORECASE,
)
_VERSION_TAG = re.compile(rf"^(?:refs/tags/)?{_VERSION}$")
_SHA_ONLY = re.compile(rf"^{_SHA}$")

# Pin shapes, one line at a time: a 40-hex sha or a version written against an
# internal sibling.
_PIN_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # git URLs, reusable-workflow refs and lock sources: OmniNode-ai/<repo>...@<sha|tag> or #<sha>
    re.compile(
        rf"OmniNode-ai/(?P<name>{_SIBLING})[^\s\"'@#]*[@#](?P<value>{_SHA}|{_VERSION}|dev-[0-9TZ]+-[0-9a-f]{{7,40}})",
        re.IGNORECASE,
    ),
    # PEP 508 requirement on a sibling: omnibase-core==0.47.31, omnibase-core>=0.47,<0.48
    re.compile(
        rf"(?<![\w./-])(?P<name>{_SIBLING})\s*(?:\[[^\]]*\])?\s*(?P<value>(?:==|>=|<=|~=|!=|<|>)\s*[0-9][0-9A-Za-z.*+\-]*(?:\s*,\s*(?:==|>=|<=|~=|!=|<|>)\s*[0-9][0-9A-Za-z.*+\-]*)*)",
        re.IGNORECASE,
    ),
    # pin maps and contract pins: omnimarket: <sha>, omnimarket_contract_ref: <sha>
    re.compile(
        rf"^\s*[\"']?(?P<name>{_SIBLING})(?:[-_][a-z0-9_]*)?[\"']?\s*:\s*[\"']?(?P<value>{_SHA})",
        re.IGNORECASE,
    ),
    # checkout refs and uv sources: ref: <sha>, rev = "<sha|tag>", tag = "<tag>"
    re.compile(
        rf"(?:^|[\s{{,])(?P<name>(?:[\w-]*_)?ref|rev|tag)\s*[:=]\s*[\"']?(?P<value>{_SHA}|{_VERSION}|dev-[0-9TZ]+-[0-9a-f]{{7,40}})\b",
        re.IGNORECASE,
    ),
)

# Files whose content can carry a pin. Tests and prose are not pins.
_PIN_FILE = re.compile(
    r"(^|/)(pyproject\.toml|uv\.lock|requirements[^/]*\.txt|constraints[^/]*\.txt|Dockerfile[^/]*|[^/]*pin[^/]*\.(ya?ml|json|toml))$"
    r"|(^|/)\.github/.*\.(ya?ml|json)$",
    re.IGNORECASE,
)
_NOT_PIN_FILE = re.compile(r"(^|/)(tests?|docs?|fixtures)/|\.md$", re.IGNORECASE)


@dataclass(frozen=True)
class Grant:
    """One unexpired grant cell from an OPERATOR-CONSENT row."""

    kind: str  # release | pin
    repo: str
    version: str
    expires: datetime
    row_stamp: str


@dataclass(frozen=True)
class Decision:
    """The guard's answer for one tool call."""

    refuse: bool
    shape: str = ""  # what the call does, when it is a guarded shape
    repo: str = ""
    version: str = ""
    grant: str = ""  # the stamp of the grant row that admitted it
    reason: str = ""
    details: tuple[str, ...] = field(default_factory=tuple)


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


# --------------------------------------------------------------------------- grants


_STAMP = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|")
_GRANT_CELL = re.compile(r"\bgrant=([^|\s]+)")
_GRANT_ITEM = re.compile(rf"^(release|pin):({_SIBLING})@([^,@\s]+)$", re.IGNORECASE)
_APPROVED_BY = re.compile(r"\bapproved_by=([\w-]+)")
_EXPIRES = re.compile(r"\bexpires=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)")


def _parse_utc(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_grants(rows: Iterable[str], now: datetime) -> tuple[Grant, ...]:
    """Unexpired grants from ledger rows. ``now`` is naive UTC.

    A row counts only when it is an OPERATOR-CONSENT row, names an approver in
    GRANT_APPROVERS, carries an expiry in the future and at least one grant
    item that names a repo and a version. Anything else grants nothing.
    """
    grants: list[Grant] = []
    for row in rows:
        if "| OPERATOR-CONSENT |" not in row:
            continue
        stamp = _STAMP.match(row)
        approver = _APPROVED_BY.search(row)
        expiry = _EXPIRES.search(row)
        if not stamp or not approver or not expiry:
            continue
        if approver.group(1).lower() not in GRANT_APPROVERS:
            continue
        expires = _parse_utc(expiry.group(1))
        granted = _parse_utc(stamp.group(1))
        if expires is None or granted is None or expires <= now or granted > now:
            continue
        for cell in _GRANT_CELL.findall(row):
            for item in cell.split(","):
                m = _GRANT_ITEM.match(item.strip())
                if not m or m.group(3) in {"*", "any", ""}:
                    continue
                grants.append(
                    Grant(
                        kind=m.group(1).lower(),
                        repo=_norm(m.group(2)),
                        version=m.group(3).lstrip("v"),
                        expires=expires,
                        row_stamp=stamp.group(1),
                    )
                )
    return tuple(grants)


def _find_grant(
    grants: Sequence[Grant], kind: str, repo: str, version: str
) -> Grant | None:
    """A grant of ``kind`` for ``repo``; when ``version`` is known it must match."""
    want = version.removeprefix("==").lstrip("v")
    for g in grants:
        if g.kind != kind or g.repo != _norm(repo):
            continue
        if want and g.version != want:
            continue
        return g
    return None


# --------------------------------------------------------------------------- pins


def pin_tokens(path: str, text: str) -> frozenset[tuple[str, str]]:
    """Every (name, value) pin written in ``text`` when ``path`` can carry a pin."""
    if not path or not _PIN_FILE.search(path) or _NOT_PIN_FILE.search(path):
        return frozenset()
    out: set[tuple[str, str]] = set()
    for line in text.splitlines():
        for pattern in _PIN_LINE_PATTERNS:
            for m in pattern.finditer(line):
                value = re.sub(r"\s+", "", m.group("value"))
                out.add((_norm(m.group("name")), value))
    return frozenset(out)


def _pin_target_repo(name: str, path: str, line_context: str) -> str:
    """The sibling a pin names. A bare ``ref:``/``rev``/``tag`` names none itself."""
    if re.fullmatch(_SIBLING, name, re.IGNORECASE):
        return name
    m = re.search(rf"({_SIBLING})", line_context, re.IGNORECASE)
    return _norm(m.group(1)) if m else ""


# --------------------------------------------------------------------------- shapes


def _words(command: str) -> list[list[str]]:
    """The command split into simple commands on ; && || | and newlines."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(";&|\n"):
            out.append([])
        else:
            out[-1].append(tok)
    return [w for w in out if w]


def _strip_env(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if i < len(words) and words[i] in {
        "env",
        "command",
        "exec",
        "sudo",
        "time",
        "nohup",
    }:
        return _strip_env(words[i + 1 :])
    return words[i:]


def _repo_hint(words: Sequence[str], cwd: str, registry: frozenset[str]) -> str:
    for flag in ("--repo", "-R"):
        if flag in words:
            idx = words.index(flag)
            if idx + 1 < len(words):
                return _norm(words[idx + 1].rsplit("/", 1)[-1])
    for w in words:
        if w.startswith("--repo="):
            return _norm(w.split("=", 1)[1].rsplit("/", 1)[-1])
    for part in reversed([*re.split(r"[/\s]", " ".join(words)), *cwd.split("/")]):
        if _norm(part) in registry:
            return _norm(part)
    return ""


def _git_sub(words: list[str]) -> tuple[str, list[str]]:
    """git's subcommand and its arguments, past -C <dir> and -c k=v."""
    i = 1
    while i < len(words):
        if words[i] in {"-C", "-c", "--git-dir", "--work-tree"}:
            i += 2
            continue
        if words[i].startswith("-"):
            i += 1
            continue
        return words[i], words[i + 1 :]
    return "", []


def _bash_shapes(
    command: str, cwd: str, registry: frozenset[str]
) -> list[tuple[str, str, str, str]]:
    """(kind, shape, repo, version) for each guarded shape in a shell command."""
    hits: list[tuple[str, str, str, str]] = []
    for raw in _words(command):
        words = _strip_env(raw)
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        repo = _repo_hint(words, cwd, registry)
        if any("skills/release-cut/" in w for w in words):
            hits.append(("release", "release-cut skill script", repo, ""))
            continue
        if (
            prog == "gh"
            and len(words) >= 3
            and words[1] == "release"
            and words[2] in {"create", "edit", "upload"}
        ):
            version = next((w for w in words[3:] if _VERSION_TAG.match(w)), "")
            hits.append(("release", f"gh release {words[2]}", repo, version))
        elif (
            prog == "gh"
            and len(words) >= 3
            and words[1] == "workflow"
            and words[2] in {"run", "enable"}
        ):
            wf = next((w for w in words[3:] if not w.startswith("-")), "")
            if _RELEASE_WORKFLOW.search(wf):
                hits.append(("release", f"gh workflow {words[2]} {wf}", repo, ""))
        elif prog == "gh" and len(words) >= 2 and words[1] == "api":
            joined = " ".join(words[2:])
            if re.search(r"/releases\b", joined) and re.search(
                r"(-X|--method)\s*POST|-f\s+tag_name=|-F\s+tag_name=", joined
            ):
                hits.append(("release", "gh api releases POST", repo, ""))
            elif re.search(r"/git/refs\b", joined) and re.search(r"refs/tags/", joined):
                m = re.search(rf"refs/tags/({_VERSION})", joined)
                hits.append(
                    ("release", "gh api tag ref", repo, m.group(1) if m else "")
                )
            elif (
                dispatched := re.search(
                    r"/actions/workflows/([^/\s]+)/dispatches", joined
                )
            ) is not None:
                wf = dispatched.group(1)
                if _RELEASE_WORKFLOW.search(wf):
                    hits.append(("release", f"workflow dispatch {wf}", repo, ""))
            elif re.search(r"/dispatches\b", joined) and _RELEASE_WORKFLOW.search(
                joined
            ):
                hits.append(("release", "repository dispatch", repo, ""))
        elif prog == "git":
            sub, rest = _git_sub(words)
            if sub == "tag":
                listing = {
                    "-l",
                    "--list",
                    "-d",
                    "--delete",
                    "-v",
                    "--verify",
                    "--contains",
                    "--points-at",
                    "-n",
                }
                if not any(a in listing or a.startswith("--list") for a in rest):
                    version = next((a for a in rest if _VERSION_TAG.match(a)), "")
                    if version:
                        hits.append(("release", "git tag", repo, version))
            elif sub == "push":
                if "--tags" in rest or "--follow-tags" in rest:
                    hits.append(("release", "git push --tags", repo, ""))
                for a in rest:
                    ref = a.split(":", 1)[-1] if ":" in a else a
                    if _VERSION_TAG.match(ref.lstrip("+")):
                        hits.append(
                            (
                                "release",
                                "git push version tag",
                                repo,
                                ref.lstrip("+").removeprefix("refs/tags/"),
                            )
                        )
        elif prog == "uv" and len(words) >= 2:
            args = words[1:]
            if args[0] == "add":
                for a in args[1:]:
                    m = re.match(
                        rf"^({_SIBLING})\s*(==|>=|<=|~=|@|<|>)(.+)$", a, re.IGNORECASE
                    )
                    if m:
                        hits.append(("pin", "uv add", _norm(m.group(1)), m.group(3)))
                    elif re.search(r"github\.com[:/]OmniNode-ai/", a):
                        hits.append(
                            (
                                "pin",
                                "uv add git source",
                                _repo_hint([a], "", registry),
                                "",
                            )
                        )
            if args[0] in {"lock", "sync", "pip"}:
                for i, a in enumerate(args):
                    if a in {"--upgrade-package", "-P"} and i + 1 < len(args):
                        name = args[i + 1].split("==", 1)[0]
                        if re.fullmatch(_SIBLING, name, re.IGNORECASE):
                            version = (
                                args[i + 1].split("==", 1)[1]
                                if "==" in args[i + 1]
                                else ""
                            )
                            hits.append(
                                (
                                    "pin",
                                    f"uv {args[0]} --upgrade-package",
                                    _norm(name),
                                    version,
                                )
                            )
                    elif a.startswith("--upgrade-package=") or a.startswith("-P="):
                        name = a.split("=", 1)[1].split("==", 1)[0]
                        if re.fullmatch(_SIBLING, name, re.IGNORECASE):
                            hits.append(
                                (
                                    "pin",
                                    f"uv {args[0]} --upgrade-package",
                                    _norm(name),
                                    "",
                                )
                            )
                    elif a in {"--upgrade", "-U"}:
                        hits.append(("pin", f"uv {args[0]} --upgrade", "", ""))
        elif prog in {"sed", "perl", "gsed"} and any(
            a.startswith("-i") or a.startswith("-pi") for a in words[1:]
        ):
            targets = [
                a
                for a in words[1:]
                if _PIN_FILE.search(a) and not _NOT_PIN_FILE.search(a)
            ]
            text = " ".join(words[1:])
            if targets and (
                re.search(_SHA, text) or re.search(_SIBLING, text, re.IGNORECASE)
            ):
                sib = re.search(_SIBLING, text, re.IGNORECASE)
                hits.append(
                    (
                        "pin",
                        f"{prog} -i on {targets[0]}",
                        _norm(sib.group(0)) if sib else "",
                        "",
                    )
                )
    return hits


def _edit_pairs(
    tool_name: str, tool_input: Mapping[str, object], current: str | None
) -> list[tuple[str, str, str]]:
    """(path, old text, new text) pairs a file tool would write."""
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if tool_name == "Edit":
        return [
            (
                path,
                str(tool_input.get("old_string") or ""),
                str(tool_input.get("new_string") or ""),
            )
        ]
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        pairs = []
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, Mapping):
                    pairs.append(
                        (
                            path,
                            str(e.get("old_string") or ""),
                            str(e.get("new_string") or ""),
                        )
                    )
        return pairs
    if tool_name == "Write":
        return [(path, current or "", str(tool_input.get("content") or ""))]
    return []


def decide(
    tool_name: str,
    tool_input: Mapping[str, object],
    *,
    cwd: str,
    grants: Sequence[Grant],
    registry: Iterable[str],
    current_file_text: str | None = None,
) -> Decision:
    """Refuse a guarded shape that no unexpired grant admits; admit everything else."""
    reg = frozenset(_norm(r) for r in registry)
    hits: list[tuple[str, str, str, str]] = []
    if tool_name == "Skill":
        skill = str(tool_input.get("skill") or "").strip()
        if skill in _RELEASE_SKILLS or skill.endswith(":release-cut"):
            args = str(tool_input.get("args") or "")
            words = args.split()
            repo = next((_norm(w) for w in words if _norm(w) in reg), "")
            version = next((w for w in words if _VERSION_TAG.match(w)), "")
            hits.append(("release", f"skill {skill}", repo, version))
    elif tool_name == "Bash":
        hits.extend(_bash_shapes(str(tool_input.get("command") or ""), cwd, reg))
    elif tool_name in {"Edit", "MultiEdit", "Write"}:
        for path, old, new in _edit_pairs(tool_name, tool_input, current_file_text):
            added = pin_tokens(path, new) - pin_tokens(path, old)
            for name, value in sorted(added):
                context = next(
                    (ln for ln in new.splitlines() if value in re.sub(r"\s+", "", ln)),
                    "",
                )
                target = _pin_target_repo(name, path, context)
                hits.append(
                    (
                        "pin",
                        f"pin write in {path.rsplit('/', 1)[-1]}",
                        _norm(target),
                        value,
                    )
                )
    if not hits:
        return Decision(refuse=False)
    details: list[str] = []
    admitted_by = ""
    for kind, shape, repo, version in hits:
        grant = _find_grant(grants, kind, repo, version) if repo else None
        if grant is None:
            what = (
                f"{shape}"
                + (f" for {repo}" if repo else "")
                + (f" at {version}" if version else "")
            )
            details.append(what)
        else:
            admitted_by = grant.row_stamp
    if not details:
        kind, shape, repo, version = hits[0]
        return Decision(
            refuse=False, shape=shape, repo=repo, version=version, grant=admitted_by
        )
    kind, shape, repo, version = hits[0]
    item = (
        f"{kind}:{repo or '<repo>'}@{(version.lstrip('v') if version else '<version>')}"
    )
    reason = (
        f"release-pin-guard ({TICKET}): refused {'; '.join(details)}. {RULING}: releases are created only by the "
        "automated overnight release train (omnibase_infra release-train-nightly.yml, 07:11 UTC); a session or lane "
        "cannot cut a release, create or push a version tag, dispatch a release, release-cut, train or cascade "
        "workflow, run /omni:release-cut, or write or bump a version or commit-sha pin of an internal sibling. "
        "A one-off needs a grant from the operator or the cloud owner: an OPERATOR-CONSENT row recorded through "
        f"/omni:operator-ruling carrying approved_by=operator (or approved_by=cloud-owner), grant={item} and "
        "expires=<UTC, e.g. 2026-10-24T07:00:00Z>. Ask for it as one plain question; do not work around this refusal."
    )
    return Decision(
        refuse=True,
        shape=shape,
        repo=repo,
        version=version,
        reason=reason,
        details=tuple(details),
    )


# Repository names a command or path can name. A repository not listed here gets no
# repo hint, and a release or pin with no repo matches no grant, so it is refused.
REGISTRY: tuple[str, ...] = (
    "omnibase_core",
    "omnibase_spi",
    "omnibase_infra",
    "omnibase_compat",
    "omnimarket",
    "omniclaude",
    "omnimemory",
    "omniintelligence",
    "omnidash",
    "onex_change_control",
)


class HandlerReleasePinGuard:
    """Typed entry: one tool call and the grant rows in force to one decision."""

    def handle(
        self, request: ModelReleasePinGuardRequest
    ) -> ModelReleasePinGuardResult:
        grants = parse_grants(request.consent_rows, request.now)
        d = decide(
            request.tool_name,
            request.tool_input,
            cwd=request.cwd,
            grants=grants,
            registry=REGISTRY,
            current_file_text=request.current_file_text,
        )
        return ModelReleasePinGuardResult(
            refuse=d.refuse,
            exit_code=1 if d.refuse else 0,
            shape=d.shape,
            repo=d.repo,
            version=d.version,
            grant=d.grant,
            reason=d.reason,
            details=d.details,
        )
