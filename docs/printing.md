# Cutting implementation and verification

The owner confirmed **Graphtec FC9000-140**. The cutter's current command mode is unconfirmed (possibly AUTO). This implementation generates HP-GL for that profile. No physical cutter or printer was connected during development.

## What the counts mean

The installed original contains **29 driver DLLs with 344 factory variants**, including 64 Graphtec variants and four FC9000 sizes. There are 343 distinct display labels; repeated model IDs and names are retained using an exact `driver:index` key. A catalog variant is not a hardware compatibility certificate. One source entry is a test profile.

`printing/drivers.json` contains the extracted metadata. `printing/driver_catalog.py` supplies lookups without loading vendor code. `printing-native-verification.json` records which profiles passed an offline 20 × 10 mm rectangle capture, their model/driver/helper/output hashes, axis mode, and measured native coordinate bounds. The compiler refuses catalog-only profiles, different driver versions, and axis modes that have not passed the gate.

All **344/344** profiles passed the final source-bound rectangle capture sweep: model identity, native configuration ancestry, payload/receipt hashes, and native coordinate bounds matched. No profile was skipped, timed out, or left with a dimensional mismatch. No model has completed a physical cutter trial. These counts describe offline generation and remain separate from physical compatibility.

## Implemented workflow

- Select decoded source paths, or create a synthetic millimeter rectangle.
- Use supported complete FDA/FDU patterns at their original size, automatically at 100%. The recovered coordinate unit is a PostScript point, converted with `25.4/72` mm per source unit. Unknown formats/versions and partial geometry do not receive this automatic scale. See [native pattern-size evidence](pattern-units.md).
- Adaptively flatten cubic curves to an explicit millimeter tolerance. Preserve subpaths and groups. Reject partial/invalid/over-budget geometry instead of omitting it. Endpoint equality establishes closure; opaque source flags do not.
- Optionally use advanced custom calibration or explicitly enable resizing for the current job. Apply quarter-turn rotation, mirrors, copy grid/spacing, passes, and a global weed border. Order paths in source order or by start/end distance, optionally respecting actual contour containment for inner-first cutting. This is not automatic packing/nesting.
- Start at the cutter origin or apply positive Start X/Y offsets to the complete layout. Validate actual placed geometry against supplied media, generate original native model commands, and show both logical dimensions and actual native coordinate bounds.
- Optionally clip an oversized calibrated layout into manually selected media-sized pages, with explicit overlap. Prepare and review one page per output attempt.
- Review the job and destination, then save or explicitly send once. Any input change invalidates the reviewed payload. A new attempt requires new preparation.
- Capture delivery byte counts, partial failures, cancellation and cleanup details in ignored `output/jobs/` logs. Never automatically retry or switch transport after an uncertain delivery.

The adapter supports explicit native speed/force/acceleration overrides and tool selection through each model's virtual methods. These are native driver units. Blank fields add no override; initialization can still emit model defaults. A method returning success is not evidence that a specific device accepted every condition.

## Size, loaded media and saved setup

Normal pattern cutting uses **Original pattern size (automatic)**. It does not ask for a conversion or resize a vehicle pattern to fit the roll. **Advanced size / quality** contains custom calibration, known-width calibration and **Resize this job** for intentional exceptions. These geometry choices reset for the next job. The unit conversion is established from the original serializer, size calculation, cutter conversion and matching original UI dimensions; physical cutter performance remains unverified.

The initial media mode is **roll**; saved setup can restore a different mode. A device destination needs the actual usable width; available length can be blank for a roll. Entering a length adds a limit. **Sheet** mode and manual paneling require both dimensions. An oversized layout is rejected or deliberately split into pages, without automatic shrinking. The Graphtec backend also checks the selected model's maximum width. Native emitted-coordinate bounds must fit the supplied media; the artwork's size alone is insufficient.

**Save cutter setup**, and a successful job preparation, save the selected model, runtime folder, transport details, roll/sheet media and tool settings to ignored `.runtime/cut-settings.json`. Setup is separate for each exact profile and the last saved model is restored. Loading setup does not enumerate, open or send to a device. The stored media still needs to match the current load. JSON is whitelisted and atomically replaced; unreadable or malformed settings produce a visible error. Source files, selected paths, calibration/resizing, copies, manual offsets and page selection are not saved across jobs.

