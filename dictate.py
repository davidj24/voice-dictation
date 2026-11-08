"""Push-to-talk dictation into the active window, or into the Claude desktop app.

Kanata sends messages over its TCP server when Right Alt is held/released:
  voice-down         start recording; text goes to the active window
  voice-down-claude  start recording; text goes to Claude (Shift was already held)
  voice-claude       Shift pressed while recording -> text goes to Claude
  voice-up           stop recording, transcribe, paste

Transcription runs locally with faster-whisper on the GPU.
"""

import json
import select
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from evdev import UInput, ecodes as e

ROOT = Path(__file__).resolve().parent
KANATA_ADDR = ("127.0.0.1", 7070)
MODEL = "large-v3-turbo"
MIN_SECONDS = 0.4  # ignore accidental taps
SOUNDS = Path("/usr/share/sounds/freedesktop/stereo")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dictate")


def load_model():
    # CUDA libraries come from pip wheels inside .venv; ctranslate2 finds them via LD_LIBRARY_PATH,
    # which run.sh sets before starting Python.
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL, device="cuda", compute_type="float16",
                         download_root=str(ROOT / "models"))
    # Warm up so the first real dictation is fast.
    import numpy as np
    list(model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
    return model


def read_wav(path):
    """16 kHz mono s16 WAV from pw-record -> float32 samples (avoids faster-whisper's PyAV decoder)."""
    import numpy as np
    import wave

    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class BluetoothMic:
    """Switches a Bluetooth headset into headset (mic) mode while recording, then back to music mode."""

    HEADSET_PROFILES = ("headset-head-unit-msbc", "headset-head-unit")

    def __init__(self):
        self.card = None  # (device id, profile index to restore)

    @staticmethod
    def dump():
        return json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3).stdout)

    def find_source(self, objects, device_id):
        for o in objects:
            props = o.get("info", {}).get("props", {})
            if props.get("media.class") == "Audio/Source" and props.get("device.id") == device_id:
                return props.get("node.name")
        return None

    def enable(self):
        """Returns the node name to record from, or None to use the default source."""
        objects = self.dump()
        for o in objects:
            info = o.get("info", {})
            if not str(info.get("props", {}).get("device.name", "")).startswith("bluez_card."):
                continue
            params = info.get("params", {})
            active = (params.get("Profile") or [{}])[0]
            profiles = {p.get("name"): p.get("index") for p in params.get("EnumProfile", [])}
            if active.get("name", "").startswith("headset"):
                return self.find_source(objects, o["id"])
            target = next((profiles[n] for n in self.HEADSET_PROFILES if n in profiles), None)
            if target is None or active.get("name") in (None, "off"):
                continue
            self.card = (o["id"], active["index"])
            subprocess.run(["wpctl", "set-profile", str(o["id"]), str(target)], timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                source = self.find_source(self.dump(), o["id"])
                if source:
                    time.sleep(0.3)  # let the link settle so the first words aren't clipped
                    return source
                time.sleep(0.1)
            log.info("headset mic did not appear")
            return None
        return None

    def restore(self):
        if self.card:
            device_id, profile = self.card
            self.card = None
            subprocess.run(["wpctl", "set-profile", str(device_id), str(profile)], timeout=3)



class Clipboard:
    """Talks to clipboard_helper.py, which owns the clipboard through XWayland."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def request(self, obj):
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                # System Python, since GTK isn't installed in the venv.
                self.proc = subprocess.Popen(["/usr/bin/python3", str(ROOT / "clipboard_helper.py")],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
            ready, _, _ = select.select([self.proc.stdout], [], [], 2)
            if not ready:
                log.info("clipboard helper not responding, restarting it")
                self.proc.kill()
                self.proc = None
                return {}
            return json.loads(self.proc.stdout.readline())

    def get(self):
        return self.request({"get": True}).get("text")

    def set(self, text):
        self.request({"set": text})


def play(name, wait=None):
    if wait:
        # Run `wait` first (e.g. until the headset is back in music mode) without holding up the caller.
        threading.Thread(target=lambda: (wait(), play(name)), daemon=True).start()
        return
    threading.Thread(target=subprocess.run, args=(["pw-play", str(SOUNDS / f"{name}.oga")],),
                     kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}, daemon=True).start()


class Indicator:
    """On-screen status shown as a GNOME notification that is updated in place."""

    def __init__(self):
        self.id = 0

    def show(self, summary, body="", sticky=True):
        # Critical urgency keeps the banner on screen until it is replaced or closed.
        urgency = 2 if sticky else 1
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications",
             "--method", "org.freedesktop.Notifications.Notify",
             "Voice dictation", str(self.id), "audio-input-microphone", summary, body, "[]",
             f"{{'urgency': <byte {urgency}>, 'transient': <true>}}", "0" if sticky else "2500"],
            capture_output=True, text=True, timeout=2).stdout
        try:
            self.id = int(out.split()[1].rstrip(",)"))
        except (IndexError, ValueError):
            self.id = 0

    def close(self):
        if self.id:
            subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.CloseNotification", str(self.id)],
                capture_output=True, timeout=2)
            self.id = 0


CLAUDE_DESKTOP_FILE = "com.anthropic.Claude.desktop"
NUMBER_KEYS = [e.KEY_1, e.KEY_2, e.KEY_3, e.KEY_4, e.KEY_5, e.KEY_6, e.KEY_7, e.KEY_8, e.KEY_9, e.KEY_0]


def claude_dock_key():
    """Number key for GNOME's Super+N "switch to the Nth dock app" shortcut, or None."""
    out = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"],
                         capture_output=True, text=True, timeout=2).stdout
    apps = [a.strip(" '") for a in out.strip("[]\n").split(",")]
    if CLAUDE_DESKTOP_FILE in apps and apps.index(CLAUDE_DESKTOP_FILE) < len(NUMBER_KEYS):
        return NUMBER_KEYS[apps.index(CLAUDE_DESKTOP_FILE)]
    return None


