# FDA/FDU inspection and SVG preview

`fvd_pattern.py` reads the supplied local pattern cache without loading vendor code,
attaching to a process, or modifying the source. It uses only Python's standard
library. Outputs must be outside the source directory.

```powershell
python tools\patterns\fvd_pattern.py inspect "data\patterns\PATTERN.FDU" --points
python tools\patterns\fvd_pattern.py export-svg "data\patterns\PATTERN.FDU" "exports\pattern.svg" --json "exports\pattern.json"
python tools\patterns\fvd_pattern.py scan "data\patterns" "analysis\pattern-validation.json"
python -m unittest discover -s tools\patterns -p "test_*.py" -v
```

Run these from the project root after extracting the separate data ZIP. Replace
`PATTERN.FDU` with an existing FDA/FDU filename. Ordinary viewer use is described
in the root README; this CLI does not submit jobs to hardware.

The parser recognizes the observed 18-byte serializer prefixes (versions 23 and
30) and walks records with a little-endian `<IIH` header: record ID, payload byte
count, and nesting depth (named `kind` in the initial parser API). A
record must fit completely, and the stream must finish exactly at EOF. Files
larger than 16 MiB are rejected rather than loaded without a bound.

Observed records:

| ID | Kind | Observed payload |
| --- | --- | --- |
| 250 | 0 | Four-byte family: 1, 2 or 4; extension alone is not a strict family identifier |
| 0 | 0 | Thumbnail block with width, height, bit depth and LEAD image bytes |
| 70, 49 | 0 | Application/document settings and descriptive metadata |
| 34 | 0 | Eight bytes; meaning unresolved |
| 1 | 0 | Four-byte declared group count |
| 5 | 1 | Group start |
| 4 | 2 | Two-byte declared number of vector paths in group |
| 3 | 3 or deeper | Point count, point array and optional opaque trailer |
| 63 | 3 or deeper | Version 30 point count, 34-byte bounds/flag header, point array |
| 8 | 1 or deeper | Plain nested group; child count follows in ID 1 at next depth |
| 9 | 2 | Group metadata; often 260 bytes |
| 2 | 0 | Final eight-byte block; meaning unresolved |

Nested curve groups are included. Primitive object types whose drawing behavior
is unresolved (for example, text and rectangle-like records) mark the result as
`complete_geometry: false`. Ordinary SVG export refuses these files. An explicit
`--allow-partial` or `allow_partial=True` permits an inspection preview of the
decoded subset, with the partial status retained in metadata. Complete here means
all recognized drawing record structures were decoded with consistent counts;
it does not establish native-rendering or cutting equivalence.

Each point is `<ddHH`: x and y doubles followed by type and knot flag. The
observed sequences strongly support type 0 = move, type 4 = line endpoint,
and consecutive types 1, 2, 3 = cubic Bezier control 1, control 2, endpoint.
Unexpected sequences fail SVG conversion instead of degrading into polygons.
The second ushort is preserved verbatim, because its editing semantics remain
unknown. Declared point arrays and optional extra trailer bytes are kept
separate; extra bytes are not guessed to be additional points.

Inspected native FDA/FDU path coordinates use PostScript points: 1/72 inch, or
25.4/72 millimeters per source unit. The native serializer/conversion evidence
and original-application dimension check are described in
[the pattern units note](../../docs/pattern-units.md). The cutting planner uses
this conversion automatically for supported complete native patterns.

SVG output remains an inspection preview in raw viewBox user units. It sets no
physical width or height and applies no conversion to millimeters or inches.
Metadata records the known source convention and that no physical SVG sizing
was applied. The default Cartesian y-axis is an inference (`--y-axis down` omits
inversion), and physical cutting has not validated the result. Unknown
record types generate warnings, and their contents are not interpreted. Bounding
boxes include control points, so they can be larger than the visible curves.
No line joining, curve simplification, scaling to inches/mm, or cut commands are
performed.

The JSON preview includes the original SHA-256, all decoded point/type/flag
values, group numbers, record byte offsets, opaque trailer hex and explicit
limitations. Original files remain the authority for future interpretation.
