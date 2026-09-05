# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex cloud`` — delegate to the OmniNode platform from a terminal (OMN-16967).

    onex cloud login --base-url https://<gateway> --api-key-stdin
    onex cloud delegate "<prompt>" --task-type summarization
    onex cloud receipt <workflow-id>
    onex cloud status
    onex cloud logout

HOW THIS COMMAND REACHES THE CLI — AND WHY IT LIVES IN THIS REPO
    It is NOT wired into the CLI by hand anywhere. ``omnimarket``'s
    ``pyproject.toml`` advertises it in the ``onex.cli`` entry-point group, and
    ``omnibase_core.cli.cli_commands`` discovers that group over the installed
    distributions at import time. Installing the delegate market package is
    therefore what makes ``onex cloud`` exist, and uninstalling it is what makes
    the command disappear — the 2026-08-29 operator ruling, made mechanical.

    The ratchet that keeps it that way is
    ``tests/unit/cli/test_cloud_cli_entry_point_registration_omn16967.py``: it
    fails if the declaration is dropped, if the name is claimed by a second
    distribution, or if anything in this repo starts calling ``add_command``
    with this group.

WHY THIS IS A SIBLING OF ``onex delegate``, NOT A FLAG ON IT
    ``onex delegate`` is the INTERNAL path: it publishes a typed command to the
    event bus (or runs the orchestrator in-process), resolves an
    omnimarket-provided node contract, and refuses to dispatch when the local
    ``omnimarket`` co-install has drifted from ``$OMNI_HOME/omnimarket``. Every
    one of those is a dev-workstation prerequisite, and none of them is
    reachable by a customer: customer credentials are minted deliberately
    without broker authorization.

    This command is the EXTERNAL/tenant path: HTTPS to the gateway, one
    credential, no checkout, no broker, no ``$OMNI_HOME``. Bolting an
    ``--api-key`` mode onto ``onex delegate`` would put two transports, two
    credential kinds and two prerequisite sets behind one command, and would
    make the internal tool a cloud client — the shape the 2026-08-03 OMN-15680
    ruling voided. Keeping them apart is what lets the internal path stay
    bus-only and the tenant path stay gateway-only.

    It is a ``click.Group`` because every other credential-bearing surface in
    this CLI is one (``auth``, ``kafka``, ``occ``); ``delegate`` is the lone
    bare command, and it is bare precisely because it carries no credential.

    The group is named ``cloud`` rather than ``delegate`` because
    ``omnibase_infra`` already advertises ``delegate`` in the same entry-point
    group. Two distributions claiming one name is not an override — the loader
    walks the group in iteration order — so a same-name registration would be a
    nondeterministic shadow of the local command.

WHERE THE CREDENTIAL COMES FROM
    Exactly three sources, in this order, and none of them is argv:

    1. ``--api-key-file`` (or ``$ONEX_API_KEY_FILE``) — a 0600 file holding the
       key. This is the CI / non-interactive form.
    2. the stored ``~/.onex`` ``cloud:`` block written by ``login``.
    3. nothing — a refusal naming ``onex cloud login``.

    There is no ``--api-key <value>`` option and there will not be one: a flag
    value lands in the process table, the shell history, and every exec log —
    three durable copies of a live customer credential that outlive the
    session. ``login`` reads it from stdin for the same reason.

WHERE THE BASE URL COMES FROM
    ``--base-url`` / ``$ONEX_API_BASE_URL`` / the stored block — and otherwise
    a REFUSAL. This module contains no gateway hostname at all. A default
    origin is not a convenience here; it is a live customer key sent to
    whatever host the release happened to ship with.

WHY THE FILES ARE THE POINT
    The operator's stated reason for preferring the terminal over a browser
    demo is that a browser cannot keep what it generates. So the saved files
    are first-class, not a side effect: every run writes ``result.txt``,
    ``receipt.json`` and ``run.json`` under
    ``<output-dir>/<workflow_id>/`` and the command prints those paths. A run
    that produced no content still writes its receipt — a failed run's receipt
    is exactly the evidence needed to say why.
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Final

import click
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr

from omnimarket.cloud.model_cloud_delegation import (
    ModelCloudDelegationReceipt,
)
from omnimarket.cloud.store_tenant_api_credential import (
    StoreTenantApiCredential,
)
from omnimarket.cloud.transport_cloud_delegation import (
    CLOUD_DELEGATION_WORKFLOW_TYPE,
    TransportCloudDelegation,
)

