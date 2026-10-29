# Live Captions Capture

Reads the text from the Windows 11 **Live Captions** window and writes it to plain-text files as it appears, so other tools can use the running transcript.

`capture_live_captions.py` polls the Live Captions window through the Windows UI Automation API, de-duplicates overlapping text, and keeps three files up to date:

- `live_captions.txt` — the full transcript for the session.
- `live_captions_recent.txt` — just the most recent text (last ~1500 chars).
- `live_captions_background.txt` — a larger rolling window (~12000 chars).

(`live_captions.txt.tmp` is a temporary write buffer.)

## Requirements

- Windows 11 with **Live Captions** turned on (Settings → Accessibility → Captions, or `Win` + `Ctrl` + `L`).
- Python 3.11.

The folder has its own virtual environment in `.venv`. Dependencies are in `requirements.txt` (`uiautomation`, `python-docx`).

To rebuild the environment from scratch:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Running

Turn on Windows Live Captions first, then:

- **Double-click `start_capture_console.bat`** — restarts capture and shows it in a console window.
- Or run `start_capture.ps1`:

  ```powershell
  .\start_capture.ps1            # start minimized in the background
  .\start_capture.ps1 -Console   # start in this window
  .\start_capture.ps1 -Restart   # stop any running copy, then start
  .\start_capture.ps1 -Stop      # stop the running copy
  ```

On start it clears the four transcript files for a fresh session.

### Options

`capture_live_captions.py` takes `--output`, `--window-hint`, `--poll-seconds`, `--stable-seconds`, and `--line-width`. Run it with `--help` for the full list. Defaults: polls every 0.8s, treats text as settled after 1.4s, output file next to the script.

## Note on the `/l` command

The project's `/l` slash command reads `live captions/live_captions_recent.txt`. If you move or rename this folder, update the path in `.claude/commands/l.md` and `.cursor/commands/l.md`.

## Also in this folder

`export_resume_from_docx.py` extracts plain text from a `.docx` resume. It is unrelated to caption capture and just lives here; by default it looks for `Jasen_Lee.docx` next to itself.
