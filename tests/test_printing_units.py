from dataclasses import replace
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from printing.geometry import JobOptions, prepare_job
from printing.units import MM_PER_POSTSCRIPT_POINT, UnitResolutionError, resolve_pattern_units
from printing.backend import CompiledJob
from tools.patterns.fvd_pattern import PREFIX, parse_bytes


def native_square(version=23, family=2):
    def record(number, depth, payload):
        return struct.pack("<IIH", number, len(payload), depth) + payload

    points = ((0, 0, 0, 0), (72, 0, 4, 0), (72, 72, 4, 0),
              (0, 72, 4, 0), (0, 0, 4, 0))
    payload = b"".join(struct.pack("<ddHH", *point) for point in points)
    geometry = (struct.pack("<IddddH", len(points), 0, 0, 72, 72, 1)
                if version == 30 else struct.pack("<I", len(points))) + payload
    return parse_bytes(struct.pack("<H", version) + PREFIX[2:]
                       + record(250, 0, struct.pack("<I", family))
                       + record(1, 0, struct.pack("<I", 1))
                       + record(5, 1, b"") + record(4, 2, struct.pack("<H", 1))
                       + record(63 if version == 30 else 3, 3, geometry))


class PatternUnitTests(unittest.TestCase):
    def test_native_one_inch_square_has_exact_physical_size_in_both_encodings(self):
        for version, family in ((23, 1), (23, 2), (30, 4)):
            with self.subTest(version=version, family=family):
                pattern = native_square(version, family)
                resolution = resolve_pattern_units(pattern)
                job = prepare_job(pattern, JobOptions(mm_per_source_unit=resolution.mm_per_source_unit))
                self.assertAlmostEqual(job.width_mm, 25.4, places=12)
                self.assertAlmostEqual(job.height_mm, 25.4, places=12)
                self.assertTrue(resolution.automatic)
                self.assertEqual(resolution.serializer_version, version)

    def test_unit_resolution_is_independent_of_filename_extension_and_drawing_size(self):
        pattern = native_square()
        for filename in ("drawing.FDU", "drawing.FDA", "renamed.dat"):
            resolution = resolve_pattern_units(replace(pattern, source=filename))
            self.assertEqual(resolution.mm_per_source_unit, MM_PER_POSTSCRIPT_POINT)
            self.assertIn("native-FD_GetPatternSize-divides-by-72", resolution.evidence)

    def test_unknown_formats_and_partial_geometry_do_not_receive_an_automatic_scale(self):
        pattern = native_square()
        for unsupported in (replace(pattern, serializer_version=31), replace(pattern, family=9),
                            replace(pattern, complete_geometry=False), replace(pattern, paths=()),
                            SimpleNamespace(serializer_version=23, family=2, complete_geometry=True)):
            with self.subTest(pattern=unsupported):
                with self.assertRaises(UnitResolutionError):
                    resolve_pattern_units(unsupported)

    def test_cli_uses_native_units_and_creates_a_new_nested_output_directory(self):
        from printing.__main__ import main
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new" / "exports" / "inch.plt"
            compiled = CompiledJob(b"IN;PU;", {"native_bounds_mm": [0, 0, 25.4, 25.4]})
            argv = ["printing", "--pattern", "pattern.FDA", "--output", str(output)]
            with patch("sys.argv", argv), patch("printing.__main__.read_pattern", return_value=native_square()), \
                    patch("printing.__main__.discover_runtime", return_value=Path(folder)), \
                    patch("printing.__main__.compile_job", return_value=compiled) as compile_mock, \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            prepared = compile_mock.call_args.args[0]
            self.assertAlmostEqual(prepared.width_mm, 25.4)
            self.assertEqual(output.read_bytes(), b"IN;PU;")
            receipt = json.loads(output.with_suffix(".plt.json").read_text())
            self.assertTrue(receipt["source_units"]["automatic"])
            self.assertAlmostEqual(receipt["geometry"]["height_mm"], 25.4)

    def test_cli_explicit_advanced_override_takes_precedence(self):
        from printing.__main__ import main
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "override.plt"
            compiled = CompiledJob(b"IN;PU;", {"native_bounds_mm": [0, 0, 72, 72]})
            argv = ["printing", "--pattern", "pattern.FDU", "--mm-per-source-unit", "1", "--output", str(output)]
            with patch("sys.argv", argv), patch("printing.__main__.read_pattern", return_value=native_square()), \
                    patch("printing.__main__.discover_runtime", return_value=Path(folder)), \
                    patch("printing.__main__.resolve_pattern_units", side_effect=AssertionError("override ignored")), \
                    patch("printing.__main__.compile_job", return_value=compiled) as compile_mock, \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertAlmostEqual(compile_mock.call_args.args[0].width_mm, 72)
            receipt = json.loads(output.with_suffix(".plt.json").read_text())
            self.assertFalse(receipt["source_units"]["automatic"])


if __name__ == "__main__":
    unittest.main()
