# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay main load check node (OMN-23963): golden loads, the incident control, the error chain, the resolver, and the CLI.

The incident the control replays: omnimarket made the key ``limits`` REQUIRED in ModelFactoryFloorPublishOverlay while the
overlays repository's main document had no ``limits``; the publisher then refused every cycle. The node must refuse in that
case, and must load the same document when the key is optional.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_main_load_check import (
    HandlerOverlayMainLoadCheck,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
    ModelImportFailedError,
    ModelSearchRoot,
    ModelUnresolvedError,
    _class_index,
    default_search_roots,
    resolve_model,
    resolve_spec,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models import (
    EnumOverlayLoadFindingCode,
    ModelOverlayMainLoadCommand,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    _overlay,
)

pytestmark = pytest.mark.unit

REAL_SPEC = "omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish:ModelFactoryFloorPublishOverlay"


class ModelKeyOptional(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    limits: dict[str, int] | None = None


class ModelKeyRequired(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    limits: dict[str, int]


def _write_root(tmp_path: Path, documents: dict[str, dict]) -> Path:
    """Write <tmp_path>/overlays/<node>/overlay.yaml for each node and return the overlays root."""
    root = tmp_path / "overlays"
    for node, document in documents.items():
        node_dir = root / node
        node_dir.mkdir(parents=True)
        (node_dir / "overlay.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return root


def _write_pins(tmp_path: Path, lines: list[str]) -> Path:
    """Write a pins file: a ``#`` comment line first, then the given lines."""
    pins = tmp_path / "pins.txt"
    pins.write_text("# pins\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return pins


def _handler(
    models: dict[str, type[BaseModel]] | None = None,
    floor: Any = None,
) -> HandlerOverlayMainLoadCheck:
    """Build a handler whose resolve looks the spec up in ``models`` and whose floor_check is ``floor``."""
    models = models or {}

    def _resolve(spec: str) -> str:
        if spec not in models:
            raise ModelUnresolvedError(f"{spec} exists nowhere in this tree")
        return f"{models[spec].__module__}:{models[spec].__name__}"

    return HandlerOverlayMainLoadCheck(
        resolve=_resolve, floor_check=floor or (lambda _root: [])
    )


def _run(
    tmp_path: Path,
    documents: dict[str, dict],
    pins: list[str],
    models: dict[str, type[BaseModel]] | None = None,
    floor: Any = None,
):
    root = _write_root(tmp_path, documents)
    pins_file = _write_pins(tmp_path, pins)
    handler = _handler(models, floor)
    return handler.handle(
        ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins_file))
    )


class TestGolden:
    def test_golden_real_publisher_document_loads(self, tmp_path: Path) -> None:
        """The real publisher document loads through the real model and the real resolver."""
        document = _overlay(tmp_path).model_dump(mode="json")
        root = _write_root(tmp_path, {"node_factory_floor_publish_effect": document})
        pins = _write_pins(
            tmp_path, [f"node_factory_floor_publish_effect abc123 {REAL_SPEC}"]
        )
        handler = HandlerOverlayMainLoadCheck(
            resolve=lambda spec: resolve_spec(spec, default_search_roots()),
            floor_check=lambda _root: [],
        )
        result = handler.handle(
            ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
        )
        assert result.ok
        assert result.loaded == ("node_factory_floor_publish_effect",)
        assert result.findings == ()

    def test_golden_two_nodes_load_in_pin_order(self, tmp_path: Path) -> None:
        """Two valid nodes load in pin order and the floor stub appends its ok lines."""
        result = _run(
            tmp_path,
            {"node_a": {"name": "a"}, "node_b": {"name": "b"}},
            ["node_a r1 zz.mod:SpecA", "node_b r2 zz.mod:SpecB"],
            models={"zz.mod:SpecA": ModelKeyOptional, "zz.mod:SpecB": ModelKeyOptional},
            floor=lambda _root: ["ok floor_a"],
        )
        assert result.ok
        assert result.loaded == ("node_a", "node_b", "floor_a")
        assert result.findings == ()


class TestFailingControl:
    def test_control_required_key_missing_from_document_refuses(
        self, tmp_path: Path
    ) -> None:
        """The incident: a required key missing from the document refuses with DOCUMENT_INVALID naming the key."""
        result = _run(
            tmp_path,
            {"node_x": {"name": "x"}},
            ["node_x r1 zz.mod:SpecRequired"],
            models={"zz.mod:SpecRequired": ModelKeyRequired},
        )
        assert not result.ok
        assert len(result.findings) == 1
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.DOCUMENT_INVALID
        assert finding.node == "node_x"
        assert "limits" in finding.detail

    def test_control_same_document_passes_when_key_optional(
        self, tmp_path: Path
    ) -> None:
        """The control's other half: the same document loads when the key is optional."""
        result = _run(
            tmp_path,
            {"node_x": {"name": "x"}},
            ["node_x r1 zz.mod:SpecOptional"],
            models={"zz.mod:SpecOptional": ModelKeyOptional},
        )
        assert result.ok
        assert result.findings == ()

    def test_control_real_publisher_document_without_limits_refuses(
        self, tmp_path: Path
    ) -> None:
        """The real publisher document with limits deleted refuses through the real resolver."""
        document = _overlay(tmp_path).model_dump(mode="json")
        document.pop("limits")
        root = _write_root(tmp_path, {"node_factory_floor_publish_effect": document})
        pins = _write_pins(
            tmp_path, [f"node_factory_floor_publish_effect abc123 {REAL_SPEC}"]
        )
        handler = HandlerOverlayMainLoadCheck(
            resolve=lambda spec: resolve_spec(spec, default_search_roots()),
            floor_check=lambda _root: [],
        )
        result = handler.handle(
            ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
        )
        assert not result.ok
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.DOCUMENT_INVALID
        assert "limits" in finding.detail


class TestErrorChain:
    def test_malformed_pin_line_refuses_the_whole_pins_file(
        self, tmp_path: Path
    ) -> None:
        """A two-field pin line refuses the pins file (the drift node's parser), naming the line, and loads nothing."""
        result = _run(
            tmp_path,
            {"node_b": {"name": "b"}},
            ["bad_pin two", "node_b r2 zz.mod:SpecB"],
            models={"zz.mod:SpecB": ModelKeyOptional},
        )
        assert not result.ok
        assert result.loaded == ()
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.PIN_MALFORMED
        assert finding.node == "pins"
        assert "line 2" in finding.detail

    def test_pin_whose_document_file_is_absent_is_noted(self, tmp_path: Path) -> None:
        """A pin whose document file is absent is DOCUMENT_ABSENT, noted, and ok stays True."""
        result = _run(
            tmp_path,
            {},
            ["node_missing r1 zz.mod:SpecA"],
            models={"zz.mod:SpecA": ModelKeyOptional},
        )
        assert result.ok
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.DOCUMENT_ABSENT

    def test_resolver_raising_model_unresolved_is_noted(self, tmp_path: Path) -> None:
        """A resolver that raises ModelUnresolvedError is MODEL_ABSENT, noted, and ok stays True."""
        result = _run(
            tmp_path,
            {"node_x": {"name": "x"}},
            ["node_x r1 zz.mod:SpecMissing"],
            models={},
        )
        assert result.ok
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.MODEL_ABSENT

    def test_resolver_raising_model_import_failed_refuses(self, tmp_path: Path) -> None:
        """A resolver that raises ModelImportFailedError refuses with that detail."""

        def _resolve(spec: str) -> str:
            raise ModelImportFailedError("x")

        handler = HandlerOverlayMainLoadCheck(
            resolve=_resolve, floor_check=lambda _root: []
        )
        root = _write_root(tmp_path, {"node_x": {"name": "x"}})
        pins = _write_pins(tmp_path, ["node_x r1 zz.mod:SpecX"])
        result = handler.handle(
            ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
        )
        assert not result.ok
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.MODEL_IMPORT_FAILED
        assert finding.detail == "x"

    def test_document_with_unknown_extra_key_refuses(self, tmp_path: Path) -> None:
        """A document with an unknown extra key (extra forbid) refuses with DOCUMENT_INVALID."""
        result = _run(
            tmp_path,
            {"node_x": {"name": "x", "unknown": 1}},
            ["node_x r1 zz.mod:SpecOptional"],
            models={"zz.mod:SpecOptional": ModelKeyOptional},
        )
        assert not result.ok
        [finding] = result.findings
        assert finding.code == EnumOverlayLoadFindingCode.DOCUMENT_INVALID

    def test_floor_check_raising_value_error_refuses(self, tmp_path: Path) -> None:
        """A floor_check that raises ValueError refuses with node floor_overlays and the detail."""

        def _floor(root: Path) -> list[str]:
            raise ValueError("boom")

        result = _run(
            tmp_path,
            {"node_a": {"name": "a"}},
            ["node_a r1 zz.mod:SpecA"],
            models={"zz.mod:SpecA": ModelKeyOptional},
            floor=_floor,
        )
        assert not result.ok
        [finding] = result.findings
        assert finding.node == "floor_overlays"
        assert finding.code == EnumOverlayLoadFindingCode.DOCUMENT_INVALID
        assert "boom" in finding.detail

    def test_missing_pins_file_raises_oserror(self, tmp_path: Path) -> None:
        """A missing pins file makes the handler raise OSError."""
        root = _write_root(tmp_path, {})
        handler = _handler(models={"zz.mod:SpecA": ModelKeyOptional})
        with pytest.raises(FileNotFoundError):
            handler.handle(
                ModelOverlayMainLoadCommand(
                    overlays_root=str(root),
                    pins_file=str(tmp_path / "nope.txt"),
                )
            )

    def test_comment_and_blank_lines_in_pins_are_ignored(self, tmp_path: Path) -> None:
        """A pins file with only comments and blanks gives ok True and no loaded nodes."""
        root = _write_root(tmp_path, {})
        pins = tmp_path / "pins.txt"
        pins.write_text("# only a comment\n\n# another\n", encoding="utf-8")
        handler = _handler(models={})
        result = handler.handle(
            ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
        )
        assert result.ok
        assert result.loaded == ()


class TestResolver:
    def test_a_real_spec_resolves_to_the_class(self) -> None:
        """A real spec resolves to the model class."""
        cls = resolve_model(REAL_SPEC, default_search_roots())
        assert cls is ModelFactoryFloorPublishOverlay

    def test_a_spec_with_no_colon_raises_model_import_failed(self) -> None:
        """A spec with no colon raises ModelImportFailedError."""
        with pytest.raises(ModelImportFailedError):
            resolve_model("no_colon_here", default_search_roots())

    def test_a_moved_class_resolves_to_its_new_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A class that moved to a new module resolves through the search roots."""
        _class_index.cache_clear()
        monkeypatch.syspath_prepend(tmp_path)
        pkg = tmp_path / "zzsearch"
        inner = pkg / "inner"
        inner.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (inner / "__init__.py").write_text("", encoding="utf-8")
        (inner / "model_moved.py").write_text(
            "from pydantic import BaseModel\n\n\nclass ModelMoved(BaseModel):\n    pass\n",
            encoding="utf-8",
        )
        roots = (ModelSearchRoot(package="zzsearch", directory=tmp_path / "zzsearch"),)
        cls = resolve_model("zzsearch.old.model_moved:ModelMoved", roots)
        assert cls.__name__ == "ModelMoved"

    def test_a_class_defined_nowhere_raises_model_unresolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A class defined nowhere raises ModelUnresolvedError."""
        _class_index.cache_clear()
        monkeypatch.syspath_prepend(tmp_path)
        pkg = tmp_path / "zzsearch"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        roots = (ModelSearchRoot(package="zzsearch", directory=tmp_path / "zzsearch"),)
        with pytest.raises(ModelUnresolvedError):
            resolve_model("zzsearch.nowhere:ModelNowhere", roots)

    def test_a_class_in_two_files_raises_model_import_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same class name in two files raises ModelImportFailedError naming both modules."""
        _class_index.cache_clear()
        monkeypatch.syspath_prepend(tmp_path)
        pkg = tmp_path / "zzsearch"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "a.py").write_text(
            "from pydantic import BaseModel\n\n\nclass ModelDup(BaseModel):\n    pass\n",
            encoding="utf-8",
        )
        (pkg / "b.py").write_text(
            "from pydantic import BaseModel\n\n\nclass ModelDup(BaseModel):\n    pass\n",
            encoding="utf-8",
        )
        roots = (ModelSearchRoot(package="zzsearch", directory=tmp_path / "zzsearch"),)
        with pytest.raises(ModelImportFailedError) as excinfo:
            resolve_model("zzsearch.a:ModelDup", roots)
        assert "zzsearch.a" in str(excinfo.value)
        assert "zzsearch.b" in str(excinfo.value)

    def test_a_model_module_importing_a_missing_module_raises_model_import_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model module that imports a missing module is an import failure, not a moved class."""
        _class_index.cache_clear()
        monkeypatch.syspath_prepend(tmp_path)
        pkg = tmp_path / "zzsearch"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "broken.py").write_text(
            "import zzsearch.does_not_exist\n\n\nclass ModelBroken(BaseModel):\n    pass\n",
            encoding="utf-8",
        )
        roots = (ModelSearchRoot(package="zzsearch", directory=tmp_path / "zzsearch"),)
        with pytest.raises(ModelImportFailedError):
            resolve_model("zzsearch.broken:ModelBroken", roots)


class TestCli:
    def test_cli_valid_document_exits_zero(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid real publisher document exits 0 with the summary line."""
        import omnimarket.nodes.node_overlay_main_load_check_effect.runtime_overlay_main_load_check as runtime

        monkeypatch.setattr(runtime, "check_overlays", lambda _root: [])
        document = _overlay(tmp_path).model_dump(mode="json")
        root = _write_root(tmp_path, {"node_factory_floor_publish_effect": document})
        pins = _write_pins(
            tmp_path, [f"node_factory_floor_publish_effect abc123 {REAL_SPEC}"]
        )
        exit_code = runtime.main(
            ["--overlays-root", str(root), "--pins-file", str(pins)]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out.splitlines()[-1].startswith(
            "overlay-main-load: loaded=1 refused=0 noted=0"
        )

    def test_cli_document_without_limits_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A document with limits removed exits 1 with the refused line."""
        import omnimarket.nodes.node_overlay_main_load_check_effect.runtime_overlay_main_load_check as runtime

        monkeypatch.setattr(runtime, "check_overlays", lambda _root: [])
        document = _overlay(tmp_path).model_dump(mode="json")
        document.pop("limits")
        root = _write_root(tmp_path, {"node_factory_floor_publish_effect": document})
        pins = _write_pins(
            tmp_path, [f"node_factory_floor_publish_effect abc123 {REAL_SPEC}"]
        )
        exit_code = runtime.main(
            ["--overlays-root", str(root), "--pins-file", str(pins)]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert (
            "refused node_factory_floor_publish_effect document_invalid:"
            in captured.out
        )

    def test_cli_missing_pins_file_exits_three(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing pins file exits 3 with INDETERMINATE on stderr."""
        import omnimarket.nodes.node_overlay_main_load_check_effect.runtime_overlay_main_load_check as runtime

        monkeypatch.setattr(runtime, "check_overlays", lambda _root: [])
        root = _write_root(tmp_path, {})
        exit_code = runtime.main(
            ["--overlays-root", str(root), "--pins-file", str(tmp_path / "nope.txt")]
        )
        captured = capsys.readouterr()
        assert exit_code == 3
        assert "INDETERMINATE" in captured.err
