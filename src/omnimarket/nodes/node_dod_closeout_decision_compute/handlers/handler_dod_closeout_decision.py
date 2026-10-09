# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the DoD closeout sweep (OMN-20675).

A behaviour-preserving port of the pure block of the ``dod-closeout-sweep``
workflow script, including its vet, merge and decide-chunk glue.
tests/fixtures/dod_closeout_decision_parity.json holds the old behaviour over
the same inputs. JavaScript truthiness, missing values, whitespace, ASCII
regexes and UTC date rollover are preserved explicitly.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast

from omnibase_core.types import JsonType

from omnimarket.nodes.node_dod_closeout_decision_compute.models.model_dod_closeout_decision import (
    EnumDodCloseoutDecisionKind,
    ModelBindingRefusal,
    ModelCandidateSelection,
    ModelChunkRef,
    ModelCloseoutConfig,
    ModelDelegationCheck,
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
    ModelDodVerifyReading,
    ModelOpenComment,
    ModelStopTimes,
    ModelTicketDecision,
    ModelUnmetCriterion,
)

_MISSING = object()
_WS = (
    r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_SPACES = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
SOURCE_READERS = {
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "ack",
    "cat",
    "head",
    "tail",
    "sed",
    "awk",
    "wc",
    "ls",
    "find",
    "test",
    "[",
    "[[",
    "stat",
    "file",
    "sort",
    "uniq",
    "cut",
    "tr",
    "jq",
    "true",
    "echo",
    "printf",
    "realpath",
    "readlink",
    "basename",
    "dirname",
}
GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
GIT_READ_SUBCOMMANDS = {
    "grep",
    "show",
    "cat-file",
    "log",
    "ls-files",
    "ls-tree",
    "diff",
    "blame",
    "rev-parse",
    "merge-base",
}
WRAPPERS = {
    "env",
    "command",
    "exec",
    "time",
    "nice",
    "timeout",
    "nohup",
    "sudo",
    "xargs",
    "stdbuf",
}
SHELLS = {"bash", "sh", "zsh"}
INTERPRETERS = {"node", "python", "python3", "ruby", "perl"}
REFUSAL_REASONS = {
    "no-check": "the binder returned no check object",
    "no-label": "the check carries no label",
    "unknown-kind": "the check kind is not test, command or lab_readback",
    "no-expected-observation": "the check names no expected observation",
    "no-test-selector": "a test check names no pytest node id",
    "test-selector-not-a-test-id": "the test selector is not a pytest node id (path/to/test_x.py::test_name)",
    "no-repo": "the test check names no single registry repo to run it in",
    "not-on-merged-dev": "the check does not run at origin/dev or a full merged sha",
    "no-command": "the check carries no runnable command",
    "no-cwd": "a command check names no working directory",
    "no-host": "a lab readback names no host",
    "not-read-only": "the command writes (a deploy, a push, a POST or SQL that changes state), and a check must only read",
    "pr-exists-only": "the command only reads pull request state, which shows a change landed, not that the criterion holds",
    "symbol-grep-only": "the command only reads source text for a symbol, which shows code exists, not that the criterion holds",
    "inspection-only": "the command only reads files or their existence and executes nothing",
}


def _regex(pattern: str, *, ignore_case: bool = False) -> re.Pattern[str]:
    # Python's whitespace and end anchor differ from ECMAScript's.
    pattern = pattern.replace(r"[\s\S]*", "(?s:.*)")
    pattern = pattern.replace(r"[^/\s]", f"[^/{_WS}]")
    pattern = pattern.replace(r"\s", f"[{_WS}]").replace(r"\S", f"[^{_WS}]")
    if pattern.endswith("$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern, re.ASCII | (re.IGNORECASE if ignore_case else 0))


FS_PROBE = _regex(
    r"\b(accessSync|existsSync|statSync|readFileSync|readdirSync|os\.path\.(exists|isfile|isdir)|\.exists\(\)|\.is_file\(\)|\.is_dir\(\)|\.read_text\(\)|File\.exist|-e\s)"
)
FS_ONLY_IMPORTS = _regex(
    r"(\bimport\s+(os|pathlib|sys)(\s*,\s*(os|pathlib|sys))*\b|\bfrom\s+(os|pathlib)(\.path)?\s+import\b|\brequire\(\s*['\"](node:)?(fs|path)['\"]\s*\))"
)
MUTATING = _regex(
    r"\b(docker\s+(restart|rm|stop|start|kill|compose\s+(up|down|restart|rm)|exec\s+\S+\s+(rm|kill))|kubectl\s+(apply|delete|rollout|set|scale|patch|edit|create|replace|annotate|label)|helm\s+(install|upgrade|uninstall)|curl\b[\s\S]*-X\s*(POST|PUT|DELETE|PATCH)|curl\b[\s\S]*--data|git\s+push|gh\s+pr\s+(merge|close|create|edit)|rpk\s+topic\s+(produce|create|delete)|kcat\b[\s\S]*-P)\b",
    ignore_case=True,
)
SQL_WRITE = _regex(
    r"(^|;)\s*(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy)\b",
    ignore_case=True,
)
REPO_NAME = _regex(r"^[a-z][a-z0-9_-]*$")
TEXT_SHA = _regex(r"^[0-9a-f]{16}$")


def _truthy(value: object) -> bool:
    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, (int, float)) and (value == 0 or math.isnan(value)):
        return False
    return not (isinstance(value, str) and value == "")


def _or(value: object, fallback: object) -> object:
    return value if _truthy(value) else fallback


def _get(value: object, key: str) -> object:
    return value.get(key, _MISSING) if isinstance(value, dict) else _MISSING


def _array(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _out(**values: object) -> dict[str, JsonType]:
    return {k: cast(JsonType, v) for k, v in values.items() if v is not _MISSING}


def _spread(value: object) -> dict[str, JsonType]:
    if isinstance(value, dict):
        return dict(cast(dict[str, JsonType], value))
    if isinstance(value, (list, str)):
        return {str(i): cast(JsonType, v) for i, v in enumerate(value)}
    return {}


def _js_string(value: object) -> str:
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, list):
        return ",".join(
            "" if v is None or v is _MISSING else _js_string(v) for v in value
        )
    return "[object Object]"


def _js_trim(value: str) -> str:
    return value.strip(_SPACES)


def _is_js_space(char: str) -> bool:
    return char in _SPACES


def _basename(word: str) -> str:
    # JS ``replace(/^.*\//, '')``: ``.`` stops at any line terminator, not only \n.
    return re.sub(r"^[^\n\r\u2028\u2029]*/", "", word)


def _integer(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and int(value) == value
    )


def _number(value: object) -> int | float:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = _js_trim(value)
        if not text:
            return 0
        if text in {"Infinity", "+Infinity", "-Infinity"}:
            return float(text)
        for prefix, base, digits in [
            ("0x", 16, "0-9a-fA-F"),
            ("0o", 8, "0-7"),
            ("0b", 2, "01"),
        ]:
            if re.fullmatch(
                f"0[{prefix[1].lower()}{prefix[1].upper()}][{digits}]+", text, re.ASCII
            ):
                return int(text, base)
        if re.fullmatch(
            r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text, re.ASCII
        ):
            return float(text)
    # Arrays and objects are deliberately outside the workflow's Number inputs.
    return math.nan


def positive_int(value: object, fallback: int, cap: int, name: str) -> int:
    if value is _MISSING or value is None or value == "":
        return fallback
    number = _number(value)
    if not _integer(number) or number < 1:
        raise ValueError(
            f"dod-closeout-sweep: {name} must be a positive integer, got '{_js_string(value)}'"
        )
    return min(int(number), cap)


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    if 0 <= year <= 99:
        year += 1900
    year, month0 = divmod(year * 12 + month - 1, 12)
    return datetime(year, month0 + 1, 1) + timedelta(
        days=day - 1, hours=hour, minutes=minute, seconds=second
    )


def _iso(date: datetime) -> str:
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}T{date.hour:02d}:{date.minute:02d}:{date.second:02d}Z"


