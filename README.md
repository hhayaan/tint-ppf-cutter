# Tint / PPF pattern investigation

An offline desktop catalog browser and inspection toolkit for the locally installed Film Designs 4 data. The current application provides catalog search, local vector previews, and SVG inspection exports. It is an initial reconstruction, with further editing and cutter behavior still to implement and validate.

## Run

Double-click `start.cmd`. The launcher uses a Python installation with Tkinter, including the existing Codex bundled runtime when the regular Python lacks it. The viewer uses the Python standard library. The default catalog is `analysis/catalog.sqlite`, and the default pattern folder is this project's `data/patterns` snapshot. If that folder is absent, it falls back to `C:\FilmVinylDesigns\ClientV2\Files`. Both can be selected in the application or overridden using command-line options shown by `python app.py --help`.

The SQLite catalog and source pattern files are opened read-only. SVG exports go to `exports` and contain geometric inspection previews. Physical scale, knot flags, object effects, and cutter output require further verification before production use.

## Evidence and reproducibility

- `analysis/snapshots/fvd.mdb` is a stable copy of the active source catalog. Its receipt records SHA-256 and the deny-writers handle used for copying.
- `analysis/catalog-export.json` records the metadata migration and validation results.
- `analysis/catalog-file-coverage.json` verifies that all 143,321 catalog rows link to cached files through `FDImageId`, with 48,021 distinct referenced assets.
- `analysis/pattern-cache-snapshot.json` records the independent local copy of all 48,177 FDA/FDU files and per-file hashes.
- `analysis/pattern-auxiliary-snapshot.json` preserves the 100 compatibility warnings from the original `warnings.json`. The viewer displays these notes by geometry asset ID.
- `analysis/data-completeness-summary.json`, `catalog-parity-verification.json`, and `pattern-parity-verification.json` record the fresh source-to-copy comparison. Every cached file matches, and all 6,217,425 migrated metadata values match the native export.
- `analysis/pattern-validation.json` records full-cache parsing: 47,834 structurally supported files and 343 explicitly partial files. Matching every stored record does not establish physical cutting equivalence.
- `analysis/file-inventory.json` and `analysis/storage-findings.md` map the supplied installation and data locations.
- `analysis/manual-feature-map.md` links documented behavior to the public manual.
- `analysis/rea-runtime-evidence.json` retains REA observations, including the editor's successful file opens of the active catalog.
- `analysis/pattern-previews` contains representative SVG, JSON, and PNG inspections.
- `tools/catalog` contains snapshot-only catalog inspection/export utilities and the raw pattern-cache copy utility. The final metadata export uses Microsoft's native ACE reader. The Jet header helper recovers the stored local database password in memory; it does not guess passwords, alter the MDB, or print the value.
- `tools/patterns/fvd_pattern.py` parses local pattern files without loading vendor code. Run its `--help` for inspection, SVG export, and cache validation commands.

Private data, snapshots, catalog exports, and geometry files are ignored by Git. No vendor account, update endpoint, chat, feedback, upload service, or physical cutter is needed by the viewer.

## Validation

The catalog passes SQLite integrity checking, native-reader semantic checks, and exact asset-to-file coverage checks. Twenty-two automated tests cover catalog filtering, compatibility notes, read-only access, path handling, curve reconstruction, malformed data, nested groups, and partial-export protection. The desktop viewer was also checked visually with an Acura ADX pattern, and its SVG export was opened and rendered.

Run `python -m unittest discover -s tests -v` and `python -m unittest discover -s tools/patterns -p "test*.py" -v`. Headless tests work without Tkinter.

For a fresh read-only completeness check, run `python tools/catalog/verify_catalog_parity.py` and `python tools/catalog/verify_pattern_parity.py`. The latter compares every FDA/FDU file and the compatibility-warning JSON against the original cache and the saved copy receipts.

## Remaining reconstruction work

Confirm geometric units and orientation against the editor, resolve any unsupported object records, and implement document editing and undo. Cutting requires independent validation of dimensions, path closure, tool order, offsets, paneling, nesting, transport, and plotter-specific commands. Pricing, user roles, reporting, custom pattern organization, and server-backed workflows also need implementation and behavioral checks.
