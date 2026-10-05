# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture corpus for the projection contract checks (OMN-20567, batch row 13).

One corpus feeds three rules (``access``, ``dlq``, ``cursor``). Each case is a
small synthetic repo tree plus the argv a hook would pass. The same corpus is
run through the original scripts (recorded once as golden JSON, then compared
against the live scripts while they exist) and through the canonical
``node_projection_contract_check_compute`` runtime, so the two must agree on
exit code, stdout and stderr for every case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NODES = "src/omnimarket/nodes"

# The escape-hatch comments the original scripts honoured. They are fixture data
# here, assembled from parts so this file itself carries no marker comment.
_ACCESS_MARKER = "# projection-access" + "-ok:"
_DLQ_MARKER = "# dlq-path-not" + "-required:"


@dataclass(frozen=True)
class CorpusCase:
    name: str
    rule: str
    files: dict[str, str]
    argv: tuple[str, ...] = field(default_factory=tuple)


def _contract(tables: list[tuple[str, str | None]] | None = None) -> str:
    if tables is None:
        return "name: node_x\nnode_type: compute\n"
    lines = ["name: node_x", "db_io:", "  db_tables:"]
    for name, access in tables:
        lines.append(f"    - name: {name}")
        if access is not None:
            lines.append(f"      access: {access}")
    return "\n".join(lines) + "\n"


def _handler(body: str) -> str:
    return f"TABLE = 'orders'\nOTHER = 'audit'\n\n\ndef run(db):\n{body}"


_ACCESS: tuple[CorpusCase, ...] = (
    CorpusCase(
        "access_clean_read_write",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "read_write")]),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(TABLE, {})\n    db.upsert(TABLE, 'k', {})\n"
            ),
        },
    ),
    CorpusCase(
        "access_write_only_but_reads",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(TABLE, {})\n    db.upsert(TABLE, 'k', {})\n"
            ),
        },
    ),
    CorpusCase(
        "access_read_only_but_writes",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "read")]),
            f"{NODES}/node_a/handler.py": _handler("    db.upsert(TABLE, 'k', {})\n"),
        },
    ),
    CorpusCase(
        "access_literal_table_arg",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler("    db.query('orders', {})\n"),
        },
    ),
    CorpusCase(
        "access_attribute_constant",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler("    db.query(self.TABLE, {})\n"),
        },
    ),
    CorpusCase(
        "access_unresolved_read_flags_write_only_table",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(dynamic_name(), {})\n    db.upsert(TABLE, 'k', {})\n"
            ),
        },
    ),
    CorpusCase(
        "access_unresolved_read_with_read_write_table",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "read_write")]),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(dynamic_name(), {})\n"
            ),
        },
    ),
    CorpusCase(
        "access_allow_marker_skips_line",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler(
                f"    db.query(TABLE, {{}})  {_ACCESS_MARKER} runtime-built name\n"
            ),
        },
    ),
    CorpusCase(
        "access_multiline_call",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(\n        TABLE,\n        {},\n    )\n"
            ),
        },
    ),
    CorpusCase(
        "access_tests_dir_and_test_files_excluded",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": _handler("    db.upsert(TABLE, 'k', {})\n"),
            f"{NODES}/node_a/tests/test_h.py": _handler("    db.query(TABLE, {})\n"),
            f"{NODES}/node_a/test_inline.py": _handler("    db.query(TABLE, {})\n"),
        },
    ),
    CorpusCase(
        "access_two_tables_one_narrow",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract(
                [("orders", "read_write"), ("audit", "write")]
            ),
            f"{NODES}/node_a/handler.py": _handler(
                "    db.query(TABLE, {})\n    db.query(OTHER, {})\n"
            ),
        },
    ),
    CorpusCase(
        "access_missing_access_declaration",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", None)]),
            f"{NODES}/node_a/handler.py": _handler("    db.query(TABLE, {})\n"),
        },
    ),
    CorpusCase(
        "access_contract_without_db_io_and_non_mapping_contract",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract(None),
            f"{NODES}/node_a/handler.py": _handler("    db.query(TABLE, {})\n"),
            f"{NODES}/node_b/contract.yaml": "- just\n- a list\n",
            f"{NODES}/node_b/handler.py": _handler("    db.query(TABLE, {})\n"),
        },
    ),
    CorpusCase(
        "access_two_nodes_independent_constants",
        "access",
        {
            f"{NODES}/node_a/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_a/handler.py": "TABLE = 'orders'\n\n\ndef run(db):\n    db.upsert(TABLE, 'k', {})\n",
            f"{NODES}/node_b/contract.yaml": _contract([("orders", "write")]),
            f"{NODES}/node_b/handler.py": "TABLE = 'other'\n\n\ndef run(db):\n    db.query(TABLE, {})\n",
        },
    ),
)