def stop_times(fire_id: object) -> ModelStopTimes | None:
    match = _regex(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-").search(
        _js_string(_or(fire_id, ""))
    )
    if not match:
        return None
    time = _utc(*(int(v) for v in match.groups()))
    return ModelStopTimes(
        no_new_work=_iso(time + timedelta(seconds=3500)),
        return_by=_iso(time + timedelta(seconds=4300)),
    )


def parse_args(raw: dict[str, JsonType]) -> ModelCloseoutConfig:
    date = _or(_get(raw, "date"), "")
    if not _truthy(date):
        raise ValueError("dod-closeout-sweep: args.date is required, format YYYY-MM-DD")
    if not _regex(r"^\d{4}-\d{2}-\d{2}$").search(_js_string(date)):
        raise ValueError(
            f"dod-closeout-sweep: args.date must be YYYY-MM-DD, got '{_js_string(date)}'"
        )
    fire_id = _or(_get(raw, "fireId"), "")
    run_key, slot = _js_string(date), "manual"
    if _truthy(fire_id):
        match = _regex(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z-").search(
            _js_string(fire_id)
        )
        if not match:
            raise ValueError(
                f"dod-closeout-sweep: args.fireId must be the driver's fire id (YYYYMMDDTHHMMSSZ-<workflow>), got '{_js_string(fire_id)}'"
            )
        y, m, d, h, minute = match.groups()
        slot = f"{h}{minute}Z"
        run_key = f"{y}-{m}-{d}T{slot}"
    fences = tuple(f for f in _array(_get(raw, "fences")) if isinstance(f, str) and f)
    apply = _get(raw, "apply")
    force = _get(raw, "force")
    return ModelCloseoutConfig(
        date=_js_string(date),
        fire_id=_js_string(fire_id),
        run_key=run_key,
        slot=slot,
        stop=stop_times(fire_id),
        project=_js_string(_or(_get(raw, "project"), "")),
        max_candidates=positive_int(_get(raw, "maxCandidates"), 6, 12, "maxCandidates"),
        chunk_size=positive_int(_get(raw, "chunkSize"), 2, 4, "chunkSize"),
        git_op_timeout_s=positive_int(
            _get(raw, "gitOpTimeoutS"), 1500, 3600, "gitOpTimeoutS"
        ),
        check_timeout_s=positive_int(
            _get(raw, "checkTimeoutS"), 600, 1800, "checkTimeoutS"
        ),
        apply=apply is not False and apply != "false",
        force=force is True or force == "true",
        fences=fences,
    )


