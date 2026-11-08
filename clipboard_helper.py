"""Clipboard owner that runs through XWayland.

GNOME's Wayland session doesn't let background programs set the clipboard, but X11 clients may,
and GNOME shares the X11 clipboard with Wayland apps. Runs under the system Python (needs GTK).

Protocol on stdin/stdout, one JSON object per line:
  {"get": true}     -> {"text": <current clipboard text, or null if empty or unreadable>}
  {"set": "text"}   -> {"ok": true}   (this process keeps owning the clipboard)
"""

import json
import os
import sys

os.environ["GDK_BACKEND"] = "x11"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def on_input(channel, condition):
    line = channel.readline()
    if not line:
        Gtk.main_quit()
        return False
    request = json.loads(line)
    if "set" in request:
        clipboard.set_text(request["set"], -1)
        reply({"ok": True})
    else:
        # Reading can hang when a Wayland app owns the clipboard, so give up after a short wait.
        done = []

        def answer(text):
            if not done:
                done.append(True)
                reply({"text": text})

        clipboard.request_text(lambda _cb, text: answer(text))
        GLib.timeout_add(500, lambda: answer(None))
    return True


GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.IO_IN | GLib.IO_HUP, on_input)
Gtk.main()
