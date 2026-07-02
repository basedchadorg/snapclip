import cairo, math, sys

W, H = 1280, 800
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
cr = cairo.Context(surf)

def rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x+w-r, y+r, r, -math.pi/2, 0)
    cr.arc(x+w-r, y+h-r, r, 0, math.pi/2)
    cr.arc(x+r, y+h-r, r, math.pi/2, math.pi)
    cr.arc(x+r, y+r, r, math.pi, 3*math.pi/2)
    cr.close_path()

# --- synthetic "desktop" wallpaper (no real data) ---
g = cairo.LinearGradient(0, 0, W, H)
g.add_color_stop_rgb(0, 0.11, 0.13, 0.18)
g.add_color_stop_rgb(1, 0.06, 0.07, 0.10)
cr.set_source(g); cr.rectangle(0, 0, W, H); cr.fill()

# top panel
cr.set_source_rgba(0,0,0,0.35); cr.rectangle(0,0,W,30); cr.fill()
cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
cr.set_font_size(13); cr.set_source_rgba(1,1,1,0.7)
cr.move_to(W/2-22, 20); cr.show_text("12:00")

def window(x, y, w, h, title, lines, accent):
    cr.set_source_rgba(0,0,0,0.35); rrect(cr, x+6, y+8, w, h, 10); cr.fill()   # shadow
    cr.set_source_rgb(0.16,0.17,0.21); rrect(cr, x, y, w, h, 10); cr.fill()
    cr.set_source_rgb(0.20,0.21,0.26); rrect(cr, x, y, w, 34, 10); cr.fill()
    cr.rectangle(x, y+22, w, 12); cr.fill()
    for i,c in enumerate([(0.95,0.34,0.33),(0.96,0.74,0.29),(0.30,0.78,0.36)]):
        cr.set_source_rgb(*c); cr.arc(x+16+i*18, y+17, 5, 0, 2*math.pi); cr.fill()
    cr.set_source_rgba(1,1,1,0.85); cr.set_font_size(12)
    cr.move_to(x+74, y+21); cr.show_text(title)
    cr.set_font_size(12)
    for i, ln in enumerate(lines):
        cr.set_source_rgb(*accent) if ln[:1]=="*" else cr.set_source_rgba(0.8,0.82,0.86,0.9)
        cr.move_to(x+16, y+58+i*22); cr.show_text(ln.lstrip("*"))

window(70, 90, 560, 360, "editor",
       ["*def capture_screen():", "    # flash-free via ScreenCast",
        "    return screencast_capture()", "", "*snapclip — no screen flash",
        "    drag · resize · Enter to copy"], (0.45,0.8,1.0))
window(700, 150, 500, 430, "terminal",
       ["$ snapclip", "  captured 1280x720", "  copied to clipboard",
        "  (no flash, no file)", "", "$ _"], (0.4,0.9,0.5))

# --- snapclip overlay on top ---
sx, sy, sw, sh = 360, 250, 540, 300        # selection
# dim outside selection (interior stays bright = see-through)
cr.set_source_rgba(0,0,0,0.42); cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
cr.rectangle(0,0,W,H); cr.rectangle(sx,sy,sw,sh); cr.fill()
cr.set_fill_rule(cairo.FILL_RULE_WINDING)
# border (matches the shipped default #0077CC)
cr.set_source_rgb(0.0, 0.467, 0.8); cr.set_line_width(2)
cr.rectangle(sx+0.5, sy+0.5, sw, sh); cr.stroke()
# handles
cr.set_source_rgb(1,1,1)
for hx,hy in [(sx,sy),(sx+sw,sy),(sx,sy+sh),(sx+sw,sy+sh),
              (sx+sw/2,sy),(sx+sw/2,sy+sh),(sx,sy+sh/2),(sx+sw,sy+sh/2)]:
    cr.rectangle(hx-4, hy-4, 8, 8)
cr.fill()
# WxH badge
cr.set_font_size(13); txt="1080 × 600"; ext=cr.text_extents(txt)
bx,by=sx, sy-ext.height-12
cr.set_source_rgba(0,0,0,0.7); rrect(cr, bx, by, ext.width+14, ext.height+10, 5); cr.fill()
cr.set_source_rgb(1,1,1); cr.move_to(bx+7, by+ext.height+4); cr.show_text(txt)

# toolbar
tb_x, tb_y, tb_h = sx, sy+sh+10, 38
items=["1080×600","Copy","Save","Cancel"]
cr.set_font_size(13)
widths=[cr.text_extents(t).width for t in items]
tb_w=sum(widths)+ (len(items))*26 + 48
cr.set_source_rgba(0.12,0.12,0.13,0.95); rrect(cr, tb_x, tb_y, tb_w, tb_h, 10); cr.fill()
cx=tb_x+12
for i,t in enumerate(items):
    if i==0: cr.set_source_rgba(1,1,1,0.6)
    else: cr.set_source_rgba(0.95,0.95,0.95,1)
    cr.move_to(cx, tb_y+24); cr.show_text(t)
    cx += widths[i] + 26
# small gear icon for "settings"
import math as _m
gx, gy = cx+12, tb_y+tb_h/2
cr.new_path(); cr.set_source_rgba(0.95,0.95,0.95,1); cr.set_line_width(2)
cr.arc(gx, gy, 6, 0, 2*_m.pi); cr.stroke()
cr.arc(gx, gy, 2.5, 0, 2*_m.pi); cr.stroke()
for k in range(8):
    a=k*_m.pi/4
    cr.move_to(gx+_m.cos(a)*6, gy+_m.sin(a)*6)
    cr.line_to(gx+_m.cos(a)*9, gy+_m.sin(a)*9)
cr.stroke()
surf.write_to_png(sys.argv[1])
print("wrote demo", W, "x", H, "->", sys.argv[1])
