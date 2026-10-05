# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI guard: savings has one definition, in node_metering_summary_compute (OMN-19980).

The savings figure is defined once, in ``node_metering_summary_compute`` (pure
compute; the caller pins the baseline price from the pricing manifest). This
guard fails when a second definition appears in SQL:

  R1  a string literal feeding a ``baseline_model`` column, for example
      ``'claude-opus-4.1' AS baseline_model`` or
      ``COALESCE(x, 'claude-opus-4.1') AS baseline_model``;
  R2  the LAST migration that defines each savings view still carries such a
      literal (the live definition, not the history);
  R3  the literal ``claude-opus-4.1`` appears anywhere under ``src/`` outside
      the five frozen historical migrations (076, 078, 079, 083, 087), and the
      frozen set is exact: a sixth holder fails, and a listed file that no
      longer holds the literal fails too, so the list can only shrink;
  R4  SQL multiplying a token column by a price column, in ``.sql`` files, in
      SQL held in Python string constants, and in YAML contract text, anywhere
      outside ``node_metering_summary_compute``;
  R5  ``.github/workflows/ci.yml`` runs this guard as a named, blocking step.

KNOWN GAP (measured, asserted below rather than left in a comment): price
arithmetic written in PYTHON, not SQL, exists in pricing.py, cost_pricing.py,
handler_llm_delegation_call.py and the ab-compare / demo cost nodes. The AC
this guard enforces is about SQL, so Python arithmetic is not scanned. SQL
assembled at runtime from fragments is not seen either. To widen the guard,
change ``_PYTHON_PRICE_ARITHMETIC_KNOWN_GAP`` and the planted test with it.

The page half (no page of the six shows a savings figure from an exposure other
than ``metering-summary.v1``) lives in omnidash: the T2.4 vitest page test. The
rules here are plain functions over a root directory so a sibling rule can be
added without touching these.

The module imports nothing from ``src`` on purpose: it reads files, so a change
that breaks an import cannot hide the guard, and it runs in under a second.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]

BASELINE_LITERAL = "claude-opus-4.1"

_SAVINGS_MIGRATIONS = Path("src/omnimarket/nodes/node_projection_savings/migrations")

# The five applied, immutable migrations that still spell the literal. Each is
# superseded by a later CREATE OR REPLACE VIEW (R2 proves it). Shrink-only.
FROZEN_LITERAL_HOLDERS: frozenset[str] = frozenset(
    {
        f"{_SAVINGS_MIGRATIONS}/076_create_delegation_savings_projection_view.sql",
        f"{_SAVINGS_MIGRATIONS}/078_create_delegation_savings_series_projection_view.sql",
        f"{_SAVINGS_MIGRATIONS}/079_add_savings_series_tier_mix.sql",
        f"{_SAVINGS_MIGRATIONS}/083_reconcile_delegation_savings_projection_views.sql",
        f"{_SAVINGS_MIGRATIONS}/087_savings_views_read_persisted_provenance.sql",
    }
)

# Found while writing R1 against the real tree: 077 feeds baseline_model from
# COALESCE(NULLIF(model_cloud_baseline, ''), 'cloud-baseline'), a placeholder
# string, not the opus literal. Superseded by 089 (R2 proves it). Shrink-only.
FROZEN_PLACEHOLDER_BASELINES: frozenset[str] = frozenset(
    {
        f"{_SAVINGS_MIGRATIONS}/077_create_cost_savings_overview_projection_view.sql",
    }
)

SAVINGS_VIEWS: tuple[str, ...] = (
    "projection_delegation_savings",
    "projection_delegation_savings_series",
    "projection_cost_savings_overview",
)

# The only place token x price arithmetic may live.
_METERING_COMPUTE_DIR = "src/omnimarket/nodes/node_metering_summary_compute/"

_PYTHON_PRICE_ARITHMETIC_KNOWN_GAP: tuple[str, ...] = (
    "src/omnimarket/pricing.py",
    "src/omnimarket/cost/cost_pricing.py",
    "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/handler_llm_delegation_call.py",
    "src/omnimarket/nodes/node_ab_compare_orchestrator/handlers/handler_ab_compare_orchestrator.py",
    "src/omnimarket/nodes/node_demo_cost_compute/handlers/handler_cost_compute.py",
)

