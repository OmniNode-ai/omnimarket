# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pause-check findings of one or more pull requests, as a pure function.

A faithful port of the finding logic of the shell+awk script ``pause_check.sh``
(spec: ``spec/awk_spec_pause_check.sh.txt``). No I/O, no clock, no env reads, no
subprocess: the ledger text, the PR labels, the drain-map rules and the time are
the request's facts, and the findings are the awk stdout lines, line for line
(minus the ``pause-check ...`` header and the ORDER-STATE lines the shell prints).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from omnimarket.nodes.node_pause_check_findings_compute.models import (
    EnumFindingKind,
    EnumPauseVerdict,
    ModelFinding,
    ModelPauseCheckOverlay,
    ModelPauseCheckPr,
    ModelPauseCheckPrResult,
    ModelPauseCheckRequest,
    ModelPauseCheckResult,
)

# The awk FS " [|] ": a field boundary is a space, a pipe, a space.
_FIELD_RE = re.compile(r" [|] ")
# The awk prefilter's typed / lifts type-cell matchers (the embedded python heredoc).
_TYPED_RE = re.compile(r"\| [ \t]*(?:REVIEW_HOLD|HOLD|RELEASE)[ \t]*(?: \||$)")
_LIFTS_RE = re.compile(r"\| [ \t]*(?:STATUS|RELEASE|RULING)[ \t]*(?: \||$)")
_ASCII_LOWER = {
    c: c + 32 for c in range(ord("A"), ord("Z") + 1)
}  # awk tolower under LC_ALL=C
# The awk isrow(): the timestamp cell starts a ledger row.
_ISROW_RE = re.compile(r"^20[0-9][0-9]-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]")
# The awk until= normalisation: HH:MMZ -> HH:MM:00Z.
_UNTIL_RE = re.compile(r"^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]Z$")
# The awk until= stamp prefix (a 20xx- date).
_UNTIL_PREFIX_RE = re.compile(r"^20[0-9][0-9]-")
# The awk label pause-family regex.
_LABEL_RE = re.compile(
    r"(^|[^a-z])(hold|on-hold|do-not-merge|do not merge|dnm|wip|blocked|block|pause|paused|freeze|frozen)([^a-z]|$)"
)
# The awk is_tick() actor prefix.
_TICK_ACTOR_RE = re.compile(r"^launchd:")
# The awk is_notice() adopt-merged actor.
_ADOPT_RE = re.compile(r"adopt_merged")
# The awk handoff notice phrase.
_HANDOFF_RE = re.compile(r"(^|[^a-z])handoff from ")
# The awk CLAIM out-of-scope cut.
_OOS_RE = re.compile(r"out of scope.*$")
# The awk ORDER-token extraction (the shell's grep -Eo), excluding this PR's own ref.
_ORDER_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*#[0-9]+")


# The awk functions, ported as small private helpers with the same semantics.


def _cellall(fields: Sequence[str], key: str) -> str:
    """The whole value of the first field that starts with ``key=``, trailing blanks trimmed."""
    prefix = key + "="  # awk: index($i, key "=") == 1, over the fields from $3 on
    for f in fields[2:]:
        if f.startswith(prefix):
            return f[len(prefix) :].rstrip(" \t")
    return ""


def _cell(fields: Sequence[str], key: str) -> str:
    """The first whitespace-delimited word of ``_cellall``."""
    v = _cellall(fields, key)
    return v.split(" ", 1)[0].split("\t", 1)[0] if v else ""


def _has_token(list_: str, want: str) -> bool:
    """awk has_token: split on /[ ,]+/ and compare whole tokens."""
    return want in re.split(r"[ ,]+", list_) if list_ else False


def _names_repo_cell(fields: Sequence[str], repo: str) -> bool:
    """awk names_repo_cell: a repo= (or repos=) cell lists this repo."""
    v = _cellall(fields, "repo")
    if not v:
        v = _cellall(fields, "repos")
    return any(p.rsplit("/", 1)[-1] == repo for p in re.split(r"[ ,]+", v))


