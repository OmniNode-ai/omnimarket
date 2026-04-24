# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for verify_target_mapping and ModelPrLifecycleStartCommand verify fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
    EnumVerificationTarget,
    classify_verification_outcome,
    map_changed_files_to_target,
)


@pytest.mark.unit
class TestMapChangedFilesToTarget:
    def test_projection_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/projection_bar.py"])
            == EnumVerificationTarget.PROJECTION_ROW_CHECK
        )

    def test_projector_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/projector_baz.py"])
            == EnumVerificationTarget.PROJECTION_ROW_CHECK
        )

    def test_handler_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/handler_qux.py"])
            == EnumVerificationTarget.PROJECTION_SINK_CHECK
        )

    def test_route_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/route_x.py"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_api_file(self) -> None:
        assert (
            map_changed_files_to_target(["src/foo/api_y.py"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_pages_api(self) -> None:
        assert (
            map_changed_files_to_target(["pages/api/something.ts"])
            == EnumVerificationTarget.API_ROUTE_CHECK
        )

    def test_drizzle(self) -> None:
        assert (
            map_changed_files_to_target(["drizzle/001.sql"])
            == EnumVerificationTarget.DB_MIGRATION_CHECK
        )

    def test_migrations(self) -> None:
        assert (
            map_changed_files_to_target(["migrations/002.sql"])
            == EnumVerificationTarget.DB_MIGRATION_CHECK
        )

    def test_topics_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["topics.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_contract_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["contract.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_nested_topics_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["src/omnimarket/nodes/node_foo/topics.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_nested_contract_yaml(self) -> None:
        assert (
            map_changed_files_to_target(["src/omnimarket/nodes/node_foo/contract.yaml"])
            == EnumVerificationTarget.KAFKA_TOPIC_CHECK
        )

    def test_no_match(self) -> None:
        assert (
            map_changed_files_to_target(["README.md"])
            == EnumVerificationTarget.SKIPPED_NO_MAPPING
        )

    def test_first_match_wins(self) -> None:
        assert (
            map_changed_files_to_target(["src/handler.py", "drizzle/001.sql"])
            == EnumVerificationTarget.PROJECTION_SINK_CHECK
        )

    def test_empty_list(self) -> None:
        assert (
            map_changed_files_to_target([]) == EnumVerificationTarget.SKIPPED_NO_MAPPING
        )


@pytest.mark.unit
class TestClassifyVerificationOutcome:
    def test_exit_zero_is_merged(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.PROJECTION_ROW_CHECK, 0, "", 1.0, 30
            )
            == EnumVerificationOutcome.MERGED
        )

    def test_exit_nonzero_is_verification_failed(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.PROJECTION_ROW_CHECK, 1, "error", 1.0, 30
            )
            == EnumVerificationOutcome.VERIFICATION_FAILED
        )

    def test_timeout_exceeded(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.API_ROUTE_CHECK, 0, "", 35.0, 30
            )
            == EnumVerificationOutcome.VERIFICATION_TIMEOUT
        )

    def test_unavailable_target_skipped(self) -> None:
        assert (
            classify_verification_outcome(
                EnumVerificationTarget.SKIPPED_NO_MAPPING, 0, "", 1.0, 30
            )
            == EnumVerificationOutcome.SKIPPED_NO_MAPPING
        )


@pytest.mark.unit
class TestModelPrLifecycleStartCommand:
    def test_default_verify_false(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        cmd = ModelPrLifecycleStartCommand(correlation_id=uuid4(), run_id="test-run")
        assert cmd.verify is False

    def test_verify_fields_set(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        cmd = ModelPrLifecycleStartCommand(
            correlation_id=uuid4(),
            run_id="test-run",
            verify=True,
            verify_timeout_seconds=60,
        )
        assert cmd.verify is True
        assert cmd.verify_timeout_seconds == 60

    def test_verify_timeout_minimum(self) -> None:
        from uuid import uuid4

        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        with pytest.raises(ValidationError):
            ModelPrLifecycleStartCommand(
                correlation_id=uuid4(), run_id="test-run", verify_timeout_seconds=0
            )
