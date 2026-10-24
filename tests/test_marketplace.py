import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("marketplace", Path(__file__).resolve().parents[1] / "scripts/marketplace.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "plugins/example.test"
        self.package.mkdir(parents=True)
        self.data = b"Original test resource\n"
        (self.package / "guide.txt").write_bytes(self.data)
        self.manifest = {
            "format": 1, "identity": {"id": "example.test", "version": "1.0.0"},
            "protocol": {"major": 1, "min_minor": 113, "max_minor": 113},
            "targets": [{"os": "linux", "architecture": "x86_64"}],
            "entry": {"type": "declarative", "handlers": {"guide.read": {"type": "resource_read", "paths": ["guide.txt"]}}},
            "capabilities": [{"name": "guide.read", "kind": "tool", "effect": "read_only", "description": "Read test guide", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}}],
            "max_concurrency": 1,
            "files": [{"path": "guide.txt", "sha256": m.sha(self.data)}],
        }
        self.save()

    def save(self):
        body = (json.dumps(self.manifest, indent=2) + "\n").encode()
        (self.package / "plugin.json").write_bytes(body)
        self.catalog = {"schema_version": 1, "plugins": [{"id": "example.test", "version": "1.0.0", "path": "plugins/example.test", "manifest_sha256": m.sha(body), "validation": "source-only"}]}
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))

    def test_empty_catalog_is_valid_but_not_a_release(self):
        (self.root / "catalog.json").write_text('{"schema_version":1,"plugins":[]}')
        self.assertEqual(m.verify(self.root), [])
        with self.assertRaises(ValueError):
            m.build(self.root, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_reproducible_archive_and_allowlist(self):
        (self.package / ".env").write_text("THIS_MUST_NOT_SHIP")
        m.build(self.root, self.root / "one")
        m.build(self.root, self.root / "two")
        name = "example.test-1.0.0.zip"
        self.assertEqual((self.root / "one" / name).read_bytes(), (self.root / "two" / name).read_bytes())
        with zipfile.ZipFile(self.root / "one" / name) as archive:
            self.assertEqual(archive.namelist(), ["guide.txt", "plugin.json"])
            self.assertEqual(archive.read("guide.txt"), self.data)

    def test_no_overwrite(self):
        out = self.root / "out"
        out.mkdir()
        (out / "keep").write_text("original")
        with self.assertRaises(ValueError):
            m.build(self.root, out)
        self.assertEqual((out / "keep").read_text(), "original")

    def test_modified_file_fails(self):
        (self.package / "guide.txt").write_text("modified")
        with self.assertRaisesRegex(ValueError, "checksum"):
            m.verify(self.root)

    def test_modified_manifest_fails(self):
        (self.package / "plugin.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            m.verify(self.root)

    def test_path_traversal_and_reserved_paths(self):
        for path in ("../secret", "/etc/passwd", "x/../guide.txt", "x//y", "x\\y", "C:/secret", "plugin.json", ".package-receipt.json/x"):
            with self.subTest(path=path):
                self.manifest["files"][0]["path"] = path
                self.save()
                with self.assertRaises(ValueError):
                    m.verify(self.root)

    def test_symlinks_rejected(self):
        source = self.package / "guide.txt"
        source.unlink()
        external = self.root / "outside"
        external.write_bytes(self.data)
        source.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "Symbolic link"):
            m.verify(self.root)

    def test_duplicate_inventory_rejected(self):
        self.manifest["files"] *= 2
        self.save()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            m.verify(self.root)

    def test_duplicate_json_keys_rejected(self):
        (self.root / "catalog.json").write_text('{"schema_version":1,"schema_version":1,"plugins":[]}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            m.verify(self.root)

    def test_duplicate_catalog_id_rejected(self):
        self.catalog["plugins"] *= 2
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError, "Duplicate plugin"):
            m.verify(self.root)

    def test_path_controls_and_bounds_rejected(self):
        for path in ("guide\n.txt", "x" * 1025, "/".join(["x"] * 33), "guide\x7f.txt", "guide\x85.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                m.local_file(self.package, path)

    def test_semver(self):
        for version in ("1.0.0-01", "1.0.0-a..b", "1.0.0+", "1.0.0+..", "01.0.0"):
            self.assertFalse(m.valid_version(version), version)
        for version in ("1.0.0", "1.0.0-rc.1", "1.0.0+01", "1.0.0-a-b"):
            self.assertTrue(m.valid_version(version), version)

    def test_read_only_script_rejected(self):
        self.manifest["entry"]["handlers"]["guide.read"] = {"type": "managed_script_start", "path": "guide.txt"}
        self.save()
        with self.assertRaisesRegex(ValueError, "side-effect"):
            m.verify(self.root)

    def test_undeclared_resource_rejected(self):
        self.manifest["entry"]["handlers"]["guide.read"]["paths"] = ["undeclared"]
        self.save()
        with self.assertRaisesRegex(ValueError, "declared files"):
            m.verify(self.root)

    def test_host_receipt_validates_scope_not_independent_truth(self):
        receipt = {"plugin": "example.test", "version": "1.0.0", "host_revision": "a" * 40,
                   "platform": "linux-x86_64", "package_digest": "b" * 64, "installed_revision": 1, "manifest_sha256": self.catalog["plugins"][0]["manifest_sha256"],
                   "checks": {"install": "passed", "describe": "passed", "invoke": "passed", "process_completion": "not_applicable", "upgrade": "not_run", "disable": "not_run", "cold_start": "not_run"}}
        directory = self.root / "validation"
        directory.mkdir()
        path = directory / "example.test-1.0.0.json"
        body = json.dumps(receipt).encode()
        path.write_bytes(body)
        self.catalog["plugins"][0].update(validation="host-verified", verification={"path": "validation/example.test-1.0.0.json", "sha256": m.sha(body)})
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        self.assertEqual(len(m.verify(self.root)), 1)
        receipt["manifest_sha256"] = "0" * 64
        body = json.dumps(receipt).encode()
        path.write_bytes(body)
        self.catalog["plugins"][0]["verification"]["sha256"] = m.sha(body)
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError, "not bound"):
            m.verify(self.root)
        receipt["manifest_sha256"] = self.catalog["plugins"][0]["manifest_sha256"]
        receipt["checks"]["invoke"] = "not_run"
        body = json.dumps(receipt).encode()
        path.write_bytes(body)
        self.catalog["plugins"][0]["verification"]["sha256"] = m.sha(body)
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError, "observed install"):
            m.verify(self.root)

    def test_false_host_verification_rejected(self):
        self.catalog["plugins"][0]["validation"] = "host-verified"
        (self.root / "catalog.json").write_text(json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError, "reviewed evidence"):
            m.verify(self.root)


if __name__ == "__main__":
    unittest.main()
