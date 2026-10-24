# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the owning transport gate without ever executing refused fixtures."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci import check_unimported_handlers as gate

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures/dynamic_import_gate"
FORMS = (
    "import_module",
    "__import__",
    "find_spec",
    "spec_from_file_location",
    "spec_from_loader",
)
DRIVERS = (
    "psycopg",
    "psycopg2",
    "asyncpg",
    "sqlalchemy",
    "kafka",
    "aiokafka",
    "confluent_kafka",
    "redis",
)
FOREIGN = "omnimarket.nodes.node_other_effect.handlers.handler_other"


def _handler(tmp_path: Path, source: str, kind: str = "orchestrator") -> Path:
    path = tmp_path / f"node_probe_{kind}/handlers/helper.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("target", [*DRIVERS, FOREIGN])
@pytest.mark.parametrize("constant", [False, True], ids=["literal", "module_constant"])
def test_dynamic_import_gate_forms(
    tmp_path: Path, form: str, target: str, constant: bool
) -> None:
    source = (FIXTURES / f"{form}.py.txt").read_text()
    source = (
        (f"from typing import Final\nTARGET: Final[str] = {target!r}\n" + source)
        if constant
        else source.replace("TARGET", repr(target))
    )
    path = _handler(tmp_path, source)
    violations = gate.dynamic_import_violations(path)
    assert len(violations) == 1
    assert target in violations[0][1]
    assert gate.transport_import_main([str(path)]) == 1


@pytest.mark.parametrize(
    "kind", ["compute", "reducer", "orchestrator", "effect", "legacy"]
)
@pytest.mark.parametrize("target", ["psycopg2.sql", FOREIGN])
def test_dynamic_import_gate_node_kinds(tmp_path: Path, kind: str, target: str) -> None:
    path = _handler(
        tmp_path, f"import importlib\nimportlib.import_module({target!r})\n", kind
    )
    assert gate.transport_import_main([str(path)]) == 1


@pytest.mark.parametrize(
    "name", ["compaction_runtime_binding.py", "session_barrier_probe.py"]
)
def test_dynamic_import_gate_compaction_fixture_refused(
    tmp_path: Path, name: str
) -> None:
    source = (FIXTURES / f"{name}.txt").read_text()
    path = _handler(tmp_path, source)
    assert gate.dynamic_import_violations(path)
    assert gate.transport_import_main([str(path)]) == 1
    # The live files were repaired, never admitted to the baseline.
    live = (
        ROOT
        / "src/omnimarket/nodes/node_context_compaction_orchestrator/handlers"
        / name
    )
    assert gate.dynamic_import_violations(live) == []


def test_dynamic_import_gate_compaction_fixture_bus_rework(tmp_path: Path) -> None:
    path = _handler(tmp_path, (FIXTURES / "bus_rework.py.txt").read_text())
    assert gate.transport_import_main([str(path)]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "from importlib import import_module as load\nload('redis')",
        "import importlib as il\nil.import_module('psycopg2.sql')",
        "from importlib import util as u\nu.find_spec('asyncpg')",
        "from importlib.util import spec_from_loader as load\nload('kafka', None)",
        "from builtins import __import__ as load\nload('sqlalchemy')",
        "import builtins as b\nb.__import__('confluent_kafka')",
        "import importlib\nload = importlib.import_module\nload('redis')",
        "import importlib\nTARGET = 'psy' + 'copg2.sql'\nimportlib.import_module(TARGET)",
        "import importlib\nTARGET = 'psycopg2'\nimportlib.import_module(f'{TARGET}.sql')",
        "import importlib.util\nimportlib.util.spec_from_file_location('safe', 'src/omnimarket/nodes/node_other_effect/handlers/x.py')",
        "import importlib\nimportlib.import_module('.node_other_effect.handlers.x', package='omnimarket.nodes')",
    ],
)
def test_dynamic_import_gate_aliases_and_indirection(
    tmp_path: Path, source: str
) -> None:
    assert gate.transport_import_main([str(_handler(tmp_path, source))]) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module('json')",
        "import importlib.util\nimportlib.util.find_spec('omnimarket')",
        "import importlib\nimportlib.import_module('omnimarket.nodes.node_probe_orchestrator.models.x')",
        "# importlib.import_module('redis')\nEXAMPLE = \"importlib.import_module('redis')\"",
        "import importlib\nimportlib.import_module('redis_tools')",
    ],
)
def test_dynamic_import_gate_safe_targets(tmp_path: Path, source: str) -> None:
    assert gate.transport_import_main([str(_handler(tmp_path, source))]) == 0


@pytest.mark.parametrize("kind", ["compute", "reducer", "orchestrator", "effect"])
def test_dynamic_import_gate_driver_static_parity(tmp_path: Path, kind: str) -> None:
    path = _handler(
        tmp_path, "import importlib\nimportlib.import_module('psycopg2.sql')", kind
    )
    assert gate.transport_import_main([str(path)]) == 1
    (path.parents[1] / "metadata.yaml").write_text(
        "dependencies: ['psycopg2-binary>=2.9']\n"
    )
    assert gate.transport_import_main([str(path)]) == (0 if kind == "effect" else 1)
    path.write_text("import psycopg2\n")
    assert gate.transport_import_main([str(path)]) == (
        1 if kind in {"compute", "reducer"} else 0
    )
    path.write_text(f"import importlib\nimportlib.import_module({FOREIGN!r})")
    assert gate.transport_import_main([str(path)]) == 1


