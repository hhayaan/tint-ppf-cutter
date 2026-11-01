"""Read-only PE imports, exports and printing-related static string inventory.

Does not load target DLLs, attach a process, open a device or send output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct

MAX_BYTES = 64 * 1024 * 1024
TOKENS = re.compile(
    r"(?i)(plotter|printer|print|spool|serial|parallel|tcp|winsock|socket|usb|"
    r"hpgl|dmpl|baud|parity|handshake|flow.?control|overcut|offset|calib|"
    r"resolution|resolucio|units|unitats|pressure|pressio|velocitat|speed|"
    r"acceler|pagina|paginacio|tile|panel|weed|pelat|cut.?order|tallar|"
    r"TDispIO|TPlotter|TDriverTall|CFGCONEXIO|JobManager|com[1-9]|lpt[1-9])")
COMMAND = re.compile(r"^(?:IN;|PA[;, %0-9]|PR[;, %0-9]|PU[;, %0-9]|PD[;, %0-9]|"
                     r"SP[;, %0-9]|VS[;, %0-9]|FS[;, %0-9]|OA[;?]|OH[;?]|"
                     r"IP[;, %0-9]|SC[;, %0-9]|PS[;, %0-9]|PG[; ]|"
                     r"RO[;, %0-9]|AA[;, %0-9]|AR[;, %0-9]|CI[;, %0-9]|"
                     r"!PG|%[-0-9]|[!~^][A-Za-z0-9,;])")


class PE:
    def __init__(self, path: Path):
        self.path = path
        with path.open("rb") as stream:
            self.data = stream.read(MAX_BYTES + 1)
        b = self.data
        if len(b) > MAX_BYTES or b[:2] != b"MZ":
            raise ValueError("Not a bounded PE image")
        self.pe = struct.unpack_from("<I", b, 0x3c)[0]
        if b[self.pe:self.pe + 4] != b"PE\0\0":
            raise ValueError("Missing PE signature")
        self.machine, count = struct.unpack_from("<HH", b, self.pe + 4)
        optsize = struct.unpack_from("<H", b, self.pe + 20)[0]
        opt = self.pe + 24
        magic = struct.unpack_from("<H", b, opt)[0]
        if magic not in (0x10b, 0x20b):
            raise ValueError("Unknown optional header")
        self.bits = 32 if magic == 0x10b else 64
        self.base = struct.unpack_from("<I" if self.bits == 32 else "<Q", b,
                                       opt + (28 if self.bits == 32 else 24))[0]
        self.directory_offset = opt + (96 if self.bits == 32 else 112)
        self.sections = []
        for index in range(count):
            offset = opt + optsize + index * 40
            name, virtual_size, rva, raw_size, raw = struct.unpack_from("<8sIIII", b, offset)
            self.sections.append({"name": name.rstrip(b"\0").decode("ascii", "replace"),
                                  "rva": rva, "virtual_size": virtual_size,
                                  "raw_offset": raw, "raw_size": raw_size})

    def offset(self, rva: int) -> int:
        for section in self.sections:
            delta = rva - section["rva"]
            if 0 <= delta < section["raw_size"]:
                offset = section["raw_offset"] + delta
                if offset >= len(self.data):
                    break
                return offset
        raise ValueError(f"RVA {rva:#x} is not file-backed")

    def string(self, offset: int) -> str:
        end = self.data.index(b"\0", offset, min(len(self.data), offset + 32768))
        return self.data[offset:end].decode("ascii", "replace")

    def directory(self, index: int) -> tuple[int, int]:
        return struct.unpack_from("<II", self.data, self.directory_offset + index * 8)

    def exports(self) -> list[dict]:
        rva, size = self.directory(0)
        if not rva:
            return []
        off = self.offset(rva)
        ordinal_base, count, names_count, functions, names, ordinals = struct.unpack_from(
            "<IIIIII", self.data, off + 16)
        name_map = {}
        for i in range(names_count):
            name_rva = struct.unpack_from("<I", self.data, self.offset(names) + i * 4)[0]
            ordinal_index = struct.unpack_from("<H", self.data, self.offset(ordinals) + i * 2)[0]
            name_map.setdefault(ordinal_index, []).append(self.string(self.offset(name_rva)))
        rows = []
        for i in range(count):
            function_rva = struct.unpack_from("<I", self.data, self.offset(functions) + i * 4)[0]
            if not function_rva:
                continue
            for name in name_map.get(i, [None]):
                rows.append({"ordinal": ordinal_base + i, "name": name,
                             "rva": hex(function_rva), "address": hex(self.base + function_rva),
                             "forwarder": self.string(self.offset(function_rva)) if rva <= function_rva < rva + size else None})
        return rows

    def imports(self) -> list[dict]:
        rva, size = self.directory(1)
        if not rva:
            return []
        position = self.offset(rva)
        result = []
        word_size = self.bits // 8
        high_bit = 1 << (self.bits - 1)
        fmt = "<I" if self.bits == 32 else "<Q"
        for descriptor in range(4096):
            original, timestamp, chain, name_rva, first = struct.unpack_from("<IIIII", self.data, position)
            if not any((original, timestamp, chain, name_rva, first)):
                return result
            module = self.string(self.offset(name_rva))
            thunk = self.offset(original or first)
            functions = []
            for index in range(100000):
                value = struct.unpack_from(fmt, self.data, thunk + index * word_size)[0]
                if not value:
                    break
                functions.append({"name": None, "ordinal": value & 0xffff} if value & high_bit
                                 else {"name": self.string(self.offset(value) + 2), "ordinal": None})
            else:
                raise ValueError("Import thunk budget exhausted")
            result.append({"dll": module, "functions": functions})
            position += 20
        raise ValueError("Import descriptor budget exhausted")

    def strings(self) -> tuple[list[dict], list[dict]]:
        interesting, commands = [], []
        for section in self.sections:
            if section["name"] not in (".rdata", ".data", ".rsrc"):
                continue
            start = section["raw_offset"]
            data = self.data[start:start + section["raw_size"]]
            for encoding, pattern in [("ascii", rb"[\x20-\x7e]{3,}"),
                                      ("utf-16-le", rb"(?:[\x20-\x7e]\0){3,}")]:
                for match in re.finditer(pattern, data):
                    text = match.group().decode(encoding)
                    if text.startswith("?"):
                        continue  # Already retained without noise in exports/imports.
                    row = {"offset": hex(start + match.start()), "section": section["name"],
                           "encoding": encoding, "text": text}
                    if TOKENS.search(text):
                        interesting.append(row)
                    if len(text) < 250 and COMMAND.search(text):
                        commands.append(row)
        return interesting, commands

    def inspect(self) -> dict:
        exports = self.exports()
        imports = self.imports()
        strings, commands = self.strings()
        return {"path": str(self.path.resolve()), "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(), "bits": self.bits,
                "machine": hex(self.machine), "image_base": hex(self.base),
                "sections": self.sections, "exports": exports, "imports": imports,
                "printing_strings": strings, "protocol_string_candidates": commands,
                "coverage": {"exports": len(exports), "import_dlls": len(imports),
                             "import_functions": sum(len(d["functions"]) for d in imports),
                             "printing_strings": len(strings), "protocol_candidates": len(commands)},
                "limitations": ["Static names and string literals do not prove runtime reachability or exact command generation.",
                                "Delay imports and dynamic GetProcAddress resolution are not decoded by this probe.",
                                "No code executed, no device opened, no process attached."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args()
    names = args.modules or ["ccut60.dll", "gsfwkcut62.dll", "gsprint62.dll", "fdlib40.dll",
                             "gslibio62.dll", "drvcut/graphtec.dll", "drvcut/summaHPGL.dll",
                             "drvcut/summaDMPL.dll", "drvcut/iolineDMPL.dll"]
    records = []
    for name in names:
        path = args.root / name
        if not path.resolve().is_relative_to(args.root.resolve()):
            raise ValueError("Module escaped selected installation directory")
        records.append(PE(path).inspect())
    report = {"schema": "printing-static-probe.v1", "observed_utc": datetime.now(timezone.utc).isoformat(),
              "source_open_mode": "rb", "modules": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "modules": [{"name": Path(r["path"]).name,
          "bits": r["bits"], **r["coverage"]} for r in records]}))


if __name__ == "__main__":
    main()
