#!/usr/bin/env python3
"""
snapclip — a minimal region-screenshot tool for Ubuntu / GNOME / Wayland.

Architecture (why it works on GNOME 50 Wayland, and why it is fast):

  * The screen is captured ONCE at launch, flash-free, by grabbing a single
    frame from Mutter's ScreenCast interface over PipeWire.  (On GNOME 50 the
    older org.gnome.Shell.Screenshot D-Bus interface returns "AccessDenied",
    grim fails because Mutter has no wlr-screencopy, and the screenshot portal
    plays GNOME's shutter flash — it is only used with --allow-flash.)

  * The capture runs on a worker thread while the main thread builds and
    realizes the GTK window (GL context, shaders, widgets).  Nothing is shown
    until the frame is in hand, so the overlay never appears in its own shot,
    but the two ~80 ms jobs overlap instead of running back to back.

  * That frame is shown FROZEN inside a single full-screen, undecorated GTK4
    window.  The frame is uploaded to the GPU once as a texture; the selection
    box, dimming, handles and readout are GSK render nodes, so a redraw during
    a drag costs the GPU a handful of quads instead of the CPU a full-screen
    software blit.  The selection is drawn *as graphics* inside that fixed
    surface (never an OS window that gets moved), which sidesteps Wayland's
    "an app may not position its own window" restriction.

  * On confirm the window is hidden FIRST (GNOME starts its close animation
    at once), then the *cached* capture is cropped, PNG-encoded and handed to
    `wl-copy --type image/png` over stdin — no temp file, no second capture,
    no flash, and the selection border can never appear in the result.  Save
    additionally writes a timestamped PNG.

Keys:  Enter / Ctrl+C = copy   •   S = save+copy   •   arrows = nudge
       (Shift+arrows = resize)   •   Ctrl+Z = undo annotation
       Esc = leave tool mode / cancel
"""

import fcntl
import io
import itertools
import json
import os
import sys
import threading
import types

# GTK picks its Vulkan renderer on this class of system; its device and
# pipeline setup costs ~100 ms more at window realize than the GL renderer, for
# identical output (measured 158 ms vs 55 ms).  A screenshot tool lives for two
# seconds, so startup wins.  (GL compiles its shaders on the very first run
# after a driver/GTK update — Mesa's disk cache makes every later launch fast.)
# An explicit GSK_RENDERER in the environment still takes precedence.
os.environ.setdefault("GSK_RENDERER", "gl")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gdk, Gsk, Graphene, GLib, Gio, Pango  # noqa: E402

import cairo  # noqa: E402  (needs python3-gi-cairo / pycairo)

APP_ID = "dev.snapclip.SnapClip"

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

CONFIG_DIR = os.path.join(
    GLib.get_user_config_dir() or os.path.expanduser("~/.config"), "snapclip"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# GLib guarantees a per-user directory here (it falls back to the user cache
# dir when XDG_RUNTIME_DIR is unset), so no world-writable /tmp fallback.
LOCK_PATH = os.path.join(GLib.get_user_runtime_dir(), "snapclip.lock")


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
        # O_NOFOLLOW: never open through a symlink someone planted at the path.
        fd = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fp = os.fdopen(fd, "w")
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
    # Default colours are chosen as a WCAG set: border vs pen vs text have
    # pairwise contrast ratios >= 3:1 (WCAG 1.4.11), so the three elements
    # are distinguishable out of the box.  Users can still pick anything.
    "default_monitor": "primary",    # "primary", index ("0", "1"), or connector name ("HDMI-1", "eDP-1")
    "border_color": "#0077CC",      # selection outline colour
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
    "always_save": False,            # Copy also writes the PNG file
    # Double-tap the launch hotkey to save the whole screen instantly with no
    # overlay. OFF by default. When on, a single tap waits double_tap_ms for a
    # possible second tap before showing the overlay (so it can be suppressed).
    "quick_save_double_tap": False,
    "double_tap_ms": 300,            # second-tap window / arming delay (120-800)
    # Optional tools. ALL off by default — the stock toolbar stays exactly
    # Copy / Save / Cancel; enabling one adds its button.
    "tool_polygon": False,           # polygon selection (click corners)
    "tool_lasso": False,             # freehand selection (drag a loop)
    "tool_pen": False,               # draw strokes onto the shot
    "tool_text": False,             # place text onto the shot
    "pen_color": "#FFD60A",
    "pen_width": 3,
    "text_color": "#2D0A4E",
    "text_size": 18,
}


def load_config():
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
    if not isinstance(cfg.get("default_monitor"), (str, int)):
        cfg["default_monitor"] = DEFAULT_CONFIG["default_monitor"]
    else:
        cfg["default_monitor"] = str(cfg["default_monitor"]).strip() or "primary"
    for key in ("save_dir", "filename_format", "border_color", "handle_color",
                "pen_color", "text_color"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            cfg[key] = DEFAULT_CONFIG[key]
    # Colours must actually parse, else Gdk.RGBA.parse() leaves garbage.
    for key in ("border_color", "handle_color", "pen_color", "text_color"):
        if not Gdk.RGBA().parse(cfg[key]):
            cfg[key] = DEFAULT_CONFIG[key]
    for key in ("border_width", "dim_opacity", "default_size_pct",
                "pen_width", "text_size", "double_tap_ms"):
        if not isinstance(cfg.get(key), (int, float)) or isinstance(cfg.get(key), bool):
            cfg[key] = DEFAULT_CONFIG[key]
    # Numbers must also be in range: a hand-edited border_width of 0 or a
    # dim_opacity of 7 would mean an invisible border / an all-black overlay.
    cfg["border_width"] = min(12, max(1, cfg["border_width"]))
    cfg["dim_opacity"] = min(0.9, max(0.0, cfg["dim_opacity"]))
    cfg["default_size_pct"] = min(1.0, max(0.05, cfg["default_size_pct"]))
    cfg["pen_width"] = min(16, max(1, cfg["pen_width"]))
    cfg["text_size"] = min(72, max(8, cfg["text_size"]))
    cfg["double_tap_ms"] = int(min(800, max(120, cfg["double_tap_ms"])))
    for key in ("include_cursor", "remember_selection", "always_save",
                "quick_save_double_tap",
                "tool_polygon", "tool_lasso", "tool_pen", "tool_text"):
        if not isinstance(cfg.get(key), bool):
            cfg[key] = DEFAULT_CONFIG[key]
    sel = cfg.get("last_selection")
    if sel is not None and not (isinstance(sel, list) and len(sel) == 4
                                and all(isinstance(v, (int, float)) for v in sel)):
        cfg["last_selection"] = None
    return cfg


def save_config(cfg):
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


_portal_token_seq = itertools.count(1)   # unique handle_token per request


class _SignalWait:
    """Block the calling thread until a D-Bus signal handler calls quit() or
    `timeout_s` elapses.  Runs on the thread-default GLib context, so the same
    code works on the main thread (tests, --self-test) and on the capture
    worker thread, which pushes a private context so nothing it does can touch
    GTK's main loop."""

    def __init__(self, timeout_s):
        self._ctx = GLib.MainContext.get_thread_default()
        self._loop = GLib.MainLoop.new(self._ctx, False)
        self.timed_out = False
        self._src = GLib.timeout_source_new_seconds(timeout_s)
        self._src.set_callback(self._on_timeout)
        self._src.attach(self._ctx)

    def _on_timeout(self, *_args):
        self.timed_out = True
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def quit(self):
        self._loop.quit()

    def run(self):
        """Returns True if quit() was called, False on timeout."""
        self._loop.run()
        if not self.timed_out:
            self._src.destroy()
        return not self.timed_out


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
    token = "snapclip_%d_%d" % (os.getpid(), next(_portal_token_seq))
    request_path = f"/org/freedesktop/portal/desktop/request/{unique}/{token}"

    wait = _SignalWait(timeout_s)
    state = {}

    def on_response(_c, _s, _o, _i, _sig, params):
        code, results = params.unpack()
        state["code"] = code
        state["results"] = results
        wait.quit()

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
        wait.run()
    finally:
        bus.signal_unsubscribe(sub)

    if wait.timed_out:
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


def _get_session_bus(bus=None):
    if bus is not None:
        return bus
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise CaptureError(f"cannot reach the session bus: {exc}")


def _list_monitors(bus=None):
    """List detected monitors with indices, connectors, friendly names,
    resolutions, and primary status via Mutter DisplayConfig."""
    try:
        b = _get_session_bus(bus)
        r = b.call_sync(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )
        _serial, mutter_monitors, logical, _props = r.unpack()
        conn_meta = {}
        for m in mutter_monitors:
            # m = ((connector, vendor, product, serial), modes, props)
            conn = m[0][0]
            display_name = m[2].get("display-name", "")
            res = (0, 0)
            for mode in m[1]:
                if mode[6].get("is-current"):
                    res = (mode[1], mode[2])
                    break
            conn_meta[conn] = {"display_name": display_name, "res": res}

        layout_mode = _props.get("layout-mode", 1) if isinstance(_props, dict) else 1

        # Sort by screen coordinates (x, then y) to provide consistent indexing
        sorted_logical = sorted(logical, key=lambda lm: (lm[0], lm[1]))
        monitors = []
        for idx, lm in enumerate(sorted_logical):
            # lm = (x, y, scale, transform, primary, [(connector, ...)], props)
            if lm[5]:
                conn = lm[5][0][0]
                meta = conn_meta.get(conn, {"display_name": "", "res": (0, 0)})
                name = meta["display_name"] or conn
                w, h = meta["res"]
                scale = float(lm[2]) if float(lm[2]) > 0 else 1.0
                transform = int(lm[3]) if len(lm) > 3 else 0

                # If rotated 90 or 270 degrees, physical mode dimensions are transposed
                if transform in (1, 3, 5, 7):
                    pw, ph = h, w
                else:
                    pw, ph = w, h

                # RecordArea takes compositor stage (logical) coordinates, NOT physical mode pixels.
                # In logical layout mode (GNOME's default on Wayland), stage dimensions are physical / scale.
                if layout_mode == 1 and scale > 0:
                    stage_w = int(round(pw / scale))
                    stage_h = int(round(ph / scale))
                else:
                    stage_w = pw
                    stage_h = ph

                # If GDK display is available, its monitor geometry directly reflects
                # the compositor's logical stage coordinates.
                try:
                    display = Gdk.Display.get_default()
                    if display is not None:
                        for gm in display.get_monitors():
                            if gm.get_connector() == conn:
                                geo = gm.get_geometry()
                                stage_w = geo.width
                                stage_h = geo.height
                                break
                except Exception:
                    pass

                monitors.append({
                    "index": idx,
                    "connector": conn,
                    "name": name,
                    "primary": bool(lm[4]),
                    "x": int(lm[0]),
                    "y": int(lm[1]),
                    "width": int(w),
                    "height": int(h),
                    "scale": scale,
                    "stage_x": int(lm[0]),
                    "stage_y": int(lm[1]),
                    "stage_width": stage_w,
                    "stage_height": stage_h,
                })
        return monitors
    except Exception:
        # Fallback to Gdk monitors if Mutter DisplayConfig is unreachable
        monitors = []
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                g_monitors = display.get_monitors()
                for idx in range(g_monitors.get_n_items()):
                    m = g_monitors.get_item(idx)
                    geo = m.get_geometry()
                    conn = m.get_connector() or f"MON-{idx}"
                    desc = m.get_description() or conn
                    mscale = float(getattr(m, "get_scale", lambda: 1.0)())
                    monitors.append({
                        "index": idx,
                        "connector": conn,
                        "name": desc,
                        "primary": idx == 0,
                        "x": geo.x,
                        "y": geo.y,
                        "width": int(round(geo.width * mscale)),
                        "height": int(round(geo.height * mscale)),
                        "scale": mscale,
                        "stage_x": geo.x,
                        "stage_y": geo.y,
                        "stage_width": geo.width,
                        "stage_height": geo.height,
                    })
        except Exception:
            pass
        return monitors


def _resolve_target_monitor(bus=None, target="primary"):
    """Find target monitor dict by index, connector name, friendly name, or 'primary'."""
    monitors = _list_monitors(bus)
    if not monitors:
        raise CaptureError("no monitor reported by Mutter")
    if target is None:
        target = "primary"
    target_str = str(target).strip()

    # 1. "primary" keyword
    if target_str.lower() == "primary":
        for m in monitors:
            if m["primary"]:
                return m
        return monitors[0]

    # 2. Integer index ("0", "1", 0, 1)
    try:
        idx = int(target_str)
        if 0 <= idx < len(monitors):
            return monitors[idx]
    except ValueError:
        pass

    # 3. Exact connector name match (case-insensitive: "HDMI-1", "edp-1")
    for m in monitors:
        if m["connector"].lower() == target_str.lower():
            return m

    # 4. Friendly name substring match (case-insensitive: "LG", "Built-in")
    for m in monitors:
        if target_str.lower() in m["name"].lower():
            return m

    # Fallback to primary if not found
    print(f"snapclip: monitor '{target}' not found; falling back to primary",
          file=sys.stderr)
    for m in monitors:
        if m["primary"]:
            return m
    return monitors[0]


def _primary_connector(bus):
    """Connector name (e.g. 'HDMI-1') of the primary monitor, via Mutter."""
    return _resolve_target_monitor(bus, "primary")["connector"]


def _print_monitors(bus=None):
    monitors = _list_monitors(bus)
    if not monitors:
        print("No monitors detected.", file=sys.stderr)
        return
    print("Available monitors:")
    for m in monitors:
        pri = " [Primary]" if m["primary"] else ""
        res_str = f" ({m['width']}x{m['height']})" if m["width"] and m["height"] else ""
        print(f"  [{m['index']}] {m['connector']}: {m['name']}{res_str}{pri}")


def _sample_to_surface(sample):
    """Convert a GStreamer BGRx sample into a cairo RGB24 surface.

    BGRx byte order equals cairo's RGB24 memory layout (little-endian), so no
    pixel conversion is needed.  PyGObject hands the mapped frame over as
    `bytes` (one copy); cairo needs a writable buffer, so one more copy into a
    bytearray is the minimum.  pycairo keeps that buffer alive for the
    surface's lifetime, so wrapping it is safe.
    """
    from gi.repository import Gst

    caps = sample.get_caps().get_structure(0)
    w, h = caps.get_value("width"), caps.get_value("height")
    if not w or not h:
        raise CaptureError("captured frame has no dimensions")
    buf = sample.get_buffer()
    cstride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, w)

    ok, minfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        raise CaptureError("could not map the captured frame")
    try:
        data = minfo.data
    finally:
        buf.unmap(minfo)

    if len(data) == cstride * h:                  # the common, tightly packed case
        packed = bytearray(data)
    else:
        # Rows are padded to an alignment: take the real stride from the video
        # meta when the allocator reports one, else infer it from the length.
        gstride = None
        try:
            gi.require_version("GstVideo", "1.0")
            from gi.repository import GstVideo
            vmeta = GstVideo.buffer_get_video_meta(buf)
            if vmeta is not None and vmeta.stride:
                gstride = vmeta.stride[0]
        except Exception:
            gstride = None
        if not gstride:
            gstride = len(data) // h
        if gstride * h > len(data):
            raise CaptureError("captured frame is truncated")
        row_bytes = min(gstride, cstride)
        mv = memoryview(data)
        packed = bytearray(cstride * h)
        for row in range(h):
            packed[row * cstride:row * cstride + row_bytes] = \
                mv[row * gstride:row * gstride + row_bytes]

    return cairo.ImageSurface.create_for_data(
        packed, cairo.FORMAT_RGB24, w, h, cstride)


