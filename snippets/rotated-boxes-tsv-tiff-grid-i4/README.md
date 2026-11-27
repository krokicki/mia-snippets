# rotated-boxes-tsv-tiff-grid-i4

Reads the **per-image cell ROI tables `<stem>_roisCells.txt`** that the TrypTag ImageJ macros (after Wheeler et
al. 2012) write beside each image `<stem>.tif`, and paints each table on the pixel grid of its image, one table
per z plane. Written for S-BIAD1932 (`<plate>_<date>/<gene>_<n>_<N|C>_<field>.tif` with
`<gene>_<n>_<N|C>_<field>_roisCells.txt`).

A table is tab-separated text (CRLF or LF) with a header row and one row per cell:

```
cell	xWand	yWand	xCentre	yCentre	width	height	angle
1	40	1560	85.0947	1609.3482	322.56	161.28	129.2225
2	60	1180	115.9314	1247.1511	322.56	161.28	147.744
```

Each row is a **rotated box** in pixels of the image, the window the macros cut each cell out with: `width` x
`height` centred on (`xCentre`, `yCentre`), its `width` side turned `angle` degrees. It is a label of where
each cell is and how it lies, not an outline of the cell.

## What the columns mean (measured, not documented in the files)

On the record's sample field (`R1566_20180201/Tb427.01.2670_4_C_1`, 61 cells) against plane 0 of its
`_thr.tif`, the thresholded image the macros found the cells in:

- `xCentre`, `yCentre` are the centroid of the connected component of that plane which holds the pixel
  (`xWand`, `yWand`), in ImageJ coordinates: pixel (column j, row i) covers [j, j + 1) x [i, i + 1), so its
  centre is (j + 0.5, i + 0.5). All 61 agree to the 4 decimals written.
- `angle` is ImageJ's ellipse angle of that component: degrees counter-clockwise from +x as the image is
  displayed (y down), 0..180. All 61 agree with the component's major axis to 0.1 degree; the clockwise reading
  does not fit.
- `width` lies along that angle and `height` across it: every component lies wholly inside its box read this
  way (the longest reaches 114 px from its centre along the angle, under width / 2 = 161.28), and 25 of 61
  would stick out of it read the other way. In S-BIAD1932 every box is 322.56 x 161.28 px (5.12 x 2.56 px per
  unit of the 63x magnification).
- `xWand`, `yWand` (where the wand tool started) are not used.

## Array

- **Values.** uint16: a pixel whose centre is inside a box (within width / 2 along the angle and height / 2
  across it, lower edge included, upper not) holds that row's `cell` number; 0 is background. The boxes are
  windows of one size, wider than the cells are apart, so they overlap (38 % of the sample plane is in some box):
  a pixel inside several boxes holds the one whose centre is nearest, the earlier row when two are equally near.
  On the sample field that leaves 96 % of each cell's thresholded pixels (at least 69 %) under its own number;
  painting the rows one over another in file order leaves 62 %, and none of some cells. A box that reaches
  over the image's edge is cut there.
- **Planes.** The images `params.images` names, in that order (each glob's matches sorted by name, the urls in
  binding order), each with the table `<stem>_roisCells.txt` beside it. Give `images` the urls of the image
  array exactly as its binding lists them, and plane k of the label is the table of plane k of that array. The
  table names alone would not give that order: `a_1_roisCells.txt` sorts after `a_10_roisCells.txt`, `a_1.tif`
  before `a_10.tif`. An image without a table among the urls, or a table without an image, raises at open.
- **Axes** `zyx`, z being the image (independent fields, not depth); `yx` when there is one image, as
  `tiff-channels-glob` gives a single file without z.
- **Grid.** A table states no size: each plane is (Y, X) of the first page of its image's TIFF header, read by
  range (one 1 MiB block of the image, the same block the image's own array reads first), no pixels decoded.
  `open_array` reads the first image's header and the first table. An image whose size differs from the first
  raises when its plane is read, naming it.
- **Access** is `plane`: a region costs its table and its image's header.
- **Voxel size** is `None`: a table states none.

## Urls and params

Urls are the tables: plain URLs, zip members (`<zip url>::<member>`), or globs (`<dir url>/::<glob>` over an
HTTP directory index, `<zip url>::<glob>` over a zip's members; `fnmatch` syntax), e.g.
`https://.../R1566_20180201/::*_roisCells.txt`.

- `images` (required): the urls of the images, globs included, as the image array's binding lists them, e.g.
  `https://.../R1566_20180201/::*_[NC]_*[0-9].tif`.

## What is refused

- A table whose first row does not name `cell`, `xCentre`, `yCentre`, `width`, `height` and `angle` once each
  (other columns are allowed and ignored), a row with another number of fields, a `cell` that is not a whole
  number in 1..65535 or is repeated in its file, a size that is not positive, a number that is not finite, and
  a centre outside the image (a centroid of a component of the image cannot be; it would mean other units or
  the wrong image).
- A table with a header and no rows is not refused: it is an all-background plane.

## Limits

- The table must be named `<stem>_roisCells.txt` and lie in the folder (or zip folder) of `<stem>.tif` /
  `.tiff`; the columns are taken by these names only.
- The boxes are an automatic product of the macros, and where they overlap the choice of the nearest centre is
  this reader's, not the files'. Cells the macros did not take (touching the image edge, clumps) have no row
  and are background.
- The `_thr.tif` files beside the tables are not read. On the sample field their plane 0 is the cell masks the
  rows were measured from (one component per row) and plane 1 a second threshold that lies on the brightest
  Hoechst signal (the third channel), but the files do not say so.
