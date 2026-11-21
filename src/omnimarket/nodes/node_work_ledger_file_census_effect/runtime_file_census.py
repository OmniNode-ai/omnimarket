# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_file_census_effect``: the census against its ratchet.

Exit 0: every counted repository is at or under its ratchet row. Exit 1: a count grew past its row
(red). Exit 69: a repository is absent or unreadable, a repository has no ratchet row, or the ratchet
file is unreadable: nothing is passed on a read that failed. ``--lower`` rewrites the ratchet file with
every lowerable count lowered after a passing run; it never raises a row. ``--registry`` counts every
repository of the registry's clone list (a clone absent on the host is a typed failure, never a skip);
``--seed`` adds the ratchet row and per-file table of a present repository that has none, from this run's
own count, and changes no row already held.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    GitRepositorySource,
    YamlFileBaselineStore,
    YamlRatchetStore,
    read_registry_clone_aliases,
    read_registry_repositories,
    resolve_directories,
    seed_missing_rows,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusRepository,
    ModelCensusRequest,
    ModelCensusResult,
    ModelRepositoryCount,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.overlay import (
    OVERLAY_FILE_ENV,
    load_overlay,
)

_RED = ("ratchet-grew", "new-file-reader")
_HEADER = (
    "# Shrink-only ratchet: executable readers and writers of the ledger file's name or path variable,\n"
    "# counted in files per repository. A count above its row fails; `--lower` only ever lowers a row.\n"
    "# The `files` table holds, for each repository, the files that hold such a reader or writer with their\n"
    "# line counts (run with --per-file): a file with no row, or with more lines than its row, fails the\n"
    "# census (new code opening the ledger file). `--lower` drops a cleared file and never adds one.\n"
)


def _files_held(count: ModelRepositoryCount) -> dict[str, int]:
    held: dict[str, int] = {}
    for hit in count.hits:
        held[hit.path] = held.get(hit.path, 0) + 1
    return held


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
        "--per-file",
        action="store_true",
        help="also hold the per-file rows of the ratchet file's `files` table: a file with no row, or "
        "more reader or writer lines than its row, fails",
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
    parser.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help="the registry's clone list (repos.yaml; ONEX_REPO_REGISTRY when unset): every repository in it is "
        "counted, in place of the overlay's repositories list",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="add the ratchet row and per-file table of a present repository the ratchet file has none for, from "
        "this run's own count; a row already held is never changed",
    )
    parser.add_argument(
        "--daily-record",
        default=None,
        metavar="PATH",
        help="write the run's typed daily record (per-repository counts by surface and the runtime-code count) "
        "to PATH as JSON, and print it as one CENSUS-RECORD line",
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
    directories = dict(item.split("=", 1) for item in args.directory)
    registry_text = args.registry or env.get("ONEX_REPO_REGISTRY", "").strip()
    if registry_text:
        if args.register:
            parser.error("--registry and --register name the repositories twice")
        try:
            overlay = overlay.model_copy(
                update={"repositories": read_registry_repositories(Path(registry_text))}
            )
            directories = resolve_directories(
                Path(root_text),
                read_registry_clone_aliases(Path(registry_text)),
                directories,
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            sys.stderr.write(
                f"CENSUS unreadable: registry {registry_text}: {type(exc).__name__}: {exc}\n"
            )
            return 69
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolEmissionSink = (
        JsonlEmissionSink(Path(journal)) if journal else InMemoryEmissionSink()
    )
    store = YamlRatchetStore(
        Path(args.ratchet_file) if args.ratchet_file else Path(overlay.ratchet_file)
    )
    baseline = (
        YamlFileBaselineStore(Path(args.ratchet_file or overlay.ratchet_file))
        if args.per_file or overlay.per_file
        else None
    )
    census = HandlerFileCensus(
        overlay,
        GitRepositorySource(Path(root_text), overlay.max_file_bytes, directories),
        store,
        sink,
        backend="real" if journal else "in_memory",
        baseline=baseline,
    )

    def run() -> ModelCensusResult:
        return census.handle(
            ModelCensusRequest(
                correlation_id=uuid.uuid4().hex,
                repositories=tuple(args.repos) if args.repos else None,
            )
        )

    result = run()
    if args.seed:
        ratchet_path = Path(args.ratchet_file or overlay.ratchet_file)
        unseeded = [
            c for c in result.counts if c.state in ("unratcheted", "unbaselined")
        ]
        for repository in seed_missing_rows(
            ratchet_path,
            {c.repository: c.files for c in unseeded},
            {c.repository: _files_held(c) for c in unseeded},
            _HEADER,
        ):
            sys.stdout.write(f"SEED {repository}\n")
        result = run()
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
    if result.daily_record is not None:
        record = result.daily_record
        sys.stdout.write(f"CENSUS runtime-code files={record.runtime_files}\n")
        if args.daily_record:
            text = record.model_dump_json()
            Path(args.daily_record).write_text(text + "\n", encoding="utf-8")
            sys.stdout.write(f"CENSUS-RECORD {text}\n")
    sys.stdout.write(
        f"CENSUS total={result.total_files} passed={'yes' if result.passed else 'no'} "
        f"failure={result.failure_code or 'none'} {result.failure_detail}\n"
    )
    if result.passed and args.lower:
        store.write(result.lowered_ratchet, _HEADER)
        if baseline is not None:
            baseline.write(result.lowered_baseline, _HEADER)
    if result.passed:
        return 0
    return 1 if result.failure_code in _RED else 69
