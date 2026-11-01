# Tint & PPF Pattern Explorer

An offline Windows desktop prototype for browsing a vehicle-pattern catalog, previewing geometry, and preparing cutting jobs at the original pattern size.

**Current testing scope:** catalog search, vector inspection, original-size job preparation, native driver command capture, and explicit file/Windows RAW queue/TCP/serial delivery, plus an advanced driver-dependent USB path. All 344 model profiles passed the offline command-capture gate. **No physical cutter has been tested. Full original-software printing parity is not complete.** Supported complete FDA/FDU patterns now use their recovered native units automatically at 100%; SVG exports remain inspection previews. See the [pattern-size evidence](docs/pattern-units.md).

## Set up on another Windows PC

1. Download the latest project from GitHub, or update your clone. Extract the GitHub project ZIP to a local Windows folder. Find **`start.cmd`**: the folder containing that file is the **project root** used below.
2. Install **Python 3.11 or newer with Tcl/Tk** from [Python for Windows](https://www.python.org/downloads/windows/). The viewer uses only the Python standard library; no `pip install` step is needed. You can check Tk with `py -3 -m tkinter`, which should open a small demonstration window. See [Python's Tkinter documentation](https://docs.python.org/3/library/tkinter.html).
3. Go to the **[latest release on GitHub](https://github.com/hhayaan/tint-ppf-cutter/releases/latest)**. Under **Assets**, download the **PatternData ZIP**, the **NativeRuntime ZIP**, and their two matching **`.sha256` checksum files**. These are separate downloads: cloning the repository or downloading its source ZIP does not include them. See [Release downloads](#release-downloads) for the filename patterns.
4. Extract **both downloaded ZIPs** directly into that **same project root containing `start.cmd`**. In Windows **Extract All…**, change the suggested destination if it adds a folder named after the ZIP; select the project root itself. The archives already contain `data/` and `.runtime/vendor/`. Do not place or extract either ZIP inside `data/` or `.runtime/vendor/`, and avoid extra `TintPPF-…/` or `data/data/` layers. The `.sha256` files are plain-text checksums: keep them with the downloads; do not extract them.
5. Double-click **`start.cmd`**. It locates Python with Tk and opens Pattern Explorer. Choose a catalog entry to preview it; use the filters to narrow the list.

After extraction, the layout should be:

```text
tint-ppf-cutter/
  start.cmd
  app.py
  README.md
  tools/
  .runtime/
    vendor/
      gsfwkcut62.dll
      drvcut/
        graphtec.dll
        ... other driver DLLs
  data/
    catalog.sqlite
    bundle-manifest.json
    patterns/
      warnings.json
      ... .FDA and .FDU files
```

Allow at least **4 GB of free disk space** for downloading and extracting the files. The extracted catalog and pattern cache occupy about 1.2 GB. Finish extracting both ZIPs to the local project folder before opening the app.

Browsing requires Python with Tk and the separate dataset. Codex, REA, Ghidra, Microsoft Access/ACE, and the original Film Designs installation are not viewer requirements. Cutting additionally needs the inspected native driver runtime and Windows .NET Framework 4; see the cutting setup below.

## Prepare a cutting test

1. Confirm that extracting the **NativeRuntime ZIP from the [latest GitHub release](https://github.com/hhayaan/tint-ppf-cutter/releases/latest)** created **`.runtime/vendor/gsfwkcut62.dll`** and **`.runtime/vendor/drvcut/`**. Alternatively, select a matching existing Film Designs `ClientV2` DLL folder in the cutting dialog.
2. Open **Cut / test…**. The last saved model/setup is restored; the initial choice is the owner's confirmed **Graphtec FC9000-140**. Changing a profile does not contact a device. A standalone 20 × 20 mm test rectangle is available even without pattern data.
3. Select **File** for the first check. Choose the intended paths, prepare the job, and inspect millimeter dimensions, actual native coordinate bounds, model, and generated commands. Save to a new output filename.
4. Leave supported FDA/FDU patterns at **Original pattern size (automatic)** and 100%. Custom source calibration and **Resize this job** are optional controls under **Advanced size / quality**. They apply to the current job only.
5. For an owner-operated hardware trial, enter the actual usable media width. **Roll** mode permits a blank available length; the job preview shows the length needed. **Sheet** mode requires width and length. Media limits reject oversized output without shrinking it. Select an existing Windows RAW queue, a verified TCP host/port, or explicit COM settings. The advanced **Direct USB** tab instead requires the actual selected interface path and matching VID/PID; its installed-driver behavior is unverified. Review the newly generated job before **Send reviewed job once**. A successful send reports local acceptance, not a verified physical cut.

**Save cutter setup** remembers the runtime, connection, media and tool settings separately for each exact model in ignored `.runtime/cut-settings.json`; successful preparation also saves that setup. Pattern selection, resizing, custom calibration, copies and page selection do not carry into the next job. Start X/Y default to zero at the cutter origin; positive values offset the complete layout. Native axes remain locked to the captured mode, return-to-origin stays enabled, and unverified feed/continuation choices are disabled.

For an oversized layout, enable **Manual paneling** and enter both media width and length as the page size. Start X/Y must be zero in this mode. Select a page number and optional overlap, then prepare, review and output that page separately. Previous/Next changes the page selection only; there is no automatic advance, feed or continuation. Position the media/origin yourself and use a new output filename for each saved page. Prepare, Send and status stay visible while the rest of the form scrolls.

For the FC9000-140, this backend generates HP-GL. The owner has not confirmed the cutter's current COMMAND setting; check it against HP-GL before the trial. Direct X/Y axes are used for the verified profile. USB can use an appropriate installed Windows queue or the separate advanced generic device-path primitive. Neither establishes universal manufacturer USB or Data Link support, and no TCP port is guessed. [Graphtec's OPS662 driver](https://www.graphteccorp.com/download/ops662_w64/) and [Data Link connection guidance](https://www.graphteccorp.com/download/gps_w/) describe distinct routes.

The native adapter reuses the original model drivers in a separate x86 process and captures their output in memory. It is not a fully independent reimplementation. Cutting requires Windows .NET Framework 4 and the inspected DLLs' x86 Visual C++ prerequisites. In particular, the legacy imaging DLLs require a compatible **Visual C++ 2005 x86 runtime** (`Microsoft.VC80.CRT 8.0.50608.0`); a modern Visual C++ redistributable alone does not satisfy that dependency. Runtime files are version/hash checked. REA and Ghidra are development tools, not cutting requirements. See [cutting implementation and remaining gaps](docs/printing.md) and the [per-profile capture report](docs/printing-native-verification.json).

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
| Cutting jobs | Automatic original size for supported complete FDA/FDU, selected paths, adaptive curves, transforms, copies, passes, weed border, path order, manual offsets and native driver generation |
| Cutter setup | Saved per model: connection, runtime, roll/sheet media and tools; job geometry choices remain separate |
| Loaded media | Roll width with optional available length, or sheet width/length; rejects overflow without automatic resizing |
| Manual paneling | Media-sized path clipping, optional overlap and explicit page selection; each page is prepared, reviewed and output separately |
| Delivery | Exclusive file output, Windows RAW queue, explicit TCP/COM, and advanced driver-dependent USB; no automatic retries or fallback |
| Driver coverage | 344 catalog profiles; offline capture status is recorded separately for each profile; no hardware certification |
| Physical dimensions | Native FDA/FDU conversion established from serializer/cutter analysis and original UI dimensions; physical cutter validation remains pending; SVG previews are inspection exports |
| Original output parity | Automatic page continuation/pauses, optical registration workflows, vendor-specific/Data Link USB extensions, retained job queue, and ordinary graphics/page printing remain incomplete |
| Editing and production workflows | Undo/editing, automatic nesting, pricing, roles, and reporting still need implementation |

Importing an SVG into another program can help compare its outline visually. SVG export does not establish a production-ready cutting file. The cutting dialog separately prepares machine jobs at the established native pattern size. Output scale, orientation, machine conditions and the physical cut still need an owner-operated hardware check.

The initial 2026-11-01 local dataset contains **143,321 catalog listings** and **48,177 pattern files**, plus **100 compatibility warnings**. Listings can share a geometry asset. All files were copied and SHA-256 checked; the catalog was compared against its native export. The parser structurally supports **47,834 files** and marks **343 files** as partial. Preserving every raw file and rendering every drawing feature are separate milestones.

## Release downloads

Get the required bundles from **Assets** on the **[latest release on GitHub](https://github.com/hhayaan/tint-ppf-cutter/releases/latest)**. Download these four files; `*` below represents the version included in that release:

| Release asset | Purpose |
| --- | --- |
| `TintPPF-PatternData-*.zip` | Catalog, pattern files, compatibility notes and verification manifest |
| `TintPPF-PatternData-*.sha256` | SHA-256 checksum for the pattern-data ZIP |
| `TintPPF-NativeRuntime-*.zip` | Matching native cutting runtime, including model driver DLLs |
| `TintPPF-NativeRuntime-*.zip.sha256` | SHA-256 checksum for the native-runtime ZIP |

The dataset and runtime are versioned independently, so their version numbers may differ. Use the assets attached to the latest release. Only the two ZIPs are extracted, both into the same project root containing `start.cmd`; the two checksum files stay with the downloads. GitHub's automatically generated **Source code (zip)** and **Source code (tar.gz)** downloads contain the project source, not these bundles.

To optionally verify the downloads before extraction, open PowerShell in their download folder:

```powershell
Get-FileHash -Algorithm SHA256 -Path ".\TintPPF-PatternData-*.zip"
Get-FileHash -Algorithm SHA256 -Path ".\TintPPF-NativeRuntime-*.zip"
```

Compare each displayed `Hash` with the 64-character hash in its corresponding `.sha256` text file; letter case does not matter. Keep the ZIPs and checksum files together for download verification.

The repository contains source, launcher, documentation, tests and tools. Bundles are distributed as release assets rather than committed to Git history. See [GitHub's large-file guidance](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

`.gitignore` excludes `data/`, `analysis/`, `exports/`, `distribution/`, databases, pattern files, archives, local credentials, environments, and vendor/build binaries. Existing tracked history has also been checked for these files. Ignore rules do not remove a file that was already committed, so review `git status` and the staged file list before publishing.

For maintainers, the prepared release assets are in ignored `distribution/private/`. Attach the two ZIPs and their matching checksum files to the GitHub release; exclude extracted verification folders and local investigation receipts. The packaging tools do not upload files or publish releases.

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
