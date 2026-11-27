# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for ``dreamsheets-desktop mcp-headless``: answers the three
messages the node sends, misbehaving on request.

Usage: ``python fake_dreamsheets_server.py <scenario>`` with stdin = the
node's newline-delimited JSON-RPC. Scenarios: ok, ok_values, open_error,
summary_error, bad_record, extra_key, sleep, flood, garbage, crash.
"""

from __future__ import annotations

import json
import sys
import time

RECORD: dict[str, object] = {
    "summary_version": 1,
    "values_withheld": True,
    "tabs": 1,
    "tables": 2,
    "columns": 5,
    "rows": None,
    "cells": None,
    "size_bucket": None,
    "largest_table": None,
    "capabilities": {
        "sql_tables": 1,
        "external_query_tables": 0,
        "dataset_tables": 0,
        "calc_columns": 1,
        "tables_with_cell_formulas": 0,
        "tiles": 1,
        "charts": 0,
        "scripts": 0,
        "custom_functions": 0,
        "comment_threads": 0,
        "connections": 0,
        "ai_notes": False,
    },
}
VALUES: dict[str, object] = {
    "values_withheld": False,
    "rows": 7,
    "cells": 12,
    "size_bucket": "xs",
    "largest_table": {"columns": 2, "rows": 2, "cells": 4},
}
#: Planted in every error so tests can prove the node never copies it out.
MARKER = "ZQXMARK7F3A"


def text(id_: int, body: str, error: bool = False) -> str:
    result: dict[str, object] = {"content": [{"type": "text", "text": body}]}
    if error:
        result["isError"] = True
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result})


def main() -> None:
    scenario = sys.argv[1]
    if scenario == "crash":
        sys.exit(3)
    if scenario == "sleep":
        time.sleep(60)
    if scenario == "flood":
        sys.stdout.write("x" * 200_000)
        sys.stdout.flush()
        return
    if scenario == "garbage":
        print("not json")
        return
    for line in sys.stdin:
        msg = json.loads(line)
        id_ = msg["id"]
        if msg["method"] == "initialize":
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": id_,
                        "result": {
                            "serverInfo": {
                                "name": "dreamsheets-files",
                                "version": "0.2.9",
                            }
                        },
                    }
                )
            )
        elif msg["params"]["name"] == "open_document":
            if scenario == "open_error":
                print(text(id_, f"E_OPEN_IO: {MARKER} could not be opened", error=True))
            else:
                print(text(id_, "Opened the document."))
        elif msg["params"]["name"] == "get_structure_summary":
            record = dict(RECORD)
            if scenario == "ok_values":
                record.update(VALUES)
            if scenario == "extra_key":
                record["names"] = [MARKER]
            if scenario == "summary_error":
                print(text(id_, f"weird failure {MARKER}", error=True))
            elif scenario == "bad_record":
                print(text(id_, json.dumps({"summary_version": 2})))
            else:
                print(text(id_, json.dumps(record)))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
