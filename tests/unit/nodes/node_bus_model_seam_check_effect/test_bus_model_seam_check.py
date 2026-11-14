# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Direct handler tests for the bus-model seam check."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_bus_model_seam_check_effect.handlers.handler_bus_model_seam_check import (
    HandlerBusModelSeamCheck,
)
from omnimarket.nodes.node_bus_model_seam_check_effect.models.model_bus_model_seam_check_request import (
    ModelBusModelSeamCheckRequest,
)

pytestmark = pytest.mark.unit
TOPIC = "onex.evt.seamfix.produced.v1"


def _repo_root() -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise AssertionError("pyproject.toml not found")


def _write_tree(
    tmp_path: Path,
    pkg: str,
    *,
    producer_fields: str = "a: int",
    consumer_extra: str = "forbid",
    consumer_fields: str = "a: int",
    consumer_event_model: bool = True,
) -> Path:
    root = tmp_path / "src" / pkg
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_producer").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_consumer").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "model_producer.py").write_text(
        "from pydantic import BaseModel\n\n\n"
        "class ModelSeamProducer(BaseModel):\n"
        + "\n".join(f"    {f}" for f in producer_fields.splitlines())
        + "\n"
    )
    consumer_body = (
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class ModelSeamConsumer(BaseModel):\n"
    )
    if consumer_extra:
        consumer_body += f'    model_config = ConfigDict(extra="{consumer_extra}")\n'
    consumer_body += "\n".join(f"    {f}" for f in consumer_fields.splitlines()) + "\n"
    (root / "models" / "model_consumer.py").write_text(consumer_body)
    producer_contract = {
        "terminal_event": TOPIC,
        "output_model": {
            "name": "ModelSeamProducer",
            "module": f"{pkg}.models.model_producer",
        },
        "published_events": [{"event_type": "ModelSeamProducer", "topic": TOPIC}],
    }
    handler_entry: dict = {"topic": TOPIC}
    if consumer_event_model:
        handler_entry["event_model"] = {
            "name": "ModelSeamConsumer",
            "module": f"{pkg}.models.model_consumer",
        }
    consumer_contract = {
        "input_model": {
            "name": "ModelSeamConsumer",
            "module": f"{pkg}.models.model_consumer",
        },
        "handler_routing": {"handlers": [handler_entry]},
    }
    (root / "nodes" / "node_seam_producer" / "contract.yaml").write_text(
        yaml.safe_dump(producer_contract)
    )
    (root / "nodes" / "node_seam_consumer" / "contract.yaml").write_text(
        yaml.safe_dump(consumer_contract)
    )
    return tmp_path


def _policy(tmp_path: Path, **overrides) -> Path:
    data = {"strict_models": [], "external_producers": [], "pins": []}
    data.update(overrides)
    p = tmp_path / "seam_policy.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _run(tmp_path: Path, pkg: str, policy: Path, monkeypatch) -> object:
    monkeypatch.syspath_prepend(tmp_path / "src")
    return HandlerBusModelSeamCheck().handle(
        ModelBusModelSeamCheckRequest(
            repo_root=str(tmp_path), package=pkg, policy_path=str(policy)
        )
    )


def test_a_field_added_to_a_bus_model_breaks_the_consumer_that_refuses_it(
    tmp_path, monkeypatch
):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg)
    policy = _policy(tmp_path)
    ok = _run(tmp_path, pkg, policy, monkeypatch)
    assert ok.admitted is True
    assert ok.exit_code == 0
    assert ok.pair_count == 1
    # The same tree after the producer's model gained a field. A fresh package name:
    # an already imported module would hide the change behind sys.modules.
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path / "after", pkg, producer_fields="a: int\nb: int = 0")
    tmp_path = tmp_path / "after"
    policy = _policy(tmp_path)
    bad = _run(tmp_path, pkg, policy, monkeypatch)
    assert bad.pair_count == 1
    assert bad.admitted is False
    assert bad.exit_code == 1
    pair = bad.pairs[0]
    assert pair.source == "explicit"
    assert pair.refused == ("b",)
    assert any(line.startswith("REFUSED") for line in bad.output_lines)


