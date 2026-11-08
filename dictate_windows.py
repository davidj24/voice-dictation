"""Push-to-talk dictation for Windows: the same keys and status pill as dictate.py on Linux.

  Tap Right Alt, speak, tap again   paste into the active window (or hold it, and let go)
  Right Ctrl while dictating        the text goes to Claude instead
  Esc while dictating, or Cancel    abort; nothing is pasted

A low-level keyboard hook replaces kanata's TCP messages, so kanata's config and elevated task are
untouched. Right Alt is taken over completely; Esc and Right Ctrl only while a dictation runs.
"""

import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes as wt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "voice-dictation"
MODEL = "large-v3-turbo"
RATE = 16000
MIN_SECONDS = 0.4  # ignore accidental taps
HOLD_SECONDS = 0.5  # holding Right Alt at least this long makes releasing it finish
MAX_RECORDING = 300  # stop listening after this long, in case a key release was missed
START_SOUND = r"C:\Windows\Media\Speech On.wav"
STOP_SOUND = r"C:\Windows\Media\Speech Sleep.wav"
COLORS = {"connecting": "#b7791f", "listening": "#c53030", "busy": "#2b6cb0", "error": "#4a5568"}

VK_RMENU, VK_RCONTROL, VK_ESCAPE = 0xA5, 0xA3, 0x1B
VK_MENU, VK_CONTROL, VK_V, VK_X, VK_BACK = 0x12, 0x11, 0x56, 0x58, 0x08
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
OWN_INPUT = 0x56444943  # tags keys we send, so our own hook lets them through

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG_DIR / "dictate.log", level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dictate")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


def add_cuda_paths():
    """The CUDA libraries come from pip wheels inside .venv; Windows has to be told where they are."""
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for folder in (site / "cublas" / "bin", site / "cudnn" / "bin"):
        if folder.is_dir():
            os.add_dll_directory(str(folder))
            os.environ["PATH"] = f"{folder};{os.environ['PATH']}"


def load_model():
    add_cuda_paths()
    if (ROOT / "models").is_dir() and any((ROOT / "models").iterdir()):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL, device="cuda", compute_type="float16", download_root=str(ROOT / "models"))
    list(model.transcribe(np.zeros(RATE, dtype=np.float32), language="en")[0])  # first real one is then fast
    return model


def play(path, delay=0.0):
    def run():
        if delay:
            time.sleep(delay)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    threading.Thread(target=run, daemon=True).start()


def key_event(vk, up=False):
    event = INPUT(type=1)  # INPUT_KEYBOARD
    event.u.ki = KEYBDINPUT(wVk=vk, dwFlags=2 if up else 0, dwExtraInfo=OWN_INPUT)
    return event


def tap(key, *modifiers):
    for event in [key_event(m) for m in modifiers] + [key_event(key), key_event(key, up=True)] + \
            [key_event(m, up=True) for m in reversed(modifiers)]:
        user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        time.sleep(0.02)


def find_claude_window():
    found = []

    def check(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        kind = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, kind, 64)
        if kind.value != "Chrome_WidgetWin_1":
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            size = wt.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)) \
                    and os.path.basename(path.value).lower() == "claude.exe":
                found.append(hwnd)
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(WNDENUMPROC(check), 0)
    return found[0] if found else None


def focus_claude():
    hwnd = find_claude_window()
    if not hwnd:
        log.info("no Claude window found; not switching windows")
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    tap(VK_MENU)  # Windows only lets a background program raise a window right after input
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    # Claude sends typed letters to the prompt box even when something else in the window has
    # focus (pastes it doesn't), so type a letter and delete it to put the cursor there.
    tap(VK_X)
    tap(VK_BACK)