__all__ = ["CLOUD_TASK_TYPE_CHOICES", "cloud_group"]

# The task taxonomy the gateway's delegation-inference contract accepts,
# transcribed from its payload_schema pattern
# (omninode_infra ``docker/onex-api/workflow-contracts.yaml``). Offering a
# closed choice here turns a server-side 400 into a shell completion.
CLOUD_TASK_TYPE_CHOICES: Final[tuple[str, ...]] = (
    "test",
    "document",
    "research",
    "code_generation",
    "code_review",
    "refactor",
    "reasoning",
    "complex_reasoning",
    "planning",
    "review",
    "summarization",
)

_DEFAULT_OUTPUT_DIR: Final[str] = "onex-delegations"
_DEFAULT_PROFILE: Final[str] = "default"
_LOGIN_HINT: Final[str] = (
    "run 'onex cloud login --base-url <gateway origin> --api-key-stdin' with a "
    "key created in the dashboard"
)


def _fail(message: str) -> click.ClickException:
    """Build the one exception shape this module raises for operator errors."""
    return click.ClickException(message)


def _store(onex_home: Path | None) -> StoreTenantApiCredential:
    return StoreTenantApiCredential(onex_home=onex_home or (Path.home() / ".onex"))


def _read_key_file(path: Path) -> str:
    """Read a key from a file, refusing a group- or world-readable one.

    The permission check is not ceremony: this is the CI form, and a key file
    committed or copied at 0644 is the most common way a live credential
    becomes readable by every process on a shared runner.
    """
    if not path.exists():
        raise _fail(f"no API key file at {path}.")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise _fail(
            f"{path} is mode {mode:04o}; it must be 0600 (owner-only). "
            f"Fix with: chmod 600 {path}"
        )
    key = path.read_text().strip()
    if not key:
        raise _fail(f"{path} is empty — it must contain the onxk_ API key.")
    return key


def _resolve_credential(
    *,
    onex_home: Path | None,
    base_url: str | None,
    api_key_file: Path | None,
) -> tuple[str, SecretStr]:
    """Resolve (base_url, api_key) from explicit config, or refuse.

    Explicit flags/env beat the stored block; nothing beats "unset". Both
    halves must resolve — a base URL with no key, or a key with no base URL,
    is a half-configured client, which is the state that produces a request
    nobody meant to send.
    """
    if api_key_file is not None:
        if base_url is None:
            raise _fail(
                "--api-key-file was given but no base URL. Pass --base-url "
                "(or set $ONEX_API_BASE_URL) — there is no default gateway "
                "origin."
            )
        return base_url, SecretStr(_read_key_file(api_key_file))

    try:
        credential = _store(onex_home).load()
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc
    # An explicit --base-url still wins over the stored one, so a customer can
    # point a stored key at a second environment without re-running login.
    return base_url or credential.base_url, credential.api_key


def _transport_factory_from_context(ctx: click.Context) -> Any:
    """Return the client constructor, honouring a test-injected seam.

    The real path constructs :class:`TransportCloudDelegation`; a test passes
    ``obj={"transport_factory": ...}`` and drives the command with no socket. The
    seam is the constructor, not a patched module attribute, so what the tests
    exercise is the command's real wiring.
    """
    if isinstance(ctx.obj, dict) and "transport_factory" in ctx.obj:
        return ctx.obj["transport_factory"]
    return TransportCloudDelegation


@click.group("cloud")
def cloud_group() -> None:  # stub-ok
    """Delegate to the OmniNode platform with a dashboard API key."""


# --------------------------------------------------------------------------
# login / status / logout
# --------------------------------------------------------------------------