Native axes are locked to each profile's captured mode. Return-to-origin stays enabled, and unverified non-return/feed modes are unavailable. Positive manual offsets are supported; preservation of the original design's absolute position is still pending. Manual panels require zero offsets and a fresh operator-selected origin for each page.

Generated-job metadata records the source-unit conversion/provenance, applied job scale and media mode. The offline CLI also writes an adjacent `.plt.json` receipt containing geometry, source units, native metadata and delivery details. A raw `.plt` file alone does not carry those explanatory units.

## Original cut-function parity

The table distinguishes public documentation, observed original UI and static native analysis from functionality in this build. Original UI observations describe this installation, rather than factory defaults for every model. Private screenshots/accessibility snapshots and REA JSON remain under ignored `analysis/`; the [pattern-size evidence](pattern-units.md) and [source-bound profile report](printing-native-verification.json) publish the relevant method and result summaries.

| Area | Original evidence | Implemented | Pending or limited |
| --- | --- | --- | --- |
| Physical size | Original UI showed 100% size; serializer and cutter functions establish points. | Automatic original-size conversion for admitted native patterns; advanced per-job override. | Unknown formats/partial drawings stay blocked; physical output still needs testing. |
| Selected pieces | The [cutting manual](https://filmvinyldesigns.notion.site/Cutting-Patterns-1215fa01383980e0af23dec80bf1c2cb) requires checking intended parts before output. | Explicit multiple-path selection and prepared-job preview. | Path list initially selects all; original graphical part-selection behavior is not fully reproduced. |
| Device/setup | [Plotter setup](https://filmvinyldesigns.notion.site/Plotter-Setup-1135fa01383980bab5d2fd1b7da876e2) describes reusable model/connection setup; native configuration reads saved preferences. | Last model plus per-profile connection, media, runtime and tools. | Original named media presets and all advanced preferences are not reproduced. |
| Media and fitting | Original UI exposes roll mode, width/length and stored sizes; the [workspace manual](https://filmvinyldesigns.notion.site/The-Software-Interface-1195fa01383980ea9424f83af78dbea6) identifies roll/feed dimensions. | Roll width with optional length; sheets use both limits; overflow never silently changes scale. | No automatic media sensing or alignment. |
| Placement | Observed UI offers origin, design-position and manual-position choices. | Cutter-origin placement and positive X/Y offsets with placed-bounds checks. | Keep-design-position semantics and original offset parity need further comparison. |
| Tool conditions | Original UI has separate host-control choices; native code maps model condition values. | Optional native-unit speed/force/acceleration and tool calls. | Original user-value mapping, full model ranges and physical acceptance are not established. |
| Curves and order | Native resolution mapping has five levels; the manual documents ordering and direction controls. | Adaptive curve tolerance, source/start-nearest/end-nearest ordering and optional inner-first constraints. | Exact native flattening/order equivalence and unidirectional mode remain pending. |
| Pages and sequence | The manual describes page selection, offsets, pauses and continuation. | One explicitly selected manual panel at a time, with overlap and separate review/output. | All-page sequencing, continuation, row/column pauses and feed remain pending. |
| End behavior and repeat | The manual describes retained jobs and origin/continuation choices; native analysis exposed extra travel in the alternate end mode. | Captured axes and return-to-origin locked; each new attempt requires preparation. | Retained-job workflow, initial pause and alternate end behavior are not implemented. |
| Other production output | Original UI exposes crop-mark and printing areas. | Ordinary unregistered cut paths plus the listed explicit transports. | Optical registration, vendor-specific/Data Link USB, device status and graphics/page printing remain incomplete. |

REA/Ghidra source anchors include `gslibcad62!TTrajecte::Llegir` (RVA `0x5da30`), `fdthmb40!FD_GetPatternSize` (`0x1c30`), `gsfwkcut62!TConfigDriverTall::TConfigDriverTall` (`0x52e0`), `TDriverTall::MapResolucioTall` (`0xb850`), and the native page begin/end functions. Their meaning is constrained by the tested artifact hashes and capture gate; naming a native option does not establish that its full workflow has been reimplemented.

## Manual paneling

Enter the usable media width and length, set Start X/Y to zero, open **Manual paneling**, enable it, and choose a page number starting at 1. These media dimensions define the clipping page; the full layout can exceed them. Overlap repeats tool paths in shared areas. Page numbers use a stable row-major grid, and a blank selected page is rejected without compiling or sending a job.

Prepare the selected page, inspect its page number, geometry, actual native coordinate bounds and destination, then save or send that page once. **Previous page** and **Next page** only change the selected number and invalidate the generated job. Each page needs its own preparation, review and output attempt; File output needs a new filename. The operator positions the media and origin. Automatic page advancement, continuation and pause sequences are not implemented, and this clipping workflow does not establish byte parity with the original paneling system.

The dialog keeps Prepare, Send and status controls in a fixed footer while the form scrolls. A mapped 1180 × 700 window was checked after job preparation; Send remained within the client area. A complete file-only test split a 30 × 20 mm rectangle across two 20 × 20 mm pages, saved both through the actual dialog, and checked emitted bounds of `[0, 0, 20, 20]` and `[0, 0, 10, 20]`. Hardware transport and enumeration APIs were blocked for that check.

## How the original functionality is reused

```text
decoded complete native pattern + established units (or explicit advanced override)
    -> Python geometry planner (millimeters)
    -> isolated x86 C# helper
    -> original model factory -> native configuration -> native plotter
    -> native NULL I/O object with an in-memory Write callback
    -> captured byte payload + verified receipt
    -> reviewed File / Windows RAW queue / TCP / COM / advanced USB destination
```

REA identified native modules, exports, classes and call relationships. Its Ghidra provider exposed the actual constructors, configuration layout, virtual-table offsets, coordinate transforms and model overrides. This allowed a small adapter to invoke the original implementation instead of approximating every dialect from model names.

The original process's memory was not copied into this project. The adapter loads the supplied DLLs in its own short-lived x86 process. Its supplied I/O object is the native NULL device, with only the write method redirected to bounded memory. Actual delivery happens later through the separately implemented transport layer. Static review and capture admission are needed for specialized modules that can call device APIs directly.

The helper verifies the inspected core library and model DLL hashes, verifies that the requested factory index returns the expected model ID, checks actual configuration ancestry and that the plotter retains the supplied memory sink, checks native return codes, bounds command capture, and validates the resulting payload hash/receipt. The Python entry point locks all 58 native dependencies and binds profile admission to the exact adapter source fingerprint. `.gitattributes` preserves the C# source line endings across Windows checkouts. This is **native-library reuse**, not a complete independent rewrite.

## Graphtec fixture

For `graphtec:53` / `gr_fc_9600140`, direct X/Y axes produce a 20 × 10 mm rectangle with native bounds `[0, 0, 20, 10]`, at 40 units/mm on both axes. The base capture is 75 bytes with SHA-256:

`ff0f536b70bed49dbec20342e7618315bb4cced84e895846fba67bc054ec6b6f`

Its movement coordinates include `(0,0)`, `(800,0)`, `(800,400)`, `(0,400)` and closure. A separate options capture includes native `VS10;FS20;AS1.5;` overrides. A 20 × 20 mm job passed the actual dialog's prepare/review/exclusive-file-save workflow.

The original alternate HP-GL orientation uses `(profile_width - y, x)`. A small job can therefore begin near the full profile width. That mode is not enabled by the tested direct-axis profile. Device destinations must also pass the emitted-coordinate media check; checking the outline's width alone is insufficient.

Graphtec documents HP-GL increments of 0.025 mm and selectable GP-GL increments. These are separate command languages/settings. See the [FC9000 product information](https://www.graphteccorp.com/cutting/fc9000/) and [FC9000 working manual](https://mygraphtec.jp/site_download/manual/fc9000/FC9000-UM-155-03-01-ENG_Protect.pdf), especially command selection, origin, and condition priority. The captured bytes establish software behavior; the owner's trial must establish actual scale and orientation on loaded media.

## Connections

| Output | Implemented behavior | Verification |
| --- | --- | --- |
| File | New binary file only; refuses overwriting | Exact captured bytes and end-to-end dialog save |
| Windows RAW queue | Explicit installed queue; partial writes, spool job ID, abort and cleanup | Fake Winspool tests; no physical printer queue submission |
| TCP | Explicit host **and port**; bounded optional reply; timeouts/cancellation; no discovery or retry | Exact bytes through an ephemeral localhost server |
| Serial | Explicit COM, baud, parity, bits, stops and flow control; exclusive open and partial/cancel reporting | Fake Win32 serial API tests; no physical COM output |
| Advanced direct USB path | Explicit selected interface path and matching VID/PID; optional explicit pipe; overlapped writes, timeout/cancel reporting and retained pending-I/O cleanup | Fake SetupAPI/kernel tests only; installed-driver-dependent and hardware-unverified |

Socket/spool/serial/USB acceptance does not prove the machine completed a cut. A TCP reply is opaque bytes, not an interpreted Graphtec completion acknowledgement. No port such as 9100 is assumed. The official [Graphtec SDK](https://www.graphteccorp.com/support/sdk/) includes command documentation and a USB driver library; requesting it requires the owner's application. No form or message has been submitted.

The **Direct USB** tab implements the original shared transport's generic device-path primitive, not universal Graphtec USB support. Listing interfaces requires an explicit button; selecting one supplies its actual VID/PID, and the reviewed destination must match. No device identity or pipe is guessed. A stalled cancellation retains its live buffers and handles; use **Recheck pending USB cleanup** before another attempt on that device.

An installed Windows RAW queue, direct USB, and Graphtec Data Link are distinct routes. [Graphtec OPS662 documentation](https://www.graphteccorp.com/download/ops662_w64/) describes its Windows print-driver path. [Graphtec Pro Studio documentation](https://www.graphteccorp.com/download/gps_w/) specifies a separate Graphtec USB connection for Data Link and excludes OPS662-created ports from that workflow. The advanced generic path has not been verified against the owner's installed FC9000 function driver; vendor-specific/Data Link extensions remain separate work.

## Private native runtime handoff

The optional `TintPPF-NativeRuntime-v1.zip` contains the selected DLL dependency closure (58 DLLs, 27,494,968 uncompressed DLL bytes). It expands under `.runtime/vendor/` and excludes executables, patterns, databases, licenses, accounts and configuration files. Native DLLs and the ZIP remain ignored by Git. Windows and global redistributable DLLs are not copied.

This runtime was extracted and successfully generated the same Graphtec fixture on the development PC. This is not a clean-Windows dependency test: the original installation's global x86 Visual C++ prerequisites are present on this PC. In particular, nine bundled legacy imaging DLLs request **Microsoft.VC80.CRT 8.0.50608.0 x86** through side-by-side manifests; a compatible Visual C++ 2005 x86 runtime is required in addition to the bundled newer runtime DLLs. A modern Visual C++ redistributable alone does not satisfy that dependency. The owner may instead select his matching existing `ClientV2` folder. Python/Tk and Windows .NET Framework 4 are required; REA/Ghidra/Java are not runtime requirements.

To reproduce the private archive from an authorized matching installation:

```powershell
py -3 tools\printing\pack_runtime.py --runtime "C:\Program Files (x86)\FilmVinylDesigns\ClientV2" --output distribution\private\TintPPF-NativeRuntime-v1.zip
```

The output filename must be new. Share this small optional ZIP privately with the existing pattern-data ZIP; no GitHub upload or push is performed.

## Remaining parity work

Beyond the manual page clipping and selection above, the original manual describes automated page continuation and pauses, cut-mode choices, retained jobs, unidirectional cutting, and additional axes/control/enhancement settings. Optical registration, sheet cutting/page feed, device polling/status and ordinary graphics/page printing also require separate implementation and verification. These workflows remain incomplete.

Pattern decoding still marks 343 assets as partial; these cannot enter the cutting planner. Full raw-file preservation does not establish support for every drawing record. The native source-unit convention is established; vehicle-specific fit and actual machine output remain separate checks.

The current build is suitable for **file inspection and an owner-controlled calibration trial of an enabled profile**. It does not meet a claim of complete feature parity or production-tested support for every original device.

## Reproduce engineering checks

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m unittest discover -s tools\patterns -p "test*.py" -v
py -3 -m printing --list-models --search FC9000
py -3 -m printing --test-rectangle 20 20 --output exports\fc9000-square.plt
```

`tools/printing/verify_native_profiles.py` is the explicit offline native probe. It snapshots the helper, runs each admitted profile in its own process against the memory sink, and stores full local evidence under ignored `analysis/`. Its small public verification report contains hashes and results, not vendor binaries or private paths.