def texture_for_surface(surface):
    """A Gdk.Texture of a cairo RGB24 capture, for the GPU-rendered overlay.

    RGB24 memory is B8G8R8X8, which GTK uploads as-is (the X byte is ignored).
    GLib.Bytes.new_take is the single-copy path in PyGObject (plain
    GLib.Bytes.new copies twice, and a bytearray is marshalled byte by byte).
    """
    surface.flush()
    data = GLib.Bytes.new_take(bytes(surface.get_data()))
    return Gdk.MemoryTexture.new(
        surface.get_width(), surface.get_height(),
        Gdk.MemoryFormat.B8G8R8X8, data, surface.get_stride())


def screencast_capture(timeout_s=10, on_connector=None, on_frame=None, target_monitor=None):
    """Grab one frame of the target monitor via Mutter ScreenCast + PipeWire.

    Returns (surface, connector): a cairo.ImageSurface (physical pixels of that
    monitor) and the connector name that was captured (e.g. 'HDMI-1'), so the
    overlay can be placed on the same monitor.  Two optional hooks let a caller
    overlap its own work with the capture: `on_connector(name)` fires as soon
    as the monitor is known (before the frame arrives), and `on_frame(surface)`
    fires the moment the frame is converted — before the ~10 ms ScreenCast /
    GStreamer teardown that precedes the return.
    Unlike the screenshot portal this does NOT play GNOME's shutter flash and
    shows no picker dialog.
    Raises CaptureError if ScreenCast/GStreamer is unavailable.
    """
    try:
        bus = _get_session_bus()
    except GLib.Error as exc:
        raise CaptureError(f"cannot reach the session bus: {exc}")

    mon_info = _resolve_target_monitor(bus, target_monitor)
    connector = mon_info["connector"]
    if on_connector is not None:
        on_connector(connector)
    session = None
    pipeline = None
    Gst = None
    try:
        try:
            r = bus.call_sync(
                "org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast",
                "org.gnome.Mutter.ScreenCast", "CreateSession",
                GLib.Variant("(a{sv})", ({},)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            session = r.unpack()[0]
            stream = None
            # RecordArea takes stage (logical) coordinates, NOT physical mode pixels.
            # If stage coordinates and dimensions are known, RecordArea captures from
            # Mutter's compositor stage directly without waiting for KMS pageflip/damage
            # events (crucial for idle secondary displays or PSR/VRR laptop panels).
            stage_w = mon_info.get("stage_width") or mon_info.get("width", 0)
            stage_h = mon_info.get("stage_height") or mon_info.get("height", 0)
            stage_x = mon_info.get("stage_x", mon_info.get("x", 0))
            stage_y = mon_info.get("stage_y", mon_info.get("y", 0))

            if stage_w > 0 and stage_h > 0:
                try:
                    r = bus.call_sync(
                        "org.gnome.Mutter.ScreenCast", session,
                        "org.gnome.Mutter.ScreenCast.Session", "RecordArea",
                        GLib.Variant("(iiiia{sv})",
                                     (stage_x, stage_y, stage_w, stage_h,
                                      {"cursor-mode": GLib.Variant("u", 0)})),
                        None, Gio.DBusCallFlags.NONE, -1, None,
                    )
                    stream = r.unpack()[0]
                except GLib.Error:
                    stream = None
            if stream is None:
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
        wait = _SignalWait(timeout_s)

        def on_added(_c, _s, _o, _i, _sig, params):
            state["node"] = params.unpack()[0]
            wait.quit()

        sub = bus.signal_subscribe(
            "org.gnome.Mutter.ScreenCast", "org.gnome.Mutter.ScreenCast.Stream",
            "PipeWireStreamAdded", stream, None, Gio.DBusSignalFlags.NONE, on_added)
        try:
            bus.call_sync(
                "org.gnome.Mutter.ScreenCast", session,
                "org.gnome.Mutter.ScreenCast.Session", "Start",
                None, None, Gio.DBusCallFlags.NONE, -1, None)
            # Load GStreamer while Mutter brings the PipeWire stream up.
            try:
                gi.require_version("Gst", "1.0")
                from gi.repository import Gst
            except (ValueError, ImportError) as exc:
                raise CaptureError(f"GStreamer not available: {exc}")
            if not Gst.is_initialized():
                Gst.init(None)
            wait.run()
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
            surface = _sample_to_surface(sample)
            if on_frame is not None:
                on_frame(surface)
            return surface, connector
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


def capture_screen(allow_flash=False, on_connector=None, on_frame=None, target_monitor=None):
    """Capture the target monitor, flash-free.

    Uses Mutter ScreenCast, which does not flash.  If that is unavailable we
    REFUSE to capture by default rather than silently flashing — the whole
    point of this tool is no flash, so falling back to a flashing capture would
    defeat it.  Pass allow_flash=True to deliberately opt into the (flashing)
    screenshot portal anyway.

    Returns (surface, full_desktop, connector).  full_desktop is False for the
    per-monitor ScreenCast capture and True only for the opt-in portal path;
    connector is the captured monitor's connector name (None for the portal,
    whose capture spans every monitor).  `on_connector(name)` is forwarded to
    screencast_capture (never called on the portal path); `on_frame(surface,
    full_desktop, connector)` receives the same values as the return, as early
    as they exist (on the ScreenCast path: before its teardown).
    """
    seen = {}

    def _connector(name):
        seen["connector"] = name
        if on_connector is not None:
            on_connector(name)

    def _frame(surface):
        if on_frame is not None:
            on_frame(surface, False, seen.get("connector"))

    try:
        surface, connector = screencast_capture(on_connector=_connector,
                                                on_frame=_frame,
                                                target_monitor=target_monitor)
        return surface, False, connector
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
        surface = portal_capture()
        if on_frame is not None:
            on_frame(surface, True, None)
        return surface, True, None


def compute_scale(surface_w, surface_h, logical_w, logical_h):
    """Physical-pixels-per-logical-pixel for each axis.

    Handles fractional scaling: a selection in logical coords is multiplied by
    this to index the physical-resolution capture.
    """
    sx = surface_w / logical_w if logical_w else 1.0
    sy = surface_h / logical_h if logical_h else 1.0
    return sx, sy


def selection_to_physical(sel, scale, origin_px=(0, 0)):
    """Physical-pixel rect (x, y, w, h) of a logical selection.

    Rounds each EDGE to a physical pixel, then takes the difference, so the
    size is stable under fractional scaling (no independent-rounding
    off-by-one between position and size).  This is the single source of
    truth shared by the crop and the on-screen W×H readout.
    """
    sx, sy = scale
    ox, oy = origin_px
    x, y, w, h = sel
    pleft = int(round(ox + x * sx))
    ptop = int(round(oy + y * sy))
    pw = max(1, int(round(ox + (x + w) * sx)) - pleft)
    ph = max(1, int(round(oy + (y + h) * sy)) - ptop)
    return pleft, ptop, pw, ph


def path_bbox(points):
    """Bounding rect [x, y, w, h] of a list of (x, y) points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0 = min(xs), min(ys)
    return [x0, y0, max(xs) - x0, max(ys) - y0]


def transform_path(points, old_bbox, new_bbox):
    """Map path points from one bounding box to another (move/scale/flip).

    This is what lets a polygon/lasso region be dragged and resized exactly
    like the rectangle: manipulate the bbox, then remap every point into it.
    """
    ox, oy, ow, oh = old_bbox
    nx, ny, nw, nh = new_bbox
    fx = nw / ow if ow else 1.0
    fy = nh / oh if oh else 1.0
    return [(nx + (px - ox) * fx, ny + (py - oy) * fy) for px, py in points]


_text_metrics_cache = {}


def _text_metrics(text, size):
    """(ascent, descent, x_advance, ink width) of a label, memoized: hit
    testing calls this per label per motion event."""
    key = (text, size)
    m = _text_metrics_cache.get(key)
    if m is None:
        surf = cairo.ImageSurface(cairo.FORMAT_A8, 1, 1)
        cr = cairo.Context(surf)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(size)
        ascent, descent = cr.font_extents()[:2]
        ext = cr.text_extents(text)
        m = (ascent, descent, ext.x_advance, ext.width)
        _text_metrics_cache[key] = m
    return m


def text_bbox(tnote):
    """Logical bounding box [x, y, w, h] of a committed text annotation,
    matching where draw_annotations paints it (entry padding + baseline)."""
    ascent, descent, x_advance, width = _text_metrics(tnote["text"],
                                                      tnote["size"])
    x, y = tnote["pos"]
    return [x + 6, y + 4, max(x_advance, width), ascent + descent]


def text_hit(tnote, x, y, pad=4.0):
    """True if (x, y) falls on a committed text annotation (padded a little
    so short labels stay easy to grab)."""
    bx, by, bw, bh = text_bbox(tnote)
    return bx - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad


def stroke_hit(stroke, x, y, slop=6.0):
    """True if (x, y) lies on `stroke` (within half its width plus `slop`)."""
    pts = stroke["points"]
    r = stroke["width"] / 2.0 + slop
    r2 = r * r
    if len(pts) == 1:
        px, py = pts[0]
        return (px - x) ** 2 + (py - y) ** 2 <= r2
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(
            0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / seg2))
        cx, cy = x1 + t * dx, y1 + t * dy
        if (cx - x) ** 2 + (cy - y) ** 2 <= r2:
            return True
    return False


def draw_stroke(cr, rgba, width, pts):
    cr.set_source_rgba(*rgba)
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.move_to(*pts[0])
    for p in pts[1:] or [pts[0]]:       # single click = a round dot
        cr.line_to(*p)
    cr.stroke()


def draw_annotations(cr, strokes, texts):
    """Draw committed strokes/texts in LOGICAL coordinates with cairo.  The
    SAME code paints the live preview (inside a cairo render node) and bakes
    the annotations into the crop, so what you see is exactly what you get."""
    for s in strokes:
        draw_stroke(cr, s["rgba"], s["width"], s["points"])
    for t in texts:
        cr.set_source_rgba(*t["rgba"])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(t["size"])
        ascent = cr.font_extents()[0]
        tx, ty = t["pos"]
        # ~the floating entry's own text position (padding + baseline)
        cr.move_to(tx + 6, ty + ascent + 4)
        cr.show_text(t["text"])


def annotation_bounds(strokes, texts, logical_w, logical_h):
    """Logical rect (x, y, w, h) that contains every committed annotation
    (round caps and glyph overhang included), clipped to the monitor, or None
    when there is nothing to draw.  It bounds the cairo node that previews
    them, so that node re-rasterizes an annotation-sized area, not the screen."""
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for s in strokes:
        r = s["width"] / 2.0 + 2.0
        for px, py in s["points"]:
            x0 = min(x0, px - r)
            y0 = min(y0, py - r)
            x1 = max(x1, px + r)
            y1 = max(y1, py + r)
    for t in texts:
        bx, by, bw, bh = text_bbox(t)
        pad = t["size"] * 0.5 + 8.0
        x0 = min(x0, bx - pad)
        y0 = min(y0, by - pad)
        x1 = max(x1, bx + bw + pad)
        y1 = max(y1, by + bh + pad)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(logical_w), x1), min(float(logical_h), y1)
    if not (x1 > x0 and y1 > y0):
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def crop_to_png_bytes(surface, sel, scale, origin_px=(0, 0), cursor=None,
                      mask_path=None, decorate=None):
    """Crop `surface` to the logical-coord selection `sel` = (x, y, w, h).

    `scale` = (sx, sy) physical-pixels-per-logical-pixel for the monitor being
    captured.  `origin_px` is the captured monitor's top-left position inside
    the (possibly multi-monitor) capture, in PHYSICAL pixels.  `cursor`, if
    given, is a logical (x, y) where a pointer glyph is composited (only if it
    falls inside the selection).
    `mask_path` is an optional list of logical (x, y) points forming a closed
    region (polygon/freehand selection): pixels outside it come out fully
    transparent, so the output gains an alpha channel.
    `decorate` is an optional callable(cr) invoked with the context in LOGICAL
    coordinates, used to bake annotations (pen/text) into the crop at the
    capture's full physical resolution.
    Returns PNG bytes.  Never touches disk.
    """
    sx, sy = scale
    ox, oy = origin_px
    x, y, w, h = sel
    pleft, ptop, pw, ph = selection_to_physical(sel, scale, origin_px)
    # Clamp to the capture bounds.
    sw, sh = surface.get_width(), surface.get_height()
    px = max(0, min(pleft, sw - 1))
    py = max(0, min(ptop, sh - 1))
    pw = min(pw, sw - px)
    ph = min(ph, sh - py)

    # RGB24 (no fake alpha channel) unless a freeform mask needs transparency.
    fmt = cairo.FORMAT_ARGB32 if mask_path else cairo.FORMAT_RGB24
    out = cairo.ImageSurface(fmt, pw, ph)
    cr = cairo.Context(out)
    # Logical-coordinate space: device = logical * scale + origin - crop_pos.
    cr.translate(ox - px, oy - py)
    cr.scale(sx, sy)
    if mask_path:
        cr.move_to(*mask_path[0])
        for pt in mask_path[1:]:
            cr.line_to(*pt)
        cr.close_path()
        # EVEN_ODD to match the overlay's dim preview: a self-intersecting
        # lasso must produce exactly the region the user saw as selected.
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()                    # everything outside stays transparent
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)

    cr.save()
    cr.identity_matrix()             # the base blit is 1:1 physical pixels
    cr.set_source_surface(surface, -px, -py)
    cr.get_source().set_filter(cairo.FILTER_NEAREST)  # exact pixels, no blur
    cr.paint()
    cr.restore()

    if decorate is not None:
        decorate(cr)                 # logical coords; mask clip still applies

    if cursor is not None:
        cxl, cyl = cursor
        if x <= cxl <= x + w and y <= cyl <= y + h:
            cr.save()
            cr.identity_matrix()
            _draw_pointer(cr, (ox + cxl * sx) - px, (oy + cyl * sy) - py)
            cr.restore()

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
    import subprocess          # only needed at confirm time; keep launch lean
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
        # communicate() bounds the stdin WRITE as well as the wait — a plain
        # write() to a stalled wl-copy would block forever with no timeout.
        proc.communicate(png_bytes, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("wl-copy timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"wl-copy exited with status {proc.returncode}")


def save_png(png_bytes, save_dir, filename_format, when=None):
    """Write PNG bytes to a timestamped file under `save_dir`. Returns path.

    `filename_format` may include subdirectories (e.g. '%Y/%m/shot-%H%M%S.png'),
    which are created; but it can never escape `save_dir` — an absolute path or
    '..' that would land outside falls back to the basename inside save_dir.
    """
    if when is None:
        from datetime import datetime
        when = datetime.now()
    folder = os.path.abspath(os.path.expanduser(save_dir))
    name = when.strftime(filename_format)
    path = os.path.normpath(os.path.join(folder, name))
    if path != folder and not path.startswith(folder + os.sep):
        # escaped save_dir (absolute path / '..') -> keep just the file name
        path = os.path.join(folder, os.path.basename(name) or "snapclip.png")
    os.makedirs(os.path.dirname(path) or folder, exist_ok=True)
    # 'x' = atomic create-or-fail, so two shots landing in the same second
    # can never clobber each other (an exists() pre-check would race).
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        try:
            with open(path, "xb") as fh:
                fh.write(png_bytes)
            return path
        except FileExistsError:
            path = f"{base}-{n}{ext}"
            n += 1


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


def dim_rects(sel, width, height):
    """The four rects that cover everything OUTSIDE `sel` within (0, 0, width,
    height): top and bottom bands full-width, left and right strips between
    them.  They tile the outside exactly (no overlap, no gap), which is what
    the old even-odd fill produced — but as plain GPU quads."""
    x, y, w, h = sel
    left, top = max(0.0, x), max(0.0, y)
    right, bottom = min(float(width), x + w), min(float(height), y + h)
    if right <= left or bottom <= top:              # nothing visible: dim all
        return [(0.0, 0.0, float(width), float(height))]
    rects = []
    if top > 0:
        rects.append((0.0, 0.0, float(width), top))
    if bottom < height:
        rects.append((0.0, bottom, float(width), height - bottom))
    if left > 0:
        rects.append((0.0, top, left, bottom - top))
    if right < width:
        rects.append((right, top, width - right, bottom - top))
    return rects


# ----------------------------------------------------------------------------
# Overlay rendering — GSK render nodes (GPU), one cairo node for annotations
# ----------------------------------------------------------------------------

_BADGE_FONT = Pango.FontDescription.from_string("Sans 13px")
_BADGE_BG = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.65)
_BADGE_FG = Gdk.RGBA(red=1, green=1, blue=1, alpha=1)
_BADGE_PAD = 5


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _stroke(width, dash=None):
    s = Gsk.Stroke.new(width)
    if dash:
        s.set_dash(dash)
    return s


def _path_of(points, close, extra=None):
    pb = Gsk.PathBuilder.new()
    pb.move_to(*points[0])
    for p in points[1:]:
        pb.line_to(*p)
    if extra is not None:
        pb.line_to(*extra)
    if close:
        pb.close()
    return pb.to_path()


def _rect_path(x, y, w, h):
    pb = Gsk.PathBuilder.new()
    pb.add_rect(_rect(x, y, w, h))
    return pb.to_path()


def _region_paths(st, width, height):
    """(outline, outline + full-screen rect) Gsk paths for the committed
    freeform region, cached on the region's identity (a drag replaces the
    point list, so identity is a complete change key)."""
    pts = st.region_path
    cache = st._region_cache
    if cache is not None and cache[0] is pts and cache[1] == (width, height):
        return cache[2], cache[3]
    outline = _path_of(pts, close=True)
    pb = Gsk.PathBuilder.new()
    pb.add_rect(_rect(0, 0, width, height))
    pb.move_to(*pts[0])
    for p in pts[1:]:
        pb.line_to(*p)
    pb.close()
    dim = pb.to_path()
    st._region_cache = (pts, (width, height), outline, dim)
    return outline, dim


def _annotation_node(st, width, height):
    """Committed pen strokes / text labels as ONE cairo render node covering
    just their bounding box.  Rebuilt only when an annotation is added, moved,
    erased or restored, so dragging the selection over them costs nothing."""
    key = (tuple(id(s) for s in st.strokes),
           tuple((id(t), t["pos"], t["text"], t["size"]) for t in st.texts),
           width, height)
    cache = st._annot_cache
    if cache is not None and cache[0] == key:
        return cache[1]
    node = None
    bounds = annotation_bounds(st.strokes, st.texts, width, height)
    if bounds is not None:
        snap = Gtk.Snapshot.new()
        cr = snap.append_cairo(_rect(*bounds))
        draw_annotations(cr, st.strokes, st.texts)
        node = snap.to_node()
    st._annot_cache = (key, node)
    return node


def _pending_pen_node(st, width, height):
    """The stroke being drawn right now, in its final look (same cairo code
    that will bake it), bounded to the stroke's own bbox."""
    pts = st._pending_points
    rgba, w = st._pending_pen
    r = w / 2.0 + 2.0
    x0 = max(0.0, min(p[0] for p in pts) - r)
    y0 = max(0.0, min(p[1] for p in pts) - r)
    x1 = min(float(width), max(p[0] for p in pts) + r)
    y1 = min(float(height), max(p[1] for p in pts) + r)
    if not (x1 > x0 and y1 > y0):
        return None
    snap = Gtk.Snapshot.new()
    cr = snap.append_cairo(_rect(x0, y0, x1 - x0, y1 - y0))
    draw_stroke(cr, rgba, w, pts)
    return snap.to_node()


def _draw_text(snapshot, layout, x, y, rgba):
    snapshot.save()
    snapshot.translate(Graphene.Point().init(x, y))
    snapshot.append_layout(layout, rgba)
    snapshot.restore()


def render_overlay(snapshot, st, width, height):
    """Build the overlay scene for one frame.

    `st` is the overlay state (the OverlayWindow, or a stub in tests): the
    frozen capture as `texture`, geometry (`scale`, `origin_px`, `logical_w/h`),
    the selection, region/annotation/tool state, and the cached style colours.
    Everything is appended as GSK nodes — a texture, colour quads, a border,
    path fills/strokes and a text layout — so GTK composites it on the GPU;
    only annotation previews go through cairo (see _annotation_node).
    """
    x, y, w, h = st.selection
    lw, lh = st.logical_w, st.logical_h

    # 1. frozen screen (interior shows real content => "see-through").  The
    #    physical-resolution capture is mapped onto the logical monitor rect;
    #    at 1:1 device mapping the GPU samples it exactly, at any other scale
    #    trilinear filtering keeps it smooth.
    tex = st.texture
    if tex is not None:
        sx, sy = st.scale
        ox, oy = st.origin_px
        snapshot.append_scaled_texture(
            tex, Gsk.ScalingFilter.TRILINEAR,
            _rect(-ox / sx, -oy / sy, tex.get_width() / sx, tex.get_height() / sy))

    # 2. committed annotations (dimmed outside the selection, like the
    #    screen content they sit on)
    if st.strokes or st.texts:
        node = _annotation_node(st, lw, lh)
        if node is not None:
            snapshot.append_node(node)

    # 3. dim everything OUTSIDE the selection/region (interior untouched)
    region = st.region_path
    outline = None
    if region:
        outline, dim_path = _region_paths(st, width, height)
    if st._dim > 0:
        if region:
            snapshot.append_fill(dim_path, Gsk.FillRule.EVEN_ODD, st._dim_rgba)
        else:
            for r in dim_rects(st.selection, width, height):
                snapshot.append_color(st._dim_rgba, _rect(*r))

    # 4. border along the region path / selection rect.  A stroke of width bw
    #    centred on rect(x+.5, y+.5, w, h) == a border node whose outline sits
    #    bw/2 outside that rect.
    bw = st._border_width
    bc = st._border_rgba
    if region:
        snapshot.append_stroke(outline, _stroke(bw), bc)
    else:
        # GskRoundedRect is a plain C struct (no GType): init_from_rect() must
        # be called on a Python object that stays alive until append_border
        # has copied it — chaining Gsk.RoundedRect().init_from_rect(...) hands
        # GTK a pointer into an already-freed temporary.
        outline_rect = Gsk.RoundedRect()
        outline_rect.init_from_rect(
            _rect(x + 0.5 - bw / 2, y + 0.5 - bw / 2, w + bw, h + bw), 0)
        snapshot.append_border(outline_rect, [bw, bw, bw, bw], [bc, bc, bc, bc])

    # 5. corner + edge handles — regions resize via their bbox, so they
    #    get the same handles (plus a faint dashed bbox to anchor them)
    if region and st.mode == "select":
        snapshot.append_stroke(_rect_path(x + 0.5, y + 0.5, w, h),
                               _stroke(1.0, [4.0, 4.0]), st._border_faint_rgba)
    if st.mode == "select":
        hc = st._handle_rgba
        for hx, hy in [
            (x, y), (x + w, y), (x, y + h), (x + w, y + h),
            (x + w / 2, y), (x + w / 2, y + h),
            (x, y + h / 2), (x + w, y + h / 2),
        ]:
            snapshot.append_color(hc, _rect(hx - HANDLE_DRAW, hy - HANDLE_DRAW,
                                            HANDLE_DRAW * 2, HANDLE_DRAW * 2))

    # 6. in-progress tool previews (drawn undimmed, on top)
    pts = st._pending_points
    if pts:
        if st.mode == "pen":            # live stroke in its final look
            node = _pending_pen_node(st, lw, lh)
            if node is not None:
                snapshot.append_node(node)
        else:                           # polygon / lasso outline in progress
            rubber = st.pointer if st.mode == "polygon" else None
            if len(pts) >= 2 or rubber is not None:
                snapshot.append_stroke(_path_of(pts, close=False, extra=rubber),
                                       _stroke(max(1.0, bw)), bc)
            if st.mode == "polygon":    # vertex dots
                hc = st._handle_rgba
                for p in pts:
                    snapshot.append_color(hc, _rect(p[0] - 3, p[1] - 3, 6, 6))

    # 7. selected text label (text mode): dashed grab box around it
    if st.mode == "text" and st.selected_text is not None:
        bx, by, bw_, bh_ = text_bbox(st.selected_text)
        snapshot.append_stroke(_rect_path(bx - 4.5, by - 4.5, bw_ + 9, bh_ + 9),
                               _stroke(1.0, [4.0, 3.0]), st._handle_strong_rgba)

    # 8. live W x H readout near the top-left of the selection
    pw, ph = st._readout_px()
    layout = st.pango_layout("%d × %d" % (pw, ph))
    ink = layout.get_pixel_extents()[0]
    pad = _BADGE_PAD
    bw_, bh_ = ink.width + pad * 2, ink.height + pad * 2
    by = y - bh_ - 4
    if by < 0:                       # not enough room above -> put inside
        by = y + 4
    bx = max(0, min(x, lw - bw_))
    snapshot.append_color(_BADGE_BG, _rect(bx, by, bw_, bh_))
    _draw_text(snapshot, layout, bx + pad - ink.x, by + pad - ink.y, _BADGE_FG)


# ----------------------------------------------------------------------------
# Tool icons — drawn with cairo so they exist on every icon theme and follow
# the button's CSS colour (hover/checked states included).
# ----------------------------------------------------------------------------


def _icon_polygon(cr, c):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
    cr.set_line_width(1.6)
    for i, p in enumerate([(8, 1.8), (14.4, 6.4), (12, 14.2),
                           (4, 14.2), (1.6, 6.4)]):
        (cr.move_to if i == 0 else cr.line_to)(*p)
    cr.close_path()
    cr.stroke()


def _icon_lasso(cr, c):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
    cr.set_line_width(1.6)
    cr.save()
    cr.translate(8, 6.5)
    cr.scale(1.0, 0.72)
    cr.arc(0, 0, 5.8, 0.6, 0.25 + 2 * 3.14159)   # open loop
    cr.restore()
    cr.stroke()
    cr.move_to(12.5, 10.5)                        # dangling tail
    cr.curve_to(11.5, 12.5, 9.5, 13.0, 8.0, 14.5)
    cr.stroke()


def _icon_pen(cr, c):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
    cr.set_line_width(2.6)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(5.2, 10.8)
    cr.line_to(12.8, 3.2)
    cr.stroke()
    cr.move_to(2.2, 13.8)                         # nib
    cr.line_to(3.2, 10.4)
    cr.line_to(5.6, 12.8)
    cr.close_path()
    cr.fill()


def _icon_eraser(cr, c):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
    cr.set_line_width(1.6)
    cr.save()
    cr.translate(8, 7)
    cr.rotate(-0.62)
    cr.rectangle(-2.6, -4.6, 5.2, 9.2)
    cr.move_to(-2.6, 1.2)                         # rubber/ferrule split
    cr.line_to(2.6, 1.2)
    cr.restore()
    cr.stroke()
    cr.move_to(4, 14.6)                           # the swept line
    cr.line_to(14, 14.6)
    cr.stroke()


def _icon_text(cr, c):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(13)
    ext = cr.text_extents("T")
    cr.move_to(8 - ext.width / 2 - ext.x_bearing,
               8 - ext.height / 2 - ext.y_bearing)
    cr.show_text("T")


TOOL_ICONS = {"polygon": _icon_polygon, "lasso": _icon_lasso,
              "pen": _icon_pen, "eraser": _icon_eraser, "text": _icon_text}


def tool_icon_widget(kind):
    area = Gtk.DrawingArea()
    area.set_content_width(16)
    area.set_content_height(16)
    draw = TOOL_ICONS[kind]
    area.set_draw_func(lambda a, cr, w, h: draw(cr, a.get_color()))
    return area


# ----------------------------------------------------------------------------
# GTK overlay window
# ----------------------------------------------------------------------------


class _Canvas(Gtk.Widget):
    """The full-screen drawing surface: a bare widget whose snapshot is the
    GSK scene built by render_overlay (no cairo backing surface, no per-frame
    software blit or texture re-upload — the frozen frame lives on the GPU)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def do_measure(self, orientation, _for_size):
        # A full-monitor minimum/natural size guarantees the window measures
        # to the monitor even before the fullscreen configure arrives.
        size = self.win.logical_w if orientation == Gtk.Orientation.HORIZONTAL \
            else self.win.logical_h
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        render_overlay(snapshot, self.win, self.get_width(), self.get_height())


class OverlayWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config, monitor):
        super().__init__(application=app)
        self.app = app
        self.config = config
        self.surface = None             # physical-res capture (for cropping)
        self.texture = None             # the same frame, for the GPU preview

        self.set_decorated(False)
        self.add_css_class("snapclip-overlay")

        # The window is built (and realized) BEFORE the capture lands, on the
        # monitor Mutter reported as primary; set_capture() finalises scale and
        # origin once the frame is known and re-targets the monitor if the
        # capture turned out to cover a different one.
        self.monitor = monitor
        geo = monitor.get_geometry()
        self.logical_w, self.logical_h = geo.width, geo.height
        self.scale = (1.0, 1.0)
        self.origin_px = (0, 0)
        # Request the full monitor size up front.  Do NOT mark the window
        # non-resizable: on Wayland that makes GTK reject the compositor's
        # fullscreen configure and the window collapses to its 200x200 minimum.
        self.set_default_size(self.logical_w, self.logical_h)

        # Parse configured colours/sizes once — rendering runs per frame.
        self.refresh_style()

        # Initial selection.
        self.selection = self._initial_selection()
        self.pointer = (self.logical_w / 2, self.logical_h / 2)

        self._drag_mode = None
        self._drag_origin = None
        self._drag_anchor = None
        self._drag_moved = False        # did this drag actually move? (vs a click)
        self._prev_selection = None     # box to restore from a full-monitor toggle
        self._hover_zone = None         # last zone the cursor was set for
        self._done = False  # guard against double actions

        # Optional tools (all off by default). mode "select" = the stock
        # rectangle behaviour; a region path replaces the rect until a new
        # drag/double-click clears it; annotations bake into the crop.
        self.mode = "select"            # select | polygon | lasso | pen | text
        self.region_path = None         # committed freeform region (logical pts)
        self._pending_points = []       # in-progress polygon/lasso/pen points
        self._pending_pen = None        # (rgba, width) of the stroke being drawn
        self.strokes = []               # committed pen strokes
        self.texts = []                 # committed text annotations
        self._undo = []                 # annotation kinds, in commit order
        self._text_entry = None         # floating Gtk.Entry while typing
        self._text_pos = None
        self.selected_text = None       # label picked in text mode
        self._drag_text = None          # (label, start pos) while dragging one
        self._mode_sync = False         # guards toggle-button feedback loops

        # Render caches (see _region_paths / _annotation_node).
        self._region_cache = None
        self._annot_cache = None
        self._layout_cache = (None, None)
        self._first_paint_id = None     # one-shot toolbar re-place after map

        # Drawing surface.
        self.area = _Canvas(self)

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

        self.connect("realize", lambda *_: self.fullscreen_on_monitor(self.monitor))
        GLib.idle_add(self._reposition_toolbar)

    # -- setup helpers -------------------------------------------------------

    @staticmethod
    def pick_monitor(display, connector):
        """The Gdk monitor whose connector matches the captured one, else 0.

        (GTK4 removed Gdk.Monitor.is_primary; matching the connector Mutter
        called primary keeps the overlay on the same screen we captured.)
        """
        monitors = display.get_monitors()
        if connector:
            for i in range(monitors.get_n_items()):
                m = monitors.get_item(i)
                if m.get_connector() == connector:
                    return m
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

    def retarget(self, monitor):
        """Move the (still unmapped) window to `monitor` if it is not already
        there: the window is built on the first monitor before Mutter reports
        which one is primary / which one the capture covers."""
        if monitor.get_connector() == self.monitor.get_connector():
            return
        self.monitor = monitor
        geo = monitor.get_geometry()
        self.logical_w, self.logical_h = geo.width, geo.height
        self.set_default_size(self.logical_w, self.logical_h)
        self.area.queue_resize()
        self.selection = self._initial_selection()
        self._prev_selection = None
        self.pointer = (self.logical_w / 2, self.logical_h / 2)
        self.fullscreen_on_monitor(monitor)
        self._reposition_toolbar()

    def set_capture(self, surface, texture, full_desktop=False, connector=None):
        """Attach the captured frame: fix the monitor, derive scale/origin."""
        display = self.get_display()
        monitors = display.get_monitors()
        monitor = self.pick_monitor(display, connector)
        self.retarget(monitor)
        geo = monitor.get_geometry()
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
        self.surface = surface
        self.texture = texture
        self.area.queue_draw()
        self._reposition_toolbar()      # the readout depends on the scale

    def refresh_style(self):
        """Cache the parsed config values rendering needs; config is sanitized,
        so per-frame re-parsing of colour strings would be pure overhead."""
        bc = Gdk.RGBA(); bc.parse(self.config["border_color"])
        hc = Gdk.RGBA(); hc.parse(self.config["handle_color"])
        self._border_rgba = bc
        self._handle_rgba = hc
        self._border_faint_rgba = Gdk.RGBA(red=bc.red, green=bc.green,
                                           blue=bc.blue, alpha=0.55)
        self._handle_strong_rgba = Gdk.RGBA(red=hc.red, green=hc.green,
                                            blue=hc.blue, alpha=0.9)
        self._border_width = float(self.config["border_width"])
        self._dim = float(self.config["dim_opacity"])
        self._dim_rgba = Gdk.RGBA(red=0, green=0, blue=0, alpha=self._dim)

    def pango_layout(self, text):
        """The badge layout for `text`, reused while the readout is unchanged."""
        cached_text, layout = self._layout_cache
        if cached_text != text:
            layout = self.area.create_pango_layout(text)
            layout.set_font_description(_BADGE_FONT)
            self._layout_cache = (text, layout)
        return layout

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
        # a configurable fraction of the screen (sanitized+clamped at load).
        pct = self.config["default_size_pct"]
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

        copy_tip = "Copy to clipboard (Enter)"
        if self.config.get("always_save"):
            copy_tip += " — also saves a PNG (always-save is on)"
        button("edit-copy-symbolic", "Copy", copy_tip,
               lambda *_: self.do_copy())
        button("document-save-symbolic", "Save", "Save to disk + copy (S)",
               lambda *_: self.do_save())

        # Optional tools appear only when enabled in settings; the stock bar
        # stays exactly Copy / Save / Cancel.  The eraser rides along with the
        # pen (no separate setting — it only erases pen strokes).
        self._tool_buttons = {}
        tools = [
            ("polygon", "tool_polygon", "Poly",
             "Polygon selection — click corners; double-click or Enter closes"),
            ("lasso", "tool_lasso", "Lasso", "Freehand selection — drag a loop"),
            ("pen", "tool_pen", "Pen", "Draw on the shot — Ctrl+Z undoes"),
            ("eraser", "tool_pen", "Erase",
             "Erase pen strokes — click or drag over them (Ctrl+Z restores)"),
            ("text", "tool_text", "Text", "Click to place text — Ctrl+Z undoes"),
        ]
        enabled = [t for t in tools if self.config.get(t[1])]
        if enabled:
            bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            for key, _gate, label, tip in enabled:
                tb = Gtk.ToggleButton()
                content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=5)
                content.append(tool_icon_widget(key))
                content.append(Gtk.Label(label=label))
                tb.set_child(content)
                tb.set_tooltip_text(tip)
                tb.set_focusable(False)
                tb.connect("toggled", self._on_tool_toggled, key)
                bar.append(tb)
                self._tool_buttons[key] = tb

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
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

    def rebuild_toolbar(self):
        """Settings toggled a tool on/off: swap in a freshly built toolbar."""
        self.overlay.remove_overlay(self.toolbar)
        self.toolbar = self._build_toolbar()
        self.overlay.add_overlay(self.toolbar)
        gate = {"eraser": "tool_pen"}.get(self.mode, "tool_" + self.mode)
        if self.mode != "select" and not self.config.get(gate):
            self._set_mode("select")    # the active tool was just disabled
        self._sync_tool_buttons()
        self._reposition_toolbar()

    # -- tool modes ----------------------------------------------------------

    def _set_mode(self, mode):
        if mode == self.mode:
            return
        self._remove_text_entry(commit=True)   # don't orphan an open entry
        self.mode = mode
        self._pending_points = []       # drop any half-drawn polygon/stroke
        self.selected_text = None
        self._drag_text = None
        self._sync_tool_buttons()
        self._hover_zone = None         # re-derive the cursor on next motion
        if mode == "select":
            self._set_cursor("default")
        else:
            self._set_cursor({"text": "text", "eraser": "cell"}
                             .get(mode, "crosshair"))
        self.area.queue_draw()

    def _sync_tool_buttons(self):
        self._mode_sync = True
        try:
            for key, btn in self._tool_buttons.items():
                btn.set_active(key == self.mode)
        finally:
            self._mode_sync = False

    def _on_tool_toggled(self, btn, key):
        if self._mode_sync:
            return
        if btn.get_active():
            self._set_mode(key)
        elif self.mode == key:
            self._set_mode("select")

    def _clamp_point(self, x, y):
        return (max(0.0, min(x, self.logical_w)),
                max(0.0, min(y, self.logical_h)))

    def _set_region(self, points):
        pts = [self._clamp_point(*p) for p in points]
        # Normalise to a >= MIN_SIZE bbox NOW: later moves/resizes all go
        # through _clamp_bbox, so committing a tinier bbox would let the very
        # first nudge silently stretch the shape.
        bbox = path_bbox(pts)
        target = self._clamp_bbox(bbox)
        if target != bbox:
            pts = transform_path(pts, bbox, target)
        self.region_path = pts
        self.selection = list(target)
        self.area.queue_draw()
        self._reposition_toolbar()

    def _close_polygon(self):
        if len(self._pending_points) >= 3:
            self._set_region(self._pending_points)
            self._set_mode("select")    # region committed; Enter now copies
        # with < 3 corners there is nothing to close — keep collecting

    # -- annotations (pen / text) ---------------------------------------------

    def _commit_stroke(self, points):
        rgba = Gdk.RGBA(); rgba.parse(self.config["pen_color"])
        stroke = {
            "rgba": (rgba.red, rgba.green, rgba.blue, rgba.alpha),
            "width": float(self.config["pen_width"]),
            "points": list(points),
        }
        self.strokes.append(stroke)
        self._undo.append(("stroke", stroke))

    def _erase_at(self, x, y):
        """Object eraser: remove every pen stroke under (x, y)."""
        for stroke in [s for s in self.strokes if stroke_hit(s, x, y)]:
            self.strokes.remove(stroke)
            self._undo.append(("erase", stroke))
            self.area.queue_draw()

    def _undo_annotation(self):
        if not self._undo:
            return
        kind, obj = self._undo.pop()
        if obj is self.selected_text:   # never leave a dangling selection
            self.selected_text = None
        if kind == "stroke":
            if obj in self.strokes:
                self.strokes.remove(obj)
        elif kind == "text":
            if obj in self.texts:
                self.texts.remove(obj)
        elif kind == "erase":           # undoing an erase restores the stroke
            self.strokes.append(obj)
        elif kind == "del_text":        # undoing a delete restores the label
            self.texts.append(obj)
        self.area.queue_draw()

    def _place_text_entry(self, x, y):
        self._remove_text_entry(commit=True)    # commit any open one first
        entry = Gtk.Entry()
        entry.add_css_class("snapclip-textedit")
        entry.set_size_request(180, -1)
        entry.set_halign(Gtk.Align.START)
        entry.set_valign(Gtk.Align.START)
        # Clamp ONCE and share the result: the committed text must bake at
        # the same spot the preview entry showed, even near screen edges.
        ex = int(max(0, min(x, self.logical_w - 190)))
        ey = int(max(0, min(y, self.logical_h - 44)))
        entry.set_margin_start(ex)
        entry.set_margin_top(ey)
        entry.connect("activate",
                      lambda *_: self._remove_text_entry(commit=True))
        self.overlay.add_overlay(entry)
        entry.grab_focus()
        self._text_entry = entry
        self._text_pos = (ex, ey)

    def _text_at(self, x, y):
        for t in reversed(self.texts):      # topmost (most recent) first
            if text_hit(t, x, y):
                return t
        return None

    def _remove_text_entry(self, commit):
        entry = self._text_entry
        if entry is None:
            return
        text = entry.get_text().strip()
        self._text_entry = None
        self.overlay.remove_overlay(entry)
        if commit and text:
            rgba = Gdk.RGBA(); rgba.parse(self.config["text_color"])
            tnote = {
                "rgba": (rgba.red, rgba.green, rgba.blue, rgba.alpha),
                "size": float(self.config["text_size"]),
                "text": text,
                "pos": self._text_pos,
            }
            self.texts.append(tnote)
            self._undo.append(("text", tnote))
        self.area.queue_draw()

    def _draw_annotations(self, cr):
        """Bake committed strokes/texts into the crop (logical coords) — the
        same routine the on-screen preview node uses."""
        draw_annotations(cr, self.strokes, self.texts)

    def _readout_px(self):
        """Physical pixel size of the current selection — the same function
        the crop uses, so the readout can never disagree with the PNG."""
        return selection_to_physical(self.selection, self.scale,
                                     self.origin_px)[2:]

    # -- interaction ---------------------------------------------------------

    def on_motion(self, _c, mx, my):
        self.pointer = (mx, my)
        if self.mode == "polygon" and self._pending_points:
            self.area.queue_draw()          # rubber-band line follows pointer
            return
        if self.mode == "text" and self._drag_text is None:
            hover = "move" if self._text_at(mx, my) else "text"
            if hover != self._hover_zone:   # labels are grabbable: show it
                self._hover_zone = hover
                self._set_cursor(hover)
            return
        if self.mode != "select":
            return                          # tool modes keep their own cursor
        if self._drag_mode is None:
            zone = hit_zone(self.selection, mx, my)   # regions use their bbox
            if zone != self._hover_zone:    # new cursor only on zone change
                self._hover_zone = zone
                self._set_cursor(CURSOR_FOR_ZONE.get(zone, "default"))

    def _set_cursor(self, name):
        try:
            self.set_cursor(Gdk.Cursor.new_from_name(name, None))
        except Exception:
            pass

    def on_drag_begin(self, gesture, sx, sy):
        if self.mode == "pen":
            rgba = Gdk.RGBA(); rgba.parse(self.config["pen_color"])
            self._pending_pen = ((rgba.red, rgba.green, rgba.blue, rgba.alpha),
                                 float(self.config["pen_width"]))
            self._drag_anchor = (sx, sy)
            self._pending_points = [(sx, sy)]
            self.area.queue_draw()
            return
        if self.mode == "eraser":
            self._drag_anchor = (sx, sy)
            self._erase_at(sx, sy)
            return
        if self.mode == "lasso":
            self._drag_anchor = (sx, sy)
            self._pending_points = [self._clamp_point(sx, sy)]
            return
        if self.mode == "text":
            hit = self._text_at(sx, sy)
            if hit is not None:         # drag an existing label to move it
                self._drag_text = (hit, tuple(hit["pos"]))
            return
        if self.mode != "select":       # polygon places points on click
            return
        self._drag_origin = list(self.selection)
        self._drag_moved = False
        # a freeform region moves/resizes exactly like the rect: same zones on
        # its bbox; dragging OUTSIDE rubber-bands a fresh rectangle (the
        # universal escape hatch back to rect mode).
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
        if self.mode in ("pen", "lasso"):
            if self._pending_points:
                ax, ay = self._drag_anchor
                pt = (ax + ox, ay + oy)
                self._pending_points.append(
                    pt if self.mode == "pen" else self._clamp_point(*pt))
                self.area.queue_draw()
            return
        if self.mode == "eraser":
            ax, ay = self._drag_anchor
            self._erase_at(ax + ox, ay + oy)
            return
        if self.mode == "text":
            if self._drag_text is not None:
                tnote, (px0, py0) = self._drag_text
                tnote["pos"] = (max(0.0, min(px0 + ox, self.logical_w - 24)),
                                max(0.0, min(py0 + oy, self.logical_h - 24)))
                self.area.queue_draw()
            return
        if self._drag_mode is None:
            return
        self._drag_moved = True
        mode = self._drag_mode
        if mode == "new":
            self.region_path = None     # rubber-banding replaces any region
            ax, ay = self._drag_anchor
            self.selection = [ax, ay, ox, oy]
            self._clamp_selection_soft()
        elif mode == Z_INSIDE:
            x, y, w, h = self._drag_origin
            nx = max(0, min(x + ox, self.logical_w - w))
            ny = max(0, min(y + oy, self.logical_h - h))
            self._set_selection_bbox([nx, ny, w, h])
        else:
            self._set_selection_bbox(
                resize_rect(self._drag_origin, mode, ox, oy))
        self.pointer = (self._drag_origin[0] + ox, self._drag_origin[1] + oy) \
            if mode != "new" else (self._drag_anchor[0] + ox,
                                   self._drag_anchor[1] + oy)
        self.area.queue_draw()
        self._reposition_toolbar()

    def _clamp_bbox(self, box):
        x, y, w, h = normalize(box)
        # Pull the origin inside first, leaving room for at least MIN_SIZE, so
        # enforcing the minimum below can't push the far edge past the monitor.
        x = max(0, min(x, self.logical_w - MIN_SIZE))
        y = max(0, min(y, self.logical_h - MIN_SIZE))
        w = max(MIN_SIZE, min(w, self.logical_w - x))
        h = max(MIN_SIZE, min(h, self.logical_h - y))
        return [x, y, w, h]

    def _clamp_selection_soft(self):
        self.selection = self._clamp_bbox(self.selection)

    def _set_selection_bbox(self, target):
        """Move/resize to `target` bbox; a freeform region follows along."""
        target = self._clamp_bbox(target)
        if self.region_path is not None:
            self.region_path = transform_path(
                self.region_path, path_bbox(self.region_path), target)
        self.selection = list(target)

    def on_drag_end(self, gesture, ox, oy):
        if self.mode == "pen":
            if self._pending_points:
                self._commit_stroke(self._pending_points)
                self._pending_points = []
                self.area.queue_draw()
            return
        if self.mode == "lasso":
            pts, self._pending_points = self._pending_points, []
            if len(pts) >= 3:
                self._set_region(pts)
                self._set_mode("select")    # loop committed; Enter now copies
            else:
                self.area.queue_draw()
            return
        if self.mode == "text":
            self._drag_text = None
            return
        if self.mode != "select":
            return
        moved = self._drag_moved
        self._drag_mode = None
        self._drag_moved = False
        self._hover_zone = None         # re-evaluate the cursor on next motion
        if moved:                       # a real drag — finalise it
            self._clamp_selection_soft()
            self.area.queue_draw()
            self._reposition_toolbar()
        # a click (no movement) leaves the selection exactly as it was

    def on_pressed(self, gesture, n_press, x, y):
        if self.mode == "polygon":
            if n_press == 2:
                # The double-click's own first press (n_press==1) already
                # appended a vertex.  If it sits on the previous corner the
                # user meant "close HERE" — drop the duplicate; if it is a new
                # spot they meant "final corner + close" — keep it.
                pts = self._pending_points
                if len(pts) >= 2 and abs(pts[-1][0] - pts[-2][0]) <= 5 \
                        and abs(pts[-1][1] - pts[-2][1]) <= 5:
                    pts.pop()
                self._close_polygon()
            else:
                self._pending_points.append(self._clamp_point(x, y))
                self.area.queue_draw()
            return
        if self.mode == "text":
            if n_press == 1:
                hit = self._text_at(x, y)
                if hit is not None:             # click a label: select it
                    self._remove_text_entry(commit=True)
                    self.selected_text = hit
                    self.area.queue_draw()
                else:                           # empty space: type a new one
                    self.selected_text = None
                    self._place_text_entry(x, y)
            return
        if self.mode != "select":
            return
        if n_press == 2:
            self._toggle_full_selection()

    def _toggle_full_selection(self):
        """Double-click: snap to the whole monitor; double-click again restore."""
        self.region_path = None         # a rect selection replaces any region
        self.selection, self._prev_selection = toggle_full_selection(
            self.selection, self._prev_selection,
            self.logical_w, self.logical_h)
        # cancel any in-flight drag so a stray drag_end can't fight us
        self._drag_mode = None
        self._drag_moved = False
        self._hover_zone = None
        self.area.queue_draw()
        self._reposition_toolbar()

    def _reposition_toolbar(self):
        x, y, w, h = self.selection
        tb = self.toolbar
        tb_w, tb_h = tb.get_width(), tb.get_height()
        if not tb_w or not tb_h:
            # Not allocated yet (the window is built and positioned before it
            # is mapped): fall back to the measured natural size.  measure()
            # includes the widget's own margins — the very thing this method
            # sets — so strip them, or a second call would see a toolbar
            # inflated by its previous position.
            nat_w = tb.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            nat_h = tb.measure(Gtk.Orientation.VERTICAL, -1)[1]
            tb_w = tb_w or max(1, nat_w - tb.get_margin_start()
                               - tb.get_margin_end()) or 280
            tb_h = tb_h or max(1, nat_h - tb.get_margin_top()
                               - tb.get_margin_bottom()) or 40
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
        # Esc peels back one layer at a time: text entry -> tool mode -> app.
        if keyval == Gdk.KEY_Escape:
            if self._text_entry is not None:
                self._remove_text_entry(commit=False)
                return True
            if self.mode != "select":
                self._set_mode("select")    # drops any half-drawn shape too
                return True
            self.do_cancel()
            return True
        if self._text_entry is not None:
            return False                    # typing belongs to the entry
        # Ctrl+C is the one modifier combo everyone expects to copy.
        if (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_c, Gdk.KEY_C)):
            self.do_copy()
            return True
        if (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_z, Gdk.KEY_Z)):
            self._undo_annotation()
            return True
        # Ignore the other action keys when Ctrl/Alt/Super are held, so e.g.
        # Ctrl+S (a habit) doesn't trigger Save. Shift is allowed (capital S).
        if state & (Gdk.ModifierType.CONTROL_MASK
                    | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK):
            return False
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.mode == "polygon":
                self._close_polygon()       # needs >= 3 corners; else no-op
                return True
            self.do_copy()
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.do_save()
            return True
        # Delete removes the selected text label (Ctrl+Z brings it back).
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) \
                and self.selected_text is not None:
            tnote, self.selected_text = self.selected_text, None
            if tnote in self.texts:
                self.texts.remove(tnote)
                self._undo.append(("del_text", tnote))
            self.area.queue_draw()
            return True
        # Arrow keys: pixel-precise adjustment. Plain = move, Shift = resize.
        # Freeform regions follow their bbox just like the rectangle.
        delta = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
                 Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}.get(keyval)
        if delta:
            if self.mode == "select":
                x, y, w, h = self.selection
                if state & Gdk.ModifierType.SHIFT_MASK:
                    w, h = w + delta[0], h + delta[1]
                else:
                    x, y = x + delta[0], y + delta[1]
                self._set_selection_bbox([x, y, w, h])
                self.area.queue_draw()
                self._reposition_toolbar()
            return True
        return False

    # -- actions -------------------------------------------------------------

    def _png_bytes(self):
        if self.region_path is None:
            self._clamp_selection_soft()    # a region's bbox is already valid
        cursor = self.pointer if self.config.get("include_cursor") else None
        decorate = self._draw_annotations \
            if (self.strokes or self.texts) else None
        return crop_to_png_bytes(
            self.surface, self.selection, self.scale,
            origin_px=self.origin_px, cursor=cursor,
            mask_path=self.region_path, decorate=decorate,
        )

    def _remember(self):
        if self.config.get("remember_selection"):
            self.config["last_selection"] = [int(v) for v in self.selection]
            save_config(self.config)

    def _fail(self, msg):
        """Surface a failure instead of silently 'succeeding' and closing.

        The fullscreen overlay would hide any stderr message, so on failure we
        bring the window back, show a banner, and re-arm so the user can retry
        or press Esc.  A non-zero process exit is also recorded for callers.
        """
        print(f"snapclip: {msg}", file=sys.stderr)
        self.app.had_error = True
        self.error_label.set_text(f"⚠  {msg}   —   press Esc to close, or retry")
        self.error_label.set_visible(True)
        self._done = False
        self.area.queue_draw()
        self.present()                  # it was hidden for the attempt

    # NOTE on closing: the window quits visible and GNOME plays its normal
    # window-close animation on the frozen shot.  We deliberately do NOT try
    # to suppress it: every trick (opacity 0, unmap-first, committing a
    # transparent frame and waiting for presentation feedback) either gets
    # optimized away by GTK or runs into Mutter's black backdrop behind
    # fullscreen windows / the direct-scanout handoff, producing black
    # flashes worse than the animation itself.  The compositor owns the
    # close; let it.

    def _confirm(self, save):
        if self._done:
            return
        # Commit work-in-progress first: Ctrl+C / S / the toolbar buttons must
        # capture what is on screen, not a stale earlier selection.
        self._remove_text_entry(commit=True)   # keep what was being typed
        if self.mode == "polygon" and len(self._pending_points) >= 3:
            self._close_polygon()
        self._done = True
        self._finish(save)

    def _finish(self, save):
        # Hide FIRST: GNOME starts its close animation on the frozen shot at
        # once, and the crop / PNG encode / wl-copy work below runs underneath
        # that animation instead of holding the overlay on screen while the
        # user waits.  On failure the window comes straight back (see _fail).
        self.set_visible(False)
        self.get_display().flush()      # push the unmap out before we block
        GLib.idle_add(self._finish_work, save)

    def _finish_work(self, save):
        try:
            png = self._png_bytes()
            copy_png_to_clipboard(png)
        except Exception as exc:
            self._fail(f"copy failed: {exc}")
            return False
        if save:
            try:
                path = save_png(png, self.config["save_dir"],
                                self.config["filename_format"])
                print(f"snapclip: saved {path}")
            except Exception as exc:
                self._fail(f"saved to clipboard but writing the file "
                           f"failed: {exc}")
                return False
        self.app.had_error = False      # a prior failed attempt is now resolved
        self._remember()
        self.app.quit()
        return False

    def do_copy(self):
        # "Always save" makes plain Copy also keep a file on disk.
        self._confirm(save=bool(self.config.get("always_save")))

    def do_save(self):
        self._confirm(save=True)

    def do_cancel(self):
        if self._done:
            return
        self._done = True
        self._remove_text_entry(commit=False)
        self._remember()
        self.app.quit()

    def present_overlay(self):
        """Map the window.  The toolbar was placed from a pre-map measurement;
        once the first frame has allocated it at its real size (icons and
        fonts finish loading on map), place it again."""
        self.present()
        clock = self.get_frame_clock()
        if clock is not None and self._first_paint_id is None:
            self._first_paint_id = clock.connect("after-paint",
                                                 self._on_first_paint)

    def _on_first_paint(self, clock):
        clock.disconnect(self._first_paint_id)
        self._first_paint_id = None
        self._reposition_toolbar()

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
        self.set_default_size(470, -1)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        grid.set_margin_top(14); grid.set_margin_bottom(14)
        grid.set_margin_start(16); grid.set_margin_end(16)
        self.set_child(grid)
        row = 0

        def section(title, subtitle=None):
            nonlocal row
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.add_css_class("snapclip-section")
            if row:
                lbl.set_margin_top(12)
            grid.attach(lbl, 0, row, 2, 1)
            row += 1
            if subtitle:
                sub = Gtk.Label(label=subtitle, xalign=0)
                sub.add_css_class("snapclip-subtle")
                sub.set_wrap(True)
                grid.attach(sub, 0, row, 2, 1)
                row += 1

        def add(label, widget):
            nonlocal row
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(False)
            grid.attach(lbl, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            row += 1

        def switch(active, handler):
            sw = Gtk.Switch()
            sw.set_active(bool(active))
            sw.set_halign(Gtk.Align.START)
            sw.set_valign(Gtk.Align.CENTER)
            sw.connect("notify::active", handler)
            return sw

        def color_button(color, handler):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            rgba = Gdk.RGBA(); rgba.parse(color)
            btn.set_rgba(rgba)
            btn.connect("notify::rgba", handler)
            return btn

        def spin(lo, hi, value, handler, tip):
            sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
            sp.set_value(value)
            sp.set_tooltip_text(tip)
            sp.connect("value-changed", handler)
            return sp

        def text_popover(value, handler, width_chars=44):
            """Click-to-edit text: the button shows the current value; clicking
            opens a wide entry in a popover that dismisses on click-out."""
            btn = Gtk.MenuButton()
            lbl = Gtk.Label(label=value, xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            lbl.set_max_width_chars(26)
            btn.set_child(lbl)
            pop = Gtk.Popover()
            entry = Gtk.Entry()
            entry.set_text(value)
            entry.set_width_chars(width_chars)
            entry.connect("changed",
                          lambda e: (handler(e), lbl.set_label(e.get_text())))
            entry.connect("activate", lambda *_: pop.popdown())
            pop.set_child(entry)
            btn.set_popover(pop)
            return btn, entry

        # ---- Selection ------------------------------------------------------
        section("Selection")
        monitors = _list_monitors()
        self.monitor_items = ["Primary"]
        self.monitor_values = ["primary"]
        selected_idx = 0
        current_cfg = str(self.cfg.get("default_monitor", "primary")).strip()

        for idx, m in enumerate(monitors):
            label = f"{m['index']}: {m['connector']} ({m['name']})"
            self.monitor_items.append(label)
            self.monitor_values.append(m["connector"])
            if current_cfg.lower() == m["connector"].lower():
                selected_idx = idx + 1
            elif current_cfg.isdigit() and int(current_cfg) == m["index"]:
                selected_idx = idx + 1

        self.monitor_dd = Gtk.DropDown.new_from_strings(self.monitor_items)
        self.monitor_dd.set_selected(selected_idx)
        self.monitor_dd.connect("notify::selected", self._on_monitor_selected)
        self.monitor_dd.set_tooltip_text(
            "Default monitor to capture when no -m/--monitor flag is specified")
        add("Default monitor", self.monitor_dd)

        self.color_btn = color_button(self.cfg["border_color"], self._on_color)
        add("Border colour", self.color_btn)
        self.width_spin = spin(1, 12, self.cfg["border_width"],
                               self._on_width, "Outline thickness (px)")
        add("Border width", self.width_spin)
        self.dim_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 0.8, 0.05)
        self.dim_scale.set_value(self.cfg["dim_opacity"])
        self.dim_scale.set_draw_value(True)
        self.dim_scale.connect("value-changed", self._on_dim)
        add("Outside dim", self.dim_scale)
        self.size_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.1, 1.0, 0.05)
        self.size_scale.set_value(self.cfg["default_size_pct"])
        self.size_scale.set_draw_value(True)
        self.size_scale.set_tooltip_text(
            "Size of the initial box when not remembering the last selection")
        self.size_scale.connect("value-changed", self._on_default_size)
        add("Default size (× screen)", self.size_scale)
        self.remember_sw = switch(self.cfg["remember_selection"],
                                  self._on_remember)
        self.remember_sw.set_tooltip_text(
            "Reopen with your previous selection box instead of a fresh "
            "centered one")
        add("Remember last selection", self.remember_sw)

        # ---- Output ---------------------------------------------------------
        section("Output")
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dir_btn, self.dir_entry = text_popover(self.cfg["save_dir"],
                                               self._on_dir)
        dir_btn.set_hexpand(True)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse)
        folder_box.append(dir_btn)
        folder_box.append(browse)
        add("Save folder", folder_box)
        fmt_btn, self.fmt_entry = text_popover(self.cfg["filename_format"],
                                               self._on_fmt)
        add("Filename format", fmt_btn)
        self.always_sw = switch(self.cfg["always_save"], self._on_always_save)
        self.always_sw.set_tooltip_text(
            "Copy (Enter) also writes a PNG to the save folder")
        add("Always save a copy", self.always_sw)
        self.cursor_sw = switch(self.cfg["include_cursor"], self._on_cursor)
        self.cursor_sw.set_tooltip_text(
            "Composite a mouse-pointer glyph into the shot where the cursor is "
            "(best-effort — the capture itself has no pointer)")
        add("Include mouse cursor", self.cursor_sw)
        self.quicksave_sw = switch(self.cfg["quick_save_double_tap"],
                                   self._on_quick_save)
        self.quicksave_sw.set_tooltip_text(
            "Double-tap your snapclip hotkey to instantly save the whole screen "
            "with no overlay. While on, a single tap waits briefly for a "
            "possible second tap before the overlay appears.")
        add("Quick-save on double-tap", self.quicksave_sw)

        # ---- Tools ----------------------------------------------------------
        section("Tools",
                "Extra toolbar buttons — all off by default so the overlay "
                "stays clean")

        def tool_switch(key, tip):
            sw = switch(self.cfg[key], lambda sw, _p: self._on_tool(key, sw))
            sw.set_tooltip_text(tip)
            return sw

        self.poly_sw = tool_switch(
            "tool_polygon",
            "Adds the Poly button — click corners for a polygon selection; "
            "the shot comes out transparent outside the shape")
        add("Polygon selection", self.poly_sw)
        self.lasso_sw = tool_switch(
            "tool_lasso",
            "Adds the Lasso button — drag a freehand loop; the shot comes out "
            "transparent outside the shape")
        add("Freehand selection", self.lasso_sw)

        pen_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.pen_sw = tool_switch(
            "tool_pen",
            "Adds the Pen and Erase buttons — draw strokes onto the shot; "
            "Ctrl+Z undoes")
        self.pen_color_btn = color_button(self.cfg["pen_color"],
                                          self._on_pen_color)
        self.pen_width_spin = spin(1, 16, self.cfg["pen_width"],
                                   self._on_pen_width, "Stroke width (px)")
        pen_box.append(self.pen_sw)
        pen_box.append(self.pen_color_btn)
        pen_box.append(self.pen_width_spin)
        add("Pen (draw on the shot)", pen_box)

        text_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.text_sw = tool_switch(
            "tool_text",
            "Adds the Text button — click to place a label; drag to move, "
            "Delete to remove, Ctrl+Z to undo")
        self.text_color_btn = color_button(self.cfg["text_color"],
                                           self._on_text_color)
        self.text_size_spin = spin(8, 72, self.cfg["text_size"],
                                   self._on_text_size, "Text size (px)")
        text_box.append(self.text_sw)
        text_box.append(self.text_color_btn)
        text_box.append(self.text_size_spin)
        add("Text (label the shot)", text_box)

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

        # One config write when the dialog closes — NOT one per keystroke /
        # slider tick (also avoids persisting a half-typed save_dir).
        self.connect("close-request", self._on_close)

    def _on_close(self, *_a):
        save_config(self.cfg)
        return False

    def _apply(self):
        self.overlay.refresh_style()
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

    def _on_monitor_selected(self, dd, _p):
        idx = dd.get_selected()
        if 0 <= idx < len(self.monitor_values):
            self.cfg["default_monitor"] = self.monitor_values[idx]

    def _on_remember(self, sw, _p):
        self.cfg["remember_selection"] = sw.get_active()

    def _on_default_size(self, scale):
        self.cfg["default_size_pct"] = round(scale.get_value(), 3)

    def _on_dir(self, entry):
        self.cfg["save_dir"] = entry.get_text()

    def _on_fmt(self, entry):
        self.cfg["filename_format"] = entry.get_text()

    def _on_always_save(self, sw, _p):
        self.cfg["always_save"] = sw.get_active()
        self.overlay.rebuild_toolbar()      # the Copy tooltip mentions it

    def _on_quick_save(self, sw, _p):
        self.cfg["quick_save_double_tap"] = sw.get_active()

    def _on_tool(self, key, sw):
        self.cfg[key] = sw.get_active()
        self.overlay.rebuild_toolbar()

    def _on_pen_color(self, btn, _p):
        self.cfg["pen_color"] = btn.get_rgba().to_string()

    def _on_pen_width(self, spin):
        self.cfg["pen_width"] = int(spin.get_value())

    def _on_text_color(self, btn, _p):
        self.cfg["text_color"] = btn.get_rgba().to_string()

    def _on_text_size(self, spin):
        self.cfg["text_size"] = int(spin.get_value())

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
        self.monitor_dd.set_selected(0)
        rgba = Gdk.RGBA(); rgba.parse(self.cfg["border_color"])
        self.color_btn.set_rgba(rgba)
        self.width_spin.set_value(self.cfg["border_width"])
        self.dim_scale.set_value(self.cfg["dim_opacity"])
        self.size_scale.set_value(self.cfg["default_size_pct"])
        self.remember_sw.set_active(self.cfg["remember_selection"])
        self.dir_entry.set_text(self.cfg["save_dir"])
        self.fmt_entry.set_text(self.cfg["filename_format"])
        self.always_sw.set_active(self.cfg["always_save"])
        self.cursor_sw.set_active(self.cfg["include_cursor"])
        self.quicksave_sw.set_active(self.cfg["quick_save_double_tap"])
        self.poly_sw.set_active(self.cfg["tool_polygon"])
        self.lasso_sw.set_active(self.cfg["tool_lasso"])
        self.pen_sw.set_active(self.cfg["tool_pen"])
        rgba = Gdk.RGBA(); rgba.parse(self.cfg["pen_color"])
        self.pen_color_btn.set_rgba(rgba)
        self.pen_width_spin.set_value(self.cfg["pen_width"])
        self.text_sw.set_active(self.cfg["tool_text"])
        rgba = Gdk.RGBA(); rgba.parse(self.cfg["text_color"])
        self.text_color_btn.set_rgba(rgba)
        self.text_size_spin.set_value(self.cfg["text_size"])
        self.overlay.rebuild_toolbar()
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
.snapclip-toolbar button:checked { background-color: rgba(0,163,255,0.35); }
.snapclip-toolbar separator {
    background-color: rgba(255,255,255,0.15);
    margin: 5px 3px;
    min-width: 1px;
}
.snapclip-size {
    color: #ffffff;
    font-size: 12px;
    padding: 0 8px 0 4px;
    opacity: 0.85;
}
.snapclip-textedit {
    background-color: rgba(20,20,22,0.9);
    color: #ffffff;
    border: 1px solid rgba(255,255,255,0.25);
    border-radius: 6px;
    padding: 2px 6px;
    min-height: 0;
}
.snapclip-section { font-weight: bold; }
.snapclip-subtle { font-size: 11px; opacity: 0.6; }
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
    """Owns the launch sequence:

      activate -> start the capture worker thread
               -> (worker reports the primary connector) build + realize the
                  overlay window on that monitor: widgets, CSS, GL context —
                  all of GTK's expensive setup — while the frame is captured
               -> (worker delivers the frame) attach it to the window and
                  present; or run the self-test; or, with double-tap
                  quick-save armed, wait out the tap window first.

    Failures are reported on stderr with a non-zero exit, exactly like the
    old sequential flow; the window is never mapped before the frame exists.
    """

    def __init__(self, config, self_test=None, allow_flash=False,
                 launch_us=0, quick_save_on=False, target_monitor=None):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.config = config
        self.self_test = self_test  # None | "copy" | "save"
        self.allow_flash = allow_flash
        self.launch_us = launch_us
        self.quick_save_on = quick_save_on
        self.target_monitor = target_monitor or config.get("default_monitor", "primary")
        self.window = None
        self.test_result = {}
        self.had_error = False
        self.exit_code = None       # set when a non-overlay path decides it

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
            self.hold()                 # keep running until the capture lands
            threading.Thread(target=self._capture_worker, name="snapclip-capture",
                             daemon=True).start()
            # Build and realize right away on the first monitor rather than
            # waiting for Mutter to name the primary one: realizing (the GPU
            # renderer's setup) is the longest single step, so it starts as
            # early as possible; the window is re-targeted — still unmapped —
            # if the capture turns out to cover another monitor.
            initial_connector = None
            if self.target_monitor and self.target_monitor.lower() != "primary":
                try:
                    target_info = _resolve_target_monitor(target=self.target_monitor)
                    initial_connector = target_info.get("connector")
                except Exception:
                    initial_connector = self.target_monitor
            self._ensure_window(initial_connector)
        except Exception as exc:
            self._abort(f"failed to start: {exc}")

    # -- capture worker (no GTK in here) --------------------------------------

    def _capture_worker(self):
        # A private main context keeps the D-Bus signal wait off GTK's loop.
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        posted = False

        def on_connector(connector):
            GLib.idle_add(self._on_connector, connector,
                          priority=GLib.PRIORITY_HIGH)

        def on_frame(surface, full_desktop, connector):
            # Hand the frame to the main loop the moment it exists; the
            # ScreenCast teardown then runs on this thread, off the critical
            # path to the first visible frame.
            nonlocal posted
            texture = texture_for_surface(surface)
            posted = True
            GLib.idle_add(self._on_capture,
                          (surface, texture, full_desktop, connector),
                          priority=GLib.PRIORITY_HIGH)

        try:
            capture_screen(allow_flash=self.allow_flash,
                           on_connector=on_connector, on_frame=on_frame,
                           target_monitor=self.target_monitor)
            if posted:
                return
            result = CaptureError("capture produced no frame")
        except CaptureError as exc:
            if posted:                  # the frame was delivered; the rest
                return                  # was teardown noise
            result = exc
        except Exception as exc:        # never lose the error in a thread
            if posted:
                return
            result = CaptureError(f"capture failed: {exc!r}")
        finally:
            ctx.pop_thread_default()
        GLib.idle_add(self._on_capture, result, priority=GLib.PRIORITY_HIGH)

    # -- main-loop side -------------------------------------------------------

    def _ensure_window(self, connector):
        if self.window is None:
            try:
                monitor = OverlayWindow.pick_monitor(Gdk.Display.get_default(),
                                                     connector)
                self.window = OverlayWindow(self, self.config, monitor)
                if not self.self_test:
                    # Realize now (GL context, renderer, surface) so present()
                    # later is just a map; the window stays invisible.
                    self.window.realize()
            except Exception as exc:
                self._abort(f"failed to open the overlay: {exc}")
        return self.window

    def _on_connector(self, connector):
        win = self._ensure_window(connector)
        if win is not None:
            win.retarget(OverlayWindow.pick_monitor(Gdk.Display.get_default(),
                                                    connector))
        return False

    def _on_capture(self, result):
        try:
            if isinstance(result, CaptureError):
                self._abort(str(result), code=2)
                return False
            surface, texture, full_desktop, connector = result
            win = self._ensure_window(connector)
            if win is None:
                return False
            win.set_capture(surface, texture, full_desktop=full_desktop,
                            connector=connector)
            if self.self_test:
                self._run_self_test(win)
            else:
                self._present_or_quick_save(win, surface)
        except Exception as exc:
            self._abort(f"failed to open the overlay: {exc}")
        finally:
            self.release()
        return False

    def _present_or_quick_save(self, win, surface):
        """Show the overlay — unless a double-tap arrives inside the window."""
        cfg = self.config
        if not self.quick_save_on or _quick_save_recent(
                self.launch_us, _quick_save_cooldown_us(cfg)):
            win.present_overlay()
            return
        # We hold the single-instance lock, so a rapid second press marks a
        # tap file (see main).  Wait out the rest of the window without
        # mapping anything; if it was tapped, save the whole screen headlessly
        # and never show the overlay.  The cooldown makes a mashed key open
        # the overlay once instead of spamming saves.
        window_us = _quick_save_window_us(cfg)

        def decide():
            if _saw_double_tap(self.launch_us, window_us):
                _record_quick_save(GLib.get_monotonic_time())
                self.exit_code = quick_save(surface, cfg)
                self.quit()
            else:
                win.present_overlay()
            return False

        remaining_us = (self.launch_us + window_us) - GLib.get_monotonic_time()
        if remaining_us > 0:
            GLib.timeout_add(-(-remaining_us // 1000), decide)   # ceil to ms
        else:
            decide()

    def _abort(self, msg, code=1):
        print(f"snapclip: {msg}", file=sys.stderr)
        self.had_error = True
        self.exit_code = code
        self.test_result.setdefault("error", msg)
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
# Double-tap quick-save: tap the launch hotkey twice quickly to save the whole
# screen with no overlay.  Each hotkey press is a separate process, so the two
# taps rendezvous through small files in the runtime dir plus the existing
# single-instance lock: the first press holds the lock and waits out a short
# window; a rapid second press fails the lock, drops a timestamp, and exits;
# the first press sees it and saves headlessly.  A cooldown collapses a burst
# of presses (key-mashing) into a single save.
# ----------------------------------------------------------------------------


def _double_tap_files():
    d = GLib.get_user_runtime_dir()
    return (os.path.join(d, "snapclip.tap"),    # second-tap timestamp
            os.path.join(d, "snapclip.qs"))     # last quick-save timestamp


def _is_double_tap(tap_us, launch_us, window_us):
    """A recorded second-launch time is a double-tap iff it lands in the window
    just after our own launch — so stale marks from earlier runs are ignored."""
    return tap_us is not None and launch_us < tap_us <= launch_us + window_us


def _within_cooldown(last_qs_us, now_us, cooldown_us):
    """True if a quick-save fired within the cooldown: the spam guard that keeps
    key-mashing (or a bounced key) from writing a burst of files."""
    return last_qs_us is not None and 0 <= now_us - last_qs_us < cooldown_us


def _quick_save_window_us(config):
    return int(config["double_tap_ms"]) * 1000


def _quick_save_cooldown_us(config):
    # Derived, not a separate knob: a few tap-windows, floored at ~1.2 s.
    return max(1_200_000, 4 * _quick_save_window_us(config))


def _read_us(path):
    try:
        with open(path) as fh:
            return int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return None


def _write_us(path, value_us):
    try:
        with open(path, "w") as fh:
            fh.write(str(int(value_us)))
    except OSError:
        pass                        # best-effort: a lost mark just means no save


def _mark_double_tap(launch_us):
    _write_us(_double_tap_files()[0], launch_us)


def _saw_double_tap(launch_us, window_us):
    return _is_double_tap(_read_us(_double_tap_files()[0]), launch_us, window_us)


def _quick_save_recent(now_us, cooldown_us):
    return _within_cooldown(_read_us(_double_tap_files()[1]), now_us, cooldown_us)


def _record_quick_save(now_us):
    _write_us(_double_tap_files()[1], now_us)


def surface_to_png_bytes(surface):
    """Encode a whole cairo surface to PNG bytes — the headless double-tap
    quick-save grabs the entire captured frame (no crop, no overlay)."""
    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def quick_save(surface, config):
    """Save + copy the full captured frame with no UI.  Returns an exit code."""
    png = surface_to_png_bytes(surface)
    copied = False
    try:
        copy_png_to_clipboard(png)
        copied = True
    except Exception as exc:
        print(f"snapclip: quick-save copy failed: {exc}", file=sys.stderr)
    try:
        path = save_png(png, config["save_dir"], config["filename_format"])
        print(f"snapclip: quick-saved {path}")
    except Exception as exc:
        print(f"snapclip: quick-save could not write the file: {exc}",
              file=sys.stderr)
        return 0 if copied else 2
    return 0


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


VERSION = "snapclip 1.3"


def _which(cmd):
    """shutil.which for one plain command name, without importing shutil."""
    for d in os.environ.get("PATH", os.defpath).split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _parse_args(argv):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        # The hotkey path: nothing to parse, so skip importing argparse.
        return types.SimpleNamespace(self_test=None, allow_flash=False,
                                     monitor=None, list_monitors=False)
    import argparse
    parser = argparse.ArgumentParser(description="Region screenshot tool")
    parser.add_argument("-m", "--monitor", metavar="MONITOR",
                        help="target monitor: index (0, 1, ...), connector name "
                             "(HDMI-1, eDP-1), friendly name substring, or 'primary'")
    parser.add_argument("-l", "--list-monitors", action="store_true",
                        help="list detected monitors and exit")
    parser.add_argument("--self-test", choices=["copy", "save"],
                        help="capture + crop + act without the GUI, then exit")
    parser.add_argument("--allow-flash", action="store_true",
                        help="if flash-free ScreenCast is unavailable, use the "
                             "screenshot portal instead of erroring (the portal "
                             "triggers GNOME's screenshot flash)")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    if getattr(args, "list_monitors", False):
        _print_monitors()
        return 0

    # Fail fast on missing session prerequisites: discovering that wl-copy is
    # absent only AFTER the user has framed a selection would throw that work
    # away (the copy happens last).
    if not os.environ.get("WAYLAND_DISPLAY"):
        print("snapclip: not a Wayland session (WAYLAND_DISPLAY is unset) — "
              "the clipboard step needs wl-copy/Wayland", file=sys.stderr)
        return 2
    if _which("wl-copy") is None:
        print("snapclip: wl-copy not found — install the 'wl-clipboard' "
              "package", file=sys.stderr)
        return 2

    config = load_config()
    launch_us = GLib.get_monotonic_time()
    quick_save_on = bool(config.get("quick_save_double_tap")) and not args.self_test
    target_monitor = getattr(args, "monitor", None) or config.get("default_monitor", "primary")

    # Single-instance for the interactive overlay: a second hotkey press must
    # not stack another full-screen overlay. Hold the lock for the whole run.
    # (self-test is a non-interactive debug mode and is exempt.)
    lock = None
    if not args.self_test:
        lock = acquire_single_instance_lock()
        if lock is ALREADY_RUNNING:
            # Another overlay already holds the lock. With double-tap quick-save
            # on, leave a timestamp so that instance can fire a headless save;
            # otherwise a second press is the usual no-op.
            if quick_save_on:
                _mark_double_tap(launch_us)
            else:
                print("snapclip: a snapclip overlay is already open",
                      file=sys.stderr)
            return 0

    app = SnapClipApp(config, self_test=args.self_test,
                      allow_flash=args.allow_flash, launch_us=launch_us,
                      quick_save_on=quick_save_on, target_monitor=target_monitor)
    app.run([])
    # `lock` stays referenced until here so the flock is held for the whole
    # session; it releases automatically when the process exits.

    if args.self_test:
        r = app.test_result
        ok = r.get("copied") and r.get("png_len", 0) > 0
        print("SELF-TEST", "PASS" if ok else "FAIL", r)
        return 0 if ok else 1
    if app.exit_code is not None:
        return app.exit_code
    return 1 if app.had_error else 0


def run(argv=None):
    """Run main() and exit immediately.

    GTK/interpreter teardown takes ~100 ms here, during which the process would
    still hold the single-instance lock — and, on cancel, the window.  All
    output is written and flushed, the config is already on disk, and wl-copy's
    daemon is a separate process, so there is nothing left to tear down.
    """
    code = main(argv)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    run()