@cloud_group.command("login")
@click.option(
    "--base-url",
    required=True,
    envvar="ONEX_API_BASE_URL",
    help=(
        "Gateway origin the key belongs to, e.g. https://dev.api.omninode.ai. "
        "Required — no default origin exists, because a wrong default sends a "
        "live key to the wrong host."
    ),
)
@click.option(
    "--api-key-stdin",
    "api_key_stdin",
    is_flag=True,
    required=True,
    help=(
        "Read the onxk_ API key from stdin. The only accepted form — a flag "
        "value would leak into the process table and shell history."
    ),
)
@click.option(
    "--profile",
    default=_DEFAULT_PROFILE,
    show_default=True,
    help="Label for this key, so a staging and a production key can coexist.",
)
@click.option(
    "--onex-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ~/.onex root (test and multi-profile use).",
)
def cloud_login(
    base_url: str, api_key_stdin: bool, profile: str, onex_home: Path | None
) -> None:
    """Store a dashboard API key by reference under ~/.onex.

    \b
    Example:
        read -rs ONXK && printf '%s' "$ONXK" | \\
          onex cloud login --base-url https://dev.api.omninode.ai --api-key-stdin
    """
    if not api_key_stdin:  # pragma: no cover - click marks the flag required
        raise _fail("--api-key-stdin is required; the key is never taken from argv.")

    api_key = sys.stdin.read().strip()
    if not api_key:
        raise _fail(
            "no API key on stdin. Pipe it, e.g.: "
            "printf '%s' \"$ONXK\" | onex cloud login ... --api-key-stdin"
        )

    try:
        _store(onex_home).save(base_url=base_url, api_key=api_key, profile=profile)
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc

    click.echo(f"Stored the OmniNode API key for {base_url} (profile '{profile}').")
    click.echo("Key written by reference to ~/.onex/credentials.json (mode 0600).")


@cloud_group.command("status")
@click.option(
    "--onex-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ~/.onex root.",
)
def cloud_status(onex_home: Path | None) -> None:
    """Print which key is configured, without printing the key.

    This is the command a customer pastes into a support thread, so it prints
    identity and endpoints only.
    """
    try:
        credential = _store(onex_home).load()
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc
    click.echo(f"gateway base_url: {credential.base_url}")
    click.echo(f"profile:          {credential.profile}")
    click.echo("api_key:          stored by reference (not shown)")


@cloud_group.command("logout")
@click.option(
    "--onex-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ~/.onex root.",
)
def cloud_logout(onex_home: Path | None) -> None:
    """Remove the stored API key and the config block that references it."""
    try:
        _store(onex_home).clear()
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc
    click.echo("Removed the OmniNode API key from ~/.onex.")


# --------------------------------------------------------------------------
# delegate
# --------------------------------------------------------------------------


def _write_run_files(
    *,
    output_dir: Path,
    workflow_id: str,
    prompt: str,
    task_type: str,
    max_tokens: int | None,
    base_url: str,
    terminal_status: str,
    receipt: ModelCloudDelegationReceipt,
) -> dict[str, Path]:
    """Persist the run to disk and return what was written.

    Three files, split by what each is for:

    * ``result.txt`` — the generated output, verbatim, so it can be piped,
      diffed and edited like any other file. Written ONLY when there is
      content; an empty file would misrepresent a contentless run as an empty
      answer.
    * ``receipt.json`` — the server's receipt, unmodified, hashes included.
    * ``run.json`` — what was asked and where, so the receipt can be tied back
      to its request months later without the shell history.
    """
    run_dir = output_dir / workflow_id
    run_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}

    if receipt.result_content is not None:
        result_path = run_dir / "result.txt"
        result_path.write_text(receipt.result_content, encoding="utf-8")
        written["result"] = result_path

    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    written["receipt"] = receipt_path

    run_path = run_dir / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "workflow_type": CLOUD_DELEGATION_WORKFLOW_TYPE,
                "base_url": base_url,
                "prompt": prompt,
                "task_type": task_type,
                "max_tokens": max_tokens,
                "terminal_status": terminal_status,
                "model_used": receipt.terminal_model_used,
                "total_tokens": receipt.terminal_total_tokens,
                "latency_ms": receipt.terminal_latency_ms,
                "projection_row_hash": receipt.projection_row_hash,
                "terminal_event_hash": receipt.terminal_event_hash,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    written["run"] = run_path

    return written