class Overlay:
    """The always-on-top status pill. It never takes keyboard focus. Runs on the main thread."""

    def __init__(self, on_cancel):
        self.commands = queue.Queue()
        self.hide_at = None
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.frame = tk.Frame(self.root, padx=16, pady=8)
        self.frame.pack()
        self.label = tk.Label(self.frame, font=("Segoe UI", 11, "bold"), fg="white")
        self.label.pack(side="left")
        self.cancel = tk.Label(self.frame, text="Cancel", font=("Segoe UI", 10, "underline"), fg="white",
                               padx=12, cursor="hand2")
        self.cancel.pack(side="left")
        self.cancel.bind("<Button-1>", lambda _: threading.Thread(target=on_cancel, daemon=True).start())
        self.root.update()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        style = user32.GetWindowLongPtrW(self.hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongPtrW(self.hwnd, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE, TOOLWINDOW
        user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
        self.root.after(40, self.pump)

    def show(self, text, kind="busy", hide_after=None):
        self.commands.put(("show", text, kind, hide_after))

    def hide(self):
        self.commands.put(("hide",))

    def pump(self):
        try:
            while True:
                command = self.commands.get_nowait()
                if command[0] == "hide":
                    self.hide_at = None
                    user32.ShowWindow(self.hwnd, 0)
                else:
                    self.draw(*command[1:])
        except queue.Empty:
            pass
        if self.hide_at and time.monotonic() >= self.hide_at:
            self.hide_at = None
            user32.ShowWindow(self.hwnd, 0)
        self.root.after(40, self.pump)

    def draw(self, text, kind, hide_after):
        color = COLORS[kind]
        for widget in (self.root, self.frame, self.label, self.cancel):
            widget.configure(bg=color)
        self.label.configure(text=text)
        if kind == "error":
            self.cancel.pack_forget()
        elif not self.cancel.winfo_manager():
            self.cancel.pack(side="left")
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left = (self.root.winfo_screenwidth() - width) // 2
        top = self.root.winfo_screenheight() - height - 90
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        self.root.attributes("-topmost", True)
        self.hide_at = time.monotonic() + hide_after if hide_after else None


class Dictation:
    def __init__(self):
        self.model = None
        self.stream = None
        self.chunks = []
        self.started = 0.0
        self.to_claude = False
        self.busy = False  # recording or transcribing: Esc belongs to the dictation
        self.lock = threading.Lock()
        self.generation = 0  # bumped by cancel() so in-flight transcriptions don't paste
        self.watchdog = None
        self.press = None  # (time Right Alt was pressed, whether that press started recording)
        self.indicator = Overlay(on_cancel=self.cancel)

    @property
    def recording(self):
        return self.stream is not None

    def right_alt_down(self, when):
        if self.recording:
            self.press = (when, False)
            self.stop()
        else:
            self.press = (when, True)
            self.start()

    def right_alt_up(self, when):
        if self.press and self.press[1] and when - self.press[0] >= HOLD_SECONDS:
            self.stop()
        self.press = None

    def start(self):
        if self.model is None:
            self.indicator.show("Dictation is still loading the model…", "error", hide_after=2.5)
            return
        import sounddevice as sd

        with self.lock:
            if self.recording:
                return
            self.busy = True
            self.to_claude = False
            self.chunks = []
            self.indicator.show("Connecting mic…   ·   Esc cancels", "connecting")
            try:
                stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                        callback=lambda data, *_: self.chunks.append(data.copy()))
                stream.start()
            except Exception as err:
                log.info("mic unavailable: %s", err)
                self.busy = False
                self.indicator.show("Couldn't open a microphone", "error", hide_after=4)
                return
            self.stream = stream
            self.started = time.monotonic()
            self.watchdog = threading.Timer(MAX_RECORDING, self.stop)
            self.watchdog.daemon = True
            self.watchdog.start()
        play(START_SOUND, delay=0.2)
        self.show_listening()
        log.info("recording from default mic")

    def show_listening(self):
        target = "Claude" if self.to_claude else "this window"
        hint = "Right Alt finishes, Esc cancels" if self.to_claude else "Right Alt finishes, Right Ctrl → Claude, Esc cancels"
        self.indicator.show(f"Listening → {target}   ·   {hint}", "listening")

    def send_to_claude(self):
        if self.recording and not self.to_claude:
            self.to_claude = True
            self.show_listening()
            focus_claude()

    def end_recording(self):
        if self.watchdog:
            self.watchdog.cancel()
            self.watchdog = None
        self.stream.stop()
        self.stream.close()
        self.stream = None

    def stop(self):
        with self.lock:
            if not self.recording:
                return
            self.end_recording()
            duration = time.monotonic() - self.started
            chunks, self.chunks = self.chunks, []
            self.indicator.show("Transcribing…", "busy")
            threading.Thread(target=self.finish, args=(chunks, duration, self.generation), daemon=True).start()
        play(STOP_SOUND)

    def cancel(self):
        log.info("cancelled")
        self.generation += 1
        with self.lock:
            if self.recording:
                self.end_recording()
        self.busy = False
        self.indicator.show("Cancelled", "error", hide_after=1.5)

    def finish(self, chunks, duration, generation):
        import numpy as np

        try:
            if duration < MIN_SECONDS or not chunks:
                log.info("too short (%.2fs), ignored", duration)
                self.indicator.hide()
                return
            samples = np.concatenate(chunks).flatten().astype(np.float32) / 32768.0
            t = time.monotonic()
            segments, _ = self.model.transcribe(samples, language="en", vad_filter=True,
                                                condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("transcribed %.1fs of audio in %.2fs (%d characters)", duration, time.monotonic() - t, len(text))
            if generation != self.generation:
                return  # cancelled while transcribing
            if not text:
                self.indicator.show("No speech heard", "error", hide_after=2.5)
                return
            self.indicator.hide()
            self.paste(text)
        except Exception:
            log.exception("dictation failed")
            self.indicator.show(f"Dictation failed (see {LOG_DIR / 'dictate.log'})", "error", hide_after=5)
        finally:
            if generation == self.generation:
                self.busy = False

    def paste(self, text):
        import pyperclip

        previous = pyperclip.paste()
        pyperclip.copy(text)
        time.sleep(0.15)
        tap(VK_V, VK_CONTROL)
        log.info("pasted")
        time.sleep(0.5)  # put back what was on the clipboard before dictating
        if previous:
            pyperclip.copy(previous)


def worker(dictation, jobs):
    handlers = {"down": dictation.right_alt_down, "up": dictation.right_alt_up,
                "claude": lambda when: dictation.send_to_claude()}
    while True:
        name, when = jobs.get()
        try:
            handlers[name](when)
        except Exception:
            log.exception("error handling %s", name)


def watch_keys(dictation, jobs):
    swallowed = set()  # keys whose press we hid, so their release is hidden too

    @HOOKPROC
    def on_key(code, message, data):
        info = ctypes.cast(data, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if code == 0 and info.dwExtraInfo != OWN_INPUT:
            vk = info.vkCode
            is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = message in (WM_KEYUP, WM_SYSKEYUP)
            if is_up and vk in swallowed:
                swallowed.discard(vk)
                if vk == VK_RMENU:
                    jobs.put(("up", time.monotonic()))
                return 1
            if is_down and vk in swallowed:
                return 1  # auto-repeat of a key we already took
            if is_down and vk == VK_RMENU:
                swallowed.add(vk)
                jobs.put(("down", time.monotonic()))
                return 1
            if is_down and vk == VK_RCONTROL and dictation.recording:
                swallowed.add(vk)
                jobs.put(("claude", time.monotonic()))
                return 1
            if is_down and vk == VK_ESCAPE and dictation.busy:
                swallowed.add(vk)
                threading.Thread(target=dictation.cancel, daemon=True).start()
                return 1
        return user32.CallNextHookEx(None, code, message, data)

    hook = user32.SetWindowsHookExW(13, on_key, kernel32.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
    if not hook:
        log.error("keyboard hook failed: %s", ctypes.get_last_error())
        return
    log.info("keyboard hook installed")
    message = wt.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        pass


def load_in_background(dictation):
    try:
        log.info("loading %s", MODEL)
        dictation.model = load_model()
        log.info("ready")
    except Exception:
        log.exception("could not load the model")
        dictation.indicator.show(f"Dictation couldn't load (see {LOG_DIR / 'dictate.log'})", "error", hide_after=8)


def main():
    # Only one copy may own Right Alt.
    kernel32.CreateMutexW(None, False, "Local\\voice-dictation")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log.info("already running")
        return 0
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    dictation = Dictation()
    jobs = queue.Queue()
    threading.Thread(target=worker, args=(dictation, jobs), daemon=True).start()
    threading.Thread(target=watch_keys, args=(dictation, jobs), daemon=True).start()
    threading.Thread(target=load_in_background, args=(dictation,), daemon=True).start()
    dictation.indicator.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