def _names_pr(
    row: str,
    fields: Sequence[str],
    repo: str,
    number: str,
    pr_ref_re: re.Pattern[str],
    pr_url_re: re.Pattern[str],
) -> bool:
    """awk names_pr: the row names the PR (repo AND number, never the bare number)."""
    if pr_ref_re.search(row) or pr_url_re.search(row):
        return True
    if not _names_repo_cell(fields, repo):
        return False
    return _pr_cell_lists(_cellall(fields, "pr"), number)


def _pr_cell_lists(cell: str, number: str) -> bool:
    """A bare number token of a pr= cell (``123`` or ``#123``; a ``repo#n`` token is read elsewhere)."""
    for p in re.split(r"[ ,]+", cell):
        if p.find("#") > 0:  # awk: index(p, "#") > 1
            continue
        if (p[1:] if p.startswith("#") else p) == number:
            return True
    return False


def _pf_names_pr(
    pcell: str,
    fields: Sequence[str],
    repo: str,
    number: str,
    pr_ref_re: re.Pattern[str],
    pr_url_re: re.Pattern[str],
) -> bool:
    """awk pf_names_pr: does the HOLD pr= field cell alone name this PR?"""
    if pcell == "":
        return False
    if pr_ref_re.search(pcell) or pr_url_re.search(pcell):
        return True
    if not _names_repo_cell(fields, repo):
        return False
    return _pr_cell_lists(pcell, number)


def _is_tick(fields: Sequence[str]) -> bool:
    """awk is_tick: a tick digest from a launchd job or pr-watcher."""
    return bool(
        _TICK_ACTOR_RE.match(_cell(fields, "actor"))
        or _cell(fields, "from") == "pr-watcher"
        or _cell(fields, "lane") == "pr-watcher"
    )


def _is_notice(typ: str, fields: Sequence[str], row_low: str) -> bool:
    """awk is_notice: an adopt-merged notice or a handoff MSG."""
    if typ != "MSG":
        return False
    if _cell(fields, "from") == "adopt-merged" or _ADOPT_RE.search(
        _cell(fields, "actor")
    ):
        return True
    nd = _cell(fields, "needs")
    return (
        nd in ("land", "train", "companion") and _HANDOFF_RE.search(row_low) is not None
    )


def _short(row: str, snippet_bytes: int) -> str:
    """awk short()/cut(): at most ``snippet_bytes`` UTF-8 bytes, never splitting a multi-byte character.

    The awk reads the byte after the cut: when it is a continuation byte (0x80-0xBF) the cut is
    mid-character, so it backs off over the continuation bytes and drops that character whole."""
    b = row.encode("utf-8")
    if len(b) <= snippet_bytes:
        return row
    if not 0x80 <= b[snippet_bytes] <= 0xBF:
        return b[:snippet_bytes].decode("utf-8", errors="ignore")
    i = snippet_bytes
    while i > 0 and 0x80 <= b[i - 1] <= 0xBF:
        i -= 1
    return b[: max(i - 1, 0)].decode("utf-8", errors="ignore")


