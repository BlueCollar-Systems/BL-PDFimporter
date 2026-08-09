from __future__ import annotations

import ast
import importlib
import socket
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest

from pdf_vector_importer import dependency_manager


REPO_ROOT = Path(__file__).resolve().parents[1]
_FRESH_HOST_MODULES = (
    "pdf_vector_importer.operators",
    "pdf_vector_importer.preferences",
)


class _HostRegistry:
    def __init__(self) -> None:
        self.registered: list[type] = []
        self.menu_entries: list[object] = []
        self.scene_mutations: list[str] = []


class _MutationCollection:
    def __init__(self, registry: _HostRegistry, name: str) -> None:
        self._registry = registry
        self._name = name

    def new(self, *_args, **_kwargs):
        self._registry.scene_mutations.append(f"{self._name}.new")
        raise AssertionError("dependency failure must precede scene creation")

    def remove(self, *_args, **_kwargs):
        self._registry.scene_mutations.append(f"{self._name}.remove")
        raise AssertionError("dependency failure must precede scene removal")


class _MutationChildren:
    def __init__(self, registry: _HostRegistry) -> None:
        self._registry = registry

    def link(self, *_args, **_kwargs):
        self._registry.scene_mutations.append("scene.children.link")
        raise AssertionError("dependency failure must precede scene linking")


@pytest.fixture
def fake_blender_modules(monkeypatch: pytest.MonkeyPatch):
    for name in _FRESH_HOST_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)

    registry = _HostRegistry()

    class Operator:
        pass

    class AddonPreferences:
        pass

    class ImportHelper:
        pass

    class ImportMenu:
        @staticmethod
        def append(callback) -> None:
            registry.menu_entries.append(callback)

        @staticmethod
        def remove(callback) -> None:
            registry.menu_entries.remove(callback)

    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(version=(5, 2, 0))
    bpy.types = types.SimpleNamespace(
        AddonPreferences=AddonPreferences,
        Collection=object,
        ImportHelper=ImportHelper,
        Material=object,
        Object=object,
        Operator=Operator,
        TOPBAR_MT_file_import=ImportMenu,
        VectorFont=object,
    )
    bpy.utils = types.SimpleNamespace(
        register_class=lambda cls: registry.registered.append(cls),
        unregister_class=lambda cls: registry.registered.remove(cls),
    )
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(redraw_timer=lambda **_kwargs: None),
    )
    bpy.data = types.SimpleNamespace(
        collections=_MutationCollection(registry, "collections"),
        objects=_MutationCollection(registry, "objects"),
    )
    bpy.context = types.SimpleNamespace(
        scene=types.SimpleNamespace(
            collection=types.SimpleNamespace(children=_MutationChildren(registry)),
        ),
        view_layer=types.SimpleNamespace(update=lambda: None),
    )

    props = types.ModuleType("bpy.props")
    props.BoolProperty = lambda **_kwargs: None
    props.EnumProperty = lambda **_kwargs: None
    props.FloatProperty = lambda **_kwargs: None
    props.StringProperty = lambda **_kwargs: None
    io_utils = types.ModuleType("bpy_extras.io_utils")
    io_utils.ImportHelper = ImportHelper
    bpy_extras = types.ModuleType("bpy_extras")
    bpy_extras.io_utils = io_utils

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bmesh", types.ModuleType("bmesh"))
    monkeypatch.setitem(sys.modules, "bpy_extras", bpy_extras)
    monkeypatch.setitem(sys.modules, "bpy_extras.io_utils", io_utils)

    yield bpy, registry

    for name in _FRESH_HOST_MODULES:
        sys.modules.pop(name, None)


