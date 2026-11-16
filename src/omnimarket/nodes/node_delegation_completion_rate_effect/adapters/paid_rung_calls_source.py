# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The completed calls on the paid rungs: from given rows, or from delegation_events in the dev postgres over ssh (OMN-23756).

One read only transaction prints one JSON object per call: the model, its token counts, the booked cost and the completion
time. No prompt, no answer and no identifier leaves the database. The provider's own reported cost is not a column of
delegation_events, so it is never read here and the reading says per-model provider cost is absent.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from omnimarket.models.delegation.model_paid_rung_burn import ModelPaidRungCallRecord
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    Runner,
    psql_script,
    run_process,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_paid_rung_burn_run import (
    ModelPaidRungCallsRead,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    ModelDelegationEventsSource,
)

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")


class InMemoryPaidRungCallSource:
    """Reads given calls, or fails with a given reason."""

    def __init__(
        self,
        calls: Sequence[ModelPaidRungCallRecord] = (),
        error: str | None = None,
    ) -> None:
        self._calls = tuple(calls)
        self._error = error

    def read(self, since: datetime, slugs: tuple[str, ...]) -> ModelPaidRungCallsRead:
        if self._error is not None:
            return ModelPaidRungCallsRead(error=self._error)
        return ModelPaidRungCallsRead(
            calls=tuple(
                c
                for c in self._calls
                if c.model_id in slugs and c.completed_at >= since
            )
        )


def parse_calls(text: str) -> ModelPaidRungCallsRead:
    """The call lines of the ssh read, or the typed reason they were not usable."""
    try:
        calls = tuple(
            ModelPaidRungCallRecord(
                backend_id=item["backend"] or "not-recorded",
                model_id=item["model"],
                tokens_in=item["tin"],
                tokens_out=item["tout"],
                booked_cost_usd=Decimal(str(item["cost"])),
                completed_at=datetime.fromisoformat(item["t"]),
            )
            for item in (json.loads(line) for line in text.splitlines() if line.strip())
        )
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
        ValidationError,
    ):
        return ModelPaidRungCallsRead(error="calls-answer-malformed")
    return ModelPaidRungCallsRead(calls=calls)


class SshPostgresPaidRungCallSource:
    """Reads the paid-rung calls in a read only transaction inside the postgres container, over ssh."""

    def __init__(
        self,
        source: ModelDelegationEventsSource,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._source = source
        self._environ = environ
        self._run = run

    def script(self, since: datetime, slugs: tuple[str, ...]) -> str:
        if not slugs or not all(_SLUG.match(s) for s in slugs):
            msg = "paid rung slugs must be non-empty and plain model identifiers"
            raise ValueError(msg)
        listed = ", ".join("'" + s + "'" for s in slugs)
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
        sql = "\n".join(
            [
                "BEGIN READ ONLY;",
                "SELECT json_build_object('model', model_name, 'backend', backend_id, "
                "'tin', tokens_input, 'tout', tokens_output, 'cost', cost_usd, "
                "'t', to_char(\"timestamp\" AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'))::text "
                f"FROM delegation_events WHERE model_name IN ({listed}) "
                f"AND \"timestamp\" >= '{stamp}'::timestamptz AND NOT is_shadow "
                'ORDER BY "timestamp";',
                "COMMIT;",
            ]
        )
        return psql_script(self._source, sql)

    def read(self, since: datetime, slugs: tuple[str, ...]) -> ModelPaidRungCallsRead:
        host = self._environ.get(self._source.ssh_host_env, "").strip()
        if not host:
            return ModelPaidRungCallsRead(error="ssh-host-variable-unset")
        try:
            script = self.script(since, slugs)
            done = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                script.encode(),
                self._source.read_timeout_seconds,
            )
        except ValueError as exc:
            return ModelPaidRungCallsRead(error=f"calls-query-refused-{exc}")
        except subprocess.TimeoutExpired:
            return ModelPaidRungCallsRead(error="calls-read-timeout")
        except OSError as exc:
            return ModelPaidRungCallsRead(
                error=f"calls-read-unstartable-{type(exc).__name__}"
            )
        if done.returncode:
            return ModelPaidRungCallsRead(error=f"calls-read-exit-{done.returncode}")
        return parse_calls(done.stdout.decode("utf-8", errors="replace"))


__all__ = [
    "InMemoryPaidRungCallSource",
    "SshPostgresPaidRungCallSource",
    "parse_calls",
]
