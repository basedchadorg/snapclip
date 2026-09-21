# snapclip — screenshots **without the screen flash** on Linux (Wayland / GNOME)

**snapclip is a minimal region-screenshot tool for Linux that does *not* flash the
screen when you take a screenshot.** Drag a see-through box over anything, press
**Enter** to copy it straight to the clipboard (or **S** to also save a PNG), and
the window closes. No flash, no shutter animation, no leftover files, and the
selection border is never in the shot.

Built for **Ubuntu / GNOME / Wayland**, where the usual screenshot tools either
don't work or trigger GNOME's annoying screen-flash effect.

![snapclip's flash-free screenshot overlay on GNOME Wayland — a see-through selection box with a thin border](docs/overlay.png)

---

## Why does taking a screenshot flash the screen on GNOME / Wayland?

If you searched for *"how to take a screenshot without the screen flashing on
Linux"*, *"disable GNOME screenshot flash"*, or *"Wayland screenshot no flash
animation"* — here's what's going on:

On **GNOME (Mutter) under Wayland**, the screenshot is taken by GNOME Shell
itself, through the **`org.gnome.Shell.Screenshot` D-Bus interface** or the **XDG
desktop `Screenshot` portal**. Every time that path captures the screen, GNOME
Shell plays a **white shutter-flash animation**. There is **no setting to turn
the screenshot flash off** — it's baked into the capture path that almost every
screenshot tool on GNOME Wayland uses (including the built-in `PrintScreen` UI and
portal-based tools like Flameshot on Wayland).

You also can't just switch tools: `grim`/`slurp`/`scrot` **don't work on GNOME**
(Mutter has no `wlr-screencopy`), and on modern GNOME `org.gnome.Shell.Screenshot`
returns `AccessDenied` to normal apps. So the only "supported" path is the portal
— and the portal flashes.

## How snapclip avoids the screenshot flash

snapclip captures the screen a different way: it grabs a **single frame from
Mutter's ScreenCast interface** (`org.gnome.Mutter.ScreenCast`) over **PipeWire**,
the same screen-recording machinery used for screen sharing. Screen recording does
**not** play the shutter flash and shows **no picker dialog** — so the capture is
completely silent and invisible.

That's the whole trick: **use screen-*cast* (recording), not screen-*shot*
(the flashing path).** snapclip grabs one frozen frame, lets you crop it, copies
the crop to the clipboard, and exits — with no flash at any point.

> Because *no flash* is the entire point, snapclip will **never** silently fall
> back to the flashing portal. If ScreenCast is unavailable it tells you what to
> install instead of flashing; you can pass `--allow-flash` to deliberately use
> the portal anyway.

---

## Features

- ✅ **No screen flash** — ever (Mutter ScreenCast + PipeWire capture).
- ✅ **Copy to clipboard** as `image/png` — paste into chat, a browser, GIMP, etc.
- ✅ **Copy never writes a file**; **Save** writes a timestamped PNG only when you ask.
- ✅ **See-through selection** — thin border + handles, the interior shows the real screen.
- ✅ Move / resize / rubber-band a new box; **double-click** to grab the whole monitor.
- ✅ **No capture flash, no toolbar clutter, no leftover temp files.**
- ✅ Correct under **fractional scaling** and **multi-monitor**.
- ✅ **Works over fullscreen apps and games** (Minecraft, fullscreen video):
  the capture does not depend on the compositor repainting, and the
  overlay opens on top with focus.
- ✅ Settings (border, dim, save folder, …) + a hotkey-friendly single command.
- ✅ **Optional tools, all off by default** so the toolbar stays just
  Copy / Save / Cancel: polygon & freehand selection (transparent outside the
  shape), a pen and a text tool (each with its own colour) that bake into the
  shot, and an "always save a copy" mode. Flip them on in ⚙ settings.

---

## Install

Tested on **Ubuntu 26.04 / GNOME 50 / Wayland** (should work on any GNOME-Wayland
with `org.gnome.Mutter.ScreenCast`).

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 wl-clipboard \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-pipewire gstreamer1.0-plugins-base -y

git clone https://github.com/gitoffmylibrary/snapclip.git
cd snapclip
./install.sh        # symlinks ~/.local/bin/snapclip (the launcher) + an apps-menu entry (no root)
```

`python3-gi-cairo` is required (GTK's Cairo drawing fails without it) and the
overlay needs **GTK 4.14 or newer** (it renders through GSK paths/textures); the
GStreamer packages provide the flash-free capture. You can also run it in place
without installing: `python3 snapclip.py`.

---

## Usage

Run `snapclip` (after `install.sh`), from your apps menu, or via a hotkey.

| Action | Key | Button |
|---|---|---|
| Copy region to clipboard, then quit | **Enter** or **Ctrl+C** | **Copy** |
| Save timestamped PNG **and** copy, then quit | **S** | **Save** |
| Nudge the box by 1 px (**Shift**: resize by 1 px) | **Arrow keys** | — |
| Undo the last pen stroke / text | **Ctrl+Z** | — |
| Leave the active tool mode; then cancel | **Esc** | **Cancel** |
| Open settings | — | **⚙** |

- **Move:** drag inside the box (or arrow keys for 1 px steps).
- **Resize:** drag any edge or corner handle (or Shift+arrows for 1 px steps).
- **New selection:** drag on empty space to rubber-band a fresh box.
- **Double-click:** snap to the whole monitor; double-click again to restore.
- A live **W×H** readout (in real pixels) sits at the selection corner.

### Optional tools (off by default)

Enable any of these in ⚙ settings and a button for it appears on the toolbar
(the default toolbar stays exactly Copy / Save / Cancel):

- **Poly** — click the corners of a polygon; double-click or **Enter** closes
  it. The copied/saved PNG is transparent outside the shape.
- **Lasso** — drag a freehand loop around anything; same transparent result.
- Once closed, a Poly/Lasso region **moves and resizes exactly like the
  rectangle** — drag inside it, grab its handles, or use the arrow keys.
  Dragging on empty space (or double-clicking) brings the rectangle back.
- **Pen** — draw strokes on the frozen shot in your configured colour/width;
  they're baked into the result at full resolution. **Ctrl+Z** undoes.
- **Erase** — appears automatically whenever Pen is enabled: click or drag
  over a stroke to remove it (**Ctrl+Z** brings it back). Erases pen strokes
  only.
- **Text** — click to type a label in your configured colour/size; **Enter**
  places it, **Esc** discards it. Placed labels stay editable: **click one to
  select it** (dashed grab box), **drag it** to move, **Delete** removes it —
  and **Ctrl+Z** undoes any of that.

The default colours ship as a **WCAG set** — selection border `#0077CC`, pen
`#FFD60A`, text `#2D0A4E` have pairwise contrast ratios ≥ 3:1, so the three
are distinguishable out of the box (and each is still fully customizable).

Copy puts the crop on the clipboard as `image/png` and **never writes a file**.
Save additionally writes `~/Pictures/Screenshots/snapclip-YYYY-MM-DD_HH-MM-SS.png`
(folder/format configurable).

### Multi-monitor

snapclip captures the **primary** monitor unless told otherwise:

```bash
snapclip -l                   # list monitors: index, connector, name, [Primary]
snapclip -m HDMI-1            # capture by connector name (recommended for hotkeys)
snapclip -m 1                 # ...or by index, as printed by -l
snapclip -m "Built-in"        # ...or by a substring of the display name
```

Indices are assigned left-to-right by screen position, so they can change
when a monitor is plugged in or rearranged; connector names stay put, which
makes them the safer choice for a hotkey (e.g. `Super+Shift+1` →
`snapclip -m eDP-1`, `Super+Shift+2` → `snapclip -m HDMI-1`). An unknown
target falls back to the primary monitor with a note on stderr rather than
refusing to shoot. With two or more monitors, ⚙ settings also gains a
**Default monitor** row for launches without `-m`.

### Bind it to a hotkey (recommended)

GNOME → **Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts → +**

- **Name:** snapclip
- **Command:** `snapclip` (after `./install.sh`) — or `python3 /full/path/to/snapclip.py`
  (only one overlay opens at a time — pressing the key again while it's open is a no-op)
- **Shortcut:** e.g. `Super+Shift+S` or `Ctrl+Alt+S`

To put it on **PrintScreen**, first clear GNOME's built-in binding at
*Settings → Keyboard → Screenshots*. Avoid plain app combos like `Ctrl+S`.

---

## Settings (the ⚙ button)

Stored in `~/.config/snapclip/config.json`:

The dialog is grouped into **Selection**, **Output**, and **Tools**; changes
apply live and are written once when the dialog closes.

| Setting | Default | Notes |
|---|---|---|
| Default monitor | `primary` | Monitor to capture when no `-m` flag is given; the row appears only with two or more monitors |
| Border colour | `#0077CC` | Selection outline colour (WCAG-distinct from pen/text defaults) |
| Border width | `2` | px |
| Outside dim | `0.35` | Darkening **outside** the selection; interior is always see-through. `0` = none |
| Default size (× screen) | `0.4` | Size of the fresh centered box (used when not remembering) |
| Remember last selection | `off` | On: reopen with your previous box. Off: a fresh centered box each time |
| Save folder | `~/Pictures/Screenshots` | Click to edit in a popover, or Browse… |
| Filename format | `snapclip-%Y-%m-%d_%H-%M-%S.png` | `strftime` pattern; click to edit in a popover |
| Always save a copy | `off` | On: plain **Copy** (Enter) also writes the timestamped PNG |
| Include mouse cursor | `off` | Best-effort: composites a pointer glyph inside the selection |
| Quick-save on double-tap | `off` | On: double-tap the hotkey to save + copy the whole screen with no overlay (a single tap waits ~300 ms first) |
| Polygon selection | `off` | Adds the **Poly** toolbar button |
| Freehand selection | `off` | Adds the **Lasso** toolbar button |
| Pen (+ colour, width) | `off`, `#FFD60A`, `3` | Adds the **Pen** button — and **Erase** rides along automatically |
| Text (+ colour, size) | `off`, `#2D0A4E`, `18` | Adds the **Text** toolbar button |

---

## How it works (and why, on Wayland)

1. **Capture once, up front, flash-free.** Before any window exists, snapclip
   grabs one frame of the primary monitor via Mutter ScreenCast + PipeWire into a
   Cairo surface (≈0.2 s, no flash, no file on disk). If ScreenCast is unavailable
   it refuses to capture (rather than silently flashing) unless you pass
   `--allow-flash`, which uses the portal and deletes its PNG dump.
   The monitor is recorded as an **area stream** of its logical rectangle
   (`RecordArea`) rather than a monitor stream (`RecordMonitor`): a monitor
   stream only produces a frame when the compositor next paints that monitor,
   and while a fullscreen game holds the display on **direct scanout** the
   compositor is not painting at all, so no frame ever comes. An area stream
   paints the scene into the PipeWire buffer on demand the moment it starts
   (~20 ms over a fullscreen Minecraft, where a monitor stream delivered
   nothing in 10 s). `RecordMonitor` remains the fallback if the area call is
   refused.
2. **Frozen full-screen overlay.** That capture is shown frozen inside a single
   undecorated, full-screen GTK4 window. The selection rectangle is drawn *as
   graphics inside that fixed surface* — the window is never moved, which sidesteps
   Wayland's rule that an app may not set its own window position (the thing that
   breaks "draggable floating window" designs).
3. **Crop the pre-overlay capture.** Confirm crops the *cached* image, so the
   border/dim/toolbar can never appear in the result and there is no second capture.
4. **Clipboard with no temp file.** The crop is piped to `wl-copy --type image/png`
   over stdin; `wl-copy` daemonizes so the paste survives after snapclip exits.
5. **Scaling.** The crop scales the logical selection by `capture_px / logical_px`,
   so it stays correct under fractional display scaling.

### Performance notes

Every launch is a fresh process (that is how a hotkey works), so the whole
tool is built around latency:

- **Capture and GTK setup overlap.** The ScreenCast grab runs on a worker
  thread while the main thread builds and realizes the (still invisible)
  overlay window — GL context, shaders, widgets. Nothing is mapped until the
  frame exists, so the overlay never appears in its own shot.
- **The overlay is rendered by the GPU.** The frozen frame is uploaded once as
  a texture; the dimming, border, handles and readout are GSK render nodes.
  Dragging the box costs ~1 ms of CPU per frame instead of a full-screen
  software blit, so it stays smooth at 120 Hz and on 4K/HiDPI displays.
- **Copy/Save hide the window first.** GNOME's close animation starts the
  instant you press Enter; the crop, PNG encode and `wl-copy` happen
  underneath it and the process exits without waiting for interpreter
  teardown.
- **GL renderer by default.** GTK's Vulkan renderer costs ~100 ms more per
  launch here for identical output, so snapclip sets `GSK_RENDERER=gl` unless
  you have set `GSK_RENDERER` yourself.
- **`snapclip` (the launcher) imports `snapclip.py`** so Python caches its
  bytecode; running the `.py` directly still works but recompiles it each time.

## Testing

```bash
python3 test_snapclip.py               # core pipeline suite (capture, crop,
                                       # scaling, clipboard, save, config, geometry,
                                       # no-flash policy)
python3 snapclip.py --self-test copy   # exercise capture->crop->clipboard, no GUI
python3 snapclip.py --self-test save   # ...and the save path
```

Mouse-driven drag/resize can't be auto-tested (no input-injection tool works on
GNOME Wayland), so the move/resize **math** is unit-tested directly and the overlay
rendering is verified by screenshotting it.

