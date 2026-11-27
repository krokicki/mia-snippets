"""One image per member (JPEG or PNG, told apart by the member's first bytes) from the members of 7z archives
chosen by glob, stacked along z. A colour member's samples are a `c` axis (`zcyx`); a grey member is one plane
(`zyx`), its values as the decoder gives them. With `palette: index` a palette (indexed-colour) PNG is one
plane of its stored palette indices, e.g. the class ids of an indexed mask, never the palette's colours;
without the param a palette PNG raises.

Each url is a 7z archive, staged whole with `io.fetch`. Its header is parsed here (the 7z container format,
with the standard library only) and the members are decoded from folders packed with LZMA2, LZMA or Copy. A
solid folder is one compressed stream of many members: `read` decodes it up to the member it needs, starting at
the last point before the member where an LZMA2 stream resets its dictionary.
"""
import lzma
import struct
import zlib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NamedTuple

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
PALETTE, GREY = 3, 0            # IHDR colour types
PALETTE_MODES = ("index",)

SEVENZ = b"7z\xbc\xaf\x27\x1c"
START = 32                      # the signature header's length; pack streams are addressed from its end
HEADER_MAX = 64 << 20           # a packed or unpacked 7z header above this is refused
NESTED_MAX = 4                  # a packed header may unpack to another packed header; this many times
DICT_MAX = 1 << 30              # the decoder allocates the dictionary a folder's coder states
BLOCK = 1 << 20                 # packed bytes read, and unpacked bytes skipped, at a time
# property ids of the 7z header
END, HEADER, ARCHIVE_PROPERTIES, ADDITIONAL_STREAMS, MAIN_STREAMS, FILES = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
PACK_INFO, UNPACK_INFO, SUBSTREAMS, SIZE, CRC, FOLDER = 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B
UNPACK_SIZES, NUM_STREAMS, EMPTY_STREAM, NAMES, ENCODED_HEADER = 0x0C, 0x0D, 0x0E, 0x11, 0x17
# coder ids
COPY, LZMA, LZMA2 = b"\x00", b"\x03\x01\x01", b"\x21"


class _Coder(NamedTuple):
    method: bytes
    streams_in: int
    streams_out: int
    props: bytes


class _FolderInfo(NamedTuple):
    """One folder of the header: a chain of coders that turns pack streams into one unpacked stream."""
    coders: tuple[_Coder, ...]
    pack_streams: int
    size: int                   # of the unpacked stream
    crc: int | None


