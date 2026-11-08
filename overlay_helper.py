"""Always-on-top status pill for voice dictation, with a Cancel button.

Runs through XWayland because GNOME's Wayland session doesn't let apps keep a window on top or
position it. The window never takes keyboard focus, so pasting into the active window still works.
Runs under the system Python (needs GTK).

Protocol, one JSON object per line:
  stdin:  {"show": "text", "kind": "connecting" | "listening" | "busy" | "error"}   or   {"hide": true}
  stdout: {"event": "cancel"} when the Cancel button is clicked
"""

import json
import os
import sys

os.environ["GDK_BACKEND"] = "x11"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

COLORS = {"connecting": "#b7791f", "listening": "#c53030", "busy": "#2b6cb0", "error": "#4a5568"}

css = Gtk.CssProvider()
css.load_from_data(b"""
window { background: transparent; }
.pill { border-radius: 18px; padding: 6px 8px 6px 16px; }
.pill label { color: white; font-weight: bold; font-size: 13pt; }
.pill button { color: white; background: rgba(0,0,0,0.25); border: none; border-radius: 12px;
               padding: 2px 12px; box-shadow: none; text-shadow: none; }
.pill button:hover { background: rgba(0,0,0,0.45); }
""")
Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_USER)

window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
window.set_decorated(False)
window.set_keep_above(True)
window.set_skip_taskbar_hint(True)
window.set_skip_pager_hint(True)
window.set_accept_focus(False)
window.set_focus_on_map(False)
window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
window.stick()
visual = window.get_screen().get_rgba_visual()
if visual:
    window.set_visual(visual)
window.set_app_paintable(True)

box = Gtk.Box(spacing=12)
box.get_style_context().add_class("pill")
label = Gtk.Label()
button = Gtk.Button(label="Cancel")
button.set_can_focus(False)
box.pack_start(label, False, False, 0)
box.pack_start(button, False, False, 0)
window.add(box)

style = Gtk.CssProvider()
box.get_style_context().add_provider(style, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


button.connect("clicked", lambda _b: emit({"event": "cancel"}))


def place():
    # Bottom centre of the primary monitor, above the dock.
    monitor = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
    area = monitor.get_workarea()
    width, height = window.get_size()
    window.move(area.x + (area.width - width) // 2, area.y + area.height - height - 24)


def on_input(channel, condition):
    line = channel.readline()
    if not line:
        Gtk.main_quit()
        return False
    request = json.loads(line)
    if request.get("hide"):
        window.hide()
        return True
    label.set_text(request.get("show", ""))
    color = COLORS.get(request.get("kind"), COLORS["busy"])
    style.load_from_data(f".pill {{ background: {color}; }}".encode())
    window.resize(1, 1)
    window.show_all()
    GLib.idle_add(place)
    return True


GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.IO_IN | GLib.IO_HUP, on_input)
Gtk.main()
