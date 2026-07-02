#!/usr/bin/env python3
"""Automated tests for snapclip's core pipeline.

These avoid driving the GUI (no input-injection tool works on GNOME Wayland)
and instead exercise the risky, testable parts directly: portal capture, crop
correctness against known coordinates, scale math, clipboard round-trip, save
path/filename behaviour, config load/save, and file hygiene.

Run:  python3 test_snapclip.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GdkPixbuf  # noqa: E402

import cairo  # noqa: E402

import snapclip as sc  # noqa: E402

PASS, FAIL = 0, 0
PICTURES = os.path.expanduser("~/Pictures")


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def png_size(data):
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(data)
    loader.close()
    pb = loader.get_pixbuf()
    return pb.get_width(), pb.get_height()


def make_surface(w, h):
    """A synthetic 'screen': solid colours per quadrant so crops are checkable."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    quads = [((0, 0), (1, 0, 0)), ((w // 2, 0), (0, 1, 0)),
             ((0, h // 2), (0, 0, 1)), ((w // 2, h // 2), (1, 1, 0))]
    for (qx, qy), (r, g, b) in quads:
        cr.set_source_rgb(r, g, b)
        cr.rectangle(qx, qy, w // 2, h // 2)
        cr.fill()
    surf.flush()
    return surf


def pixel(data, px, py):
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(data); loader.close()
    pb = loader.get_pixbuf()
    pbytes = pb.get_pixels()
    stride = pb.get_rowstride()
    nch = pb.get_n_channels()
    off = py * stride + px * nch
    return tuple(pbytes[off:off + 3])


def pixel_rgba(data, px, py):
    """Like pixel() but includes alpha (255 for PNGs without an alpha channel)."""
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(data); loader.close()
    pb = loader.get_pixbuf()
    pbytes = pb.get_pixels()
    off = py * pb.get_rowstride() + px * pb.get_n_channels()
    rgba = tuple(pbytes[off:off + pb.get_n_channels()])
    return rgba if len(rgba) == 4 else rgba + (255,)


# ---------------------------------------------------------------------------
print("scale math")
check("scale 1:1", sc.compute_scale(1920, 1080, 1920, 1080) == (1.0, 1.0))
sx, sy = sc.compute_scale(3840, 2160, 1920, 1080)
check("scale 2x", sx == 2.0 and sy == 2.0)
sx, sy = sc.compute_scale(2400, 1350, 1920, 1080)
check("scale 1.25x", abs(sx - 1.25) < 1e-9 and abs(sy - 1.25) < 1e-9)

# the shared crop/readout rect helper: edges rounded first, then differenced
# 3*1.25=3.75->4, 7*1.25=8.75->9, 53*1.25=66.25->66, 47*1.25=58.75->59
check("selection_to_physical rounds edges then differences",
      sc.selection_to_physical([3, 7, 50, 40], (1.25, 1.25)) == (4, 9, 62, 50),
      sc.selection_to_physical([3, 7, 50, 40], (1.25, 1.25)))

# ---------------------------------------------------------------------------
print("crop correctness (scale 1.0)")
surf = make_surface(1920, 1080)
data = sc.crop_to_png_bytes(surf, [100, 50, 200, 150], (1.0, 1.0))
check("crop size matches selection", png_size(data) == (200, 150),
      png_size(data))
# top-left quadrant is red(1,0,0): a crop fully inside it should be red
check("crop pixel colour = top-left quadrant (red)",
      pixel(data, 5, 5) == (255, 0, 0), pixel(data, 5, 5))

# crop from green (top-right) quadrant
data2 = sc.crop_to_png_bytes(surf, [1000, 50, 100, 100], (1.0, 1.0))
check("crop from top-right quadrant (green)",
      pixel(data2, 5, 5) == (0, 255, 0), pixel(data2, 5, 5))

# ---------------------------------------------------------------------------
print("crop correctness (scale 2.0  -> fractional-scaling analogue)")
surf2 = make_surface(3840, 2160)  # physical
# logical selection 200x150 at (100,50); scale 2 -> 400x300 physical px
data3 = sc.crop_to_png_bytes(surf2, [100, 50, 200, 150], (2.0, 2.0))
check("scaled crop size = logical*scale", png_size(data3) == (400, 300),
      png_size(data3))

# ---------------------------------------------------------------------------
print("crop size is tile-consistent under fractional scaling (no off-by-one drift)")
big = make_surface(400, 400)
a = png_size(sc.crop_to_png_bytes(big, [0, 0, 5, 10], (1.1, 1.0)))
b = png_size(sc.crop_to_png_bytes(big, [5, 0, 5, 10], (1.1, 1.0)))
ab = png_size(sc.crop_to_png_bytes(big, [0, 0, 10, 10], (1.1, 1.0)))
check("adjacent crop widths sum to combined width", a[0] + b[0] == ab[0],
      f"{a[0]}+{b[0]} vs {ab[0]}")

# The live WxH readout must equal the actual produced crop size at every
# position under fractional scaling (readout uses the same edges-then-diff math)
fsurf = make_surface(2400, 1350)
mismatch = 0
for xpos in range(0, 120, 7):
    actual = png_size(sc.crop_to_png_bytes(fsurf, [xpos, 10, 50, 40], (1.25, 1.25)))[0]
    formula = max(1, int(round((xpos + 50) * 1.25)) - int(round(xpos * 1.25)))
    mismatch += (actual != formula)
check("crop width == readout formula at all positions (1.25x)", mismatch == 0,
      f"{mismatch} mismatches")

# ---------------------------------------------------------------------------
print("crop clamps to bounds (selection partly off-screen)")
surf3 = make_surface(800, 600)
data4 = sc.crop_to_png_bytes(surf3, [700, 500, 400, 400], (1.0, 1.0))
w4, h4 = png_size(data4)
check("clamped width", w4 == 100, w4)
check("clamped height", h4 == 100, h4)

# ---------------------------------------------------------------------------
print("region transform (move/resize a freeform path via its bbox)")
tri_path = [(10, 10), (30, 10), (10, 30)]
moved = sc.transform_path(tri_path, sc.path_bbox(tri_path), [110, 60, 20, 20])
check("translate keeps shape", moved == [(110, 60), (130, 60), (110, 80)], moved)
scaled = sc.transform_path(tri_path, sc.path_bbox(tri_path), [10, 10, 40, 10])
check("scale maps corners to the new bbox",
      scaled == [(10, 10), (50, 10), (10, 20)], scaled)
check("degenerate old bbox does not divide by zero",
      sc.transform_path([(5, 5)], [5, 5, 0, 0], [1, 2, 3, 4]) == [(1, 2)])

print("text label geometry (select/drag/delete hit-testing)")
t1 = {"text": "Hi", "size": 18, "pos": (100, 50)}
bb = sc.text_bbox(t1)
check("text bbox has positive size", bb[2] > 0 and bb[3] > 0, bb)
check("text bbox anchored at pos + entry padding",
      bb[0] == 106 and bb[1] == 54, bb)
check("longer text -> wider bbox",
      sc.text_bbox({**t1, "text": "Hi there, much longer"})[2] > bb[2])
check("bigger size -> taller bbox",
      sc.text_bbox({**t1, "size": 36})[3] > bb[3])
check("hit inside the label", sc.text_hit(t1, bb[0] + 2, bb[1] + 2))
check("hit within grab padding", sc.text_hit(t1, bb[0] - 3, bb[1] - 3))
check("miss far away", not sc.text_hit(t1, 400, 400))

print("eraser stroke hit-testing")
stk = {"width": 4.0, "points": [(0, 0), (100, 0)]}
check("point on the segment hits", sc.stroke_hit(stk, 50, 0))
check("point within width+slop hits", sc.stroke_hit(stk, 50, 7))
check("point beyond slop misses", not sc.stroke_hit(stk, 50, 20))
check("point past the endpoint misses", not sc.stroke_hit(stk, 130, 0))
dot = {"width": 6.0, "points": [(40, 40)]}
check("single-point stroke (dot) hits nearby", sc.stroke_hit(dot, 43, 42))

print("default colours are a WCAG set (pairwise contrast >= 3:1)")
def _lum(hexs):
    r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
def _contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
_dc = sc.DEFAULT_CONFIG
for a, b in [("border_color", "pen_color"), ("border_color", "text_color"),
             ("pen_color", "text_color")]:
    check(f"{a} vs {b} >= 3:1",
          _contrast(_dc[a], _dc[b]) >= 3.0,
          f"{_contrast(_dc[a], _dc[b]):.2f}")

# ---------------------------------------------------------------------------
print("freeform region (polygon/lasso) mask")
check("path_bbox", sc.path_bbox([(10, 20), (30, 5), (20, 40)]) == [10, 5, 20, 35],
      sc.path_bbox([(10, 20), (30, 5), (20, 40)]))
surfm = make_surface(100, 100)          # top-left quadrant (0-49,0-49) is red
tri = [(0, 0), (48, 0), (0, 48)]        # triangle inside the red quadrant
datam = sc.crop_to_png_bytes(surfm, sc.path_bbox(tri), (1.0, 1.0), mask_path=tri)
check("inside the path keeps pixels (opaque red)",
      pixel_rgba(datam, 4, 4) == (255, 0, 0, 255), pixel_rgba(datam, 4, 4))
check("outside the path is fully transparent",
      pixel_rgba(datam, 44, 44)[3] == 0, pixel_rgba(datam, 44, 44))
_rect_loader = GdkPixbuf.PixbufLoader.new_with_type("png")
_rect_loader.write(sc.crop_to_png_bytes(surfm, [0, 0, 10, 10], (1.0, 1.0)))
_rect_loader.close()
check("plain rect crops stay alpha-free (RGB png)",
      not _rect_loader.get_pixbuf().get_has_alpha())
# self-intersecting mask: preview dims with EVEN_ODD, crop must match it —
# a doubly-wound square is filled under WINDING but empty under EVEN_ODD
loop2 = [(0, 0), (20, 0), (20, 20), (0, 20),
         (0, 0), (20, 0), (20, 20), (0, 20)]
check("self-intersecting mask uses even-odd like the preview",
      pixel_rgba(sc.crop_to_png_bytes(surfm, [0, 0, 20, 20], (1.0, 1.0),
                                      mask_path=loop2), 10, 10)[3] == 0,
      pixel_rgba(sc.crop_to_png_bytes(surfm, [0, 0, 20, 20], (1.0, 1.0),
                                      mask_path=loop2), 10, 10))

print("annotations bake into the crop at physical resolution")
def deco(cr):
    cr.set_source_rgb(1, 0, 1)
    cr.rectangle(10, 10, 10, 10)        # logical coords
    cr.fill()
surfd = make_surface(200, 200)          # 100x100 physical crop below, all red
datad = sc.crop_to_png_bytes(surfd, [0, 0, 50, 50], (2.0, 2.0), decorate=deco)
check("decorated pixel lands at logical*scale",
      pixel(datad, 24, 24) == (255, 0, 255), pixel(datad, 24, 24))
check("pixels outside the annotation untouched",
      pixel(datad, 5, 5) == (255, 0, 0), pixel(datad, 5, 5))
check("annotation is clipped by a mask when both are used",
      pixel_rgba(sc.crop_to_png_bytes(surfd, sc.path_bbox(tri), (1.0, 1.0),
                                      mask_path=tri, decorate=deco),
                 44, 44)[3] == 0)

# ---------------------------------------------------------------------------
print("monitor origin offset")
surf5 = make_surface(1920, 1080)
# selection at logical (10,10) on a monitor whose origin is (1000,0) lands in
# the top-right (green) quadrant of the capture
data5 = sc.crop_to_png_bytes(surf5, [10, 10, 50, 50], (1.0, 1.0),
                             origin_px=(1000, 0))
check("origin_px offset picks right region (green)",
      pixel(data5, 5, 5) == (0, 255, 0), pixel(data5, 5, 5))

# ---------------------------------------------------------------------------
print("interaction geometry: hit_zone")
sel = [100, 100, 200, 150]   # x,y,w,h -> spans (100,100)-(300,250)
check("center -> inside", sc.hit_zone(sel, 200, 175) == sc.Z_INSIDE)
check("far away -> outside", sc.hit_zone(sel, 800, 800) == sc.Z_OUTSIDE)
check("top-left corner -> nw", sc.hit_zone(sel, 100, 100) == sc.Z_NW)
check("top-right corner -> ne", sc.hit_zone(sel, 300, 100) == sc.Z_NE)
check("bottom-left corner -> sw", sc.hit_zone(sel, 100, 250) == sc.Z_SW)
check("bottom-right corner -> se", sc.hit_zone(sel, 300, 250) == sc.Z_SE)
check("right edge mid -> e", sc.hit_zone(sel, 300, 175) == sc.Z_E)
check("top edge mid -> n", sc.hit_zone(sel, 200, 100) == sc.Z_N)

print("interaction geometry: resize_rect")
# drag the SE corner by (+50,+30) grows w and h
check("SE grow", sc.resize_rect([100, 100, 200, 150], sc.Z_SE, 50, 30)
      == [100, 100, 250, 180])
# drag the W edge by (-40,0) moves left edge out, grows width
check("W edge", sc.resize_rect([100, 100, 200, 150], sc.Z_W, -40, 0)
      == [60, 100, 240, 150])
# drag N edge down by +30 shrinks height, moves top
check("N edge", sc.resize_rect([100, 100, 200, 150], sc.Z_N, 0, 30)
      == [100, 130, 200, 120])
# flip: drag SE corner far left/up past the origin -> normalised positive rect
flipped = sc.resize_rect([100, 100, 200, 150], sc.Z_SE, -260, -200)
check("flip normalises to positive size",
      flipped[2] > 0 and flipped[3] > 0, flipped)

print("normalize")
check("negative width flips", sc.normalize([100, 100, -40, 60]) == [60, 100, 40, 60])
check("negative height flips", sc.normalize([100, 100, 40, -60]) == [100, 40, 40, 60])

print("single-instance lock (no stacked overlays)")
_lock_orig = sc.LOCK_PATH
sc.LOCK_PATH = os.path.join(tempfile.gettempdir(), "snapclip-test.lock")
try:
    fp1 = sc.acquire_single_instance_lock()
    check("first instance gets the lock (returns a file)", hasattr(fp1, "close"))
    fp2 = sc.acquire_single_instance_lock()
    check("second instance -> ALREADY_RUNNING (exit, not stack)",
          fp2 is sc.ALREADY_RUNNING)
    if hasattr(fp1, "close"):
        fp1.close()                       # first instance exits -> lock released
    fp3 = sc.acquire_single_instance_lock()
    check("lock is reacquirable after the first exits", hasattr(fp3, "close"))
    if hasattr(fp3, "close"):
        fp3.close()
    # unwritable/missing lock dir must NOT be read as 'already running' — it
    # must return None so the tool still runs (lockless), never silently no-op.
    sc.LOCK_PATH = "/nonexistent-dir-xyz/snapclip.lock"
    res = sc.acquire_single_instance_lock()
    check("uncreatable lock -> None (proceed lockless, not a silent no-op)",
          res is None)
finally:
    sc.LOCK_PATH = _lock_orig

print("interaction geometry: double-click full-monitor toggle")
LW, LH = 1920, 1080
box = [100, 100, 400, 300]
# first toggle: remember box, expand to full
sel, prev = sc.toggle_full_selection(box, None, LW, LH)
check("toggle expands to full monitor", sel == [0, 0, LW, LH], sel)
check("toggle remembers previous box", prev == box, prev)
# second toggle: restore the box
sel2, prev2 = sc.toggle_full_selection(sel, prev, LW, LH)
check("toggle back restores previous box", sel2 == box, sel2)
# toggling full with no remembered box stays full (no crash)
sel3, prev3 = sc.toggle_full_selection([0, 0, LW, LH], None, LW, LH)
check("full with no memory stays full", sel3 == [0, 0, LW, LH], sel3)
# a box 1px from the edge must NOT be mistaken for "already full" and discarded
near = [0, 0, LW - 1, LH]
sel4, prev4 = sc.toggle_full_selection(near, None, LW, LH)
check("near-full box is remembered, not discarded",
      sel4 == [0, 0, LW, LH] and prev4 == near, (sel4, prev4))

# ---------------------------------------------------------------------------
print("clipboard round-trip (no temp file)")
surf6 = make_surface(400, 300)
png = sc.crop_to_png_bytes(surf6, [0, 0, 120, 80], (1.0, 1.0))
sc.copy_png_to_clipboard(png)
time.sleep(0.3)
types = subprocess.run(["wl-paste", "--list-types"],
                       capture_output=True, text=True).stdout
check("clipboard offers image/png", "image/png" in types, types.strip())
back = subprocess.run(["wl-paste", "--type", "image/png"],
                      capture_output=True).stdout
check("clipboard image survives & matches size", png_size(back) == (120, 80),
      png_size(back))
subprocess.run(["wl-copy", "--clear"])

# ---------------------------------------------------------------------------
print("save_png: timestamped + no clobber")
with tempfile.TemporaryDirectory() as td:
    fmt = "shot-%Y.png"  # same name for both -> must not clobber
    from datetime import datetime
    when = datetime(2026, 6, 30, 12, 0, 0)
    p1 = sc.save_png(b"\x89PNG\r\n\x1a\n", td, fmt, when=when)
    p2 = sc.save_png(b"\x89PNG\r\n\x1a\n", td, fmt, when=when)
    check("first save exists", os.path.exists(p1))
    check("second save did not clobber first", p1 != p2 and os.path.exists(p2),
          f"{p1} / {p2}")
    check("filename is timestamped", os.path.basename(p1) == "shot-2026.png",
          os.path.basename(p1))
    # subdirectory formats are allowed and created
    ps = sc.save_png(b"x", td, "%Y/sub/shot.png", when=when)
    check("nested filename format creates subdirs",
          os.path.exists(ps) and os.sep + "2026" + os.sep in ps, ps)
    # absolute-path / parent-escape formats must stay inside save_dir
    pe = sc.save_png(b"x", td, "/etc/evil.png", when=when)
    check("absolute filename format cannot escape save_dir",
          os.path.realpath(pe).startswith(os.path.realpath(td)), pe)
    pd = sc.save_png(b"x", td, "../../escape.png", when=when)
    check("parent-dir filename format cannot escape save_dir",
          os.path.realpath(pd).startswith(os.path.realpath(td)), pd)

# ---------------------------------------------------------------------------
print("config load/save round-trip")
orig = sc.CONFIG_PATH
try:
    with tempfile.TemporaryDirectory() as td:
        sc.CONFIG_DIR = td
        sc.CONFIG_PATH = os.path.join(td, "config.json")
        cfg = sc.load_config()
        check("defaults loaded when no file",
              cfg["border_color"] == sc.DEFAULT_CONFIG["border_color"])
        cfg["border_color"] = "#123456"
        cfg["last_selection"] = [1, 2, 3, 4]
        sc.save_config(cfg)
        cfg2 = sc.load_config()
        check("config persists", cfg2["border_color"] == "#123456")
        check("last_selection persists", cfg2["last_selection"] == [1, 2, 3, 4])
        # corrupt config must not raise
        with open(sc.CONFIG_PATH, "w") as fh:
            fh.write("{ not json ]")
        cfg3 = sc.load_config()
        check("corrupt config falls back to defaults",
              cfg3["border_color"] == sc.DEFAULT_CONFIG["border_color"])
        # malformed/null fields must be coerced back to safe defaults
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"save_dir": None, "filename_format": "",
                       "border_width": "huge", "include_cursor": "yes",
                       "last_selection": [1, 2, 3]}, fh)
        cfg4 = sc.load_config()
        check("null save_dir coerced to default str",
              cfg4["save_dir"] == sc.DEFAULT_CONFIG["save_dir"])
        check("empty filename_format coerced",
              cfg4["filename_format"] == sc.DEFAULT_CONFIG["filename_format"])
        check("non-numeric border_width coerced",
              cfg4["border_width"] == sc.DEFAULT_CONFIG["border_width"])
        check("non-bool include_cursor coerced",
              cfg4["include_cursor"] is sc.DEFAULT_CONFIG["include_cursor"])
        check("malformed last_selection nulled", cfg4["last_selection"] is None)
        # an unparseable colour string must fall back to the default
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"border_color": "blurple", "handle_color": "#zzz"}, fh)
        cfg5 = sc.load_config()
        check("invalid border_color coerced to default",
              cfg5["border_color"] == sc.DEFAULT_CONFIG["border_color"])
        check("invalid handle_color coerced to default",
              cfg5["handle_color"] == sc.DEFAULT_CONFIG["handle_color"])
        # remember-last-selection ships OFF by default
        check("remember_selection defaults to off",
              sc.DEFAULT_CONFIG["remember_selection"] is False)
        # default_size_pct sanitized
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"default_size_pct": "big"}, fh)
        cfg6 = sc.load_config()
        check("non-numeric default_size_pct coerced",
              cfg6["default_size_pct"] == sc.DEFAULT_CONFIG["default_size_pct"])
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"default_size_pct": 0.6}, fh)
        check("valid default_size_pct persists",
              sc.load_config()["default_size_pct"] == 0.6)
        # out-of-range numbers are clamped, not taken at face value
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"border_width": 99, "dim_opacity": 7,
                       "default_size_pct": 0.001}, fh)
        cfg7 = sc.load_config()
        check("oversized border_width clamped", cfg7["border_width"] == 12,
              cfg7["border_width"])
        check("out-of-range dim_opacity clamped", cfg7["dim_opacity"] == 0.9,
              cfg7["dim_opacity"])
        check("tiny default_size_pct clamped",
              cfg7["default_size_pct"] == 0.05, cfg7["default_size_pct"])
        # optional tools + always-save ship OFF (clean default toolbar)
        check("tools and always_save default off",
              not any(sc.DEFAULT_CONFIG[k] for k in
                      ("tool_polygon", "tool_lasso", "tool_pen", "tool_text",
                       "always_save")))
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"pen_width": 99, "text_size": 1, "pen_color": "nope",
                       "tool_pen": "yes"}, fh)
        cfg8 = sc.load_config()
        check("pen_width clamped", cfg8["pen_width"] == 16, cfg8["pen_width"])
        check("text_size clamped", cfg8["text_size"] == 8, cfg8["text_size"])
        check("bad pen_color coerced",
              cfg8["pen_color"] == sc.DEFAULT_CONFIG["pen_color"])
        check("non-bool tool flag coerced", cfg8["tool_pen"] is False)
