# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick entry: ``--omni-home`` and ``--state-dir`` are the registry module tick's two flags."""

from __future__ import annotations

import argparse
import json
import sys
import time

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    HandlerDelegationReport,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_operator_access import (
    HandlerOperatorAccess,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_reports_publish import (
    HandlerReportsPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReportRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
    ModelOperatorAccessRequest,
    ModelReportsPublishRequest,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="factory_floor_publish",
        description="Refresh the factory floor inputs and record and publish them with the page to the serving host.",
    )
    ap.add_argument(
        "--omni-home",
        dest="registry_home",
        help="the registry home; default: the contract's home_env",
    )
    ap.add_argument("--state-dir", help="where last-run.json is written; default: none")
    ap.add_argument(
        "--skip-refresh",
        action="store_true",
        help="publish the record already on disk without running the factory floor effect",
    )
    ap.add_argument(
        "--skip-reports",
        action="store_true",
        help="publish only the factory floor, not the reports site beside it",
    )
    ap.add_argument(
        "--delegation-only",
        action="store_true",
        help="read delegation projection windows without publishing",
    )
    ap.add_argument(
        "--tick",
        action="store_true",
        help="print the delegation read as one session tick line",
    )
    ap.add_argument(
        "--lanes-running", type=int, help="live lane count from the caller's lane read"
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="make a small real call through each bound local chat backend",
    )
    ap.add_argument(
        "--refresh-delegation",
        action="store_true",
        help="refresh delegation even when other source refreshes are skipped",
    )
    args = ap.parse_args(argv)
    if args.tick and (not args.delegation_only or args.lanes_running is None):
        ap.error("--tick requires --delegation-only and --lanes-running")
    if args.lanes_running is not None and args.lanes_running < 0:
        ap.error("--lanes-running must be nonnegative")
    return args


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if a.delegation_only:
        reading = HandlerDelegationReport().handle(
            ModelDelegationReportRequest(
                probe=a.probe, lanes_running=a.lanes_running or 0
            )
        )
        sys.stdout.write(
            (reading.tick if a.tick else json.dumps(reading.report, indent=2)) + "\n"
        )
        return 0 if reading.report["status"] == "read" else 2
    # The operator-only gate comes first: no file is published to a server that does not carry it. A refusal there
    # withdraws the operator-only reports, is reported, and does not hold the shared pages back (the exit code stays 2).
    gate_refused = False
    run_start = time.monotonic()
    access_start = run_start
    try:
        access = HandlerOperatorAccess().handle(
            ModelOperatorAccessRequest(
                home=a.registry_home, skip_reports=a.skip_reports
            )
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"REFUSED factory-floor-publish: {exc}\n")
        return 2
    except PublishRefusedError as exc:
        sys.stderr.write(f"REFUSED operator-access: {exc}\n")
        gate_refused = True
    else:
        sys.stdout.write(
            f"operator-access: admitted_devices={access.admitted_devices} "
            f"include={access.include_sha256[:12]} changed={access.include_changed} "
            f"published={len(access.published)} files unread={','.join(access.unread) or 'none'} "
            f"seconds={time.monotonic() - access_start:.0f}\n"
        )
    request = ModelFactoryFloorPublishRequest(
        home=a.registry_home,
        state_dir=a.state_dir,
        skip_refresh=a.skip_refresh,
        refresh_delegation=a.refresh_delegation,
    )
    floor_refused = False
    try:
        result = HandlerFactoryFloorPublish().handle(request)
    except (PublishRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED factory-floor-publish: {exc}\n")
        floor_refused = True
    else:
        for step in result.steps:
            if step.status == "failed":
                sys.stderr.write(
                    f"factory-floor-publish: step {step.name} failed ({step.detail}): {step.diagnostic}\n"
                )
        sys.stdout.write(
            f"factory-floor-publish: published={','.join(result.published)} "
            f"record={result.generated_at} sha256={result.data_sha256[:12]} "
            f"steps={','.join(f'{s.name}:{s.status}:{s.seconds:.0f}s' for s in result.steps) or 'skipped'} "
            f"after_publish={','.join(f'{s.name}:{s.status}:{s.seconds:.0f}s' for s in result.after_publish) or 'none'} "
            f"cycle_seconds={result.cycle_seconds:.0f} "
            f"phases={','.join(f'{k}:{v:.0f}s' for k, v in result.phase_seconds.items())}\n"
        )
    if a.skip_reports:
        return 2 if gate_refused or floor_refused else 0
    reports_start = time.monotonic()
    try:
        reports = HandlerReportsPublish().handle(
            ModelReportsPublishRequest(home=a.registry_home)
        )
    except (PublishRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED reports-site-publish: {exc}\n")
        return 2
    sys.stdout.write(
        f"reports-site-publish: published={len(reports.published)} files "
        f"unread={','.join(reports.unread) or 'none'} manifest={reports.manifest_sha256[:12]} "
        f"index={reports.index_url} seconds={time.monotonic() - reports_start:.0f} "
        f"run_seconds={time.monotonic() - run_start:.0f}\n"
    )
    return 2 if gate_refused or floor_refused else 0
