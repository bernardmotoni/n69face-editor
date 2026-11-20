"""Minimal ELF32 little-endian (ARM) reader for the N69 watch face modules."""
import struct


class Elf:
    def __init__(self, data: bytes):
        self.d = data
        if data[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
        raw = [struct.unpack_from("<10I", data, shoff + i * shentsize) for i in range(shnum)]
        strtab_off = raw[shstrndx][4]
        self.sections = {}
        for s in raw:
            end = data.index(b"\0", strtab_off + s[0])
            name = data[strtab_off + s[0]:end].decode()
            self.sections[name] = dict(type=s[1], addr=s[3], off=s[4], size=s[5], entsize=s[9])
        self._symbols = None

    @staticmethod
    def size_at(data: bytes, off: int) -> int:
        shoff, = struct.unpack_from("<I", data, off + 0x20)
        shentsize, shnum = struct.unpack_from("<HH", data, off + 0x2E)
        return shoff + shentsize * shnum

    def v2o(self, addr: int) -> int:
        for name, s in self.sections.items():
            if s["addr"] and s["type"] != 8 and s["addr"] <= addr < s["addr"] + s["size"]:
                return s["off"] + addr - s["addr"]
        raise ValueError(f"address outside sections: {addr:#x}")

    def symbols(self):
        """[(name, value, size, shndx)] in .dynsym order (the index matters for relocations)."""
        if self._symbols is None:
            ds, dstr = self.sections[".dynsym"], self.sections[".dynstr"]
            out = []
            for i in range(ds["size"] // 16):
                n, val, size, info, other, shndx = struct.unpack_from("<IIIBBH", self.d, ds["off"] + i * 16)
                end = self.d.index(b"\0", dstr["off"] + n)
                out.append((self.d[dstr["off"] + n:end].decode(), val, size, shndx))
            self._symbols = out
        return self._symbols

    def relocs(self):
        """[(index, offset, info)] from .rel.dyn."""
        r = self.sections.get(".rel.dyn")
        if not r:
            return []
        return [(i, *struct.unpack_from("<II", self.d, r["off"] + i * 8)) for i in range(r["size"] // 8)]


def find_elfs(data: bytes) -> list:
    out, i = [], data.find(b"\x7fELF")
    while i != -1:
        out.append(i)
        i = data.find(b"\x7fELF", i + 4)
    return out
