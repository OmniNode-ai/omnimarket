# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the bare-aiokafka-construction CI gate (OMN-15833).

The load-bearing coverage here is the OMN-14158 regression class: the prior
(``omnibase_infra``) version of this guard had a file-level substring
fallback that treated the helper name appearing *anywhere in the file* as
"wired", even on an unrelated/aliased import or a docstring mention, and even
when a *different* construction in the same file was genuinely bare. The
tests in the "call-site, not file-level" section below reproduce that exact
bypass and assert this gate does NOT have it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_aiokafka_construction_auth import _scan_file, main


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _scan_file — single-call cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_consumer_construction_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bare_consumer.py",
        "from aiokafka import AIOKafkaConsumer\n"
        'consumer = AIOKafkaConsumer("topic", bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1
    assert "AIOKafkaConsumer" in violations[0]


@pytest.mark.unit
def test_bare_producer_construction_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bare_producer.py",
        "from aiokafka import AIOKafkaProducer\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1
    assert "AIOKafkaProducer" in violations[0]


@pytest.mark.unit
def test_helper_dict_unpack_on_same_call_is_authed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "authed_consumer.py",
        "from aiokafka import AIOKafkaConsumer\n"
        "from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env\n"
        "consumer = AIOKafkaConsumer(\n"
        '    "topic",\n'
        '    bootstrap_servers="broker:9092",\n'
        "    **build_aiokafka_auth_kwargs_from_env(),\n"
        ")\n",
    )
    assert _scan_file(path) == []


@pytest.mark.unit
def test_explicit_security_protocol_kwarg_is_authed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "explicit_kwarg.py",
        "from aiokafka import AIOKafkaProducer\n"
        "producer = AIOKafkaProducer(\n"
        '    bootstrap_servers="broker:9092",\n'
        '    security_protocol="SASL_SSL",\n'
        ")\n",
    )
    assert _scan_file(path) == []


@pytest.mark.unit
def test_docstring_example_is_not_a_real_construction(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "docstring_only.py",
        '"""Example usage.\n\n'
        ">>> producer = AIOKafkaProducer(bootstrap_servers='x')\n"
        '"""\n',
    )
    assert _scan_file(path) == []