---

## FAQ

### How do I take a screenshot on Linux without the screen flashing?
Use a tool that captures via **screen recording** (PipeWire / `ScreenCast`) instead
of the GNOME screenshot path. snapclip does exactly this. The GNOME screenshot UI
and portal-based tools flash because GNOME Shell plays a shutter animation on every
shot, and there is no option to disable it.

### Can I disable the GNOME screenshot flash with a setting?
No. GNOME does not expose a setting to turn off the screenshot flash — it's part of
the Shell's capture path. The only way to avoid it is to capture a different way
(which is what snapclip does).

### Why don't `grim`, `slurp`, or `scrot` work on my GNOME Wayland system?
`grim`/`slurp` rely on the `wlr-screencopy` protocol, which **wlroots** compositors
implement but **GNOME's Mutter does not**. `scrot` is X11-only. On GNOME Wayland the
supported capture paths are the GNOME Shell screenshot D-Bus interface (now
`AccessDenied` to normal apps), the XDG screenshot portal (which flashes), or — what
snapclip uses — Mutter ScreenCast over PipeWire (no flash).

### Does it work over fullscreen games and videos?
Yes. Fullscreen windows on GNOME are usually put on **direct scanout** (their
buffers go straight to the display, bypassing the compositor). A plain
ScreenCast *monitor* stream waits for the compositor to paint, which never
happens in that state, so tools built on it hang or come back with a stale
frame. snapclip records the monitor's *area* instead, which paints the scene
on demand, so the overlay appears immediately on top of the game with the
current frame frozen behind it. Games that grab the keyboard outright
(Wayland shortcut inhibition) will keep the hotkey for themselves, as they do
for every GNOME shortcut.