class _Header:
    """Reads the property structures of a 7z header from bytes."""

    def __init__(self, data: bytes, where: str):
        self._data, self._pos, self._where = data, 0, where

    def fail(self, what: str) -> ValueError:
        return ValueError(f"{self._where}: 7z header: {what} (at byte {self._pos} of {len(self._data)})")

    def take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise self.fail(f"{n} bytes wanted past its end")
        self._pos += n
        return self._data[self._pos - n:self._pos]

    def byte(self) -> int:
        return self.take(1)[0]

    def number(self) -> int:
        """A 7z NUMBER: the leading 1 bits of the first byte count the bytes that follow (little endian), the
        bits below them are the value's high bits."""
        first, value = self.byte(), 0
        for i in range(8):
            mask = 0x80 >> i
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
        return value

    def count(self) -> int:
        """A NUMBER that counts entries each of which takes at least a bit of the header."""
        n = self.number()
        if n > 8 * len(self._data):
            raise self.fail(f"a count of {n} in a header of {len(self._data)} bytes")
        return n

    def expect(self, wanted: int, what: str) -> None:
        got = self.byte()
        if got != wanted:
            raise self.fail(f"property id {got:#04x} where {what} ({wanted:#04x}) is expected")

    def bits(self, n: int) -> list[bool]:
        raw = self.take((n + 7) // 8)
        return [bool(raw[i >> 3] & (0x80 >> (i & 7))) for i in range(n)]

    def digests(self, n: int) -> list[int | None]:
        """CRC32s of n streams; a stream may have none."""
        defined = [True] * n if self.byte() else self.bits(n)
        return [struct.unpack("<I", self.take(4))[0] if d else None for d in defined]

    def skip_property(self) -> None:
        self.take(self.number())

    def _folder(self) -> tuple[tuple[_Coder, ...], int, int]:
        """One folder: its coders, the number of its pack streams and the index of its unpacked stream among
        the coders' output streams."""
        coders = []
        for _ in range(self.count()):
            flag = self.byte()
            if flag & 0xC0:
                raise self.fail(f"coder flags {flag:#04x} use reserved bits")
            method = self.take(flag & 0x0F)
            streams_in, streams_out = (self.count(), self.count()) if flag & 0x10 else (1, 1)
            props = self.take(self.number()) if flag & 0x20 else b""
            coders.append(_Coder(method, streams_in, streams_out, props))
        total_in, total_out = sum(c.streams_in for c in coders), sum(c.streams_out for c in coders)
        if not coders or total_out < 1:
            raise self.fail("a folder without a coder or without an output stream")
        bound = set()
        for _ in range(total_out - 1):
            self.number()                   # the input stream a coder's output feeds
            bound.add(self.number())
        packed = total_in - (total_out - 1)
        if packed < 1:
            raise self.fail("a folder without a pack stream")
        if packed > 1:
            for _ in range(packed):
                self.number()
        free = [k for k in range(total_out) if k not in bound]
        if len(free) != 1:
            raise self.fail("a folder whose coders do not end in one unpacked stream")
        return tuple(coders), packed, free[0]

    def streams(self) -> tuple[int, list[int], list[_FolderInfo], list[list[tuple[int, int | None]]]]:
        """A StreamsInfo: (position of the first pack stream, pack stream sizes, folders, and per folder the
        (size, crc) of each file stored in it, in order)."""
        pack_pos, pack_sizes, folders = 0, [], []
        files: list[list[tuple[int, int | None]]] | None = None
        kind = self.byte()
        if kind == PACK_INFO:
            pack_pos, n = self.number(), self.count()
            self.expect(SIZE, "the pack sizes")
            pack_sizes = [self.number() for _ in range(n)]
            kind = self.byte()
            if kind == CRC:
                self.digests(n)
                kind = self.byte()
            if kind != END:
                raise self.fail(f"property id {kind:#04x} in the pack info")
            kind = self.byte()
        if kind == UNPACK_INFO:
            self.expect(FOLDER, "the folders")
            n = self.count()
            if self.byte():
                raise self.fail("folders stored in an external stream are not read")
            parts = [self._folder() for _ in range(n)]
            self.expect(UNPACK_SIZES, "the unpack sizes")
            sizes = []
            for coders, _, main in parts:
                outs = [self.number() for _ in range(sum(c.streams_out for c in coders))]
                sizes.append(outs[main])
            crcs: list[int | None] = [None] * n
            kind = self.byte()
            if kind == CRC:
                crcs = self.digests(n)
                kind = self.byte()
            if kind != END:
                raise self.fail(f"property id {kind:#04x} in the coders info")
            folders = [_FolderInfo(coders, packed, size, crc)
                       for (coders, packed, _), size, crc in zip(parts, sizes, crcs)]
            kind = self.byte()
        if kind == SUBSTREAMS:
            counts = [1] * len(folders)
            kind = self.byte()
            if kind == NUM_STREAMS:
                counts = [self.count() for _ in folders]
                kind = self.byte()
            sizes_in: list[list[int]] = []
            for folder, n in zip(folders, counts):
                # every size but the last is stored; the last is what is left of the folder
                part = [self.number() for _ in range(n - 1)] if kind == SIZE and n else []
                if n:
                    part.append(folder.size - sum(part))
                    if part[-1] < 0 or (kind != SIZE and n > 1):
                        raise self.fail("the sizes of a folder's files do not add up to the folder's size")
                sizes_in.append(part)
            if kind == SIZE:
                kind = self.byte()
            known = [n == 1 and folder.crc is not None for folder, n in zip(folders, counts)]
            given: list[int | None] = []
            if kind == CRC:
                given = self.digests(sum(n for n, k in zip(counts, known) if not k))
                kind = self.byte()
            if kind != END:
                raise self.fail(f"property id {kind:#04x} in the substreams info")
            rest = iter(given)
            files = []
            for folder, part, k in zip(folders, sizes_in, known):
                files.append([(size, folder.crc if k else next(rest, None)) for size in part])
            kind = self.byte()
        if kind != END:
            raise self.fail(f"property id {kind:#04x} in the streams info")
        if files is None:
            files = [[(folder.size, folder.crc)] for folder in folders]
        return pack_pos, pack_sizes, folders, files


class _Folder:
    """One folder's unpacked stream, read by offset. The decoder is kept between reads, so members read in
    stream order decode the folder once; a read behind the decoder's position starts again at the last restart
    point before it (the folder's start, or a chunk of an LZMA2 stream that resets the dictionary)."""

    def __init__(self, path: Path, address: str, start: int, packed: int, info: _FolderInfo):
        self._path, self.address, self._start, self._packed, self.size = path, address, start, packed, info.size
        if len(info.coders) != 1 or info.coders[0].streams_in != 1 or info.coders[0].streams_out != 1:
            names = ", ".join(c.method.hex() or "?" for c in info.coders)
            raise ValueError(f"{address}: a 7z folder packed with a chain of coders ({names}); this snippet reads "
                             "folders of one coder: LZMA2, LZMA or Copy")
        method, _, _, props = info.coders[0]
        self._filter: dict | None = None
        if method == LZMA2 and len(props) == 1 and props[0] <= 40:
            size = 0xFFFFFFFF if props[0] == 40 else (2 | (props[0] & 1)) << (props[0] // 2 + 11)
            self._filter = {"id": lzma.FILTER_LZMA2, "dict_size": size}
        elif method == LZMA and len(props) == 5 and props[0] < 9 * 5 * 5:
            pb, rest = divmod(props[0], 45)
            lp, lc = divmod(rest, 9)
            self._filter = {"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
                            "dict_size": max(struct.unpack("<I", props[1:])[0], 4096)}
        elif method != COPY or props:
            raise ValueError(f"{address}: a 7z folder packed with coder {method.hex() or '?'} (properties "
                             f"{props.hex() or 'none'}); this snippet reads LZMA2 (21), LZMA (030101) and Copy (00)")
        if self._filter is not None and self._filter["dict_size"] > DICT_MAX:
            raise ValueError(f"{address}: a 7z folder with a {self._filter['dict_size']}-byte dictionary, above "
                             f"the {DICT_MAX} bytes this snippet lets the decoder allocate")
        self._lzma2 = method == LZMA2
        self._fh: BinaryIO | None = None
        self._restarts: list[tuple[int, int]] | None = None     # (packed offset, unpacked offset), ascending
        self._decoder: lzma.LZMADecompressor | None = None
        self._pos = 0                                           # of the decoder in the unpacked stream

    def _file(self) -> BinaryIO:
        if self._fh is None:
            self._fh = open(self._path, "rb")
        return self._fh

    def _scan(self) -> list[tuple[int, int]]:
        """The chunks of an LZMA2 stream at which a new decoder can start: those that reset the dictionary
        (an encoder writes one at the start of each of its independent blocks). Only the chunk headers are
        read: 3 to 6 bytes for at most 64 KiB packed."""
        out, packed, unpacked = [], 0, 0
        with open(self._path, "rb", buffering=0) as fh:         # unbuffered: a seek and 5 bytes per chunk
            while packed < self._packed:
                fh.seek(self._start + packed)
                head = fh.read(5)
                if not head or head[0] == 0:
                    break                                       # the end marker
                control = head[0]
                if len(head) < 5 or 2 < control < 0x80:
                    raise ValueError(f"{self.address}: LZMA2 chunk header {head.hex()} at byte {packed} of the "
                                     "pack stream is not one (corrupt archive?)")
                if control < 0x80:                              # stored: 1 resets the dictionary, 2 keeps it
                    if control == 1:
                        out.append((packed, unpacked))
                    n = ((head[1] << 8) | head[2]) + 1
                    packed, unpacked = packed + 3 + n, unpacked + n
                else:                                           # LZMA: bits 5-6 say what is reset; 3 is everything
                    reset = (control >> 5) & 3
                    if reset == 3:
                        out.append((packed, unpacked))
                    packed += 5 + (reset >= 2) + ((head[3] << 8) | head[4]) + 1
                    unpacked += (((control & 0x1F) << 16) | (head[1] << 8) | head[2]) + 1
        if not out or out[0] != (0, 0) or unpacked != self.size:
            raise ValueError(f"{self.address}: the LZMA2 chunks unpack to {unpacked} bytes, the 7z header says "
                             f"{self.size} (corrupt archive?)")
        return out

    def _restart(self, offset: int) -> tuple[int, int]:
        """The last point at or before unpacked `offset` where decoding can start."""
        if not self._lzma2:
            return 0, 0
        if self._restarts is None:
            self._restarts = self._scan()
        lo, hi = 0, len(self._restarts)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if self._restarts[mid][1] <= offset else (lo, mid)
        return self._restarts[lo]

    def _decode(self, n: int, keep: bool) -> bytes:
        """The next n bytes of the unpacked stream; discarded piece by piece unless `keep`."""
        fh, decoder, parts = self._file(), self._decoder, []
        while n:
            data = b""
            if decoder.needs_input and not decoder.eof:
                data = fh.read(min(BLOCK, self._start + self._packed - fh.tell()))
            try:
                out = decoder.decompress(data, n if keep else min(n, BLOCK))
            except (lzma.LZMAError, EOFError) as e:
                self._decoder = None
                raise ValueError(f"{self.address}: the folder's stream does not decode at unpacked byte "
                                 f"{self._pos}: {type(e).__name__}: {e}") from e
            if not out and not data:
                self._decoder = None
                raise ValueError(f"{self.address}: the folder's stream ends at unpacked byte {self._pos}, "
                                 f"{n} bytes short (truncated archive?)")
            n -= len(out)
            self._pos += len(out)
            if keep:
                parts.append(out)
        return b"".join(parts)

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ValueError(f"{self.address}: bytes {offset}..{offset + size} lie outside the folder's "
                             f"{self.size} unpacked bytes")
        if self._filter is None:                                # Copy: the pack stream is the data
            fh = self._file()
            fh.seek(self._start + offset)
            data = fh.read(size)
            if len(data) != size:
                raise ValueError(f"{self.address}: {size} bytes wanted, {len(data)} read (truncated archive?)")
            return data
        packed, unpacked = self._restart(offset)
        if self._decoder is None or offset < self._pos or self._pos < unpacked:
            self._file().seek(self._start + packed)
            self._decoder = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[self._filter])
            self._pos = unpacked
        self._decode(offset - self._pos, keep=False)
        return self._decode(size, keep=True)


class _File(NamedTuple):
    folder: int
    offset: int                 # in the folder's unpacked stream
    size: int
    crc: int | None


class _Archive:
    """One staged 7z archive: its files by name, and their bytes."""

    def __init__(self, path: Path, address: str):
        self._path, self.address = path, address
        self.files: dict[str, _File] = {}
        self.twice: set[str] = set()            # names the archive holds more than once
        self._open: dict[int, _Folder] = {}
        self._starts: list[tuple[int, int]] = []        # per folder: (file offset, size) of its pack stream
        self._folders: list[_FolderInfo] = []
        with open(path, "rb") as fh:
            sig = fh.read(START)
            if sig[:6] != SEVENZ:
                raise ValueError(f"{address}: not a 7z archive: header={sig[:8]!r}")
            if len(sig) < START:
                raise ValueError(f"{address}: a 7z archive of {len(sig)} bytes, without a start header")
            crc, offset, size, header_crc = struct.unpack("<IQQI", sig[8:])
            if zlib.crc32(sig[12:]) != crc:
                raise ValueError(f"{address}: the 7z start header fails its CRC")
            end = fh.seek(0, 2)
            if START + offset + size > end:
                raise ValueError(f"{address}: the 7z header lies at bytes {START + offset}..{START + offset + size} "
                                 f"of a file of {end} bytes (truncated archive?)")
            if size > HEADER_MAX:
                raise ValueError(f"{address}: a 7z header of {size} bytes, above the {HEADER_MAX} this snippet reads")
            fh.seek(START + offset)
            data = fh.read(size)
        if zlib.crc32(data) != header_crc:
            raise ValueError(f"{address}: the 7z header fails its CRC")
        self._end = end
        if not data:
            return                              # an archive without files
        for _ in range(NESTED_MAX):
            if data[0] != ENCODED_HEADER:
                break
            data = self._packed_header(_Header(data[1:], address))      # the header is itself packed: the usual case
        head = _Header(data, address)
        head.expect(HEADER, "the header")
        stored: list[tuple[int, int, int, int | None]] = []     # (folder, offset, size, crc) in file order
        kind = head.byte()
        while kind != END:
            if kind == ARCHIVE_PROPERTIES:
                while head.byte() != END:
                    head.skip_property()
            elif kind == ADDITIONAL_STREAMS:
                head.streams()
            elif kind == MAIN_STREAMS:
                pack_pos, pack_sizes, self._folders, per_folder = head.streams()
                self._starts = self._pack_streams(pack_pos, pack_sizes, self._folders)
                for k, part in enumerate(per_folder):
                    at = 0
                    for size, crc in part:
                        stored.append((k, at, size, crc))
                        at += size
            elif kind == FILES:
                self._names(head, stored)
            else:
                raise head.fail(f"property id {kind:#04x} in the header")
            kind = head.byte()

    def _pack_streams(self, pack_pos: int, pack_sizes: list[int],
                      folders: list[_FolderInfo]) -> list[tuple[int, int]]:
        """(file offset, size) of each folder's first pack stream; the pack streams lie one after another."""
        starts, at, k = [], START + pack_pos, 0
        for folder in folders:
            if k + folder.pack_streams > len(pack_sizes):
                raise ValueError(f"{self.address}: the 7z header lists {len(pack_sizes)} pack streams, fewer than "
                                 "its folders take")
            if at + pack_sizes[k] > self._end:
                raise ValueError(f"{self.address}: a pack stream ends at byte {at + pack_sizes[k]} of a file of "
                                 f"{self._end} bytes (truncated archive?)")
            starts.append((at, pack_sizes[k]))
            at += sum(pack_sizes[k:k + folder.pack_streams])
            k += folder.pack_streams
        return starts

    def _packed_header(self, head: _Header) -> bytes:
        """The header that an encoded header's one folder unpacks to."""
        pack_pos, pack_sizes, folders, _ = head.streams()
        if len(folders) != 1:
            raise ValueError(f"{self.address}: a packed 7z header in {len(folders)} folders")
        if folders[0].size > HEADER_MAX:
            raise ValueError(f"{self.address}: a 7z header of {folders[0].size} bytes, above the {HEADER_MAX} this "
                             "snippet reads")
        (start, packed), = self._pack_streams(pack_pos, pack_sizes, folders)
        data = _Folder(self._path, f"{self.address} (header)", start, packed, folders[0]).read(0, folders[0].size)
        if folders[0].crc is not None and zlib.crc32(data) != folders[0].crc:
            raise ValueError(f"{self.address}: the unpacked 7z header fails its CRC")
        if not data:
            raise ValueError(f"{self.address}: an empty packed 7z header")
        return data

    def _names(self, head: _Header, stored: list[tuple[int, int, int, int | None]]) -> None:
        """The FilesInfo: a file that is not an empty stream (a directory, an empty file) takes the next stored
        stream, in order."""
        n = head.count()
        empty = [False] * n
        names: list[str] | None = None
        while True:
            kind = head.byte()
            if kind == END:
                break
            body = head.take(head.number())
            if kind == EMPTY_STREAM:
                empty = _Header(body, self.address).bits(n)
            elif kind == NAMES:
                if body[:1] != b"\0":
                    raise ValueError(f"{self.address}: 7z file names stored in an external stream are not read")
                names = body[1:].decode("utf-16-le").split("\0")
                if len(names) != n + 1 or names.pop():
                    raise ValueError(f"{self.address}: the 7z header names {len(names)} files, not its {n}")
        if names is None:
            raise ValueError(f"{self.address}: the 7z header has no file names")
        if n - sum(empty) != len(stored):
            raise ValueError(f"{self.address}: the 7z header has {n - sum(empty)} files with data and "
                             f"{len(stored)} stored streams")
        streams = iter(stored)
        for name, is_empty in zip(names, empty):
            if is_empty:
                continue
            if name in self.files:
                self.twice.add(name)
            self.files[name] = _File(*next(streams))

    def read(self, name: str) -> bytes:
        f = self.files[name]
        if f.folder not in self._open:
            start, packed = self._starts[f.folder]
            self._open[f.folder] = _Folder(self._path, self.address, start, packed, self._folders[f.folder])
        data = self._open[f.folder].read(f.offset, f.size)
        if f.crc is not None and zlib.crc32(data) != f.crc:
            raise ValueError(f"{self.address}::{name}: the member's bytes fail the CRC the 7z header gives")
        return data


class _Member(NamedTuple):
    archive: _Archive
    name: str


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _indices(data: bytes, where: str) -> np.ndarray:
    """The stored palette indices of the palette PNG `data` as (Y, X) uint8.

    A PNG decoder replaces each index by its palette colour. The index of a palette PNG and the sample of a
    greyscale PNG of the same bit depth are stored the same way (one sample per pixel, the same filters, the
    same interlacing), so the file is re-wrapped as a greyscale PNG: the IHDR colour type is set from 3 to 0, the
    IDAT chunks are kept byte for byte and every other chunk (PLTE, tRNS, ...) is dropped. Decoding that gives
    the indices, stretched to 0..255 when the depth is below 8, which is undone exactly."""
    out, pos, header, pixels = [PNG], 8, None, False
    while True:
        if pos + 12 > len(data):
            raise ValueError(f"{where}: PNG ends at byte {len(data)} without an IEND chunk (truncated?)")
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + n
        if end > len(data):
            raise ValueError(f"{where}: PNG chunk {kind!r} at byte {pos} runs past the end of the file (truncated?)")
        if header is None:
            if kind != b"IHDR" or n != 13:
                raise ValueError(f"{where}: PNG does not start with a 13-byte IHDR chunk")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data[pos + 8:pos + 21])
            if colour != PALETTE or depth not in (1, 2, 4, 8):
                raise ValueError(f"{where}: PNG colour type {colour}, bit depth {depth}: not a palette PNG")
            header = (width, height, depth)
            out.append(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, GREY, compression,
                                                   filtering, interlace)))
        elif kind == b"IDAT":
            out.append(data[pos:end])       # byte for byte, with its own CRC: the decoder checks it
            pixels = True
        elif kind == b"IEND":
            break
        pos = end
    if not pixels:
        raise ValueError(f"{where}: PNG has no IDAT chunk")
    out.append(_chunk(b"IEND", b""))
    width, height, depth = header
    a = np.asarray(imagecodecs.png_decode(b"".join(out)))
    if a.shape != (height, width) or a.dtype != np.uint8:
        raise ValueError(f"{where}: decoded to {a.dtype} {a.shape}, not uint8 {(height, width)} as its header says")
    if depth < 8:
        # the decoder stretches a 1/2/4-bit grey sample to 0..255 (4-bit 0,1,2 -> 0,17,34): undo it exactly
        step = 255 // ((1 << depth) - 1)
        if np.any(a % step):
            raise ValueError(f"{where}: decoded {depth}-bit samples are not multiples of {step}, so the stored "
                             "indices cannot be recovered")
        a = a // step
    return a


