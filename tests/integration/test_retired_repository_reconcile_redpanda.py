# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deploy-agent proof of the installed host reconciler on the dev bus overlay.

The deploy agent supplies ONEX_RECONCILE_INSTALLED_PYTHON after installing the
branch revision. No checkout source is placed on that subprocess's import path.
The probe uses temporary clones and unique namespaced topics, never live clones.
An unavailable install or broker fails the check; nothing is skipped.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env

from omnimarket.models.model_host_reconcile import ModelHostReconcileRunResult
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from omnimarket.topic_namespace import apply_topic_namespace
from tests.bus_lane_guard import (
    assert_broker_in_declared_lane,
    declared_test_lane,
    load_lane_overlay,
    resolve_test_bus_broker,
)
from tests.test_host_reconcile_effect import run_git, setup_workspace
from tests.test_retired_repository_reconcile import retired_clone

pytestmark = [pytest.mark.kafka, pytest.mark.integration]


async def test_retirement_redpanda_installed_dev_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = os.environ.get("ONEX_RECONCILE_INSTALLED_PYTHON", "")
    assert installed, (
        "the deploy agent must supply the installed revision's interpreter"
    )
    interpreter = Path(installed)
    assert interpreter.is_absolute(), "the installed interpreter path must be absolute"
    assert interpreter.is_file(), "the installed interpreter must exist"
    assert declared_test_lane() == "dev", "this integration proof is dev-only"
    broker = assert_broker_in_declared_lane(resolve_test_bus_broker())
    overlay = load_lane_overlay()["lanes"][declared_test_lane()]
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", overlay["security_protocol"])
    monkeypatch.setenv("KAFKA_SASL_MECHANISM", overlay.get("sasl_mechanism", ""))
    monkeypatch.setenv("KAFKA_TOPIC_NAMESPACE", f"retirement-{uuid4().hex[:12]}")
    auth = build_aiokafka_auth_kwargs_from_env()
    topic = apply_topic_namespace(publish_topics()[1])

    root = setup_workspace(tmp_path, monkeypatch)
    clone = retired_clone(root)
    head = run_git(clone, "rev-parse", "HEAD")
    monkeypatch.setenv("KAFKA_BROKERS", broker)
    monkeypatch.setenv("ONEX_RECONCILE_STEP_TIMEOUT_S", "30")
    monkeypatch.setenv("ONEX_RECONCILE_RUN_TIMEOUT_S", "90")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    probe_env = scrub_git_location_env()
    for key in (
        "ONEX_RECONCILE_CLONE_DELEGATE",
        "ONEX_RECONCILE_VENV_DELEGATE",
        "ONEX_RECONCILE_GUARD_DELEGATE",
        "ONEX_RECONCILE_RECEIPT",
        "ONEX_DISPATCH_VENV",
        "ONEX_LEDGER_PATH",
    ):
        probe_env.pop(key, None)

    admin = AIOKafkaAdminClient(bootstrap_servers=broker, **auth)
    await asyncio.wait_for(admin.start(), timeout=15)
    created = False
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=broker,
        group_id=f"retirement-proof-{uuid4().hex}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        **auth,
    )
    try:
        await asyncio.wait_for(
            admin.create_topics(
                [NewTopic(topic, num_partitions=1, replication_factor=1)]
            ),
            timeout=15,
        )
        created = True
        await asyncio.wait_for(consumer.start(), timeout=15)
        try:
            for expected_exit in (0, 2):
                if expected_exit == 2:
                    scripts = root / "omnimarket/scripts"
                    scripts.mkdir()
                    (scripts / "sync.sh").write_text("clone onex_change_control\n")
                invocation = await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(interpreter),
                        "-m",
                        "omnimarket.nodes.node_host_reconcile_effect",
                        "--omni-home",
                        str(root),
                    ],
                    cwd=tmp_path,
                    env=probe_env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert invocation.returncode == expected_exit, invocation.stderr
                result = ModelHostReconcileRunResult.model_validate_json(
                    invocation.stdout
                )
                message = await asyncio.wait_for(consumer.getone(), timeout=30)
                delivered = ModelHostReconcileRunResult.model_validate_json(
                    message.value
                )
                assert delivered == result
                assert result.exit_code == expected_exit
                assert not any(
                    "could not publish" in line for line in result.diagnostics
                )
                if expected_exit == 0:
                    assert not clone.exists()
                    (bundle,) = (root / ".onex_state/retired-repository-bundles").glob(
                        "*.bundle"
                    )
                    restored = tmp_path / "restored"
                    run_git(tmp_path, "clone", str(bundle), str(restored))
                    assert run_git(restored, "rev-parse", "HEAD") == head
                    assert any(
                        s.surface == "retired-clone:omnibase_compat"
                        and s.verdict == "MOVED"
                        for s in delivered.surfaces
                    )
                else:
                    assert any(
                        s.surface == "retired-repository-targets"
                        and s.verdict == "UNHEALTHY"
                        for s in delivered.surfaces
                    )
        finally:
            await consumer.stop()
    finally:
        if created:
            await admin.delete_topics([topic])
        await admin.close()