@cloud_group.command("delegate")
@click.argument("prompt")
@click.option(
    "--task-type",
    "task_type",
    type=click.Choice(CLOUD_TASK_TYPE_CHOICES),
    required=True,
    help=(
        "Task classification the platform routes on. Required — the gateway "
        "contract requires it, and guessing it for a paying customer would "
        "silently change which model answers."
    ),
)
@click.option(
    "--max-tokens",
    "max_tokens",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Optional response budget. Omit to let the platform resolve it "
        "per-backend from its routing contract (no client-side default)."
    ),
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(path_type=Path),
    default=_DEFAULT_OUTPUT_DIR,
    show_default=True,
    help=(
        "Directory the run's files are written under, as <output-dir>/<workflow_id>/."
    ),
)
@click.option(
    "--base-url",
    default=None,
    envvar="ONEX_API_BASE_URL",
    help=(
        "Gateway origin. Defaults to the one stored by 'onex cloud login'. "
        "There is no built-in default."
    ),
)
@click.option(
    "--api-key-file",
    "api_key_file",
    type=click.Path(path_type=Path),
    default=None,
    envvar="ONEX_API_KEY_FILE",
    help=(
        "Path to a 0600 file holding the onxk_ key, for CI and non-interactive "
        "use. Omit to use the key stored by 'onex cloud login'. There is no "
        "option that takes the key as a value."
    ),
)
@click.option(
    "--onex-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ~/.onex root.",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=1),
    default=300,
    show_default=True,
    help="Total wall-clock budget for the delegation to reach a terminal state.",
)
@click.option(
    "--poll-interval",
    "poll_interval",
    type=click.FloatRange(min=0.0),
    default=2.0,
    show_default=True,
    help="Seconds between status polls.",
)
@click.option(
    "--runner-identity",
    "runner_identity",
    default=None,
    help=(
        "Identity stamped into the receipt's verifier field. Defaults to "
        "onex-cloud-delegate@<hostname>."
    ),
)
@click.pass_context
def cloud_delegate(
    ctx: click.Context,
    prompt: str,
    task_type: str,
    max_tokens: int | None,
    output_dir: Path,
    base_url: str | None,
    api_key_file: Path | None,
    onex_home: Path | None,
    timeout: int,
    poll_interval: float,
    runner_identity: str | None,
) -> None:
    """Delegate PROMPT to the platform, print the result, and save it locally.

    Submits the delegation, polls it to a terminal state, fetches the signed
    receipt, prints the generated output on stdout, and writes the run to
    ``<output-dir>/<workflow_id>/``. Exits non-zero when the run reaches a
    terminal ``failed`` state — with the receipt still saved, because a failed
    run's receipt is the evidence for why.

    \b
    Examples:
        onex cloud delegate "summarize this changelog" --task-type summarization
        onex cloud delegate "write a retry helper" --task-type code_generation --max-tokens 2048
    """
    resolved_base_url, api_key = _resolve_credential(
        onex_home=onex_home, base_url=base_url, api_key_file=api_key_file
    )
    identity = runner_identity or f"onex-cloud-delegate@{socket.gethostname()}"
    # One poll per interval inside the caller's declared budget. A zero
    # interval (tests, or a caller who wants a tight loop) still gets a bounded
    # number of attempts rather than an unbounded spin.
    attempts = max(1, int(timeout / poll_interval)) if poll_interval > 0 else 1

    transport_factory = _transport_factory_from_context(ctx)

    try:
        with transport_factory(
            base_url=resolved_base_url,
            api_key=api_key,
            timeout_seconds=float(timeout),
        ) as client:
            ack = client.submit(
                prompt=prompt, task_type=task_type, max_tokens=max_tokens
            )
            workflow_id = str(ack.workflow_id)
            click.echo(f"submitted delegation {workflow_id}", err=True)

            status = client.poll_until_terminal(
                workflow_id, attempts=attempts, interval_seconds=poll_interval
            )
            receipt = client.receipt(workflow_id, runner_identity=identity)
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc

    written = _write_run_files(
        output_dir=output_dir,
        workflow_id=workflow_id,
        prompt=prompt,
        task_type=task_type,
        max_tokens=max_tokens,
        base_url=resolved_base_url,
        terminal_status=status.status,
        receipt=receipt,
    )

    if receipt.result_content is not None:
        click.echo(receipt.result_content)

    click.echo("", err=True)
    for label, path in written.items():
        click.echo(f"{label:>8}: {path}", err=True)
    click.echo(
        f"   model: {receipt.terminal_model_used} "
        f"({receipt.terminal_total_tokens} tokens, "
        f"{receipt.terminal_latency_ms} ms)",
        err=True,
    )

    if status.status != "completed":
        # A terminal failure with no content is the quota-dead shape: the
        # submit was accepted, the runtime could not answer. Named, never
        # retried, and never reported as an empty success.
        detail = (
            "the runtime returned no content"
            if receipt.result_content is None
            else "the runtime returned partial content"
        )
        raise _fail(
            f"delegation {workflow_id} reached terminal status "
            f"'{status.status}' — {detail}. The receipt above records what the "
            f"platform did; it was NOT retried."
        )


