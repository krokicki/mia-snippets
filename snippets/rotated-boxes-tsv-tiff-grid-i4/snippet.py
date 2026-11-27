"""The per-image cell ROI tables `<stem>_roisCells.txt` of the TrypTag ImageJ macros (after Wheeler et al. 2012),
each rasterised on the pixel grid of its image `<stem>.tif`, one table per z plane, in the order of the images.

A table is tab-separated text with a header row and one row per cell:

    cell  xWand  yWand  xCentre   yCentre    width   height  angle
    1     40     1560   85.0947   1609.3482  322.56  161.28  129.2225

Each row is a rotated box in pixels of the image: `width` x `height` centred on (`xCentre`, `yCentre`), the
`width` side turned `angle` degrees counter-clockwise from +x as the image is displayed (y down), which is
ImageJ's ellipse angle. Coordinates are ImageJ's: pixel (column j, row i) covers [j, j + 1) x [i, i + 1). A
pixel whose centre is inside a box holds that row's `cell` number; inside several boxes, the one whose centre
is nearest. 0 is background. `xWand`, `yWand` (the point the macro's wand tool started from) are not used.

A table states no size, so its grid is the first page of its image's TIFF header, read by range without
decoding pixels. `params.images` names the images, as the image array's binding lists them: the planes are
those images in that order, which a sort of the table names would not give (`a_1_roisCells.txt` sorts after
`a_10_roisCells.txt`, `a_1.tif` before `a_10.tif`).
"""
import math

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SUFFIX = "_roisCells.txt"       # the table of image <stem>.tif is <stem>_roisCells.txt, beside it
IMAGE_EXTS = ("tif", "tiff")
COLUMNS = ("cell", "xCentre", "yCentre", "width", "height", "angle")
MAX_ID = 65535                  # uint16

Box = tuple[int, float, float, float, float, float]     # cell, xCentre, yCentre, width, height, angle (degrees)


def _grid(io: IO, url: str) -> tuple[int, int]:
    """(Y, X) of a TIFF's first page, from its header."""
    try:
        try:
            tif = tifffile.TiffFile(io.open(url))
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            tif = tifffile.TiffFile(io.fetch(url))
    except tifffile.TiffFileError as e:
        raise ValueError(f"{url}: not a TIFF file ({e})") from None
    with tif:
        page = tif.pages[0]
        return int(page.imagelength), int(page.imagewidth)


def _bytes(io: IO, url: str) -> bytes:
    try:
        return io.open(url).read()
    except NeedsFetch:
        return io.fetch(url).read_bytes()


def _rows(data: bytes, shape: tuple[int, int], url: str) -> list[Box]:
    """The rows of a roisCells table as boxes, in file order; [] for a table with a header and no rows."""
    ny, nx = shape
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a text file: header={data[:4]!r}") from None
    lines = [(n, [f.strip() for f in line.split("\t")]) for n, line in enumerate(text.splitlines(), 1)
             if line.strip()]
    header = lines[0][1] if lines else []
    if any(header.count(c) != 1 for c in COLUMNS):
        raise ValueError(f"{url}: not a roisCells table: its first row must name the tab-separated columns "
                         f"{', '.join(COLUMNS)} once each, got {header[:12]}")
    at = [header.index(c) for c in COLUMNS]
    boxes: list[Box] = []
    seen: set[int] = set()
    for n, fields in lines[1:]:
        try:
            if len(fields) != len(header) or not (fields[at[0]].isascii() and fields[at[0]].isdigit()):
                raise ValueError
            cell = int(fields[at[0]])
            xc, yc, w, h, angle = (float(fields[k]) for k in at[1:])
        except ValueError:
            raise ValueError(f"{url} line {n}: not a row of {len(header)} tab-separated fields with a whole "
                             f"cell number and numbers for {', '.join(COLUMNS[1:])}: {fields[:12]}") from None
        if not 1 <= cell <= MAX_ID or cell in seen:
            raise ValueError(f"{url} line {n}: cell number {cell} is repeated or not in 1..{MAX_ID}")
        if not (all(math.isfinite(v) for v in (xc, yc, w, h, angle)) and w > 0 and h > 0):
            raise ValueError(f"{url} line {n}: box centre {xc}, {yc} size {w} x {h} angle {angle} is empty or "
                             "not finite")
        if not (0 <= xc <= nx and 0 <= yc <= ny):
            raise ValueError(f"{url} line {n}: box centre {xc}, {yc} is outside its image, {nx} x {ny} pixels "
                             "(x, y)")
        seen.add(cell)
        boxes.append((cell, xc, yc, w, h, angle))
    return boxes


