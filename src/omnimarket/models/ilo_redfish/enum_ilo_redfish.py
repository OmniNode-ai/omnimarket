# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Vocabulary of the iLO 4 Redfish effect (OMN-21835).

Every verb the node offers is a member of :class:`EnumIloVerb`. There is no storage, account, license,
firmware, BIOS, network or log-clearing verb: those are not disabled, they do not exist.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumIloGeneration(StrEnum):
    """The ProLiant generation an iLO 4 sits in, as the overlay declares it."""

    GEN8 = "gen8"
    GEN9 = "gen9"


@unique
class EnumIloVerb(StrEnum):
    """What a request asks for. The first six change nothing on the server."""

    DISCOVER = "discover"
    IDENTITY = "identity"
    INVENTORY = "inventory"
    HEALTH = "health"
    LOGS = "logs"
    POWER_STATUS = "power-status"
    POWER = "power"
    VIRTUAL_MEDIA = "virtual-media"
    BOOT_ONCE = "boot-once"


READ_VERBS: frozenset[EnumIloVerb] = frozenset(
    {
        EnumIloVerb.DISCOVER,
        EnumIloVerb.IDENTITY,
        EnumIloVerb.INVENTORY,
        EnumIloVerb.HEALTH,
        EnumIloVerb.LOGS,
        EnumIloVerb.POWER_STATUS,
    }
)
WRITE_VERBS: frozenset[EnumIloVerb] = frozenset(
    {EnumIloVerb.POWER, EnumIloVerb.VIRTUAL_MEDIA, EnumIloVerb.BOOT_ONCE}
)


@unique
class EnumIloPowerAction(StrEnum):
    """The ResetType values offered. Nmi is deliberately absent."""

    ON = "on"
    FORCE_OFF = "force_off"
    FORCE_RESTART = "force_restart"
    POWER_BUTTON = "power_button"


@unique
class EnumIloMediaAction(StrEnum):
    INSERT = "insert"
    EJECT = "eject"


@unique
class EnumIloBootTarget(StrEnum):
    """Boot override targets offered, a subset of the documented BootSourceOverrideTarget values."""

    PXE = "pxe"
    CD = "cd"
    HDD = "hdd"
    USB = "usb"
    BIOS_SETUP = "bios_setup"
    UEFI_SHELL = "uefi_shell"


@unique
class EnumIloLogKind(StrEnum):
    """IML is the system's Integrated Management Log, IEL the iLO event log."""

    IML = "iml"
    IEL = "iel"


@unique
class EnumIloOutcome(StrEnum):
    """How a request ended."""

    OK = "ok"
    # A write found the server already in the requested state and sent nothing.
    NOOP = "noop"
    # A write was sent and the state read back did not confirm it in time.
    UNVERIFIED = "unverified"
    # The node refused before sending any write. See ``refusal``.
    REFUSED = "refused"
    # iLO answered 4xx: the request was not executed.
    FAILED = "failed"
    # The journal holds an earlier attempt for this key: nothing was sent again.
    ALREADY_ATTEMPTED = "already_attempted"
    # The write was sent and its outcome is unknown (connection lost, timeout, 5xx).
    UNKNOWN = "unknown"


@unique
class EnumIloRefusal(StrEnum):
    """Why a request was refused or failed. Codes only, never message text from the device."""

    HOST_NOT_CONFIGURED = "host_not_configured"
    HOST_NOT_LIVE = "host_not_live"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_SOURCE_NOT_STORE = "credential_source_not_store"
    TLS_FAILED = "tls_failed"
    CONNECT_FAILED = "connect_failed"
    AUTH_FAILED = "auth_failed"
    INSUFFICIENT_PRIVILEGE = "insufficient_privilege"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_INVALID = "approval_invalid"
    IDENTITY_UNCONFIRMED = "identity_unconfirmed"
    IDENTITY_MISMATCH = "identity_mismatch"
    ENDPOINT_MISSING = "endpoint_missing"
    AMBIGUOUS_ENDPOINT = "ambiguous_endpoint"
    UNSUPPORTED_VALUE = "unsupported_value"
    PRECONDITION_FAILED = "precondition_failed"
    MEDIA_URL_NOT_ALLOWED = "media_url_not_allowed"
    DEVICE_ERROR = "device_error"


@unique
class EnumIloJournalStatus(StrEnum):
    """Steps of one write in the per-host action journal."""

    INTENT = "intent"
    SENT = "sent"
    REJECTED = "rejected"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"