def test_consumer_with_extra_ignore_accepts_the_added_field(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(
        tmp_path, pkg, producer_fields="a: int\nb: int = 0", consumer_extra="ignore"
    )
    policy = _policy(tmp_path)
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is True
    assert result.exit_code == 0


def test_required_consumer_field_the_producer_lacks_is_missing(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg, consumer_fields="a: int\nc: int")
    policy = _policy(tmp_path)
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.pairs[0].missing == ("c",)
    assert any(line.startswith("REFUSED") for line in result.output_lines)


def test_inferred_pair_unpinned_fails(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(
        tmp_path, pkg, producer_fields="a: int\nb: int = 0", consumer_event_model=False
    )
    policy = _policy(tmp_path)
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any(line.startswith("UNPINNED") for line in result.output_lines)


def test_inferred_pair_pinned_is_admitted(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(
        tmp_path, pkg, producer_fields="a: int\nb: int = 0", consumer_event_model=False
    )
    policy = _policy(
        tmp_path,
        pins=[
            {
                "topic": TOPIC,
                "producer_model": f"{pkg}.models.model_producer.ModelSeamProducer",
                "consumer_model": f"{pkg}.models.model_consumer.ModelSeamConsumer",
                "refused": ["b"],
                "missing": [],
            }
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is True
    assert result.exit_code == 0
    assert result.pinned_pair_count == 1


def test_inferred_pair_stale_pin_fails(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(
        tmp_path, pkg, producer_fields="a: int\nb: int = 0", consumer_event_model=False
    )
    policy = _policy(
        tmp_path,
        pins=[
            {
                "topic": TOPIC,
                "producer_model": f"{pkg}.models.model_producer.ModelSeamProducer",
                "consumer_model": f"{pkg}.models.model_consumer.ModelSeamConsumer",
                "refused": ["b", "zzz"],
                "missing": [],
            }
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any(line.startswith("STALE PIN") for line in result.output_lines)


def test_pin_for_a_pair_that_does_not_exist_is_stale(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg)
    policy = _policy(
        tmp_path,
        pins=[
            {
                "topic": "onex.evt.nonexistent.v1",
                "producer_model": f"{pkg}.models.model_producer.ModelSeamProducer",
                "consumer_model": f"{pkg}.models.model_consumer.ModelSeamConsumer",
                "refused": [],
                "missing": [],
            }
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any(line.startswith("STALE PIN") for line in result.output_lines)


def test_explicit_pair_is_never_pinnable(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg, consumer_fields="a: int\nc: int")
    policy = _policy(
        tmp_path,
        pins=[
            {
                "topic": TOPIC,
                "producer_model": f"{pkg}.models.model_producer.ModelSeamProducer",
                "consumer_model": f"{pkg}.models.model_consumer.ModelSeamConsumer",
                "refused": [],
                "missing": ["c"],
            }
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert result.pinned_pair_count == 0
    assert any(line.startswith("REFUSED") for line in result.output_lines)


def test_strict_model_clean_pair_admitted(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg)
    policy = _policy(
        tmp_path,
        strict_models=[f"{pkg}.models.model_consumer.ModelSeamConsumer"],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is True
    assert result.exit_code == 0
    assert result.strict_failures == ()


def test_strict_model_refusing_pair_fails(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(
        tmp_path, pkg, producer_fields="a: int\nb: int = 0", consumer_event_model=False
    )
    policy = _policy(
        tmp_path,
        strict_models=[f"{pkg}.models.model_consumer.ModelSeamConsumer"],
        pins=[
            {
                "topic": TOPIC,
                "producer_model": f"{pkg}.models.model_producer.ModelSeamProducer",
                "consumer_model": f"{pkg}.models.model_consumer.ModelSeamConsumer",
                "refused": ["b"],
                "missing": [],
            }
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any(line.startswith("STRICT") for line in result.output_lines)


def test_strict_model_named_in_no_pair_fails(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    _write_tree(tmp_path, pkg)
    policy = _policy(
        tmp_path,
        strict_models=[f"{pkg}.models.model_consumer.DoesNotExist"],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any("has no pair" in line for line in result.output_lines)


def test_unresolvable_model_module(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    root = tmp_path / "src" / pkg
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_producer").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_consumer").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "model_producer.py").write_text(
        "from pydantic import BaseModel\n\n\n"
        "class ModelSeamProducer(BaseModel):\n    a: int\n"
    )
    (root / "models" / "model_consumer.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class ModelSeamConsumer(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n'
        "    a: int\n"
    )
    producer_contract = {
        "terminal_event": TOPIC,
        "output_model": {
            "name": "ModelSeamProducer",
            "module": f"{pkg}.models.model_producer",
        },
        "published_events": [{"event_type": "ModelSeamProducer", "topic": TOPIC}],
    }
    consumer_contract = {
        "input_model": {
            "name": "ModelSeamConsumer",
            "module": f"{pkg}.models.model_consumer",
        },
        "handler_routing": {
            "handlers": [
                {
                    "topic": TOPIC,
                    "event_model": {
                        "name": "ModelSeamConsumer",
                        "module": f"{pkg}.models.does_not_exist",
                    },
                }
            ]
        },
    }
    (root / "nodes" / "node_seam_producer" / "contract.yaml").write_text(
        yaml.safe_dump(producer_contract)
    )
    (root / "nodes" / "node_seam_consumer" / "contract.yaml").write_text(
        yaml.safe_dump(consumer_contract)
    )
    policy = _policy(tmp_path)
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is False
    assert result.exit_code == 1
    assert any(line.startswith("UNRESOLVED") for line in result.output_lines)


def test_external_producer_creates_pair_for_consumer_only_topic(tmp_path, monkeypatch):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    root = tmp_path / "src" / pkg
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_consumer").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "model_producer.py").write_text(
        "from pydantic import BaseModel\n\n\n"
        "class ModelSeamProducer(BaseModel):\n    a: int\n"
    )
    (root / "models" / "model_consumer.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class ModelSeamConsumer(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n'
        "    a: int\n"
    )
    consumer_contract = {
        "input_model": {
            "name": "ModelSeamConsumer",
            "module": f"{pkg}.models.model_consumer",
        },
        "handler_routing": {
            "handlers": [
                {
                    "topic": TOPIC,
                    "event_model": {
                        "name": "ModelSeamConsumer",
                        "module": f"{pkg}.models.model_consumer",
                    },
                }
            ]
        },
    }
    (root / "nodes" / "node_seam_consumer" / "contract.yaml").write_text(
        yaml.safe_dump(consumer_contract)
    )
    policy = _policy(
        tmp_path,
        external_producers=[
            {"topic": TOPIC, "model": f"{pkg}.models.model_producer.ModelSeamProducer"}
        ],
    )
    result = _run(tmp_path, pkg, policy, monkeypatch)
    assert result.admitted is True
    assert result.exit_code == 0
    assert result.pair_count == 1


def test_real_corpus_golden(tmp_path, monkeypatch):
    root = _repo_root()
    policy = (
        root
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_bus_model_seam_check_effect"
        / "seam_policy.yaml"
    )
    result = HandlerBusModelSeamCheck().handle(
        ModelBusModelSeamCheckRequest(
            repo_root=str(root), package="omnimarket", policy_path=str(policy)
        )
    )
    assert result.admitted is True
    assert result.exit_code == 0
    assert result.pair_count >= 70
    assert result.unpinned_pairs == ()
    assert result.stale_pins == ()
    assert result.strict_failures == ()
    strict_models = set(yaml.safe_load(policy.read_text())["strict_models"])
    for model in strict_models:
        assert any(
            p.producer_model == model or p.consumer_model == model for p in result.pairs
        )


def test_real_corpus_pins_are_debt_only_on_inferred_pairs(tmp_path, monkeypatch):
    root = _repo_root()
    policy_src = (
        root
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_bus_model_seam_check_effect"
        / "seam_policy.yaml"
    )
    data = yaml.safe_load(policy_src.read_text())
    data["pins"] = []
    policy = tmp_path / "seam_policy.yaml"
    policy.write_text(yaml.safe_dump(data))
    result = HandlerBusModelSeamCheck().handle(
        ModelBusModelSeamCheckRequest(
            repo_root=str(root), package="omnimarket", policy_path=str(policy)
        )
    )
    assert result.admitted is False
    assert result.exit_code == 1
    for pair in result.unpinned_pairs:
        assert pair.source == "inferred"
