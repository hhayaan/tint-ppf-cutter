"""Meaningful offline checks for catalog filters and source-path safety."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from app import Catalog, Filters, export_preview, has_local_file, load_asset_warnings, local_pattern_names, safe_pattern_path, sample_path, warning_for_asset
from tools.patterns.fvd_pattern import PatternError, VectorPath


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "catalog.sqlite"
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE patterns (id TEXT, asset_id TEXT, make TEXT, model TEXT, trim TEXT, year_from INTEGER, year_to INTEGER, pattern_type TEXT, extension TEXT, description TEXT)")
        db.executemany("INSERT INTO patterns VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("a", "asset-a", "BMW", "3 Series", "Sedan", 2018, 2020, "TINT", "FDU", "Front 100%_glass"),
            ("b", "asset-b", "BMW", "X3", "SUV", 2020, 2024, "PPF", None, "Hood"),
            ("c", "asset-c", "Toyota", "Corolla", "Hatch", 2019, 2021, "TINT", "FDU", "Front 100XXglass"),
            ("d", "asset-d", "Toyota", "Unknown", "", None, None, "TINT", "FDU", "Unknown year"),
            ("../escape", "../escape", "Unsafe", "Path", "", 2020, 2020, "TINT", "FDU", "Invalid path"),
        ])
        db.commit()
        db.close()
        (self.root / "asset-a.FDU").write_bytes(b"source")
        (self.root / "asset-b.FDA").write_bytes(b"source")
        self.catalog = Catalog(self.path)

    def tearDown(self):
        self.catalog.close()
        self.temporary.cleanup()

    def ids(self, filters, offset=0, limit=100):
        rows, total = self.catalog.search(filters, local_pattern_names(self.root), offset, limit)
        return [row["id"] for row in rows], total

    def test_make_model_type_and_inclusive_year_range(self):
        self.assertEqual(self.ids(Filters(make="bmw", year=2020, local_only=False)), (["a", "b"], 2))
        self.assertEqual(self.ids(Filters(model="3 Series", year=2018, pattern_type="TINT", local_only=False)), (["a"], 1))
        self.assertEqual(self.ids(Filters(make="BMW", year=2021, pattern_type="TINT", local_only=False)), ([], 0))
        self.assertEqual(self.ids(Filters(year=2020, query="Unknown", local_only=False)), ([], 0))

    def test_search_treats_wildcards_and_sql_as_literal_text(self):
        self.assertEqual(self.ids(Filters(query="100%_glass", local_only=False)), (["a"], 1))
        self.assertEqual(self.ids(Filters(query="' OR 1=1 --", local_only=False)), ([], 0))

    def test_local_only_uses_actual_files_and_page_offsets(self):
        self.assertEqual(self.ids(Filters(), offset=1, limit=1), (["b"], 2))
        (self.root / "asset-a.FDU").unlink()
        self.assertEqual(self.ids(Filters()), (["b"], 1))

    def test_choices_are_scoped_and_fields_cannot_inject_sql(self):
        self.assertEqual(self.catalog.choices("model", "BMW"), ["3 Series", "X3"])
        with self.assertRaises(ValueError):
            self.catalog.choices("id); DROP TABLE patterns; --")

    def test_catalog_connection_refuses_writes_without_changing_file(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.OperationalError):
            self.catalog.connection.execute("DELETE FROM patterns")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(self.path) + "-journal").exists())

    def test_path_traversal_and_unsupported_extensions_are_rejected(self):
        for identifier in ("../x", "..\\x", "C:\\x", "a/b", "..", "x\x00"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                safe_pattern_path(self.root, identifier, "FDU")
        with self.assertRaises(ValueError):
            safe_pattern_path(self.root, "a", "../FDU")
        with self.assertRaises(ValueError):
            safe_pattern_path(self.root, "a", "EXE")
        self.assertEqual(safe_pattern_path(self.root, "asset-a", ".fdu"), (self.root / "asset-a.FDU").resolve())
        self.assertFalse(has_local_file({"asset_id": "../escape", "extension": "FDU"}, {"../escape.fdu"}))

    def test_missing_catalog_extension_uses_actual_file_and_rejects_ambiguity(self):
        self.assertEqual(safe_pattern_path(self.root, "asset-b", None), (self.root / "asset-b.FDA").resolve())
        (self.root / "asset-b.FDU").write_bytes(b"other source")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            safe_pattern_path(self.root, "asset-b", None)

    def test_geometry_assets_require_exact_id_not_catalog_prefix(self):
        source = self.root / "db53f256-4eba-4e64-b806-abcdef123456.FDU"
        source.write_bytes(b"source")
        self.assertEqual(safe_pattern_path(self.root, "db53f256-4eba-4e64-b806-abcdef123456", None), source.resolve())
        other = self.root / "db53f256-4eba-4e64-b806-ab0000000000.FDA"
        other.write_bytes(b"source")
        with self.assertRaises(FileNotFoundError):
            safe_pattern_path(self.root, "db53f256-4eba-4e64-b806-a", None)


class PreviewTests(unittest.TestCase):
    def test_partial_geometry_export_is_refused(self):
        with self.assertRaisesRegex(PatternError, "Partial geometry"):
            export_preview(SimpleNamespace(complete_geometry=False))

    def test_cubic_is_sampled_to_exact_endpoint_and_moves_stay_separate(self):
        vector = VectorPath(0, 0, ((0., 0., 0, 0), (0., 10., 1, 0), (10., 10., 2, 0), (10., 0., 3, 0), (20., 20., 0, 0), (21., 20., 4, 0)), b"")
        lines = sample_path(vector, steps=2)
        self.assertEqual(lines[0], [(0., 0.), (5., 7.5), (10., 0.)])
        self.assertEqual(lines[1], [(20., 20.), (21., 20.)])


class WarningMetadataTests(unittest.TestCase):
    def test_asset_id_lookup_is_case_insensitive_and_preserves_plain_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "warnings.json").write_text('{"ABC-123": "EUROPEAN MODEL ONLY; $(plain text)"}', encoding="utf-8")
            warnings = load_asset_warnings(root)
            self.assertEqual(warning_for_asset(warnings, "abc-123"), "EUROPEAN MODEL ONLY; $(plain text)")
            self.assertEqual(warning_for_asset(warnings, "AbC-123"), "EUROPEAN MODEL ONLY; $(plain text)")
            self.assertEqual(warning_for_asset(warnings, "different-catalog-row-id"), "")

    def test_missing_optional_warning_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_asset_warnings(Path(directory)), {})

    def test_malformed_or_nontext_warning_metadata_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text in ('{broken', '[]', '{"asset": 12}'):
                with self.subTest(text=text):
                    (root / "warnings.json").write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Invalid warnings.json"):
                        load_asset_warnings(root)


if __name__ == "__main__":
    unittest.main()
