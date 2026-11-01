# Read-only catalog migration

These are optional reconstruction and source-audit tools. A friend running the
viewer with the separate data ZIP does not need Microsoft ACE, original MDB
snapshots, or the original installation. See the root README for normal setup.
Use the linker's `--files data\patterns` option for the independent copy instead
of its original-installation default. Snapshot paths follow the checkout's
location; the PowerShell tools do not require a fixed development directory.

The final reader uses Microsoft ACE, not the experimental pure-Python page parser. Native ACE comparisons caught shifted text values in that parser, so it is not a supported export route.

Requirements: Python 3.11 or later, Windows PowerShell 5.1, and the already-installed 64-bit `Microsoft.ACE.OLEDB.12.0` provider. No third-party Python dependencies are required.

From the workspace:

```powershell
python tools\catalog\export_catalog.py
python tools\catalog\link_cached_files.py
```

To create an independent, verified local copy of the FDU/FDA cache, then point
the catalog's local file associations at that copy:

```powershell
python tools\catalog\snapshot_patterns.py
python tools\catalog\link_cached_files.py --files data\patterns
```

`snapshot_patterns.py` refuses an existing destination and creates only new
files under `data\patterns`. Every source is opened read-only, SHA256 is computed
while copying, and the destination is hashed independently. Source identity,
size, and modification time must remain unchanged. Symlinks/reparse points are
refused. `analysis\pattern-cache-snapshot.json` retains source provenance and
per-file hashes. This operation copies FDU/FDA geometry assets only.

The current local snapshot additionally includes `warnings.json`: 100
asset-ID-to-warning-text records. Its independent byte-for-byte copy receipt is
`analysis\pattern-auxiliary-snapshot.json`; the geometry manifest remains unchanged.
To independently rehash all current source/cache files and compare them with
both receipts:

```powershell
python tools\catalog\verify_pattern_parity.py
```

The verifier reads every file on both sides, checks exact filename sets and
sizes, verifies each SHA256 against both copies and its original receipt,
detects changes during verification, and writes
`analysis\pattern-parity-verification.json`. It never repairs or overwrites data.

The exporter reads only `analysis\snapshots\fvd.mdb`, never the live MDB. It decodes the snapshot header's reversible stored database-password field in memory, passes it only to the child reader environment, and opens the snapshot using `Mode=Read`. It does not guess passwords, edit the MDB, execute Film Designs, or contact vendor services. SHA256 before and after must agree.

ACE streams thirteen explicitly selected pattern, vehicle, and relationship tables to JSONL staging under `analysis\catalog-ace-stage`. The SQLite importer preserves original table/column names and IDs, validates table counts, checks nonempty identifiers and type-code consistency, then publishes `analysis\catalog.sqlite`. Account, authentication, licensing, system, temporary, and duplicate partition-table values are not exported. `catalog-export.json` records schema, source fingerprint, checks, and reader identity.

The linker enumerates filenames and metadata in `C:\FilmVinylDesigns\ClientV2\Files`; it opens no pattern file contents. The exact relationship is `CarPattern.FDImageId = filename stem`. `PatternID` is a separate catalog-record identifier and must not be used to build filenames. Multiple vehicle rows may share a geometry asset. The output adds `LocalPatternFiles` and `local_patterns`, and records coverage in `catalog-file-coverage.json`.

`patterns` exposes normalized column labels while raw `CarPattern` values remain unchanged. `asset_id` holds the full `FDImageId`; `extension` is the raw nullable source field, while `local_extension` in `local_patterns` comes from the actual cache filename. Width and length are null in this snapshot; no unit or dimension inference is made by the migration.

Native table counts: 143,321 pattern rows, 136 makes, 1,851 vehicle models, 8,156 trims, nine pattern types. All 143,321 pattern rows reference 48,021 cached geometry assets. The cache also contains 156 unreferenced FDU/FDA files.