finally:
    sc.CONFIG_PATH = orig

# ---------------------------------------------------------------------------
print("ScreenCast capture (flash-free, primary path)")
pics_before = set(os.listdir(PICTURES)) if os.path.isdir(PICTURES) else set()
try:
    sc_surf, sc_conn = sc.screencast_capture()
    check("screencast returned a surface", isinstance(sc_surf, cairo.ImageSurface))
    check("screencast reports the captured connector",
          isinstance(sc_conn, str) and bool(sc_conn), sc_conn)
    check("screencast surface has sane dimensions",
          sc_surf.get_width() > 0 and sc_surf.get_height() > 0,
          (sc_surf.get_width(), sc_surf.get_height()))
    pics_after = set(os.listdir(PICTURES)) if os.path.isdir(PICTURES) else set()
    check("screencast writes nothing to ~/Pictures (no flash, no file)",
          pics_after == pics_before, f"new: {pics_after - pics_before}")
except sc.CaptureError as exc:
    check("screencast capture", False, f"CaptureError: {exc}")

print("capture_screen() prefers flash-free ScreenCast")
try:
    surf_cs, full_desktop, conn_cs = sc.capture_screen()
    check("capture_screen returns a surface",
          isinstance(surf_cs, cairo.ImageSurface))
    check("capture_screen used per-monitor ScreenCast (full_desktop is False)",
          full_desktop is False, f"full_desktop={full_desktop}")
    check("capture_screen passes the connector through",
          isinstance(conn_cs, str) and bool(conn_cs), conn_cs)