# --------------------------------------------------------------------------
# receipt
# --------------------------------------------------------------------------


@cloud_group.command("receipt")
@click.argument("workflow_id", type=click.UUID)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(path_type=Path),
    default=_DEFAULT_OUTPUT_DIR,
    show_default=True,
    help="Directory the receipt is written under, as <output-dir>/<workflow_id>/.",
)
@click.option(
    "--base-url",
    default=None,
    envvar="ONEX_API_BASE_URL",
    help="Gateway origin. Defaults to the one stored by 'onex cloud login'.",
)
@click.option(
    "--api-key-file",
    "api_key_file",
    type=click.Path(path_type=Path),
    default=None,
    envvar="ONEX_API_KEY_FILE",
    help="Path to a 0600 file holding the onxk_ key, for CI and non-interactive use.",
)
@click.option(
    "--onex-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the ~/.onex root.",
)
@click.option(
    "--runner-identity",
    "runner_identity",
    default=None,
    help=(
        "Identity stamped into the receipt's verifier field. Defaults to "
        "onex-cloud-delegate@<hostname>."
    ),
)
@click.pass_context
def cloud_receipt(
    ctx: click.Context,
    workflow_id: uuid.UUID,
    output_dir: Path,
    base_url: str | None,
    api_key_file: Path | None,
    onex_home: Path | None,
    runner_identity: str | None,
) -> None:
    """Fetch and save the receipt for an existing delegation.

    This is how a run is retrieved after the fact — a delegation that outlived
    ``delegate``'s poll budget is still running on the platform, and its
    workflow id is all that is needed to collect it later. It is also the
    command for re-downloading a receipt whose local copy was lost.

    Writes ``receipt.json`` (and ``result.txt`` when the run produced content)
    under ``<output-dir>/<workflow_id>/`` and prints the generated output.

    WHY THE ARGUMENT IS TYPED ``click.UUID`` AND NOT LEFT A STRING
        This id is not just looked up — it is interpolated into the gateway URL
        path by ``TransportCloudDelegation.receipt`` and joined onto the
        operator's ``--output-dir`` here, where the result is ``mkdir``'d and
        written to. An unvalidated string is therefore URL path structure and a
        filesystem path at once, and ``../`` in it escapes the directory the
        operator named. Typing it closes both, in the one place both start.

        It also makes the CLI agree with the contract: all three models in
        ``omnimarket.cloud.model_cloud_delegation`` already type this field
        ``uuid.UUID``, so the string form was the CLI widening its own models.

        Normalising through ``str(uuid.UUID)`` is the half that matters as much
        as rejecting: a braced or upper-case spelling is legal and denotes the
        same workflow, so it must reach the gateway and the disk in the single
        canonical hyphenated lower-case form rather than minting a second
        directory for the same run.
    """
    canonical_workflow_id = str(workflow_id)
    resolved_base_url, api_key = _resolve_credential(
        onex_home=onex_home, base_url=base_url, api_key_file=api_key_file
    )
    identity = runner_identity or f"onex-cloud-delegate@{socket.gethostname()}"
    transport_factory = _transport_factory_from_context(ctx)

    try:
        with transport_factory(base_url=resolved_base_url, api_key=api_key) as client:
            receipt = client.receipt(canonical_workflow_id, runner_identity=identity)
    except ModelOnexError as exc:
        raise _fail(str(exc)) from exc

    run_dir = output_dir / canonical_workflow_id
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = run_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    if receipt.result_content is not None:
        result_path = run_dir / "result.txt"
        result_path.write_text(receipt.result_content, encoding="utf-8")
        click.echo(receipt.result_content)
        click.echo(f"  result: {result_path}", err=True)

    click.echo(f" receipt: {receipt_path}", err=True)
    click.echo(
        f"   model: {receipt.terminal_model_used} "
        f"({receipt.terminal_total_tokens} tokens, "
        f"{receipt.terminal_latency_ms} ms)",
        err=True,
    )
