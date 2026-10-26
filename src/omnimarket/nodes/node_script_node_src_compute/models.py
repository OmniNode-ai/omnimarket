"""Typed input and output of a plugin script's node source resolution (OMN-17427).

Frozen dataclasses, not pydantic: the plugin's scripts import the handler by path and run where only the standard
library is installed.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelScriptNodeSrcRequest:
    script: str  # path of the plugin script whose node imports are read
    candidates: tuple[str, ...]  # node source directories, best first


@dataclass(frozen=True)
class ModelScriptNodeSrcResult:
    src: (
        str | None
    )  # the first candidate that defines everything the script takes from a node package; None when none does
    message: str  # when src is None, what each candidate lacks and the fix
