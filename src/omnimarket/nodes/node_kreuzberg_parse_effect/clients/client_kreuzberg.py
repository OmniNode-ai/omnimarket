# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HTTP client for the kreuzberg document extraction REST service.

Migrated from omnimemory to omnimarket for OMN-8299 (Wave 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

__all__ = [
    "KreuzbergExtractionError",
    "KreuzbergTimeoutError",
    "call_kreuzberg_extract",
    "read_cached_text",
    "write_cached_text",
]

_FINGERPRINT_PREFIX = "fingerprint:"


class KreuzbergTimeoutError(Exception):
    """Raised when the kreuzberg /extract request exceeds the configured timeout."""


class KreuzbergExtractionError(Exception):
    """Raised when kreuzberg returns a non-2xx HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class KreuzbergExtractResult:
    """Result from a successful kreuzberg /extract call."""

    extracted_text: str
    status: Literal["ok"] = "ok"


async def call_kreuzberg_extract(
    *,
    kreuzberg_url: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    timeout_seconds: float,
) -> KreuzbergExtractResult:
    """Call kreuzberg POST /extract and return the extracted text."""
    extract_url = f"{kreuzberg_url.rstrip('/')}/extract"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
            response = await client.post(
                extract_url,
                files=[("files", (filename, file_bytes, mime_type))],
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise KreuzbergTimeoutError(
            f"kreuzberg extract timed out after {timeout_seconds:.1f}s"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise KreuzbergExtractionError(
            status_code=exc.response.status_code,
            detail=exc.response.text[:200],
        ) from exc
    except httpx.TransportError as exc:
        raise KreuzbergExtractionError(
            status_code=503,
            detail=f"kreuzberg transport error: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise KreuzbergExtractionError(
            status_code=503,
            detail=f"kreuzberg request error: {exc}",
        ) from exc

    try:
        response_data = response.json()
    except (ValueError, httpx.DecodingError) as exc:
        raise KreuzbergExtractionError(
            status_code=response.status_code,
            detail=f"kreuzberg returned non-JSON response: {exc}",
        ) from exc
    if not isinstance(response_data, list):
        raise KreuzbergExtractionError(
            status_code=200,
            detail=f"kreuzberg returned unexpected response type: {type(response_data).__name__}",
        )
    if not response_data:
        raise KreuzbergExtractionError(
            status_code=200,
            detail="kreuzberg returned empty response array",
        )
    try:
        content = response_data[0]["content"]
    except (KeyError, TypeError) as exc:
        raise KreuzbergExtractionError(
            status_code=200,
            detail="kreuzberg response item has unexpected format",
        ) from exc
    if content is None:
        raise KreuzbergExtractionError(
            status_code=200,
            detail="kreuzberg returned null content",
        )
    if not isinstance(content, str):
        raise KreuzbergExtractionError(
            status_code=200,
            detail=f"kreuzberg returned non-string content type: {type(content).__name__}",
        )
    if not content:
        raise KreuzbergExtractionError(
            status_code=200,
            detail="kreuzberg returned empty string content",
        )

    return KreuzbergExtractResult(extracted_text=content)


def read_cached_text(text_path: Path) -> tuple[str, str] | None:
    """Read cached text file and return (fingerprint, text) if available."""
    if not text_path.exists():
        return None
    try:
        content = text_path.read_text(encoding="utf-8")
        first_newline = content.index("\n")
        first_line = content[:first_newline]
        if not first_line.startswith(_FINGERPRINT_PREFIX):
            return None
        stored_fingerprint = first_line[len(_FINGERPRINT_PREFIX) :]
        text = content[first_newline + 1 :]
        return stored_fingerprint, text
    except (OSError, ValueError):
        return None


def write_cached_text(text_path: Path, fingerprint: str, text: str) -> None:
    """Write extracted text to the text store file."""
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(
        f"{_FINGERPRINT_PREFIX}{fingerprint}\n{text}",
        encoding="utf-8",
    )
