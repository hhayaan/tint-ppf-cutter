"""Read Film Designs FDA/FDU records and export the observed vector subset.

Source files are opened in binary read mode. Inspected native path coordinates
use PostScript points; knot flag editing semantics remain unresolved. SVG output
retains raw coordinates without a physical-size conversion. This module never
loads vendor code.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from xml.sax.saxutils import escape

PREFIX = bytes.fromhex("170001000000000092ab6f8c9a849999948b")
HEADER = struct.Struct("<IIH")
POINT = struct.Struct("<ddHH")
MAX_BYTES = 16 * 1024 * 1024
POSTSCRIPT_POINT_NAME = "PostScript point (1/72 inch)"
MM_PER_POSTSCRIPT_POINT = 25.4 / 72.0
KNOWN_RECORDS = {(250, 0), (0, 0), (70, 0), (49, 0), (34, 0), (1, 0), (2, 0), (71, 0)}


class PatternError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    offset: int
    id: int
    kind: int
    payload: bytes


@dataclass(frozen=True)
class VectorPath:
    record_offset: int
    group: int
    points: tuple[tuple[float, float, int, int], ...]
    opaque_trailer: bytes
    geometry_header: bytes = b""


@dataclass(frozen=True)
class Pattern:
    source: str
    sha256: str
    byte_count: int
    family: int
    records: tuple[Record, ...]
    paths: tuple[VectorPath, ...]
    declared_groups: int | None
    group_path_counts: tuple[int | None, ...]
    warnings: tuple[str, ...]
    serializer_version: int
    complete_geometry: bool
    unresolved_records: tuple[tuple[int, int], ...]


def parse_bytes(data: bytes, source: str = "<bytes>") -> Pattern:
    if len(data) > MAX_BYTES:
        raise PatternError(f"File exceeds {MAX_BYTES} bytes")
    if len(data) < 18 or data[2:18] != PREFIX[2:] or struct.unpack_from("<H", data)[0] not in (23, 30):
        raise PatternError("Unsupported 18-byte serializer prefix")
    serializer_version = struct.unpack_from("<H", data)[0]
    records: list[Record] = []
    paths: list[VectorPath] = []
    warnings: list[str] = []
    declared_groups = None
    group = -1
    group_counts: list[int | None] = []
    family = None
    active_objects: dict[int, tuple[int, int | None]] = {}
    unresolved = set()
    root_object_count = 0
    pos = len(PREFIX)
    while pos < len(data):
        if len(data) - pos < HEADER.size:
            raise PatternError(f"Truncated record header at {pos}")
        record_id, size, kind = HEADER.unpack_from(data, pos)
        end = pos + HEADER.size + size
        if end > len(data):
            raise PatternError(f"Record at {pos} extends beyond EOF")
        payload = data[pos + HEADER.size:end]
        record = Record(pos, record_id, kind, payload)
        records.append(record)
        key = (record_id, kind)
        if key == (250, 0):
            if family is not None or size != 4:
                raise PatternError("Invalid or repeated family record")
            family = struct.unpack("<I", payload)[0]
            if family not in (1, 2, 4):
                warnings.append(f"Unrecognized family {family}")
        elif key == (1, 0):
            if size != 4:
                raise PatternError(f"Invalid group-count record at {pos}")
            declared_groups = struct.unpack("<I", payload)[0]
        elif record_id in (5, 8, 27, 40, 41) and kind >= 1:
            active_objects = {depth: obj for depth, obj in active_objects.items() if depth < kind}
            if kind == 1:
                root_object_count += 1
            if record_id != 8:
                group += 1
                group_counts.append(None)
                active_objects[kind] = (record_id, group)
            else:
                active_objects[kind] = (record_id, None)
            if payload:
                warnings.append(f"Group-start record at {pos} has opaque payload")
            if record_id not in (5, 8):
                unresolved.add((record_id, kind))
        elif record_id == 4 and kind >= 2:
            owner = active_objects.get(kind - 1)
            if size == 2 and owner and owner[0] == 5:
                group_counts[owner[1]] = struct.unpack("<H", payload)[0]
            else:
                warnings.append(f"Unrecognized group-path-count record at {pos}")
                unresolved.add(key)
        elif record_id in (3, 63) and kind >= 3:
            point_offset = 38 if record_id == 63 else 4
            if size < point_offset:
                raise PatternError(f"Geometry at {pos} lacks its point count")
            count = struct.unpack_from("<I", payload)[0]
            required = point_offset + POINT.size * count
            if required > size:
                raise PatternError(f"Geometry at {pos} has an incomplete point array")
            points = tuple(POINT.unpack_from(payload, point_offset + i * POINT.size)
                           for i in range(count))
            for x, y, point_type, flag in points:
                if not math.isfinite(x) or not math.isfinite(y):
                    raise PatternError(f"Non-finite coordinate at {pos}")
            owner = active_objects.get(kind - 2)
            owner_group = owner[1] if owner and owner[1] is not None else -1
            if owner_group == -1:
                unresolved.add(key)
                warnings.append(f"Geometry at {pos} has an unresolved owner")
            paths.append(VectorPath(pos, owner_group, points, payload[required:], payload[4:point_offset]))
        elif record_id == 1 and kind >= 2:
            # Container child count; descendants are separate records.
            if size != 4 or not active_objects.get(kind - 1, (None,))[0] == 8:
                unresolved.add(key)
        elif record_id == 9 and kind >= 2:
            pass  # Retain uninterpreted group metadata, not extra vector points.
        elif record_id == 1999 and size == 1 and payload == b"\0":
            pass  # Observed zero marker; retained verbatim as a record.
        elif key not in KNOWN_RECORDS:
            unresolved.add(key)
        pos = end
    if family is None:
        raise PatternError("Missing family record")
    if declared_groups is not None and declared_groups != root_object_count:
        warnings.append(f"Declared root objects {declared_groups}; observed {root_object_count}")
    actual_counts = Counter(path.group for path in paths)
    for index, expected in enumerate(group_counts):
        if expected is not None and actual_counts[index] != expected:
            warnings.append(f"Group {index}: declared paths {expected}; observed {actual_counts[index]}")
    for record_id, kind in sorted(unresolved):
        warnings.append(f"Opaque record type id={record_id}, kind={kind}")
    return Pattern(source, hashlib.sha256(data).hexdigest(), len(data), family,
                   tuple(records), tuple(paths), declared_groups,
                   tuple(group_counts), tuple(warnings), serializer_version,
                   not unresolved and not any("declared" in warning.lower() for warning in warnings),
                   tuple(sorted(unresolved)))


def read_pattern(source: Path) -> Pattern:
    with source.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    return parse_bytes(data, str(source.resolve()))


def path_commands(path: VectorPath) -> str:
    """Render the inferred move/control/control/end/line point vocabulary.

    Unexpected sequences fail rather than silently replacing curves with lines.
    The second ushort is retained in JSON and does not alter coordinates.
    """
    result: list[str] = []
    points = path.points
    i = 0
    have_start = False
    def xy(point: tuple[float, float, int, int]) -> str:
        return f"{point[0]:.12g},{point[1]:.12g}"
    while i < len(points):
        point = points[i]
        kind = point[2]
        if kind == 0:
            result.append("M" + xy(point))
            have_start = True
            i += 1
        elif not have_start:
            raise PatternError(f"Path at {path.record_offset} lacks an initial move")
        elif kind == 4:
            result.append("L" + xy(point))
            i += 1
        elif kind == 1:
            if i + 2 >= len(points) or [p[2] for p in points[i:i + 3]] != [1, 2, 3]:
                raise PatternError(f"Incomplete cubic at {path.record_offset}, point {i}")
            result.append("C" + " ".join(xy(p) for p in points[i:i + 3]))
            i += 3
        else:
            raise PatternError(f"Unsupported point sequence at {path.record_offset}, point {i}: type {kind}")
    return " ".join(result)


def summary(pattern: Pattern, include_points: bool = False) -> dict:
    result = {
        "source": pattern.source, "sha256": pattern.sha256,
        "bytes": pattern.byte_count, "family": pattern.family,
        "serializer_version": pattern.serializer_version,
        "complete_geometry": pattern.complete_geometry,
        "unresolved_records": [list(key) for key in pattern.unresolved_records],
        "exact_record_framing": True, "records": len(pattern.records),
        "declared_groups": pattern.declared_groups,
        "observed_groups": len(pattern.group_path_counts),
        "declared_paths_per_group": list(pattern.group_path_counts),
        "paths": len(pattern.paths), "points": sum(len(p.points) for p in pattern.paths),
        "units": POSTSCRIPT_POINT_NAME, "mm_per_source_unit": MM_PER_POSTSCRIPT_POINT,
        "curve_semantics": "inferred from observed point sequences",
        "knot_flag_semantics": "unknown; retained without interpretation",
        "warnings": list(pattern.warnings),
        "record_types": dict(Counter(f"{r.id}:{r.kind}" for r in pattern.records)),
        "opaque_geometry_trailer_bytes": dict(Counter(len(p.opaque_trailer) for p in pattern.paths)),
    }
    if include_points:
        result["vector_paths"] = [{"record_offset": p.record_offset, "group": p.group,
            "points": [{"x": x, "y": y, "type": kind, "flag": flag}
                       for x, y, kind, flag in p.points],
            "opaque_trailer_hex": p.opaque_trailer.hex(),
            "geometry_header_hex": p.geometry_header.hex()} for p in pattern.paths]
    return result


def export_svg(pattern: Pattern, output: Path, y_axis: str = "up", allow_partial: bool = False) -> None:
    ensure_output(output, Path(pattern.source).resolve().parent, ".svg")
    if not pattern.complete_geometry and not allow_partial:
        raise PatternError("Unresolved drawing records: export requires explicit allow_partial=True or --allow-partial")
    coordinates = [(x, y) for path in pattern.paths for x, y, _, _ in path.points]
    if not coordinates:
        raise PatternError("No vector coordinates")
    commands = [path_commands(path) for path in pattern.paths]
    xmin, xmax = min(p[0] for p in coordinates), max(p[0] for p in coordinates)
    ymin, ymax = min(p[1] for p in coordinates), max(p[1] for p in coordinates)
    margin = max(xmax - xmin, ymax - ymin, 1) * .025
    viewbox = f"{xmin-margin:.12g} {ymin-margin:.12g} {max(xmax-xmin,1)+2*margin:.12g} {max(ymax-ymin,1)+2*margin:.12g}"
    stroke = max(xmax - xmin, ymax - ymin, 1) * .001
    transform = f' transform="translate(0 {ymin+ymax:.12g}) scale(1 -1)"' if y_axis == "up" else ""
    metadata = {"source_sha256": pattern.sha256, "coordinate_units": POSTSCRIPT_POINT_NAME,
                "mm_per_source_unit": MM_PER_POSTSCRIPT_POINT,
                "svg_units": "viewBox user units; raw source coordinates",
                "physical_dimensions_set": False, "physical_scale_applied": False,
                "physical_cutter_verified": False,
                "y_axis": y_axis, "point_roles": "inferred", "warnings": pattern.warnings,
                "complete_geometry": pattern.complete_geometry,
                "purpose": "inspection preview; no physical SVG size is set; physical cutter output remains unverified"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">',
             f'<title>{escape(Path(pattern.source).name)}</title>',
             '<metadata>' + escape(json.dumps(metadata)) + '</metadata>',
             f'<rect x="{xmin-margin:.12g}" y="{ymin-margin:.12g}" width="{max(xmax-xmin,1)+2*margin:.12g}" height="{max(ymax-ymin,1)+2*margin:.12g}" fill="white"/>',
             f'<g fill="none" stroke="#152b45" stroke-width="{stroke:.12g}"{transform}>']
    parts.extend(f'<path data-group="{path.group}" d="{command}"/>'
                 for path, command in zip(pattern.paths, commands))
    parts.extend(['</g>', '</svg>'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(parts), encoding="utf-8")


def ensure_output(output: Path, source_root: Path, suffix: str) -> None:
    if output.suffix.lower() != suffix:
        raise PatternError(f"Output must use the {suffix} extension")
    if output.resolve().is_relative_to(source_root.resolve()):
        raise PatternError("Output must be outside the source directory")


def scan_directory(root: Path) -> dict:
    counts = Counter()
    point_types = Counter()
    trailers = Counter()
    record_types = Counter()
    families = Counter()
    problems = []
    warnings = []
    total_bytes = 0
    for source in sorted(root.iterdir()):
        if not source.is_file() or source.suffix.upper() not in (".FDU", ".FDA"):
            continue
        counts["files"] += 1
        counts[source.suffix.upper()] += 1
        try:
            pattern = read_pattern(source)
            total_bytes += pattern.byte_count
            counts["exact_framing"] += 1
            families[str(pattern.family)] += 1
            counts["complete_geometry" if pattern.complete_geometry else "partial_geometry"] += 1
            record_types.update(f"{r.id}:{r.kind}" for r in pattern.records)
            counts["groups"] += len(pattern.group_path_counts)
            counts["paths"] += len(pattern.paths)
            sequence_errors = []
            for path in pattern.paths:
                counts["points"] += len(path.points)
                trailers[len(path.opaque_trailer)] += 1
                point_types.update(f"{kind}:{flag}" for _, _, kind, flag in path.points)
                try:
                    path_commands(path)
                except PatternError as error:
                    sequence_errors.append(str(error))
            if pattern.warnings:
                warnings.append({"source": str(source), "warnings": list(pattern.warnings)})
            if sequence_errors:
                problems.append({"source": str(source), "errors": sequence_errors})
            else:
                counts["svg_sequences_supported"] += 1
        except (OSError, PatternError, struct.error) as error:
            problems.append({"source": str(source), "error": str(error)})
    return {"root": str(root.resolve()), "source_mode": "read-only",
            "counts": dict(counts), "bytes_parsed": total_bytes,
            "families": dict(families), "record_types": dict(record_types),
            "point_type_and_flag": dict(point_types),
            "geometry_trailer_byte_counts": dict(trailers),
            "problem_files": problems, "warning_files": warnings,
            "limits": {"max_file_bytes": MAX_BYTES, "units": POSTSCRIPT_POINT_NAME,
                       "mm_per_source_unit": MM_PER_POSTSCRIPT_POINT,
                       "point_roles": "inferred; no runtime or native decoder comparison"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("--points", action="store_true")
    export = sub.add_parser("export-svg")
    export.add_argument("source", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--json", type=Path)
    export.add_argument("--y-axis", choices=("up", "down"), default="up")
    export.add_argument("--allow-partial", action="store_true")
    scan = sub.add_parser("scan")
    scan.add_argument("directory", type=Path)
    scan.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "scan":
            ensure_output(args.output, args.directory, ".json")
            result = scan_directory(args.directory)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps({"counts": result["counts"], "problem_files": len(result["problem_files"]),
                              "warning_files": len(result["warning_files"]), "output": str(args.output)}))
        else:
            pattern = read_pattern(args.source)
            if args.command == "inspect":
                print(json.dumps(summary(pattern, args.points), indent=2))
            else:
                export_svg(pattern, args.output, args.y_axis, args.allow_partial)
                if args.json:
                    ensure_output(args.json, args.source.resolve().parent, ".json")
                    args.json.parent.mkdir(parents=True, exist_ok=True)
                    args.json.write_text(json.dumps(summary(pattern, True), indent=2), encoding="utf-8")
                print(json.dumps({"svg": str(args.output), "source_sha256": pattern.sha256,
                                  "paths": len(pattern.paths), "warnings": pattern.warnings}))
        return 0
    except (OSError, PatternError, struct.error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
