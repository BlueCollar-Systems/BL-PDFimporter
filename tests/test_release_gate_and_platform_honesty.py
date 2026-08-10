"""Two protections the independent review found missing.

1. ``_release_identity.json`` is the SINGLE discriminator for the production pip
   gate. ``preferences._is_packaged_release()`` keys on it, and it controls operator
   registration plus poll/invoke/execute. If a packaging regression dropped that one
   file, every customer install would silently look like an "unmanifested source
   tree" and the pip/network installer would re-register -- with CI green, because
   nothing checked for it.

2. The "bundled runtime could not load" error told every user to reinstall the
   release ZIP. The bundle is ``cp310-abi3-win_amd64`` and ships only .pyd/.dll
   binaries, so on macOS/Linux that is advice to repeat the one action that cannot
   possibly work. Whether to support those platforms is a product decision; telling
   the truth about what the artifact contains is not.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from pdf_vector_importer import dependency_manager  # noqa: E402
from pdf_vector_importer.build_identity import IDENTITY_FILENAME  # noqa: E402
from scripts import smoke_release_zip  # noqa: E402

IDENTITY_MEMBER = "pdf_vector_importer/%s" % IDENTITY_FILENAME


class TestReleaseIdentityIsContractual(unittest.TestCase):
    def test_identity_file_is_a_required_zip_member(self) -> None:
        self.assertIn(
            IDENTITY_MEMBER,
            smoke_release_zip.REQUIRED_MEMBERS,
            "the production pip gate keys on this file; if it is not contractual, a "
            "packaging regression re-enables the installer for every customer",
        )

    def test_smoke_rejects_a_zip_missing_the_identity_file(self) -> None:
        """The protection has to actually fire, not just be listed."""
        with tempfile.TemporaryDirectory(prefix="bl_identity_gate_") as tmp:
            zip_path = Path(tmp) / "no-identity.zip"
            members = set(smoke_release_zip.REQUIRED_MEMBERS) - {IDENTITY_MEMBER}
            with zipfile.ZipFile(zip_path, "w") as archive:
                for member in sorted(members):
                    archive.writestr(member, "")
            result = subprocess.run(
                [sys.executable, str(Path(smoke_release_zip.__file__).resolve()),
                 str(zip_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                IDENTITY_FILENAME, result.stdout + result.stderr,
                "the failure must name the missing file so the cause is obvious",
            )

    def test_the_gate_still_keys_on_that_exact_filename(self) -> None:
        """Guards the coupling: if preferences stops using this name, the zip
        contract above is protecting the wrong file."""
        source = (REPO_DIR / "pdf_vector_importer" / "preferences.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("IDENTITY_FILENAME", source)
        self.assertIn("_is_packaged_release", source)


class TestPlatformHonesty(unittest.TestCase):
    def test_windows_keeps_the_reinstall_guidance(self) -> None:
        with patch.object(dependency_manager.sys, "platform", "win32"):
            msg = dependency_manager.runtime_unavailable_message()
        self.assertIn("Reinstall", msg)
        self.assertTrue(dependency_manager.bundled_runtime_platform_supported())

    def test_non_windows_does_not_advise_a_futile_reinstall(self) -> None:
        for plat in ("darwin", "linux"):
            with self.subTest(platform=plat):
                with patch.object(dependency_manager.sys, "platform", plat):
                    supported = dependency_manager.bundled_runtime_platform_supported()
                    msg = dependency_manager.runtime_unavailable_message()
                self.assertFalse(supported)
                # It may mention reinstalling -- to say it will NOT help. What it
                # must never do is *advise* it as the remedy.
                self.assertNotIn(
                    "Reinstall the official", msg,
                    "reinstalling a Windows-only bundle cannot help on %s" % plat,
                )
                self.assertIn(
                    "will not change that", msg,
                    "the message must actively disclaim the futile remedy, not just "
                    "omit it -- users arrive already assuming reinstall is the fix",
                )
                self.assertIn("Windows", msg, "the real constraint must be named")
                self.assertIn(plat, msg, "the message must name the actual platform")

    def test_message_never_claims_a_network_action(self) -> None:
        for plat in ("win32", "darwin", "linux"):
            with patch.object(dependency_manager.sys, "platform", plat):
                msg = dependency_manager.runtime_unavailable_message()
            self.assertIn("did not download", msg)

    def test_engine_uses_the_platform_aware_message(self) -> None:
        source = (REPO_DIR / "pdf_vector_importer" / "bl_import_engine.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("runtime_unavailable_message", source)
        self.assertNotIn(
            '"The bundled PyMuPDF runtime could not load. Reinstall the "', source,
            "the hardcoded platform-blind message must be gone from the engine",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
