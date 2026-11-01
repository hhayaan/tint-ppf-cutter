# Native pattern physical size

Supported native FDA/FDU geometry uses **PostScript points: one source unit is 1/72 inch, or 25.4/72 millimeters**. Pattern jobs use this conversion automatically at 100% scale. The custom conversion remains an advanced override. Synthetic test rectangles are already in millimeters.

[SVG inspection exports](../tools/patterns/README.md) keep raw source coordinates
in viewBox user units. Their metadata declares this known source convention,
but the exporter sets no physical width/height and applies no physical sizing.
Use the cutting planner for automatic millimeter conversion.

The conversion is supported by independent evidence from the installed original software:

- `gslibcad62.dll!TTrajecte::Llegir` at RVA `0x5da30` reads both legacy geometry record ID 3 and newer ID 63. It selects the appropriate payload header and copies the serialized x/y double bits directly into native path points, with no coordinate multiplier.
- `fdthmb40.dll!FD_GetPatternSize` at RVA `0x1c30` loads the original native object list and returns its width and height multiplied by `1/72`.
- `gsfwkcut62.dll!TPlotterHPGL::EPStoPLT` and its base converter multiply source coordinates by `25.4/72` before the model's device-units-per-millimeter scale. This is separate from the cutter's HP-GL or DMPL command units.
- `gslib62.dll!unit2eps` at RVA `0x10ba0` converts millimeters with `72/25.4`, centimeters with `10*72/25.4`, inches with `72`, and feet with `864`.
- In the original app at 100% scale, a selected 65-node curve displayed position `17.35 cm, 0.00 cm` and dimensions `130.79 cm × 43.06 cm`. Our exact cubic extrema, converted with `25.4/72`, give `17.34741945 cm, 0 cm` and `130.79320331 cm × 43.06128713 cm`, matching every displayed value after rounding.

The native deserializer handles serializer versions 23 and 30 through the same point vocabulary. `printing.units.resolve_pattern_units()` accepts parsed native patterns with known versions/families and complete decoded geometry. File extensions and vehicle dimensions do not determine the scale. Unknown versions, other import formats, empty drawings, and incomplete geometry do not receive an automatic cutting scale.

The full local scan parsed 48,177 patterns: 47,834 pass automatic unit admission and 343 remain blocked by incomplete geometry. Both FDA and FDU files are admitted. All 39 stored version-30 patterns are among the blocked files because of additional drawing structures; recovering their point units does not enable their incomplete drawings for cutting.

This establishes the original software's physical coordinate convention. It does not establish that every stored vehicle pattern fits its intended vehicle, that opaque drawing records are implemented, or that a particular cutter's loaded media, origin, command language, calibration, and condition settings are correct. Original UI corroboration is a representative runtime check, not a survey of all vehicle dimensions. The size-API and serializer findings above are static native analysis; successful runtime corroboration comes from the original running application's UI and the separately verified offline command-capture path.

The full local cache admission report is retained privately in `analysis/pattern-unit-admission.json`, alongside the complete REA/Ghidra evidence for the named functions. No pattern geometry or vendor binary is included in this document.
