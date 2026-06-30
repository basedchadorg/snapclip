#!/usr/bin/env python3
"""
snapclip — a minimal region-screenshot tool for Ubuntu / GNOME / Wayland.

Architecture (why it works on GNOME 50 Wayland):

  * Capturing the screen is done ONCE at launch through the XDG Desktop Portal
    (org.freedesktop.portal.Screenshot).  On GNOME 50 the older
    org.gnome.Shell.Screenshot D-Bus interface returns "AccessDenied", and
    grim fails because Mutter has no wlr-screencopy — the portal is the only
    supported path.

  * That full-screen capture is shown FROZEN inside a single full-screen,
    undecorated GTK4 window.  The selection rectangle is drawn *as graphics*
    inside that fixed surface (never an OS window that gets moved), which
    sidesteps Wayland's "an app may not position its own window" restriction.

  * On confirm we crop the *cached* capture (taken before the overlay existed),
    so the selection border can never appear in the result, and no second
    capture / no flash is needed.

  * The crop is copied to the clipboard with `wl-copy --type image/png` fed
    over stdin (no temp file is ever written for copy-only).  Save additionally
    writes a timestamped PNG.  The portal's own full-screen dump is always
    deleted so nothing is left behind.

Keys:  Enter = copy   •   S = save+copy   •   Esc = cancel
"""

import argparse
import fcntl
import io
import os
import subprocess
import sys
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

import cairo  # noqa: E402  (needs python3-gi-cairo / pycairo)

APP_ID = "dev.snapclip.SnapClip"

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

