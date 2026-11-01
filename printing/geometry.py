"""Pure, calibrated cut-job geometry preparation. This module performs no I/O.

Inspected native FDU/FDA path coordinates use PostScript points. The caller
supplies millimeters per source unit, using printing.units for automatic native
pattern conversion or an explicit conversion for other geometry. Knot flags
never imply closure: only equal endpoints do.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
import math
from numbers import Real
from typing import Any

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]


class GeometryError(ValueError):
    """The complete requested job cannot be prepared within its constraints."""


@dataclass(frozen=True)
class JobOptions:
    mm_per_source_unit: float
    scale: float = 1.0
    flatten_tolerance_mm: float = 0.05
    selected_path_indices: tuple[int, ...] | None = None
    inner_first: bool = True
    order: str = "unsorted"
    rotation_degrees: int = 0
    mirror_x: bool = False
    mirror_y: bool = False
    copies: int = 1
    copy_columns: int = 1
    copy_spacing_mm: float = 0.0
    weed_border_mm: float | None = None
    passes: int = 1
    media_width_mm: float | None = None
    media_height_mm: float | None = None
    position_mode: str = "origin"
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    max_points: int = 1_000_000
    max_subdivisions: int = 1_000_000
    max_subdivision_depth: int = 32
    max_polylines: int = 100_000
    max_ordering_work: int = 5_000_000


@dataclass(frozen=True)
class Polyline:
    points_mm: tuple[Point, ...]
    closed: bool
    path_index: int | None
    subpath_index: int | None
    group: int | None
    copy_index: int | None = 0
    pass_index: int = 0
    role: str = "cut"

    @property
    def points(self) -> tuple[Point, ...]:
        """Compatibility alias; every coordinate is still explicitly in mm."""
        return self.points_mm

    @property
    def bounds_mm(self) -> Bounds:
        return _point_bounds(self.points_mm)

    @property
    def length_mm(self) -> float:
        return _sum_finite(math.hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(self.points_mm, self.points_mm[1:]))


@dataclass(frozen=True)
class PreparedJob:
    polylines: tuple[Polyline, ...]
    width_mm: float
    height_mm: float
    bounds_mm: Bounds
    polyline_bounds_mm: Bounds
    point_count: int
    total_length_mm: float
    travel_length_mm: float
    flatten_tolerance_mm: float
    pass_count: int
    copy_count: int
    selected_path_indices: tuple[int, ...]
    source_bounds: Bounds
    source_sha256: str | None
    options: JobOptions

    @property
    def polyline_count(self) -> int:
        return len(self.polylines)

    def preview_data(self) -> dict:
        """Deterministic JSON-compatible geometry, with no protocol commands."""
        return {
            "units": "mm", "width_mm": self.width_mm, "height_mm": self.height_mm,
            "bounds_mm": list(self.bounds_mm), "point_count": self.point_count,
            "position_mode": self.options.position_mode,
            "applied_offset_mm": list(self.bounds_mm[:2]),
            "flatten_tolerance_mm": self.flatten_tolerance_mm,
            "copy_count": self.copy_count, "pass_count": self.pass_count,
            "source_sha256": self.source_sha256,
            "paths": [{"points_mm": [list(point) for point in line.points_mm],
                       "closed": line.closed, "path_index": line.path_index,
                       "subpath_index": line.subpath_index, "group": line.group,
                       "copy_index": line.copy_index, "pass_index": line.pass_index,
                       "role": line.role} for line in self.polylines],
        }


@dataclass(frozen=True)
class _Subpath:
    start: Point
    segments: tuple[tuple, ...]
    path_index: int
    subpath_index: int
    group: int | None


class _Budget:
    def __init__(self, options):
        self.options = options
        self.points = 0
        self.subdivisions = 0
        self.ordering_work = 0

    def point(self):
        self.points += 1
        if self.points > self.options.max_points:
            raise GeometryError("Flattened point budget exceeded")

    def subdivision(self):
        self.subdivisions += 1
        if self.subdivisions > self.options.max_subdivisions:
            raise GeometryError("Cubic subdivision work budget exceeded")

    def ordering(self):
        self.ordering_work += 1
        if self.ordering_work > self.options.max_ordering_work:
            raise GeometryError("Nesting/ordering work budget exceeded")


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise GeometryError(f"{name} must be a finite number")
    try:
        value = float(value)
    except (OverflowError, ValueError) as error:
        raise GeometryError(f"{name} must be a finite number") from error
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        qualifier = "positive finite" if positive else "nonnegative finite" if nonnegative else "finite"
        raise GeometryError(f"{name} must be {qualifier}")
    return value


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GeometryError(f"{name} must be a positive integer")
    return value


def _validate_options(options):
    if not isinstance(options, JobOptions):
        raise GeometryError("JobOptions with explicit mm_per_source_unit is required")
    for name in ("mm_per_source_unit", "scale", "flatten_tolerance_mm"):
        _number(getattr(options, name), name, positive=True)
    _number(options.mm_per_source_unit*options.scale, "combined physical scale", positive=True)
    _number(options.copy_spacing_mm, "copy_spacing_mm", nonnegative=True)
    if options.weed_border_mm is not None:
        _number(options.weed_border_mm, "weed_border_mm", nonnegative=True)
    for name in ("media_width_mm", "media_height_mm"):
        if getattr(options, name) is not None:
            _number(getattr(options, name), name, positive=True)
    if options.position_mode not in ("origin", "manual"):
        raise GeometryError("position_mode must be origin or manual; design-position semantics are not established")
    for name in ("offset_x_mm", "offset_y_mm"):
        _number(getattr(options, name), name, nonnegative=True)
    for name in ("copies", "copy_columns", "passes", "max_points", "max_subdivisions", "max_subdivision_depth", "max_polylines", "max_ordering_work"):
        _integer(getattr(options, name), name)
    if isinstance(options.rotation_degrees, bool) or options.rotation_degrees not in (0, 90, 180, 270):
        raise GeometryError("rotation_degrees must be 0, 90, 180 or 270")
    for name in ("mirror_x", "mirror_y", "inner_first"):
        if not isinstance(getattr(options, name), bool):
            raise GeometryError(f"{name} must be boolean")
    if options.order not in ("unsorted", "start-nearest", "end-nearest"):
        raise GeometryError("order must be unsorted, start-nearest or end-nearest")


def _selection(pattern, indices):
    if getattr(pattern, "complete_geometry", False) is not True or getattr(pattern, "unresolved_records", ()):
        raise GeometryError("Complete geometry is required; partial/opaque geometry cannot become a cut job")
    paths = getattr(pattern, "paths", ())
    if not paths:
        raise GeometryError("Pattern has no vector paths")
    try:
        selected = tuple(range(len(paths))) if indices is None else tuple(indices)
    except TypeError as error:
        raise GeometryError("selected_path_indices must be a sequence") from error
    if not selected:
        raise GeometryError("Select at least one path")
    if any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(paths) for index in selected):
        raise GeometryError("Selected path index is invalid")
    if len(set(selected)) != len(selected):
        raise GeometryError("Duplicate selected path indices are not allowed")
    return paths, selected


def _read_subpaths(pattern, indices=None, max_points=1_000_000):
    paths, selected = _selection(pattern, indices)
    try:
        source_points = sum(len(paths[index].points) for index in selected)
    except (TypeError, AttributeError) as error:
        raise GeometryError("Selected path lacks a source point sequence") from error
    if source_points > max_points:
        raise GeometryError("Source point budget exceeded")
    result = []
    for path_index in selected:
        path = paths[path_index]
        points = path.points
        if not points:
            raise GeometryError(f"Selected path {path_index} is empty")
        normalized = []
        for point in points:
            if not isinstance(point, (tuple, list)) or len(point) != 4:
                raise GeometryError("A source knot must contain x, y, type and opaque flag")
            x, y = _number(point[0], "source x"), _number(point[1], "source y")
            kind = point[2]
            if isinstance(kind, bool) or not isinstance(kind, int):
                raise GeometryError("Source point type must be an integer")
            normalized.append((x, y, kind))  # Fourth field is deliberately uninterpreted.
        start = None
        segments = []
        subpath_index = 0
        index = 0
        def finish():
            nonlocal subpath_index
            if start is None or not segments:
                raise GeometryError(f"Path {path_index} contains a move-only/empty subpath")
            result.append(_Subpath(start, tuple(segments), path_index, subpath_index, getattr(path, "group", None)))
            subpath_index += 1
        while index < len(normalized):
            x, y, kind = normalized[index]
            if kind == 0:
                if start is not None:
                    finish()
                start, segments = (x, y), []
                index += 1
            elif start is None:
                raise GeometryError(f"Path {path_index} lacks an initial move")
            elif kind == 4:
                segments.append(("L", (x, y)))
                index += 1
            elif kind == 1:
                if index+2 >= len(normalized) or [point[2] for point in normalized[index:index+3]] != [1, 2, 3]:
                    raise GeometryError(f"Path {path_index} has an incomplete cubic sequence")
                segments.append(("C", (x, y), normalized[index+1][:2], normalized[index+2][:2]))
                index += 3
            else:
                raise GeometryError(f"Path {path_index} has unsupported point type {kind}")
        finish()
    return tuple(result), selected


def _point_bounds(points):
    points = tuple(points)
    if not points:
        raise GeometryError("No points")
    values = (min(point[0] for point in points), min(point[1] for point in points),
              max(point[0] for point in points), max(point[1] for point in points))
    if not all(math.isfinite(value) for value in values) or not all(math.isfinite(values[index+2]-values[index]) for index in (0, 1)):
        raise GeometryError("Coordinate extent is nonfinite/oversized")
    return values


def _lerp(a, b, t):
    return ((1-t)*a[0]+t*b[0], (1-t)*a[1]+t*b[1])


def _cubic_point(points, t):
    a, b, c = (_lerp(points[index], points[index+1], t) for index in range(3))
    return _lerp(_lerp(a, b, t), _lerp(b, c, t), t)


def _extrema(values):
    scale = max(abs(value) for value in values)
    if scale == 0:
        return ()
    p0, p1, p2, p3 = (value/scale for value in values)
    a = -p0+3*p1-3*p2+p3
    b = 2*(p0-2*p1+p2)
    c = p1-p0
    if a == 0:
        return (-c/b,) if b != 0 and 0 < -c/b < 1 else ()
    discriminant = b*b-4*a*c
    if discriminant < 0:
        return ()
    root = math.sqrt(discriminant)
    q = -0.5*(b+math.copysign(root, b))
    roots = (q/a, c/q) if q != 0 else (-b/(2*a),)
    return tuple(value for value in roots if 0 < value < 1)


def _subpath_bounds(subpaths):
    points = []
    for path in subpaths:
        current = path.start
        points.append(current)
        for segment in path.segments:
            if segment[0] == "C":
                curve = (current, *segment[1:])
                for axis in (0, 1):
                    points.extend(_cubic_point(curve, t) for t in _extrema(tuple(point[axis] for point in curve)))
            current = segment[-1]
            points.append(current)
    return _point_bounds(points)


def source_bounds(pattern, selected_path_indices=None) -> Bounds:
    """Exact line/cubic extrema in raw source units, for explicit calibration."""
    subpaths, selected = _read_subpaths(pattern, selected_path_indices)
    return _subpath_bounds(subpaths)


def _transform(point, options):
    factor = options.mm_per_source_unit*options.scale
    x, y = point[0]*factor, point[1]*factor
    if options.mirror_x: x = -x
    if options.mirror_y: y = -y
    rotation = options.rotation_degrees
    if rotation == 90: x, y = -y, x
    elif rotation == 180: x, y = -x, -y
    elif rotation == 270: x, y = y, -x
    if not math.isfinite(x) or not math.isfinite(y):
        raise GeometryError("Scaled coordinates are nonfinite/oversized")
    return (0.0 if x == 0 else x, 0.0 if y == 0 else y)


def _distance_to_segment(point, start, end):
    dx, dy = end[0]-start[0], end[1]-start[1]
    length = math.hypot(dx, dy)
    if not math.isfinite(length):
        raise GeometryError("Coordinate extent is too large for flattening")
    if length == 0:
        result = math.hypot(point[0]-start[0], point[1]-start[1])
    else:
        ux, uy = dx/length, dy/length
        projection = (point[0]-start[0])*ux+(point[1]-start[1])*uy
        projection = max(0.0, min(length, projection))
        result = math.hypot(point[0]-(start[0]+projection*ux), point[1]-(start[1]+projection*uy))
    if not math.isfinite(result):
        raise GeometryError("Coordinate extent is too large for flattening")
    return result


def _flatten_cubic(curve, tolerance, budget):
    """Control-hull distance to each finite chord bounds geometric deviation."""
    result = []
    stack = [(curve, 0)]
    while stack:
        points, depth = stack.pop()
        budget.subdivision()
        if max(_distance_to_segment(points[1], points[0], points[3]),
               _distance_to_segment(points[2], points[0], points[3])) <= tolerance:
            budget.point()
            result.append(points[3])
            continue
        if depth >= budget.options.max_subdivision_depth:
            raise GeometryError("Subdivision depth budget cannot meet flatten tolerance")
        a, b, c = (_lerp(points[index], points[index+1], 0.5) for index in range(3))
        d, e = _lerp(a, b, 0.5), _lerp(b, c, 0.5)
        middle = _lerp(d, e, 0.5)
        stack.append(((middle, e, c, points[3]), depth+1))
        stack.append(((points[0], a, d, middle), depth+1))
    return result


def _flatten(path, options, budget):
    current = path.start
    points = [current]
    budget.point()
    for segment in path.segments:
        if segment[0] == "L":
            points.append(segment[1])
            budget.point()
        else:
            points.extend(_flatten_cubic((current, *segment[1:]), options.flatten_tolerance_mm, budget))
        current = segment[-1]
    if not any(a != b for a, b in zip(points, points[1:])):
        raise GeometryError("Subpath has no measurable cut geometry at this tolerance; lower the tolerance")
    return Polyline(tuple(points), points[0] == points[-1], path.path_index, path.subpath_index, path.group)


def _cross(a, b, c):
    value = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    if not math.isfinite(value):
        raise GeometryError("Coordinate extent is too large for nesting")
    return value


def _on_segment(point, a, b):
    return _cross(a, b, point) == 0 and min(a[0], b[0]) <= point[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= point[1] <= max(a[1], b[1])


def _intersects(a, b, c, d, budget):
    budget.ordering()
    x, y, z, w = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    return ((x == 0 and _on_segment(c, a, b)) or (y == 0 and _on_segment(d, a, b))
            or (z == 0 and _on_segment(a, c, d)) or (w == 0 and _on_segment(b, c, d))
            or ((x > 0) != (y > 0) and (z > 0) != (w > 0)))


def _strictly_inside(point, polygon, budget):
    inside = False
    for a, b in zip(polygon, polygon[1:]):
        budget.ordering()
        if _on_segment(point, a, b):
            return False
        if (a[1] > point[1]) != (b[1] > point[1]):
            intersection = a[0]+(point[1]-a[1])/(b[1]-a[1])*(b[0]-a[0])
            if point[0] < intersection:
                inside = not inside
    return inside


def _dependencies(lines, enabled, budget):
    dependencies = [set() for line in lines]
    if not enabled:
        return dependencies
    bounds = [line.bounds_mm for line in lines]
    for outer_index, outer in enumerate(lines):
        if not outer.closed or len(outer.points_mm) < 4:
            continue
        ob = bounds[outer_index]
        for inner_index, inner in enumerate(lines):
            if inner_index == outer_index:
                continue
            budget.ordering()
            ib = bounds[inner_index]
            if not (ob[0] <= ib[0] and ob[1] <= ib[1] and ob[2] >= ib[2] and ob[3] >= ib[3]) or ob == ib:
                continue
            if not all(_strictly_inside(point, outer.points_mm, budget) for point in inner.points_mm):
                continue
            if any(_intersects(a, b, c, d, budget) for a, b in zip(inner.points_mm, inner.points_mm[1:]) for c, d in zip(outer.points_mm, outer.points_mm[1:])):
                continue
            dependencies[outer_index].add(inner_index)
    return dependencies


def _ordered(lines, dependencies, mode, budget):
    remaining = [len(items) for items in dependencies]
    followers = [[] for line in lines]
    for index, items in enumerate(dependencies):
        for dependency in items:
            followers[dependency].append(index)
    eligible = {index for index, count in enumerate(remaining) if count == 0}
    source_order = list(eligible)
    heapq.heapify(source_order)
    result = []
    cursor = (0.0, 0.0)
    while eligible:
        reverse = False
        if mode == "unsorted":
            index = heapq.heappop(source_order)
        else:
            candidates = []
            for candidate in sorted(eligible):
                budget.ordering()
                line = lines[candidate]
                start = math.hypot(line.points_mm[0][0]-cursor[0], line.points_mm[0][1]-cursor[1])
                end = math.hypot(line.points_mm[-1][0]-cursor[0], line.points_mm[-1][1]-cursor[1])
                flipped = mode == "end-nearest" and not line.closed and end < start
                candidates.append((end if flipped else start, candidate, flipped))
            distance, index, reverse = min(candidates)
        eligible.remove(index)
        line = lines[index]
        if reverse:
            line = replace(line, points_mm=tuple(reversed(line.points_mm)))
        result.append(line)
        cursor = line.points_mm[-1]
        for follower in followers[index]:
            remaining[follower] -= 1
            if remaining[follower] == 0:
                eligible.add(follower)
                if mode == "unsorted":
                    heapq.heappush(source_order, follower)
    if len(result) != len(lines):
        raise GeometryError("Nesting constraints contain a cycle")
    return result


def _sum_finite(values):
    try:
        value = math.fsum(values)
    except OverflowError as error:
        raise GeometryError("Job length is nonfinite/oversized") from error
    if not math.isfinite(value):
        raise GeometryError("Job length is nonfinite/oversized")
    return value


def prepare_job(pattern: Any, options: JobOptions) -> PreparedJob:
    """Prepare all requested geometry or raise; never return a partial job.

    Rigid transforms follow positive calibration/scale; selected geometry is
    normalized to the origin. Copies use a row-major grid. One optional weed
    border encloses the whole grid and is cut last. Passes repeat the same
    ordered job. Start-nearest preserves direction; end-nearest may reverse open
    paths to use their nearer endpoint. Closed-path starts/winding are retained.
    A manual position translates the complete copy/weed layout after its rigid
    transforms; width/height remain artwork extents and bounds retain its actual
    placed coordinates. Media limits apply to those exact placed bounds.
    """
    _validate_options(options)
    source, selected = _read_subpaths(pattern, options.selected_path_indices, options.max_points)
    if len(source) > options.max_polylines:
        raise GeometryError("Source subpath budget exceeded")
    projected_lines = (len(source)*options.copies+(options.weed_border_mm is not None))*options.passes
    if projected_lines > options.max_polylines:
        raise GeometryError("Copies/passes exceed the polyline budget")
    raw_bounds = _subpath_bounds(source)
    transformed = tuple(replace(path, start=_transform(path.start, options),
        segments=tuple((segment[0], *(_transform(point, options) for point in segment[1:])) for segment in path.segments)) for path in source)
    bounds = _subpath_bounds(transformed)
    base_width, base_height = bounds[2]-bounds[0], bounds[3]-bounds[1]
    margin = 0.0 if options.weed_border_mm is None else options.weed_border_mm
    columns = min(options.copy_columns, options.copies)
    rows = (options.copies+columns-1)//columns
    pitch_x, pitch_y = base_width+options.copy_spacing_mm, base_height+options.copy_spacing_mm
    if (columns > 1 and pitch_x == 0) or (rows > 1 and pitch_y == 0):
        raise GeometryError("Copy spacing must separate zero-width/height geometry")
    width = base_width+(columns-1)*pitch_x+2*margin
    height = base_height+(rows-1)*pitch_y+2*margin
    if not math.isfinite(width) or not math.isfinite(height):
        raise GeometryError("Copy layout is nonfinite/oversized")
    if options.weed_border_mm is not None and (width == 0 or height == 0):
        raise GeometryError("Weed border needs nonzero area; increase its margin")
    origin_x = options.offset_x_mm if options.position_mode == "manual" else 0.0
    origin_y = options.offset_y_mm if options.position_mode == "manual" else 0.0
    placed_bounds = (origin_x, origin_y, origin_x+width, origin_y+height)
    if not all(math.isfinite(value) for value in placed_bounds):
        raise GeometryError("Positioned layout is nonfinite/oversized")
    if options.media_width_mm is not None and placed_bounds[2] > options.media_width_mm:
        raise GeometryError(f"Job right edge {placed_bounds[2]:g} mm exceeds media width {options.media_width_mm:g} mm")
    if options.media_height_mm is not None and placed_bounds[3] > options.media_height_mm:
        raise GeometryError(f"Job upper edge {placed_bounds[3]:g} mm exceeds media height {options.media_height_mm:g} mm")
    budget = _Budget(options)
    base = [_flatten(path, options, budget) for path in transformed]
    base = [replace(line, points_mm=tuple((point[0]-bounds[0], point[1]-bounds[1]) for point in line.points_mm)) for line in base]
    prototype_points = sum(len(line.points_mm) for line in base)*options.copies+(5 if options.weed_border_mm is not None else 0)
    if prototype_points*options.passes > options.max_points:
        raise GeometryError("Copies/passes exceed the final point budget")
    base_dependencies = _dependencies(base, options.inner_first, budget)
    lines = []
    dependencies = []
    for copy_index in range(options.copies):
        offset_x = (copy_index % columns)*pitch_x+margin
        offset_y = (copy_index // columns)*pitch_y+margin
        first = len(lines)
        for index, line in enumerate(base):
            lines.append(replace(line, points_mm=tuple((point[0]+offset_x, point[1]+offset_y) for point in line.points_mm), copy_index=copy_index))
            dependencies.append({first+dependency for dependency in base_dependencies[index]})
    if options.weed_border_mm is not None:
        border = ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height), (0.0, 0.0))
        dependencies.append(set(range(len(lines))))
        lines.append(Polyline(border, True, None, None, None, None, role="weed"))
    if origin_x or origin_y:
        lines = [replace(line, points_mm=tuple((x+origin_x, y+origin_y) for x, y in line.points_mm)) for line in lines]
    ordered = _ordered(lines, dependencies, options.order, budget)
    output = tuple(replace(line, pass_index=pass_index) for pass_index in range(options.passes) for line in ordered)
    total_length = _sum_finite(line.length_mm for line in output)
    cursor = (0.0, 0.0)
    travel = []
    for line in output:
        travel.append(math.hypot(line.points_mm[0][0]-cursor[0], line.points_mm[0][1]-cursor[1]))
        cursor = line.points_mm[-1]
    all_points = tuple(point for line in output for point in line.points_mm)
    return PreparedJob(output, width, height, placed_bounds, _point_bounds(all_points),
                       len(all_points), total_length, _sum_finite(travel), options.flatten_tolerance_mm,
                       options.passes, options.copies, selected, raw_bounds,
                       getattr(pattern, "sha256", None), options)
