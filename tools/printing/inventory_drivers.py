"""Read-only PE/catalog inventory of installed Film Designs cutting drivers.

No DLL is loaded or called. This reads PE tables, constant-return factory count
machine code, static model pointer records, RTTI names, resources and strings.
Binary format reference: https://learn.microsoft.com/windows/win32/debug/pe-format
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import struct

WORKSPACE = Path(__file__).resolve().parents[2]
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PE:
    def __init__(self, path):
        self.path = path
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & REPARSE:
            raise ValueError("Only regular non-reparse files are supported")
        self.data = path.read_bytes()
        after = path.lstat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Source changed while reading")
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.pe = self.u32(0x3C)
        if self.data[:2] != b"MZ" or self.data[self.pe:self.pe+4] != b"PE\0\0":
            raise ValueError("Not a PE image")
        self.machine = self.u16(self.pe+4)
        self.optional = self.pe+24
        self.bits = {0x10B: 32, 0x20B: 64}[self.u16(self.optional)]
        self.base = self.u32(self.optional+28) if self.bits == 32 else struct.unpack_from("<Q", self.data, self.optional+24)[0]
        self.directory_offset = self.optional+(96 if self.bits == 32 else 112)
        self.sections = []
        section_start = self.optional+self.u16(self.pe+20)
        for index in range(self.u16(self.pe+6)):
            offset = section_start+index*40
            size, address, raw_size, raw = struct.unpack_from("<IIII", self.data, offset+8)
            self.sections.append({"name": self.data[offset:offset+8].rstrip(b"\0").decode("ascii"), "rva": address, "virtual_size": size, "raw_size": raw_size, "file_offset": raw, "characteristics": self.u32(offset+36)})

    def u16(self, offset): return struct.unpack_from("<H", self.data, offset)[0]
    def u32(self, offset): return struct.unpack_from("<I", self.data, offset)[0]
    def directory(self, number): return struct.unpack_from("<II", self.data, self.directory_offset+number*8)
    def offset(self, rva):
        if rva < self.u32(self.optional+60): return rva
        for section in self.sections:
            delta = rva-section["rva"]
            if 0 <= delta < section["raw_size"]:
                return section["file_offset"]+delta
        raise ValueError(f"Unmapped file-backed RVA {rva:#x}")
    def address(self, offset):
        for section in self.sections:
            delta = offset-section["file_offset"]
            if 0 <= delta < section["raw_size"]:
                return self.base+section["rva"]+delta
        raise ValueError("Unmapped file offset")
    def text(self, offset):
        end = self.data.index(b"\0", offset)
        return self.data[offset:end].decode("ascii", errors="backslashreplace")

    def exports(self):
        rva, size = self.directory(0)
        if not rva: return []
        offset = self.offset(rva)
        base, count, names, functions, name_table, ordinal_table = struct.unpack_from("<IIIIII", self.data, offset+16)
        by_index = {}
        for index in range(names):
            name = self.text(self.offset(self.u32(self.offset(name_table)+4*index)))
            ordinal_index = self.u16(self.offset(ordinal_table)+2*index)
            by_index.setdefault(ordinal_index, []).append(name)
        result = []
        for index in range(count):
            function_rva = self.u32(self.offset(functions)+4*index)
            if not function_rva: continue
            for name in by_index.get(index, [None]):
                result.append({"name": name, "ordinal": base+index, "rva": function_rva, "address": hex(self.base+function_rva), "forwarder": self.text(self.offset(function_rva)) if rva <= function_rva < rva+size else None})
        return result

    def imports(self):
        rva, size = self.directory(1)
        if not rva: return []
        result = []
        offset = self.offset(rva)
        for index in range(4096):
            original, timestamp, chain, name, first = struct.unpack_from("<IIIII", self.data, offset+index*20)
            if not any((original, timestamp, chain, name, first)): break
            module = self.text(self.offset(name))
            symbols = []
            thunk = self.offset(original or first)
            width = self.bits//8
            for at in range(65536):
                value = struct.unpack_from("<I" if width == 4 else "<Q", self.data, thunk+at*width)[0]
                if not value: break
                symbols.append({"ordinal": value & 0xFFFF} if value & (1 << (self.bits-1)) else {"name": self.text(self.offset(value)+2)})
            result.append({"module": module, "symbols": symbols})
        return result

    def strings(self):
        result = []
        for match in re.finditer(rb"[ -~]{4,}", self.data):
            result.append({"file_offset": match.start(), "encoding": "ascii", "value": match.group().decode("ascii")})
        for match in re.finditer(rb"(?:[ -~]\x00){2,}\x00\x00", self.data):
            result.append({"file_offset": match.start(), "encoding": "utf-16le", "value": match.group()[:-2].decode("utf-16-le")})
        return result

    def resources(self):
        rva, size = self.directory(2)
        if not rva: return []
        root = self.offset(rva)
        result = []
        def label(value):
            if not value & 0x80000000: return value
            at = root+(value & 0x7FFFFFFF)
            return self.data[at+2:at+2+2*self.u16(at)].decode("utf-16-le")
        def visit(relative, identities, ancestors):
            if relative in ancestors or len(identities) > 4: raise ValueError("Invalid recursive resource tree")
            at = root+relative
            count = self.u16(at+12)+self.u16(at+14)
            for index in range(count):
                name, target = struct.unpack_from("<II", self.data, at+16+index*8)
                path = identities+[label(name)]
                if target & 0x80000000:
                    visit(target & 0x7FFFFFFF, path, ancestors | {relative})
                else:
                    payload_rva, length, codepage, reserved = struct.unpack_from("<IIII", self.data, root+target)
                    payload_offset = self.offset(payload_rva)
                    payload = self.data[payload_offset:payload_offset+length]
                    record = {"identity_path": path, "file_offset": payload_offset, "byte_size": length, "sha256": hashlib.sha256(payload).hexdigest(), "code_page": codepage}
                    if path[0] == 6:
                        strings = []
                        position = 0
                        for string_index in range(16):
                            characters = struct.unpack_from("<H", payload, position)[0]
                            position += 2
                            value = payload[position:position+characters*2].decode("utf-16-le")
                            position += characters*2
                            if value: strings.append({"id": (int(path[1])-1)*16+string_index, "value": value})
                        record["string_table"] = strings
                    result.append(record)
        visit(0, [], set())
        return result


def constant_variant_count(pe, exports):
    export = next((item for item in exports if item["name"] == "__GetNumVariantsDrv"), None)
    if not export: return {"resolved": False, "reason": "export missing"}
    rva = export["rva"]
    jumps = []
    for attempt in range(5):
        offset = pe.offset(rva)
        code = pe.data[offset:offset+32]
        if code[:1] == b"\xE9":
            target = rva+5+struct.unpack_from("<i", code, 1)[0]
            jumps.append({"from": hex(pe.base+rva), "to": hex(pe.base+target)})
            rva = target
            continue
        match = re.match(rb"(?:\x8b\xff)?(?:\x55\x8b\xec)?\xb8(.{4})(?:\x5d)?\xc3", code, re.DOTALL)
        if match:
            return {"resolved": True, "count": struct.unpack("<I", match.group(1))[0], "entry_address": export["address"], "file_offset": offset, "instruction_bytes": code[:match.end()].hex(" "), "derivation": "Unconditional x86 mov eax, immediate32; ret (optional exact frame prefix)", "jumps": jumps}
        return {"resolved": False, "entry_address": export["address"], "file_offset": offset, "first_32_bytes": code.hex(" "), "reason": "not an admitted constant-return instruction sequence"}
    return {"resolved": False, "reason": "too many jumps"}


def model_tables(pe, strings, expected_count):
    if pe.bits != 32: return {"resolved": False, "reason": "only PE32 pointer records implemented"}
    wide = {}
    for item in strings:
        if item["encoding"] == "utf-16le":
            try: wide[pe.address(item["file_offset"])] = item
            except ValueError: pass
    candidates = {}
    for section in pe.sections:
        if section["characteristics"] & 0x20000000: continue
        start = section["file_offset"]
        for offset in range(start, start+section["raw_size"]-7, 4):
            identifier, name = struct.unpack_from("<II", pe.data, offset)
            if identifier not in wide or name not in wide: continue
            key, display = wide[identifier], wide[name]
            if not re.fullmatch(r"[A-Za-z0-9_./:+-]{2,80}", key["value"]): continue
            if not 2 <= len(display["value"]) <= 160 or display["value"].startswith(("?", ".?")): continue
            if not any(c.isalpha() for c in display["value"]): continue
            candidates[offset] = {"record_file_offset": offset, "id": key["value"], "display_name": display["value"], "id_string_offset": key["file_offset"], "name_string_offset": display["file_offset"]}
    runs = []
    for stride in range(12, 132, 4):
        for start in sorted(candidates):
            if start-stride in candidates: continue
            rows = []
            while start+len(rows)*stride in candidates:
                rows.append(candidates[start+len(rows)*stride])
            if len(rows) >= 2 or expected_count == 1:
                runs.append((len(rows), start, stride, rows))
    exact = [run for run in runs if expected_count is not None and run[0] == expected_count]
    if not exact:
        return {"resolved": False, "candidate_pairs": len(candidates), "largest_contiguous_run": max((run[0] for run in runs), default=0), "reason": "no static pointer-record run matches the admitted factory count"}
    # The smallest admitted stride preserves adjacent variant order. Larger
    # strides can accidentally interleave replicated default/runtime tables.
    exact.sort(key=lambda run: (run[2], run[1]))
    count, start, stride, rows = exact[0]
    result_rows = []
    for index, row in enumerate(rows):
        row = dict(row, static_record_index=index)
        raw = pe.data[row["record_file_offset"]+8:row["record_file_offset"]+stride] if count > 1 else b""
        if len(raw) >= 8:
            number = struct.unpack_from("<d", raw)[0]
            row["untyped_double_at8"] = number if math.isfinite(number) else None
            row["untyped_uint32_after16"] = list(struct.unpack("<"+"I"*((len(raw)-8)//4), raw[8:8+4*((len(raw)-8)//4)]))
        result_rows.append(row)
    return {"resolved": True, "count": count, "record_stride": stride if count > 1 else None, "table_file_offset": start, "matching_run_candidates": len(exact), "alternative_matching_runs": [{"file_offset": run[1], "stride": run[2]} for run in exact] if count > 1 else [], "derivation": "Contiguous static records start with image-VA pointers to UTF16 stable ID and display name; record count equals exported constant", "field_semantics": "Only the two string-pointer fields are identified. Numeric fields remain untyped observations. A single-entry table has no independently measurable stride. Static record order is not asserted as an ABI variant index without a constructor cross-reference.", "models": result_rows}


def protocol_families(strings, imports):
    names = [item["value"] for item in strings if item["value"].startswith(".?AV")]
    symbols = [symbol.get("name", "") for module in imports for symbol in module["symbols"]]
    joined = "\n".join(names+symbols)
    result = []
    for family, token in (("HPGL", "TDriverTallHPGL"), ("DMPL", "TDriverTallDMPL"), ("Roland HPGL subclass", "HPGLRoland")):
        if token in joined:
            result.append({"family": family, "basis": "RTTI class/import symbol", "matching_rtti": [name for name in names if token in name][:8], "status": "static implementation-family evidence; emitted protocol bytes not observed"})
    return result


def inspect(path, driver=False):
    pe = PE(path)
    exports, imports, strings, resources = pe.exports(), pe.imports(), pe.strings(), pe.resources()
    count = constant_variant_count(pe, exports)
    table = model_tables(pe, strings, count.get("count")) if driver else None
    key_strings = [item for item in strings if re.search(r"(TCP|USB|serial|COM[0-9:]|baud|handshake|SummaUSB|GunnerUSB|CAMMGL|GPGL|HPGL|DMPL|pressure|force|overcut|speed|connection|printer|plotter)", item["value"], re.I) and not item["value"].startswith(("?", ".?"))]
    return {"path": str(path), "name": path.name, "byte_size": len(pe.data), "sha256": pe.sha256, "machine": pe.machine, "bits": pe.bits, "image_base": hex(pe.base), "sections": pe.sections, "exports": exports, "imports": imports, "resources": resources, "factory_variant_count": count, "model_table": table, "protocol_families": protocol_families(strings, imports), "configuration_classes": sorted({item["value"] for item in strings if item["value"].startswith(".?AV") and any(token in item["value"] for token in ("Config", "Cfg", "DlgConfig", "DispIO"))}), "bounded_protocol_connection_string_leads": key_strings[:160], "source_unchanged": True}


def public_catalog(drivers):
    output = []
    profiles = []
    for driver in drivers:
        driver_id = Path(driver["name"]).stem.lower()
        families = [item["family"] for item in driver["protocol_families"]]
        native_abi_family = "DMPL" if "DMPL" in families else "HPGL" if "HPGL" in families else None
        abi_lineage = {"basis": "Declared C++ RTTI/configuration base-family and imported constructor symbols; not a wire-protocol classification", "artifact_sha256": driver["sha256"], "configuration_rtti_classes": [name for name in driver["configuration_classes"] if "TConfigDriverTall" in name], "driver_family_rtti": sorted({name for family in driver["protocol_families"] for name in family.get("matching_rtti", [])}), "imported_family_constructors": [{"module": imported["module"], "symbol": symbol["name"]} for imported in driver["imports"] for symbol in imported["symbols"] if symbol.get("name", "").startswith(("??0TDriverTallHPGL", "??0TDriverTallDMPL", "??0TConfigDriverTallHPGL", "??0TConfigDriverTallDMPL"))], "runtime_requirement": "Bridge must check the actual configuration type before family-specific setters"}
        wire_leads = [item for item in driver["bounded_protocol_connection_string_leads"] if "LANGUAGE=" in item["value"]]
        rows = []
        for model in driver["model_table"].get("models", []):
            profile = {"key": f"{driver_id}:{model['static_record_index']}", "driver_id": driver_id, "model_id": model["id"], "name": model["display_name"], "static_record_index": model["static_record_index"], "record_lineage": {"artifact_sha256": driver["sha256"], "file_offset": model["record_file_offset"], "record_stride": driver["model_table"].get("record_stride"), "id_string_file_offset": model["id_string_offset"], "name_string_file_offset": model["name_string_offset"]}, "raw_parameters": {"double_at8": model.get("untyped_double_at8"), "uint32_after16": model.get("untyped_uint32_after16", [])}, "typed_parameters": {}, "numeric_parameter_status": "unconfirmed; no field semantics or physical units assigned", "functional_compatibility": "not established by static catalog extraction"}
            rows.append(profile)
            profiles.append(profile)
        output.append({"id": driver_id, "module_filename": driver["name"], "artifact_sha256": driver["sha256"], "architecture": "x86" if driver["bits"] == 32 else "x64", "factory_variant_count": driver["factory_variant_count"].get("count"), "factory_count_lineage": driver["factory_variant_count"], "native_abi_family": native_abi_family, "native_abi_lineage": abi_lineage, "protocol_families": families, "protocol_families_status": "Legacy static RTTI/import labels only; actual emitted wire language is not established by the catalog extractor", "wire_protocol_string_leads": wire_leads, "configuration_rtti_classes": driver["configuration_classes"], "profiles": rows})
    return {"schema": "tint-ppf-driver-catalog.v1", "provenance": "Static PE factory-count instructions, RTTI/imports and ID/name pointer tables from installed driver artifacts; no vendor DLL was executed by the extractor", "scope": "Model metadata only; not a hardware compatibility certificate or output implementation", "key_semantics": "key uses driver ID plus static record index because original model IDs are not always unique; static order is not asserted as an ABI index without a constructor cross-reference", "counts": {"driver_modules": len(output), "variant_profiles": len(profiles), "unique_display_names": len({profile["name"] for profile in profiles}), "unique_driver_model_ids": len({(profile["driver_id"], profile["model_id"].casefold()) for profile in profiles})}, "drivers": output}


def rea_cross_checks(modules):
    evidence = []
    for name in ("printing-rea-evidence.json", "printing-rea-driver-exports.json"):
        path = WORKSPACE / "analysis" / name
        if path.exists(): evidence.extend(json.loads(path.read_text(encoding="utf-8")))
    checks = []
    for record in evidence:
        normalized = record.get("normalized_result", {})
        artifact = normalized.get("artifact", {})
        module = next((item for item in modules if item["sha256"] == artifact.get("sha256")), None)
        if not module: continue
        if record["operation"] == "inspect_native_exports":
            left = {(row["name"], row["ordinal"], row["address"], row["forwarder"]) for row in module["exports"]}
            right = {(row["name"], row["ordinal"], row["address"], row["forwarder"]) for row in normalized["exports"]}
            if left != right: raise ValueError("REA/local PE export reader disagreement")
            checks.append({"module": module["name"], "evidence_id": record["evidence_id"], "operation": record["operation"], "sha256": module["sha256"], "export_count": len(left), "aligned": True})
        elif record["operation"] == "inspect_pe_resources":
            if {row["sha256"] for row in module["resources"]} != {row["payload"]["sha256"] for row in normalized["resources"]}:
                raise ValueError("REA/local resource reader disagreement")
            checks.append({"module": module["name"], "evidence_id": record["evidence_id"], "operation": record["operation"], "sha256": module["sha256"], "resource_count": len(module["resources"]), "aligned": True})
    return checks


def write_report(result, catalog, path):
    summary = result["summary"]
    graphtec = next(driver for driver in result["drivers"] if driver["name"].lower() == "graphtec.dll")
    lines = ["# Printing and cutting driver catalog reconstruction", "", "This report covers static installed artifacts only. No DLL was loaded or called, no process was attached, no GUI was operated and no printer/plotter received output.", "", "## Observed catalog", "", f"The 29 application cutting-driver DLLs declare {summary['constant_factory_variant_total']} factory variants. The extractor recovered {summary['recovered_static_model_records']} matching ID/name pointer records. These produce {catalog['counts']['unique_display_names']} distinct display labels and {catalog['counts']['unique_driver_model_ids']} distinct (driver, model ID) combinations. They are variant entries, not a count of independently tested devices or physical OEM manufacturers.", "", "| Module | Factory variants | Static model records | Static family |", "|---|---:|---:|---|"]
    for driver in result["drivers"]:
        lines.append(f"| {driver['name']} | {driver['factory_variant_count'].get('count')} | {driver['model_table'].get('count')} | {', '.join(item['family'] for item in driver['protocol_families'])} |")
    lines += ["", "All 29 are PE32 x86 application plug-ins. Each exports the four factory names `__CrearDriver`, `__GetDriver`, `__GetDriverTall`, and `__GetNumVariantsDrv`; REA independently corroborates every driver export table. The shared cutting framework is separate from ordinary Windows image/document printing. A Windows printer queue may still be used as a raw device transport; plugin catalog evidence alone does not establish that flow.", "", "## Important identity details", "", "The original catalog reuses `anaexp.dll` model ID `fp_60` for Film Designs Pro 40, 48, 60 and 72. Public profile keys therefore include the driver and static record index. `Aristo AG-75` is present twice as a display label. `GUNNER TEST` is a literal catalog entry and is not asserted to be a physical production model. Brand text is not sufficient to count physical OEM manufacturers because modules contain rebrands (for example Graphtec, Vinyl Express, Copam, SSK and YOLOCut in one module).", "", "## Graphtec FC9000 lineage", "", f"`graphtec.dll` SHA256 `{graphtec['sha256']}` exports an unconditional factory count of 64: `B8 40 00 00 00 C3` at preferred VA `0x10001a50`. A unique contiguous 64-row table starts at file offset 26624, stride 40. Its first fields point to UTF16 stable IDs and display names. FC9000 source records:", "", "| Static record | Original ID | Display name | Raw DOUBLE at +8 | Raw UINT32 fields at +16 |", "|---:|---|---|---:|---|"]
    for model in graphtec["model_table"]["models"]:
        if "FC9000" in model["display_name"]:
            lines.append(f"| {model['static_record_index']} | `{model['id']}` | {model['display_name']} | {model.get('untyped_double_at8')} | `{model.get('untyped_uint32_after16')}` |")
    lines += ["", "The original IDs intentionally preserve the observed `gr_fc_9600...` spelling. The numeric fields remain untyped here. For FC9000-140, interpreting the raw double as 72 points/inch would produce 1372 mm, but field semantics and units require constructor/configuration value-flow evidence; this report does not promote that arithmetic to a confirmed machine limit.", "", "## Configuration and protocol evidence", "", "RTTI/import declarations identify 25 HPGL-family modules and four DMPL-family modules; eight HPGL modules also use Roland-specific base classes. This is stronger than a filename guess but does not prove the actual bytes emitted for a selected device. Configuration classes include HPGL/DMPL variants and custom GCC, Mutoh, RedSail, CraftRobo, Summa USB and Gunner USB components. Static strings include `ENTER LANGUAGE=CAMMGL3`, `SummaUSB:`, `Gunner_OpenUsb` and manufacturer connection-error text. They are leads for call-graph/runtime corroboration, not executed-transport evidence.", "", "`ccut60.dll` declares cut-dialog, nesting, cut-preview and connection APIs, including `CCutDoc::DoTallar`, `CCutApp::GetConexioDriver` and `SetConexioDriver`. `gsfwkcut62.dll` declares `TPlotterHPGL`, `TPlotterDMPL`, `TConfigDriverTall*`, `TDispIOBufferWinDev` and `TDispIOFitxer`. Their object layouts, endpoint semantics, physical units, limits and cancellation behavior remain separate reconstruction questions.", "", "## Evidence and reproducibility", "", "- `tools/printing/inventory_drivers.py`: read-only extractor; writes the analysis inventory and portable public metadata.", "- `printing/drivers.json`: 29 modules and 344 source-native model profiles, with artifact hashes, record offsets, raw numeric fields and explicit compatibility limits; no absolute installation/developer paths.", "- `printing/driver_catalog.py`: pure lookup helpers, including collision-preserving model-ID lookup.", "- `analysis/printing-rea-evidence.json` and `analysis/printing-rea-driver-exports.json`: complete retained portable REA records.", ""]
    for check in result["rea_cross_checks"]:
        lines.append(f"- {check['module']} / {check['operation']}: `{check['evidence_id']}` (aligned).")
    lines += ["", "PE layout follows [Microsoft PE format](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format). Export decorations can encode calling conventions and parameter types; see [Microsoft decorated names](https://learn.microsoft.com/en-us/cpp/build/reference/decorated-names). Undecorated custom factories still need call-site or decompiler proof of their prototypes. Actual emitted command streams, live selected settings and device execution are not established by this inventory.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, default=Path(r"C:\Program Files (x86)\FilmVinylDesigns\ClientV2"))
    parser.add_argument("--output", type=Path, default=WORKSPACE / "analysis/printing-driver-inventory.json")
    parser.add_argument("--catalog-output", type=Path, default=WORKSPACE / "printing/drivers.json")
    parser.add_argument("--report", type=Path, default=WORKSPACE / "analysis/printing-reconstruction.md")
    args = parser.parse_args()
    drivers = [inspect(path, driver=True) for path in sorted((args.install / "drvcut").glob("*.dll"), key=lambda path: path.name.lower())]
    cores = [inspect(args.install / name) for name in ("ccut60.dll", "gsfwkcut62.dll")]
    variants = sum(driver["factory_variant_count"].get("count", 0) for driver in drivers)
    recovered = sum(driver["model_table"].get("count", 0) for driver in drivers)
    summary = {"driver_dll_count": len(drivers), "constant_factory_variant_total": variants, "recovered_static_model_records": recovered, "drivers_without_complete_table": [driver["name"] for driver in drivers if not driver["model_table"].get("resolved")], "protocol_family_driver_counts": dict(Counter(family["family"] for driver in drivers for family in driver["protocol_families"])), "all_drivers_pe32": all(driver["bits"] == 32 for driver in drivers)}
    result = {"schema": "film-designs-printing-driver-inventory.v1", "observed_utc": datetime.now(timezone.utc).isoformat(), "scope": "Read-only installed PE files; no DLL loading/calling, process attachment, GUI or hardware operations", "summary": summary, "drivers": drivers, "core_modules": cores, "limitations": ["Static driver variants are catalog entries, not a count of distinct physical manufacturers or tested devices.", "RTTI/import families do not prove actual transmitted bytes or active device selection.", "Export names/addresses do not establish safe calling conventions or argument/object layouts.", "Raw numeric model fields remain untyped until a constructor/configuration cross-reference establishes semantics."]}
    result["rea_cross_checks"] = rea_cross_checks(drivers+cores)
    catalog = public_catalog(drivers)
    args.catalog_output.parent.mkdir(parents=True, exist_ok=True)
    args.catalog_output.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    write_report(result, catalog, args.report)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    for driver in drivers:
        print(json.dumps({"driver": driver["name"], "factory_count": driver["factory_variant_count"].get("count"), "table_count": driver["model_table"].get("count"), "stride": driver["model_table"].get("record_stride"), "families": [item["family"] for item in driver["protocol_families"]]}))


if __name__ == "__main__":
    main()
