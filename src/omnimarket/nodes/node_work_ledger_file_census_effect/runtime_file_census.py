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
    YamlFileBaselineStore,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusRepository,
    ModelCensusRequest,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.overlay import (
    OVERLAY_FILE_ENV,
    load_overlay,
)

_RED = ("ratchet-grew", "new-file-reader")
_HEADER = (
    "# Shrink-only ratchet: executable readers and writers of the ledger file's name or path variable,\n"
    "# counted in files per repository. A count above its row fails; `--lower` only ever lowers a row.\n"
)
_BASELINE_HEADER = (
    "# Shrink-only per-file baseline: the files that hold an executable reader or writer of the ledger\n"
    "# file's name or path variable, with their line counts. A file not listed here, or a listed file\n"
    "# holding more lines than its row, fails the census (new code opening the ledger file). `--lower`\n"
    "# only ever lowers a row or drops a cleared file; it never adds a file.\n"
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
    parser.add_argument(
        "--ratchet-file",
        default=None,
        metavar="PATH",
        help="where this repository's ratchet file is, in place of the overlay's ratchet_file",
    )
    parser.add_argument(
        "--baseline-file",
        default=None,
        metavar="PATH",
        help="where this repository's per-file baseline is, in place of the overlay's baseline_file",
    )
    parser.add_argument(
        "--register",
        default=None,
        metavar="NAME",
        help="the repository this run counts, registered for this run in place of the overlay's "
        "repositories list; its checkout is named with --directory",
    )
    parser.add_argument(
        "--packaged-overlay",
        action="store_true",
        help="use the overlay shipped with this node in place of one found "
        "under the overlay roots",
    )
    parser.add_argument("--verbose", action="store_true", help="print every hit")
    parser.add_argument(
        "--json-facts",
        action="store_true",
        help="print only the facts reading 2 of the no-file readings takes, as JSON",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    packaged = Path(__file__).resolve().parent / "overlay.example.yaml"
    overlay = load_overlay(
        {**env, OVERLAY_FILE_ENV: str(packaged)} if args.packaged_overlay else env
    )
    if args.register:
        overlay = overlay.model_copy(
            update={
                "repositories": (
                    ModelCensusRepository(name=args.register, own_line=False),
                )
            }
        )
    root_text = env.get(overlay.registry_root_env, "").strip()
    if not root_text:
        sys.stderr.write(f"CENSUS unreadable: {overlay.registry_root_env} is not set\n")
        return 69
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolEmissionSink = (
        JsonlEmissionSink(Path(journal)) if journal else InMemoryEmissionSink()
    )
    store = YamlRatchetStore(
        Path(args.ratchet_file) if args.ratchet_file else Path(overlay.ratchet_file)
    )
    baseline_path: str | None = args.baseline_file or overlay.baseline_file
    baseline = YamlFileBaselineStore(Path(baseline_path)) if baseline_path else None
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
        baseline=baseline,
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
        return 0 if result.passed else (1 if result.failure_code in _RED else 69)
    for count in result.counts:
        line = "own-line" if count.own_line else "counted"
        sys.stdout.write(
            f"CENSUS {count.repository} {line} state={count.state} files={count.files} "
            f"readers={count.readers} writers={count.writers} ratchet={count.ratchet}\n"
        )
        for new in count.new_readers:
            sys.stdout.write(f"  NEW-READER {new}\n")
        if args.verbose:
            for hit in count.hits:
                sys.stdout.write(f"  {hit.kind.value} {hit.path}:{hit.line}\n")
    sys.stdout.write(
        f"CENSUS total={result.total_files} passed={'yes' if result.passed else 'no'} "
        f"failure={result.failure_code or 'none'} {result.failure_detail}\n"
    )
    if result.passed and args.lower:
        store.write(result.lowered_ratchet, _HEADER)
        if baseline is not None:
            baseline.write(result.lowered_baseline, _BASELINE_HEADER)
    if result.passed:
        return 0
    return 1 if result.failure_code in _RED else 69