def _forbid_external_dependency_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime dependency checks must stay offline")

    monkeypatch.setattr(dependency_manager, "install_pymupdf", forbidden)
    monkeypatch.setattr(subprocess, "check_call", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


def test_real_registration_dependency_failure_stays_offline_and_before_scene_mutation(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
) -> None:
    """A registration regression must not turn a missing runtime into pip or scene work."""
    _bpy, registry = fake_blender_modules
    addon = importlib.import_module("pdf_vector_importer")
    calls: list[bool] = []

    def offline_check(*, auto_install: bool = False) -> bool:
        calls.append(auto_install)
        return False

    _forbid_external_dependency_actions(monkeypatch)
    monkeypatch.setattr(dependency_manager, "ensure_pymupdf_runtime", offline_check)
    monkeypatch.setattr(dependency_manager, "print_diagnostics", lambda: None)
    monkeypatch.setattr(dependency_manager, "report_host_python_floor", lambda: True)

    addon.register()

    assert calls == [False]
    assert registry.scene_mutations == []


def test_real_import_dependency_failure_stays_offline_and_before_scene_mutation(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
) -> None:
    """A failed import dependency check must stop before file, package, or scene work."""
    bpy, registry = fake_blender_modules
    engine = importlib.import_module("pdf_vector_importer.bl_import_engine")
    calls: list[bool] = []

    def offline_check(*, auto_install: bool = False) -> bool:
        calls.append(auto_install)
        return False

    _forbid_external_dependency_actions(monkeypatch)
    monkeypatch.setattr(engine, "bpy", bpy)
    monkeypatch.setattr(engine, "check_pymupdf", lambda: False)
    monkeypatch.setattr(dependency_manager, "ensure_pymupdf_runtime", offline_check)

    with pytest.raises(RuntimeError, match="official PDF Vector Importer release ZIP"):
        engine.import_pdf("C:/controlled/missing.pdf")

    assert calls == [False]
    assert registry.scene_mutations == []


class _LayoutRecorder:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.operators: list[str] = []

    def box(self):
        return self

    def row(self):
        return self

    def separator(self) -> None:
        return None

    def label(self, *, text: str, **_kwargs) -> None:
        self.labels.append(text)

    def operator(self, operator_id: str, **_kwargs):
        self.operators.append(operator_id)
        return types.SimpleNamespace()

    def prop(self, *_args, **_kwargs) -> None:
        return None


def _draw_missing_runtime_preferences(
    monkeypatch: pytest.MonkeyPatch,
    preferences,
    package_dir: Path,
) -> _LayoutRecorder:
    layout = _LayoutRecorder()
    panel = preferences.PDFVectorImporterPreferences()
    panel.layout = layout
    panel.remember_last_directory = False
    monkeypatch.setattr(preferences, "__file__", str(package_dir / "preferences.py"))
    monkeypatch.setattr(preferences, "check_pymupdf", lambda: False)
    monkeypatch.setattr(preferences, "runtime_diagnostics", lambda: "controlled unavailable")
    panel.draw(None)
    return layout


def test_packaged_release_preferences_hide_installer_and_direct_reinstall(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """A release customer must not be offered the source-only package mutator."""
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()
    (package_dir / "_release_identity.json").write_text("{}\n", encoding="utf-8")

    layout = _draw_missing_runtime_preferences(
        monkeypatch,
        preferences,
        package_dir,
    )

    assert preferences.PDFVEC_OT_install_pymupdf.bl_idname not in layout.operators
    assert any("reinstall the official release zip" in text.lower() for text in layout.labels)


def test_source_development_preferences_label_the_installer_as_development_only(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """An unmanifested source tree may expose pip only as a labelled developer tool."""
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()

    layout = _draw_missing_runtime_preferences(
        monkeypatch,
        preferences,
        package_dir,
    )

    assert layout.operators == [preferences.PDFVEC_OT_install_pymupdf.bl_idname]
    visible_text = " ".join(layout.labels + [preferences.PDFVEC_OT_install_pymupdf.bl_label])
    assert "source/development" in visible_text.lower()


def test_packaged_release_does_not_register_dependency_installer(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """Packaged add-ons must keep the pip operator out of Blender's operator registry."""
    _bpy, registry = fake_blender_modules
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()
    (package_dir / "_release_identity.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(preferences, "__file__", str(package_dir / "preferences.py"))

    preferences.register()

    assert preferences.PDFVEC_OT_install_pymupdf not in registry.registered


def test_source_installer_refuses_execution_without_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """A direct execute call must not mutate packages until the warning is accepted."""
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()
    monkeypatch.setattr(preferences, "__file__", str(package_dir / "preferences.py"))
    installs: list[str] = []
    monkeypatch.setattr(preferences, "install_pymupdf", lambda: installs.append("pip") or True)
    monkeypatch.setattr(preferences, "get_pymupdf_version", lambda: "controlled")
    operator = preferences.PDFVEC_OT_install_pymupdf()
    operator.confirm_network_install = False
    operator.report = lambda *_args, **_kwargs: None

    result = operator.execute(None)

    assert result == {"CANCELLED"}
    assert installs == []


def test_source_installer_opens_an_explicit_confirmation_dialog(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """Clicking the developer installer must show the network/package warning first."""
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()
    monkeypatch.setattr(preferences, "__file__", str(package_dir / "preferences.py"))
    operator = preferences.PDFVEC_OT_install_pymupdf()
    operator.confirm_network_install = True
    dialogs: list[object] = []
    context = types.SimpleNamespace(
        window_manager=types.SimpleNamespace(
            invoke_props_dialog=lambda actual, **_kwargs: dialogs.append(actual) or {"RUNNING_MODAL"}
        )
    )

    invoke = getattr(operator, "invoke", None)
    assert callable(invoke), "developer installer needs an explicit confirmation dialog"
    result = invoke(context, None)

    assert result == {"RUNNING_MODAL"}
    assert dialogs == [operator]
    assert operator.confirm_network_install is False


def test_packaged_release_refuses_direct_installer_execution_even_if_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    fake_blender_modules,
    tmp_path: Path,
) -> None:
    """A scripted operator call must not bypass the packaged-release gate."""
    preferences = importlib.import_module("pdf_vector_importer.preferences")
    package_dir = tmp_path / "pdf_vector_importer"
    package_dir.mkdir()
    (package_dir / "_release_identity.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(preferences, "__file__", str(package_dir / "preferences.py"))
    installs: list[str] = []
    monkeypatch.setattr(preferences, "install_pymupdf", lambda: installs.append("pip") or True)
    operator = preferences.PDFVEC_OT_install_pymupdf()
    operator.confirm_network_install = True
    operator.report = lambda *_args, **_kwargs: None

    result = operator.execute(None)

    assert result == {"CANCELLED"}
    assert installs == []


class _InstallCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.calls: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name == "install_pymupdf":
            self.calls.append(".".join(self.scope))
        self.generic_visit(node)


def test_package_installer_has_one_exact_production_call_site() -> None:
    """A new automatic caller must fail the allowlist before it can ship."""
    calls: list[tuple[str, str]] = []
    package = REPO_ROOT / "pdf_vector_importer"
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package)
        if relative.parts[0] == "lib":
            continue
        visitor = _InstallCallVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        calls.extend((relative.as_posix(), scope) for scope in visitor.calls)

    assert calls == [
        ("preferences.py", "PDFVEC_OT_install_pymupdf.execute"),
    ]


def test_compatibility_release_guidance_never_routes_customers_to_pip() -> None:
    """Release troubleshooting must not contradict the supported ZIP reinstall path."""
    text = (REPO_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")

    assert "Install PyMuPDF** if import fails on 5.x" not in text
    assert "3. Preferences → **Install PyMuPDF**" not in text
    assert "reinstall the official release zip" in text.lower()