def _invalid_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_json(value: object) -> object:
    return cast(object, json.loads(_js_string(value), parse_constant=_invalid_constant))


def parse_text_state(raw: str) -> list[object]:
    try:
        rows = _parse_json(raw)
    except (ValueError, TypeError):
        return []
    return [
        r
        for r in _array(rows)
        if isinstance(r, dict)
        and isinstance(_get(r, "id"), str)
        and _truthy(_get(r, "id"))
    ]


def with_text_state(
    candidates: Sequence[dict[str, JsonType]], rows: list[object]
) -> list[dict[str, JsonType]]:
    by_id = {_js_string(_get(r, "id")): r for r in rows}
    out = []
    for candidate in candidates:
        row = by_id.get(_js_string(_get(candidate, "id")), _MISSING)
        at = _get(row, "last_closeout_comment_at")
        first = at if isinstance(at, str) else ""
        out.append(
            {
                **candidate,
                **_out(
                    last_closeout_comment_at=_or(
                        first, _or(_get(candidate, "last_closeout_comment_at"), "")
                    ),
                    text_sha=_js_string(_or(_get(row, "text_sha"), "")),
                    held_text_sha=_js_string(_or(_get(row, "held_text_sha"), "")),
                ),
            }
        )
    return out


def held_for_amendment(row: object) -> bool:
    now = _js_string(_or(_get(row, "text_sha"), ""))
    held = _js_string(_or(_get(row, "held_text_sha"), ""))
    return TEXT_SHA.search(now) is not None and now == held


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be", errors="surrogatepass")


def select_candidates(
    records: Sequence[dict[str, JsonType]], maximum: int, text_sha_by_id: dict[str, str]
) -> ModelCandidateSelection:
    unique: dict[str, dict[str, JsonType]] = {}
    for row in records:
        identifier = _get(row, "id")
        if isinstance(identifier, str) and identifier and identifier not in unique:
            unique[identifier] = row
    held = sorted((i for i, r in unique.items() if held_for_amendment(r)), key=_utf16)

    def key(identifier: str) -> tuple[bytes, bytes]:
        at = _get(unique[identifier], "last_closeout_comment_at")
        return _utf16(at if isinstance(at, str) and at else ""), _utf16(identifier)

    ordered = sorted(
        (i for i, r in unique.items() if not held_for_amendment(r)), key=key
    )
    return ModelCandidateSelection(
        selected=tuple(ordered[:maximum]),
        deferred=tuple(ordered[maximum:]),
        held=tuple(held),
        text_sha_by_id=text_sha_by_id,
    )


def split_shell(command: object) -> list[list[str]]:
    segments: list[list[str]] = []
    stages: list[str] = []
    cur, quote = "", ""
    src = _js_string(command)

    def end_stage() -> None:
        nonlocal cur
        if _js_trim(cur):
            stages.append(_js_trim(cur))
        cur = ""

    def end_segment() -> None:
        nonlocal stages
        end_stage()
        if stages:
            segments.append(stages)
        stages = []

    i = 0
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(src):
                cur += c + src[i + 1]
                i += 2
                continue
            if c == quote:
                quote = ""
            cur += c
        elif c in {"'", '"'}:
            quote = c
            cur += c
        elif c == "\\" and i + 1 < len(src):
            cur += c + src[i + 1]
            i += 2
            continue
        elif c == "#" and (i == 0 or _is_js_space(src[i - 1])):
            while i < len(src) and src[i] != "\n":
                i += 1
            end_segment()
        elif c in {"\n", ";"}:
            end_segment()
        elif c in {"&", "|"} and src[i : i + 2] == c * 2:
            i += 1
            end_segment()
        elif c == "|":
            end_stage()
        else:
            cur += c
        i += 1
    end_segment()
    return segments


def words_of(stage: str) -> list[str]:
    words: list[str] = []
    cur, quote, any_word = "", "", False
    for c in stage:
        if quote:
            if c == quote:
                quote = ""
            else:
                cur += c
        elif c in {"'", '"'}:
            quote, any_word = c, True
        elif _is_js_space(c):
            if cur or any_word:
                words.append(cur)
            cur, any_word = "", False
        else:
            cur += c
    if cur or any_word:
        words.append(cur)
    return words


