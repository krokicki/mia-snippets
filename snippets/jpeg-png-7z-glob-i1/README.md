# jpeg-png-7z-glob-i1

Reads a set of one-image files, JPEG or PNG, that are members of 7z archives (`.7z`), and presents it as one
array: one member per z plane, or no z axis for a single member. It is `image-targz-palette-glob` for 7z
archives. No other snippet opens a 7z, and the harness's `list` and `describe` do not look inside one.

- **URLs.** Each URL is the plain URL of a 7z archive, with no `::`. A file that does not start with the 7z
  signature raises.
- **`members`** (required): a glob over the member names (`fnmatch` syntax: `*`, `?`, `[...]`, case-sensitive;
  `*` also crosses `/`), applied to every URL. It is a param, not `<url>::<glob>`, because `io` and the harness
  read `::` as a zip member. Member names are as the archive stores them, with `/` between directories
  (`dataset/JPEGImages/21c-02.jpg`). The files that match are sorted by member name within one URL (by code
  point, so `21C-...` comes before `21c-...`; this is not the archive's own order), and several URLs are
  concatenated in binding order. Directories and empty files never match. A name the archive holds twice raises.
- **`pair_members`** (optional): the glob that the paired array uses as its `members` in the same archive (a
  label names its raw's glob). The matches of `members` are then ordered as the sorted matches of
  `pair_members`, each by its file stem (the name without directories and without its last extension:
  `SegmentationClass/Y30-1.1.png` pairs with `JPEGImages/Y30-1.1.jpg`), and `open_array` raises unless the two
  sets pair one to one by stem. Without it the binding's `reasons` must say why the two globs sort alike. It
  pairs within one archive: with several URLs, each URL's archive must hold both sets.
- **Files.** By each member's first bytes, not its name: JPEG, or a PNG that is 8 or 16 bit grey or colour.
  Every member of one array must give the same shape and dtype as the first; one that differs raises at `read`.
- **`palette: index`** (optional): a palette (indexed-colour) PNG member, any bit depth, is returned as its
  stored palette indices, `uint8` with no `c` axis: the ids of a mask saved as an indexed PNG. A decoder would
  return the palette's colours instead, so the file is re-wrapped in memory as a greyscale PNG with the same
  `IDAT` bytes (the method of `png-palette-index-glob`) and the decoder's stretch of 1/2/4-bit samples is undone
  exactly. Without the param a palette PNG raises, so an indexed colour picture is never read as ids by
  accident. Other members are not affected by it.
- **Axes.** A member without samples is `zyx`. A member with samples is `zcyx`, `c` in the file's sample order
  (R, G, B). With one member the `z` axis is dropped (`yx`, `cyx`).
- **Values** are the decoder's (`imagecodecs`), unchanged; palette indices as stored. Nothing is remapped.
- **Access.** `plane`. **dtype.** The file's. **Voxel size:** `None`; JPEG/PNG density fields describe print
  size.

## How the 7z is read

`py7zr` is not on the allowlist, so the container is parsed here with the standard library (`struct`, `zlib`,
`lzma`), following 7-Zip's `7zFormat.txt`:

- The 32-byte start header points at the end header; both are checked against their CRC32. The end header is
  usually itself packed (an "encoded header", LZMA); it is unpacked first.
- The header lists the **pack streams**, the **folders** (each a coder chain that unpacks to one stream), the
  size and CRC32 of every file inside each folder, and the file names. A file that is not an "empty stream"
  takes the next stored stream, in order. A **solid** archive has many files in one folder: one compressed
  stream, without an index into it.
- A folder is decoded when one of its files is read. Folders of **one coder** are read: **LZMA2** (`21`),
  **LZMA** (`030101`) and **Copy** (`00`), with `lzma.LZMADecompressor(FORMAT_RAW)`. Anything else (a BCJ,
  Delta, BCJ2 or AES stage, PPMd, bzip2, deflate) raises, naming the coder, when a matched member lies in such
  a folder; other folders of the archive do not matter.
- **Restart points.** An LZMA2 stream is a row of chunks, each at most 64 KiB packed with its sizes in a 3 to
  6 byte header, and a chunk can reset the dictionary: decoding can start there with a fresh decoder. 7-Zip
  writes such a chunk at the start of every block it compresses independently (a multi-threaded encode: blocks
  of 4x the dictionary by default). On a folder's first read its chunk headers are walked (a seek and 5 bytes
  each) to list these points; the unpacked sizes the headers give must add up to the folder's size.
- `read` decodes from the decoder's position when the member lies ahead of it and no restart point is closer,
  otherwise from the last restart point before the member, discarding what precedes it. Members read in
  archive order cost one pass. An LZMA folder has no restart points: it is decoded from its start.
- Every member's bytes are checked against the CRC32 the header gives for it before they are decoded.

The archive is staged whole with `io.fetch` (counted against the job's cap once, then in the dataset cache):
the header is at the end, the chunk walk touches the whole pack stream, and a member near the end of a solid
folder needs everything from the restart point before it.

## Known limits

- Bytes: every archive is fetched whole, even for one member (210 MB in the example).
- Time: a read out of archive order decodes from the last restart point: at most one block (64 MiB unpacked in
  the example, about 1.7 s; LZMA2 decodes at about 40 MB/s), and from the folder's start in a solid archive
  written as one block or with LZMA. Sorted member order is not archive order, so a walk through z is not
  always one pass. In the example, image decode included, 40 raw planes in random order took 0.05 to 1.9 s each
  (median 0.6 s), 40 label planes 0.02 to 1.7 s (median 0.95 s), and all 686 raw planes in z order 29 s.
- Coders: one coder per folder, LZMA2, LZMA or Copy. No filter chains, no encrypted archives or headers, no
  multi-volume archives (`.7z.001`), no folders or names stored in an external stream. A dictionary above
  1 GiB and a header above 64 MiB are refused.
- Each member is read whole into memory (3 x 2748 x 3584 `uint8` in the example); no pyramid levels.
- A 1/2/4-bit grey PNG raises (the decoder rescales its samples to 0..255). TIFF members are not read.
- Samples are exposed as stored: an RGBA file has 4 `c` planes. A crop holds one `c` plane.
- `pair_members` pairs by stem within one archive; a mask named `<stem>_mask.png`, or a partner in another
  archive, is not paired by it.
- One open file handle per folder read is held for the array's lifetime.

## Example

`examples/zenodo-21501303-rice-stem-cross-sections.yaml`: brightfield images of hand-cut rice stem
cross-sections stained with phloroglucinol-HCl, with class masks (Zenodo 21501303). `dataset.7z` (210,466,689
bytes) is one solid LZMA2 folder (16 MiB dictionary; 264,779,427 bytes unpacked in 4,281 chunks, of which 4
reset the dictionary, at 0, 64, 128 and 192 MiB) holding 1,372 files, the 686 JPEGs first and the 686 PNGs
after them. Its header is LZMA-packed (16,551 bytes).

- raw: `dataset/JPEGImages/*.jpg`, 686 baseline RGB JPEGs, all 3584x2748: `zcyx` (686, 3, 2748, 3584) `uint8`.
- label: `dataset/SegmentationClass/*.png`, 686 8-bit palette PNGs, all 3584x2748, read with `palette: index`
  and `pair_members: dataset/JPEGImages/*.jpg`: `zyx` (686, 2748, 3584) `uint8`.

The palette is the VOC colour map in every mask and the indices are the class ids. Over the 686 masks they are
0 background 65.40%, 1 small vascular bundle 0.33%, 2 cavity 16.84%, 3 large vascular bundle 0.98%, 4 stem
16.45% of the pixels; which id is which class was read off two masks viewed beside their images
(`21C-355-2`, `zh11-3`), the record names the classes without ids. Every member's CRC32 matched, and 17 raw and
17 label planes read in random order through the snippet equalled the same members cut from one whole-stream
decode of the folder.

The images carry a burnt-in red scale bar that differs between images (500 µm on `21C-355-2`, 200 µm on
`zh11-3`; 257 to 610 px long on eight images sampled), so the stack has no single pixel size, and the bar is
part of the raw pixels.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
