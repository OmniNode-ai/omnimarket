# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence kind deployed: the dev lane's lab-pass receipt for the delivered sha, read from the lane-health projection.

The receipt is written by the lab-pass emitter as an Actions artifact and as the event node_projection_lab_lane_health
folds onto the lane's row (``receipt_sha``, ``receipt_result``). The projection API serves that row; this reader asks
it and names a PASS receipt of the declared lane as evidence for the declared runtime-served repositories. A FAIL
receipt, another lane's receipt or a row with no sha is no evidence. A projection that cannot be read is
unread, which the caller reports as unread and not as no evidence.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
)


class ModelLabPassLabels(BaseModel):
    """The lane, repositories, evidence labels and result word of the receipt: the lane id is the environment's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    repos: tuple[str, ...] = Field(min_length=1)
    kind: str = Field(min_length=1)
    source: str = Field(min_length=1)
    pass_result: str = Field(min_length=1)
    timeout_seconds: int = Field(default=10, gt=0)


def build_lab_pass_source(
    declared: dict[str, Any],
    environ: Mapping[str, str],
    fetch: Callable[[str], str] | None = None,
) -> tuple[SourceLabPassReceipts | None, str]:
    """The reader from the effect contract's ``lab_pass`` block and the environment, or None and why it cannot be built."""
    url = environ.get(declared["url_env"], "").strip()
    lane = environ.get(declared["lane_env"], "").strip()
    missing = [
        name
        for name, value in ((declared["url_env"], url), (declared["lane_env"], lane))
        if not value
    ]
    if missing:
        return None, f"{', '.join(missing)} not set"
    labels = dict(declared["labels"])
    labels["source"] = labels.pop("source_template").format(lane=lane)
    return SourceLabPassReceipts(
        url=url,
        labels=ModelLabPassLabels.model_validate({**labels, "lane": lane}),
        fetch=fetch,
    ), ""


def _http_get(url: str, timeout_seconds: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


class SourceLabPassReceipts:
    """Reads the lane-health projection and yields the declared lane's PASS receipt as deployed evidence."""

    def __init__(
        self,
        *,
        url: str,
        labels: ModelLabPassLabels,
        fetch: Callable[[str], str] | None = None,
    ) -> None:
        self._url = url
        self._labels = labels
        self._fetch = fetch or (lambda u: _http_get(u, labels.timeout_seconds))

    def read(self) -> ModelMrcEvidenceRead:
        try:
            document: dict[str, Any] = json.loads(self._fetch(self._url))
            rows: list[dict[str, Any]] = document["rows"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ModelMrcEvidenceRead(
                unread_cause=f"lab-pass receipt projection unreadable: {exc}"
            )
        found: list[ModelMrcEvidence] = []
        for row in rows:
            sha = str(row.get("receipt_sha") or "")
            if (
                row.get("lane") != self._labels.lane
                or not sha
                or row.get("receipt_result") != self._labels.pass_result
            ):
                continue
            observed = datetime.fromisoformat(str(row["receipt_observed_at"]))
            found.extend(
                ModelMrcEvidence(
                    repo=repo,
                    kind=self._labels.kind,
                    source=self._labels.source,
                    merge_sha=sha,
                    observed_at=observed,
                )
                for repo in self._labels.repos
            )
        return ModelMrcEvidenceRead(evidence=tuple(found))
