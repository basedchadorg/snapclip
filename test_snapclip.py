#!/usr/bin/env python3
"""Automated tests for snapclip's core pipeline.

These avoid driving the GUI (no input-injection tool works on GNOME Wayland)
and instead exercise the risky, testable parts directly: portal capture, crop
correctness against known coordinates, scale math, clipboard round-trip, save
path/filename behaviour, config load/save, file hygiene, and the GPU overlay
scene (rendered offscreen and compared pixel-for-pixel with the pre-1.3 cairo
drawing).

Run:  python3 test_snapclip.py
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402
from gi.repository import Gtk, Gdk, Gsk, Graphene, GLib, Pango  # noqa: E402

import cairo  # noqa: E402

import snapclip as sc  # noqa: E402

PASS, FAIL = 0, 0
PICTURES = os.path.expanduser("~/Pictures")
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def skip(name, why):
    print(f"  SKIP  {name}  ({why})")


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
check("memoized metrics give identical bboxes", sc.text_bbox(t1) == bb)
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
print("GPU overlay scene: geometry helpers")


def _rect_area(r):
    return r[2] * r[3]


def _overlap(a, b):
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


_W, _H = 400, 300
for _sel in ([50, 40, 120, 80], [0, 0, 400, 300], [0, 0, 10, 10],
             [390, 290, 10, 10], [-20, -10, 60, 50], [-50, -50, 20, 20]):
    rects = sc.dim_rects(_sel, _W, _H)
    x, y, w, h = _sel
    vis_w = max(0, min(_W, x + w) - max(0, x))
    vis_h = max(0, min(_H, y + h) - max(0, y))
    visible = [max(0, x), max(0, y), vis_w, vis_h]
    outside = _W * _H - vis_w * vis_h
    check(f"dim rects cover exactly the outside of {_sel}",
          abs(sum(_rect_area(r) for r in rects) - outside) < 1e-6
          and all(_overlap(r, visible) == 0 for r in rects)
          and all(_overlap(r1, r2) == 0 for i, r1 in enumerate(rects)
                  for r2 in rects[i + 1:])
          and all(0 <= r[0] and 0 <= r[1] and r[0] + r[2] <= _W
                  and r[1] + r[3] <= _H for r in rects),
          rects)

_strokes = [{"rgba": (1, 0, 0, 1), "width": 6.0, "points": [(20, 30), (80, 90)]}]
_texts = [{"rgba": (0, 0, 0, 1), "size": 18.0, "text": "label", "pos": (150, 20)}]
_ab = sc.annotation_bounds(_strokes, _texts, _W, _H)
check("annotation bounds contain the stroke plus its round caps",
      _ab[0] <= 20 - 3 and _ab[1] <= 30 - 3
      and _ab[0] + _ab[2] >= 80 + 3 and _ab[1] + _ab[3] >= 90 + 3, _ab)
_tb = sc.text_bbox(_texts[0])
check("annotation bounds contain the text bbox with glyph padding",
      _ab[0] + _ab[2] >= _tb[0] + _tb[2] + 8 and _ab[1] <= _tb[1] - 8, _ab)
check("annotation bounds clamp to the monitor",
      sc.annotation_bounds([{"rgba": (1, 0, 0, 1), "width": 4.0,
                             "points": [(-50, -50), (10, 10)]}], [], _W, _H)[:2]
      == (0.0, 0.0))
check("no annotations -> no bounds", sc.annotation_bounds([], [], _W, _H) is None)

_tsurf = make_surface(64, 48)
_tsurf24 = cairo.ImageSurface(cairo.FORMAT_RGB24, 64, 48)
_tcr = cairo.Context(_tsurf24); _tcr.set_source_surface(_tsurf, 0, 0); _tcr.paint()
_tex = sc.texture_for_surface(_tsurf24)
check("texture has the capture's size", (_tex.get_width(), _tex.get_height()) == (64, 48))
_dl = Gdk.TextureDownloader.new(_tex)
_dl.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED)
_tbytes, _tstride = _dl.download_bytes()
_tbytes = bytes(_tbytes.get_data())
_src = bytes(_tsurf24.get_data())
_same = all(_tbytes[r * _tstride + c * 4:r * _tstride + c * 4 + 3]
            == _src[r * _tsurf24.get_stride() + c * 4:
                    r * _tsurf24.get_stride() + c * 4 + 3]
            for r in range(48) for c in range(64))
check("texture pixels == capture pixels (BGR, one copy)", _same)

# ---------------------------------------------------------------------------
print("GPU overlay scene == the pre-1.3 cairo drawing (offscreen render, pixel compare)")

_pango_ctx = Gtk.Label().get_pango_context()


def _pango_layout(text):
    layout = Pango.Layout.new(_pango_ctx)
    layout.set_text(text, -1)
    layout.set_font_description(sc._BADGE_FONT)
    return layout


def _state(surface, **over):
    tex = sc.texture_for_surface(surface)
    bc = Gdk.RGBA(); bc.parse("#0077CC")
    hc = Gdk.RGBA(); hc.parse("#FFFFFF")
    st = dict(
        texture=tex, scale=(1.0, 1.0), origin_px=(0, 0),
        logical_w=surface.get_width(), logical_h=surface.get_height(),
        selection=[60, 50, 120, 80], region_path=None, mode="select",
        strokes=[], texts=[], _pending_points=[], _pending_pen=None,
        pointer=(0, 0), selected_text=None,
        _border_rgba=bc, _handle_rgba=hc,
        _border_faint_rgba=Gdk.RGBA(red=bc.red, green=bc.green, blue=bc.blue, alpha=0.55),
        _handle_strong_rgba=Gdk.RGBA(red=1, green=1, blue=1, alpha=0.9),
        _border_width=2.0, _dim=0.35,
        _dim_rgba=Gdk.RGBA(red=0, green=0, blue=0, alpha=0.35),
        _region_cache=None, _annot_cache=None, pango_layout=_pango_layout)
    st.update(over)
    ns = types.SimpleNamespace(**st)
    ns._readout_px = lambda: (ns.selection[2], ns.selection[3])
    return ns


def _walk_nodes(node, out):
    bnd = node.get_bounds()
    out.append((node.get_node_type().value_nick, bnd.origin.x, bnd.origin.y,
                bnd.size.width, bnd.size.height))
    if isinstance(node, Gsk.ContainerNode):
        for i in range(node.get_n_children()):
            _walk_nodes(node.get_child(i), out)


def _render_scene(st, w, h):
    """snapclip's GSK scene for `st`, rasterized by GTK's cairo renderer.
    Returns (BGRA bytes, stride, node list)."""
    snap = Gtk.Snapshot.new()
    sc.render_overlay(snap, st, w, h)
    node = snap.to_node()
    nodes = []
    _walk_nodes(node, nodes)
    renderer = Gsk.CairoRenderer.new()
    renderer.realize(None)
    tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    renderer.unrealize()
    dl = Gdk.TextureDownloader.new(tex)
    dl.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED)
    data, stride = dl.download_bytes()
    return bytes(data.get_data()), stride, nodes


def _reference_render(surface, st, w, h):
    """What OverlayWindow.on_draw painted with cairo before 1.3 (steps 1-5;
    the text badge is compared separately).  Returns (BGRA bytes, stride)."""
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.set_source_surface(surface, 0, 0)
    cr.paint()
    x, y, bw, bh = st.selection

    def region_or_rect():
        if st.region_path:
            cr.move_to(*st.region_path[0])
            for pt in st.region_path[1:]:
                cr.line_to(*pt)
            cr.close_path()
        else:
            cr.rectangle(x, y, bw, bh)

    if st._dim > 0:
        cr.set_source_rgba(0, 0, 0, st._dim)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(0, 0, w, h)
        region_or_rect()
        cr.fill()
    bc = st._border_rgba
    cr.set_line_width(st._border_width)
    cr.set_source_rgba(bc.red, bc.green, bc.blue, bc.alpha)
    if st.region_path:
        region_or_rect()
    else:
        cr.rectangle(x + 0.5, y + 0.5, bw, bh)
    cr.stroke()
    cr.set_fill_rule(cairo.FILL_RULE_WINDING)
    if st.region_path and st.mode == "select":
        cr.set_source_rgba(bc.red, bc.green, bc.blue, 0.55)
        cr.set_line_width(1.0)
        cr.set_dash([4, 4])
        cr.rectangle(x + 0.5, y + 0.5, bw, bh)
        cr.stroke()
        cr.set_dash([])
    if st.mode == "select":
        hc = st._handle_rgba
        cr.set_source_rgba(hc.red, hc.green, hc.blue, hc.alpha)
        for hx, hy in [(x, y), (x + bw, y), (x, y + bh), (x + bw, y + bh),
                       (x + bw / 2, y), (x + bw / 2, y + bh),
                       (x, y + bh / 2), (x + bw, y + bh / 2)]:
            cr.rectangle(hx - sc.HANDLE_DRAW, hy - sc.HANDLE_DRAW,
                         sc.HANDLE_DRAW * 2, sc.HANDLE_DRAW * 2)
        cr.fill()
    out.flush()
    return bytes(out.get_data()), out.get_stride()


def _compare(new, nstride, ref, rstride, w, h, mask):
    """Max per-channel difference outside `mask` (x, y, w, h) plus where it was."""
    worst = (0, None)
    for row in range(h):
        for col in range(w):
            if mask[0] <= col < mask[0] + mask[2] and mask[1] <= row < mask[1] + mask[3]:
                continue
            n = new[row * nstride + col * 4:row * nstride + col * 4 + 4]
            r = ref[row * rstride + col * 4:row * rstride + col * 4 + 4]
            d = max(abs(a - b) for a, b in zip(n, r))
            if d > worst[0]:
                worst = (d, (col, row, tuple(n), tuple(r)))
    return worst


_scene_surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 320, 240)
_scr = cairo.Context(_scene_surf)
_scr.set_source_surface(make_surface(320, 240), 0, 0); _scr.paint()
_scr.set_source_rgb(0.3, 0.6, 0.9); _scr.rectangle(100, 90, 60, 40); _scr.fill()
_scene_surf.flush()
_W, _H = 320, 240

for _label, _over in (
        ("rectangle selection", {}),
        ("rectangle, no dim", {"_dim": 0.0, "_dim_rgba": Gdk.RGBA(red=0, green=0, blue=0, alpha=0)}),
        ("rectangle, thick border", {"_border_width": 6.0}),
        ("rectangle at the screen corner", {"selection": [0, 40, 100, 60]}),
        ("freeform region (triangle)", {"region_path": [(70, 60), (180, 75), (110, 150)],
                                        "selection": [70, 60, 110, 90]}),
        ("region while a tool is active", {"region_path": [(70, 60), (180, 75), (110, 150)],
                                           "selection": [70, 60, 110, 90], "mode": "pen"}),
):
    st = _state(_scene_surf, **_over)
    new, nstride, nodes = _render_scene(st, _W, _H)
    ref, rstride = _reference_render(_scene_surf, st, _W, _H)
    x, y = st.selection[:2]
    badge_mask = (max(0, x - 2), max(0, y - 34), 140, 40)   # the readout box
    worst = _compare(new, nstride, ref, rstride, _W, _H, badge_mask)
    check(f"{_label}: GSK scene matches the cairo reference (max diff {worst[0]})",
          worst[0] <= 2, worst)
    sane = all(math.isfinite(v) for _, *vals in nodes for v in vals) and all(
        abs(bx) < 1e5 and abs(by) < 1e5 and 0 <= bw < 1e5 and 0 <= bh < 1e5
        for _, bx, by, bw, bh in nodes)
    check(f"{_label}: every render node has sane bounds", sane,
          [n for n in nodes if not all(math.isfinite(v) for v in n[1:])][:3])

# spot checks with exact expectations on the rectangle scene
st = _state(_scene_surf)
new, nstride, _ = _render_scene(st, _W, _H)
px = lambda c, r: tuple(new[r * nstride + c * 4:r * nstride + c * 4 + 4])
check("inside the selection shows the frozen screen 1:1 (red quadrant)",
      px(100, 80) == (0, 0, 255, 255), px(100, 80))
check("outside is the screen dimmed by 35%",
      px(20, 20) == (0, 0, round(255 * 0.65), 255), px(20, 20))
check("the border is drawn in the border colour",
      px(60, 70)[:3] == (0xCC, 0x77, 0x00), px(60, 70))   # (60,90) is a handle
check("the left-middle handle sits on the border", px(60, 90) == (255, 255, 255, 255), px(60, 90))
check("a handle square is drawn in the handle colour",
      px(60, 50) == (255, 255, 255, 255), px(60, 50))
_bx, _by = 60, 50 - 34
badge_px = [px(c, r) for r in range(_by, _by + 22) for c in range(_bx, _bx + 60)]
check("the size readout badge is drawn above the selection (dark box + text)",
      any(p[:3] == (255, 255, 255) for p in badge_px)
      and sum(1 for p in badge_px if p[:3] == (0, 0, round(255 * 0.65 * 0.35))) > 100,
      badge_px[:5])

# annotation preview: same cairo routine as the bake, cached as one node
st = _state(_scene_surf, strokes=[{"rgba": (1, 0, 1, 1), "width": 4.0,
                                  "points": [(20, 200), (80, 220)]}])
new, nstride, nodes = _render_scene(st, _W, _H)
check("pen stroke previews through a cairo node bounded to the stroke",
      any(n[0] == "cairo-node" and n[3] < 100 and n[4] < 60 for n in nodes),
      [n for n in nodes if n[0] == "cairo-node"])
check("stroke pixels appear dimmed outside the selection (drawn under the dim)",
      px(50, 210) == (round(255 * 0.65), 0, round(255 * 0.65), 255), px(50, 210))
first_node = st._annot_cache[1]
_render_scene(st, _W, _H)
check("annotation node is reused while annotations are unchanged",
      st._annot_cache[1] is first_node)
st.strokes.append({"rgba": (1, 0, 1, 1), "width": 4.0, "points": [(30, 30)]})
_render_scene(st, _W, _H)
check("annotation node is rebuilt when a stroke is added",
      st._annot_cache[1] is not first_node)

# ---------------------------------------------------------------------------
print("double-tap quick-save: config, timing predicates, headless encode")
# config: off by default, window clamped, wrong types coerced
check("quick_save_double_tap defaults off",
      sc.DEFAULT_CONFIG["quick_save_double_tap"] is False)
check("double_tap_ms default is 300", sc.DEFAULT_CONFIG["double_tap_ms"] == 300)
_qc = sc._sanitize_config({**sc.DEFAULT_CONFIG, "double_tap_ms": 99999})
check("double_tap_ms clamped high", _qc["double_tap_ms"] == 800, _qc["double_tap_ms"])
_qc = sc._sanitize_config({**sc.DEFAULT_CONFIG, "double_tap_ms": 5})
check("double_tap_ms clamped low", _qc["double_tap_ms"] == 120, _qc["double_tap_ms"])
_qc = sc._sanitize_config({**sc.DEFAULT_CONFIG, "double_tap_ms": "soon"})
check("bad double_tap_ms coerced to default", _qc["double_tap_ms"] == 300)
_qc = sc._sanitize_config({**sc.DEFAULT_CONFIG, "quick_save_double_tap": "yes"})
check("non-bool quick_save flag coerced", _qc["quick_save_double_tap"] is False)

# _is_double_tap: a second launch counts only inside the window after ours
_Wt = 300 * 1000
check("tap inside window is a double-tap", sc._is_double_tap(1000 + _Wt // 2, 1000, _Wt))
check("tap at the window edge still counts", sc._is_double_tap(1000 + _Wt, 1000, _Wt))
check("tap past the window does not count", not sc._is_double_tap(1000 + _Wt + 1, 1000, _Wt))
check("tap before our launch (stale) ignored", not sc._is_double_tap(500, 1000, _Wt))
check("no tap recorded is not a double-tap", not sc._is_double_tap(None, 1000, _Wt))

# _within_cooldown: the key-mash / bounce spam guard
_CD = 1_200_000
check("quick-save just now is within cooldown", sc._within_cooldown(1e4, 1e4 + _CD // 2, _CD))
check("quick-save long ago is outside cooldown", not sc._within_cooldown(1e4, 1e4 + _CD + 1, _CD))
check("no prior quick-save is not in cooldown", not sc._within_cooldown(None, 1e4, _CD))

# surface_to_png_bytes: encodes the WHOLE frame (the full-screen quick-save)
_fs = make_surface(120, 80)
check("full-frame encode is a valid PNG of the whole surface",
      png_size(sc.surface_to_png_bytes(_fs)) == (120, 80),
      png_size(sc.surface_to_png_bytes(_fs)))

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
print("launch helpers (argument parsing, PATH lookup, signal wait, launcher)")
_a = sc._parse_args([])
check("no arguments -> interactive defaults without argparse",
      _a.self_test is None and _a.allow_flash is False and _a.monitor is None and _a.list_monitors is False)
_a = sc._parse_args(["--allow-flash", "--self-test", "save"])
check("flags parsed", _a.self_test == "save" and _a.allow_flash is True)
_am = sc._parse_args(["-m", "1"])
check("-m flag parsed", _am.monitor == "1")
_am_long = sc._parse_args(["--monitor", "HDMI-1"])
check("--monitor flag parsed", _am_long.monitor == "HDMI-1")
_al = sc._parse_args(["-l"])
check("-l flag parsed", _al.list_monitors is True)
_al_long = sc._parse_args(["--list-monitors"])
check("--list-monitors flag parsed", _al_long.list_monitors is True)
import shutil  # noqa: E402
check("_which agrees with shutil.which", sc._which("wl-copy") == shutil.which("wl-copy"))
check("_which misses a bogus command", sc._which("snapclip-no-such-tool-xyz") is None)
_sw = sc._SignalWait(5)
GLib.idle_add(lambda: (_sw.quit(), False)[1])
check("_SignalWait returns True when quit() is called", _sw.run() is True and not _sw.timed_out)
_sw2 = sc._SignalWait(1)
_t0 = time.monotonic()
check("_SignalWait times out on its own", _sw2.run() is False and _sw2.timed_out
      and time.monotonic() - _t0 < 3.5, f"{time.monotonic() - _t0:.2f}s")
_launch = subprocess.run([sys.executable, os.path.join(HERE, "snapclip"), "--version"],
                         capture_output=True, text=True)
check("the `snapclip` launcher runs the module", _launch.stdout.strip() == sc.VERSION,
      (_launch.stdout + _launch.stderr).strip())
check("launcher import is bytecode-cached",
      any(f.startswith("snapclip.") and f.endswith(".pyc")
          for f in os.listdir(os.path.join(HERE, "__pycache__"))))

# ---------------------------------------------------------------------------
print("clipboard round-trip (no temp file)")
surf6 = make_surface(400, 300)
png = sc.crop_to_png_bytes(surf6, [0, 0, 120, 80], (1.0, 1.0))
sc.copy_png_to_clipboard(png)
time.sleep(0.3)
types_ = subprocess.run(["wl-paste", "--list-types"],
                        capture_output=True, text=True).stdout
check("clipboard offers image/png", "image/png" in types_, types_.strip())
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
    pn = sc.save_png(b"x", td, "now-%H%M%S.png")
    check("save without an explicit time stamps with the current time",
          os.path.exists(pn) and os.path.basename(pn).startswith("now-"), pn)

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
        # default_monitor config
        check("default_monitor defaults to primary", sc.DEFAULT_CONFIG["default_monitor"] == "primary")
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"default_monitor": 123}, fh)
        cfg_m = sc.load_config()
        check("int default_monitor coerced to string", cfg_m["default_monitor"] == "123")
        with open(sc.CONFIG_PATH, "w") as fh:
            json.dump({"default_monitor": None}, fh)
        cfg_m2 = sc.load_config()
        check("null default_monitor coerced to default", cfg_m2["default_monitor"] == "primary")
finally:
    sc.CONFIG_PATH = orig

# ---------------------------------------------------------------------------
print("primary monitor geometry (DisplayConfig -> the RecordArea rectangle)")


class _FakeBus:
    """Stands in for Gio.DBusConnection: `reply` is returned from call_sync
    (an object with .unpack()), or raised if it is an exception; every call
    is logged as (method, unpacked parameters)."""

    def __init__(self, reply=None, replies=None):
        self.reply = reply
        self.replies = dict(replies or {})    # method -> reply or exception
        self.calls = []

    def call_sync(self, _name, _path, _iface, method, params, *_rest):
        self.calls.append((method, params.unpack() if params is not None
                           else None))
        reply = self.replies.get(method, self.reply)
        if isinstance(reply, BaseException):
            raise reply
        return types.SimpleNamespace(unpack=lambda: reply)


def _state(logical, monitors, props=None):
    # Shape of org.gnome.Mutter.DisplayConfig.GetCurrentState, unpacked.
    return (1, monitors, logical, {"layout-mode": 1} if props is None else props)


def _mon(connector, w, h, current=True):
    return ((connector, "VEN", "PRD", "SER"),
            [("%dx%d@60" % (w, h), w, h, 60.0, 1.0, [1.0],
              {"is-current": True} if current else {})],
            {})


def _lm(x, y, scale, transform, primary, connector):
    return (x, y, scale, transform, primary, [(connector, "VEN", "PRD", "SER")], {})


conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 1.0, 0, True, "HDMI-1")], [_mon("HDMI-1", 1920, 1080)])))
check("plain 1080p monitor -> its logical rect",
      (conn, rect) == ("HDMI-1", (0, 0, 1920, 1080)), (conn, rect))

conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 1.0, 0, False, "DP-1"), _lm(1920, 0, 1.25, 0, True, "DP-2")],
    [_mon("DP-1", 1920, 1080), _mon("DP-2", 2560, 1440)])))
check("the PRIMARY logical monitor is chosen, not the first",
      conn == "DP-2", conn)
check("fractional scale: logical size = round(px / scale) at the layout offset",
      rect == (1920, 0, 2048, 1152), rect)

conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 2.0, 1, True, "eDP-1")], [_mon("eDP-1", 1920, 1080)])))
check("rotated output (transform 90) swaps width/height before scaling",
      rect == (0, 0, 540, 960), rect)

conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 2.0, 0, True, "eDP-1")], [_mon("eDP-1", 1920, 1080)],
    props={"layout-mode": 2})))
check("physical layout mode keeps pixel dimensions",
      rect == (0, 0, 1920, 1080), rect)

conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 1.0, 0, True, "HDMI-1")], [_mon("HDMI-1", 1920, 1080,
                                              current=False)])))
check("no current mode -> connector still known, rect None (RecordMonitor "
      "fallback)", (conn, rect) == ("HDMI-1", None), (conn, rect))

conn, rect = sc._primary_monitor(_FakeBus(_state(
    [_lm(0, 0, 1.0, 0, False, "HDMI-1")], [_mon("HDMI-1", 1920, 1080)])))
check("no primary flagged -> the first logical monitor",
      (conn, rect) == ("HDMI-1", (0, 0, 1920, 1080)), (conn, rect))

for label, bus in (("no monitors at all", _FakeBus(_state([], []))),
                   ("DisplayConfig unreachable",
                    _FakeBus(GLib.Error("simulated D-Bus failure")))):
    raised = False
    try:
        sc._primary_monitor(bus)
    except sc.CaptureError:
        raised = True
    check(f"{label} -> CaptureError", raised)

_gdk_display = Gdk.Display.get_default()
if _gdk_display is not None:
    from gi.repository import Gio as _Gio
    conn, rect = sc._primary_monitor(_Gio.bus_get_sync(_Gio.BusType.SESSION, None))
    _mons = _gdk_display.get_monitors()
    _geo = None
    for _i in range(_mons.get_n_items()):
        _m = _mons.get_item(_i)
        if _m.get_connector() == conn:
            _g = _m.get_geometry()
            _geo = (_g.x, _g.y, _g.width, _g.height)
    check("live: the derived rect equals GDK's geometry for that monitor "
          "(the overlay and the capture agree on the logical size)",
          _geo is not None and rect == _geo, f"derived={rect} gdk={_geo}")
else:
    skip("live rect vs GDK geometry", "no GDK display")

print("stream selection: RecordArea first (works over fullscreen/direct-scanout "
      "apps), RecordMonitor as the fallback")
bus = _FakeBus(reply=("/stream/1",))
path = sc._record_primary(bus, "/session/1", "HDMI-1", (0, 0, 1920, 1080))
check("with a rect the monitor is recorded as an AREA stream",
      path == "/stream/1" and [c[0] for c in bus.calls] == ["RecordArea"],
      bus.calls)
check("RecordArea gets the logical rect and a hidden cursor",
      bus.calls and bus.calls[0][1] == (0, 0, 1920, 1080, {"cursor-mode": 0}),
      bus.calls)

bus = _FakeBus(reply=("/stream/2",))
path = sc._record_primary(bus, "/session/1", "HDMI-1", None)
check("without a rect it records the monitor by connector",
      path == "/stream/2" and bus.calls == [("RecordMonitor",
                                             ("HDMI-1", {"cursor-mode": 0}))],
      bus.calls)

bus = _FakeBus(reply=("/stream/3",),
               replies={"RecordArea": GLib.Error("simulated: no RecordArea")})
import io as _io
import contextlib as _contextlib
_err = _io.StringIO()
with _contextlib.redirect_stderr(_err):
    path = sc._record_primary(bus, "/session/1", "HDMI-1", (0, 0, 1920, 1080))
check("a RecordArea D-Bus error falls back to RecordMonitor (same session)",
      path == "/stream/3" and [c[0] for c in bus.calls] ==
      ["RecordArea", "RecordMonitor"], bus.calls)
check("...and says so on stderr", "RecordArea failed" in _err.getvalue(),
      _err.getvalue())
print("multi-monitor discovery & target resolution (deterministic, two fake "
      "monitors)")
_two = _FakeBus(_state(
    [_lm(2560, 0, 1.0, 0, False, "HDMI-1"), _lm(0, 0, 2.0, 0, True, "eDP-1")],
    [_mon("HDMI-1", 1920, 1080), _mon("eDP-1", 2560, 1600)]))
_mons = sc._list_monitors(_two)
check("monitors are indexed left-to-right by layout position, not D-Bus order",
      [m["connector"] for m in _mons] == ["eDP-1", "HDMI-1"]
      and [m["index"] for m in _mons] == [0, 1], _mons)
check("each monitor carries its logical rect and physical mode",
      _mons[0]["rect"] == (0, 0, 1280, 800) and (_mons[0]["width"],
      _mons[0]["height"]) == (2560, 1600) and _mons[1]["rect"] == (2560, 0, 1920, 1080),
      _mons)
check("display name falls back to the connector", _mons[0]["name"] == "eDP-1")
_r = lambda t: sc._resolve_target_monitor(_two, t)["connector"]
check("None / 'primary' / '' -> the primary", {_r(None), _r("primary"), _r(""),
                                                 _r("PRIMARY")} == {"eDP-1"})
check("index as int or string", (_r(1), _r("1"), _r(0)) == ("HDMI-1", "HDMI-1", "eDP-1"))
check("connector name, case-insensitive", (_r("hdmi-1"), _r("EDP-1")) == ("HDMI-1", "eDP-1"))
check("display-name substring, case-insensitive", _r("hdmi") == "HDMI-1")
_err = _io.StringIO()
with _contextlib.redirect_stderr(_err):
    _fb = (_r("NOPE"), _r("7"), _r("-9"))
check("unknown target / out-of-range index -> primary, with a stderr notice",
      _fb == ("eDP-1",) * 3 and _err.getvalue().count("not found") == 3,
      (_fb, _err.getvalue()))
check("the D-Bus-derived list agrees with the -l printout",
      sc._print_monitors(_two) == 0)
try:
    sc._list_monitors(_FakeBus(_state([], [])))
    check("empty monitor list -> CaptureError", False)
except sc.CaptureError:
    check("empty monitor list -> CaptureError", True)
check("-l with DisplayConfig unreachable exits 2",
      sc._print_monitors(_FakeBus(GLib.Error("simulated"))) == 2)

print("multi-monitor discovery (live)")
try:
    monitors = sc._list_monitors()
    check("monitors discovered", len(monitors) >= 1)
    check("every monitor has a logical rect", all(m["rect"] for m in monitors), monitors)
    check("'primary' resolves to the flagged primary",
          sc._resolve_target_monitor(target="primary")["primary"] is True)
    check("connector round-trips",
          sc._resolve_target_monitor(target=monitors[0]["connector"]) is not None)
    check("the primary's rect matches _primary_monitor()",
          sc._primary_monitor(_Gio.bus_get_sync(_Gio.BusType.SESSION, None))
          == (sc._resolve_target_monitor(target="primary")["connector"],
              sc._resolve_target_monitor(target="primary")["rect"]))
except sc.CaptureError as exc:
    check("live monitor discovery", False, str(exc))

# ---------------------------------------------------------------------------
print("ScreenCast capture (flash-free, primary path)")
pics_before = set(os.listdir(PICTURES)) if os.path.isdir(PICTURES) else set()
_orig_record = sc._record_primary
_record_seen = {}


def _spy_record(bus, session, connector, rect):
    _record_seen["rect"] = rect
    return _orig_record(bus, session, connector, rect)


sc._record_primary = _spy_record
_err = _io.StringIO()
try:
    with _contextlib.redirect_stderr(_err):
        sc_surf, sc_conn = sc.screencast_capture()
    check("screencast returned a surface", isinstance(sc_surf, cairo.ImageSurface))
    check("live capture went through RecordArea (rect known, no fallback)",
          _record_seen.get("rect") is not None
          and "RecordArea failed" not in _err.getvalue(),
          f"rect={_record_seen.get('rect')} stderr={_err.getvalue()!r}")
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
finally:
    sc._record_primary = _orig_record

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

print("capture hooks + capture from a worker thread on a private GLib context "
      "(how the app overlaps capture with GTK setup)")
_events = []


def _capture_worker():
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        res = sc.capture_screen(
            on_connector=lambda c: _events.append(("connector", c)),
            on_frame=lambda s, fd, c: _events.append(("frame", s.get_width(), fd, c)))
        _events.append(("done", res[2], res[1]))
    except sc.CaptureError as exc:
        _events.append(("error", str(exc)))
    finally:
        ctx.pop_thread_default()


_th = threading.Thread(target=_capture_worker, daemon=True)
_th.start()
_th.join(30)
check("worker-thread capture finished", not _th.is_alive() and _events
      and _events[-1][0] == "done", _events)
_kinds = [e[0] for e in _events]
check("on_connector fires first, on_frame before the return",
      _kinds == ["connector", "frame", "done"], _kinds)
check("on_frame carries the same connector as the return, full_desktop=False",
      len(_events) == 3 and _events[1][2] is False
      and _events[1][3] == _events[0][1] == _events[2][1], _events)

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
    try:
        surf_fb, full_fb, conn_fb = sc.capture_screen(allow_flash=True)  # -> portal
        check("capture_screen(allow_flash=True) falls back to the portal",
              isinstance(surf_fb, cairo.ImageSurface) and full_fb is True,
              f"full_desktop={full_fb}")
        check("portal fallback reports no connector (full-desktop capture)",
              conn_fb is None, conn_fb)
    except sc.CaptureError as exc:
        # The portal itself may refuse (xdg-desktop-portal can require a
        # permission this session cannot grant).  The fallback still ROUTED to
        # the portal — that is what this test guards — so only the portal's own
        # answer is tolerated here.
        check("capture_screen(allow_flash=True) routed to the portal",
              "flash-free capture" not in str(exc), str(exc))
        skip("portal fallback capture", f"portal refused: {exc}")
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
    if "cancelled or denied" in str(exc):
        skip("portal capture", f"the portal refused this session: {exc}")
    else:
        check("portal capture", False, f"CaptureError: {exc}")

# ---------------------------------------------------------------------------
print()
print(f"=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