### Does snapclip put the screenshot in the clipboard or save a file?
**Enter copies to the clipboard only** (`image/png`) and writes nothing to disk.
**S** copies *and* saves a timestamped PNG to `~/Pictures/Screenshots`. **Esc**
cancels with no capture and no clipboard change.

### Will the selection border show up in my screenshot?
No. snapclip crops a frame captured *before* the overlay appears, so the border,
dim, handles, and toolbar are never in the result.

### Does it work with fractional scaling and multiple monitors?
Yes. It captures the monitor the overlay opens on and scales the crop by
`capture_px / logical_px`, so the result matches your selection at any scale.

---

## Limitations

- One monitor per shot: the overlay opens on the primary (or the `-m` /
  configured) monitor and the crop is offset-correct at any scale there; a
  selection cannot span two screens.
- "Include cursor" is best-effort (the capture excludes the real pointer).
- Requires `org.gnome.Mutter.ScreenCast` (GNOME Wayland). On stripped-down setups
  without it, pass `--allow-flash` to use the portal (which flashes).

## License

MIT — see [LICENSE](LICENSE).

---

*Keywords: Linux screenshot without flash · GNOME screenshot no flash · disable
screenshot flash Wayland · Wayland region screenshot to clipboard · screenshot tool
no shutter animation · Ubuntu GNOME screenshot flash fix.*
