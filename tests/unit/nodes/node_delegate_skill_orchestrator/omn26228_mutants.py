# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""
This plugin plants the defects that OMN-26228's negative controls must catch, so the contract can re-run them on origin/dev, and each mode fails closed when its target is missing.
OMN26228_REPEAT=N runs each selected test N times in the same process.
"""

import functools
import importlib
import json
import os
import time

import pytest

REPEAT_ENV = "OMN26228_REPEAT"

HITS = 0
_CURRENT_TEST: str | None = None
PER_TEST: dict[str, int] = {}
_ORIGINALS = {}
_MODE = None
_REPORT_PATH = None
_TERMINAL_TOPICS = None
_SCAN_RESULTS = {}
_ROUTE_RESULTS = {}
_SLOW_SCAN_SECONDS = 11


def _hit() -> None:
    global HITS
    HITS += 1
    if _CURRENT_TEST is not None:
        PER_TEST[_CURRENT_TEST] = PER_TEST.get(_CURRENT_TEST, 0) + 1


@pytest.fixture
def omn26228_repeat_index(request: pytest.FixtureRequest) -> int:
    """The 0-based repeat number of a test repeated by OMN26228_REPEAT."""
    return int(request.param)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    raw = os.environ.get(REPEAT_ENV, "").strip()
    if not raw:
        return
    try:
        count = int(raw)
    except ValueError as exc:
        raise pytest.UsageError(
            f"{REPEAT_ENV} must be a positive integer; got {raw!r}"
        ) from exc
    if count < 1:
        raise pytest.UsageError(f"{REPEAT_ENV} must be a positive integer; got {raw!r}")
    if count > 1:
        metafunc.fixturenames.append("omn26228_repeat_index")
        metafunc.parametrize(
            "omn26228_repeat_index",
            range(count),
            indirect=True,
            ids=[f"repeat{i + 1}" for i in range(count)],
        )


def pytest_runtest_setup(item: pytest.Item) -> None:
    global _CURRENT_TEST
    _CURRENT_TEST = item.nodeid


def pytest_runtest_teardown(item: pytest.Item) -> None:
    global _CURRENT_TEST
    _CURRENT_TEST = None


def pytest_configure(config):
    global HITS, _MODE, _REPORT_PATH, _TERMINAL_TOPICS, _SLOW_SCAN_SECONDS

    PER_TEST.clear()

    mode = os.environ.get("OMN26228_MUTANT", "").strip()
    if not mode:
        return

    if mode not in ("slow_scan", "no_terminal", "routing_reversed"):
        raise pytest.UsageError(
            f"OMN26228_MUTANT must be one of slow_scan, no_terminal, routing_reversed; got {mode!r}"
        )

    report_path = os.environ.get("OMN26228_REPORT", "").strip()
    if not report_path:
        raise pytest.UsageError(
            "OMN26228_REPORT must be set to a file path when OMN26228_MUTANT is set"
        )

    _MODE = mode
    _REPORT_PATH = report_path
    HITS = 0
    _ORIGINALS.clear()
    _SCAN_RESULTS.clear()
    _ROUTE_RESULTS.clear()

    if mode == "slow_scan":
        raw_delay = os.environ.get("OMN26228_SLOW_SCAN_SECONDS", "11")
        try:
            delay = float(raw_delay)
        except ValueError as exc:
            raise pytest.UsageError(
                f"OMN26228_SLOW_SCAN_SECONDS must be a number; got {raw_delay!r}"
            ) from exc
        if not delay > 10:
            raise pytest.UsageError(
                f"OMN26228_SLOW_SCAN_SECONDS must be greater than 10; got {raw_delay!r}"
            )
        _SLOW_SCAN_SECONDS = delay

        try:
            module = importlib.import_module(
                "omnibase_infra.handlers.handler_delegation_dispatch_terminal"
            )
        except ImportError as exc:
            raise pytest.UsageError(
                f"target module omnibase_infra.handlers.handler_delegation_dispatch_terminal is missing: {exc}"
            ) from exc
        try:
            original = module._run_step_topic
        except AttributeError as exc:
            raise pytest.UsageError(
                "target attribute _run_step_topic is missing on omnibase_infra.handlers.handler_delegation_dispatch_terminal"
            ) from exc

        # The delay is what the test measures against its deadline; the real scan's result does not change between repeats in one process, so it is computed once per package to keep 20 runs inside the hosted per-check timeout.
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            _hit()
            time.sleep(_SLOW_SCAN_SECONDS)
            package_name = args[1] if len(args) > 1 else kwargs.get("package_name")
            if package_name is None:
                raise ValueError(
                    "slow_scan wrapper requires package_name as the second positional argument or keyword"
                )
            if package_name not in _SCAN_RESULTS:
                _SCAN_RESULTS[package_name] = original(*args, **kwargs)
            return _SCAN_RESULTS[package_name]

        _ORIGINALS[
            (
                "omnibase_infra.handlers.handler_delegation_dispatch_terminal",
                "_run_step_topic",
            )
        ] = original
        module._run_step_topic = wrapper

        try:
            port_module = importlib.import_module(
                "omnibase_infra.runtime.service_delegation_dispatch_port"
            )
        except ImportError as exc:
            raise pytest.UsageError(
                f"target module omnibase_infra.runtime.service_delegation_dispatch_port is missing: {exc}"
            ) from exc
        try:
            route_original = port_module.discover_runtime_local_ingress_routes
        except AttributeError as exc:
            raise pytest.UsageError(
                "target attribute discover_runtime_local_ingress_routes is missing on omnibase_infra.runtime.service_delegation_dispatch_port"
            ) from exc

        # Route discovery parses every installed contract; the long-lived production port caches it (OMN-21271), and the guarded test runs it outside its deadline, so one discovery per process serves every repeat without changing what the deadline times.
        @functools.wraps(route_original)
        def route_wrapper(*args, **kwargs):
            package_names = args[0] if len(args) > 0 else kwargs.get("package_names")
            if package_names is None:
                raise ValueError(
                    "route wrapper requires package_names as the first positional argument or keyword"
                )
            key = tuple(package_names)
            if key not in _ROUTE_RESULTS:
                _ROUTE_RESULTS[key] = route_original(*args, **kwargs)
            return dict(_ROUTE_RESULTS[key])

        _ORIGINALS[
            (
                "omnibase_infra.runtime.service_delegation_dispatch_port",
                "discover_runtime_local_ingress_routes",
            )
        ] = route_original
        port_module.discover_runtime_local_ingress_routes = route_wrapper

    elif mode == "no_terminal":
        try:
            module = importlib.import_module(
                "omnibase_infra.event_bus.event_bus_inmemory"
            )
        except ImportError as exc:
            raise pytest.UsageError(
                f"target module omnibase_infra.event_bus.event_bus_inmemory is missing: {exc}"
            ) from exc
        try:
            cls = module.EventBusInmemory
        except AttributeError as exc:
            raise pytest.UsageError(
                "target attribute EventBusInmemory is missing on omnibase_infra.event_bus.event_bus_inmemory"
            ) from exc
        try:
            original = cls.publish
        except AttributeError as exc:
            raise pytest.UsageError(
                "target attribute publish is missing on EventBusInmemory"
            ) from exc

        try:
            port_module = importlib.import_module(
                "omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch"
            )
        except ImportError as exc:
            raise pytest.UsageError(
                f"target module omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch is missing: {exc}"
            ) from exc
        try:
            cfg = port_module.load_runtime_delegation_dispatch_config()
        except Exception as exc:
            raise pytest.UsageError(
                f"load_runtime_delegation_dispatch_config failed: {exc}"
            ) from exc
        topics = cfg.topics
        terminal_topics = {
            t
            for t in (
                topics.completed,
                topics.failed,
                topics.failed_unrouted,
                topics.pre_dispatch_failed,
            )
            if t is not None
        }
        if not terminal_topics:
            raise pytest.UsageError(
                "terminal topic set is empty; cannot plant no_terminal defect"
            )
        _TERMINAL_TOPICS = terminal_topics

        @functools.wraps(original)
        async def wrapper(self, topic, key, value, headers=None):
            if topic in _TERMINAL_TOPICS:
                _hit()
                return None
            return await original(self, topic, key, value, headers=headers)

        _ORIGINALS[
            ("omnibase_infra.event_bus.event_bus_inmemory", "EventBusInmemory.publish")
        ] = original
        cls.publish = wrapper

    elif mode == "routing_reversed":
        try:
            module = importlib.import_module(
                "omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing"
            )
        except ImportError as exc:
            raise pytest.UsageError(
                f"target module omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing is missing: {exc}"
            ) from exc
        try:
            original = module._tier_order_from_contract
        except AttributeError as exc:
            raise pytest.UsageError(
                "target attribute _tier_order_from_contract is missing on omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing"
            ) from exc

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            _hit()
            return tuple(reversed(tuple(original(*args, **kwargs))))

        _ORIGINALS[
            (
                "omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing",
                "_tier_order_from_contract",
            )
        ] = original
        module._tier_order_from_contract = wrapper


def pytest_unconfigure(config):
    global _MODE, _REPORT_PATH, _ORIGINALS, _TERMINAL_TOPICS

    if _MODE is None:
        return

    for (module_name, attr_name), original in _ORIGINALS.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if "." in attr_name:
            parts = attr_name.split(".")
            obj = module
            for part in parts[:-1]:
                obj = getattr(obj, part)
            setattr(obj, parts[-1], original)
        else:
            setattr(module, attr_name, original)

    with open(_REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"mode": _MODE, "hits": HITS, "per_test": PER_TEST}))

    PER_TEST.clear()

    _MODE = None
    _REPORT_PATH = None
    _ORIGINALS = {}
    _TERMINAL_TOPICS = None
    _SCAN_RESULTS.clear()
    _ROUTE_RESULTS.clear()
