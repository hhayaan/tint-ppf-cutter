import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

SPEC = importlib.util.spec_from_file_location("pack_data", Path(__file__).resolve().parents[1] / "tools/distribution/pack_data.py")
pack_data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack_data)


class PrivateDataBundleTests(unittest.TestCase):
    def fixture(self, root):
        patterns = root / "data/patterns"
        patterns.mkdir(parents=True)
        analysis = root / "analysis"
        analysis.mkdir()
        asset = "11111111-2222-3333-4444-555555555555"
        payloads = {asset + ".FDU": b"private pattern bytes", "warnings.json": json.dumps({asset: "Fit check"}).encode()}
        receipts = []
        for index, (name, content) in enumerate(payloads.items()):
            (patterns / name).write_bytes(content)
            receipt = analysis / f"receipt{index}.json"
            receipt.write_text(json.dumps({"complete": True, "files": [{"basename": name, "byte_size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}))
            receipts.append(receipt)
        catalog = analysis / "catalog.sqlite"
        with closing(sqlite3.connect(catalog)) as connection:
            connection.execute("CREATE TABLE CarPattern(PatternID TEXT,FDImageId TEXT)")
            connection.execute("INSERT INTO CarPattern VALUES (?,?)", ("record1", asset))
            connection.execute("CREATE TABLE LocalPatternFiles(file_path TEXT)")
            connection.execute("INSERT INTO LocalPatternFiles VALUES (?)", (str(patterns / (asset + ".FDU")),))
            connection.execute("CREATE VIEW patterns AS SELECT PatternID AS id,FDImageId AS asset_id FROM CarPattern")
            connection.execute("CREATE VIEW local_patterns AS SELECT * FROM patterns")
            connection.commit()
        (analysis / "secret-original.mdb").write_bytes(b"never package")
        return catalog, patterns, receipts

    def test_flat_private_bundle_roundtrip_and_source_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, patterns, receipts = self.fixture(root)
            before = pack_data.file_hash(catalog)
            result = pack_data.build_bundle(root, catalog, patterns, receipts, root / "distribution/private", emit=lambda event: None)
            self.assertEqual(Path(result["checksum_file"]).read_text(encoding="ascii"), result["archive_sha256"] + "  " + Path(result["archive"]).name + "\n")
            with zipfile.ZipFile(result["archive"]) as archive:
                names = archive.namelist()
                self.assertTrue(all(name.startswith("data/") for name in names))
                self.assertEqual(len(names), 4)
                self.assertFalse(any("mdb" in name or "analysis" in name for name in names))
            extracted = Path(result["verification"]["data_root"])
            with closing(sqlite3.connect(extracted / "catalog.sqlite")) as connection:
                self.assertEqual(connection.execute("SELECT file_path FROM LocalPatternFiles").fetchone()[0], "data/patterns/11111111-2222-3333-4444-555555555555.FDU")
            self.assertEqual(pack_data.file_hash(catalog), before)
            self.assertTrue(pack_data.verify_data(extracted, emit=lambda event: None)["verified"])

    def test_recipient_verifier_rejects_extra_geometry_and_changed_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, patterns, receipts = self.fixture(root)
            result = pack_data.build_bundle(root, catalog, patterns, receipts, root / "distribution/private", emit=lambda event: None)
            data = Path(result["verification"]["data_root"])
            extra = data / "patterns/extra.FDA"
            extra.write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "file set differs"):
                pack_data.verify_data(data, emit=lambda event: None)
            extra.unlink()
            target = next((data / "patterns").glob("*.FDU"))
            target.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                pack_data.verify_data(data, emit=lambda event: None)

    def test_account_table_is_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, patterns, receipts = self.fixture(root)
            with closing(sqlite3.connect(catalog)) as connection:
                connection.execute("CREATE TABLE Configuration(ClientUsername TEXT,ClientPassword TEXT)")
                connection.commit()
            with self.assertRaisesRegex(ValueError, "allowlist"):
                pack_data.build_bundle(root, catalog, patterns, receipts, root / "distribution/private", emit=lambda event: None)
            self.assertFalse(list((root / "distribution/private").glob("*.zip")))

    def test_recipient_rejects_missing_asset_even_with_valid_file_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, patterns, receipts = self.fixture(root)
            result = pack_data.build_bundle(root, catalog, patterns, receipts, root / "distribution/private", emit=lambda event: None)
            data = Path(result["verification"]["data_root"])
            packaged_catalog = data / "catalog.sqlite"
            with closing(sqlite3.connect(packaged_catalog)) as connection:
                connection.execute("UPDATE CarPattern SET FDImageId='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'")
                connection.commit()
            manifest_path = data / pack_data.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            for record in manifest["files"]:
                if record["path"] == "catalog.sqlite":
                    record["byte_size"] = packaged_catalog.stat().st_size
                    record["sha256"] = pack_data.file_hash(packaged_catalog)
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "missing pattern asset"):
                pack_data.verify_data(data, emit=lambda event: None)


if __name__ == "__main__":
    unittest.main()
