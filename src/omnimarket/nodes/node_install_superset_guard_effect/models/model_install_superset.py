# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify that an install contains the live commits or declares each missing commit as an intentional drop."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelCarriedCommit(BaseModel):
    """One commit a branch install carries beyond main, and the lane that first carried it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    commit: str
    patch_id: str
    lane: str
    subject: str = ""


class ModelDrop(BaseModel):
    """A commit of the live install that the new install leaves out on purpose, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    commit: str
    reason: str


class ModelSupersetRequest(BaseModel):
    """Is every commit of the live install also in the new one?

    ``repo`` is a git work tree holding both commits. ``live_commit`` is the commit the live tree
    was installed from ("" when nothing is installed); ``live_carried`` is what the live record
    says it carried, for the lane of a missing commit. ``history`` is the text of
    INSTALLED-FROM-BRANCHES.txt, read for the lane of a commit no typed record names. ``drops`` are
    the commits left out on purpose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    repo: Path
    new_commit: str
    lane: str
    live_commit: str = ""
    live_carried: tuple[ModelCarriedCommit, ...] = ()
    drops: tuple[ModelDrop, ...] = ()
    main_ref: str = "origin/main"
    history: str = ""


class ModelMissingCommit(BaseModel):
    """A commit of the live install the new one does not hold, and the lane that installed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    commit: str
    lane: str
    subject: str


class ModelSupersetResult(BaseModel):
    """``contained`` when nothing is missing, ``dropped`` when only declared drops are, else refused.

    ``held`` names each live commit the new tree holds and how (ancestor, patch-id or diff).
    ``carried`` is what the new install carries beyond main, each with its lane, for the next
    record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    verdict: str
    new_commit: str
    live_commit: str
    missing: tuple[ModelMissingCommit, ...] = ()
    dropped: tuple[ModelMissingCommit, ...] = ()
    held: tuple[tuple[str, str], ...] = ()
    carried: tuple[ModelCarriedCommit, ...] = ()
    unused_drops: tuple[str, ...] = ()
