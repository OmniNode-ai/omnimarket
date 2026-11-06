# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Three-way merge of a ticket contract by entry (OMN-19998).

Nearly every pull request appends to one shared contract, so each landing conflicts the rest, and
a line union of two appends can move a line across an entry boundary. On 2026-11-05 union merges
dropped ``cwd: .`` from the last check of six branches' own entries, because both sides ended in
the same line and that common suffix fell outside the conflict: omnimarket#5078 and #5103 landed
that way, #5071, #5112, #5123 and #5127 still carried it. The result parsed and validated, so
``check-yaml`` could not see it.

This merge works on parsed entries and copies text only for whole entries. A top-level list merges
entry by entry, keyed by ``id`` when every entry has one and by value otherwise; anything else
merges as one field. The result keeps ``current``'s bytes, gaining only ``incoming``'s changes.
It is refused, never guessed, when an id repeats within a side, when both sides changed one thing
differently, or when the rendered text does not parse back to exactly the structure computed from
the three sides, which is the check a line union cannot make.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

import yaml
from omnibase_core.models.errors.model_onex_error import ModelOnexError
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.nodes.node_ticket_contract_merge_compute.models import (
    EnumTicketContractMergeOutcome,
    EnumTicketContractMergeRefusal,
    ModelTicketContractMergeRefusal,
    ModelTicketContractMergeRequest,
    ModelTicketContractMergeResult,
)

_Refusal = EnumTicketContractMergeRefusal
_ABSENT: object = object()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _entry_key(entry: object, keyed: bool) -> str:
    if keyed and isinstance(entry, dict):
        return str(entry["id"])
    return _canonical(entry)


def _is_keyed(*lists: Sequence[object]) -> bool:
    """Every entry of every side has a string id, and at least one entry exists."""
    entries = [entry for side in lists for entry in side]
    return bool(entries) and all(
        isinstance(entry, dict) and isinstance(entry.get("id"), str)
        for entry in entries
    )


@dataclass(frozen=True)
class _Section:
    """One top-level key's place in a text."""

    start: int
    end: int
    node: yaml.Node

    @property
    def is_block_list(self) -> bool:
        return (
            isinstance(self.node, yaml.SequenceNode)
            and not self.node.flow_style
            and bool(self.node.value)
        )


