# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_file_census_effect``: the census against its ratchet.

Exit 0: every counted repository is at or under its ratchet row. Exit 1: a count grew past its row
(red). Exit 69: a repository is absent or unreadable, a repository has no ratchet row, or the ratchet
file is unreadable: nothing is passed on a read that failed. ``--lower`` rewrites the ratchet file with
every lowerable count lowered after a passing run; it never raises a row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    GitRepositorySource,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusRequest,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.overlay import load_overlay

_HEADER = (
    "# Shrink-only ratchet: executable readers and writers of the ledger file's name or path variable,\n"
    "# counted in files per repository. A count above its row fails; `--lower` only ever lowers a row.\n"
)


def main(
    argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="node_work_ledger_file_census_effect")
    parser.add_argument(
        "--repos", nargs="*", help="registered names; default every one"
    )
    parser.add_argument("--lower", action="store_true")
    parser.add_argument(
        "--directory",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="where a registered repository sits when it is not <root>/<name>",
    )
    parser.add_argument("--verbose", action="store_true", help="print every hit")
    parser.add_argument(
        "--json-facts",
        action="store_true",
        help="print only the facts reading 2 of the no-file readings takes, as JSON",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    root_text = env.get(overlay.registry_root_env, "").strip()
    if not root_text:
        sys.stderr.write(f"CENSUS unreadable: {overlay.registry_root_env} is not set\n")
        return 69
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolEmissionSink = (
        JsonlEmissionSink(Path(journal)) if journal else InMemoryEmissionSink()
    )
    store = YamlRatchetStore(Path(overlay.ratchet_file))
    result = HandlerFileCensus(
        overlay,
        GitRepositorySource(
            Path(root_text),
            overlay.max_file_bytes,
            dict(item.split("=", 1) for item in args.directory),
        ),
        store,
        sink,
        backend="real" if journal else "in_memory",
    ).handle(
        ModelCensusRequest(
            correlation_id=uuid.uuid4().hex,
            repositories=tuple(args.repos) if args.repos else None,
        )
    )
    if args.json_facts:
        unit_suffixes = (".plist", ".service", ".timer")
        sys.stdout.write(
            json.dumps(
                {
                    "total_files": result.total_files,
                    "own_line_files": sum(c.files for c in result.counts if c.own_line),
                    "units_naming_path": sorted(
                        {
                            f"{c.repository}/{h.path}"
                            for c in result.counts
                            for h in c.hits
                            if h.path.endswith(unit_suffixes)
                        }
                    ),
                }
            )
            + "\n"
        )
        return (
            0 if result.passed else (1 if result.failure_code == "ratchet-grew" else 69)
        )
    for count in result.counts:
        line = "own-line" if count.own_line else "counted"
        sys.stdout.write(
            f"CENSUS {count.repository} {line} state={count.state} files={count.files} "
            f"readers={count.readers} writers={count.writers} ratchet={count.ratchet}\n"
        )
        if args.verbose:
            for hit in count.hits:
                sys.stdout.write(f"  {hit.kind.value} {hit.path}:{hit.line}\n")
    sys.stdout.write(
        f"CENSUS total={result.total_files} passed={'yes' if result.passed else 'no'} "
        f"failure={result.failure_code or 'none'} {result.failure_detail}\n"
    )
    if result.passed and args.lower:
        store.write(result.lowered_ratchet, _HEADER)
    if result.passed:
        return 0
    return 1 if result.failure_code == "ratchet-grew" else 69
