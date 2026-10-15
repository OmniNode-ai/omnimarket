# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Driver-neutral SQL for a derivation on the actual UPSERT result.

Adapters own connections, transaction lifetime, value encoding and publication.
The callback owns domain computation. This handler only constrains columns,
binds values and checks the guarded update's final row.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Literal

from omnibase_core.models.projection.model_upsert_plan import ModelUpsertPlan

from omnimarket.models.model_projection_derived_write import (
    ModelProjectionDerivedWrite,
)

_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_IDENTITY_COLUMNS = frozenset({"tenant_id", "writer_identity", "written_at"})


class HandlerProjectionDerivedWrite:
    """Keep derived columns paired with the row inputs inside one transaction."""

    @staticmethod
    def prepare(
        plan: ModelUpsertPlan,
        *,
        row: Mapping[str, object],
        derive: Callable[[Mapping[str, object]], Mapping[str, object]] | None,
        input_columns: Sequence[str],
        derived_columns: Sequence[str],
        insert_only_columns: frozenset[str],
    ) -> ModelProjectionDerivedWrite | None:
        """Validate the derivation before any adapter acquires a connection."""
        if derive is None:
            if input_columns or derived_columns:
                raise ValueError("derive is required when derived columns are declared")
            return None
        for columns in (input_columns, derived_columns, plan.returning):
            if not columns or len(set(columns)) != len(columns):
                raise ValueError(
                    "derived write requires nonempty distinct column lists"
                )
            for column in columns:
                HandlerProjectionDerivedWrite._quote(column)
        forbidden = _IDENTITY_COLUMNS.union(
            plan.conflict_keys,
            input_columns,
            insert_only_columns,
            plan.expression_columns,
            row,
        )
        if forbidden.intersection(derived_columns):
            raise ValueError(
                "derived columns cannot change identity or supplied inputs"
            )
        return ModelProjectionDerivedWrite(
            table=plan.table,
            conflict_columns=tuple(plan.conflict_keys),
            input_columns=tuple(input_columns),
            derived_columns=tuple(derived_columns),
            returning=tuple(plan.returning),
            derive=derive,
        )

    @staticmethod
    def _quote(identifier: str) -> str:
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError(f"invalid projection identifier: {identifier!r}")
        return f'"{identifier}"'

    @staticmethod
    def derived_values(
        request: ModelProjectionDerivedWrite, stored: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Expose only the actual merged input columns to the domain callback."""
        inputs = {
            column: stored.get(column)
            for column in dict.fromkeys(
                (*request.conflict_columns, *request.input_columns)
            )
        }
        values = request.derive(MappingProxyType(inputs))
        if not isinstance(values, Mapping) or set(values) != set(
            request.derived_columns
        ):
            raise ValueError("derive must return exactly the declared derived columns")
        return values

    @staticmethod
    def update_statement(
        request: ModelProjectionDerivedWrite,
        stored: Mapping[str, object],
        values: Mapping[str, object],
        *,
        dialect: Literal["numeric", "pyformat", "sqlite"],
    ) -> tuple[str, list[tuple[str, object]]]:
        """Bind a null-safe equality guard against every conflict/input field."""
        quote = HandlerProjectionDerivedWrite._quote
        parameters: list[tuple[str, object]] = []

        def placeholder(column: str, value: object) -> str:
            parameters.append((column, value))
            if dialect == "numeric":
                return f"${len(parameters)}"
            return "?" if dialect == "sqlite" else "%s"

        assignments = ", ".join(
            f"{quote(column)} = {placeholder(column, values[column])}"
            for column in request.derived_columns
        )
        equality = "IS" if dialect == "sqlite" else "IS NOT DISTINCT FROM"
        predicates = " AND ".join(
            f"{quote(column)} {equality} {placeholder(column, stored[column])}"
            for column in dict.fromkeys(
                (*request.conflict_columns, *request.input_columns)
            )
        )
        statement = " ".join(
            ("UPDATE", quote(request.table), "SET", assignments, "WHERE", predicates)
        )
        if dialect != "sqlite":
            statement += " RETURNING " + ", ".join(
                quote(column) for column in request.returning
            )
        return statement, parameters

    @staticmethod
    def check_final_rows(
        request: ModelProjectionDerivedWrite,
        rows: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        """Refuse a missing/conflicting derived update instead of publishing it."""
        if len(rows) != 1:
            raise RuntimeError("derived input guard did not update exactly one row")
        return [{column: rows[0].get(column) for column in request.returning}]
