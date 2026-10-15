# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure documentation oracle for the retry_delay case of the delegation walk.

The answer text is parsed, never executed. Doctest examples are checked by a
restricted evaluator (literal arguments, an allowlist of calls) against the
caller's reference function, which is only reachable once the answer's code is
proven identical to it. Anything the checker cannot match is a FAIL.
"""

import ast
import doctest
import re
from collections.abc import Callable
from typing import Any

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_doc_check_outcome import (
    EnumDocCheckOutcome,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_doc_check_request import (
    DOC_CHECK_FUNCTION,
    ModelDocCheckRequest,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_doc_check_verdict import (
    ModelDocCheckVerdict,
)

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n[ \t]*```", re.S)
_SECTION = re.compile(
    r"^(Args|Arguments|Parameters|Returns|Return|Raises|Examples?|Example):\s*$"
)
_SECTION_NAME = {
    "Arguments": "Args",
    "Parameters": "Args",
    "Return": "Returns",
    "Example": "Examples",
}
_ENTRY = re.compile(r"^(\s*)`?(\w+)`?\s*(\([^)]*\))?\s*:\s*(.*)$")

# parameter: (must match one of, must match none of)
_RANGES: dict[str, tuple[list[str], list[str]]] = {
    "attempt": (
        [
            r">=\s*1\b",
            r"≥\s*1\b",
            r"at least\s*(?:1|one)\b",
            r"greater than or equal to\s*(?:1|one)\b",
            r"starts? (?:at|from) (?:1|one)\b",
            r"1-based",
            r"\b1 or (?:more|greater|higher)\b",
        ],
        [
            r">=\s*0\b",
            r"at least\s*(?:0|zero)\b",
            r"0-based",
            r"\bnon-?negative\b",
            r">\s*1\b(?!\.)",
        ],
    ),
    "base": (
        [
            r">\s*0(?:\.0)?(?!\d|\.\d)",
            r"\bpositive\b",
            r"greater than\s*(?:0|zero)\b",
            r"strictly above zero",
        ],
        [
            r">=\s*0(?!\d|\.\d)",
            r"\bnon-?negative\b",
            r"at least\s*(?:0|zero)\b",
            r"may be (?:0|zero)\b",
        ],
    ),
    "cap": (
        [
            r">\s*0(?:\.0)?(?!\d|\.\d)",
            r"\bpositive\b",
            r"greater than\s*(?:0|zero)\b",
            r"strictly above zero",
        ],
        [
            r">=\s*0(?!\d|\.\d)",
            r"\bnon-?negative\b",
            r"at least\s*(?:0|zero)\b",
            r"may be (?:0|zero)\b",
        ],
    ),
    "jitter": (
        [
            r"\[\s*0(?:\.0)?\s*,\s*1(?:\.0)?\s*\)",
            r"0(?:\.0)?\s*<=\s*jitter\s*<\s*1(?:\.0)?",
            r"at least\s*0(?:\.0)?\s*(?:\(inclusive\)\s*)?(?:and|,)\s*(?:strictly\s*)?(?:less than|below)\s*1(?:\.0)?",
            r"from 0(?:\.0)? \(inclusive\) (?:up )?to 1(?:\.0)? \(exclusive\)",
            r"\b0(?:\.0)? (?:inclusive )?(?:up )?to (?:but not including|just under|below) 1(?:\.0)?\b",
        ],
        [
            r"\[\s*0(?:\.0)?\s*,\s*1(?:\.0)?\s*\]",
            r"\(\s*0(?:\.0)?\s*,",
            r"<=\s*1\b",
            r"\bup to and including 1\b",
            r"0(?:\.0)?\s*<\s*jitter",
        ],
    ),
}

_CONDITIONS = {
    "attempt below 1": r"attempt[^.;]*(?:<\s*1\b|less than\s*(?:1|one)\b|below\s*(?:1|one)\b|not (?:at least|>=)\s*1\b|zero|negative)|(?:<\s*1\b|less than\s*(?:1|one)\b|below\s*(?:1|one)\b)[^.;]*attempt",
    "base or cap not positive": r"(?:base|cap)[^.;]*(?:<=\s*0|not positive|non-?positive|zero or (?:less|negative)|less than or equal to (?:0|zero)|(?:is|are) (?:zero|negative)|<\s*=\s*0)",
    "jitter outside [0, 1)": r"jitter[^.;]*(?:outside|not in|not within|<\s*0|>=\s*1|negative|greater than or equal to 1|out of)",
}

_ALLOWED_BUILTINS: dict[str, Callable[..., Any]] = {
    "round": round,
    "min": min,
    "max": max,
    "abs": abs,
    "float": float,
    "int": int,
}
_REFERENCE_BUILTINS: dict[str, Any] = {**_ALLOWED_BUILTINS, "ValueError": ValueError}
_ALLOWED_NODES = (
    ast.Expression,
    ast.Call,
    ast.keyword,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Tuple,
    ast.List,
)
_MAX_LITERAL = 10_000
_MIN_DOCTESTS = 2


def _find(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.I) is not None


def _answer_source(answer_text: str) -> tuple[str, str | None]:
    blocks = _FENCE.findall(answer_text)
    if not blocks:
        return answer_text, None
    if len(blocks) != 1:
        return "", f"the answer must be one fenced code block, found {len(blocks)}"
    return blocks[0], None


def _function(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == DOC_CHECK_FUNCTION:
            return node
    return None


def _stripped_code(function: ast.FunctionDef) -> str:
    body = list(function.body)
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        body = body[1:]
    returns = ast.dump(function.returns) if function.returns else ""
    return (
        ast.dump(ast.Module(body=body, type_ignores=[]))
        + ast.dump(function.args)
        + returns
    )


def _sections(doc: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in doc.splitlines():
        match = _SECTION.match(line)
        if match:
            current = _SECTION_NAME.get(match.group(1), match.group(1))
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def _entries(lines: list[str]) -> dict[str, str]:
    """Map entry name to its text, joining continuation lines indented deeper."""
    out: dict[str, str] = {}
    name: str | None = None
    indent = 0
    for line in lines:
        match = _ENTRY.match(line)
        if match and (name is None or len(match.group(1)) <= indent):
            name, indent = match.group(2), len(match.group(1))
            out[name] = match.group(4)
        elif name and line.strip():
            out[name] += " " + line.strip()
    return out


def _check_args(
    params: list[str],
    defaults: dict[str, float],
    args: dict[str, str],
    fails: list[str],
) -> None:
    for param in params:
        text = args.get(param)
        if text is None:
            fails.append(f"parameter not documented in Args: {param}")
            continue
        stated = re.findall(
            rf"default(?:s)?(?:\s+(?:to|is|of|value(?:\s+is)?))?\s*[:=]?\s*`?({_NUM})`?",
            text,
            re.I,
        )
        if param in defaults:
            if not stated:
                fails.append(
                    f"default of {param} ({defaults[param]}) not stated in its Args entry"
                )
            for value in stated:
                if float(value) != float(defaults[param]):
                    fails.append(
                        f"wrong default for {param}: states {value}, code has {defaults[param]}"
                    )
            if _find(r"\brequired\b|\bmandatory\b", text) and not _find(
                r"\bnot required\b|\boptional\b", text
            ):
                fails.append(f"{param} is optional but documented as required")
        else:
            if stated:
                fails.append(
                    f"{param} has no default but the docstring invents one: {stated[0]}"
                )
            if not _find(r"\brequired\b|\bno default\b|\bmandatory\b", text):
                fails.append(
                    f"{param} has no default and its entry does not say it is required"
                )
    for param, (must, never) in _RANGES.items():
        text = args.get(param, "")
        if not any(_find(pattern, text) for pattern in must):
            fails.append(f"valid range of {param} not stated correctly")
        for pattern in never:
            if _find(pattern, text):
                fails.append(f"false range for {param}: matches /{pattern}/")


def _check_returns(
    doc: str, sections: dict[str, list[str]], args: dict[str, str], fails: list[str]
) -> None:
    ret = " ".join(line.strip() for line in sections.get("Returns", []))
    if not _find(r"\bfloat\b", ret):
        fails.append("Returns does not say float")
    if _find(r"\bint(eger)?\b", ret):
        fails.append("Returns says int, the code returns a float")
    if not _find(r"\bseconds?\b", ret + " " + " ".join(args.values())):
        fails.append("the unit (seconds) is not stated")
    if _find(r"millisecond|\bms\b|minutes?\b", doc):
        fails.append("wrong unit: the delay is in seconds")
    if not _find(
        r"2\s*\*\*\s*\(\s*attempt\s*-\s*1\s*\)|2\s*\^\s*\(\s*attempt\s*-\s*1\s*\)|doubl|exponential|powers? of (?:2|two)",
        ret + doc,
    ):
        fails.append("the exponential (doubling) formula is not stated")
    if _find(
        r"\blinear(?:ly)?\b|\barithmetic\b|\badditive(?:ly)?\b|\bconstant(?:ly)? (?:delay|backoff)|\bfixed (?:delay|backoff)|\bquadratic|\bfibonacci",
        doc,
    ):
        fails.append("the growth is described as something other than doubling")
    if not _find(
        r"capped|at most|no more than|never exceed|not exceed|min\(|upper bound|limited to|clamped",
        doc,
    ):
        fails.append("the cap is not stated as an upper bound of the delay")
    if not _find(
        r"1\s*-\s*jitter|\(1\s*-\s*jitter\)|reduced by|scaled down|shorter by|fraction",
        ret + " " + args.get("jitter", ""),
    ):
        fails.append("the jitter reduction (delay * (1 - jitter)) is not stated")
    if _find(r"\brandom|\buniform|\bstochastic|\bnon-?deterministic", doc):
        fails.append("jitter is documented as random; the code is deterministic")
    if _find(
        r"\b(?:added|adds|increase[sd]?|longer)\b[^.]*jitter|jitter[^.]*\b(?:added|adds|increase[sd]?)\b",
        doc,
    ):
        fails.append(
            "jitter is documented as increasing the delay; the code reduces it"
        )


def _check_raises(doc: str, sections: dict[str, list[str]], fails: list[str]) -> None:
    raises = " ".join(line.strip() for line in sections.get("Raises", []))
    if "ValueError" not in raises:
        fails.append("Raises does not name ValueError")
    others = set(re.findall(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Warning))\b", doc))
    for other in sorted(others - {"ValueError"}):
        fails.append(f"documents an exception the code never raises: {other}")
    if _find(
        r"network|connect|timeout|timed out|socket|\bio\b|retr(?:y|ies) (?:exhaust|fail)",
        raises,
    ):
        fails.append(
            "Raises gives a condition the code never checks (network or timeout)"
        )
    for name, pattern in _CONDITIONS.items():
        if not _find(pattern, raises):
            fails.append(f"Raises does not state the condition: {name}")


def _safe_expression(source: str) -> ast.Expression | None:
    """Parse one doctest source line; None unless it is a call of an allowed shape."""
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return None
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, str | bytes | complex)
            or (isinstance(node.value, int | float) and abs(node.value) > _MAX_LITERAL)
        ):
            return None
        if isinstance(node, ast.Call):
            func = node.func
            allowed = {DOC_CHECK_FUNCTION, *_ALLOWED_BUILTINS}
            if not isinstance(func, ast.Name) or func.id not in allowed:
                return None
            if any(kw.arg is None for kw in node.keywords) or any(
                isinstance(arg, ast.Starred) for arg in node.args
            ):
                return None
        elif isinstance(node, ast.Name) and not (
            node.id == DOC_CHECK_FUNCTION or node.id in _ALLOWED_BUILTINS
        ):
            return None
    return tree


def _run_doctests(
    doc: str,
    reference_function: Callable[..., float],
    fails: list[str],
) -> None:
    examples = doctest.DocTestParser().get_examples(doc)
    if len(examples) < _MIN_DOCTESTS:
        fails.append(f"fewer than {_MIN_DOCTESTS} doctest examples ({len(examples)})")
    if not all(DOC_CHECK_FUNCTION in example.source for example in examples):
        fails.append(f"a doctest example does not call {DOC_CHECK_FUNCTION}")
    namespace: dict[str, Any] = {
        "__builtins__": {},
        DOC_CHECK_FUNCTION: reference_function,
        **_ALLOWED_BUILTINS,
    }
    checker = doctest.OutputChecker()
    failed = 0
    for example in examples:
        tree = _safe_expression(example.source)
        if tree is None:
            failed += 1
            fails.append(
                "doctest: example is not a plain call with literal arguments: "
                + example.source.strip()
            )
            continue
        flags = 0
        for flag, enabled in example.options.items():
            if enabled:
                flags |= flag
        got = ""
        raised: str | None = None
        try:
            value = eval(compile(tree, "<doctest>", "eval"), namespace)
            got = "" if value is None else repr(value) + "\n"
        except Exception as exc:
            raised = f"{type(exc).__name__}: {exc}\n"
        if example.exc_msg is not None:
            ok = raised is not None and checker.check_output(
                example.exc_msg, raised, flags
            )
            shown = raised if raised is not None else got
        else:
            ok = raised is None and checker.check_output(example.want, got, flags)
            shown = raised if raised is not None else got
        if not ok:
            failed += 1
            expected = example.exc_msg if example.exc_msg is not None else example.want
            fails.append(
                f"doctest: {example.source.strip()} expected {expected.strip()!r}, got {shown.strip()!r}"
            )
    if failed:
        fails.append(f"{failed} doctest example(s) failed")


class HandlerDelegationDocCheck:
    """Grade a documentation draw from supplied text without running it."""

    def handle(self, request: ModelDocCheckRequest) -> ModelDocCheckVerdict:
        failures = self._failures(request)
        return ModelDocCheckVerdict(
            outcome=EnumDocCheckOutcome.FAIL if failures else EnumDocCheckOutcome.PASS,
            run_directory=request.run_directory,
            failures=tuple(dict.fromkeys(failures)),
        )

    def _failures(self, request: ModelDocCheckRequest) -> list[str]:
        source, fence_problem = _answer_source(request.answer_text)
        if fence_problem:
            return [fence_problem]
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [f"the answer does not parse as Python: {exc.msg}"]
        function = _function(tree)
        if function is None:
            return [f"the answer does not define {DOC_CHECK_FUNCTION}"]
        reference = ast.parse(request.reference_source)
        reference_function = _function(reference)
        if reference_function is None:  # unreachable: the request validated it
            return ["reference_source does not define the function"]

        fails: list[str] = []
        code_unchanged = _stripped_code(function) == _stripped_code(reference_function)
        if not code_unchanged:
            fails.append("the code was changed: only the docstring may be added")
        doc = ast.get_docstring(function) or ""
        sections = _sections(doc)
        for need in ("Args", "Returns", "Raises"):
            if need not in sections:
                fails.append(f"no {need} section")
        args = _entries(sections.get("Args", []))
        params = [a.arg for a in reference_function.args.args]
        defaults = dict(
            zip(
                reversed(params),
                reversed(
                    [
                        float(ast.literal_eval(d))
                        for d in reference_function.args.defaults
                    ]
                ),
                strict=False,
            )
        )
        _check_args(params, defaults, args, fails)
        _check_returns(doc, sections, args, fails)
        _check_raises(doc, sections, fails)

        namespace: dict[str, Any] = {"__builtins__": _REFERENCE_BUILTINS}
        exec(compile(request.reference_source, "<reference>", "exec"), namespace)
        _run_doctests(doc, namespace[DOC_CHECK_FUNCTION], fails)

        known = {*params, DOC_CHECK_FUNCTION, "ValueError", "float", "int", "min"}
        known |= {"True", "False", "None"}
        for ident in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc):
            if ident not in known:
                fails.append(f"invented identifier in backticks: {ident}")
        return fails
