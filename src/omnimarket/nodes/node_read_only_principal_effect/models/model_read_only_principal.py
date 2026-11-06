# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read-only principal's overlay, the ssh gate decision and the probe receipt (step 0, OMN-22478).

The principal holds three credentials: a forced-command ssh key, a GitHub token that can only
read, and a ledger writer that cannot append. Every value that names one of them (the commands
the ssh key may run, the argv of each probe attempt, the words a refusal carries) comes from the
``node_read_only_principal_effect`` overlay; nothing here is a deployment fact.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OVERLAY_ID = "node_read_only_principal_effect"
OVERLAY_FILE_ENV = "ONEX_READ_ONLY_PRINCIPAL_OVERLAY_FILE"


class EnumChannel(StrEnum):
    """The three places a mutating step must be refused by the credential."""

    SSH = "ssh"
    GITHUB = "github"
    LEDGER = "ledger"


class EnumGateDecision(StrEnum):
    """What the forced-command gate does with the command a client asked for."""

    ALLOW = "allow"
    REFUSE = "refuse"


class EnumGateReason(StrEnum):
    """Why the gate refused, or ``allowed``. Never carries the command text."""

    ALLOWED = "allowed"
    EMPTY = "empty-command"
    UNPARSEABLE = "unparseable-command"
    NOT_ON_ALLOWLIST = "not-on-allowlist"
    FLAG_NOT_ALLOWED = "flag-not-allowed"
    TOO_MANY_OPERANDS = "too-many-operands"
    OPERAND_SHAPE = "operand-shape"
    OPERAND_REFUSED = "operand-refused"


class EnumAttemptOutcome(StrEnum):
    """What one probe attempt showed."""

    READ_OK = "read-ok"
    READ_FAILED = "read-failed"
    MUTATION_REFUSED = "mutation-refused"
    MUTATION_ACCEPTED = "mutation-accepted"
    MUTATION_INDETERMINATE = "mutation-indeterminate"


class ModelAllowedCommand(BaseModel):
    """One read command the forced-command key may run: a fixed program and its fixed leading words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv_prefix: tuple[str, ...] = Field(min_length=1)
    allowed_flags: tuple[str, ...] = ()
    max_operands: int = Field(default=0, ge=0, le=16)
    operand_pattern: str = r"[A-Za-z0-9_][A-Za-z0-9_.:/@=-]*"

    @field_validator("argv_prefix")
    @classmethod
    def _prefix_has_no_flag_or_space(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for word in value:
            if not word or any(ch.isspace() for ch in word):
                raise ValueError("argv_prefix words must be non-empty with no space")
        return value

    @field_validator("operand_pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ModelSshGate(BaseModel):
    """The ssh credential: the commands its forced command runs and the key line it installs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_commands: tuple[ModelAllowedCommand, ...] = Field(min_length=1)
    refused_operand_patterns: tuple[str, ...] = ()
    gate_command: str = Field(min_length=1)
    key_path: str = Field(min_length=1)
    key_comment: str = Field(min_length=1)

    @field_validator("refused_operand_patterns")
    @classmethod
    def _refused_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            re.compile(pattern)
        return value

    @field_validator("gate_command")
    @classmethod
    def _gate_command_has_no_quote(cls, value: str) -> str:
        if '"' in value or "\n" in value or "\r" in value:
            raise ValueError("gate_command must not hold a double quote or a newline")
        return value

    @field_validator("key_comment")
    @classmethod
    def _comment_is_one_word(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", value):
            raise ValueError("key_comment must be one word of letters, digits, _ . @ -")
        return value


class ModelProbeAttempt(BaseModel):
    """One command run as the principal. ``pass_env`` names the variables the child may see."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    pass_env: tuple[str, ...] = ()
    timeout_s: int = Field(default=30, ge=1, le=300)


class ModelChannelProbe(BaseModel):
    """One channel: a read that must work, a mutation that must be refused, and what a refusal says."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: EnumChannel
    read: ModelProbeAttempt
    mutate: ModelProbeAttempt
    refusal_markers: tuple[str, ...] = Field(min_length=1)

    @field_validator("refusal_markers")
    @classmethod
    def _markers_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not marker.strip() for marker in value):
            raise ValueError("a refusal marker must not be blank")
        return value