def _paint(shape: tuple[int, int], boxes: list[Box]) -> np.ndarray:
    """Each box filled with its cell number, where it lies on the grid. A pixel belongs to a box when its centre
    (j + 0.5, i + 0.5) is within half the width along the box's angle and half the height across it; a pixel of
    several boxes goes to the one whose centre is nearest (the earlier row when equally near)."""
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    nearest = np.full(shape, np.inf)        # squared distance to the centre of the box each pixel holds
    for cell, xc, yc, w, h, angle in boxes:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        ex, ey = (abs(c) * w + abs(s) * h) / 2, (abs(s) * w + abs(c) * h) / 2      # half the bounding box
        x0, x1 = max(math.floor(xc - ex), 0), min(math.ceil(xc + ex), nx)
        y0, y1 = max(math.floor(yc - ey), 0), min(math.ceil(yc + ey), ny)
        if x0 >= x1 or y0 >= y1:
            continue
        dx = np.arange(x0, x1) + 0.5 - xc
        dy = (np.arange(y0, y1) + 0.5 - yc)[:, None]
        along, across = dx * c - dy * s, dx * s + dy * c       # y is down, the angle counter-clockwise as displayed
        d2 = dx * dx + dy * dy
        window = (slice(y0, y1), slice(x0, x1))
        take = ((-w / 2 <= along) & (along < w / 2) & (-h / 2 <= across) & (across < h / 2)
                & (d2 < nearest[window]))
        out[window][take] = cell
        nearest[window][take] = d2[take]
    return out


class _Planes:
    """One table per plane, painted on its image's grid: zyx, or yx when there is one image."""

    def __init__(self, io: IO, pairs: list[tuple[str, str]]):
        self._io, self._pairs = io, pairs       # (image url, table url) per plane
        self._last: tuple[int, np.ndarray] | None = None        # the last painted plane
        self._plane_shape = _grid(io, pairs[0][0])
        self._plane(0)                          # a bad first table fails at open
        many = len(pairs) > 1
        self.axes = "zyx" if many else "yx"
        self.shape: tuple[int, ...] = ((len(pairs),) if many else ()) + self._plane_shape
        self.dtype = np.dtype("uint16")
        self.voxel_size_nm = None       # a table states no pixel size
        self.access = "plane"           # a y/x region costs its table and its image's header

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = (region.get("y", slice(None)), region.get("x", slice(None)))
        if "z" not in self.axes:
            return self._plane(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._pairs))[z])[inner]       # IndexError when out of range, like numpy
        planes = [self._plane(k)[inner] for k in range(len(self._pairs))[z]]
        return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self._plane_shape, bool)[inner].shape,
                                                        self.dtype)

    def _plane(self, k: int) -> np.ndarray:
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        image, table = self._pairs[k]
        shape = self._plane_shape if k == 0 else _grid(self._io, image)
        if shape != self._plane_shape:
            raise ValueError(f"{image}: {shape} (y, x) differs from the first image's {self._plane_shape} "
                             f"({self._pairs[0][0]}); all images of one array must agree, so bind the tables "
                             "of another image shape as their own array")
        a = _paint(shape, _rows(_bytes(self._io, table), shape, table))
        self._last = (k, a)
        return a


def _expand(urls: list[str], io: IO, what: str) -> list[str]:
    """Each glob url as its matches sorted by name, the urls in the order given."""
    files = [u for url in urls for u in io.expand(url)]
    if not files:
        raise ValueError(f"{what} name no file")
    if len(set(files)) != len(files):
        raise ValueError(f"{what} (after glob expansion) name the same file twice")
    return files


def _pairs(images: list[str], tables: list[str]) -> list[tuple[str, str]]:
    """(image, table) per plane, in the images' order: the table of `<stem>.tif` is `<stem>_roisCells.txt`
    beside it. Images and tables must pair one to one, so no plane is passed over and z cannot shift."""
    by_stem: dict[str, str] = {}
    for t in tables:
        if not t.endswith(SUFFIX) or t[:-len(SUFFIX)].endswith(("/", "::")) or len(t) == len(SUFFIX):
            raise ValueError(f"{t}: a table is named <image stem>{SUFFIX}")
        by_stem[t[:-len(SUFFIX)]] = t
    pairs: list[tuple[str, str]] = []
    for image in images:
        stem, dot, ext = image.rpartition(".")
        if not dot or ext.lower() not in IMAGE_EXTS:
            raise ValueError(f"params.images: {image} is not named .tif / .tiff")
        if stem not in by_stem:
            raise ValueError(f"params.images: {image} has no table {stem}{SUFFIX} among the urls")
        pairs.append((image, by_stem.pop(stem)))
    if by_stem:
        left = sorted(by_stem.values())
        raise ValueError(f"{len(left)} table(s) have no image <stem>.tif / .tiff in params.images, or share one "
                         f"with another table: {left[:5]}{' ...' if len(left) > 5 else ''}")
    return pairs


def open_array(src: Source, io: IO) -> Array:
    images = src.params.get("images")
    if not (isinstance(images, list) and images and all(isinstance(u, str) and u for u in images)):
        raise ValueError("params.images must be the list of the image urls (globs included) the tables were "
                         f"written for, got {str(images)[:200]}")
    return _Planes(io, _pairs(_expand(images, io, "params.images"), _expand(list(src.urls), io, "the urls")))
