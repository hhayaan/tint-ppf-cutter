# Tint & PPF Pattern Explorer

An offline Windows desktop prototype for browsing a vehicle-pattern catalog, previewing geometry, and preparing calibrated cutting jobs.

**Current testing scope:** catalog search, vector inspection, calibrated job preparation, native driver command capture, and explicit file/Windows RAW queue/TCP/serial delivery, plus an advanced driver-dependent USB path. The recovered catalog contains 344 model profiles; only profiles that pass the offline native capture gate can generate jobs. **No physical cutter has been tested. Full original-software printing parity is not complete.** Pattern scale requires a supplied conversion or known dimension; SVG previews retain their earlier inspection-only meaning.

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

Browsing requires Python with Tk and the separate dataset. Codex, REA, Ghidra, Microsoft Access/ACE, and the original Film Designs installation are not viewer requirements. Cutting additionally needs the inspected native driver runtime and Windows .NET Framework 4; see the cutting setup below.

## Prepare a cutting test

1. Supply the matching Film Designs `ClientV2` DLL folder, or extract the separately shared **`TintPPF-NativeRuntime-v1.zip`** into this project. The private ZIP creates `.runtime/vendor/`; it contains 58 DLLs and is about 11.6 MB. Keep this optional runtime outside GitHub, alongside the separate pattern-data handoff.
2. Open **Cut / test…**. The owner's confirmed **Graphtec FC9000-140** profile is selected initially. Changing a profile does not contact a device. A standalone 20 × 20 mm test rectangle is available even without pattern data.
3. Keep **File** selected, prepare the job, and inspect the selected paths, millimeter dimensions, actual native coordinate bounds, model, and generated commands. Save to a new output filename.
4. For a pattern, supply its verified millimeters-per-source-unit conversion or use a known unrotated selected width. Complete decoding alone does not establish a physical size.
5. For an owner-operated hardware trial, enter the actual usable media width/length and select an existing Windows RAW queue, a verified TCP host/port, or explicit COM settings. The advanced **Direct USB** tab instead requires the actual selected interface path and matching VID/PID; its installed-driver behavior is unverified. Review the newly generated job before **Send reviewed job once**. A successful send reports local acceptance, not a verified physical cut.

For an oversized layout, enable **Manual paneling** and use the media width/length as the page size. Select a page number and optional overlap, then prepare, review and output that page separately. Previous/Next changes the page selection only; there is no automatic advance, feed or continuation. Position the media/origin yourself and use a new output filename for each saved page. Prepare, Send and status stay visible while the rest of the form scrolls.

For the FC9000-140, this backend generates HP-GL. The owner has not confirmed the cutter's current COMMAND setting; check it against HP-GL before the trial. Direct X/Y axes are used for the verified profile. USB can use an appropriate installed Windows queue or the separate advanced generic device-path primitive. Neither establishes universal manufacturer USB or Data Link support, and no TCP port is guessed. [Graphtec's OPS662 driver](https://www.graphteccorp.com/download/ops662_w64/) and [Data Link connection guidance](https://www.graphteccorp.com/download/gps_w/) describe distinct routes.

The native adapter reuses the original model drivers in a separate x86 process and captures their output in memory. It is not a fully independent reimplementation. Runtime files are version/hash checked, and the inspected version's x86 Visual C++ runtime prerequisites still apply. REA and Ghidra are development tools, not cutting requirements. See [cutting implementation and remaining gaps](docs/printing.md) and the [per-profile capture report](docs/printing-native-verification.json).

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
| Cutting jobs | Selected paths, adaptive curves, transforms, copies, passes, weed border, path order, media bounds, native driver generation |
| Manual paneling | Media-sized path clipping, optional overlap and explicit page selection; each page is prepared, reviewed and output separately |
| Delivery | Exclusive file output, Windows RAW queue, explicit TCP/COM, and advanced driver-dependent USB; no automatic retries or fallback |
| Driver coverage | 344 catalog profiles; offline capture status is recorded separately for each profile; no hardware certification |
| Physical dimensions | Cutting requires explicit calibration; SVG inspection previews have no verified mm/inch size |
| Original output parity | Automatic page continuation/pauses, optical registration workflows, vendor-specific/Data Link USB extensions, retained job queue, and ordinary graphics/page printing remain incomplete |
| Editing and production workflows | Undo/editing, automatic nesting, pricing, roles, and reporting still need implementation |

Importing an SVG into another program can help compare its outline visually. SVG export does not establish a production-ready cutting file. The cutting dialog separately prepares calibrated machine jobs; dimensional, orientation, machine-condition, and physical cutter validation remain part of the owner's trial.

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
py -3 -m printing --list-models --search FC9000
py -3 -m printing --test-rectangle 20 20 --output exports\fc9000-square.plt
```

The portable defaults are `data/catalog.sqlite` and `data/patterns`. An existing `analysis/catalog.sqlite` is accepted as a local development fallback. Custom locations can be selected in the UI or supplied on the command line.

To build a new private data bundle from this checkout's existing verified data:

```powershell
py -3 tools\distribution\pack_data.py
```

See [data packaging](tools/distribution/README.md), [pattern format and CLI](tools/patterns/README.md), and [catalog reconstruction](tools/catalog/README.md). Catalog reconstruction is an optional development workflow that requires Windows PowerShell 5.1 and Microsoft ACE; it is not part of normal viewer startup. Local investigation evidence, MDB snapshots, and receipts stay under ignored `analysis/`.