CONFIG_DIR = os.path.join(
    GLib.get_user_config_dir() or os.path.expanduser("~/.config"), "snapclip"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

LOCK_PATH = os.path.join(
    GLib.get_user_runtime_dir() or "/tmp", "snapclip.lock"
)


ALREADY_RUNNING = object()   # sentinel: another instance holds the lock


def acquire_single_instance_lock():
    """Try to become the single running snapclip instance.

    Returns one of:
      * an open file object  -> we hold the lock (keep it referenced),
      * ALREADY_RUNNING      -> another overlay is open; the caller should exit,
      * None                 -> the lock file could not even be created; proceed
                                WITHOUT the guard rather than refuse to run.

    A screenshot overlay must be a singleton (a second hotkey press must not
    stack another full-screen overlay), but a missing/unwritable runtime dir
    must NOT make the tool silently no-op — that's far worse than allowing a
    rare stacked overlay. The lock releases automatically on exit (even on
    crash, since flock is tied to the open file description).
    """
    try:
        fp = open(LOCK_PATH, "w")
    except OSError as exc:
        print(f"snapclip: could not create lock file ({exc}); "
              f"continuing without the single-instance guard", file=sys.stderr)
        return None
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return ALREADY_RUNNING
    return fp

DEFAULT_CONFIG = {
    "border_color": "#00A3FF",      # selection outline colour
    "border_width": 2,               # outline thickness (px)
    "handle_color": "#FFFFFF",       # corner/edge handle colour
    "dim_opacity": 0.35,             # darkening applied OUTSIDE the selection
    "include_cursor": False,         # best-effort: composite a pointer glyph
    "remember_selection": False,     # reopen with the last selection (off by
                                     # default: most people want a fresh box)
    "last_selection": None,          # [x, y, w, h] in logical px
    "default_size_pct": 0.4,         # fallback box = this fraction of the
                                     # screen, centred (used when not remembering)
    "save_dir": "~/Pictures/Screenshots",
    "filename_format": "snapclip-%Y-%m-%d_%H-%M-%S.png",
}


def load_config():
    import json

    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as exc:  # corrupt config must never break the tool
        print(f"snapclip: ignoring bad config ({exc})", file=sys.stderr)
    return _sanitize_config(cfg)


def _sanitize_config(cfg):
    """Coerce wrong-typed / null fields back to safe defaults so a hand-edited
    or partially-written config can never crash capture or save."""
    for key in ("save_dir", "filename_format", "border_color", "handle_color"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            cfg[key] = DEFAULT_CONFIG[key]
    # Colours must actually parse, else Gdk.RGBA.parse() leaves garbage.
    for key in ("border_color", "handle_color"):
        if not Gdk.RGBA().parse(cfg[key]):
            cfg[key] = DEFAULT_CONFIG[key]
    for key in ("border_width", "dim_opacity", "default_size_pct"):
        if not isinstance(cfg.get(key), (int, float)) or isinstance(cfg.get(key), bool):
            cfg[key] = DEFAULT_CONFIG[key]
    for key in ("include_cursor", "remember_selection"):
        if not isinstance(cfg.get(key), bool):
            cfg[key] = DEFAULT_CONFIG[key]
    sel = cfg.get("last_selection")
    if sel is not None and not (isinstance(sel, list) and len(sel) == 4
                                and all(isinstance(v, (int, float)) for v in sel)):
        cfg["last_selection"] = None
    return cfg


def save_config(cfg):
    import json

    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as exc:
        print(f"snapclip: could not save config ({exc})", file=sys.stderr)


# ----------------------------------------------------------------------------
# Core pipeline (no GUI — unit-testable on its own)
# ----------------------------------------------------------------------------


class CaptureError(RuntimeError):
    pass


def portal_capture(timeout_s=30):
    """Capture the whole screen via the XDG Desktop Portal.

    Returns a cairo.ImageSurface (physical pixels).  The portal's own PNG file
    is deleted before returning so nothing is left on disk.
    Raises CaptureError on failure / denial / timeout.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise CaptureError(f"cannot reach the session bus: {exc}")
    unique = bus.get_unique_name().lstrip(":").replace(".", "_")
    token = "snapclip%d" % int(time.time() * 1000 % 1_000_000)
    request_path = f"/org/freedesktop/portal/desktop/request/{unique}/{token}"

    loop = GLib.MainLoop()
    state = {}

    def on_response(_c, _s, _o, _i, _sig, params):
        code, results = params.unpack()
        state["code"] = code
        state["results"] = results
        loop.quit()

    sub = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop",
        "org.freedesktop.portal.Request",
        "Response",
        request_path,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    try:
        opts = {
            "interactive": GLib.Variant("b", False),
            "handle_token": GLib.Variant("s", token),
        }
        try:
            bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                GLib.Variant("(sa{sv})", ("", opts)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            raise CaptureError(f"screenshot portal call failed: {exc}")

        def _timed_out():
            state["timeout"] = True
            loop.quit()
            return False

        tid = GLib.timeout_add_seconds(timeout_s, _timed_out)
        loop.run()
        if not state.get("timeout"):
            GLib.source_remove(tid)
    finally:
        bus.signal_unsubscribe(sub)

    if state.get("timeout"):
        # Cancel the outstanding request so the portal does not write its PNG
        # to disk after we have given up (would leave a file behind).
        try:
            bus.call_sync(
                "org.freedesktop.portal.Desktop", request_path,
                "org.freedesktop.portal.Request", "Close",
                None, None, Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error:
            pass
        raise CaptureError("screenshot portal timed out (permission not granted?)")
    if state.get("code") != 0:
        raise CaptureError("screenshot was cancelled or denied")
    uri = (state.get("results") or {}).get("uri")
    if not uri:
        raise CaptureError("portal returned no image")

    path = uri[7:] if uri.startswith("file://") else uri
    path = GLib.uri_unescape_string(path, None) or path
    try:
        surface = cairo.ImageSurface.create_from_png(path)
    except Exception as exc:
        raise CaptureError(f"could not read captured image: {exc}")
    finally:
        # Never leave the portal's full-screen dump behind.
        try:
            os.unlink(path)
        except OSError:
            pass
    return surface


def _primary_connector(bus):
    """Connector name (e.g. 'HDMI-1') of the primary monitor, via Mutter."""
    try:
        r = bus.call_sync(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )
        _serial, _monitors, logical, _props = r.unpack()
        for lm in logical:
            # lm = (x, y, scale, transform, primary, [(connector, ...)], props)
            if lm[4] and lm[5]:
                return lm[5][0][0]
        if logical and logical[0][5]:
            return logical[0][5][0][0]
    except (GLib.Error, IndexError, ValueError, TypeError) as exc:
        raise CaptureError(f"could not determine the primary monitor: {exc}")
    raise CaptureError("no monitor reported by Mutter")


def _sample_to_surface(sample):
    """Convert a GStreamer BGRx sample into an owned cairo RGB24 surface."""
    from gi.repository import Gst

    caps = sample.get_caps().get_structure(0)
    w, h = caps.get_value("width"), caps.get_value("height")
    if not w or not h:
        raise CaptureError("captured frame has no dimensions")
    buf = sample.get_buffer()

    # Use the buffer's real row stride when the allocator reports one (rows can
    # be padded to an alignment); only guess from the length as a last resort.
    gstride = None
    try:
        gi.require_version("GstVideo", "1.0")
        from gi.repository import GstVideo
        vmeta = GstVideo.buffer_get_video_meta(buf)
        if vmeta is not None and vmeta.stride:
            gstride = vmeta.stride[0]
    except Exception:
        gstride = None

    ok, minfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        raise CaptureError("could not map the captured frame")
    try:
        if not gstride:
            gstride = len(minfo.data) // h
        cstride = cairo.ImageSurface.format_stride_for_width(
            cairo.FORMAT_RGB24, w)
        data = bytes(minfo.data)
        row_bytes = min(gstride, cstride)
        packed = bytearray(cstride * h)
        for row in range(h):
            packed[row * cstride:row * cstride + row_bytes] = \
                data[row * gstride:row * gstride + row_bytes]
    finally:
        buf.unmap(minfo)

    src = cairo.ImageSurface.create_for_data(
        packed, cairo.FORMAT_RGB24, w, h, cstride)
    owned = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(owned)
    cr.set_source_surface(src, 0, 0)
    cr.paint()
    owned.flush()
    src.finish()
    return owned


def screencast_capture(timeout_s=10):
    """Grab one frame of the primary monitor via Mutter ScreenCast + PipeWire.

    Returns a cairo.ImageSurface (physical pixels of that monitor).  Unlike the
    screenshot portal this does NOT play GNOME's shutter flash and shows no
    picker dialog.  Raises CaptureError if ScreenCast/GStreamer is unavailable.
    """
    try:
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ValueError, ImportError) as exc:
        raise CaptureError(f"GStreamer not available: {exc}")
    if not Gst.is_initialized():
        Gst.init(None)

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise CaptureError(f"cannot reach the session bus: {exc}")

    connector = _primary_connector(bus)
    session = None
    pipeline = None
    try:
        try:
            r = bus.call_sync(
                "org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast",
                "org.gnome.Mutter.ScreenCast", "CreateSession",
                GLib.Variant("(a{sv})", ({},)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            session = r.unpack()[0]
            r = bus.call_sync(
                "org.gnome.Mutter.ScreenCast", session,
                "org.gnome.Mutter.ScreenCast.Session", "RecordMonitor",
                GLib.Variant("(sa{sv})",
                             (connector, {"cursor-mode": GLib.Variant("u", 0)})),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            stream = r.unpack()[0]
        except GLib.Error as exc:
            raise CaptureError(f"Mutter ScreenCast setup failed: {exc}")

        state = {}
        loop = GLib.MainLoop()

        def on_added(_c, _s, _o, _i, _sig, params):
            state["node"] = params.unpack()[0]
            loop.quit()

        sub = bus.signal_subscribe(
            "org.gnome.Mutter.ScreenCast", "org.gnome.Mutter.ScreenCast.Stream",
            "PipeWireStreamAdded", stream, None, Gio.DBusSignalFlags.NONE, on_added)
        try:
            bus.call_sync(
                "org.gnome.Mutter.ScreenCast", session,
                "org.gnome.Mutter.ScreenCast.Session", "Start",
                None, None, Gio.DBusCallFlags.NONE, -1, None)
            def _timed_out():
                state["timed_out"] = True
                loop.quit()
                return False
            tid = GLib.timeout_add_seconds(timeout_s, _timed_out)
            loop.run()
            if not state.get("timed_out"):
                GLib.source_remove(tid)
        except GLib.Error as exc:
            raise CaptureError(f"Mutter ScreenCast start failed: {exc}")
        finally:
            bus.signal_unsubscribe(sub)

        node = state.get("node")
        if node is None:
            raise CaptureError("ScreenCast produced no PipeWire stream")

        # Everything GStreamer is funnelled into CaptureError so capture_screen
        # can act on it (error with guidance, or --allow-flash -> portal). The
        # most common real failure here is a missing 'gstreamer1.0-pipewire'
        # plugin, which makes Gst.parse_launch raise a raw GLib.Error.
        try:
            pipeline = Gst.parse_launch(
                "pipewiresrc path=%d num-buffers=1 ! videoconvert ! "
                "video/x-raw,format=BGRx ! appsink name=sink max-buffers=1 "
                "drop=false sync=false" % int(node))
            sink = pipeline.get_by_name("sink")
            if sink is None:
                raise CaptureError("no appsink (gstreamer1.0-plugins-base?)")
            if pipeline.set_state(Gst.State.PLAYING) == \
                    Gst.StateChangeReturn.FAILURE:
                raise CaptureError("GStreamer pipeline refused to start")
            sample = sink.emit("try-pull-sample", Gst.SECOND * timeout_s)
            if sample is None:
                raise CaptureError("ScreenCast delivered no frame")
            return _sample_to_surface(sample)
        except CaptureError:
            raise
        except Exception as exc:   # GLib.Error (missing plugin) and anything else
            raise CaptureError(
                f"GStreamer capture failed (is gstreamer1.0-pipewire "
                f"installed?): {exc}")
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if session is not None:
            try:
                bus.call_sync(
                    "org.gnome.Mutter.ScreenCast", session,
                    "org.gnome.Mutter.ScreenCast.Session", "Stop",
                    None, None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass


def capture_screen(allow_flash=False):
    """Capture the primary monitor, flash-free.

    Uses Mutter ScreenCast, which does not flash.  If that is unavailable we
    REFUSE to capture by default rather than silently flashing — the whole
    point of this tool is no flash, so falling back to a flashing capture would
    defeat it.  Pass allow_flash=True to deliberately opt into the (flashing)
    screenshot portal anyway.

    Returns (surface, full_desktop).  full_desktop is False for the per-monitor
    ScreenCast capture and True only for the opt-in portal path.
    """
    try:
        return screencast_capture(), False
    except CaptureError as exc:
        if not allow_flash:
            raise CaptureError(
                "flash-free capture via Mutter ScreenCast is unavailable "
                f"({exc}).\n"
                "  Install it with:  sudo apt install gstreamer1.0-pipewire "
                "gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base\n"
                "  Or re-run with --allow-flash to use the screenshot portal "
                "instead (it triggers GNOME's screenshot flash).")
        print("snapclip: --allow-flash set; using the screenshot portal, which "
              "flashes", file=sys.stderr)
        return portal_capture(), True


def compute_scale(surface_w, surface_h, logical_w, logical_h):
    """Physical-pixels-per-logical-pixel for each axis.

    Handles fractional scaling: a selection in logical coords is multiplied by
    this to index the physical-resolution capture.
    """
    sx = surface_w / logical_w if logical_w else 1.0
    sy = surface_h / logical_h if logical_h else 1.0
    return sx, sy


def crop_to_png_bytes(surface, sel, scale, origin_px=(0, 0), cursor=None):
    """Crop `surface` to the logical-coord selection `sel` = (x, y, w, h).

    `scale` = (sx, sy) physical-pixels-per-logical-pixel for the monitor being
    captured.  `origin_px` is the captured monitor's top-left position inside
    the (possibly multi-monitor) capture, in PHYSICAL pixels.  `cursor`, if
    given, is a logical (x, y) where a pointer glyph is composited (only if it
    falls inside the selection).
    Returns PNG bytes.  Never touches disk.
    """
    sx, sy = scale
    ox, oy = origin_px
    x, y, w, h = sel
    # Round each edge to a physical pixel, then take the difference, so the
    # crop size is stable under fractional scaling (no independent-rounding
    # off-by-one between position and size).
    pleft = int(round(ox + x * sx))
    ptop = int(round(oy + y * sy))
    pright = int(round(ox + (x + w) * sx))
    pbottom = int(round(oy + (y + h) * sy))
    pw = max(1, pright - pleft)
    ph = max(1, pbottom - ptop)
    # Clamp to the capture bounds.
    sw, sh = surface.get_width(), surface.get_height()
    px = max(0, min(pleft, sw - 1))
    py = max(0, min(ptop, sh - 1))
    pw = min(pw, sw - px)
    ph = min(ph, sh - py)

    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    cr = cairo.Context(out)
    cr.set_source_surface(surface, -px, -py)
    cr.get_source().set_filter(cairo.FILTER_NEAREST)  # exact pixels, no blur
    cr.paint()

    if cursor is not None:
        cxl, cyl = cursor
        if x <= cxl <= x + w and y <= cyl <= y + h:
            _draw_pointer(cr, (ox + cxl * sx) - px, (oy + cyl * sy) - py)

    out.flush()
    buf = io.BytesIO()
    out.write_to_png(buf)
    return buf.getvalue()


def _draw_pointer(cr, x, y):
    """Draw a standard white arrow-pointer glyph (best-effort cursor compositing)."""
    cr.save()
    cr.translate(x, y)
    cr.move_to(0, 0)
    for dx, dy in [(0, 16), (4, 12), (7, 18), (9, 17), (6, 11), (11, 11)]:
        cr.line_to(dx, dy)
    cr.close_path()
    cr.set_source_rgb(1, 1, 1)
    cr.fill_preserve()
    cr.set_source_rgb(0, 0, 0)
    cr.set_line_width(1.0)
    cr.stroke()
    cr.restore()


def copy_png_to_clipboard(png_bytes):
    """Put PNG bytes on the Wayland clipboard as image/png.

    `wl-copy` reads all of stdin, then double-forks a daemon that keeps serving
    the data after we exit, so the paste survives the app closing.  No temp
    file is used.

    NOTE: stdout/stderr go to /dev/null (not pipes): the daemon child inherits
    those fds, so a captured pipe would never reach EOF and `.wait()` would
    hang forever.  We rely on the parent's exit code instead.
    """
    try:
        proc = subprocess.Popen(
            ["wl-copy", "--type", "image/png"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError("wl-copy not found — install the 'wl-clipboard' package")
    try:
        proc.stdin.write(png_bytes)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("wl-copy timed out")
    if rc != 0:
        raise RuntimeError(f"wl-copy exited with status {rc}")


def save_png(png_bytes, save_dir, filename_format, when=None):
    """Write PNG bytes to a timestamped file under `save_dir`. Returns path.

    `filename_format` may include subdirectories (e.g. '%Y/%m/shot-%H%M%S.png'),
    which are created; but it can never escape `save_dir` — an absolute path or
    '..' that would land outside falls back to the basename inside save_dir.
    """
    when = when or datetime.now()
    folder = os.path.abspath(os.path.expanduser(save_dir))
    name = when.strftime(filename_format)
    path = os.path.normpath(os.path.join(folder, name))
    if path != folder and not path.startswith(folder + os.sep):
        # escaped save_dir (absolute path / '..') -> keep just the file name
        path = os.path.join(folder, os.path.basename(name) or "snapclip.png")
    os.makedirs(os.path.dirname(path) or folder, exist_ok=True)
    # Avoid clobbering if two shots land in the same second.
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base}-{n}{ext}"
        n += 1
    with open(path, "wb") as fh:
        fh.write(png_bytes)
    return path


# ----------------------------------------------------------------------------
# Geometry helpers for the interactive selection
# ----------------------------------------------------------------------------

HANDLE = 14          # px hit radius for edge/corner grab zones
MIN_SIZE = 8         # smallest allowed selection
HANDLE_DRAW = 4      # half-size of the drawn handle squares

# zone identifiers
Z_OUTSIDE, Z_INSIDE = "outside", "inside"
Z_N, Z_S, Z_E, Z_W = "n", "s", "e", "w"
Z_NW, Z_NE, Z_SW, Z_SE = "nw", "ne", "sw", "se"

CURSOR_FOR_ZONE = {
    Z_INSIDE: "move",
    Z_OUTSIDE: "crosshair",
    Z_N: "ns-resize", Z_S: "ns-resize",
    Z_E: "ew-resize", Z_W: "ew-resize",
    Z_NW: "nwse-resize", Z_SE: "nwse-resize",
    Z_NE: "nesw-resize", Z_SW: "nesw-resize",
}


def hit_zone(sel, mx, my, m=HANDLE):
    x, y, w, h = sel
    near_l = abs(mx - x) <= m
    near_r = abs(mx - (x + w)) <= m
    near_t = abs(my - y) <= m
    near_b = abs(my - (y + h)) <= m
    within_x = x - m <= mx <= x + w + m
    within_y = y - m <= my <= y + h + m
    if within_x and within_y:
        if near_t and near_l:
            return Z_NW
        if near_t and near_r:
            return Z_NE
        if near_b and near_l:
            return Z_SW
        if near_b and near_r:
            return Z_SE
        if near_t and x <= mx <= x + w:
            return Z_N
        if near_b and x <= mx <= x + w:
            return Z_S
        if near_l and y <= my <= y + h:
            return Z_W
        if near_r and y <= my <= y + h:
            return Z_E
    if x <= mx <= x + w and y <= my <= y + h:
        return Z_INSIDE
    return Z_OUTSIDE


def normalize(sel):
    x, y, w, h = sel
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    return [x, y, w, h]


def resize_rect(origin, mode, ox, oy):
    """Apply a drag offset (ox, oy) to the edges named by `mode`.

    `mode` is one of the edge/corner zone ids ('n','se',...).  Each present
    direction moves only its edge, so corners move two edges at once.  The
    result is normalised so a flipped drag produces a valid positive rect.
    """
    x, y, w, h = origin
    left, top, right, bottom = x, y, x + w, y + h
    if "w" in mode:
        left = x + ox
    if "e" in mode:
        right = (x + w) + ox
    if "n" in mode:
        top = y + oy
    if "s" in mode:
        bottom = (y + h) + oy
    return normalize([left, top, right - left, bottom - top])


def toggle_full_selection(selection, prev, logical_w, logical_h):
    """Toggle between a whole-monitor selection and the previous box.

    Returns (new_selection, new_prev).  If `selection` already fills the
    monitor and a `prev` box is remembered, restore `prev`; otherwise remember
    the current box and expand to full screen.
    """
    full = [0, 0, logical_w, logical_h]
    x, y, w, h = normalize(selection)
    # Exact match only: a user box that merely sits 1px from the edge must NOT
    # be treated as "already full" (that would silently discard it).
    is_full = (x == 0 and y == 0 and w == logical_w and h == logical_h)
    if is_full and prev:
        return list(prev), prev
    new_prev = [x, y, w, h] if not is_full else prev
    return full, new_prev


# ----------------------------------------------------------------------------
# GTK overlay window
# ----------------------------------------------------------------------------


class OverlayWindow(Gtk.ApplicationWindow):
    def __init__(self, app, surface, config, full_desktop=False):
        super().__init__(application=app)
        self.app = app
        self.surface = surface          # physical-res capture (for cropping)
        self.config = config
        self.full_desktop = full_desktop  # True only for the portal fallback

        self.set_decorated(False)
        self.add_css_class("snapclip-overlay")

        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        monitor = self._primary_monitor(display)
        geo = monitor.get_geometry()
        self.logical_w, self.logical_h = geo.width, geo.height
        # scale (physical px per logical px) and origin_px (this monitor's
        # top-left within the capture, in PHYSICAL pixels).
        if not full_desktop or monitors.get_n_items() <= 1:
            # ScreenCast (per-monitor) OR single-monitor portal: the surface is
            # exactly this screen, so capture/logical is the exact scale and the
            # origin is (0, 0).  This also handles any fractional scaling.
            self.scale = compute_scale(
                surface.get_width(), surface.get_height(),
                self.logical_w, self.logical_h,
            )
            self.origin_px = (0, 0)
        else:
            # Portal fallback on multi-monitor: the capture spans every screen,
            # so use THIS monitor's own scale and physical origin.
            mscale = self._monitor_scale(monitor)
            self.scale = (mscale, mscale)
            self.origin_px = (int(round(geo.x * mscale)),
                              int(round(geo.y * mscale)))
        # Request the full monitor size up front.  Do NOT mark the window
        # non-resizable: on Wayland that makes GTK reject the compositor's
        # fullscreen configure and the window collapses to its 200x200 minimum.
        self.set_default_size(self.logical_w, self.logical_h)

        # Pre-render a logical-sized background so each frame is a 1:1 blit.
        self.bg = self._build_background()

        # Initial selection.
        self.selection = self._initial_selection()
        self.pointer = (self.logical_w / 2, self.logical_h / 2)

        self._drag_mode = None
        self._drag_origin = None
        self._drag_anchor = None
        self._drag_moved = False        # did this drag actually move? (vs a click)
        self._prev_selection = None     # box to restore from a full-monitor toggle
        self._done = False  # guard against double actions

        # Drawing surface.
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        # Explicit content size guarantees a full-monitor natural size even
        # before the fullscreen configure arrives.
        self.area.set_content_width(self.logical_w)
        self.area.set_content_height(self.logical_h)
        self.area.set_draw_func(self.on_draw)

        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.area)
        self.toolbar = self._build_toolbar()
        self.overlay.add_overlay(self.toolbar)

        # Error banner (hidden unless a copy/save actually fails).
        self.error_label = Gtk.Label(label="")
        self.error_label.add_css_class("snapclip-error")
        self.error_label.set_halign(Gtk.Align.CENTER)
        self.error_label.set_valign(Gtk.Align.START)
        self.error_label.set_margin_top(36)
        self.error_label.set_visible(False)
        self.overlay.add_overlay(self.error_label)

        self.set_child(self.overlay)

        # Controllers.
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.area.add_controller(drag)

        # Double-click toggles a whole-monitor selection (and back). Lives in a
        # separate click gesture; drag_begin is non-destructive (see below) so
        # the two never fight regardless of event-delivery order.
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)   # left-button double-clicks only
        click.connect("pressed", self.on_pressed)
        self.area.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        self.area.add_controller(motion)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.connect("realize", lambda *_: self.fullscreen_on_monitor(monitor))
        GLib.idle_add(self._reposition_toolbar)

    # -- setup helpers -------------------------------------------------------

    def _primary_monitor(self, display):
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            m = monitors.get_item(i)
            try:
                if m.is_primary():
                    return m
            except Exception:
                pass
        return monitors.get_item(0)

    def _monitor_scale(self, monitor):
        """Fractional physical-per-logical scale of a monitor (>=1)."""
        try:
            s = monitor.get_scale()       # GTK >= 4.12, fractional
            if s and s > 0:
                return float(s)
        except Exception:
            pass
        try:
            return float(monitor.get_scale_factor() or 1)
        except Exception:
            return 1.0

    def _build_background(self):
        sx, sy = self.scale
        ox, oy = self.origin_px
        bg = cairo.ImageSurface(cairo.FORMAT_RGB24, self.logical_w, self.logical_h)
        cr = cairo.Context(bg)
        cr.scale(1.0 / sx, 1.0 / sy)
        # Offset by this monitor's physical origin so a multi-monitor capture
        # shows only this screen's region.
        cr.set_source_surface(self.surface, -ox, -oy)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        bg.flush()
        return bg

    def _initial_selection(self):
        last = self.config.get("last_selection")
        if self.config.get("remember_selection") and last and len(last) == 4:
            x, y, w, h = last
            # Pull a remembered box fully inside the current monitor (the screen
            # may have shrunk); only use it if it still fits at >= MIN_SIZE.
            x = max(0, min(int(x), self.logical_w - MIN_SIZE))
            y = max(0, min(int(y), self.logical_h - MIN_SIZE))
            w = min(int(w), self.logical_w - x)
            h = min(int(h), self.logical_h - y)
            if w >= MIN_SIZE and h >= MIN_SIZE:
                return [x, y, w, h]
        # Not remembering (or nothing usable remembered): a centred box sized to
        # a configurable fraction of the screen.
        pct = float(self.config.get("default_size_pct", 0.4) or 0.4)
        pct = min(1.0, max(0.05, pct))
        w = max(MIN_SIZE, int(self.logical_w * pct))
        h = max(MIN_SIZE, int(self.logical_h * pct))
        return [(self.logical_w - w) // 2, (self.logical_h - h) // 2, w, h]

    def _build_toolbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.add_css_class("snapclip-toolbar")
        bar.set_halign(Gtk.Align.START)
        bar.set_valign(Gtk.Align.START)

        def button(icon, label, tip, handler):
            b = Gtk.Button()
            b.set_tooltip_text(tip)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            content.append(Gtk.Image.new_from_icon_name(icon))
            content.append(Gtk.Label(label=label))
            b.set_child(content)
            b.connect("clicked", handler)
            b.set_focusable(False)
            bar.append(b)
            return b

        self.size_label = Gtk.Label(label="")
        self.size_label.add_css_class("snapclip-size")
        bar.append(self.size_label)

        button("edit-copy-symbolic", "Copy", "Copy to clipboard (Enter)",
               lambda *_: self.do_copy())
        button("document-save-symbolic", "Save", "Save to disk + copy (S)",
               lambda *_: self.do_save())
        button("window-close-symbolic", "Cancel", "Cancel (Esc)",
               lambda *_: self.do_cancel())

        gear = Gtk.Button()
        gear.set_tooltip_text("Settings")
        gear.set_child(Gtk.Image.new_from_icon_name("emblem-system-symbolic"))
        gear.set_focusable(False)
        gear.connect("clicked", lambda *_: self.open_settings())
        gear.add_css_class("snapclip-gear")
        bar.append(gear)

        # Over the toolbar the pointer should be a normal arrow, not a stale
        # resize/move cursor left over from hovering the selection.
        try:
            bar.set_cursor(Gdk.Cursor.new_from_name("default", None))
        except Exception:
            pass

        return bar

    # -- drawing -------------------------------------------------------------

    def on_draw(self, area, cr, width, height):
        x, y, w, h = self.selection

        # 1. frozen screen (interior shows real content => "see-through")
        cr.set_source_surface(self.bg, 0, 0)
        cr.paint()

        # 2. dim everything OUTSIDE the selection (interior untouched)
        dim = float(self.config.get("dim_opacity", 0.0) or 0.0)
        if dim > 0:
            cr.set_source_rgba(0, 0, 0, dim)
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(0, 0, width, height)
            cr.rectangle(x, y, w, h)
            cr.fill()

        # 3. border
        bc = Gdk.RGBA(); bc.parse(self.config.get("border_color", "#00A3FF"))
        bw = float(self.config.get("border_width", 2) or 2)
        cr.set_line_width(bw)
        cr.set_source_rgba(bc.red, bc.green, bc.blue, bc.alpha)
        cr.rectangle(x + 0.5, y + 0.5, w, h)
        cr.stroke()

        # 4. corner + edge handles
        # Reset to the winding rule: the dim step above left EVEN_ODD set, which
        # would punch holes where the handle squares overlap (tiny selections).
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        hc = Gdk.RGBA(); hc.parse(self.config.get("handle_color", "#FFFFFF"))
        cr.set_source_rgba(hc.red, hc.green, hc.blue, hc.alpha)
        for hx, hy in [
            (x, y), (x + w, y), (x, y + h), (x + w, y + h),
            (x + w / 2, y), (x + w / 2, y + h),
            (x, y + h / 2), (x + w, y + h / 2),
        ]:
            cr.rectangle(hx - HANDLE_DRAW, hy - HANDLE_DRAW,
                         HANDLE_DRAW * 2, HANDLE_DRAW * 2)
        cr.fill()

        # 5. live W x H readout near the top-left of the selection
        pw, ph = self._readout_px()
        self._draw_badge(cr, "%d × %d" % (pw, ph), x, y)

    def _readout_px(self):
        """Physical pixel size of the current selection — computed with the SAME
        edges-then-difference math as the crop, so the readout never disagrees
        with the produced PNG (notably under fractional scaling)."""
        sx, sy = self.scale
        ox, oy = self.origin_px
        x, y, w, h = self.selection
        pw = max(1, int(round(ox + (x + w) * sx)) - int(round(ox + x * sx)))
        ph = max(1, int(round(oy + (y + h) * sy)) - int(round(oy + y * sy)))
        return pw, ph

    def _draw_badge(self, cr, text, x, y):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(13)
        ext = cr.text_extents(text)
        pad = 5
        bw_, bh_ = ext.width + pad * 2, ext.height + pad * 2
        bx = x
        by = y - bh_ - 4
        if by < 0:                       # not enough room above -> put inside
            by = y + 4
        bx = max(0, min(bx, self.logical_w - bw_))
        cr.set_source_rgba(0, 0, 0, 0.65)
        cr.rectangle(bx, by, bw_, bh_)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(bx + pad - ext.x_bearing, by + pad - ext.y_bearing)
        cr.show_text(text)

    # -- interaction ---------------------------------------------------------

    def on_motion(self, _c, mx, my):
        self.pointer = (mx, my)
        if self._drag_mode is None:
            zone = hit_zone(self.selection, mx, my)
            self._set_cursor(CURSOR_FOR_ZONE.get(zone, "default"))

    def _set_cursor(self, name):
        try:
            self.set_cursor(Gdk.Cursor.new_from_name(name, None))
        except Exception:
            pass

    def on_drag_begin(self, gesture, sx, sy):
        self._drag_origin = list(self.selection)
        self._drag_moved = False
        zone = hit_zone(self.selection, sx, sy)
        if zone == Z_OUTSIDE:
            # Don't create the new box yet — only once the pointer actually
            # moves (on_drag_update). A click with no movement then leaves the
            # selection untouched, which also keeps double-click robust.
            self._drag_mode = "new"
            self._drag_anchor = (sx, sy)
        else:
            self._drag_mode = zone
        self._set_cursor(CURSOR_FOR_ZONE.get(zone, "default"))

    def on_drag_update(self, gesture, ox, oy):
        if self._drag_mode is None:
            return
        self._drag_moved = True
        mode = self._drag_mode
        if mode == "new":
            ax, ay = self._drag_anchor
            self.selection = [ax, ay, ox, oy]
        elif mode == Z_INSIDE:
            x, y, w, h = self._drag_origin
            nx = max(0, min(x + ox, self.logical_w - w))
            ny = max(0, min(y + oy, self.logical_h - h))
            self.selection = [nx, ny, w, h]
        else:
            self.selection = resize_rect(self._drag_origin, mode, ox, oy)
        self._clamp_selection_soft()
        self.pointer = (self._drag_origin[0] + ox, self._drag_origin[1] + oy) \
            if mode != "new" else (self._drag_anchor[0] + ox,
                                   self._drag_anchor[1] + oy)
        self.area.queue_draw()
        self._reposition_toolbar()

    def _clamp_selection_soft(self):
        x, y, w, h = normalize(self.selection)
        # Pull the origin inside first, leaving room for at least MIN_SIZE, so
        # enforcing the minimum below can't push the far edge past the monitor.
        x = max(0, min(x, self.logical_w - MIN_SIZE))
        y = max(0, min(y, self.logical_h - MIN_SIZE))
        w = max(MIN_SIZE, min(w, self.logical_w - x))
        h = max(MIN_SIZE, min(h, self.logical_h - y))
        self.selection = [x, y, w, h]

    def on_drag_end(self, gesture, ox, oy):
        moved = self._drag_moved
        self._drag_mode = None
        self._drag_moved = False
        if moved:                       # a real drag — finalise it
            self._clamp_selection_soft()
            self.area.queue_draw()
            self._reposition_toolbar()
        # a click (no movement) leaves the selection exactly as it was

    def on_pressed(self, gesture, n_press, x, y):
        if n_press == 2:
            self._toggle_full_selection()

    def _toggle_full_selection(self):
        """Double-click: snap to the whole monitor; double-click again restore."""
        self.selection, self._prev_selection = toggle_full_selection(
            self.selection, self._prev_selection,
            self.logical_w, self.logical_h)
        # cancel any in-flight drag so a stray drag_end can't fight us
        self._drag_mode = None
        self._drag_moved = False
        self.area.queue_draw()
        self._reposition_toolbar()

    def _reposition_toolbar(self):
        x, y, w, h = self.selection
        # Prefer the real allocation; before first allocation fall back to the
        # measured natural size (get_width() is 0 until allocated).
        tb_w = self.toolbar.get_width() \
            or self.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[1] or 280
        tb_h = self.toolbar.get_height() \
            or self.toolbar.measure(Gtk.Orientation.VERTICAL, tb_w)[1] or 40
        tx = int(max(0, min(x, self.logical_w - tb_w)))
        ty = int(y + h + 8)
        if ty + tb_h > self.logical_h:        # no room below -> above
            ty = int(y - tb_h - 8)
        if ty < 0:                            # no room above -> inside, top
            ty = int(y + 8)
        self.toolbar.set_margin_start(tx)
        self.toolbar.set_margin_top(ty)
        self.size_label.set_text(self._size_text())
        return False

    def _size_text(self):
        pw, ph = self._readout_px()
        return "%d×%d" % (pw, ph)

    # -- key handling --------------------------------------------------------

    def on_key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.do_cancel()
            return True
        # Ignore the action keys when Ctrl/Alt/Super are held, so e.g. Ctrl+S
        # (a habit) doesn't trigger Save. Shift is allowed (capital S).
        if state & (Gdk.ModifierType.CONTROL_MASK
                    | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK):
            return False
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.do_copy()
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.do_save()
            return True
        return False

    # -- actions -------------------------------------------------------------

    def _png_bytes(self):
        self._clamp_selection_soft()
        cursor = self.pointer if self.config.get("include_cursor") else None
        return crop_to_png_bytes(
            self.surface, self.selection, self.scale,
            origin_px=self.origin_px, cursor=cursor,
        )

    def _remember(self):
        if self.config.get("remember_selection"):
            self.config["last_selection"] = [int(v) for v in self.selection]
            save_config(self.config)

    def _fail(self, msg):
        """Surface a failure instead of silently 'succeeding' and closing.

        The fullscreen overlay would hide any stderr message, so on failure we
        keep the window open, show a banner, and re-arm so the user can retry
        or press Esc.  A non-zero process exit is also recorded for callers.
        """
        print(f"snapclip: {msg}", file=sys.stderr)
        self.app.had_error = True
        self.error_label.set_text(f"⚠  {msg}   —   press Esc to close, or retry")
        self.error_label.set_visible(True)
        self._done = False
        self.area.queue_draw()

    def do_copy(self):
        if self._done:
            return
        self._done = True
        try:
            copy_png_to_clipboard(self._png_bytes())
        except Exception as exc:
            self._fail(f"copy failed: {exc}")
            return
        self.app.had_error = False      # a prior failed attempt is now resolved
        self._remember()
        self.app.quit()

    def do_save(self):
        if self._done:
            return
        self._done = True
        try:
            png = self._png_bytes()
            copy_png_to_clipboard(png)
        except Exception as exc:
            self._fail(f"copy failed: {exc}")
            return
        try:
            path = save_png(png, self.config.get("save_dir"),
                            self.config.get("filename_format"))
            print(f"snapclip: saved {path}")
        except Exception as exc:
            self._fail(f"saved to clipboard but writing the file failed: {exc}")
            return
        self.app.had_error = False      # a prior failed attempt is now resolved
        self._remember()
        self.app.quit()

    def do_cancel(self):
        if self._done:
            return
        self._done = True
        self._remember()
        self.app.quit()

    # -- settings ------------------------------------------------------------

    def open_settings(self):
        SettingsDialog(self).present()


# ----------------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------------


class SettingsDialog(Gtk.Window):
    def __init__(self, overlay):
        super().__init__(title="snapclip settings")
        self.overlay = overlay
        self.cfg = overlay.config
        self.set_transient_for(overlay)
        self.set_modal(True)
        self.set_default_size(420, -1)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        grid.set_margin_top(16); grid.set_margin_bottom(16)
        grid.set_margin_start(16); grid.set_margin_end(16)
        self.set_child(grid)
        row = 0

        def add(label, widget):
            nonlocal row
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(False)
            grid.attach(lbl, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            row += 1

        # Border colour
        self.color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        rgba = Gdk.RGBA(); rgba.parse(self.cfg["border_color"])
        self.color_btn.set_rgba(rgba)
        self.color_btn.connect("notify::rgba", self._on_color)
        add("Border colour", self.color_btn)

        # Border width
        self.width_spin = Gtk.SpinButton.new_with_range(1, 12, 1)
        self.width_spin.set_value(self.cfg["border_width"])
        self.width_spin.connect("value-changed", self._on_width)
        add("Border width", self.width_spin)

        # Dim opacity
        self.dim_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 0.8, 0.05)
        self.dim_scale.set_value(self.cfg["dim_opacity"])
        self.dim_scale.set_draw_value(True)
        self.dim_scale.connect("value-changed", self._on_dim)
        add("Outside dim", self.dim_scale)

        # Include cursor
        self.cursor_sw = Gtk.Switch()
        self.cursor_sw.set_active(bool(self.cfg["include_cursor"]))
        self.cursor_sw.set_halign(Gtk.Align.START)
        self.cursor_sw.connect("notify::active", self._on_cursor)
        add("Include mouse cursor", self.cursor_sw)

        # Remember selection
        self.remember_sw = Gtk.Switch()
        self.remember_sw.set_active(bool(self.cfg["remember_selection"]))
        self.remember_sw.set_halign(Gtk.Align.START)
        self.remember_sw.connect("notify::active", self._on_remember)
        add("Remember last selection", self.remember_sw)

        # Default size (used when not remembering): fraction of the screen
        self.size_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
        self.size_scale.set_value(self.cfg["default_size_pct"])
        self.size_scale.set_draw_value(True)
        self.size_scale.set_tooltip_text(
            "Size of the initial box when not remembering the last selection")
        self.size_scale.connect("value-changed", self._on_default_size)
        add("Default size (× screen)", self.size_scale)

        # Save folder
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.dir_entry = Gtk.Entry()
        self.dir_entry.set_text(self.cfg["save_dir"])
        self.dir_entry.set_hexpand(True)
        self.dir_entry.connect("changed", self._on_dir)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse)
        folder_box.append(self.dir_entry)
        folder_box.append(browse)
        add("Save folder", folder_box)

        # Filename format
        self.fmt_entry = Gtk.Entry()
        self.fmt_entry.set_text(self.cfg["filename_format"])
        self.fmt_entry.connect("changed", self._on_fmt)
        add("Filename format", self.fmt_entry)

        # Buttons
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        reset = Gtk.Button(label="Reset defaults")
        reset.connect("clicked", self._on_reset)
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda *_: self.close())
        btns.append(reset)
        btns.append(done)
        grid.attach(btns, 0, row, 2, 1)

    def _apply(self):
        save_config(self.cfg)
        self.overlay.area.queue_draw()
        self.overlay._reposition_toolbar()

    def _on_color(self, btn, _p):
        self.cfg["border_color"] = btn.get_rgba().to_string()
        self._apply()

    def _on_width(self, spin):
        self.cfg["border_width"] = int(spin.get_value())
        self._apply()

    def _on_dim(self, scale):
        self.cfg["dim_opacity"] = round(scale.get_value(), 3)
        self._apply()

    def _on_cursor(self, sw, _p):
        self.cfg["include_cursor"] = sw.get_active()
        save_config(self.cfg)

    def _on_remember(self, sw, _p):
        self.cfg["remember_selection"] = sw.get_active()
        save_config(self.cfg)

    def _on_default_size(self, scale):
        self.cfg["default_size_pct"] = round(scale.get_value(), 3)
        save_config(self.cfg)

    def _on_dir(self, entry):
        self.cfg["save_dir"] = entry.get_text()
        save_config(self.cfg)

    def _on_fmt(self, entry):
        self.cfg["filename_format"] = entry.get_text()
        save_config(self.cfg)

    def _on_browse(self, _b):
        dialog = Gtk.FileDialog()
        start = os.path.expanduser(self.cfg.get("save_dir") or "~")
        if os.path.isdir(start):
            dialog.set_initial_folder(Gio.File.new_for_path(start))

        def done(dlg, res):
            try:
                folder = dlg.select_folder_finish(res)
                if folder:
                    self.dir_entry.set_text(folder.get_path())
            except GLib.Error:
                pass

        dialog.select_folder(self, None, done)

    def _on_reset(self, _b):
        for k, v in DEFAULT_CONFIG.items():
            if k != "last_selection":
                self.cfg[k] = v
        rgba = Gdk.RGBA(); rgba.parse(self.cfg["border_color"])
        self.color_btn.set_rgba(rgba)
        self.width_spin.set_value(self.cfg["border_width"])
        self.dim_scale.set_value(self.cfg["dim_opacity"])
        self.cursor_sw.set_active(self.cfg["include_cursor"])
        self.remember_sw.set_active(self.cfg["remember_selection"])
        self.size_scale.set_value(self.cfg["default_size_pct"])
        self.dir_entry.set_text(self.cfg["save_dir"])
        self.fmt_entry.set_text(self.cfg["filename_format"])
        self._apply()


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------

CSS = """
.snapclip-toolbar {
    background-color: rgba(30,30,32,0.92);
    border-radius: 10px;
    padding: 5px;
    box-shadow: 0 3px 12px rgba(0,0,0,0.5);
}
.snapclip-toolbar button {
    background: transparent;
    color: #f2f2f2;
    border: none;
    box-shadow: none;
    padding: 5px 9px;
    border-radius: 7px;
    min-height: 0;
}
.snapclip-toolbar button:hover { background-color: rgba(255,255,255,0.16); }
.snapclip-size {
    color: #ffffff;
    font-size: 12px;
    padding: 0 8px 0 4px;
    opacity: 0.85;
}
.snapclip-error {
    background-color: rgba(180,30,30,0.95);
    color: #ffffff;
    font-weight: bold;
    padding: 8px 16px;
    border-radius: 8px;
    box-shadow: 0 3px 12px rgba(0,0,0,0.5);
}
"""


class SnapClipApp(Gtk.Application):
    def __init__(self, surface, config, self_test=None, full_desktop=False):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.surface = surface
        self.config = config
        self.self_test = self_test  # None | "copy" | "save"
        self.full_desktop = full_desktop
        self.test_result = {}
        self.had_error = False

    def do_activate(self):
        # GTK swallows exceptions raised from an activate handler, which would
        # otherwise leave the app exiting 0 with no window (silent no-op) or, in
        # self-test, hang because quit() never runs. Guard the whole thing.
        try:
            provider = Gtk.CssProvider()
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            win = OverlayWindow(self, self.surface, self.config,
                                full_desktop=self.full_desktop)
            if self.self_test:
                self._run_self_test(win)
                return
            win.present()
        except Exception as exc:
            print(f"snapclip: failed to open the overlay: {exc}", file=sys.stderr)
            self.had_error = True
            self.test_result.setdefault("error", str(exc))
            self.quit()

    def _run_self_test(self, win):
        # Use a deterministic selection covering the centre of the screen.
        x = int(win.logical_w * 0.25)
        y = int(win.logical_h * 0.25)
        w = int(win.logical_w * 0.5)
        h = int(win.logical_h * 0.5)
        win.selection = [x, y, w, h]
        png = win._png_bytes()
        self.test_result = {
            "selection": win.selection,
            "scale": win.scale,
            "expected_px": win._readout_px(),
            "png_len": len(png),
        }
        try:
            copy_png_to_clipboard(png)
            self.test_result["copied"] = True
        except Exception as exc:
            self.test_result["copied"] = False
            self.test_result["error"] = str(exc)
        if self.self_test == "save":
            try:
                self.test_result["saved"] = save_png(
                    png, self.config.get("save_dir"),
                    self.config.get("filename_format"))
            except Exception as exc:
                self.test_result["copied"] = False   # mark the run as failed
                self.test_result["error"] = f"save failed: {exc}"
        self.quit()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="Region screenshot tool")
    parser.add_argument("--self-test", choices=["copy", "save"],
                        help="capture + crop + act without the GUI, then exit")
    parser.add_argument("--allow-flash", action="store_true",
                        help="if flash-free ScreenCast is unavailable, use the "
                             "screenshot portal instead of erroring (the portal "
                             "triggers GNOME's screenshot flash)")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print("snapclip 1.0")
        return 0

    config = load_config()

    # Single-instance for the interactive overlay: a second hotkey press must
    # not stack another full-screen overlay. Hold the lock for the whole run.
    # (self-test is a non-interactive debug mode and is exempt.)
    lock = None
    if not args.self_test:
        lock = acquire_single_instance_lock()
        if lock is ALREADY_RUNNING:
            print("snapclip: a snapclip overlay is already open", file=sys.stderr)
            return 0

    try:
        surface, full_desktop = capture_screen(allow_flash=args.allow_flash)
    except CaptureError as exc:
        print(f"snapclip: {exc}", file=sys.stderr)
        return 2

    app = SnapClipApp(surface, config, self_test=args.self_test,
                      full_desktop=full_desktop)
    app.run([])
    # `lock` stays referenced until here so the flock is held for the whole
    # session; it releases automatically when the process exits.

    if args.self_test:
        r = app.test_result
        ok = r.get("copied") and r.get("png_len", 0) > 0
        print("SELF-TEST", "PASS" if ok else "FAIL", r)
        return 0 if ok else 1
    return 1 if app.had_error else 0


if __name__ == "__main__":
    sys.exit(main())
