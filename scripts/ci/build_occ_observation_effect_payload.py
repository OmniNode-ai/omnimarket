#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the node_occ_observation_effect payload from the attestation-observe
result + the GitHub Actions run identity (OMN-14888).

Reads ``occ_attestation_observe_result.json`` (the ``onex node
node_occ_attestation_observe --output receipt`` artifact from
``call-occ-attestation-observe.yml``) and recursively locates the nested
``ModelOccAutoauthorObservation`` payload by structural signature (the set of
field names unique to that model), rather than trusting one hardcoded nesting
path into the CLI's receipt wrapper — the wrapper shape is an implementation
detail of ``omnimarket.cli.reporting`` this script does not own.

Writes a ``ModelOccObservationEffectRequest`` payload (``mode="dry_run"`` by
default — see the OMN-14888 ticket's Architecture note for what going live
requires) ready for ``onex node node_occ_observation_effect --input <path>``.

``evidence_ticket`` is emitted EXPLICITLY (OMN-15323). It used to be omitted, so
every emission silently inherited the request model's field default and no
caller could see — let alone choose — which ticket the generated OCC PR would
bind its evidence to. The default is unchanged
(``OCC_OBSERVATION_EVIDENCE_TICKET``, the observation-store ticket): it must be
a ticket whose contract already exists on the OCC default branch, which the
triggering product PR's own ticket is NOT at the moment an observation fires —
citing it returns ``missing_contract`` rather than a merged PR.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnimarket.events.occ_observation_store import OCC_OBSERVATION_EVIDENCE_TICKET

_OBSERVATION_SIGNATURE_FIELDS = frozenset(
    {"product_repo", "product_pr_number", "observed_at", "reason"}
)


def find_observation_payload(node: object) -> dict[str, object] | None:
    """Recursively find the first dict matching the observation's field signature."""
    if isinstance(node, dict):
        if _OBSERVATION_SIGNATURE_FIELDS.issubset(node.keys()):
            return node
        for value in node.values():
            found = find_observation_payload(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_observation_payload(item)
            if found is not None:
                return found
    return None


def build_payload(
    *,
    observation: dict[str, object],
    head_sha: str,
    policy_version: str,
    workflow_run_id: int,
    run_attempt: int,
    recorded_at: str,
    occ_repo: str,
    mode: str,
    evidence_ticket: str,
) -> dict[str, object]:
    record = {
        "product_repo": observation["product_repo"],
        "product_pr_number": observation["product_pr_number"],
        "head_sha": head_sha,
        "policy_version": policy_version,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "recorded_at": recorded_at,
        "observation": observation,
    }
    return {
        "record": record,
        "occ_repo": occ_repo,
        "mode": mode,
        "evidence_ticket": evidence_ticket,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe-result", required=True, type=Path)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--policy-version", default="v1")
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--occ-repo", default="OmniNode-ai/onex_change_control")
    parser.add_argument("--mode", default="dry_run", choices=["dry_run", "mutate"])
    parser.add_argument(
        "--evidence-ticket",
        default=OCC_OBSERVATION_EVIDENCE_TICKET,
        help=(
            "OMN ticket the generated OCC PR binds its evidence to. Must be a "
            "ticket whose contract already exists on the OCC default branch."
        ),
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        raw = json.loads(args.observe_result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"build_occ_observation_effect_payload: cannot read/parse "
            f"{args.observe_result}: {exc}",
            file=sys.stderr,
        )
        return 1

    observation = find_observation_payload(raw)
    if observation is None:
        print(
            "build_occ_observation_effect_payload: no ModelOccAutoauthorObservation "
            f"payload found in {args.observe_result} (looked for a dict containing "
            f"{sorted(_OBSERVATION_SIGNATURE_FIELDS)})",
            file=sys.stderr,
        )
        return 1

    payload = build_payload(
        observation=observation,
        head_sha=args.head_sha,
        policy_version=args.policy_version,
        workflow_run_id=args.workflow_run_id,
        run_attempt=args.run_attempt,
        recorded_at=args.recorded_at,
        occ_repo=args.occ_repo,
        mode=args.mode,
        evidence_ticket=args.evidence_ticket,
    )
    args.output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    print(f"build_occ_observation_effect_payload: wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
