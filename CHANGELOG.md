# Changelog

## 1.2 — 2026-07-02

### Added — optional tools (all OFF by default; the stock toolbar stays Copy / Save / Cancel)
- **Polygon selection** (click corners; double-click or Enter closes) and
  **freehand lasso selection** — the copied/saved PNG is transparent outside
  the shape. Committed regions **move and resize exactly like the rectangle**
  (bbox handles, inside-drag, arrow keys).
- **Pen tool** with configurable colour/width; strokes bake into the shot at
  full physical resolution. Enabling Pen automatically adds an **Eraser**
  (click or drag across a stroke to remove it — no extra setting).
- **Text tool** with configurable colour/size, typed in a floating entry.
  Placed labels stay live: click to **select**, drag to **move**, **Delete**
  to remove.
- **Ctrl+Z** undoes any annotation action, including restoring erased strokes
  and deleted labels.
- **Always save a copy** setting: plain Copy (Enter) also writes the
  timestamped PNG.
- **Ctrl+C** copies (alongside Enter); **arrow keys** nudge the selection by
  1 px, **Shift+arrows** resize by 1 px.
- Every tool button has a **drawn icon** (cairo, theme-independent, follows
  hover/active colours).

### Changed
- **WCAG default colours**: selection border `#0077CC`, pen `#FFD60A`, text
  `#2D0A4E` — pairwise contrast ≥ 3:1 (WCAG 1.4.11), enforced by a test.
  All remain customizable.
- **Settings dialog redesigned**: grouped Selection / Output / Tools sections;
  pen and text each configure on one row; save-folder and filename-format are
  click-to-edit popovers (click out to dismiss); config is written **once on
  close** instead of on every keystroke.
- **Esc peels one layer at a time**: text entry → tool mode → cancel.
- Closing is left to GNOME's standard window animation. Suppression tricks
  (opacity 0, unmap-first, committing a transparent frame before quit) are
  deliberately not used: each either gets optimized away by GTK or collides
  with Mutter's black backdrop behind fullscreen windows and produces black
  flashes. The reasoning is documented in the code.

### Fixed
- Multi-monitor: the overlay now opens on the monitor that was actually
  captured (matched by connector; GTK4 has no `Gdk.Monitor.is_primary`, so the
  old code silently used monitor 0).
- Copy/Save during an in-progress polygon now commits the polygon instead of
  silently capturing the previous rectangle.
- Text labels placed near screen edges bake exactly where the preview showed
  them (position clamped once, shared by preview and crop).
- Self-intersecting lasso regions crop with the same even-odd rule the
  overlay previews, so output matches what was on screen.
- Closing a polygon by double-clicking its last corner no longer records a
  duplicate vertex; tiny regions no longer distort on their first nudge.
- Saving can no longer clobber a file from the same second (atomic exclusive
  create), a stalled `wl-copy` can no longer hang the app (bounded write),
  and the portal request token can no longer collide across captures.
- Hand-edited configs are clamped to sane ranges (border width, dim, sizes)
  on load instead of producing invisible borders or an all-black overlay.
- `install.sh` writes an absolute, properly quoted `Exec=` path, so the app
  launches from GNOME's app grid even when `~/.local/bin` is not on the
  launcher's PATH — including home directories containing spaces.
- Missing `wl-copy` / non-Wayland sessions fail fast at startup instead of
  after a selection was framed.

### Performance
- Frame conversion from the ScreenCast capture is a single copy (was three
  full-frame copies plus a Python per-row loop): capture is ~10 ms faster at
  1080p and ~55 ms faster at 4K.
- The frozen preview paints directly from the physical-resolution capture:
  one less full-screen buffer in memory and a visibly sharper preview under
  HiDPI/fractional scaling.
- Per-frame colour re-parsing and per-motion cursor allocation eliminated.
- Plain rectangle shots export as RGB PNGs (no fake alpha channel); freeform
  shots keep alpha for the transparent outside.

## 1.1 and earlier

See git history (`snapclip 1.0` shipped flash-free region capture, the frozen
fullscreen overlay, clipboard-first workflow, fractional-scaling-correct
crops, and the single-instance guard).