except sc.CaptureError as exc:
    check("capture_screen", False, f"CaptureError: {exc}")

print("capture_screen() never silently flashes")
_orig_sccap = sc.screencast_capture
sc.screencast_capture = lambda *a, **k: (_ for _ in ()).throw(
    sc.CaptureError("simulated ScreenCast outage"))
try:
    raised = False
    try:
        sc.capture_screen()           # default: must error, NOT flash
    except sc.CaptureError:
        raised = True
    check("default capture_screen errors instead of flashing", raised)
    surf_fb, full_fb, conn_fb = sc.capture_screen(allow_flash=True)  # -> portal
    check("capture_screen(allow_flash=True) falls back to the portal",
          isinstance(surf_fb, cairo.ImageSurface) and full_fb is True,
          f"full_desktop={full_fb}")
    check("portal fallback reports no connector (full-desktop capture)",
          conn_fb is None, conn_fb)
finally:
    sc.screencast_capture = _orig_sccap

print("screencast_capture converts a GStreamer failure to CaptureError "
      "(no raw traceback escaping)")
import gi as _gi
_gi.require_version("Gst", "1.0")
from gi.repository import Gst as _Gst   # noqa: E402
_orig_pl = _Gst.parse_launch
def _boom(*a, **k):
    raise RuntimeError("simulated missing pipewiresrc plugin")
