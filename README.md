# Voice dictation

Hold a key, talk, release: your words are transcribed locally (Whisper large-v3-turbo on the GPU)
and pasted where you want them. No tokens are used for listening.

| Keys | What happens |
|---|---|
| Hold **Right Alt**, speak, release | Text is pasted wherever your cursor is (Notion, Chrome, email, …) |
| Hold **Right Alt** + **Shift**, speak, release | Claude comes forward and the text is pasted into its prompt box, unsent |

Shift can be pressed at any point during the hold. The microphone records only while Right Alt is held.
Pasting uses Ctrl+V, so in terminals (which paste with Ctrl+Shift+V) plain Right Alt won't work.

**Cues:** a banner at the top of the screen shows Connecting mic → Listening → Transcribing.
A water-drop pop means start talking, and a ding plays as soon as you let go;
the text appears within about a second.

## How it fits together

- `~/Kanata/kanata.kbd` (pieces in `kanata-voice.kbd`): Right Alt sends messages over kanata's TCP port (127.0.0.1:7070) instead of acting as Alt.
- `dictate.py`: background service. On each dictation it
  1. switches the Bluetooth headset (Powerbeats) to headset mode so its mic works, records with `pw-record`, then switches back to music mode,
  2. transcribes,
  3. for Claude only: presses Super+N, GNOME's shortcut for the Nth dock app, to bring Claude forward (Claude must stay pinned in the first 10 dock slots;
     the dock's own number hotkeys were turned off so this only focuses Claude instead of opening its window overview),
     then types and deletes an "x" to put the cursor in the prompt box (Claude routes typed letters there),
  4. pastes with Ctrl+V and restores your previous clipboard.
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