def test_dynamic_import_gate_precommit_ci_wiring(tmp_path: Path) -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(
        h
        for repo in config["repos"]
        for h in repo["hooks"]
        if h["id"] == "validate-no-transport-imports"
    )
    assert hook["pass_filenames"] is False
    assert hook["always_run"] is True
    entry = shlex.split(hook["entry"])
    assert entry == [
        "uv",
        "run",
        "python",
        "scripts/ci/check_unimported_handlers.py",
        "--transport-imports",
    ]
    for kind in ("compute", "reducer", "orchestrator", "effect"):
        assert re.search(
            hook["files"], f"src/omnimarket/nodes/node_probe_{kind}/handlers/helper.py"
        )
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    lint = workflow["jobs"]["lint"]
    step = next(
        s
        for s in lint["steps"]
        if s.get("name") == "Transport imports (static and dynamic)"
    )
    assert step["run"] == hook["entry"]
    assert "if" not in step
    assert not step.get("continue-on-error")
    assert any(
        s.get("run") == "uv run pytest tests/ci -q -k dynamic_import_gate"
        for s in lint["steps"]
    )
    path = _handler(
        tmp_path, "import importlib\nimportlib.import_module('psycopg2.sql')"
    )
    result = subprocess.run(
        [sys.executable, *entry[3:], str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "psycopg2.sql" in result.stdout


def test_dynamic_import_gate_dev_census() -> None:
    assert gate.transport_import_main([]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nTARGET='json'\ndef f():\n    importlib.import_module(TARGET)\nTARGET='redis'",
        "import importlib\ndef f():\n    importlib.import_module(TARGET)\nTARGET='redis'",
        "def f():\n    load('redis')\nfrom importlib import import_module as load",
        "import importlib.util\nimportlib.util.spec_from_file_location('redis', 'src/omnimarket/nodes/node_probe_orchestrator/own.py')",
    ],
)
def test_dynamic_import_gate_late_globals_and_spec_name(
    tmp_path: Path, source: str
) -> None:
    assert gate.transport_import_main([str(_handler(tmp_path, source))]) == 1


def test_dynamic_import_gate_compaction_fixture_default_requires_adapters() -> None:
    from omnimarket.nodes.node_context_compaction_orchestrator.handlers.session_barrier_probe import (
        ModelSessionBarrierReadBinding,
        PostgresSessionBarrierProbeSource,
    )

    source = PostgresSessionBarrierProbeSource(
        ModelSessionBarrierReadBinding(
            dsn_env="PROBE_NOT_BOUND",
            lineage_relation="fixture.lineage",
            content_relation="fixture.content",
        )
    )
    with pytest.raises(RuntimeError, match="runtime-injected read adapters"):
        source.read("session", "barrier")


@pytest.mark.parametrize("location", ["services", "protocols", "orchestration", ""])
def test_dynamic_import_gate_helper_locations(tmp_path: Path, location: str) -> None:
    path = tmp_path / "node_probe_orchestrator" / location / "helper.py"
    path.parent.mkdir(parents=True)
    path.write_text("import importlib\nimportlib.import_module('redis')")
    assert gate.transport_import_main([str(path.parents[1])]) == 1


def test_dynamic_import_gate_effect_kind_from_metadata(tmp_path: Path) -> None:
    path = _handler(
        tmp_path, "import importlib\nimportlib.import_module('kafka')", "legacy"
    )
    (path.parents[1] / "metadata.yaml").write_text(
        "node_role: effect\ndependencies: ['kafka-python>=2']\n"
    )
    assert gate.transport_import_main([str(path)]) == 0


def test_dynamic_import_gate_incident_defeats_static_only_check(tmp_path: Path) -> None:
    from omnibase_core.validation.validator_transport_import import (
        ValidatorTransportImport,
    )

    validator = ValidatorTransportImport()
    for name in ("compaction_runtime_binding.py", "session_barrier_probe.py"):
        path = _handler(tmp_path, (FIXTURES / f"{name}.txt").read_text())
        assert validator.check_file(path) == []
        assert gate.transport_import_main([str(path)]) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.__import__('redis')",
        "from importlib import __import__ as load\nload('asyncpg')",
    ],
)
def test_dynamic_import_gate_importlib_dunder_import(
    tmp_path: Path, source: str
) -> None:
    assert gate.transport_import_main([str(_handler(tmp_path, source))]) == 1


@pytest.mark.parametrize("kind", ["compute", "reducer"])
def test_dynamic_import_gate_metadata_cannot_widen_static_policy(
    tmp_path: Path, kind: str
) -> None:
    path = _handler(
        tmp_path, "import importlib\nimportlib.import_module('redis')", kind
    )
    (path.parents[1] / "metadata.yaml").write_text(
        "node_role: effect\ndependencies: ['redis>=5']\n"
    )
    assert gate.transport_import_main([str(path)]) == 1
