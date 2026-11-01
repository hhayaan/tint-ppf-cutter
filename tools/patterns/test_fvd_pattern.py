import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fvd_pattern import (PREFIX, PatternError, VectorPath, export_svg,
                         parse_bytes, path_commands, read_pattern)


def record(record_id, kind, payload):
    return struct.pack("<IIH", record_id, len(payload), kind) + payload


def fixture(points, trailer=b""):
    geometry = struct.pack("<I", len(points)) + b"".join(
        struct.pack("<ddHH", *point) for point in points) + trailer
    return (PREFIX + record(250, 0, struct.pack("<I", 2))
            + record(1, 0, struct.pack("<I", 1)) + record(5, 1, b"")
            + record(4, 2, struct.pack("<H", 1)) + record(3, 3, geometry)
            + record(9, 2, bytes(260)) + record(2, 0, bytes(8)))


class PatternTests(unittest.TestCase):
    def test_cubic_remains_cubic_and_line_remains_line(self):
        pattern = parse_bytes(fixture([(0, 0, 0, 0), (1, 2, 1, 1),
                                      (3, 4, 2, 2), (5, 6, 3, 0), (9, 6, 4, 0)]))
        self.assertEqual(path_commands(pattern.paths[0]), "M0,0 C1,2 3,4 5,6 L9,6")
        self.assertEqual(pattern.warnings, ())

    def test_trailer_is_preserved_without_becoming_another_point(self):
        trailer = bytes(range(20))
        pattern = parse_bytes(fixture([(0, 0, 0, 0), (10, 0, 4, 0)], trailer))
        self.assertEqual(len(pattern.paths[0].points), 2)
        self.assertEqual(pattern.paths[0].opaque_trailer, trailer)
        self.assertEqual(path_commands(pattern.paths[0]), "M0,0 L10,0")

    def test_truncated_and_overflowing_records_are_rejected(self):
        valid = fixture([(0, 0, 0, 0)])
        with self.assertRaises(PatternError):
            parse_bytes(valid[:-1])
        with self.assertRaises(PatternError):
            parse_bytes(PREFIX + struct.pack("<IIH", 250, 0xffffffff, 0))
        with self.assertRaises(PatternError):
            parse_bytes(valid + b"\x01")

    def test_corrupt_point_count_and_nonfinite_coordinates_are_rejected(self):
        valid = fixture([(0, 0, 0, 0)])
        pattern = parse_bytes(valid)
        offset = pattern.paths[0].record_offset + 10
        broken = bytearray(valid)
        struct.pack_into("<I", broken, offset, 100000)
        with self.assertRaises(PatternError):
            parse_bytes(bytes(broken))
        with self.assertRaises(PatternError):
            parse_bytes(fixture([(float("nan"), 0, 0, 0)]))

    def test_incomplete_curve_is_not_silently_drawn_as_lines(self):
        path = VectorPath(0, 0, ((0, 0, 0, 0), (1, 2, 1, 0), (5, 6, 3, 0)), b"")
        with self.assertRaises(PatternError):
            path_commands(path)

    def test_nested_group_coordinates_are_not_omitted(self):
        inner = fixture([(0, 0, 0, 0), (7, 8, 4, 0)])
        # Wrap the primitive in one group and increase its record depths by 2.
        from fvd_pattern import HEADER
        nested = PREFIX + record(250, 0, struct.pack("<I", 2)) + record(1, 0, struct.pack("<I", 1))
        nested += record(8, 1, b"") + record(1, 2, struct.pack("<I", 1))
        pos = len(PREFIX)
        while pos < len(inner):
            record_id, count, depth = HEADER.unpack_from(inner, pos)
            payload = inner[pos+10:pos+10+count]
            if depth > 0:
                nested += record(record_id, depth + 2, payload)
            pos += 10 + count
        nested += record(2, 0, bytes(8))
        pattern = parse_bytes(nested)
        self.assertTrue(pattern.complete_geometry)
        self.assertEqual(path_commands(pattern.paths[0]), "M0,0 L7,8")

    def test_version30_bound_header_is_not_misread_as_points(self):
        prefix = struct.pack("<H", 30) + PREFIX[2:]
        geometry = (struct.pack("<IddddH", 2, 0, 0, 7, 8, 1)
                    + struct.pack("<ddHHddHH", 0, 0, 0, 0, 7, 8, 4, 0))
        data = (prefix + record(250, 0, struct.pack("<I", 4))
                + record(1, 0, struct.pack("<I", 1)) + record(5, 1, b"")
                + record(4, 2, struct.pack("<H", 1)) + record(63, 3, geometry))
        pattern = parse_bytes(data)
        self.assertEqual(pattern.serializer_version, 30)
        self.assertEqual(path_commands(pattern.paths[0]), "M0,0 L7,8")

    def test_opaque_drawing_record_marks_partial_and_blocks_export(self):
        pattern = parse_bytes(fixture([(0, 0, 0, 0), (7, 8, 4, 0)]) + record(40, 1, b""))
        self.assertFalse(pattern.complete_geometry)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PatternError):
                export_svg(pattern, Path(directory) / "sample.svg")

    def test_svg_declares_known_source_units_without_scaling_or_physical_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            source = source_dir / "sample.FDU"
            source.write_bytes(fixture([(0, 0, 0, 0), (10, 0, 4, 0)]))
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            pattern = read_pattern(source)
            output = root / "output" / "sample.svg"
            export_svg(pattern, output)
            tree = ET.parse(output)
            svg = tree.getroot()
            metadata = json.loads(tree.find("{http://www.w3.org/2000/svg}metadata").text)
            self.assertEqual(metadata["coordinate_units"], "PostScript point (1/72 inch)")
            self.assertAlmostEqual(metadata["mm_per_source_unit"], 25.4 / 72)
            self.assertFalse(metadata["physical_dimensions_set"])
            self.assertFalse(metadata["physical_scale_applied"])
            self.assertFalse(metadata["physical_cutter_verified"])
            self.assertIn("user units", metadata["svg_units"])
            self.assertNotIn("width", svg.attrib)
            self.assertNotIn("height", svg.attrib)
            self.assertEqual(svg.attrib["viewBox"], "-0.25 -0.25 10.5 1.5")
            self.assertEqual(tree.find(".//{http://www.w3.org/2000/svg}path").attrib["d"], "M0,0 L10,0")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            with self.assertRaises(PatternError):
                export_svg(pattern, source_dir / "accidental.svg")


if __name__ == "__main__":
    unittest.main()
