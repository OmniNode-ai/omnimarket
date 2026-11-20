# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex-capability-catalog``: render, digest, show and find over a catalog.json.

This CLI never collects a live fact itself in its render, digest, show and find verbs: it renders
whatever ``catalog.json`` it is given. See ``pyproject.toml``'s ``onex-capability-catalog`` entry
point.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ..models.model_capability_catalog import (
    Catalog,
)
from ..models.model_capability_catalog_collect_request import (
    ModelCapabilityCatalogCollectRequest,
)
from ..models.model_capability_catalog_render_request import (
    ModelCapabilityCatalogRenderRequest,
)
from .handler_capability_catalog_render import (
    HandlerCapabilityCatalogRender,
    find_term,
    load_static_facts,
    render_brief_part,
    render_digest,
)

DEFAULT_CATALOG_ENV = "ONEX_CAPABILITY_CATALOG_JSON"


def _load_catalog(path: Path) -> Catalog:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return Catalog.from_dict(data)


def _resolve_catalog_path(explicit: str | None) -> Path:
    import os

    if explicit:
        return Path(explicit)
    env_value = os.environ.get(DEFAULT_CATALOG_ENV)
    if env_value:
        return Path(env_value)
    raise SystemExit(
        f"no catalog.json given: pass --in <path> or set {DEFAULT_CATALOG_ENV} "
        "(fail-fast, no silent default path)"
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    """Collect the requested sections; the tools command is hosted on the contract's bus."""
    from .handler_capability_catalog_collect_lab import (
        HandlerCapabilityCatalogCollectLab,
        load_sources,
    )

    if not args.lab and not args.tools:
        print("collect: pass --lab and/or --tools", file=sys.stderr)
        return 2

    registry_root = Path(os.environ["OMNI_HOME"])
    sources = load_sources(Path(args.sources) if args.sources else None)
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    from ..models.model_capability_catalog import SCHEMA_ID

    catalog = Catalog(schema=SCHEMA_ID, generated_at=now)
    if args.lab:
        catalog = (
            HandlerCapabilityCatalogCollectLab()
            .handle(
                ModelCapabilityCatalogCollectRequest(
                    sources=sources, registry_root=registry_root, now=now
                )
            )
            .catalog
        )
    if args.tools:
        tools_catalog = dispatch_tools(
            registry_root=registry_root, home=Path.home(), now=now
        )
        if tools_catalog is None:
            return 1
        catalog = replace(
            tools_catalog,
            hosts=catalog.hosts,
            lanes=catalog.lanes,
            model_servers=catalog.model_servers,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "catalog.json"
    out_path.write_text(
        json.dumps(catalog.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        f"collected {len(catalog.hosts)} host(s), {len(catalog.lanes)} lane(s), "
        f"{len(catalog.launchd_jobs)} job(s), {len(catalog.engines)} engine(s) to {out_path}"
    )
    return 0


def dispatch_tools(*, registry_root: Path, home: Path, now: str) -> Catalog | None:
    """Use the same packaged-node runtime as the host installer; never call the handler here."""
    from omnibase_core.runtime.runtime_local import RuntimeLocal

    from ..models.model_capability_catalog_collect_tools_request import (
        ModelCapabilityCatalogCollectToolsRequest,
    )
    from ..models.model_capability_catalog_collect_tools_result import (
        ModelCapabilityCatalogCollectToolsResult,
    )

    request = ModelCapabilityCatalogCollectToolsRequest(
        registry_root=registry_root,
        home=home,
        now=now,
        ledger_path=Path(os.environ["ONEX_LEDGER_PATH"])
        if os.environ.get("ONEX_LEDGER_PATH")
        else None,
        delegation_health_path=(
            Path(os.environ["ONEX_DELEGATION_HEALTH_JSON"])
            if os.environ.get("ONEX_DELEGATION_HEALTH_JSON")
            else None
        ),
    )
    with TemporaryDirectory(prefix="capability-tools-") as scratch:
        root = Path(scratch)
        input_path = root / "request.json"
        input_path.write_text(request.model_dump_json())
        runtime = RuntimeLocal(
            Path(__file__).resolve().parents[1] / "contract.yaml",
            input_path=input_path,
            state_root=root / "state",
            run_id=request.correlation_id,
            backend_overrides={"event_bus": "inmemory"},
            timeout=180,
            host_handlers=True,
        )
        runtime.run()
        result = runtime.handler_result
        if not isinstance(result, ModelCapabilityCatalogCollectToolsResult):
            print(
                f"collect --tools: bus dispatch failed: {runtime.last_error}",
                file=sys.stderr,
            )
            return None
        return result.catalog


def _cmd_render(args: argparse.Namespace) -> int:
    catalog = _load_catalog(Path(args.in_path))
    static_facts = load_static_facts(
        Path(args.static_facts) if args.static_facts else None
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = HandlerCapabilityCatalogRender().handle(
        ModelCapabilityCatalogRenderRequest(
            catalog=catalog, static_facts=static_facts, target=args.target
        )
    )
    for name, text in result.views.items():
        (out_dir / name).write_text(text, encoding="utf-8")

    print(f"rendered to {out_dir}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    catalog = _load_catalog(_resolve_catalog_path(args.in_path))
    static_facts = load_static_facts(
        Path(args.static_facts) if args.static_facts else None
    )
    sections = args.section.split(",") if args.section else None
    text = render_digest(catalog, static_facts, sections=sections)
    if args.for_brief:
        text = render_brief_part(catalog, static_facts)
    sys.stdout.write(text)
    return 0


_SHOW_SECTIONS = {
    "hosts": lambda c: [h.to_dict() for h in c.hosts],
    "lanes": lambda c: [lane.to_dict() for lane in c.lanes],
    "engines": lambda c: [e.to_dict() for e in c.engines],
    "model_servers": lambda c: [m.to_dict() for m in c.model_servers],
    "jobs": lambda c: [j.to_dict() for j in c.launchd_jobs],
    "plugins": lambda c: [p.to_dict() for p in c.plugins],
    "skills": lambda c: [s.to_dict() for s in c.skills],
    "clis": lambda c: [cl.to_dict() for cl in c.clis],
    "mcp_servers": lambda c: [m.to_dict() for m in c.mcp_servers],
    "credentials": lambda c: [r.to_dict() for r in c.credential_refs],
    "docs": lambda c: [d.to_dict() for d in c.doc_references],
}


def _cmd_show(args: argparse.Namespace) -> int:
    catalog = _load_catalog(_resolve_catalog_path(args.in_path))
    section = args.section
    if section == "jobs" and args.state:
        rows = [j.to_dict() for j in catalog.launchd_jobs if j.state == args.state]
    elif section in _SHOW_SECTIONS:
        rows = _SHOW_SECTIONS[section](catalog)
    else:
        available = ", ".join(sorted(_SHOW_SECTIONS))
        print(f"unknown section {section!r}; one of: {available}", file=sys.stderr)
        return 2
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    catalog = _load_catalog(_resolve_catalog_path(args.in_path))
    static_facts = load_static_facts(
        Path(args.static_facts) if args.static_facts else None
    )
    hits = find_term(catalog, static_facts, args.term)
    if not hits:
        print(f"no match for {args.term!r} in the capability catalog", file=sys.stderr)
        return 1
    for hit in hits:
        print(hit)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-capability-catalog")
    parser.add_argument("--static-facts", help="override path to static_facts.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="collect live facts into catalog.json")
    collect_p.add_argument(
        "--lab", action="store_true", help="hosts, lanes, model servers"
    )
    collect_p.add_argument(
        "--tools", action="store_true", help="host-local tools and credential refs"
    )
    collect_p.add_argument("--out-dir", required=True)
    collect_p.add_argument(
        "--sources", default=None, help="override path to sources.yaml"
    )
    collect_p.set_defaults(func=_cmd_collect)

    render_p = sub.add_parser("render", help="render catalog.json into every view")
    render_p.add_argument("--in", dest="in_path", required=True)
    render_p.add_argument("--out-dir", required=True)
    render_p.add_argument("--target", choices=["claude-md"], default=None)
    render_p.set_defaults(func=_cmd_render)

    digest_p = sub.add_parser("digest", help="print the at-most-40-line digest")
    digest_p.add_argument("--in", dest="in_path", default=None)
    digest_p.add_argument(
        "--section", default=None, help="comma-separated: pool,engines,skills,ticks,lag"
    )
    digest_p.add_argument("--for-brief", action="store_true")
    digest_p.set_defaults(func=_cmd_digest)

    show_p = sub.add_parser("show", help="print one section as JSON")
    show_p.add_argument("section", choices=sorted(_SHOW_SECTIONS))
    show_p.add_argument("--in", dest="in_path", default=None)
    show_p.add_argument("--state", default=None, help="filter jobs by state, e.g. dead")
    show_p.set_defaults(func=_cmd_show)

    find_p = sub.add_parser(
        "find", help="search the catalog and static facts for a term"
    )
    find_p.add_argument("term")
    find_p.add_argument("--in", dest="in_path", default=None)
    find_p.set_defaults(func=_cmd_find)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
