# Tint & PPF Pattern Explorer

An offline Windows desktop prototype for browsing a vehicle-pattern catalog, previewing vector geometry, and exporting SVG inspection previews.

**Current testing scope:** catalog search, vehicle filters, compatibility notes, vector previews, pan/zoom, and SVG preview export. This build has no direct printer or cutter control. SVG physical units, scale, orientation, and cutting equivalence are still unverified. Use exports for inspection until dimensional and cutter validation is complete.

## Set up on another Windows PC

1. Download or clone this repository and extract it to a local folder.
2. Install **Python 3.11 or newer with Tcl/Tk** from [Python for Windows](https://www.python.org/downloads/windows/). The viewer uses only the Python standard library; no `pip install` step is needed. You can check Tk with `py -3 -m tkinter`, which should open a small demonstration window. See [Python's Tkinter documentation](https://docs.python.org/3/library/tkinter.html).
3. Obtain **`TintPPF-PatternData-*.zip`** separately from the project owner. The GitHub repository does not contain the catalog or pattern collection.
4. Extract that ZIP **into the project folder containing `start.cmd`**. The archive starts with `data/`; avoid creating an extra nested project or `data/data` folder.
5. Double-click **`start.cmd`**. It locates Python with Tk and opens Pattern Explorer. Choose a catalog entry to preview it; use the filters to narrow the list.

After extraction, the layout should be:

```text
tint-ppf-cutter/
  start.cmd
  app.py
  README.md
  tools/
  data/
    catalog.sqlite
    bundle-manifest.json
    patterns/
      warnings.json
      ... .FDA and .FDU files
```

Allow at least **4 GB of free disk space** for downloading and extracting the data. The extracted catalog and pattern cache occupy about 1.2 GB. Keep the data on a local disk and finish extracting before opening the app.

The recipient needs Python with Tk and the separate dataset. Codex, REA, Ghidra, Microsoft Access/ACE, and the original Film Designs installation are not viewer requirements.

## Verify the separate dataset

From a terminal in the project folder:

```powershell
py -3 tools\distribution\pack_data.py --verify-data data
```

This checks the supplied manifest, file hashes, catalog integrity, and catalog-to-file associations. The dataset's `bundle-manifest.json` is the authority for that bundle version. A missing or incomplete dataset produces setup instructions in the viewer.

`start.cmd` checks Python 3.11+ and Tk before launch. Standard `py -3` and `python` installations are tried first; an existing Codex runtime is a development fallback. The launcher does not install software.

## What can be tested

| Feature | Current behavior |
| --- | --- |
| Catalog | Search, make/model/year/type filters, pagination, and local-file filtering |
| Compatibility notes | Displays the supplied asset-specific warnings |
| Geometry | Vector preview, pan/zoom, and fit-to-window |
| SVG | Exports a new inspection preview under `exports/`; original files stay read-only |
| Partial geometry | Clearly labeled; ordinary SVG export is disabled |
| Printer/cutter connections | Not implemented: no USB, serial, TCP, HPGL job submission, or printer output |
| Physical dimensions | Not calibrated; SVGs have no verified mm/inch size |
| Editing and production workflows | Undo/editing, nesting, paneling, pricing, roles, and reporting still need implementation |

Importing an SVG into another program can help compare its outline visually. The current export does not establish a production-ready cutting file. Hardware testing needs dimensional calibration, orientation and path checks, and a verified output driver first.

The initial 2026-11-01 local dataset contains **143,321 catalog listings** and **48,177 pattern files**, plus **100 compatibility warnings**. Listings can share a geometry asset. All files were copied and SHA-256 checked; the catalog was compared against its native export. The parser structurally supports **47,834 files** and marks **343 files** as partial. Preserving every raw file and rendering every drawing feature are separate milestones.

## Keep the data separate from GitHub

Push the source, launcher, documentation, tests, and tools. Share the private data ZIP separately through Proton Drive or another file-sharing service. GitHub recommends file sharing for large databases and blocks individual regular Git files over 100 MiB: [GitHub's large-file guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

`.gitignore` excludes `data/`, `analysis/`, `exports/`, `distribution/`, databases, pattern files, archives, local credentials, environments, and vendor/build binaries. Existing tracked history has also been checked for these files. Ignore rules do not remove a file that was already committed, so review `git status` and the staged file list before publishing.

The data ZIP is generated locally under `distribution/private/`, which is ignored. Send the ZIP and its checksum file privately; keep private download links and passwords out of the public README. Proton Drive supports downloadable share links: [Proton sharing instructions](https://proton.me/support/drive-shareable-link). No upload or Git publishing is performed by the packaging tools.

## Developer commands

```powershell
py -3 app.py --help
py -3 app.py --catalog "D:\PatternData\catalog.sqlite" --pattern-root "D:\PatternData\patterns"
py -3 -m unittest discover -s tests -v
py -3 -m unittest discover -s tools\patterns -p "test*.py" -v
```

The portable defaults are `data/catalog.sqlite` and `data/patterns`. An existing `analysis/catalog.sqlite` is accepted as a local development fallback. Custom locations can be selected in the UI or supplied on the command line.

To build a new private data bundle from this checkout's existing verified data:

```powershell
py -3 tools\distribution\pack_data.py
```

See [data packaging](tools/distribution/README.md), [pattern format and CLI](tools/patterns/README.md), and [catalog reconstruction](tools/catalog/README.md). Catalog reconstruction is an optional development workflow that requires Windows PowerShell 5.1 and Microsoft ACE; it is not part of normal viewer startup. Local investigation evidence, MDB snapshots, and receipts stay under ignored `analysis/`.
