# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared host bring-up enums (OMN-21883): the check kinds and actions of a target row.

Promoted here so the orchestrator generator does not import a sibling node's models.
"""

from __future__ import annotations

from enum import StrEnum


class EnumBringupCheckKind(StrEnum):
    """The read-only already-satisfied probe a target row declares.

    The set is closed and covers the 57 targets of the host bring-up graph: each
    value names a family of probe, and the symbolic ``subject`` of the row picks
    the one the host profile resolves.
    """

    DECLARATION_VALID = "declaration_valid"
    DECLARATION_DRIFT = "declaration_drift"
    REVISION_CURRENT = "revision_current"
    PERSON_CONFIRMED = "person_confirmed"
    MANAGEMENT_READ = "management_read"
    TRANSPORT_ANSWERS = "transport_answers"
    HOST_STATE = "host_state"
    FILE_STATE = "file_state"
    PACKAGE_INSTALLED = "package_installed"
    SERVICE_ACTIVE = "service_active"
    GROUP_MEMBER = "group_member"
    UNIT_STATUS = "unit_status"
    CHECKOUT_STATE = "checkout_state"
    CREDENTIAL_RESOLVES = "credential_resolves"
    MEASUREMENT_CURRENT = "measurement_current"
    TABLE_ROW = "table_row"
    RUNBOOK_VERIFIED = "runbook_verified"
    FLEET_HEALTHY = "fleet_healthy"
    AGGREGATE = "aggregate"


class EnumBringupAction(StrEnum):
    """What the bring-up does when a target is not already satisfied.

    The eight host primitives of the plan (``FILE_CONTENT`` to
    ``GUARDED_SSHD_DROPIN``) and ``HOST_UNIT_INSTALL`` are the section 4.1 kinds;
    the rest name the other ways a target is met: a person step, a stop, a data
    pull request, or a call to a broker, the store or a repository.
    """

    PERSON_STEP = "person_step"
    STOP = "stop"
    NO_ACTION = "no_action"
    LOCAL_INCLUDE = "local_include"
    DATA_PR = "data_pr"
    ADOPT_MERGED = "adopt_merged"
    MANAGEMENT_WRITE = "management_write"
    FILE_CONTENT = "file_content"
    DIRECTORY = "directory"
    APT_SOURCE = "apt_source"
    APT_PACKAGES = "apt_packages"
    GROUP_MEMBER = "group_member"
    SERVICE_ENABLED = "service_enabled"
    LINE_IN_FILE = "line_in_file"
    GUARDED_SSHD_DROPIN = "guarded_sshd_dropin"
    HOST_UNIT_INSTALL = "host_unit_install"
    TOOL_INSTALL = "tool_install"
    CLONE_REPOSITORY = "clone_repository"
    SYNC_ENVIRONMENT = "sync_environment"
    INSTALL_PLUGIN_TREE = "install_plugin_tree"
    TAILNET_LOGIN = "tailnet_login"
    STORE_CREDENTIAL = "store_credential"
    MINT_LOGIN = "mint_login"
    APPLY_BROKER_GRANTS = "apply_broker_grants"
    REFILL_CLAUDE_POOL = "refill_claude_pool"
    ROLE_ACTION = "role_action"
    RUN_WORKLOAD = "run_workload"
    PREPARE_VOLUME = "prepare_volume"
    MOVE_DATA_ROOTS = "move_data_roots"
    RERUN_TARGETS = "rerun_targets"
    DEPLOY_RUNNERS = "deploy_runners"