def classify_words(words: list[str], depth: int) -> str:
    i = 0
    while i < len(words) and _regex(r"^[A-Za-z_][A-Za-z0-9_]*=").search(words[i]):
        i += 1
    cmd = _basename(words[i] if i < len(words) else "")
    if not cmd:
        return "read"
    if cmd in {"cd", "export", "set", "unset", "source", "."}:
        return "skip"
    if cmd in WRAPPERS:
        j = i + 1
        while j < len(words) and (
            words[j].startswith("-")
            or _regex(r"^[A-Za-z_][A-Za-z0-9_]*=").search(words[j])
            or (cmd == "timeout" and _regex(r"^\d+[smhd]?$").search(words[j]))
            or (cmd == "nice" and _regex(r"^-?\d+$").search(words[j]))
        ):
            j += 1
        if j >= len(words):
            return "read" if cmd == "xargs" else "skip"
        return classify_words(words[j:], depth + 1)
    next_word = words[i + 1] if i + 1 < len(words) else ""
    if cmd in SHELLS and next_word == "-c" and i + 2 < len(words) and depth < 4:
        inner = kinds_of(words[i + 2], depth + 1)
        return next((k for k in ("exec", "pr", "grep") if k in inner), "read")
    if cmd in INTERPRETERS and next_word in {"-e", "-c"} and i + 2 < len(words):
        code = words[i + 2]
        rest = FS_ONLY_IMPORTS.sub(" ", code)
        return (
            "read"
            if FS_PROBE.search(code)
            and not _regex(r"\b(import|require)\b").search(rest)
            else "exec"
        )
    if cmd == "git" and next_word in GIT_READ_SUBCOMMANDS:
        return "grep" if next_word == "grep" else "read"
    if cmd == "gh" and (
        next_word == "pr"
        or (
            next_word == "api"
            and any(_regex(r"/pulls\b").search(x) for x in words[i + 2 :])
        )
    ):
        return "pr"
    if cmd in GREP_FAMILY:
        return "grep"
    return "read" if cmd in SOURCE_READERS else "exec"


def kinds_of(command: object, depth: int = 0) -> list[str]:
    kinds = [
        classify_words(words_of(stage), depth)
        for segment in split_shell(command)
        for stage in segment
    ]
    return [k for k in kinds if k != "skip"]


def writes_sql(command: str) -> bool:
    for segment in split_shell(command):
        for stage in segment:
            words = words_of(stage)
            at = next(
                (i for i, w in enumerate(words) if _basename(w) == "psql"),
                -1,
            )
            if at < 0:
                continue
            for k in range(at + 1, len(words) - 1):
                if words[k] in {"-c", "--command"}:
                    sql = re.sub(r"'(?:[^']|'')*'", "''", words[k + 1])
                    if SQL_WRITE.search(sql):
                        return True
    return False


def normalize_repo(repo: object) -> str:
    text = _js_trim(_js_string(_or(repo, "")))
    match = _regex(r"^omninode-ai/([^/\s]+)$", ignore_case=True).search(text)
    return match[1] if match else text


def normalize_check(check: object) -> object:
    repo = _get(check, "repo")
    return (
        {**_spread(check), "repo": normalize_repo(repo)}
        if isinstance(check, dict) and isinstance(repo, str)
        else check
    )


def refusal_reason(refusal: object) -> str:
    cls = _js_string(refusal)
    reason = REFUSAL_REASONS.get(
        cls, "the check is not runnable as written for its kind, so it was never re-run"
    )
    return f"binding refused ({cls}): {reason}"


def _string_field(check: object, field: str) -> str:
    value = _get(check, field)
    return _js_trim(value) if isinstance(value, str) else ""


def refusal_of(check: object) -> str | None:
    if not isinstance(check, (dict, list)):
        return "no-check"
    if not _string_field(check, "label"):
        return "no-label"
    kind = _get(check, "kind")
    if kind not in ("test", "command", "lab_readback"):
        return "unknown-kind"
    if not _string_field(check, "expect"):
        return "no-expected-observation"
    ref_ok = (
        _regex(r"^(origin/dev|[0-9a-f]{40})$").search(
            _js_string(_or(_get(check, "ref"), ""))
        )
        is not None
    )
    if kind == "test":
        selector = _string_field(check, "selector")
        if not selector:
            return "no-test-selector"
        if not _regex(
            r"^[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_./-]+(\[[^\]`$;&|<>\n\\]*\])?)*$"
        ).search(selector):
            return "test-selector-not-a-test-id"
        repo = _get(check, "repo")
        if not isinstance(repo, str) or not REPO_NAME.search(normalize_repo(repo)):
            return "no-repo"
        return None if ref_ok else "not-on-merged-dev"
    command = _string_field(check, "command")
    if not command:
        return "no-command"
    if kind == "command":
        if not _string_field(check, "cwd"):
            return "no-cwd"
        if not ref_ok:
            return "not-on-merged-dev"
    if kind == "lab_readback" and not _string_field(check, "host"):
        return "no-host"
    if MUTATING.search(command) or writes_sql(command):
        return "not-read-only"
    kinds = kinds_of(command)
    if not kinds:
        return "no-command"
    if "exec" in kinds:
        return None
    if "pr" in kinds:
        return "pr-exists-only"
    return "symbol-grep-only" if "grep" in kinds else "inspection-only"


def identities(actor: object) -> set[str]:
    folded = _js_trim(_js_string(_or(actor, "")).lower())
    return {folded, *(_regex(r"\blane=(\S+)").findall(folded))} if folded else set()


def same_actor(a: object, b: object) -> bool:
    ia, ib = identities(a), identities(b)
    return not ia or not ib or bool(ia & ib)