def _dlq_handler(*lines: str) -> str:
    return "\n".join(lines) + "\n"


_DLQ_HANDLER = f"{NODES}/node_projection_a/handlers/handler_a.py"

_DLQ: tuple[CorpusCase, ...] = (
    CorpusCase(
        "dlq_validation_error_without_route",
        "dlq",
        {
            _DLQ_HANDLER: _dlq_handler(
                "from pydantic import ValidationError",
                "def run():",
                "    try:",
                "        pass",
                "    except ValidationError:",
                "        return None",
            )
        },
    ),
    CorpusCase(
        "dlq_route_to_dlq_wired",
        "dlq",
        {
            _DLQ_HANDLER: _dlq_handler(
                "from pydantic import ValidationError",
                "from x import route_to_dlq",
                "except ValidationError:",
                "    route_to_dlq()",
            )
        },
    ),
    CorpusCase(
        "dlq_dlq_topics_wired",
        "dlq",
        {_DLQ_HANDLER: _dlq_handler("ValidationError", "x = contract.dlq_topics")},
    ),
    CorpusCase(
        "dlq_private_route_symbol_wired",
        "dlq",
        {
            _DLQ_HANDLER: _dlq_handler(
                "ValidationError", "self._route_malformed_to_dlq()"
            )
        },
    ),
    CorpusCase(
        "dlq_allow_marker_skips",
        "dlq",
        {
            _DLQ_HANDLER: _dlq_handler(
                "ValidationError",
                f"{_DLQ_MARKER} re-raises to the caller",
            )
        },
    ),
    CorpusCase(
        "dlq_no_validation_error_out_of_scope",
        "dlq",
        {_DLQ_HANDLER: _dlq_handler("def run():", "    return 1")},
    ),
    CorpusCase(
        "dlq_non_handler_file_and_non_projection_node_not_scanned",
        "dlq",
        {
            f"{NODES}/node_projection_a/handlers/helper.py": "ValidationError\n",
            f"{NODES}/node_other/handlers/handler_x.py": "ValidationError\n",
            _DLQ_HANDLER: "def run():\n    return 1\n",
        },
    ),
    CorpusCase(
        "dlq_two_violations_sorted",
        "dlq",
        {
            f"{NODES}/node_projection_b/handlers/handler_b.py": "ValidationError\n",
            _DLQ_HANDLER: "ValidationError\n",
        },
    ),
    CorpusCase(
        "dlq_explicit_paths_precommit_mode",
        "dlq",
        {
            f"{NODES}/node_projection_b/handlers/handler_b.py": "ValidationError\n",
            _DLQ_HANDLER: "ValidationError\n",
        },
        argv=(f"{NODES}/node_projection_b/handlers/handler_b.py",),
    ),
    CorpusCase(
        "dlq_explicit_paths_clean_file_only",
        "dlq",
        {
            f"{NODES}/node_projection_b/handlers/handler_b.py": "x = 1\n",
            _DLQ_HANDLER: "ValidationError\n",
        },
        argv=(f"{NODES}/node_projection_b/handlers/handler_b.py",),
    ),
)


