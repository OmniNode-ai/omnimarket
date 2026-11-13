# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the validator estate from git and append one shadow row the floor draws under piece 4.

The operator asked how many validators are canonical generic validators, not only the ones in core.
A validator is canonical and generic when it subclasses the generic base, its rules sit in a rule
document (contract or overlay data) that resolves with at least one rule, and its module holds no
rule table or pattern written in code. Every other validator is counted against it: a validator
class that is not canonical (a base subclass with rules in code, a bespoke class outside the format),
and every hook of the repositories' pre-commit configuration that does not run a class already
counted. The share of canonical validators whose module (or base) emits a trace on the bus is read
from the code. The validators plan's tasks are counted merged when every ticket the plan names for the task is cited by a commit
on a declared branch, partial when some are, open when none is, and no-ticket when the plan names none.

The row is a reading, not a gate: it refuses nothing and holds nothing. A repository that cannot be
read gives ``outcome=source-unreadable`` and no numbers; an unreadable repository is never an empty one.
"""

from __future__ import annotations

import ast
import hashlib
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath

import yaml

from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_validator_estate_reading_effect.models.model_validator_estate import (
    ModelFormatClass,
    ModelHookCount,
    ModelPlanTask,
    ModelValidatorEstateCommand,
    ModelValidatorEstateResult,
)
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import (
    ModelEstateOverlay,
    ModelGenericFormat,
)
from omnimarket.nodes.node_validator_estate_reading_effect.protocols import (
    EstateSourceError,
    ProtocolEstateSource,
)

_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=validator-estate"


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _is_name(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _string_count(node: ast.AST) -> int:
    if isinstance(node, ast.Call) and node.args:
        # frozenset({...}) / tuple([...]) / set(...)
        return _string_count(node.args[0])
    if isinstance(node, ast.Dict):
        return sum(
            1
            for item in (*node.keys, *node.values)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return sum(
            1
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return 0


def literal_tables(tree: ast.Module, fmt: ModelGenericFormat) -> int:
    """Module-level rule tables and patterns written in code."""
    found = 0
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
        if value is None:
            continue
        if _string_count(value) >= fmt.literal_table_min_strings or (
            isinstance(value, ast.Call)
            and any(
                (isinstance(value.func, ast.Attribute) and value.func.attr == call)
                or (isinstance(value.func, ast.Name) and value.func.id == call)
                for call in fmt.literal_pattern_calls
            )
        ):
            found += 1
    return found


def _class_constant(cls: ast.ClassDef, name: str) -> str | None:
    for stmt in cls.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _has_rules(text: str, fmt: ModelGenericFormat) -> bool:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    for holder in (doc, doc.get(fmt.nested_key)):
        if isinstance(holder, dict):
            rules = holder.get(fmt.rules_key)
            if isinstance(rules, (list, dict)) and len(rules) > 0:
                return True
    return False


def _module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _expand(listing: str, ticket: re.Pattern[str]) -> set[str]:
    """Tickets of a list such as ``OMN-5 and OMN-7`` or the range ``OMN-5 to OMN-8``."""
    found = ticket.findall(listing)
    if " to " in listing and len(found) == 2:
        prefix, low = found[0].rsplit("-", 1)
        high = found[1].rsplit("-", 1)[1]
        return {f"{prefix}-{n}" for n in range(int(low), int(high) + 1)}
    return set(found)


class HandlerValidatorEstate:
    """Read the validator estate of the overlay's repositories; append one row."""

    def __init__(
        self,
        *,
        overlay: ModelEstateOverlay,
        host: str,
        source: ProtocolEstateSource,
        plan_source: ProtocolEstateSource | None = None,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._source = source
        self._plan_source = plan_source if plan_source is not None else source
        self._appender = appender
        self._clock = clock

    def handle(
        self, command: ModelValidatorEstateCommand
    ) -> ModelValidatorEstateResult:
        if command.write and self._appender is None:
            return ModelValidatorEstateResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                outcome="source-unreadable",
                failure="no-appender-for-a-writing-run",
            )
        now = self._clock().astimezone(UTC)
        try:
            if command.fault == "core-unreadable":
                raise EstateSourceError(f"{self._overlay.core_repo}: injected fault")
            result = self._read(command)
        except EstateSourceError as exc:
            result = ModelValidatorEstateResult(
                correlation_id=command.correlation_id,
                status="completed",
                host=self._host,
                outcome="source-unreadable",
                unreadable=(str(exc),),
            )
        return self._publish(result, command, now)

    # --- reading -----------------------------------------------------------------------------------------------

    def _read(self, command: ModelValidatorEstateCommand) -> ModelValidatorEstateResult:
        heads: dict[str, str] = {}
        classes: list[ModelFormatClass] = []
        bespoke: list[tuple[str, str]] = []
        base_emits = self._base_emits()
        for repo in self._overlay.repos:
            heads[repo.name] = self._source.head(repo.name, repo.ref)[:12]
            found, plain = self._classes(
                repo.name, repo.ref, repo.source_roots, base_emits
            )
            classes.extend(found)
            bespoke.extend((repo.name, module) for module in plain)
        hooks, hook_texts = self._hooks(classes)
        # A bespoke class run by a hook is that hook, counted once in the hooks.
        unreferenced = [
            module
            for _repo, module in bespoke
            if not any(module.rsplit(".", 1)[-1] in text for text in hook_texts)
        ]
        return ModelValidatorEstateResult(
            correlation_id=command.correlation_id,
            status="completed",
            host=self._host,
            outcome="read",
            classes=tuple(classes),
            base_emits=base_emits,
            bespoke_classes=len(unreferenced),
            hooks=hooks,
            tasks=self._tasks(),
            heads=heads,
        )

    def _base_emits(self) -> bool:
        fmt = self._overlay.generic_format
        core = next(r for r in self._overlay.repos if r.name == self._overlay.core_repo)
        text = self._source.read(core.name, core.ref, fmt.base_module_path)
        if text is None:
            raise EstateSourceError(f"{core.name}: {fmt.base_module_path} not readable")
        return any(name in text for name in fmt.bus_trace_names)

    def _classes(
        self, repo: str, ref: str, roots: tuple[str, ...], base_emits: bool
    ) -> tuple[list[ModelFormatClass], list[str]]:
        fmt = self._overlay.generic_format
        suffixes = "|".join(re.escape(s) for s in fmt.bespoke_suffixes)
        pattern = rf"{re.escape(fmt.base_class)}|class +\w+({suffixes})\b"
        found: list[ModelFormatClass] = []
        bespoke: list[str] = []
        for path in self._source.files_containing(repo, ref, pattern, roots):
            text = self._source.read(repo, ref, path)
            if text is None:
                raise EstateSourceError(f"{repo}: {path} not readable")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            tables = literal_tables(tree, fmt)
            emits = base_emits or any(n in text for n in fmt.bus_trace_names)
            module = _module(path)
            for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                if any(_is_name(base, fmt.base_class) for base in cls.bases):
                    vid = _class_constant(cls, "validator_id")
                    found.append(
                        ModelFormatClass(
                            repo=repo,
                            module=f"{module}.{cls.name}",
                            name=cls.name,
                            validator_id=vid,
                            rules_as_data=self._rules_as_data(
                                repo, ref, path, vid, cls
                            ),
                            literal_tables=tables,
                            emits_trace=emits,
                        )
                    )
                elif cls.name.endswith(
                    fmt.bespoke_suffixes
                ) and not cls.name.startswith("Test"):
                    bespoke.append(f"{module}.{cls.name}")
        return found, bespoke

    def _rules_as_data(
        self, repo: str, ref: str, path: str, vid: str | None, cls: ast.ClassDef
    ) -> bool:
        fmt = self._overlay.generic_format
        if vid is None:
            return False
        directory = str(PurePosixPath(path).parent)
        document = f"{directory}/{fmt.contract_subdir}/{vid}{fmt.contract_suffix}"
        text = self._source.read(repo, ref, document)
        if text is None:
            return False
        return _has_rules(text, fmt)

    def _hooks(
        self, classes: list[ModelFormatClass]
    ) -> tuple[tuple[ModelHookCount, ...], list[str]]:
        rule = self._overlay.hooks
        counted = [
            token
            for c in classes
            for token in (c.validator_id, c.module.rsplit(".", 1)[0])
            if token
        ]
        counts: list[ModelHookCount] = []
        texts: list[str] = []
        for repo in self._overlay.repos:
            text = self._source.read(repo.name, repo.ref, repo.hook_config)
            if text is None:
                raise EstateSourceError(f"{repo.name}: {repo.hook_config} not readable")
            doc = yaml.safe_load(text) or {}
            hooks = covered = pytest = 0
            for block in doc.get("repos", []) if isinstance(doc, dict) else []:
                where = str(block.get("repo", ""))
                if where != rule.local_repo_value and not any(
                    f in where for f in rule.own_repo_fragments
                ):
                    continue
                for hook in block.get("hooks", []):
                    body = " ".join(
                        [str(hook.get("id", "")), str(hook.get("entry", ""))]
                        + [str(a) for a in hook.get("args", []) or []]
                    )
                    hooks += 1
                    texts.append(body)
                    covered += any(token in body for token in counted)
                    pytest += rule.pytest_marker in body
            counts.append(
                ModelHookCount(
                    repo=repo.name, hooks=hooks, covered=covered, pytest=pytest
                )
            )
        return tuple(counts), texts

    def _tasks(self) -> tuple[ModelPlanTask, ...]:
        plan = self._overlay.plan
        text = self._plan_source.read(plan.repo, plan.ref, plan.path)
        if text is None:
            raise EstateSourceError(f"{plan.repo}: {plan.path} not readable")
        start = re.compile(plan.task_pattern)
        ticket = re.compile(plan.ticket_pattern)
        tasks: dict[str, set[str]] = {}
        for line in text.splitlines():
            mark = start.match(line)
            if mark is not None:
                bold = " ".join(re.findall(r"\*\*(.+?)\*\*", line))
                tasks.setdefault(mark.group(1), set()).update(ticket.findall(bold))
        for entry in re.finditer(plan.minted_pattern, text, re.MULTILINE):
            if entry.group(1) in tasks:
                tasks[entry.group(1)].update(_expand(entry.group(2), ticket))
        merged_cache: dict[str, bool] = {}
        result: list[ModelPlanTask] = []
        for task, found in tasks.items():
            tickets = sorted(found - set(plan.exclude_tickets))
            merged: list[str] = []
            for t in tickets:
                if t not in merged_cache:
                    merged_cache[t] = any(
                        self._source.commits_citing(repo, ref, t) > 0
                        for repo, ref in self._merge_refs()
                    )
                if merged_cache[t]:
                    merged.append(t)
            result.append(
                ModelPlanTask(
                    task=task, tickets=tuple(tickets), merged_tickets=tuple(merged)
                )
            )
        return tuple(result)

    def _merge_refs(self) -> list[tuple[str, str]]:
        refs = {r.name: r.ref for r in self._overlay.repos}
        return [
            (name, refs[name])
            for name in self._overlay.plan.merge_repos
            if name in refs
        ]

    # --- publishing --------------------------------------------------------------------------------------------

    def _publish(
        self,
        result: ModelValidatorEstateResult,
        command: ModelValidatorEstateCommand,
        now: datetime,
    ) -> ModelValidatorEstateResult:
        key = hashlib.sha256(
            repr(
                (
                    result.outcome,
                    result.canonical,
                    result.estate,
                    result.canonical_emitting,
                    result.tasks_merged,
                    len(result.tasks),
                    sorted(result.heads.items()),
                )
            ).encode()
        ).hexdigest()[:32]
        if not command.write:
            return result.model_copy(update={"ledger": "not-written", "row": ""})
        assert self._appender is not None
        publish = self._overlay.publish
        read = result.outcome == "read"
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"drill={'no' if command.fault == 'none' else 'yes'}",
            f"fault={command.fault}",
            "gate=none",
            f"outcome={result.outcome}",
        ]
        if read:
            by_class = result.canonical
            cells += [
                f"canonical_of={by_class}/{result.estate}",
                f"other={result.other}",
                f"other_hooks={result.other_hooks}",
                f"other_classes={result.other_classes}",
                f"classes_total={len(result.classes)}",
                f"hook_pytest={sum(h.pytest for h in result.hooks)}",
                f"traces_of={result.canonical_emitting}/{result.canonical}",
                f"tasks_merged_of={result.tasks_merged}/{len(result.tasks)}",
                f"tasks_partial={sum(1 for t in result.tasks if t.state == 'partial')}",
                f"tasks_open={sum(1 for t in result.tasks if t.state == 'open')}",
                f"tasks_no_ticket={sum(1 for t in result.tasks if t.state == 'no-ticket')}",
                "heads="
                + ",".join(f"{r}@{h}" for r, h in sorted(result.heads.items())),
            ]
        else:
            cells.append(
                "unreadable=" + reason_token(";".join(result.unreadable) or "unknown")
            )
        cells += [
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(now)}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"overlay_hash={self._overlay.overlay_hash()}",
            f"correlation={reason_token(command.correlation_id)}",
            _clean(
                "reading: counts validators from git; it refuses nothing and holds no merge"
            ),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        appended = self._appender.append(
            row + "\n", str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{stamp_text(now)}"))
        )
        if not appended.ok:
            why = f"append-failed:{appended.status}"
            return result.model_copy(
                update={
                    "status": "failed",
                    "ledger": why,
                    "row": row,
                    "failure": f"{why}:{reason_token(appended.message or 'no-message')}",
                }
            )
        return result.model_copy(
            update={
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )
