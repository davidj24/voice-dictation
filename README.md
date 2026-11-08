# Voice dictation

Tap a key, talk, tap again: your words are transcribed locally (Whisper large-v3-turbo on the GPU)
and pasted where you want them. No tokens are used for listening.

| Keys | What happens |
|---|---|
| Tap **Right Alt**, speak, tap **Right Alt** again | Text is pasted wherever your cursor is (Notion, Chrome, email, …) |
| **Right Ctrl** any time while dictating | Claude comes forward and the text goes into its prompt box, unsent |
| Hold **Right Alt** while speaking, let go | Push-to-talk: same as tapping twice |
| **Esc** while dictating, or **Cancel** on the status pill | Abort at any stage (connecting, listening, transcribing); nothing is pasted |

The microphone records only while a dictation is running, and stops by itself after 5 minutes.
If the headset mic doesn't connect within 4 seconds, it gives up. Esc and Right Ctrl behave normally
when no dictation is running. Pasting uses Ctrl+V, so terminals (Ctrl+Shift+V) won't receive the text.

**Cues:** a coloured pill at the bottom of the screen stays up the whole time: amber Connecting mic →
red Listening → blue Transcribing. It disappears when there's nothing going on.
A water-drop pop means start talking, and a ding plays as soon as you finish;
the text appears within about a second.

## How it fits together

- `~/Kanata/kanata.kbd` (pieces in `kanata-voice.kbd`): Right Alt, Esc and Right Ctrl send messages over kanata's TCP port (127.0.0.1:7070);
  the service holds kanata's `voice-active` virtual key during a dictation so Esc and Right Ctrl know to act on it.
- `dictate.py`: background service. On each dictation it
  1. switches the Bluetooth headset (Powerbeats) to headset mode so its mic works, records with `pw-record`, then switches back to music mode,
  2. transcribes,
  3. for Claude only: presses Super+N, GNOME's shortcut for the Nth dock app, to bring Claude forward (Claude must stay pinned in the first 10 dock slots;
     the dock's own number hotkeys were turned off so this only focuses Claude instead of opening its window overview),
     then types and deletes an "x" to put the cursor in the prompt box (Claude routes typed letters there),
  4. pastes with Ctrl+V and restores your previous clipboard.
- `overlay_helper.py`: the status pill, an XWayland window kept on top that never takes keyboard focus.
- `clipboard_helper.py`: sets the clipboard through XWayland, since GNOME doesn't let background programs set it directly.
- `voice-dictation.service`: systemd user service (symlinked into `~/.config/systemd/user/`).

## Useful commands

    journalctl --user -u voice-dictation -f      # live log, shows each transcription
    systemctl --user restart voice-dictation     # restart after editing dictate.py
    systemctl --user stop voice-dictation        # turn it off until next login

## Disk use and removal

About 4 GB: `.venv` (Python + CUDA libraries) and `models/` (Whisper).
`./uninstall.sh` stops the service, restores the kanata config from `kanata.kbd.backup`
(this undoes any kanata edits made after setup, so remove the VOICE DICTATION sections by hand instead if you've changed it since),
and deletes this folder.