def _cursor_contract(*exposures: str, nested: bool = False, expose: bool = True) -> str:
    body = "projection_api:\n"
    body += f"  expose: {'true' if expose else 'false'}\n"
    if nested:
        body += "  exposures:\n"
        for exposure in exposures:
            body += exposure
    else:
        body += exposures[0]
    return body


def _flat(table: str | None, cursor: str | None, columns: str | None) -> str:
    lines = []
    if table is not None:
        lines.append(f"  table: {table}\n")
    if cursor is not None:
        lines.append(f"  cursor_column: {cursor}\n")
    if columns is not None:
        lines.append(f"  columns: {columns}\n")
    return "".join(lines)


def _nested(table: str, cursor: str | None, columns: str | None) -> str:
    out = f"    - table: {table}\n"
    if cursor is not None:
        out += f"      cursor_column: {cursor}\n"
    if columns is not None:
        out += f"      columns: {columns}\n"
    return out


_BASELINE = "scripts/validation/projection_cursor_baseline.txt"

_CURSOR: tuple[CorpusCase, ...] = (
    CorpusCase(
        "cursor_all_declared_clean",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", "id", "[id, name]")
            ),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_new_missing_cursor_not_in_baseline",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", None, "[id]")),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_missing_cursor_baselined",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", None, "[id]")),
            _BASELINE: "# header\nnode_a::t#0\n",
        },
    ),
    CorpusCase(
        "cursor_baseline_entry_now_fixed_reports_shrink",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", "id", "[id]")),
            _BASELINE: "# header\nnode_a::t#0\n",
        },
    ),
    CorpusCase(
        "cursor_nested_exposures_indexed_ids",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _nested("t", "id", "[id]"),
                _nested("t", None, "[id]"),
                _nested("t", None, "[id]"),
                nested=True,
            ),
            _BASELINE: "# header\nnode_a::t#1\n",
        },
    ),
    CorpusCase(
        "cursor_hard_membership_violation",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", "ghost", "[id, name]")
            ),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_hard_violation_no_columns_list",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", "id", None)),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_star_columns_accepted",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", "anything", '["*"]')
            ),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_quoted_column_names_compared_unquoted",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", "'\"id\"'", "['\"id\"', name]")
            ),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_expose_false_and_non_mapping_ignored",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", None, "[id]"), expose=False
            ),
            f"{NODES}/node_b/contract.yaml": "- a\n- list\n",
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_unnamed_table",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat(None, None, "[id]")
            ),
            _BASELINE: "# header\nnode_a::unnamed#0\n",
        },
    ),
    CorpusCase(
        "cursor_new_and_hard_together",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", None, "[id]")),
            f"{NODES}/node_b/contract.yaml": _cursor_contract(
                _flat("u", "ghost", "[id]")
            ),
            _BASELINE: "# header\n",
        },
    ),
    CorpusCase(
        "cursor_write_baseline_shrinks",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", "id", "[id]")),
            _BASELINE: "# header\nnode_a::t#0\n",
        },
        argv=("--write-baseline",),
    ),
    CorpusCase(
        "cursor_write_baseline_refuses_growth",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", None, "[id]")),
            _BASELINE: "# header\nnode_z::z#0\n",
        },
        argv=("--write-baseline",),
    ),
    CorpusCase(
        "cursor_write_baseline_allow_growth",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(_flat("t", None, "[id]")),
            _BASELINE: "# header\nnode_z::z#0\n",
        },
        argv=("--write-baseline", "--allow-growth"),
    ),
    CorpusCase(
        "cursor_write_baseline_refused_on_hard_violation",
        "cursor",
        {
            f"{NODES}/node_a/contract.yaml": _cursor_contract(
                _flat("t", "ghost", "[id]")
            ),
            _BASELINE: "# header\n",
        },
        argv=("--write-baseline", "--allow-growth"),
    ),
)

CASES: tuple[CorpusCase, ...] = _ACCESS + _DLQ + _CURSOR
