"""The bundled PyMuPDF must survive every install failure mode.

PyMuPDF is the sole rasteriser and raster is the terminal representation rung, so
losing the bundle does not degrade one feature -- it takes out the product's
last-resort output on a machine that may have no network to repair itself.

An independent review reproduced three ways the previous quarantine/restore code
destroyed the bundle *permanently*, all of them while the suite stayed green:

  1. a failed restore silently orphaned the only good copy in ``.quarantine``;
  2. the next install attempt rmtree'd that orphan -- the last copy -- while
     printing "The bundled runtime was left in place";
  3. a failed quarantine did not abort, and ``pip install --target --upgrade``
     rmtree's the destination, so pip destroyed the un-backed-up bundle.

Every test here fails against that old implementation. They assert on the bytes
that survive on disk, not on the return value, because the return value was
identical whether the bundle came back or was lost.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from pdf_vector_importer import dependency_manager  # noqa: E402

GOOD = "GOOD-BUNDLE-BYTES"


def _make_bundle(lib: Path) -> None:
    (lib / "pymupdf").mkdir(parents=True)
    (lib / "pymupdf" / "__init__.py").write_text(GOOD, encoding="utf-8")
    (lib / "pymupdf-1.27.2.3.dist-info").mkdir()
    (lib / "pymupdf-1.27.2.3.dist-info" / "WHEEL").write_text(GOOD, encoding="utf-8")


def _bundle_intact(lib: Path) -> bool:
    f = lib / "pymupdf" / "__init__.py"
    return f.is_file() and f.read_text(encoding="utf-8") == GOOD


class TestBundleSurvivesInstallFailure(unittest.TestCase):
    def test_every_failure_branch_restores_the_bundle(self) -> None:
        """All four failure branches, not just CalledProcessError."""
        branches = [
            subprocess.CalledProcessError(1, ["pip"]),
            FileNotFoundError("no python"),
            OSError("disk"),
            subprocess.TimeoutExpired(["pip"], 300),
        ]
        for exc in branches:
            with self.subTest(branch=type(exc).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    lib = Path(tmp)
                    _make_bundle(lib)

                    def _fail(*_a, **_k):
                        raise exc

                    with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                            patch("subprocess.check_call", _fail):
                        ok = dependency_manager.install_pymupdf(clear_vendored=True)

                    self.assertFalse(ok)
                    self.assertTrue(
                        _bundle_intact(lib),
                        "%s destroyed the bundled runtime" % type(exc).__name__,
                    )
                    self.assertEqual([], list(lib.glob("*.quarantine*")))

    def test_unexpected_exception_still_restores_the_bundle(self) -> None:
        """No try/finally previously: any unnamed exception stranded the bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _make_bundle(lib)

            def _boom(*_a, **_k):
                raise RuntimeError("unexpected")

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", _boom):
                with self.assertRaises(RuntimeError):
                    dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertTrue(
                _bundle_intact(lib),
                "an unexpected exception stranded the bundle in quarantine",
            )

    def test_pip_succeeds_but_import_fails_restores_the_bundle(self) -> None:
        """The ABI-wheel branch the landing commit itself called out."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _make_bundle(lib)

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", lambda *_a, **_k: 0), \
                    patch.object(dependency_manager, "_purge_stale_pymupdf_modules"), \
                    patch.object(dependency_manager, "ensure_lib_path"), \
                    patch.object(dependency_manager, "check_pymupdf", return_value=False):
                ok = dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertFalse(ok)
            self.assertTrue(_bundle_intact(lib))


class TestAbortRatherThanRiskTheBundle(unittest.TestCase):
    def test_unsecured_backup_aborts_before_pip_runs(self) -> None:
        """pip --target --upgrade rmtree's the destination, so an unsecured
        bundle must stop the install rather than be handed to pip."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _make_bundle(lib)
            calls = []

            def _record(*a, **k):
                calls.append(a)
                return 0

            original_rename = Path.rename

            def _locked(self, target):
                if self.name == "pymupdf":
                    raise PermissionError("directory in use")
                return original_rename(self, target)

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", _record), \
                    patch.object(Path, "rename", _locked):
                ok = dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertFalse(ok)
            self.assertEqual([], calls, "pip must not run without a secured backup")
            self.assertTrue(_bundle_intact(lib))
            self.assertEqual([], list(lib.glob("*.quarantine*")))


class TestNeverDeleteAForeignQuarantine(unittest.TestCase):
    def test_preexisting_quarantine_is_not_destroyed(self) -> None:
        """A stale quarantine can be the only surviving copy after an earlier
        failed restore; deleting it turns a recoverable orphan into total loss."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _make_bundle(lib)
            orphan = lib / "pymupdf.quarantine"
            orphan.mkdir()
            (orphan / "__init__.py").write_text("ORPHANED-LAST-COPY", encoding="utf-8")

            def _fail(*_a, **_k):
                raise subprocess.CalledProcessError(1, ["pip"])

            with patch.object(dependency_manager, "get_lib_dir", return_value=lib), \
                    patch("subprocess.check_call", _fail):
                dependency_manager.install_pymupdf(clear_vendored=True)

            self.assertTrue(
                (orphan / "__init__.py").is_file(),
                "a quarantine this run did not create must never be deleted",
            )
            self.assertEqual(
                "ORPHANED-LAST-COPY",
                (orphan / "__init__.py").read_text(encoding="utf-8"),
            )


class TestRestoreFailureIsLoud(unittest.TestCase):
    def test_failed_restore_reports_the_recovery_path(self) -> None:
        """The old code returned the same False whether the bundle came back or
        was lost, so the user was told nothing had happened."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            _make_bundle(lib)
            src = lib / "pymupdf"
            quarantine = lib / "pymupdf.quarantine-deadbeef"
            shutil.move(str(src), str(quarantine))

            def _cannot_restore(*_a, **_k):
                raise OSError("still locked")

            printed = []
            with patch.object(Path, "rename", _cannot_restore), \
                    patch("shutil.copytree", _cannot_restore), \
                    patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
                failed = dependency_manager._restore_quarantined([(src, quarantine)])

            self.assertEqual([src], failed, "a failed restore must be reported, not swallowed")
            joined = "\n".join(printed)
            self.assertIn("COULD NOT RESTORE", joined)
            self.assertIn(quarantine.name, joined, "must name the recovery path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