CI_STEP_NAME_FRAGMENT = "single savings definition"
CI_GUARD_PATH = "tests/test_single_savings_definition.py"

_SKIP_DIRS = frozenset({"__pycache__", ".venv", "node_modules", ".git"})
_TEXT_SUFFIXES = frozenset(
    {".py", ".sql", ".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".ts", ".tsx"}
)

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_BASELINE_ALIAS = re.compile(r"\bAS\s+baseline_model\b", re.IGNORECASE)
_SQL_LOOKING = re.compile(
    r"\bselect\b[\s\S]*?\bfrom\b|\bcreate\s+(?:or\s+replace\s+)?view\b"
    r"|\binsert\s+into\b|\bupdate\s+\w+\s+set\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# file walking
# --------------------------------------------------------------------------- #


def _walk(root: Path, suffixes: frozenset[str]) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --------------------------------------------------------------------------- #
# SQL text helpers
# --------------------------------------------------------------------------- #


def strip_sql_comments(sql: str) -> str:
    """Drop /* */ and -- comments, keeping line structure; strings are respected."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if sql[i] == "'":
            m = _STRING_LITERAL.match(sql, i)
            end = m.end() if m else n
            out.append(sql[i:end])
            i = end
        elif two == "--":
            while i < n and sql[i] != "\n":
                i += 1
        elif two == "/*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * sql.count("\n", i, end))
            i = end
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _mask_strings(sql: str) -> str:
    """Same length, string contents blanked, so scans cannot be fooled by text."""
    return _STRING_LITERAL.sub(lambda m: "'" + "x" * (len(m.group(0)) - 2) + "'", sql)


def _expression_before(masked: str, end: int) -> tuple[int, int]:
    """Span of the select-list expression that ends at ``end`` (a column alias)."""
    depth = 0
    i = end - 1
    while i >= 0:
        c = masked[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i -= 1
    start = i + 1
    selects = list(re.finditer(r"\bSELECT\b", masked[start:end], re.IGNORECASE))
    if selects:
        start += selects[-1].end()
    return start, end


def baseline_literal_findings(sql: str) -> list[str]:
    """R1: string literals that feed a ``baseline_model`` column. Comments ignored."""
    text = strip_sql_comments(sql)
    masked = _mask_strings(text)
    found: list[str] = []
    for alias in _BASELINE_ALIAS.finditer(masked):
        start, end = _expression_before(masked, alias.start())
        for literal in _STRING_LITERAL.finditer(text, start, end):
            line = text.count("\n", 0, literal.start()) + 1
            found.append(f"line {line}: {literal.group(0)} AS baseline_model")
    return found


def _view_definitions(sql: str, view: str) -> list[str]:
    """Bodies of every CREATE [OR REPLACE] VIEW <view> statement, comments removed."""
    text = strip_sql_comments(sql)
    masked = _mask_strings(text)
    bodies: list[str] = []
    pattern = re.compile(
        rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:public\.)?{re.escape(view)}\b",
        re.IGNORECASE,
    )
    for m in pattern.finditer(masked):
        semi = masked.find(";", m.end())
        bodies.append(text[m.start() : len(text) if semi == -1 else semi])
    return bodies


def last_definition_findings(root: Path) -> list[str]:
    """R2: the live (last) definition of each savings view carries no literal."""
    problems: list[str] = []
    migrations = root / _SAVINGS_MIGRATIONS
    ordered = sorted(migrations.glob("*.sql")) if migrations.is_dir() else []
    other = [
        p
        for p in _walk(root / "src", frozenset({".sql"}))
        if migrations not in p.parents
    ]
    for view in SAVINGS_VIEWS:
        last: tuple[Path, str] | None = None
        for path in ordered:
            for body in _view_definitions(path.read_text(errors="ignore"), view):
                last = (path, body)
        if last is None:
            problems.append(f"{view}: no CREATE VIEW found in {_SAVINGS_MIGRATIONS}")
        else:
            problems.extend(
                f"{view}: last definition {_rel(root, last[0])} {f}"
                for f in baseline_literal_findings(last[1])
            )
        # A definition outside the savings node has no known apply order here,
        # so every one of them must be clean.
        for path in other:
            for body in _view_definitions(path.read_text(errors="ignore"), view):
                problems.extend(
                    f"{view}: {_rel(root, path)} {f}"
                    for f in baseline_literal_findings(body)
                )
    return problems


def baseline_literal_holders(root: Path) -> set[str]:
    """R3: every file under src/ that spells the literal, comments included."""
    needle = BASELINE_LITERAL.lower()
    return {
        _rel(root, p)
        for p in _walk(root / "src", _TEXT_SUFFIXES)
        if needle in p.read_text(errors="ignore").lower()
    }


def frozen_holder_problems(root: Path) -> list[str]:
    """R3: the literal holders must equal the frozen set, in both directions."""
    holders = baseline_literal_holders(root)
    problems = [
        f"{BASELINE_LITERAL} spelled in: {name}"
        for name in sorted(holders - FROZEN_LITERAL_HOLDERS)
    ]
    problems += [
        f"frozen entry no longer holds the literal, remove it: {name}"
        for name in sorted(FROZEN_LITERAL_HOLDERS - holders)
    ]
    return problems


def literal_baseline_in_sql_files(root: Path) -> dict[str, list[str]]:
    """R1 over every .sql file under src/ (the frozen files are expected here)."""
    out: dict[str, list[str]] = {}
    for p in _walk(root / "src", frozenset({".sql"})):
        hits = baseline_literal_findings(p.read_text(errors="ignore"))
        if hits:
            out[_rel(root, p)] = hits
    return out


# --------------------------------------------------------------------------- #
# R4: tokens x price in SQL
# --------------------------------------------------------------------------- #

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRICE_WORDS = frozenset({"price", "prices", "rate", "rates", "pricing"})


def _is_token_name(name: str) -> bool:
    return any(part in {"token", "tokens"} for part in name.lower().split("_"))


def _is_price_name(name: str) -> bool:
    parts = name.lower().split("_")
    if any(part in _PRICE_WORDS for part in parts):
        return True
    return "per" in parts and any(
        part in {"1k", "1m", "token", "tokens", "million", "thousand"} for part in parts
    )


def _operand_left(text: str, star: int) -> str:
    i = star - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    # `tokens / 1000.0 * price`: step over a numeric divisor to its dividend.
    m = re.search(r"/\s*[0-9.]+\s*$", text[: i + 1])
    if m:
        i = m.start() - 1
        while i >= 0 and text[i].isspace():
            i -= 1
    end = i + 1
    if i >= 0 and text[i] == ")":
        depth = 0
        while i >= 0:
            if text[i] == ")":
                depth += 1
            elif text[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        i -= 1
        while i >= 0 and (text[i].isalnum() or text[i] in "_."):
            i -= 1
        return text[i + 1 : end]
    # strip a ::cast, then take the dotted identifier
    cast = re.search(r"::\s*[A-Za-z_][A-Za-z0-9_]*(?:\([0-9, ]*\))?\s*$", text[:end])
    if cast:
        end = cast.start()
        i = end - 1
    while i >= 0 and (text[i].isalnum() or text[i] in "_."):
        i -= 1
    return text[i + 1 : end]


def _operand_right(text: str, star: int) -> str:
    i = star + 1
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    start = i
    if i < n and text[i] == "(":
        depth = 0
        while i < n:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        return text[start:i]
    while i < n and (text[i].isalnum() or text[i] in "_."):
        i += 1
    # a function call: include its argument list
    if i < n and text[i] == "(":
        depth = 0
        while i < n:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
    return text[start:i]


def token_price_findings(sql: str) -> list[str]:
    """R4: a multiplication with a token operand on one side and a price on the other."""
    text = strip_sql_comments(sql)
    masked = _mask_strings(text)
    found: list[str] = []
    for star in re.finditer(r"\*", masked):
        pos = star.start()
        left, right = _operand_left(masked, pos), _operand_right(masked, pos)
        l_names = _IDENT.findall(left)
        r_names = _IDENT.findall(right)
        crossed = (
            any(_is_token_name(n) for n in l_names)
            and any(_is_price_name(n) for n in r_names)
        ) or (
            any(_is_price_name(n) for n in l_names)
            and any(_is_token_name(n) for n in r_names)
        )
        if crossed:
            line = masked.count("\n", 0, pos) + 1
            found.append(f"line {line}: {left.strip()} * {right.strip()}")
    return found


def _python_sql_strings(source: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SQL_LOOKING.search(node.value)
        ):
            yield node.lineno, node.value


def _yaml_text(raw: str) -> str:
    return "\n".join(
        re.sub(r"\s#.*$", "", line)
        for line in raw.splitlines()
        if not line.lstrip().startswith("#")
    )


def token_price_findings_in_tree(root: Path) -> tuple[dict[str, list[str]], int]:
    """R4 over src/: (findings by file, number of files scanned)."""
    out: dict[str, list[str]] = {}
    scanned = 0
    for path in _walk(root / "src", frozenset({".sql", ".py", ".yaml", ".yml"})):
        rel = _rel(root, path)
        if rel.startswith(_METERING_COMPUTE_DIR):
            continue
        scanned += 1
        raw = path.read_text(errors="ignore")
        hits: list[str] = []
        if path.suffix == ".sql":
            hits = token_price_findings(raw)
        elif path.suffix == ".py":
            for lineno, text in _python_sql_strings(raw):
                hits += [f"py line {lineno}: {h}" for h in token_price_findings(text)]
        else:
            text = _yaml_text(raw)
            if _SQL_LOOKING.search(text):
                hits = token_price_findings(text)
        if hits:
            out[rel] = hits
    return out, scanned


# --------------------------------------------------------------------------- #
# R5: CI wiring
# --------------------------------------------------------------------------- #


def ci_guard_step_problems(root: Path) -> list[str]:
    ci = root / ".github/workflows/ci.yml"
    if not ci.is_file():
        return [f"{ci} is missing"]
    workflow: Any = yaml.safe_load(ci.read_text())
    matches: list[dict[str, Any]] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if CI_GUARD_PATH in str(step.get("run", "")):
                matches.append(step)
    if not matches:
        return [f"no ci.yml step runs {CI_GUARD_PATH}"]
    problems: list[str] = []
    for step in matches:
        if CI_STEP_NAME_FRAGMENT not in str(step.get("name", "")).lower():
            problems.append(f"step {step.get('name')!r} is not named for the guard")
        if step.get("continue-on-error") in (True, "true"):
            problems.append(f"step {step.get('name')!r} is continue-on-error")
        if step.get("if") is not None:
            problems.append(f"step {step.get('name')!r} is conditional: {step['if']}")
    return problems


# --------------------------------------------------------------------------- #
# planted-tree helper
# --------------------------------------------------------------------------- #


def _plant(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


_MIGRATIONS = _SAVINGS_MIGRATIONS.as_posix()


def _view(name: str, baseline_expr: str) -> str:
    return (
        f"CREATE OR REPLACE VIEW public.{name} AS\n"
        f"SELECT {baseline_expr} AS baseline_model, 1 AS n FROM public.savings_estimates;\n"
    )


def _clean_savings_tree(root: Path) -> Path:
    return _plant(
        root,
        {
            f"{_MIGRATIONS}/001_{v}.sql": _view(v, "model_cloud_baseline")
            for v in SAVINGS_VIEWS
        },
    )


# =========================================================================== #
# The real tree
# =========================================================================== #


@functools.cache
def _real_tree_r4() -> tuple[dict[str, list[str]], int]:
    """R4 over the real tree, computed once: it parses every Python file in src/."""
    return token_price_findings_in_tree(_REPO_ROOT)


class TestRealTree:
    """The guard on this repository. Each test names the AC it enforces."""

    def test_instrument_sees_the_corpus(self) -> None:
        # A zero is not a finding until the instrument is shown to see something
        # (WORKING_PREFERENCES: a scan that walks no files passes vacuously).
        migrations = list((_REPO_ROOT / _SAVINGS_MIGRATIONS).glob("*.sql"))
        assert len(migrations) >= 19, f"only {len(migrations)} savings migrations seen"
        _, scanned = _real_tree_r4()
        assert scanned >= 500, f"R4 scanned only {scanned} files"
        for name in FROZEN_LITERAL_HOLDERS:
            assert (_REPO_ROOT / name).is_file(), (
                f"frozen file moved or deleted: {name}"
            )

    def test_the_literal_scanner_flags_exactly_the_frozen_history(self) -> None:
        # Positive control on the real corpus with the real function, and the
        # R1 freeze: a literal feeding baseline_model is allowed only in the
        # five frozen files and the one frozen placeholder file, both history.
        hits = set(literal_baseline_in_sql_files(_REPO_ROOT))
        expected = FROZEN_LITERAL_HOLDERS | FROZEN_PLACEHOLDER_BASELINES
        assert hits - expected == set(), (
            f"new literal baseline: {sorted(hits - expected)}"
        )
        assert expected - hits == set(), (
            f"stale frozen entry: {sorted(expected - hits)}"
        )

    def test_last_definition_of_each_savings_view_has_no_literal_baseline(
        self,
    ) -> None:
        assert last_definition_findings(_REPO_ROOT) == []

    def test_literal_holders_are_exactly_the_frozen_set(self) -> None:
        assert frozen_holder_problems(_REPO_ROOT) == []

    def test_no_sql_multiplies_tokens_by_a_price_outside_the_metering_compute(
        self,
    ) -> None:
        findings, _ = _real_tree_r4()
        assert findings == {}

    def test_ci_runs_this_guard_as_a_named_blocking_step(self) -> None:
        assert ci_guard_step_problems(_REPO_ROOT) == []

    def test_python_price_arithmetic_is_the_known_unscanned_gap(self) -> None:
        # Measured 2026-10-04 on origin/dev 8ee3d6103: these files do arithmetic
        # on tokens and prices in Python, which is out of this guard's SQL scope.
        for rel in _PYTHON_PRICE_ARITHMETIC_KNOWN_GAP:
            path = _REPO_ROOT / rel
            assert path.is_file(), f"known-gap file moved: {rel}"
            assert re.search(r"tokens?\w*\)?\s*\*|\*\s*\w*tokens?", path.read_text()), (
                f"{rel} no longer multiplies tokens; update the known gap"
            )
        findings, _ = _real_tree_r4()
        assert not set(findings) & set(_PYTHON_PRICE_ARITHMETIC_KNOWN_GAP)


# =========================================================================== #
# Planted positive controls: each must be REPORTED
# =========================================================================== #


class TestPlantedBaselineLiteral:
    @pytest.mark.parametrize(
        "expr",
        [
            "'claude-opus-4.1'",
            "COALESCE(latest.baseline_model, 'claude-opus-4.1')",
            "'CLAUDE-OPUS-4.1'",
            "'claude-opus-4.1'::text",
            "COALESCE(a.x,\n        'claude-opus-4.1'\n    )",
            "'some-other-model'",
        ],
    )
    def test_literal_feeding_baseline_model_is_reported(self, expr: str) -> None:
        assert baseline_literal_findings(f"SELECT {expr} AS baseline_model") != []

    def test_the_ticket_planted_line_is_reported_exactly(self) -> None:
        sql = (
            "CREATE VIEW v AS SELECT\n    'claude-opus-4.1' AS baseline_model\nFROM t;"
        )
        assert baseline_literal_findings(sql) == [
            "line 2: 'claude-opus-4.1' AS baseline_model"
        ]

    def test_crlf_and_tabs_are_normalised(self) -> None:
        sql = "SELECT\r\n\t'claude-opus-4.1'\tAS\tbaseline_model\r\nFROM t;"
        assert baseline_literal_findings(sql) != []

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT model_cloud_baseline AS baseline_model FROM t",
            "SELECT NULL::text AS baseline_model FROM t",
            "SELECT latest.baseline_model AS baseline_model FROM latest",
            "SELECT 'claude-opus-4.1' AS some_other_column FROM t",
            "-- 'claude-opus-4.1' AS baseline_model\nSELECT 1",
            "/* COALESCE(x, 'claude-opus-4.1') AS baseline_model */ SELECT 1",
            "SELECT 'a,b' AS note, model_cloud_baseline AS baseline_model FROM t",
        ],
    )
    def test_clean_shapes_are_not_reported(self, sql: str) -> None:
        assert baseline_literal_findings(sql) == []

    def test_a_neighbouring_literal_column_is_not_blamed(self) -> None:
        sql = "SELECT 'claude-opus-4.1' AS other, x AS baseline_model FROM t"
        assert baseline_literal_findings(sql) == []


class TestPlantedLastDefinition:
    def test_a_later_migration_that_brings_the_literal_back_is_reported(
        self, tmp_path: Path
    ) -> None:
        root = _clean_savings_tree(tmp_path)
        _plant(
            root,
            {
                f"{_MIGRATIONS}/094_regress.sql": _view(
                    "projection_delegation_savings", "'claude-opus-4.1'"
                )
            },
        )
        problems = last_definition_findings(root)
        assert len(problems) == 1
        assert "094_regress.sql" in problems[0]
        assert "projection_delegation_savings" in problems[0]

    def test_an_old_literal_that_a_later_migration_replaced_is_history_not_a_finding(
        self, tmp_path: Path
    ) -> None:
        root = _clean_savings_tree(tmp_path)
        _plant(
            root,
            {
                f"{_MIGRATIONS}/000_old.sql": _view(
                    "projection_delegation_savings", "'claude-opus-4.1'"
                )
            },
        )
        assert last_definition_findings(root) == []

    def test_a_view_with_no_definition_at_all_is_reported(self, tmp_path: Path) -> None:
        root = _plant(
            tmp_path,
            {
                f"{_MIGRATIONS}/001_x.sql": _view(
                    "projection_delegation_savings", "model_cloud_baseline"
                )
            },
        )
        problems = last_definition_findings(root)
        assert any("projection_cost_savings_overview" in p for p in problems)
        assert any("projection_delegation_savings_series" in p for p in problems)

    def test_a_definition_in_another_node_folder_must_be_clean(
        self, tmp_path: Path
    ) -> None:
        root = _clean_savings_tree(tmp_path)
        _plant(
            root,
            {
                "src/omnimarket/nodes/node_other/migrations/0001_x.sql": _view(
                    "projection_cost_savings_overview", "'claude-opus-4.1'"
                )
            },
        )
        assert len(last_definition_findings(root)) == 1

    def test_a_drop_and_create_pair_is_read_as_a_definition(
        self, tmp_path: Path
    ) -> None:
        root = _clean_savings_tree(tmp_path)
        body = (
            "DROP VIEW IF EXISTS public.projection_delegation_savings;\n"
            "CREATE VIEW public.projection_delegation_savings AS\n"
            "SELECT 'claude-opus-4.1' AS baseline_model FROM t;\n"
        )
        _plant(root, {f"{_MIGRATIONS}/095_recreate.sql": body})
        assert len(last_definition_findings(root)) == 1


class TestPlantedFreeze:
    def _frozen_plus(self, tmp_path: Path, extra: dict[str, str]) -> set[str]:
        files = dict.fromkeys(
            FROZEN_LITERAL_HOLDERS,
            f"-- history\nSELECT '{BASELINE_LITERAL}' AS baseline_model;\n",
        )
        files.update(extra)
        return baseline_literal_holders(_plant(tmp_path, files))

    def test_a_sixth_holder_is_reported_in_sql_python_and_yaml(
        self, tmp_path: Path
    ) -> None:
        holders = self._frozen_plus(
            tmp_path,
            {
                f"{_MIGRATIONS}/094_new.sql": f"SELECT '{BASELINE_LITERAL}' AS baseline_model;",
                "src/omnimarket/x.py": f'BASELINE = "{BASELINE_LITERAL}"\n',
                "src/omnimarket/nodes/n/contract.yaml": f"baseline: {BASELINE_LITERAL}\n",
            },
        )
        assert holders - FROZEN_LITERAL_HOLDERS == {
            f"{_MIGRATIONS}/094_new.sql",
            "src/omnimarket/x.py",
            "src/omnimarket/nodes/n/contract.yaml",
        }

    def test_a_listed_file_that_lost_the_literal_is_stale(self, tmp_path: Path) -> None:
        files = dict.fromkeys(
            FROZEN_LITERAL_HOLDERS, f"SELECT '{BASELINE_LITERAL}' AS baseline_model;"
        )
        victim = sorted(FROZEN_LITERAL_HOLDERS)[0]
        files[victim] = "SELECT model_cloud_baseline AS baseline_model;"
        problems = frozen_holder_problems(_plant(tmp_path, files))
        assert len(problems) == 1
        assert "no longer holds the literal" in problems[0]
        assert victim in problems[0]

    def test_an_exact_frozen_tree_has_no_problems(self, tmp_path: Path) -> None:
        files = dict.fromkeys(
            FROZEN_LITERAL_HOLDERS, f"SELECT '{BASELINE_LITERAL}' AS baseline_model;"
        )
        assert frozen_holder_problems(_plant(tmp_path, files)) == []

    def test_the_other_spelling_used_by_fixtures_is_not_the_literal(
        self, tmp_path: Path
    ) -> None:
        root = _plant(tmp_path, {"src/omnimarket/x.py": 'M = "claude-opus-4-1"\n'})
        assert baseline_literal_holders(root) == set()

    def test_tests_and_pycache_are_not_scanned(self, tmp_path: Path) -> None:
        root = _plant(
            tmp_path,
            {
                "tests/t.py": f'X = "{BASELINE_LITERAL}"\n',
                "src/omnimarket/__pycache__/x.py": f'X = "{BASELINE_LITERAL}"\n',
            },
        )
        assert baseline_literal_holders(root) == set()


class TestPlantedTokenPriceArithmetic:
    @pytest.mark.parametrize(
        "expr",
        [
            "tokens_input * price_per_token",
            "price_per_token * tokens_input",
            "COALESCE(tokens_input, 0) * input_price",
            "tokens_input::numeric * rate",
            "(tokens_input + tokens_output) * blended_price",
            "tokens_input / 1000.0 * price_per_1k",
            "price_per_1k * tokens_input / 1000",
            "d.completion_tokens * m.price_out",
            "tokens_output * COALESCE(p.output_rate, 0)",
            "tokens\n    *\n    price",
        ],
    )
    def test_tokens_times_price_is_reported(self, expr: str) -> None:
        assert token_price_findings(f"SELECT {expr} AS cost FROM t") != []

    @pytest.mark.parametrize(
        "expr",
        [
            "tokens_input + tokens_output",
            "tokens_to_compliance * 2",
            "cost_usd * 1000",
            "price * quantity",
            "tokens_input * success_ratio",
            "COUNT(*)",
            "tokens_input / NULLIF(tokens_total, 0)",
            "pricing_manifest_version",
        ],
    )
    def test_unrelated_arithmetic_is_not_reported(self, expr: str) -> None:
        assert token_price_findings(f"SELECT {expr} AS x FROM t") == []

    def test_a_commented_out_expression_is_not_reported(self) -> None:
        sql = (
            "-- tokens_input * price_per_token\nSELECT 1 FROM t;\n/* tokens * price */"
        )
        assert token_price_findings(sql) == []

    def test_a_string_that_mentions_the_expression_is_not_reported(self) -> None:
        assert token_price_findings("SELECT 'tokens * price' AS note FROM t") == []

    def test_a_planted_sql_file_is_reported(self, tmp_path: Path) -> None:
        root = _plant(
            tmp_path,
            {
                "src/omnimarket/nodes/n/migrations/0001.sql": "SELECT tokens_input * price_per_token FROM t;"
            },
        )
        findings, scanned = token_price_findings_in_tree(root)
        assert scanned == 1
        assert list(findings) == ["src/omnimarket/nodes/n/migrations/0001.sql"]

    def test_a_planted_python_sql_string_is_reported(self, tmp_path: Path) -> None:
        source = (
            'QUERY = """\n'
            "SELECT tokens_input * price_per_token AS cost\n"
            "FROM delegation_events\n"
            '"""\n'
        )
        root = _plant(tmp_path, {"src/omnimarket/nodes/n/handlers/h.py": source})
        findings, _ = token_price_findings_in_tree(root)
        assert list(findings) == ["src/omnimarket/nodes/n/handlers/h.py"]

    def test_a_planted_yaml_query_is_reported_and_a_yaml_comment_is_not(
        self, tmp_path: Path
    ) -> None:
        root = _plant(
            tmp_path,
            {
                "src/omnimarket/nodes/n/contract.yaml": (
                    "query: SELECT tokens_input * price_per_token FROM t\n"
                ),
                "src/omnimarket/nodes/m/contract.yaml": (
                    "# SELECT tokens_input * price_per_token FROM t\nname: m\n"
                ),
            },
        )
        findings, _ = token_price_findings_in_tree(root)
        assert list(findings) == ["src/omnimarket/nodes/n/contract.yaml"]

    def test_the_same_expression_inside_the_metering_compute_is_allowed(
        self, tmp_path: Path
    ) -> None:
        root = _plant(
            tmp_path,
            {
                f"{_METERING_COMPUTE_DIR}handlers/q.sql": "SELECT tokens_input * price_per_token FROM t;",
                f"{_METERING_COMPUTE_DIR}handlers/h.py": 'Q = "SELECT tokens * price FROM t"\n',
            },
        )
        findings, scanned = token_price_findings_in_tree(root)
        assert findings == {}
        assert scanned == 0

    def test_python_arithmetic_is_not_scanned_the_documented_gap(
        self, tmp_path: Path
    ) -> None:
        root = _plant(
            tmp_path, {"src/omnimarket/p.py": "cost = prompt_tokens * price_in\n"}
        )
        findings, scanned = token_price_findings_in_tree(root)
        assert findings == {}
        assert scanned == 1

    def test_an_unparseable_python_file_is_not_silently_skipped(
        self, tmp_path: Path
    ) -> None:
        root = _plant(tmp_path, {"src/omnimarket/bad.py": "def (:\n"})
        with pytest.raises(SyntaxError):
            token_price_findings_in_tree(root)


class TestPlantedCiWiring:
    _GOOD = (
        "jobs:\n  lint:\n    steps:\n"
        "      - name: Single savings definition guard (OMN-19980)\n"
        f"        run: uv run pytest {CI_GUARD_PATH} -q -p no:randomly\n"
    )

    def _ci(self, tmp_path: Path, text: str) -> Path:
        return _plant(tmp_path, {".github/workflows/ci.yml": text})

    def test_a_named_blocking_step_passes(self, tmp_path: Path) -> None:
        assert ci_guard_step_problems(self._ci(tmp_path, self._GOOD)) == []

    def test_no_step_is_reported(self, tmp_path: Path) -> None:
        text = (
            "jobs:\n  lint:\n    steps:\n      - name: Ruff\n        run: ruff check\n"
        )
        assert ci_guard_step_problems(self._ci(tmp_path, text)) != []

    def test_a_missing_ci_file_is_reported(self, tmp_path: Path) -> None:
        assert ci_guard_step_problems(tmp_path) != []

    def test_continue_on_error_is_reported(self, tmp_path: Path) -> None:
        text = self._GOOD + "        continue-on-error: true\n"
        assert ci_guard_step_problems(self._ci(tmp_path, text)) != []

    def test_a_step_not_named_for_the_guard_is_reported(self, tmp_path: Path) -> None:
        text = self._GOOD.replace("Single savings definition guard", "Run some tests")
        problems = ci_guard_step_problems(self._ci(tmp_path, text))
        assert len(problems) == 1
        assert "not named for the guard" in problems[0]

    def test_a_conditional_step_is_reported(self, tmp_path: Path) -> None:
        text = self._GOOD.replace(
            "        run:", "        if: github.event_name == 'push'\n        run:"
        )
        assert ci_guard_step_problems(self._ci(tmp_path, text)) != []

    def test_a_step_that_only_mentions_the_path_in_a_comment_is_not_a_step(
        self, tmp_path: Path
    ) -> None:
        text = (
            f"# runs {CI_GUARD_PATH}\n"
            "jobs:\n  lint:\n    steps:\n      - name: Ruff\n        run: ruff check\n"
        )
        assert ci_guard_step_problems(self._ci(tmp_path, text)) != []
