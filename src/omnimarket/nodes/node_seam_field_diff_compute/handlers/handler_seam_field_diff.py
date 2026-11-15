# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure seam field diff handler (OMN-24121).

Compares the declared message shape a producer publishes with the declared
shape a consumer validates, per (topic, producer, consumer) pair, and
reports what is new compared with the base branch. No I/O.
"""

from __future__ import annotations

import re

from omnimarket.models.seam_field_diff import (
    EnumSeamFieldIssue,
    ModelSeamFieldDiffRequest,
    ModelSeamFieldDiffVerdict,
    ModelSeamFieldFacts,
    ModelSeamFieldFinding,
)


def _enum_values(type_name: str) -> set[str] | None:
    if type_name.startswith("enum:"):
        return set(type_name[5:].split(","))
    return None


def _const_value(type_name: str) -> set[str] | None:
    if type_name.startswith("const:"):
        return {type_name[6:]}
    return None


def _is_numeric_literal(value: str) -> bool:
    return re.fullmatch(r"-?\d+(\.\d+)?", value) is not None


def _compat(
    producer_type: str,
    consumer_type: str,
    producer_nested: dict[str, dict[str, ModelSeamFieldFacts]],
    consumer_nested: dict[str, dict[str, ModelSeamFieldFacts]],
    depth: int,
    depth_limit: int,
) -> bool:
    """Return True when producer_type is compatible with consumer_type."""
    # Treat either side 'any' as compatible
    if producer_type == "any" or consumer_type == "any":
        return True

    # Exact match for non-ref types
    if (
        producer_type == consumer_type
        and not producer_type.startswith("ref:")
        and "ref:" not in producer_type
    ):
        return True

    # Union handling: split on '|'
    producer_parts = set(producer_type.split("|"))
    consumer_parts = set(consumer_type.split("|"))
    if len(producer_parts) > 1 or len(consumer_parts) > 1:
        return all(
            any(
                _compat(a, b, producer_nested, consumer_nested, depth, depth_limit)
                for b in consumer_parts
            )
            for a in producer_parts
        )

    # Enum / const subset checks
    producer_vals = _enum_values(producer_type) or _const_value(producer_type)
    consumer_vals = _enum_values(consumer_type) or _const_value(consumer_type)
    if producer_vals is not None and consumer_vals is not None:
        return producer_vals <= consumer_vals
    if producer_vals is not None and consumer_type in (
        "string",
        "boolean",
        "integer",
        "number",
    ):
        if consumer_type == "boolean":
            return producer_vals <= {"True", "False"}
        if consumer_type in ("integer", "number"):
            return all(_is_numeric_literal(v) for v in producer_vals)
        return True

    # Array / map element compatibility
    if producer_type.startswith("array<") and consumer_type.startswith("array<"):
        return _compat(
            producer_type[6:-1],
            consumer_type[6:-1],
            producer_nested,
            consumer_nested,
            depth,
            depth_limit,
        )
    if producer_type.startswith("map<") and consumer_type.startswith("map<"):
        return _compat(
            producer_type[4:-1],
            consumer_type[4:-1],
            producer_nested,
            consumer_nested,
            depth,
            depth_limit,
        )

    # Ref recursion: compare nested model fields
    if producer_type.startswith("ref:") and consumer_type.startswith("ref:"):
        if depth + 1 >= depth_limit:
            return True
        producer_name = producer_type[4:]
        consumer_name = consumer_type[4:]
        producer_fields = producer_nested.get(producer_name)
        consumer_fields = consumer_nested.get(consumer_name)
        if producer_fields is None or consumer_fields is None:
            return producer_type == consumer_type
        return not _nested_issues(
            producer_fields,
            consumer_fields,
            producer_nested,
            consumer_nested,
            depth + 1,
            depth_limit,
        )

    # integer is compatible with number
    if producer_type == "integer" and consumer_type == "number":
        return True

    return False


def _nested_issues(
    producer_fields: dict[str, ModelSeamFieldFacts],
    consumer_fields: dict[str, ModelSeamFieldFacts],
    producer_nested: dict[str, dict[str, ModelSeamFieldFacts]],
    consumer_nested: dict[str, dict[str, ModelSeamFieldFacts]],
    depth: int,
    depth_limit: int,
) -> list[str]:
    """Return field names in consumer_fields that have issues vs producer_fields."""
    issues: list[str] = []
    for field_name, consumer_fact in consumer_fields.items():
        if field_name not in producer_fields:
            if consumer_fact.required and not consumer_fact.has_default:
                issues.append(field_name)
            continue
        producer_fact = producer_fields[field_name]
        if not _compat(
            producer_fact.type_name,
            consumer_fact.type_name,
            producer_nested,
            consumer_nested,
            depth,
            depth_limit,
        ):
            issues.append(field_name)
    return issues


class HandlerSeamFieldDiff:
    """Pure, deterministic seam field diff handler."""

    def handle(self, request: ModelSeamFieldDiffRequest) -> ModelSeamFieldDiffVerdict:
        base_keys = set(request.base_finding_keys)
        all_findings: list[ModelSeamFieldFinding] = []
        adapter_skipped: list[str] = []
        pairs_compared = 0

        for pair in request.head_pairs:
            # Same model ref: no findings, not counted
            if pair.producer.model_ref == pair.consumer.model_ref:
                continue

            # Consumer has adapter: skip, record in adapter_skipped
            if pair.consumer.has_adapter:
                adapter_skipped.append(pair.pair_key)
                continue

            pairs_compared += 1
            producer_fields = pair.producer.fields
            consumer_fields = pair.consumer.fields
            producer_nested = pair.producer.nested
            consumer_nested = pair.consumer.nested
            depth_limit = request.nested_depth_limit

            # Find consumer fields absent from producer
            for field_name, consumer_fact in sorted(consumer_fields.items()):
                if field_name not in producer_fields:
                    if consumer_fact.required and not consumer_fact.has_default:
                        all_findings.append(
                            ModelSeamFieldFinding(
                                pair_key=pair.pair_key,
                                topic=pair.topic,
                                issue=EnumSeamFieldIssue.MISSING_FIELD,
                                field=field_name,
                                detail=(
                                    f"consumer-required field '{field_name}' "
                                    f"(type={consumer_fact.type_name}) absent from producer"
                                ),
                            )
                        )
                    continue

                producer_fact = producer_fields[field_name]

                # Type compatibility check
                if not _compat(
                    producer_fact.type_name,
                    consumer_fact.type_name,
                    producer_nested,
                    consumer_nested,
                    0,
                    depth_limit,
                ):
                    all_findings.append(
                        ModelSeamFieldFinding(
                            pair_key=pair.pair_key,
                            topic=pair.topic,
                            issue=EnumSeamFieldIssue.TYPE_MISMATCH,
                            field=field_name,
                            detail=(
                                f"field '{field_name}': producer type "
                                f"'{producer_fact.type_name}' incompatible with "
                                f"consumer type '{consumer_fact.type_name}'"
                            ),
                        )
                    )
                else:
                    # Optional vs required check
                    if (
                        consumer_fact.required
                        and not producer_fact.required
                        and not producer_fact.has_default
                    ) or (
                        consumer_fact.required
                        and not consumer_fact.nullable
                        and producer_fact.nullable
                    ):
                        all_findings.append(
                            ModelSeamFieldFinding(
                                pair_key=pair.pair_key,
                                topic=pair.topic,
                                issue=EnumSeamFieldIssue.OPTIONAL_VS_REQUIRED,
                                field=field_name,
                                detail=(
                                    f"field '{field_name}': producer is optional "
                                    f"(required={producer_fact.required}, "
                                    f"nullable={producer_fact.nullable}) but "
                                    f"consumer requires it"
                                ),
                            )
                        )

            # Extra field rejected: producer fields absent from consumer
            if pair.consumer.extra_forbidden:
                for field_name in sorted(producer_fields):
                    if field_name not in consumer_fields:
                        producer_fact = producer_fields[field_name]
                        all_findings.append(
                            ModelSeamFieldFinding(
                                pair_key=pair.pair_key,
                                topic=pair.topic,
                                issue=EnumSeamFieldIssue.EXTRA_FIELD_REJECTED,
                                field=field_name,
                                detail=(
                                    f"producer field '{field_name}' "
                                    f"(type={producer_fact.type_name}) "
                                    f"rejected: consumer forbids extra fields"
                                ),
                            )
                        )

        # Sort all findings by key for determinism
        all_findings.sort(key=lambda f: f.key)
        findings = tuple(all_findings)

        # New findings: those whose key is not in base_finding_keys
        new_findings = tuple(f for f in findings if f.key not in base_keys)

        status = "refused" if new_findings else "clean"

        return ModelSeamFieldDiffVerdict(
            status=status,
            pairs_compared=pairs_compared,
            adapter_skipped=tuple(sorted(adapter_skipped)),
            findings=findings,
            new_findings=new_findings,
        )
