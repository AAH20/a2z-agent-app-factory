import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from a2z_app_factory.core import install, pack, run, verify


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "tiny-source"
        (self.source / "src" / "tinyapp").mkdir(parents=True)
        (self.source / "src" / "tinyapp" / "__init__.py").write_text("")
        (self.source / "src" / "tinyapp" / "cli.py").write_text(
            "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('executed pinned app')\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)], check=True)
        git(self.source, "add", ".")
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "tiny app"], check=True)
        self.manifest = self.root / "app.json"
        self.value = {"schema_version": 1, "id": "tiny-test-app", "version": "0.1.0",
                      "description": "Tiny test app", "source": {"url": "https://github.com/example/tiny-app.git",
                      "commit": git(self.source, "rev-parse", "HEAD")}, "python_min": "3.11",
                      "entry_module": "tinyapp.cli", "evidence_class": "SYNTHETIC_DEMO_ONLY"}
        self.manifest.write_text(json.dumps(self.value))
        self.bundle = self.root / "app.a2zapp"
        self.target = self.root / "installed"

    def test_pack_install_verify_run(self):
        first = pack(self.manifest, self.source, self.bundle)
        second_path = self.root / "again.a2zapp"
        second = pack(self.manifest, self.source, second_path)
        self.assertEqual(first["bundle_sha256"], second["bundle_sha256"])
        receipt = install(self.bundle, self.target)
        self.assertEqual(receipt["source_commit"], self.value["source"]["commit"])
        self.assertTrue(verify(self.target)["valid"])
        output = self.root / "output.txt"
        self.assertEqual(run(self.target, [str(output)], env=os.environ.copy()), 0)
        self.assertEqual(output.read_text(), "executed pinned app")
        with self.assertRaisesRegex(ValueError, "already exists"):
            install(self.bundle, self.target)

    def test_pin_mismatch_and_dirty_source(self):
        self.value["source"]["commit"] = "0" * 40
        self.manifest.write_text(json.dumps(self.value))
        with self.assertRaisesRegex(ValueError, "pinned manifest"):
            pack(self.manifest, self.source, self.bundle)
        self.value["source"]["commit"] = git(self.source, "rev-parse", "HEAD")
        self.manifest.write_text(json.dumps(self.value))
        (self.source / "src" / "tinyapp" / "cli.py").write_text("print('dirty')")
        with self.assertRaisesRegex(ValueError, "modified"):
            pack(self.manifest, self.source, self.bundle)

    def test_installed_source_or_manifest_tamper_rejected(self):
        pack(self.manifest, self.source, self.bundle)
        install(self.bundle, self.target)
        entry = self.target / "source" / "src" / "tinyapp" / "cli.py"
        entry.write_text("print('tampered')")
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.target)
        entry.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('executed pinned app')\n")
        manifest = json.loads((self.target / "manifest.json").read_text())
        manifest["description"] = "Changed"
        (self.target / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.target)

    def test_archive_traversal_rejected(self):
        pack(self.manifest, self.source, self.bundle)
        with zipfile.ZipFile(self.bundle) as bundle:
            manifest = bundle.read("manifest.json")
            index = json.loads(bundle.read("index.json"))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            payload = b"outside"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        raw = stream.getvalue()
        index["source_tar_sha256"] = hashlib.sha256(raw).hexdigest()
        evil = self.root / "evil.a2zapp"
        with zipfile.ZipFile(evil, "w") as bundle:
            bundle.writestr("manifest.json", manifest)
            bundle.writestr("index.json", json.dumps(index))
            bundle.writestr("source.tar", raw)
        with self.assertRaisesRegex(ValueError, "unsafe entry"):
            install(evil, self.target)
        self.assertFalse((self.root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