class ModelReadOnlyPrincipalOverlay(BaseModel):
    """The ``node_read_only_principal_effect`` overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(min_length=1)
    ssh: ModelSshGate
    probes: tuple[ModelChannelProbe, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _every_channel_is_probed_once(self) -> ModelReadOnlyPrincipalOverlay:
        seen = [probe.channel for probe in self.probes]
        missing = sorted(set(EnumChannel) - set(seen))
        if missing or len(set(seen)) != len(seen):
            raise ValueError(
                "probes must name each of ssh, github and ledger exactly once"
            )
        return self


class ModelGateDecideRequest(BaseModel):
    """Decide one ssh command against the overlay's allowlist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    overlay: ModelReadOnlyPrincipalOverlay | None = None


class ModelGateDecision(BaseModel):
    """The gate's answer. ``argv`` is set only when the decision is ``allow``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: EnumGateDecision
    reason: EnumGateReason
    argv: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _argv_only_on_allow(self) -> ModelGateDecision:
        if (self.decision == EnumGateDecision.ALLOW) != bool(self.argv):
            raise ValueError("argv is set on allow and only on allow")
        if (self.decision == EnumGateDecision.ALLOW) != (
            self.reason == EnumGateReason.ALLOWED
        ):
            raise ValueError("reason allowed goes with allow and only with allow")
        return self


class ModelChannelReceipt(BaseModel):
    """One channel's read and mutation outcomes. No output text, no argv, no environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: EnumChannel
    read: EnumAttemptOutcome
    mutate: EnumAttemptOutcome
    passed: bool


class ModelProbeRequest(BaseModel):
    """Probe the principal. An explicit overlay wins over the overlay file.

    ``channels`` narrows the probe while the credentials are set one at a time; empty probes all
    three. A narrowed probe can pass but never reads as the whole acceptance (see ``complete``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelReadOnlyPrincipalOverlay | None = None
    channels: tuple[EnumChannel, ...] = ()

    @field_validator("channels")
    @classmethod
    def _channels_unique(
        cls, value: tuple[EnumChannel, ...]
    ) -> tuple[EnumChannel, ...]:
        if len(set(value)) != len(value):
            raise ValueError("a channel is named once")
        return value


class ModelProbeResult(BaseModel):
    """The verdict. ``passed`` is true when every probed channel read and refused its mutation.

    ``complete`` is true only when all three channels were probed, and only a complete pass is the
    acceptance of section 11: a narrowed pass reads PASS-PARTIAL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str
    passed: bool
    complete: bool
    receipts: tuple[ModelChannelReceipt, ...]

    @property
    def verdict(self) -> str:
        if not self.passed:
            return "FAIL"
        return "PASS" if self.complete else "PASS-PARTIAL"

    def lines(self) -> tuple[str, ...]:
        rows = tuple(
            f"{r.channel.value} read={r.read.value} mutate={r.mutate.value} "
            f"{'ok' if r.passed else 'FAIL'}"
            for r in self.receipts
        )
        return (*rows, f"principal={self.principal} verdict={self.verdict}")


class EnumKeyInstallStatus(StrEnum):
    """The outcome of one pool host in the key install."""

    OK = "ok"
    PLANNED = "planned"
    REFUSED = "refused"
    SKIPPED = "skipped"


class ModelKeyInstallRequest(BaseModel):
    """Install the principal's forced-command key on the pool hosts (the operator's one command)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelReadOnlyPrincipalOverlay | None = None
    hosts: tuple[str, ...] = ()
    dry_run: bool = False
    local_host: str | None = None
    timeout_s: int = Field(default=30, ge=1)


class ModelKeyInstallHost(BaseModel):
    """What one pool host took. No key text, no address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: EnumKeyInstallStatus
    detail: str = ""


class ModelKeyInstallResult(BaseModel):
    """The key's fate, each host in table order, the exit code and any refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool
    key_created: bool
    key_present: bool
    hosts: tuple[ModelKeyInstallHost, ...] = ()
    exit_code: int
    refusal: str | None = None

    def lines(self) -> tuple[str, ...]:
        if self.key_created:
            key = "key would-create" if self.dry_run else "key created"
        else:
            key = "key present" if self.key_present else "key absent"
        rows = tuple(
            f"{h.host} {h.status.value}" + (f" {h.detail}" if h.detail else "")
            for h in self.hosts
        )
        return (key, *rows)