# ---------------------------------------------------------------------------
# Call-site, not file-level (OMN-14158 regression class)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_one_wired_one_bare_in_same_file_still_flags_the_bare_one(
    tmp_path: Path,
) -> None:
    """Reproduces OMN-14158's proven bypass: 'a 2-construction file with only
    1 wired passes' under the old file-level guard. This gate must flag the
    bare call even though the wired one appears earlier in the same file.
    """
    path = _write(
        tmp_path,
        "mixed.py",
        "from aiokafka import AIOKafkaConsumer, AIOKafkaProducer\n"
        "from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env\n"
        "consumer = AIOKafkaConsumer(\n"
        '    "topic",\n'
        '    bootstrap_servers="broker:9092",\n'
        "    **build_aiokafka_auth_kwargs_from_env(),\n"
        ")\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1
    assert "AIOKafkaProducer" in violations[0]


@pytest.mark.unit
def test_helper_mentioned_only_in_comment_does_not_authorize_bare_call(
    tmp_path: Path,
) -> None:
    """Reproduces OMN-14158's substring-fallback bypass: the helper name
    appearing anywhere in the file (here, a comment) must NOT satisfy the
    gate for a call that never actually spreads it.
    """
    path = _write(
        tmp_path,
        "comment_mention.py",
        "from aiokafka import AIOKafkaProducer\n"
        "# TODO: wire build_aiokafka_auth_kwargs_from_env() in here eventually\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1


@pytest.mark.unit
def test_unused_aliased_import_does_not_authorize_bare_call(tmp_path: Path) -> None:
    """Reproduces OMN-14158's other proven bypass shape: leaving an aliased,
    unused import of the helper must not satisfy the gate.
    """
    path = _write(
        tmp_path,
        "unused_import.py",
        "from aiokafka import AIOKafkaProducer\n"
        "from omnibase_infra.event_bus.kafka_auth import ("
        "    build_aiokafka_auth_kwargs_from_env as _unused,\n"
        ")\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1


@pytest.mark.unit
def test_helper_spread_on_a_different_call_does_not_authorize_this_one(
    tmp_path: Path,
) -> None:
    """A **build_aiokafka_auth_kwargs_from_env() spread bound to some other
    call (e.g. a helper factory unrelated to this constructor) must not
    authorize a sibling bare AIOKafkaConsumer/Producer call.
    """
    path = _write(
        tmp_path,
        "unrelated_call.py",
        "from aiokafka import AIOKafkaProducer\n"
        "from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env\n"
        "def _unrelated(**kwargs):\n"
        "    return kwargs\n"
        "_unrelated(**build_aiokafka_auth_kwargs_from_env())\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
    )
    violations = _scan_file(path)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# main() — allowlist + exit codes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_exits_zero_on_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src" / "omnimarket"
    src.mkdir(parents=True)
    (src / "clean.py").write_text(
        "from aiokafka import AIOKafkaProducer\n"
        "from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env\n"
        "producer = AIOKafkaProducer(\n"
        '    bootstrap_servers="broker:9092",\n'
        "    **build_aiokafka_auth_kwargs_from_env(),\n"
        ")\n",
        encoding="utf-8",
    )
    import scripts.ci.check_aiokafka_construction_auth as gate

    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_SCAN_ROOTS", (src,))
    assert main() == 0


@pytest.mark.unit
def test_main_exits_one_on_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src" / "omnimarket"
    src.mkdir(parents=True)
    (src / "bare.py").write_text(
        "from aiokafka import AIOKafkaProducer\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
        encoding="utf-8",
    )
    import scripts.ci.check_aiokafka_construction_auth as gate

    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_SCAN_ROOTS", (src,))
    assert main() == 1


@pytest.mark.unit
def test_main_allowlist_suppresses_named_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src" / "omnimarket"
    src.mkdir(parents=True)
    (src / "bare.py").write_text(
        "from aiokafka import AIOKafkaProducer\n"
        'producer = AIOKafkaProducer(bootstrap_servers="broker:9092")\n',
        encoding="utf-8",
    )
    import scripts.ci.check_aiokafka_construction_auth as gate

    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_SCAN_ROOTS", (src,))
    monkeypatch.setattr(
        gate, "_ALLOWLIST", {"src/omnimarket/bare.py": "test allowlist entry"}
    )
    assert main() == 0


@pytest.mark.unit
def test_main_stale_allowlist_entry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src" / "omnimarket"
    src.mkdir(parents=True)
    (src / "now_clean.py").write_text(
        "from aiokafka import AIOKafkaProducer\n"
        "from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env\n"
        "producer = AIOKafkaProducer(\n"
        '    bootstrap_servers="broker:9092",\n'
        "    **build_aiokafka_auth_kwargs_from_env(),\n"
        ")\n",
        encoding="utf-8",
    )
    import scripts.ci.check_aiokafka_construction_auth as gate

    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_SCAN_ROOTS", (src,))
    monkeypatch.setattr(
        gate, "_ALLOWLIST", {"src/omnimarket/now_clean.py": "stale — no longer applies"}
    )
    assert main() == 1


@pytest.mark.unit
def test_main_missing_scan_roots_is_invocation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ci.check_aiokafka_construction_auth as gate

    monkeypatch.setattr(gate, "_SCAN_ROOTS", (tmp_path / "does_not_exist",))
    assert main() == 2


# ---------------------------------------------------------------------------
# Live repo — positive control (this is the actual gate running for real)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_clean_against_the_real_repo() -> None:
    """Runs the gate against the real omnimarket src/ + scripts/ trees with no
    monkeypatching — the same invocation CI and pre-commit make. Must be 0
    after OMN-15833's conversion of all bare sites.
    """
    assert main() == 0
