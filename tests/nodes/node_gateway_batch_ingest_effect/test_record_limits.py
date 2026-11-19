# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC11: per-record limits, and base64 is never decoded on an over-length string."""

from __future__ import annotations

import base64
import types

import pytest

from omnimarket.nodes.node_gateway_batch_ingest_effect import record_admission
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    post,
    record,
)


def _with(**fields: object) -> dict[str, object]:
    out = record()
    out.update(fields)
    return out


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (_with(key="A" * 65), "record-too-large"),
        (_with(value="A" * 8193), "record-too-large"),
        (_with(headers={f"h{i}": "v" for i in range(5)}), "record-headers-too-large"),
        (_with(headers={"h" * 33: "v"}), "record-headers-too-large"),
        (_with(headers={"h": "v" * 65}), "record-headers-too-large"),
    ],
)
async def test_record_limits_refuse(bad: dict[str, object], reason: str) -> None:
    app, publisher = make_app()
    result = await post(app, batch(record(), bad))
    assert (result.status, result.body["error"], result.body["index"]) == (
        422,
        reason,
        1,
    )
    assert publisher.calls == []


async def test_record_limits_base64_is_never_decoded_on_an_over_length_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lengths: list[int] = []
    real = base64.b64decode

    def spy(data: str | bytes, *args: object, **kwargs: object) -> bytes:
        lengths.append(len(data))
        return real(data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        record_admission, "base64", types.SimpleNamespace(b64decode=spy)
    )
    app, _ = make_app()
    await post(app, batch(_with(value="A" * 8193)))
    await post(app, batch(_with(key="A" * 65)))
    assert lengths == []
    await post(app, batch(record()))
    assert lengths
    assert max(lengths) <= 8192
