# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse the exposure's declared backend readers without a partial result."""

from __future__ import annotations

from pathlib import Path

from omnimarket.projection.models import ModelProjectionBackendReader


class MalformedBackendReadersError(ValueError):
    """A served exposure declared backend readers that cannot be trusted."""

    def __init__(self, node_name: str, contract_path: Path, detail: str) -> None:
        super().__init__(
            f"Contract {node_name!r} (path: {contract_path}): "
            f"projection_api.backend_readers {detail}"
        )


def _parse_backend_readers(
    raw_backend_readers: object,
    node_name: str,
    contract_path: Path,
) -> tuple[ModelProjectionBackendReader, ...]:
    """Parse backend readers without accepting a partial declaration."""
    if raw_backend_readers is None:
        return ()
    if not isinstance(raw_backend_readers, list):
        raise MalformedBackendReadersError(
            node_name, contract_path, "must be a list when present"
        )

    readers: list[ModelProjectionBackendReader] = []
    for index, raw_reader in enumerate(raw_backend_readers):
        if not isinstance(raw_reader, dict):
            raise MalformedBackendReadersError(
                node_name, contract_path, f"entries[{index}] must be mappings"
            )
        try:
            readers.append(ModelProjectionBackendReader.model_validate(raw_reader))
        except ValueError as exc:
            raise MalformedBackendReadersError(
                node_name, contract_path, f"entries[{index}] is invalid: {exc}"
            ) from exc
    reader_ids = [reader.id for reader in readers]
    if len(reader_ids) != len(set(reader_ids)):
        raise MalformedBackendReadersError(
            node_name, contract_path, "declares duplicate reader ids"
        )

    return tuple(readers)