def _build_pr_regexes(
    repo: str, number: str
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """awk BEGIN: the PR ref and URL regexes for this PR."""
    rq = re.escape(repo)
    pr_ref_re = re.compile(r"(^|[^A-Za-z0-9_.-])" + rq + r"#" + number + r"([^0-9]|$)")
    pr_url_re = re.compile(r"/" + rq + r"/pull/" + number + r"([^0-9]|$)")
    return pr_ref_re, pr_url_re


def _build_pause_regexes() -> tuple[
    re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]
]:
    """awk BEGIN: the pause, order, selfref and lift regexes (LC_ALL=C byte classes)."""
    b = r"(^|[^a-z])"
    e = r"([^a-z]|$)"
    pause_re = re.compile(
        b
        + r"(hold|holds|held|holding|pause|paused|pausing|freeze|frozen|halt|halted|do not merge|do-not-merge|dont merge|don.t merge|must not merge|not be merged|standalone|merge alongside|merged alongside|as a pair)"
        + e
    )
    order_re = re.compile(
        b
        + r"(merge order|merge-order|landing order|land(s|ed)? after|merge(s|d)? after|land(s)? first|lands? before|merge(s)? before|waiting_order)"
        + e
    )
    selfref_re = re.compile(
        r"(pause[_ -]check(\.sh)?|hold[_ -]check|repo-pause|pr-row|hold:auto-merge|hold label)"
    )
    lift_re = re.compile(
        b
        + r"(lift|lifts|lifted|lifting|release|releases|released|unpause|unpaused|unfreeze|unfrozen|resume|resumed|cleared to merge|may merge|go ahead)"
        + e
    )
    return pause_re, order_re, selfref_re, lift_re


def _build_repo_lift_re() -> re.Pattern[str]:
    """awk BEGIN: the repo-wide pause-lift regex."""
    b = r"(^|[^a-z])"
    e = r"([^a-z]|$)"
    return re.compile(
        b
        + r"((lift|lifts|lifted|lifting|end|ends|ended|release|releases|released|resume|resumes|resumed|unpause|unpaused|unfreeze|unfrozen)( the| this| that)?( [a-z-]+)?( [a-z-]+)? (hold|pause|freeze|halt|merges?)|(hold|pause|freeze|halt)[a-z]* (is |are |was |has been |have been )?(lifted|released|ended|over|cleared))"
        + e
    )


def _line_is_seen(
    line: str, number: str, repo: str, n: int, declared: set[int]
) -> bool:
    """The embedded python prefilter's per-line test: can this line change a finding?"""
    return bool(
        number in line
        or (declared and repo in line and (n in declared or _LIFTS_RE.search(line)))
        or (("HOLD" in line or "RELEASE" in line) and _TYPED_RE.search(line))
    )


def _prefilter(
    lines: list[str],
    repo: str,
    number: str,
    declared: set[int],
    keep_all: bool,
) -> tuple[tuple[int, str], ...]:
    """Which ledger lines the awk loop sees, with their 1-based numbers (keep_all reads every line)."""
    return tuple(
        (n, line)
        for n, line in enumerate(lines, 1)
        if keep_all or _line_is_seen(line, number, repo, n, declared)
    )