def _decode(data: bytes, where: str, palette: str | None) -> np.ndarray:
    """The member's one image as (Y, X) or (Y, X, S), by its magic bytes (its name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26:
            raise ValueError(f"{where}: a PNG of {len(data)} bytes, without a header")
        if data[25] == PALETTE:
            if palette != "index":
                raise ValueError(f"{where}: a palette PNG; the decoder would replace the stored indices by "
                                 "palette colours. Set params.palette: index to read the indices (an indexed mask)")
            return _indices(data, where)
        if data[24] < 8:
            raise ValueError(f"{where}: a {data[24]}-bit grey PNG; the decoder would rescale its samples to "
                             "0..255, so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:3] == JPEG:
        a = imagecodecs.jpeg_decode(data)
    else:
        raise ValueError(f"{where}: not a PNG or JPEG file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{where}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Stack:
    def __init__(self, members: list[_Member], palette: str | None):
        self._members, self._palette = members, palette
        self._last: tuple[int, np.ndarray] | None = None    # the last member decoded: c planes are read one by one
        first = self._load(0)                               # (Y, X) or (C, Y, X); every member must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(members) > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape = ((len(members),) if len(members) > 1 else ()) + first.shape
        self.access = "plane"           # one member per z plane: any region of it costs the member, unpacked
        self.voxel_size_nm = None       # JPEG/PNG density fields describe print size
        self._last = (0, first)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._members))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._members))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """Member k as (Y, X) or (C, Y, X), checked against member 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            m = self._members[k]
            raise ValueError(f"{m.archive.address}::{m.name}: {a.dtype} {a.shape} differs from the first member's "
                             f"{self.dtype} {self._plane_shape}; all members of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        m = self._members[k]
        a = _decode(m.archive.read(m.name), f"{m.archive.address}::{m.name}", self._palette)
        return a if a.ndim == 2 else np.ascontiguousarray(np.moveaxis(a, 2, 0))


def _matches(archive: _Archive, pattern: str) -> list[str]:
    """The archive's files that match `pattern`, sorted by name."""
    names = sorted(n for n in archive.files if fnmatchcase(n, pattern))
    if not names:
        raise ValueError(f"{archive.address}: none of the {len(archive.files)} files of the 7z matches {pattern!r}")
    twice = sorted(archive.twice.intersection(names))
    if twice:
        raise ValueError(f"{archive.address}: the 7z holds {twice[0]!r} more than once")
    return names


def _by_stem(archive: _Archive, pattern: str) -> dict[str, str]:
    """The matches of `pattern` by file stem (the name without its directories and its last extension)."""
    out: dict[str, str] = {}
    for name in _matches(archive, pattern):
        stem = PurePosixPath(name).stem
        if stem in out:
            raise ValueError(f"{archive.address}: {out[stem]!r} and {name!r}, both matching {pattern!r}, have "
                             f"the same stem {stem!r}, so they cannot be paired by stem")
        out[stem] = name
    return out


def _members(io: IO, url: str, pattern: str, pair: str | None) -> list[_Member]:
    """The files of the url's archive that match `pattern`: sorted by name, or, with `pair`, in the order of
    the sorted matches of `pair`, each with the file of the same stem."""
    archive = _Archive(io.fetch(url), url)
    names = _matches(archive, pattern)
    if pair is not None:
        mine, theirs = _by_stem(archive, pattern), _by_stem(archive, pair)
        if mine.keys() != theirs.keys():
            lone = sorted(mine.keys() - theirs.keys())[:3]
            missing = sorted(theirs.keys() - mine.keys())[:3]
            raise ValueError(f"{url}: the {len(mine)} files matching {pattern!r} and the {len(theirs)} matching "
                             f"{pair!r} do not pair one to one by stem (without a partner: {lone}; partners "
                             f"without a file: {missing})")
        names = [mine[stem] for stem in sorted(theirs, key=theirs.__getitem__)]
    return [_Member(archive, name) for name in names]


def open_array(src: Source, io: IO) -> Array:
    pattern = src.params.get("members")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"params.members must be a glob over the 7z member names, got {pattern!r}")
    pair = src.params.get("pair_members")
    if pair is not None and (not isinstance(pair, str) or not pair):
        raise ValueError(f"params.pair_members must be a glob over the 7z member names, got {pair!r}")
    palette = src.params.get("palette")
    if palette is not None and palette not in PALETTE_MODES:
        raise ValueError(f"params.palette must be one of {PALETTE_MODES}, got {palette!r}")
    if len(set(src.urls)) != len(src.urls):
        raise ValueError("the urls name the same archive twice")
    return _Stack([m for url in src.urls for m in _members(io, url, pattern, pair)], palette)
