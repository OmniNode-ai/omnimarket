"""HandlerDiffFetcher — fetches a PR diff from GitHub and parses it into DiffHunk models.

Side-effect handler (I/O). Uses the GitHub REST API via httpx.

Responsibilities:
- Fetch the unified diff for a PR (with pagination for large diffs)
- Parse the unified diff into DiffHunk objects (one per file section)
- Filter out generated/lock files that should not be reviewed
- Respect the design doc constraint R3: truncate intelligently when diff is large

Design doc: docs/plans/2026-04-09-pr-review-bot-design.md (FETCH_DIFF phase)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

from omnimarket.nodes.node_pr_review_bot.models.models import DiffHunk

logger = logging.getLogger(__name__)

# Files that are auto-generated and should not be reviewed
_GENERATED_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
    re.compile(r"pnpm-lock\.yaml$"),
    re.compile(r"uv\.lock$"),
    re.compile(r"poetry\.lock$"),
    re.compile(r"Pipfile\.lock$"),
    re.compile(r"\.lock$"),
    re.compile(r".*\.min\.js$"),
    re.compile(r".*\.min\.css$"),
    re.compile(r"dist/"),
    re.compile(r"build/"),
    re.compile(r"__pycache__/"),
    re.compile(r"\.pyc$"),
    re.compile(r"migrations/\d+_"),
    re.compile(r"generated/"),
    re.compile(r"\.pb\.go$"),
    re.compile(r"_pb2\.py$"),
]

# Max raw diff bytes to process; beyond this we truncate (R3 guard)
_MAX_DIFF_BYTES = 512_000  # 512 KB


def _is_generated_file(path: str) -> bool:
    return any(pattern.search(path) for pattern in _GENERATED_FILE_PATTERNS)


@dataclass
class DiffFetcherConfig:
    """Configuration for HandlerDiffFetcher."""

    github_token: str = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN", "")
    )
    github_api_base: str = field(
        default_factory=lambda: os.environ.get(
            "GITHUB_API_BASE", "https://api.github.com"
        )
    )
    # Request timeout in seconds; see R4 note (judge latency) — diff fetch is fast
    request_timeout: float = 30.0
    # Max hunks to return; enforces context-window budget for the reviewer
    max_hunks: int = 200
    # Max lines per hunk before splitting — prevents single massive hunks
    max_lines_per_hunk: int = 150


class HandlerDiffFetcher:
    """Fetches a GitHub PR diff and returns a list of DiffHunk objects.

    Usage::

        fetcher = HandlerDiffFetcher(DiffFetcherConfig())
        hunks = await fetcher.fetch(pr_number=42, repo="owner/repo")
    """

    def __init__(self, config: DiffFetcherConfig | None = None) -> None:
        self._config = config or DiffFetcherConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self, pr_number: int, repo: str) -> list[DiffHunk]:
        """Fetch the unified diff for a PR and parse into DiffHunk objects.

        Args:
            pr_number: GitHub PR number.
            repo: Repository in ``owner/repo`` format.

        Returns:
            List of DiffHunk objects, one per contiguous changed segment.
            Generated/lock files are excluded.
            Large diffs are truncated to ``config.max_hunks`` hunks.
        """
        raw_diff = await self._fetch_raw_diff(pr_number=pr_number, repo=repo)
        if not raw_diff:
            logger.info("PR #%d in %s returned an empty diff", pr_number, repo)
            return []

        # R3: truncate extremely large diffs before parsing
        if len(raw_diff) > _MAX_DIFF_BYTES:
            logger.warning(
                "PR #%d diff is %d bytes — truncating to %d bytes (R3)",
                pr_number,
                len(raw_diff),
                _MAX_DIFF_BYTES,
            )
            raw_diff = raw_diff[:_MAX_DIFF_BYTES]

        hunks = self._parse_unified_diff(raw_diff)

        if len(hunks) > self._config.max_hunks:
            logger.warning(
                "PR #%d produced %d hunks — capping at %d",
                pr_number,
                len(hunks),
                self._config.max_hunks,
            )
            hunks = hunks[: self._config.max_hunks]

        return hunks

    # ------------------------------------------------------------------
    # Internal: GitHub API
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._config.github_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _fetch_raw_diff(self, pr_number: int, repo: str) -> str:
        """Fetch the raw unified diff from the GitHub API.

        Handles pagination via the ``Link`` header.  GitHub returns the full
        diff in one response for the ``diff`` media type, but the API may
        redirect; httpx follows redirects automatically.
        """
        url = f"{self._config.github_api_base}/repos/{repo}/pulls/{pr_number}"
        headers = self._build_headers()
        chunks: list[str] = []

        async with httpx.AsyncClient(
            timeout=self._config.request_timeout,
            follow_redirects=True,
        ) as client:
            page = 1
            while True:
                params: dict[str, str | int] = {"per_page": 100, "page": page}
                resp = await client.get(url, headers=headers, params=params)

                # Surface rate-limit information
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining is not None and int(remaining) < 100:
                    logger.warning(
                        "GitHub rate limit low: %s requests remaining (R8)", remaining
                    )

                resp.raise_for_status()
                chunk = resp.text
                if not chunk:
                    break
                chunks.append(chunk)

                # GitHub diff endpoint does not paginate — break after first response
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' not in link_header:
                    break
                page += 1

        return "".join(chunks)

    # ------------------------------------------------------------------
    # Internal: unified diff parser
    # ------------------------------------------------------------------

    def _parse_unified_diff(self, raw_diff: str) -> list[DiffHunk]:
        """Parse a unified diff string into a list of DiffHunk objects.

        Splits at ``diff --git`` boundaries, then further splits each file's
        hunks at ``@@`` markers.  Generated files are skipped.
        """
        hunks: list[DiffHunk] = []

        # Split on file boundaries
        file_sections = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)

        for section in file_sections:
            if not section.strip():
                continue

            file_path, is_new, is_deleted = _extract_file_meta(section)
            if not file_path:
                continue

            if _is_generated_file(file_path):
                logger.debug("Skipping generated file: %s", file_path)
                continue

            # Split into individual @@ hunks within the file section
            hunk_blocks = re.split(r"(?=^@@)", section, flags=re.MULTILINE)

            for block in hunk_blocks:
                if not block.startswith("@@"):
                    continue

                start_line, _end_line = _extract_line_range(block)
                if start_line == 0:
                    # Could not parse line range; use 1 as fallback
                    start_line = 1

                # Split large blocks if needed
                sub_blocks = _split_large_block(
                    block, max_lines=self._config.max_lines_per_hunk
                )
                sub_start = max(start_line, 1)
                for sub_block in sub_blocks:
                    if not sub_block.strip():
                        continue
                    sub_line_count = sub_block.count("\n")
                    sub_end = max(sub_start + sub_line_count - 1, sub_start)
                    hunks.append(
                        DiffHunk(
                            file_path=file_path,
                            start_line=sub_start,
                            end_line=sub_end,
                            content=sub_block,
                            is_new_file=is_new,
                            is_deleted_file=is_deleted,
                        )
                    )
                    sub_start = sub_end + 1

        return hunks


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_file_meta(section: str) -> tuple[str, bool, bool]:
    """Extract file path and new/deleted flags from a diff section header."""
    is_new = bool(re.search(r"^new file mode", section, re.MULTILINE))
    is_deleted = bool(re.search(r"^deleted file mode", section, re.MULTILINE))

    # Try +++ b/path first (standard unified diff)
    m = re.search(r"^\+\+\+ b/(.+)$", section, re.MULTILINE)
    if m:
        return m.group(1).strip(), is_new, is_deleted

    # Fallback: diff --git a/path b/path
    m = re.search(r"^diff --git a/\S+ b/(.+)$", section, re.MULTILINE)
    if m:
        return m.group(1).strip(), is_new, is_deleted

    return "", is_new, is_deleted


def _extract_line_range(hunk_block: str) -> tuple[int, int]:
    """Extract the new-file start and end line numbers from a @@ header.

    Unified diff format: ``@@ -old_start,old_count +new_start,new_count @@``
    We care about the new-file range (+) for finding placement.
    """
    m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk_block)
    if not m:
        return 0, 0
    start = int(m.group(1))
    count = int(m.group(2)) if m.group(2) is not None else 1
    end = start + max(count - 1, 0)
    return start, end


def _split_large_block(block: str, max_lines: int) -> list[str]:
    """Split a hunk block into sub-blocks of at most ``max_lines`` lines.

    Preserves the @@ header in the first sub-block only, since downstream
    consumers use the DiffHunk.start_line field for positioning.
    """
    lines = block.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return [block]

    sub_blocks: list[str] = []
    for i in range(0, len(lines), max_lines):
        sub_blocks.append("".join(lines[i : i + max_lines]))
    return sub_blocks


__all__: list[str] = [
    "DiffFetcherConfig",
    "HandlerDiffFetcher",
]