class HandlerPauseCheckFindings:
    """``handle(ModelPauseCheckRequest) -> ModelPauseCheckResult``."""

    def __init__(self, overlay: ModelPauseCheckOverlay) -> None:
        self._overlay = overlay
        self._pause_re, self._order_re, self._selfref_re, self._lift_re = (
            _build_pause_regexes()
        )
        self._repo_lift_re = _build_repo_lift_re()

    def handle(self, request: ModelPauseCheckRequest) -> ModelPauseCheckResult:
        lines = request.ledger_text.split("\n")
        results: list[ModelPauseCheckPrResult] = []
        for pr in request.prs:
            results.append(self._decide(pr, request, lines))
        return ModelPauseCheckResult(results=tuple(results))

    def _decide(
        self,
        pr: ModelPauseCheckPr,
        request: ModelPauseCheckRequest,
        lines: list[str],
    ) -> ModelPauseCheckPrResult:
        repo = pr.repo
        number = str(pr.number)
        me = pr.lane or ""
        now = request.now
        rules = request.rules
        report_type = set(rules.report_types)
        declared = set(rules.repo_pause_lines.get(repo, ()))
        pr_ref_re, pr_url_re = _build_pr_regexes(repo, number)
        repo_word_re = re.compile(
            r"(^|[^A-Za-z0-9_-])" + re.escape(repo) + r"([^A-Za-z0-9_-]|$)"
        )

        # a. labels: the BEGIN block.
        found: list[ModelFinding] = []
        armoff: list[ModelFinding] = []
        for lab in pr.labels:
            if lab.translate(_ASCII_LOWER) == "hold:auto-merge":
                armoff.append(
                    ModelFinding(
                        kind=EnumFindingKind.ARM_OFF,
                        text=lab,
                    )
                )
            elif _LABEL_RE.search(lab.translate(_ASCII_LOWER)):
                found.append(ModelFinding(kind=EnumFindingKind.LABEL, text=lab))

        # b. the line prefilter (split the ledger once; do not re-decode per PR).
        seen = _prefilter(lines, repo, number, declared, request.full_scan)

        # c. the per-row awk loop.
        hold: dict[str, int] = {}
        holdrow: dict[str, str] = {}
        hold_scoped: dict[str, bool] = {}
        pr_pause: dict[int, str] = {}
        repo_pause: dict[int, str] = {}
        order_rows: dict[int, str] = {}
        ctx: dict[int, str] = {}
        released: set[str] = set()
        rel_pr: set[str] = set()
        rel_names: set[str] = set()

        for ln, row in seen:
            fields = _FIELD_RE.split(row)
            if not fields or not _ISROW_RE.match(fields[0]):
                continue
            typ = fields[1].strip(" \t") if len(fields) > 1 else ""
            own = me != "" and (
                _cell(fields, "lane") == me or _cell(fields, "from") == me
            )
            if own and typ not in ("HOLD", "RELEASE"):
                continue
            if typ == "MSG" and _is_tick(fields):
                continue
            npr = _names_pr(row, fields, repo, number, pr_ref_re, pr_url_re)
            row_low = row.translate(_ASCII_LOWER)

            # RELEASE with a re= cell.
            if typ == "RELEASE":
                re_cell = _cell(fields, "re")
                if re_cell != "":
                    if _cellall(fields, "pr") == "":
                        released.add(re_cell)
                    else:
                        rel_pr.add(re_cell)
                        if _pf_names_pr(
                            _cellall(fields, "pr"),
                            fields,
                            repo,
                            number,
                            pr_ref_re,
                            pr_url_re,
                        ):
                            rel_names.add(re_cell)

            if typ in ("HOLD", "REVIEW_HOLD"):
                id_ = _cell(fields, "id")
                pcell = _cellall(fields, "pr")
                rcell = _names_repo_cell(fields, repo)
                to = _cell(fields, "to")
                by_pr = (
                    _pf_names_pr(pcell, fields, repo, number, pr_ref_re, pr_url_re)
                    if pcell != ""
                    else npr
                )
                applies = (
                    by_pr
                    or (rcell and pcell == "")
                    or (
                        pcell == ""
                        and _cellall(fields, "repo") == ""
                        and _cellall(fields, "repos") == ""
                        and ((me != "" and _has_token(to, me)) or _has_token(to, "all"))
                    )
                )
                if _cellall(fields, "surface") != "":
                    applies = False
                u = _cell(fields, "until")
                if _UNTIL_RE.match(u):
                    u = u[:-1] + ":00Z"
                if _UNTIL_PREFIX_RE.match(u) and u < now:
                    if applies:
                        ctx[ln] = (
                            _short(row, self._overlay.snippet_bytes)
                            + " [until= passed]"
                        )
                    continue
                if applies and id_ != "":
                    hold[id_] = ln
                    holdrow[id_] = _short(row, self._overlay.snippet_bytes)
                    hold_scoped[id_] = pcell != "" or rcell
                elif applies and npr:
                    pr_pause[ln] = _short(row, self._overlay.snippet_bytes)
                elif npr:
                    ctx[ln] = _short(row, self._overlay.snippet_bytes)
                continue

            if npr:
                if typ == "RELEASE" or (
                    (typ in ("RULING", "OPERATOR-CONSENT"))
                    and self._lift_re.search(row_low)
                ):
                    for k in list(pr_pause):
                        del pr_pause[k]
                    for k in list(repo_pause):
                        del repo_pause[k]
                    ctx[ln] = _short(row, self._overlay.snippet_bytes)
                    continue
                s = row_low
                s = self._selfref_re.sub(" ", s)
                if typ == "CLAIM":
                    s = _OOS_RE.sub("", s)
                if _is_notice(typ, fields, row_low):
                    s = ""
                if typ not in report_type and self._pause_re.search(s):
                    pr_pause[ln] = _short(row, self._overlay.snippet_bytes)
                elif self._order_re.search(s):
                    order_rows[ln] = _short(row, self._overlay.snippet_bytes)
                else:
                    ctx[ln] = _short(row, self._overlay.snippet_bytes)

            if ln in declared:
                if not npr:
                    repo_pause[ln] = _short(row, self._overlay.snippet_bytes)
            elif (
                (typ in ("STATUS", "RELEASE") and _names_repo_cell(fields, repo))
                or (
                    typ == "RULING"
                    and (
                        _names_repo_cell(fields, repo)
                        or repo_word_re.search(row) is not None
                    )
                )
            ) and self._repo_lift_re.search(row_low):
                repo_pause.clear()

        # d. the END block.
        findings: list[ModelFinding] = []
        findings.extend(found)
        findings.extend(armoff)
        stops = len(found)
        parked_reason = rules.parked.get(repo, "")
        if parked_reason != "":
            findings.append(
                ModelFinding(
                    kind=EnumFindingKind.PARKED, text=f"{repo}: {parked_reason}"
                )
            )
            stops += 1

        open_hold: dict[int, str] = {}
        for id_ in hold:
            if (
                id_ not in released
                and id_ not in rel_names
                and not (id_ in rel_pr and not hold_scoped[id_])
            ):
                open_hold[hold[id_]] = holdrow[id_]
        stops += self._sorted_print("HOLD", open_hold, findings)
        stops += self._sorted_print("PR-ROW", pr_pause, findings)
        stops += self._sorted_print("REPO-PAUSE", repo_pause, findings)
        norder = self._sorted_print("ORDER", order_rows, findings)
        landing_order_note: str | None = None
        if norder > 0:
            landing_order_note = (
                f"ORDER is not a stop. Landing order: {rules.landing_order}"
            )
        context: list[ModelFinding] = []
        self._sorted_print("CONTEXT", ctx, context)

        verdict = EnumPauseVerdict.PAUSED if stops > 0 else EnumPauseVerdict.CLEAR

        # The ORDER tokens: the PR refs in the ORDER finding lines, excluding this PR's own ref.
        own_ref = f"{repo}#{number}"
        tokens: set[str] = set()
        for f in findings:
            if f.kind is EnumFindingKind.ORDER:
                for tok in _ORDER_TOKEN_RE.findall(f.text):
                    if tok.casefold() != own_ref.casefold():
                        tokens.add(tok)
        order_tokens = tuple(sorted(tokens)[: self._overlay.max_order_tokens])

        return ModelPauseCheckPrResult(
            repo=repo,
            number=pr.number,
            findings=tuple(findings),
            context=tuple(context),
            landing_order_note=landing_order_note,
            stops=stops,
            verdict=verdict,
            order_tokens=order_tokens,
        )

    @staticmethod
    def _sorted_print(
        tag: str,
        arr: dict[int, str],
        out: list[ModelFinding],
    ) -> int:
        """awk sorted_print: sort by line number, append the findings, return the count."""
        kind = EnumFindingKind(tag)
        for ln in sorted(arr):
            out.append(ModelFinding(kind=kind, line=ln, text=f"{ln}: {arr[ln]}"))
        return len(arr)


__all__ = ["HandlerPauseCheckFindings"]