class Dictation:
    def __init__(self, model):
        self.model = model
        # Declare a full key set: GNOME ignores shortcuts from devices that don't look like real keyboards.
        self.keyboard = UInput({e.EV_KEY: list(range(e.KEY_ESC, e.KEY_MICMUTE + 1))},
                               name="voice-dictation-keyboard")
        self.recorder = None
        self.wav = None
        self.started = 0.0
        self.to_claude = False
        self.lock = threading.Lock()
        self.indicator = Indicator()
        self.mic = BluetoothMic()
        self.clipboard = Clipboard()

    def start(self, to_claude):
        with self.lock:
            if self.recorder:
                return
            self.to_claude = to_claude
            self.indicator.show("🎧 Connecting mic…", "Start speaking after the pop.")
            source = self.mic.enable()
            fd, self.wav = tempfile.mkstemp(suffix=".wav", prefix="dictate-")
            os.close(fd)
            target = ["--target", source] if source else []
            self.recorder = subprocess.Popen(
                ["pw-record", *target, "--rate", "16000", "--channels", "1", "--format", "s16", self.wav],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.started = time.monotonic()
        if to_claude:
            self.focus_claude()
        # Sounds only reach a Bluetooth headset in mic mode once its audio link is open, i.e. while recording.
        play("message", wait=lambda: time.sleep(0.8))
        self.show_listening()
        log.info("recording from %s (to %s)", source or "default mic", "Claude" if to_claude else "active window")

    def show_listening(self):
        if self.to_claude:
            self.indicator.show("🎙️ Listening — to Claude", "Release Right Alt to paste into Claude.")
        else:
            self.indicator.show("🎙️ Listening", "Release Right Alt to type here. Add Shift to send to Claude instead.")

    def send_to_claude(self):
        # Sticky for the rest of this hold, so the order you let go of the keys doesn't matter.
        if self.recorder and not self.to_claude:
            self.to_claude = True
            self.show_listening()
            self.focus_claude()

    def stop(self):
        with self.lock:
            if not self.recorder:
                return
            self.recorder.terminate()
            self.recorder.wait()
            self.recorder = None
            wav, to_claude = self.wav, self.to_claude
            duration = time.monotonic() - self.started
            self.indicator.show("⏳ Transcribing…")
            threading.Thread(target=self.finish, args=(wav, to_claude, duration), daemon=True).start()
            # Play the "stopped listening" cue before leaving headset mode: sounds played right after
            # switching back to music mode get lost while the earbuds reconnect.
            subprocess.run(["pw-play", str(SOUNDS / "bell.oga")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.mic.restore()

    def finish(self, wav, to_claude, duration):
        try:
            if duration < MIN_SECONDS:
                log.info("too short (%.2fs), ignored", duration)
                self.indicator.close()
                return
            t = time.monotonic()
            segments, _ = self.model.transcribe(read_wav(wav), language="en", vad_filter=True,
                                                condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("transcribed %.1fs of audio in %.2fs: %r", duration, time.monotonic() - t, text)
            if not text:
                self.indicator.show("No speech heard", sticky=False)
                return
            self.indicator.close()
            self.paste(text)
        except Exception:
            log.exception("dictation failed")
            self.indicator.show("Dictation failed", "See: journalctl --user -u voice-dictation", sticky=False)
        finally:
            os.unlink(wav)

    def paste(self, text):
        previous = self.clipboard.get()
        self.clipboard.set(text)
        time.sleep(0.15)
        self.tap(e.KEY_V, ctrl=True)
        log.info("pasted")
        # Put back what was on the clipboard before dictating.
        time.sleep(0.5)
        if previous:
            self.clipboard.set(previous)

    def focus_claude(self):
        # GNOME blocks apps from raising windows themselves, but Super+N from the keyboard
        # switches to the Nth app pinned in the dock.
        key = claude_dock_key()
        if key is None:
            log.info("Claude is not pinned in the first 10 dock slots; not switching windows")
            return
        self.tap(key, e.KEY_LEFTMETA)
        time.sleep(0.3)  # let the window come forward
        # Claude sends typed letters to the prompt box even when something else in the window has
        # focus (pastes it doesn't), so type a letter and delete it to put the cursor there.
        self.tap(e.KEY_X)
        self.tap(e.KEY_BACKSPACE)

    def tap(self, key, *modifiers, ctrl=False):
        if ctrl:
            modifiers = (e.KEY_LEFTCTRL,)
        events = [(m, 1) for m in modifiers] + [(key, 1), (key, 0)] + [(m, 0) for m in reversed(modifiers)]
        for code, value in events:
            self.keyboard.write(e.EV_KEY, code, value)
            self.keyboard.syn()
            time.sleep(0.02)  # real keyboards never send events back to back


def listen(dictation):
    handlers = {
        "voice-down": lambda: dictation.start(to_claude=False),
        "voice-down-claude": lambda: dictation.start(to_claude=True),
        "voice-claude": dictation.send_to_claude,
        "voice-up": dictation.stop,
    }
    while True:
        try:
            with socket.create_connection(KANATA_ADDR) as sock:
                log.info("connected to kanata")
                for line in sock.makefile():
                    try:
                        msg = json.loads(line).get("MessagePush", {}).get("message")
                    except json.JSONDecodeError:
                        continue
                    # Kanata sends push-msg arguments as a list, e.g. ["voice-down"].
                    if isinstance(msg, list) and msg:
                        msg = msg[0]
                    if isinstance(msg, str) and msg in handlers:
                        handlers[msg]()
        except OSError as err:
            log.info("kanata not reachable (%s), retrying", err)
        time.sleep(2)


def main():
    log.info("loading %s", MODEL)
    dictation = Dictation(load_model())
    log.info("ready")
    listen(dictation)


if __name__ == "__main__":
    sys.exit(main())
