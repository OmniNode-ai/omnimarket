# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""publish_incremental_scan.py -- incremental corpus scanner for ADR extraction pipeline.

Detects new/modified markdown files since last run and publishes scoped canary
commands for extraction. State is persisted in .onex_state/adr-pipeline/last-run.json
to allow idempotent re-runs.

Usage:
    uv run python scripts/publish_incremental_scan.py --repos-root /path/to/omni_home [options]

Options:
    --dry-run                  Print planned scope and cost without publishing
    --since ISO8601            Override last-run timestamp (e.g. 2026-01-01T00:00:00)
    --manifest PATH            Path to discovery_manifest.yaml
    --rejected-manifest PATH   Path to rejected_manifest.yaml
    --state-dir PATH           Directory for last-run.json state
    --repos-root PATH          Root directory containing repo clones (required)
    --force-repropose          Re-propose previously rejected entries
    --bootstrap SERVERS        Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)
    --topic TOPIC              Override command topic (default: read from contract.yaml)

[OMN-11845]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Rough token estimate per markdown file for cost projection
_TOKENS_PER_FILE_EST = 2000
_COST_PER_1K_TOKENS_USD = 0.002


def _load_command_topic() -> str:
    """Load the subscribe topic from the canary orchestrator contract.yaml."""
    contract_path = (
        Path(__file__).parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_adr_canary_orchestrator"
        / "contract.yaml"
    )
    try:
        raw: dict[str, object] = yaml.safe_load(
            contract_path.read_text(encoding="utf-8")
        )
        eb = raw["event_bus"]
        topics = eb["subscribe_topics"]  # type: ignore[index]
        return str(topics[0])
    except Exception as exc:
        logger.warning("Could not read topic from contract: %s — using fallback", exc)
        parts = ["onex", "cmd", "omnimarket", "adr-canary-requested", "v1"]
        return ".".join(parts)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Incremental corpus scanner for ADR extraction pipeline"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned scope and cost without publishing",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override last-run timestamp (ISO 8601)",
    )
    p.add_argument(
        "--manifest",
        type=str,
        default="docs/adr-canary/discovery_manifest.yaml",
    )
    p.add_argument(
        "--rejected-manifest",
        type=str,
        default="docs/adr-canary/rejected_manifest.yaml",
    )
    p.add_argument(
        "--state-dir",
        type=str,
        default=".onex_state/adr-pipeline",
    )
    p.add_argument(
        "--repos-root",
        type=str,
        required=True,
        help="Root directory containing repo clones",
    )
    p.add_argument(
        "--force-repropose",
        action="store_true",
        help="Re-propose previously rejected entries",
    )
    p.add_argument(
        "--bootstrap",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        help="Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)",
    )
    p.add_argument(
        "--topic",
        default=None,
        help="Override command topic (default: read from contract.yaml)",
    )
    return p.parse_args()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_state(state_dir: Path) -> dict[str, object]:
    state_file = state_dir / "last-run.json"
    if not state_file.exists():
        return {}
    try:
        result: dict[str, object] = json.loads(state_file.read_text(encoding="utf-8"))
        return result
    except Exception as exc:
        logger.warning("Could not read state file %s: %s", state_file, exc)
        return {}


