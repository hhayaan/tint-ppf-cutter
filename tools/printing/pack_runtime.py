"""Package the inspected native driver dependency closure for private handoff.

Copies DLLs only from the selected existing installation. No executable,
database, pattern, account, license or configuration file is included. Windows
and globally installed redistributable libraries are not copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from printing.backend import IO_SHA256, SHARED_SHA256
from printing.driver_catalog import load_catalog
from tools.printing.static_probe import PE


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(runtime: Path) -> tuple[dict, list[Path]]:
    runtime = runtime.resolve(strict=True)
    catalog = load_catalog()
    required = {"gsfwkcut62.dll": SHARED_SHA256, "gslibio62.dll": IO_SHA256,
                **{"drvcut/" + d["module_filename"]: d["artifact_sha256"] for d in catalog["drivers"]}}
    for name, expected in required.items():
        if sha(runtime / name) != expected:
            raise ValueError(f"Runtime version differs from inspected metadata: {name}")
    root_dlls = {p.name.casefold(): p for p in runtime.iterdir() if p.is_file() and p.suffix.casefold() == ".dll"}
    pending = [runtime / name for name in required]
    included: set[Path] = set()
    external: set[str] = set()
    while pending:
        file = pending.pop()
        if file in included:
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(runtime) or file.suffix.casefold() != ".dll":
            raise ValueError("Native dependency must be an ordinary DLL inside the selected runtime")
        pe = PE(file)
        if pe.bits != 32:
            raise ValueError(f"Expected an x86 native dependency: {file.name}")
        included.add(file)
        for dependency in pe.imports():
            name = dependency["dll"].casefold()
            if name in root_dlls:
                pending.append(root_dlls[name])
            else:
                external.add(name)
    ordered = sorted(included, key=lambda p: p.relative_to(runtime).as_posix().casefold())
    manifest = {"schema": "tint-ppf-native-runtime.v1", "layout": ".runtime/vendor",
                "scope": "Private native cutting driver DLL dependency closure; not an independent driver reimplementation",
                "driver_modules": len(catalog["drivers"]), "catalog_profiles": catalog["counts"]["variant_profiles"],
                "files": [{"path": p.relative_to(runtime).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p)} for p in ordered],
                "external_imports": sorted(external),
                "external_runtime_requirements": [{"assembly": "Microsoft.VC80.CRT", "version": "8.0.50608.0", "architecture": "x86",
                                                   "reason": "Bundled LEADTools 15 side-by-side manifests"}],
                "prerequisites": "Windows .NET Framework 4 and a compatible Visual C++ 2005 x86 CRT assembly, in addition to the bundled newer DLLs. Device USB drivers are installed separately."}
    return manifest, ordered


def package(runtime: Path, output: Path) -> dict:
    runtime = runtime.resolve(strict=True)
    output = output.resolve()
    private_root = (ROOT / "distribution" / "private").resolve()
    if not output.is_relative_to(private_root) or output.suffix.casefold() != ".zip":
        raise ValueError("Native runtime archives must stay under distribution/private with a .zip suffix")
    manifest, files = inventory(runtime)
    output.parent.mkdir(parents=True, exist_ok=True)
    # 'x' refuses overwriting an earlier handoff.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in files:
            archive.write(file, ".runtime/vendor/" + file.relative_to(runtime).as_posix())
        archive.writestr(".runtime/vendor/runtime-manifest.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Native runtime ZIP CRC verification failed")
        for item in manifest["files"]:
            value = archive.read(".runtime/vendor/" + item["path"])
            if len(value) != item["bytes"] or hashlib.sha256(value).hexdigest() != item["sha256"]:
                raise ValueError("Native runtime ZIP member hash differs from source")
    digest = sha(output)
    checksum = output.with_suffix(output.suffix + ".sha256")
    with checksum.open("x", encoding="utf-8") as stream:
        stream.write(f"{digest}  {output.name}\n")
    return {"file": str(output), "bytes": output.stat().st_size, "sha256": digest,
            "dll_files": len(files), "uncompressed_dll_bytes": sum(i["bytes"] for i in manifest["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.runtime, args.output), indent=2))


if __name__ == "__main__":
    main()
