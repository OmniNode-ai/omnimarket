# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry mutation and actual terminal controls; no real provider requests."""

import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.handlers.routing import byok_provider_backends as registry
from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsResult,
)
from omnimarket.models.model_endpoint_probe import (
    ModelEndpointProbeRequest,
    ModelEndpointProbeResult,
)
from omnimarket.nodes.node_onex_setup_effect.cli_choices import ask_choices

pytestmark = pytest.mark.e2e


def test_large_catalog_requires_explicit_shortlist_before_probe_consent(tmp_path):
    limit = ModelEndpointProbeRequest.model_json_schema()["properties"]["models"][
        "maxItems"
    ]
    models = tuple(f"new-vendor:model-{index}" for index in range(limit + 1))
    calls = []
    url = "http://127.0.0.1:12345"

    def probe(**kwargs):
        requested = kwargs["models"]
        assert requested == (models[0], models[-1])
        calls.append(requested)
        return ModelEndpointProbeResult.model_validate(
            {
                "url": url,
                "probes": [{"model": model, "status": "ready"} for model in requested],
            }
        )

    @click.command()
    def choose():
        chosen = ask_choices(
            "ollama",
            None,
            url,
            discover=lambda **_: ModelEndpointModelsResult(
                url=url, status="ready", models=models
            ),
            probe=probe,
            read_key=lambda *_: None,
        )
        click.echo("CHOSEN " + json.dumps(chosen))

    shortlist = models[0] + "," + models[-1]
    oversized = CliRunner().invoke(choose, input=",".join(models) + "\n")
    assert oversized.exit_code != 0
    assert "Nothing was probed" in oversized.output
    assert not calls
    denied = CliRunner().invoke(choose, input=shortlist + "\nn\n")
    assert denied.exit_code != 0
    assert not calls
    selected = CliRunner().invoke(
        choose, input=shortlist + "\ny\n" + models[-1] + "," + models[0] + "\n"
    )
    assert selected.exit_code == 0, selected.output
    assert all(model in selected.output for model in models)
    assert calls == [(models[0], models[-1])]
    assert (
        json.loads(selected.output.split("CHOSEN ")[-1])[1]
        == models[-1] + "," + models[0]
    )
    (tmp_path / "large-catalog-proof.json").write_text(
        json.dumps(
            {
                "catalog_count": len(models),
                "probe_limit": limit,
                "explicit_probe_selection": calls,
                "selected_order": [models[-1], models[0]],
            }
        )
    )


def test_live_metadata_orders_suggestions_but_preserves_literal_choice(
    tmp_path, monkeypatch
):
    original = next(iter(registry.load_byok_provider_catalog().values()))
    document = original.model_dump()
    document.update(
        provider="new-hosted",
        key_page="https://provider.example/keys",
        key_prefixes=["new-specific-"],
        first_key=True,
        default_models=["future-2"],
        model_preference=[
            {"pattern": r"future-(?P<generation>\d+)", "tags": ["future-family"]}
        ],
    )
    backend = registry.ModelByokProviderBackend.model_validate(document)
    monkeypatch.setattr(
        registry, "load_byok_provider_catalog", lambda: {backend.provider: backend}
    )
    monkeypatch.setattr(
        registry,
        "load_byok_plan_catalog",
        lambda: {(backend.provider, backend.plan): backend},
    )
    models = ("unknown.vendor:beta", "future-3", "future-12", "future-2")
    calls = []

    def discover(**kwargs):
        calls.append("catalog")
        return ModelEndpointModelsResult(
            url=backend.endpoint_url, status="ready", models=models
        )

    def probe(**kwargs):
        calls.append("probe")
        return ModelEndpointProbeResult.model_validate(
            {
                "url": backend.endpoint_url,
                "probes": [
                    {"model": name, "status": "ready", "message": "fixture"}
                    for name in models
                ],
            }
        )

    @click.command()
    def choose():
        result = ask_choices(
            None,
            None,
            None,
            discover=discover,
            probe=probe,
            read_key=lambda *_: SecretStr("new-specific-private"),
        )
        click.echo("CHOSEN " + json.dumps(result))

    declined = CliRunner().invoke(choose, input="\nn\n")
    assert declined.exit_code != 0
    assert calls == ["catalog"]
    calls.clear()
    selected = CliRunner().invoke(choose, input="\ny\nunknown.vendor:beta,future-12\n")
    assert selected.exit_code == 0, selected.output
    assert calls == ["catalog", "probe"]
    assert "new-hosted" in selected.output
    assert "Recommendation basis:" in selected.output
    displayed = [
        line.split(" ", 1)[0]
        for line in selected.output.splitlines()
        if line.split(" ", 1)[0] in models
    ]
    assert displayed == ["future-2", "future-12", "future-3", "unknown.vendor:beta"]
    assert (
        json.loads(selected.output.split("CHOSEN ")[-1])[1]
        == "unknown.vendor:beta,future-12"
    )
    assert "new-specific-private" not in selected.output
    (tmp_path / "recommendation-proof.json").write_text(
        json.dumps(
            {
                "displayed": displayed,
                "selected": ["unknown.vendor:beta", "future-12"],
                "calls": calls,
            }
        )
    )


def test_declared_prefixes_are_longest_match_and_endpoint_exempt(monkeypatch):
    template = next(iter(registry.load_byok_provider_catalog().values()))
    rows = {
        name: registry.ModelByokProviderBackend.model_validate(
            {
                **template.model_dump(),
                "provider": name,
                "key_prefixes": prefixes,
                "endpoint_from_registration": endpoint,
            }
        )
        for name, prefixes, endpoint in (
            ("broad", ["fixture-"], False),
            ("specific", ["fixture-child-"], False),
            ("arbitrary-endpoint", [], True),
        )
    }
    monkeypatch.setattr(registry, "load_byok_provider_catalog", lambda: rows)
    assert registry.key_provider_hint("fixture-child-secret", "broad") == "specific"
    assert registry.key_provider_hint("fixture-secret", "specific") == "broad"
    assert registry.key_provider_hint("unknown-secret-shape", "specific") is None
    assert (
        registry.key_provider_hint("fixture-child-secret", "arbitrary-endpoint") is None
    )


def test_actual_terminal_key_page_and_wrong_provider_refusal(tmp_path: Path):
    cli = str(Path(sys.executable).parent / "onex")
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            cli,
            "setup",
            "--interactive",
            "--yes",
            "--workspace",
            str(workspace),
            "--component",
            "delegation",
            "--provider",
            "openai",
            "--model",
            "literal-model",
        ],
        cwd=home,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={
            "HOME": str(home),
            "PATH": str(Path(cli).parent) + ":/usr/bin:/bin",
            "ONEX_STATE_DIR": str(tmp_path / "state"),
            "TERM": "dumb",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
        },
    )
    os.close(slave)
    output = bytearray()
    sent = False
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if b"input hidden" in output and not sent:
                    assert b"https://platform.openai.com/api-keys" in output
                    os.write(master, b"sk-ant-private-fixture\n")
                    sent = True
            if process.poll() is not None and not readable:
                break
        assert sent, output.decode(errors="replace")
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    text = output.decode(errors="replace")
    assert "https://platform.openai.com/api-keys" in text
    assert "anthropic" in text
    assert "openai" in text
    assert "sk-ant-private-fixture" not in text
    assert not workspace.exists()
    assert not (home / ".onex" / "setup.json").exists()
    (tmp_path / "terminal-guidance-proof.json").write_text(
        json.dumps(
            {
                "exit_code": process.returncode,
                "transcript": text,
                "workspace_created": workspace.exists(),
            }
        )
    )