_Gst.parse_launch = _boom
try:
    kind = None
    try:
        sc.screencast_capture()        # real ScreenCast session, then patched pipeline
    except sc.CaptureError:
        kind = "CaptureError"
    except Exception as e:             # would be the bug: raw error escapes
        kind = type(e).__name__
    check("GStreamer failure surfaces as CaptureError (clean fallback path)",
          kind == "CaptureError", f"got {kind}")
finally:
    _Gst.parse_launch = _orig_pl

# ---------------------------------------------------------------------------
print("portal capture + cleanup (fallback path)")
before = set(os.listdir(PICTURES)) if os.path.isdir(PICTURES) else set()
try:
    surf_real = sc.portal_capture()
    check("portal returned a surface",
          isinstance(surf_real, cairo.ImageSurface))
    check("captured surface has sane dimensions",
          surf_real.get_width() > 0 and surf_real.get_height() > 0,
          (surf_real.get_width(), surf_real.get_height()))
    after = set(os.listdir(PICTURES)) if os.path.isdir(PICTURES) else set()
    new_files = after - before
    check("portal dump cleaned up (no leftover in ~/Pictures)",
          not new_files, f"leftover: {new_files}")
except sc.CaptureError as exc:
    check("portal capture", False, f"CaptureError: {exc}")

# ---------------------------------------------------------------------------
print()
print(f"=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