def _sections(text: str) -> dict[str, _Section]:
    """Each top-level key's line range, from the parser's own marks."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return {}
    pairs = [(str(key.value), key, value) for key, value in root.value]
    line_count = len(text.splitlines())
    sections: dict[str, _Section] = {}
    for index, (name, key, value) in enumerate(pairs):
        end = (
            pairs[index + 1][1].start_mark.line
            if index + 1 < len(pairs)
            else line_count
        )
        sections[name] = _Section(key.start_mark.line, end, value)
    return sections


def _trim_trailing(lines: list[str], start: int, end: int) -> int:
    """The end of a range without its trailing blank and comment lines."""
    while end > start and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return end


def _item_blocks(lines: list[str], section: _Section) -> list[list[str]]:
    """The verbatim lines of each item of a block list."""
    items = section.node.value
    starts = [item.start_mark.line for item in items]
    last = _trim_trailing(lines, starts[-1], section.end)
    ends = [*starts[1:], last]
    return [lines[a:b] for a, b in zip(starts, ends, strict=True)]


def _indent_of(section: _Section) -> int:
    return int(section.node.start_mark.column)


def _reindent(block: list[str], delta: int) -> list[str]:
    if delta == 0:
        return block
    if delta > 0:
        return [" " * delta + line if line.strip() else line for line in block]
    return [line[-delta:] if line.strip() else line for line in block]


def _dump_block(value: object, indent: int) -> list[str]:
    rendered = yaml.safe_dump(
        value, sort_keys=False, default_flow_style=False, width=4096, allow_unicode=True
    )
    pad = " " * indent
    return [pad + line if line.strip() else line for line in rendered.splitlines()]


class _Sides:
    """The three parsed sides, their sections and their lines."""

    def __init__(self, request: ModelTicketContractMergeRequest) -> None:
        self.source = request.source
        self.current_lines = request.current_text.splitlines()
        self.incoming_lines = request.incoming_text.splitlines()
        self.base = (
            self._load(request.base_text, "base") if request.base_text.strip() else {}
        )
        self.current = self._load(request.current_text, "current")
        self.incoming = self._load(request.incoming_text, "incoming")
        self.current_sections = _sections(request.current_text)
        self.incoming_sections = _sections(request.incoming_text)

    def _load(self, text: str, side: str) -> dict[str, object]:
        loaded = load_yaml_mapping_no_duplicates(text, source=f"{self.source} ({side})")
        return {str(key): value for key, value in loaded.items()}


def _refuse(
    code: EnumTicketContractMergeRefusal,
    section: str = "",
    entry: str = "",
    detail: str = "",
) -> ModelTicketContractMergeRefusal:
    return ModelTicketContractMergeRefusal(
        code=code, section=section, entry=entry, detail=detail
    )


def _three_way(base: object, current: object, incoming: object) -> tuple[object, bool]:
    """The merged value and whether it is decidable. ``_ABSENT`` means removed."""
    if current == incoming or incoming == base:
        return current, True
    if current == base:
        return incoming, True
    return _ABSENT, False


class _Merge:
    """One merge: the expected structure and the edits that render it onto ``current``."""

    def __init__(self, sides: _Sides) -> None:
        self.sides = sides
        self.refusals: list[ModelTicketContractMergeRefusal] = []
        self.expected: dict[str, object] = {}
        self.from_incoming: list[str] = []
        self.section_lines: dict[str, list[str] | None] = {}

    def run(self) -> None:
        sides = self.sides
        names = list(sides.current)
        names += [name for name in sides.incoming if name not in sides.current]
        names += [
            name
            for name in sides.base
            if name not in sides.current and name not in sides.incoming
        ]
        for name in names:
            base = sides.base.get(name, _ABSENT)
            current = sides.current.get(name, _ABSENT)
            incoming = sides.incoming.get(name, _ABSENT)
            if isinstance(current, list) and isinstance(incoming, list):
                self._merge_list(
                    name, base if isinstance(base, list) else [], current, incoming
                )
            else:
                self._merge_field(name, base, current, incoming)

    def _merge_field(
        self, name: str, base: object, current: object, incoming: object
    ) -> None:
        merged, decidable = _three_way(base, current, incoming)
        if not decidable:
            self.refusals.append(
                _refuse(
                    _Refusal.DIVERGENT_FIELD,
                    name,
                    detail="both sides changed this field differently",
                )
            )
            return
        if merged is not _ABSENT:
            self.expected[name] = merged
        if merged == current:
            return
        self.from_incoming.append(name)
        section = self.sides.incoming_sections.get(name)
        self.section_lines[name] = (
            None
            if section is None
            else self.sides.incoming_lines[section.start : section.end]
        )

    def _duplicates(self, name: str, keyed: bool, entries: Sequence[object]) -> bool:
        if not keyed:
            return False
        seen: set[str] = set()
        found = False
        for entry in entries:
            key = _entry_key(entry, True)
            if key in seen:
                self.refusals.append(
                    _refuse(
                        _Refusal.DUPLICATE_ID,
                        name,
                        key,
                        "one id on two entries within one side",
                    )
                )
                found = True
            seen.add(key)
        return found

    def _merge_list(
        self,
        name: str,
        base: Sequence[object],
        current: Sequence[object],
        incoming: Sequence[object],
    ) -> None:
        keyed = _is_keyed(base, current, incoming)
        dup = [
            self._duplicates(name, keyed, side) for side in (base, current, incoming)
        ]
        if any(dup):
            return
        by_base = {_entry_key(e, keyed): e for e in base}
        by_current = {_entry_key(e, keyed) for e in current}
        by_incoming = {_entry_key(e, keyed): e for e in incoming}
        merged: list[object] = []
        replaced: dict[str, object] = {}
        for entry in current:
            key = _entry_key(entry, keyed)
            value, decidable = _three_way(
                by_base.get(key, _ABSENT), entry, by_incoming.get(key, _ABSENT)
            )
            if not decidable:
                self.refusals.append(
                    _refuse(
                        _Refusal.DIVERGENT_ENTRY
                        if key in by_incoming
                        else _Refusal.REMOVED_AND_CHANGED,
                        name,
                        key,
                        "both sides changed this entry differently",
                    )
                )
                continue
            if value is _ABSENT:
                replaced[key] = _ABSENT
                continue
            merged.append(value)
            if value != entry:
                replaced[key] = value
        added: list[str] = []
        for key, entry in by_incoming.items():
            if key in by_current:
                continue
            if key in by_base:
                if by_base[key] != entry:
                    self.refusals.append(
                        _refuse(
                            _Refusal.REMOVED_AND_CHANGED,
                            name,
                            key,
                            "one side removed this entry and the other changed it",
                        )
                    )
                continue
            added.append(key)
            merged.append(entry)
        self.expected[name] = merged
        if not added and not replaced:
            return
        self.from_incoming += [f"{name}:{key}" for key in (*added, *replaced)]
        self.section_lines[name] = self._render_list(
            name, keyed, current, incoming, added, replaced, merged
        )

    def _render_list(
        self,
        name: str,
        keyed: bool,
        current: Sequence[object],
        incoming: Sequence[object],
        added: Sequence[str],
        replaced: dict[str, object],
        merged: Sequence[object],
    ) -> list[str]:
        sides = self.sides
        target = sides.current_sections[name]
        source = sides.incoming_sections[name]
        lines = sides.current_lines
        if not target.is_block_list:
            if merged and not any(isinstance(entry, dict) for entry in merged):
                flow = yaml.safe_dump(
                    list(merged), default_flow_style=True, width=4096
                ).strip()
                return [f"{name}: {flow}"]
            return [f"{name}:", *_dump_block(list(merged), 2)]
        indent = _indent_of(target)
        current_blocks = [
            (_entry_key(entry, keyed), block)
            for entry, block in zip(current, _item_blocks(lines, target), strict=True)
        ]
        incoming_blocks: dict[str, list[str]] = {}
        if source.is_block_list:
            delta = indent - _indent_of(source)
            incoming_blocks = {
                _entry_key(e, keyed): _reindent(block, delta)
                for e, block in zip(
                    incoming,
                    _item_blocks(sides.incoming_lines, source),
                    strict=True,
                )
            }
        by_incoming = {_entry_key(e, keyed): e for e in incoming}

        def block_of(key: str) -> list[str]:
            return incoming_blocks.get(key) or _dump_block([by_incoming[key]], indent)

        body: list[str] = []
        for key, block in current_blocks:
            if key not in replaced:
                body += block
            elif replaced[key] is not _ABSENT:
                body += block_of(key)
        for key in added:
            body += block_of(key)
        last = _trim_trailing(lines, target.node.value[-1].start_mark.line, target.end)
        head = lines[target.start : target.node.value[0].start_mark.line]
        tail = lines[last : target.end]
        return [*head, *body, *tail]


def _render(sides: _Sides, merge: _Merge) -> str:
    """``current``'s lines with each changed section's lines put in its place."""
    lines = sides.current_lines
    out: list[str] = []
    cursor = 0
    ordered = sorted(sides.current_sections.items(), key=lambda kv: kv[1].start)
    for name, section in ordered:
        if name not in merge.section_lines:
            continue
        out += lines[cursor : section.start]
        out += merge.section_lines[name] or []
        cursor = section.end
    out += lines[cursor:]
    for name, new in merge.section_lines.items():
        if name not in sides.current_sections and new:
            out += new
    return "\n".join(out) + "\n"


class HandlerTicketContractMerge:
    """COMPUTE: merge three contract texts by entry, or refuse with every reason."""

    def handle(
        self, request: ModelTicketContractMergeRequest
    ) -> ModelTicketContractMergeResult:
        try:
            sides = _Sides(request)
        except (yaml.YAMLError, ValueError, ModelOnexError) as error:
            return _refused(
                [
                    _refuse(
                        _Refusal.UNPARSEABLE,
                        detail=" ".join(str(error).split())[:400],
                    )
                ]
            )
        merge = _Merge(sides)
        merge.run()
        if merge.refusals:
            return _refused(merge.refusals)
        text = _render(sides, merge)
        try:
            parsed = load_yaml_mapping_no_duplicates(text, source=request.source)
        except (yaml.YAMLError, ValueError, ModelOnexError) as error:
            return _refused(
                [
                    _refuse(
                        _Refusal.RENDER_MISMATCH,
                        detail=" ".join(str(error).split())[:400],
                    )
                ]
            )
        if _canonical(parsed) != _canonical(merge.expected):
            return _refused(
                [
                    _refuse(
                        _Refusal.RENDER_MISMATCH,
                        detail="the rendered text does not parse back to the merged structure",
                    )
                ]
            )
        try:
            ModelTicketContract.from_yaml(text)
        except ModelOnexError as error:
            return _refused(
                [
                    _refuse(
                        _Refusal.INVALID_CONTRACT,
                        detail=" ".join(str(error).split())[:400],
                    )
                ]
            )
        return ModelTicketContractMergeResult(
            outcome=EnumTicketContractMergeOutcome.MERGED,
            merged_text=text,
            entries_from_incoming=tuple(merge.from_incoming),
        )


def _refused(
    refusals: Sequence[ModelTicketContractMergeRefusal],
) -> ModelTicketContractMergeResult:
    return ModelTicketContractMergeResult(
        outcome=EnumTicketContractMergeOutcome.REFUSED, refusals=tuple(refusals)
    )