def read_dod_verify(receipt_json: object) -> dict[str, JsonType]:
    refused = _out(
        verify_status="not_run", error_code="VERIFY_RECEIPT_UNREADABLE", checks=[]
    )
    try:
        receipt = _parse_json(receipt_json)
    except (ValueError, TypeError):
        return refused
    tag = _get(receipt, "result_model")
    if (
        tag
        == "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState"
    ):
        verdict = _get(receipt, "result")
    elif (
        tag
        == "omnibase_infra.cli.model_receipt_runtime_summary.ModelReceiptRuntimeSummary"
    ):
        verdict = _get(_get(receipt, "result"), "terminal_payload")
    else:
        return refused
    total = _get(verdict, "total_checks")
    checks = _get(verdict, "checks")
    if (
        not _integer(total)
        or cast(int | float, total) < 1
        or not isinstance(checks, list)
    ):
        return refused
    message = _string_field(verdict, "error_message")
    match = _regex(r"^[A-Z][A-Z0-9_]+").search(message)
    return _out(
        verify_status=_get(verdict, "status"),
        error_code=(match[0] if match else "VERIFY_ERROR") if message else "",
        total_checks=total,
        verified_count=_get(verdict, "verified_count"),
        checks=checks,
    )


def binding_label(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    match = _regex(r"^(AC|DOD)[-_ .]?(\d+)([a-zA-Z]?)$", ignore_case=True).search(
        _js_trim(raw)
    )
    return f"{match[1].upper()}{int(match[2])}{match[3]}" if match else ""


def ac_verdicts(dv: object) -> dict[str, object]:
    verdicts: dict[str, object] = {}
    for check in _array(_get(dv, "checks")):
        if not _string_field(check, "evidence_id") or not isinstance(
            _get(check, "binds_ac"), list
        ):
            continue
        draft = _get(check, "draft_binds_ac")
        if draft is not _MISSING and not isinstance(draft, list):
            continue
        drafts = {binding_label(raw) for raw in _array(draft)}
        for raw in _array(_get(check, "binds_ac")):
            label = binding_label(raw)
            if not label or label in drafts:
                continue
            status = _get(check, "status")
            if status == "verified":
                verdicts[label] = "verified"
            elif verdicts.get(label) != "verified":
                verdicts[label] = _or(status, "unbound")
    return verdicts


def _amend(value: object) -> dict[str, JsonType]:
    return (
        {"amendment": _js_trim(value)}
        if isinstance(value, str) and _js_trim(value)
        else {}
    )


def decide_ticket(ticket: object) -> dict[str, JsonType]:
    unmet: list[dict[str, JsonType]] = []
    acs = _array(_get(ticket, "acs"))
    dv = _or(_get(ticket, "dod_verify"), {})
    verdicts = ac_verdicts(dv)
    if not acs:
        unmet.append(
            {
                "label": "criteria",
                "reason": cast(
                    JsonType,
                    _or(
                        _get(ticket, "criteria_note"),
                        "no acceptance criterion could be read from the ticket, so nothing can be bound",
                    ),
                ),
                **_amend(_get(ticket, "criteria_amendment")),
            }
        )
    for ac in acs:
        label = _js_string(_or(_get(ac, "label"), "?"))
        amendment: dict[str, JsonType] = {}
        if not _truthy(_get(ac, "proposed")):
            reason = f"no check authored: {_js_string(_or(_get(ac, 'reason'), 'the binder found no honest check'))}"
            amendment = _amend(_get(ac, "amendment"))
        elif _truthy(_get(ac, "refused")):
            reason = refusal_reason(_get(ac, "refused"))
        elif not _truthy(_get(ac, "accepted")):
            reason = f"binding rejected by the acceptor: {_js_string(_or(_get(ac, 'reason'), 'no reason given'))}"
            amendment = _amend(_get(ac, "amendment"))
        elif same_actor(_get(ac, "accepted_by"), _get(ac, "proposed_by")):
            reason = "binding was not accepted by a lane other than its author"
        elif verdicts.get(binding_label(label)) != "verified":
            verdict = _or(
                verdicts.get(binding_label(label), _MISSING),
                "no verdict for this criterion",
            )
            reason = (
                f"dod_verify did not verify the bound check ({_js_string(verdict)})"
            )
        else:
            continue
        unmet.append({"label": label, "reason": reason, **amendment})
    error = _get(dv, "error_code")
    if acs and (_get(dv, "verify_status") != "verified" or _truthy(error)):
        reason = (
            f"verify_status={_js_string(_or(_get(dv, 'verify_status'), 'not run'))}"
        )
        if _truthy(error):
            reason += f" error={_js_string(error)}"
        unmet.append({"label": "dod_verify", "reason": reason})
    return _out(
        id=_get(ticket, "id"),
        decision="open" if unmet else "done",
        unmet=unmet,
        needs_amendment=any(_truthy(_get(u, "amendment")) for u in unmet),
    )


def unmet_signature(unmet: Sequence[object]) -> str:
    rows = [
        f"{_js_string(_get(u, 'label'))}:{_js_trim(re.split(r'[:(]', _js_string(_get(u, 'reason')))[0])}"
        for u in unmet
    ]
    return ",".join(sorted(rows, key=_utf16))


def comment_signature(unmet: Sequence[object], text_sha: object) -> str:
    held = any(_truthy(_get(u, "amendment")) for u in unmet) and TEXT_SHA.search(
        _js_string(_or(text_sha, ""))
    )
    return unmet_signature(unmet) + (f" text={_js_string(text_sha)}" if held else "")


def open_comment(
    identifier: object, run_key: object, unmet: Sequence[object], text_sha: object
) -> str:
    amendments = [u for u in unmet if _truthy(_get(u, "amendment"))]
    sha = text_sha if TEXT_SHA.search(_js_string(_or(text_sha, ""))) else ""
    lines = [
        "actor: dod-closeout-sweep (sonnet)",
        f"dod-closeout-sweep run {_js_string(run_key)}: {_js_string(identifier)} stays open. These acceptance criteria are not proven by a check another lane accepted and dod_verify verified:",
        *(
            f"- {_js_string(_get(u, 'label'))}: {_js_string(_get(u, 'reason'))}"
            for u in unmet
        ),
    ]
    if amendments:
        lines.append(
            "Amendment needed. These criteria cannot be proven as written, so no run can close the ticket until its text changes:"
        )
        lines.extend(
            f"- {_js_string(_get(u, 'label'))}: {_js_string(_get(u, 'amendment'))}"
            for u in amendments
        )
        lines.append(
            "The closer skips this ticket until its description changes, then examines it again."
            if _truthy(sha)
            else "The closer re-examines this ticket on a later run."
        )
        if _truthy(sha):
            lines.append(f"amendment-text-sha={_js_string(sha)}")
    else:
        lines.append(
            "The closer re-examines this ticket on a later run. It moves to Done when every criterion has an accepted, verified check."
        )
    lines.append(f"signature={comment_signature(unmet, sha)}")
    return "\n".join(lines).replace("?", "")


def _map_key(value: object) -> tuple[type[object], object]:
    # Maps compare primitive types separately and objects by identity.
    return type(value), id(value) if isinstance(value, (dict, list)) else value


def _tickets_by_id(result: object) -> dict[tuple[type[object], object], object]:
    return {
        _map_key(_get(t, "id")): t
        for t in _array(_get(result, "tickets"))
        if _truthy(_get(t, "id"))
    }


def vet(
    bind_result: object, chunk: ModelChunkRef, run_key: str
) -> tuple[dict[str, JsonType], ...]:
    by_id = _tickets_by_id(bind_result)
    out: list[dict[str, JsonType]] = []
    for identifier in chunk.tickets:
        ticket = by_id.get(_map_key(identifier), _MISSING)
        if ticket is _MISSING:
            out.append(
                _out(
                    id=identifier,
                    missing="the Bind agent returned nothing for this ticket",
                    acs=[],
                )
            )
            continue
        acs = []
        for ac in _array(_get(ticket, "acs")):
            check = _or(normalize_check(_get(ac, "check")), None)
            proposed = _truthy(check)
            refused = (
                refusal_of(
                    {
                        **_spread(check),
                        **_out(label=_or(_get(check, "label"), _get(ac, "label"))),
                    }
                )
                if proposed
                else None
            )
            acs.append(
                _out(
                    label=_get(ac, "label"),
                    text=_get(ac, "text"),
                    proposed=proposed,
                    refused=refused,
                    reason=_or(_get(ac, "reason"), ""),
                    amendment="" if proposed else _or(_get(ac, "amendment"), ""),
                    check=check,
                    proposed_by=f"lane=dod-closeout-bind-{run_key}-c{chunk.index}",
                )
            )
        out.append(
            _out(
                id=identifier,
                criteria_note=_or(_get(ticket, "criteria_note"), ""),
                criteria_amendment=""
                if acs
                else _or(_get(ticket, "criteria_amendment"), ""),
                acs=acs,
            )
        )
    return tuple(out)


def for_review(
    vetted: Sequence[dict[str, JsonType]],
) -> tuple[dict[str, JsonType], ...]:
    out = []
    for v in vetted:
        acs = [
            _out(
                label=_get(ac, "label"),
                text=_get(ac, "text"),
                proposed_by=_get(ac, "proposed_by"),
                check=None if _truthy(_get(ac, "refused")) else _get(ac, "check"),
                refused=_or(_get(ac, "refused"), ""),
                binder_reason=_get(ac, "reason"),
            )
            for ac in _array(_get(v, "acs"))
        ]
        out.append(
            _out(
                id=_get(v, "id"),
                criteria_note=_or(
                    _get(v, "criteria_note"), _or(_get(v, "missing"), "")
                ),
                acs=acs,
            )
        )
    return tuple(out)


def merge(
    vetted: Sequence[dict[str, JsonType]],
    accept_result: object,
    chunk: ModelChunkRef,
    run_key: str,
) -> tuple[dict[str, JsonType], ...]:
    by_id = _tickets_by_id(accept_result)
    out = []
    for v in vetted:
        a = by_id.get(_map_key(_get(v, "id")), _MISSING)
        if _truthy(_get(v, "missing")):
            out.append(
                _out(
                    id=_get(v, "id"),
                    acs=[],
                    criteria_note=_get(v, "missing"),
                    dod_verify={},
                )
            )
            continue
        common = _out(
            id=_get(v, "id"),
            criteria_note=_get(v, "criteria_note"),
            criteria_amendment=_get(v, "criteria_amendment"),
        )
        if a is _MISSING:
            acs = [
                {
                    **_spread(ac),
                    "accepted": False,
                    "reason": cast(
                        JsonType,
                        _or(
                            _get(ac, "reason"),
                            "the Accept agent returned nothing for this ticket",
                        ),
                    ),
                }
                for ac in _array(_get(v, "acs"))
            ]
            out.append({**common, **_out(acs=acs, dod_verify={})})
            continue
        by_label = {_map_key(_get(x, "label")): x for x in _array(_get(a, "acs"))}
        acs = []
        for ac in _array(_get(v, "acs")):
            x = by_label.get(_map_key(_get(ac, "label")), _MISSING)
            accepted = (
                _truthy(_get(x, "accepted"))
                and not _truthy(_get(ac, "refused"))
                and _truthy(_get(ac, "proposed"))
            )
            amendment = (
                _get(ac, "amendment")
                if not _truthy(_get(ac, "proposed"))
                else _or(
                    _get(x, "amendment")
                    if not accepted and not _truthy(_get(ac, "refused"))
                    else "",
                    "",
                )
            )
            reason = (
                _get(x, "reason")
                if x is not _MISSING
                else _or(_get(ac, "reason"), "not reviewed by the acceptor")
            )
            row = _spread(ac)
            # An explicit undefined value in the spread replaces an existing key.
            row.pop("reason", None)
            row.pop("amendment", None)
            row.update(
                _out(
                    accepted=accepted,
                    amendment=amendment,
                    reason=reason,
                    accepted_by=f"lane=dod-closeout-accept-{run_key}-c{chunk.index}"
                    if accepted
                    else "",
                )
            )
            acs.append(row)
        reaccepted = _get(a, "reaccepted")
        out.append(
            {
                **common,
                **_out(
                    acs=acs,
                    dod_verify=read_dod_verify(
                        _get(_get(a, "dod_verify"), "receipt_json")
                    ),
                    evidence_pr=_or(_get(a, "evidence_pr"), ""),
                    reaccepted=reaccepted if isinstance(reaccepted, list) else [],
                    fenced_midrun=_truthy(_get(a, "fenced_midrun")),
                ),
            }
        )
    return tuple(out)


def decide_chunk(
    vetted: Sequence[dict[str, JsonType]],
    accept_result: object,
    chunk: ModelChunkRef,
    run_key: str,
    apply: bool,
    text_sha_by_id: dict[str, str],
) -> tuple[
    tuple[dict[str, JsonType], ...],
    tuple[dict[str, JsonType], ...],
    tuple[dict[str, JsonType], ...],
    tuple[dict[str, JsonType], ...],
]:
    merged = merge(vetted, accept_result, chunk, run_key)
    decisions = []
    for ticket in merged:
        decision = decide_ticket(ticket)
        if _truthy(_get(ticket, "fenced_midrun")):
            decision["decision"] = "fenced"
        else:
            decision.update(
                _out(
                    evidence_pr=_or(_get(ticket, "evidence_pr"), ""),
                    reaccepted=_or(_get(ticket, "reaccepted"), []),
                    dod_verify=_or(_get(ticket, "dod_verify"), {}),
                )
            )
        decisions.append(decision)
    writable = [d for d in decisions if d["decision"] in ("done", "open")]
    flips, opens = [], []
    if apply and writable:
        for d in writable:
            if d["decision"] == "done":
                flips.append(
                    _out(
                        id=_get(d, "id"),
                        evidence_pr=_get(d, "evidence_pr"),
                        dod_verify=_get(d, "dod_verify"),
                    )
                )
            else:
                sha = (
                    text_sha_by_id.get(_js_string(_get(d, "id")), "")
                    if _truthy(_get(d, "needs_amendment"))
                    else ""
                )
                unmet = _array(_get(d, "unmet"))
                opens.append(
                    _out(
                        id=_get(d, "id"),
                        signature=f"signature={comment_signature(unmet, sha)}",
                        comment=open_comment(_get(d, "id"), run_key, unmet, sha),
                    )
                )
    return merged, tuple(decisions), tuple(flips), tuple(opens)


def check_delegation(result: object, precheck_only: bool) -> ModelDelegationCheck:
    cell = _get(result, "delegation")
    count, runs, reason = (
        _get(cell, "delegated"),
        _get(cell, "runs"),
        _get(cell, "reason"),
    )
    problem = ""
    if not isinstance(cell, dict):
        problem = "missing or invalid cell"
    elif not _integer(count) or cast(int | float, count) < 0:
        problem = "invalid delegated count"
    elif not isinstance(runs, list) or not all(
        isinstance(r, str) and _js_trim(r) for r in runs
    ):
        problem = "invalid run ids"
    elif not isinstance(reason, str):
        problem = "missing or invalid reason"
    elif cast(int | float, count) > 0:
        if not runs or reason != "":
            problem = "positive delegation requires runs and an empty reason"
    elif runs or not (
        _regex(r"^(route-refused:\S+|route-unavailable:\S+)$").search(reason)
        or (precheck_only and reason == "read-only-lane")
    ):
        problem = "zero delegation requires a recorded route failure and no runs"
    return ModelDelegationCheck(ok=not problem, problem=problem)


class HandlerDodCloseoutDecision:
    """Stateless compute: one request kind in, one typed answer out."""

    def handle(
        self, request: ModelDodCloseoutDecisionRequest
    ) -> ModelDodCloseoutDecisionResult:
        kind = request.kind
        k = EnumDodCloseoutDecisionKind
        if kind is k.PARSE_ARGS and request.args is not None:
            return ModelDodCloseoutDecisionResult(
                kind=kind, config=parse_args(request.args)
            )
        if (
            kind is k.SELECT_CANDIDATES
            and request.candidates is not None
            and request.max_candidates is not None
        ):
            candidates: Sequence[dict[str, JsonType]] = request.candidates
            text_sha_by_id = {}
            if request.text_state is not None:
                candidates = with_text_state(
                    candidates, parse_text_state(request.text_state)
                )
                text_sha_by_id = {
                    _js_string(_get(c, "id")): _js_string(_get(c, "text_sha"))
                    for c in candidates
                }
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                selection=select_candidates(
                    candidates, request.max_candidates, text_sha_by_id
                ),
            )
        if (
            kind is k.CHUNK_LIST
            and request.items is not None
            and request.size is not None
        ):
            chunks = tuple(
                ModelChunkRef(
                    index=i // request.size, tickets=request.items[i : i + request.size]
                )
                for i in range(0, len(request.items), request.size)
            )
            return ModelDodCloseoutDecisionResult(kind=kind, chunks=chunks)
        if kind is k.REFUSAL_OF and request.check is not None:
            refusal = refusal_of(request.check)
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                binding_refusal=ModelBindingRefusal(
                    refusal=refusal,
                    reason=refusal_reason(refusal) if refusal is not None else None,
                ),
            )
        if (
            kind is k.VET_BINDINGS
            and request.bind_result is not None
            and request.chunk is not None
            and request.run_key is not None
        ):
            vetted = vet(request.bind_result, request.chunk, request.run_key)
            return ModelDodCloseoutDecisionResult(
                kind=kind, vetted=vetted, for_review=for_review(vetted)
            )
        if (
            kind is k.DECIDE_CHUNK
            and request.vetted is not None
            and request.accept_result is not None
            and request.chunk is not None
            and request.run_key is not None
            and request.apply is not None
            and request.text_sha_by_id is not None
        ):
            merged, decisions, flips, opens = decide_chunk(
                request.vetted,
                request.accept_result,
                request.chunk,
                request.run_key,
                request.apply,
                request.text_sha_by_id,
            )
            return ModelDodCloseoutDecisionResult(
                kind=kind, merged=merged, decisions=decisions, flips=flips, opens=opens
            )
        if kind is k.DECIDE_TICKET and request.ticket is not None:
            d = decide_ticket(request.ticket)
            decision = ModelTicketDecision(
                id=d.get("id"),
                decision="done" if d["decision"] == "done" else "open",
                unmet=tuple(
                    ModelUnmetCriterion(
                        label=_js_string(_get(u, "label")),
                        reason=_js_string(_get(u, "reason")),
                        amendment=_string_field(u, "amendment") or None,
                    )
                    for u in _array(d["unmet"])
                ),
                needs_amendment=_truthy(d["needs_amendment"]),
            )
            return ModelDodCloseoutDecisionResult(kind=kind, decision=decision)
        if (
            kind is k.OPEN_COMMENT
            and request.ticket_id is not None
            and request.run_key is not None
            and request.unmet is not None
        ):
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                open_comment=ModelOpenComment(
                    comment=open_comment(
                        request.ticket_id,
                        request.run_key,
                        request.unmet,
                        request.text_sha,
                    ),
                    signature=comment_signature(request.unmet, request.text_sha),
                ),
            )
        if kind is k.READ_DOD_VERIFY and request.receipt_json is not None:
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                dod_verify=ModelDodVerifyReading.model_validate(
                    read_dod_verify(request.receipt_json)
                ),
            )
        if kind is k.MERGED_SINCE and request.date is not None:
            y, m, d_day = (int(v) for v in request.date.split("-"))
            since = _iso(_utc(y, m, d_day) - timedelta(days=7))[:10]
            return ModelDodCloseoutDecisionResult(kind=kind, merged_since=since)
        if (
            kind is k.CHECK_DELEGATION
            and request.delegation_result is not None
            and request.precheck_only is not None
        ):
            return ModelDodCloseoutDecisionResult(
                kind=kind,
                delegation_check=check_delegation(
                    request.delegation_result, request.precheck_only
                ),
            )
        raise ValueError(f"{kind.value}: request is missing its inputs")