def _save_state(state_dir: Path, state: dict[str, object]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "last-run.json"
    state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    logger.info("State saved to %s", state_file)


def _load_rejected_hashes(rejected_manifest_path: Path) -> set[str]:
    if not rejected_manifest_path.exists():
        logger.warning(
            "rejected_manifest.yaml not found at %s — skipping rejection check",
            rejected_manifest_path,
        )
        return set()
    try:
        raw = yaml.safe_load(rejected_manifest_path.read_text(encoding="utf-8"))
        entries = raw.get("rejected_entries", []) if raw else []
        hashes: set[str] = set()
        for entry in entries:
            source_hashes = entry.get("source_hashes", [])
            if isinstance(source_hashes, list):
                hashes.update(source_hashes)
            elif isinstance(source_hashes, str):
                hashes.add(source_hashes)
        return hashes
    except Exception as exc:
        logger.warning("Could not parse rejected_manifest.yaml: %s", exc)
        return set()


def _resolve_workspace_config_path(raw_path: str, repos_root: Path) -> Path:
    """Resolve scanner config paths without assuming they live at repos_root."""
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate

    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.exists():
        return cwd_candidate

    return repos_root / candidate


def _git_modified_files(repo_path: Path, since: str) -> list[Path]:
    """Return .md files added or modified in repo since the given timestamp."""
    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        f"--since={since}",
        "--name-only",
        "--diff-filter=AM",
        "--pretty=format:",
        "--",
        "*.md",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "does not have any commits yet" in stderr:
                logger.warning("Skipping git repo with no commits yet: %s", repo_path)
                return []
            logger.error("git log failed for %s: %s", repo_path, stderr)
            return []
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        # Deduplicate (git log may list same file multiple times across commits)
        seen: set[str] = set()
        paths: list[Path] = []
        for rel in lines:
            if rel in seen:
                continue
            seen.add(rel)
            full = repo_path / rel
            if full.exists():
                paths.append(full)
        return paths
    except subprocess.TimeoutExpired:
        logger.error("git log timed out for %s", repo_path)
        return []
    except Exception as exc:
        logger.error("git log error for %s: %s", repo_path, exc)
        return []


def _discover_repos(repos_root: Path) -> list[Path]:
    """Return subdirectories of repos_root that are git repos."""
    repos: list[Path] = []
    for child in sorted(repos_root.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            repos.append(child)
    return repos


async def _publish_to_kafka(
    topic: str, bootstrap: str, payload: dict[str, object], envelope_id: str
) -> None:
    try:
        from aiokafka import AIOKafkaProducer
    except ImportError:
        logger.error("aiokafka not installed. Install with: uv add aiokafka")
        sys.exit(1)

    envelope = {
        "event_id": envelope_id,
        "event_type": topic,
        "correlation_id": str(uuid.uuid4()),
        "payload": payload,
    }

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=envelope)
        logger.info("Published to %s (event_id=%s)", topic, envelope_id)
    finally:
        await producer.stop()


async def _run(args: argparse.Namespace) -> int:
    repos_root = Path(args.repos_root)
    if not repos_root.is_dir():
        logger.error("--repos-root %s is not a directory", repos_root)
        return 1

    # Resolve state dir relative to repos_root if not absolute
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = repos_root / state_dir

    state = _load_state(state_dir)

    # Determine the since timestamp
    if args.since:
        since_ts = args.since
    elif state.get("last_run_timestamp"):
        since_ts = state["last_run_timestamp"]
    else:
        logger.info(
            "No last-run state found — treating as first run (scanning all history)"
        )
        # Use git epoch so we get everything
        since_ts = "1970-01-01T00:00:00"

    logger.info("Scanning for .md files modified since: %s", since_ts)

    # Load previously processed hashes for idempotency
    raw_processed = state.get("processed_files", [])
    processed_records: list[dict[str, object]] = (
        list(raw_processed) if isinstance(raw_processed, list) else []
    )
    processed_hashes: set[str] = {
        str(rec["sha256"]) for rec in processed_records if "sha256" in rec
    }

    # Load rejection hashes
    rejected_hashes: set[str] = set()
    if not args.force_repropose:
        rejected_manifest_path = _resolve_workspace_config_path(
            args.rejected_manifest, repos_root
        )
        rejected_hashes = _load_rejected_hashes(rejected_manifest_path)

    repos = _discover_repos(repos_root)
    if not repos:
        logger.warning("No git repos found under %s", repos_root)

    all_new_files: list[Path] = []
    skipped_rejected = 0
    skipped_processed = 0

    for repo in repos:
        repo_files = _git_modified_files(repo, since_ts)
        for f in repo_files:
            try:
                sha = _sha256_file(f)
            except Exception as exc:
                logger.warning("Could not hash %s: %s", f, exc)
                continue

            if sha in processed_hashes:
                skipped_processed += 1
                continue
            if sha in rejected_hashes:
                skipped_rejected += 1
                logger.debug("Skipping rejected file: %s", f)
                continue
            all_new_files.append(f)

    if not all_new_files:
        logger.info(
            "No new content since last run (skipped %d processed, %d rejected)",
            skipped_processed,
            skipped_rejected,
        )
        print("No new content since last run")
        return 0

    logger.info(
        "Found %d new/modified files (skipped %d processed, %d rejected)",
        len(all_new_files),
        skipped_processed,
        skipped_rejected,
    )

    estimated_tokens = len(all_new_files) * _TOKENS_PER_FILE_EST
    estimated_cost = (estimated_tokens / 1000) * _COST_PER_1K_TOKENS_USD

    if args.dry_run:
        print("\n--- Dry Run Report ---")
        print(f"New files to process: {len(all_new_files)}")
        print(f"Estimated extraction calls: {len(all_new_files)}")
        print(f"Estimated tokens: {estimated_tokens:,}")
        print(f"Estimated cost: ${estimated_cost:.4f} USD")
        print("\nFiles that would be processed:")
        for f in all_new_files:
            print(f"  {f}")
        return 0

    if not args.bootstrap:
        logger.error("KAFKA_BOOTSTRAP_SERVERS not set and --bootstrap not provided")
        return 1

    topic = args.topic or _load_command_topic()

    # Publish one scoped canary command covering the modified files
    manifest_path = _resolve_workspace_config_path(args.manifest, repos_root)
    file_paths = [str(f) for f in all_new_files]
    payload = {
        "manifest_path": str(manifest_path),
        "scoped_files": file_paths,
        "dry_run": False,
        "source": "incremental_scan",
        "since_timestamp": since_ts,
    }
    envelope_id = str(uuid.uuid4())

    logger.info("Publishing scoped canary command for %d files", len(all_new_files))
    await _publish_to_kafka(topic, args.bootstrap, payload, envelope_id)

    # Update state
    now = datetime.now(tz=UTC).isoformat()
    raw_prev = state.get("processed_files", [])
    new_records: list[dict[str, object]] = (
        list(raw_prev) if isinstance(raw_prev, list) else []
    )
    for f in all_new_files:
        try:
            sha = _sha256_file(f)
        except Exception:
            continue
        new_records.append({"path": str(f), "sha256": sha, "processed_at": now})

    new_state = {
        "last_run_timestamp": now,
        "processed_files": new_records,
        "scan_repos": [str(r) for r in repos],
        "total_files_scanned": len(all_new_files)
        + skipped_processed
        + skipped_rejected,
        "total_files_published": len(all_new_files),
    }
    _save_state(state_dir, new_state)

    logger.info("Done. Published command for %d files.", len(all_new_files))
    return 0


def main() -> None:
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
