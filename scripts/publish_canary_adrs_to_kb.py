# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Publish canary-extracted ADRs to the knowledge-base repo as a PR.

Usage:
    uv run python scripts/publish_canary_adrs_to_kb.py \\
        --canary-run-dir docs/adr-canary-runs/<run_id>/ \\
        --model-key qwen3-coder-local \\
        [--dry-run]

extracted_decisions.json format (array of ModelADRDraft-compatible dicts):
    [
        {
            "status": "Proposed",
            "date": "2026-05-23T10:00:00+00:00",
            "title": "...",
            "context": "...",
            "decision": "...",
            "consequences": "...",
            "alternatives_considered": [...],
            "supersedes": [],
            "source_evidence": [...],
            "extraction_metadata": {
                "model_id": "qwen3-coder-30b",
                "confidence": 0.87,
                "pipeline_version": "1.0.0",
                "prompt_template_id": "adr-extraction-v3",
                "prompt_template_version": "3.0.1",
                "canary_run_id": "<run_id>",
                "extracted_at": "2026-05-23T10:00:00+00:00"
            }
        },
        ...
    ]

[OMN-11808]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Publish canary ADR extractions to knowledge-base repo as a PR"
    )
    p.add_argument(
        "--canary-run-dir",
        type=Path,
        required=True,
        help="Path to canary run output directory containing extracted_decisions.json",
    )
    p.add_argument(
        "--model-key",
        type=str,
        required=True,
        help="Filter to decisions from this extraction_model_id (e.g. qwen3-coder-local)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without cloning, committing, or creating a PR",
    )
    p.add_argument(
        "--kb-repo",
        default="OmniNode-ai/knowledge-base",
        help="GitHub repo slug for the knowledge base (default: OmniNode-ai/knowledge-base)",
    )
    return p.parse_args()


def _load_decisions(decisions_file: Path, model_key: str) -> list[dict[str, object]]:
    """Read extracted_decisions.json and filter to the requested model key."""
    decisions: list[dict[str, object]] = json.loads(
        decisions_file.read_text(encoding="utf-8")
    )
    filtered = [
        d
        for d in decisions
        if isinstance(d, dict)
        and d.get("extraction_metadata", {}).get("model_id") == model_key  # type: ignore[union-attr]
    ]
    return filtered


def _run(
    decisions_file: Path,
    canary_run_dir: Path,
    model_key: str,
    kb_repo: str,
    dry_run: bool,
) -> int:
    if not decisions_file.exists():
        logger.error("extracted_decisions.json not found: %s", decisions_file)
        return 1

    model_decisions = _load_decisions(decisions_file, model_key)

    if not model_decisions:
        logger.error(
            "No decisions found with extraction_metadata.model_id == %r in %s",
            model_key,
            decisions_file,
        )
        return 1

    logger.info("Found %d decisions from model key %r", len(model_decisions), model_key)

    if dry_run:
        for d in model_decisions:
            title = d.get("title", "untitled")
            logger.info("  Would create ADR: %s", title)
        logger.info("Dry-run complete — no files written, no PR created.")
        return 0

    from omnibase_core.models.adr.model_adr_draft import ModelADRDraft

    from omnimarket.adapters.adr.kb_adr_renderer import render_adr_to_kb

    run_id = canary_run_dir.name

    with tempfile.TemporaryDirectory() as tmpdir:
        kb_dir = Path(tmpdir) / "knowledge-base"

        logger.info("Cloning %s ...", kb_repo)
        subprocess.run(
            ["gh", "repo", "clone", kb_repo, str(kb_dir)],
            check=True,
        )

        proposed_dir = kb_dir / "adrs" / "proposed"
        proposed_dir.mkdir(parents=True, exist_ok=True)

        branch = f"canary/{run_id}/{model_key}"
        subprocess.run(
            ["git", "-C", str(kb_dir), "checkout", "-b", branch],
            check=True,
        )

        rendered_count = 0
        for i, decision in enumerate(model_decisions, start=1):
            adr_id = f"ADR-PROPOSED-{i:04d}"
            draft = ModelADRDraft.model_validate(decision)
            result = render_adr_to_kb(
                draft=draft, adr_id=adr_id, output_dir=proposed_dir
            )
            logger.info("  Rendered: %s", result.adr_path.name)
            rendered_count += 1

        logger.info("Committing %d rendered ADRs ...", rendered_count)
        subprocess.run(["git", "-C", str(kb_dir), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(kb_dir),
                "commit",
                "-m",
                f"feat: propose {rendered_count} ADRs from canary run {run_id}",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(kb_dir), "push", "-u", "origin", branch],
            check=True,
        )

        pr_body = (
            "## Canary Extraction Results\n\n"
            f"- **Run ID**: `{run_id}`\n"
            f"- **Model**: `{model_key}`\n"
            f"- **Decisions extracted**: {rendered_count}\n\n"
            "All ADRs placed in `adrs/proposed/` for human review.\n"
            "Move to `adrs/` and change status to `accepted` after review.\n\n"
            "## Test plan\n"
            "- [ ] Each proposed ADR has valid frontmatter\n"
            "- [ ] No internal references in content\n"
            "- [ ] Evidence JSON files present in `adrs/proposed/`\n"
        )

        result_pr = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                kb_repo,
                "--title",
                f"feat: {rendered_count} proposed ADRs from canary run {run_id}",
                "--body",
                pr_body,
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(kb_dir),
        )
        pr_url = result_pr.stdout.strip()
        logger.info("PR created: %s", pr_url)

    return 0


def main() -> int:
    args = _parse_args()
    decisions_file = args.canary_run_dir / "extracted_decisions.json"
    return _run(
        decisions_file=decisions_file,
        canary_run_dir=args.canary_run_dir,
        model_key=args.model_key,
        kb_repo=args.kb_repo,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
