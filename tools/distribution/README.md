# Private data handoff

The GitHub repository contains code and documentation. The recipient gets the
catalog and native pattern cache through a separate private ZIP.

## Build from the existing verified copies

From the project root:

```powershell
python tools\distribution\pack_data.py
```

The default input is `data/catalog.sqlite` when present, otherwise
`analysis/catalog.sqlite`, together with `data/patterns`. Building also requires
the two original copy receipts under `analysis/`: `pattern-cache-snapshot.json`
and `pattern-auxiliary-snapshot.json`. The tool reads only these local copies;
it does not read the installed original program or upload anything.

Output goes under ignored `distribution/private/`. Share the generated ZIP and
SHA-256 checksum file through your private file-sharing service. Bundles are named
`TintPPF-PatternData-vN.zip`; existing outputs are preserved when building
another bundle. Share only the version identified as verified in its receipt. The generated receipt records the actual output path,
size, and verification result.

The archive extracts directly to:

```text
data/
  catalog.sqlite
  bundle-manifest.json
  patterns/
    warnings.json
    ... .FDA and .FDU files
```

The catalog is created with SQLite's backup API. Only the backup's derived local
file paths are changed to portable `data/patterns/<filename>` paths. The
canonical catalog is preserved. The package accepts the pattern/vehicle table
allowlist and omits MDB originals, investigation evidence, account/licensing
databases, logs, vendor executables, and reference-resource collections.

Every packed source file is checked against its saved receipt. The complete ZIP
is then extracted into a new ignored verification folder, with CRC, SHA-256,
SQLite integrity, catalog count, and asset linkage checks. Keep this a private
artifact; it is deliberately excluded by `.gitignore`.

## Verify after downloading

First compare the downloaded ZIP's checksum with the supplied checksum file
(use the actual downloaded filename; v2 is an example):

```powershell
Get-FileHash .\TintPPF-PatternData-v2.zip -Algorithm SHA256
```

Extract its contents into the source-project folder containing `start.cmd`.
Then run:

```powershell
python tools\distribution\pack_data.py --verify-data data
```

This recipient command needs only the extracted dataset and the source code.
It does not require the development `analysis/` directory or the original
software. It reports failures for missing, changed, or unexpected data files.

The main [README](../../README.md) describes Python/Tk setup and the current
preview-only hardware limitations.
