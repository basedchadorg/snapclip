# Changelog

## Unreleased

### Added
- **Multi-monitor targeting** (from PR #1 by @basedchadorg, reworked):
  `-l` / `--list-monitors` prints the connected displays; `-m` / `--monitor`
  picks one by connector name, index (left-to-right by position) or a
  substring of its display name, `primary` being the default. An unknown
  target falls back to the primary monitor with one stderr notice. With two
  or more monitors the settings dialog gains a **Default monitor** row
  (config key `default_monitor`); single-monitor setups see no new UI.
  Monitor discovery is a single DisplayConfig call on the capture worker
  (no GDK off the main thread, no second round trip on the main thread) and
  shares the 1.4 RecordArea rectangle derivation, so the PR's own
  RecordArea change folded into it.

## 1.4 — 2026-09-21

### Fixed
- **Fullscreen applications.** Over a fullscreen game or video (Minecraft
  was the report) snapclip either sat for its 10 s capture timeout and
  errored, or — if the game repainted in the meantime — showed up late with
  that later frame. Mutter puts fullscreen windows on direct scanout, so
  the compositor stops painting the monitor; the `RecordMonitor` ScreenCast
  stream only records on a compositor paint and therefore never delivered
  its first frame. The primary monitor is now captured as a `RecordArea`
  stream of its logical rectangle, which paints the scene into the PipeWire
  buffer on demand: ~20 ms to the first frame over a fullscreen Minecraft.
  The buffer is the same size as before (logical size × the monitor's scale),
  so fractional scaling, rotation and multi-monitor layouts are unchanged;
  `RecordMonitor` stays as the fallback if the area call is refused. Verified
  live over Minecraft: the overlay maps on top, focused, with the current
  game frame frozen behind it, both from a shell and through the same
  launch path gnome-settings-daemon uses for custom shortcuts.
- The primary monitor's logical rectangle is derived from Mutter's
  `DisplayConfig` exactly as Mutter does (current mode, swapped for 90°/270°
  rotations, divided by the scale in the logical layout mode), and the test
  suite checks it against GDK's geometry live.

## 1.3 — 2026-09-11

### Added
- **Quick-save on double-tap** (OFF by default): tap the snapclip hotkey
  twice within `double_tap_ms` (300 ms, config-only, 120–800) to save and copy
  the whole screen instantly with no overlay. While on, a single tap waits out
  the tap window before the overlay appears; a cooldown collapses a mashed key
  into one save. The two presses rendezvous through the single-instance lock
  plus small timestamp files in the runtime dir.
- Settings tooltips for every switch.

### Performance
- **Overlay rendering moved to the GPU.** The frozen capture is uploaded once
  as a GSK texture and the selection box, dimming, handles and size readout
  are render nodes, replacing a full-screen software blit + re-upload on every
  motion event. Measured on a 1080p / 120 Hz desktop: 1.0 ms CPU per frame
  during a drag (was 3.7 ms), with no dropped frames. Pen/text annotations
  preview through a single cached cairo node bounded to the annotations, using
  the very same drawing routine that bakes them into the shot.
- **Capture overlaps GTK setup.** The ScreenCast grab runs on a worker thread
  (private GLib main context) while the main thread builds and realizes the
  still-invisible overlay window; the frame is handed over before the
  ScreenCast session is torn down. Hotkey-to-first-frame: ~250 ms (was ~410).
- **Copy/Save hide the overlay before doing any work**, so GNOME's close
  animation starts the instant Enter is pressed while the crop, PNG encode and
  `wl-copy` run underneath it (a failure brings the window straight back with
  the error banner, as before). The process then exits immediately instead of
  spending ~100 ms in GTK/interpreter teardown while still holding the
  single-instance lock.
- **GL renderer by default** (`GSK_RENDERER=gl` unless already set): GTK's
  Vulkan renderer costs ~100 ms more at window realize for identical output.
- Leaner launch: a `snapclip` launcher imports the module so its bytecode is
  cached (~15 ms), `argparse` is only imported when there are arguments, and
  `subprocess`/`datetime` load lazily at confirm time.

### Fixed
- The selection border's outline was handed to GTK through a temporary
  `GskRoundedRect` that Python could free first (pixman "Invalid rectangle"
  warnings and, occasionally, a frame without the border); it is now kept
  alive for the call.

### Changed
- Requires GTK 4.14 or newer (GSK path fills/strokes and `B8G8R8X8` textures).
- The test suite renders the GSK scene offscreen and compares it pixel-for-pixel
  with the pre-1.3 cairo drawing; the portal-fallback tests report a SKIP
  when the portal itself refuses the session instead of aborting the run.

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
