# TrackTracks — Project Re-Entry File
*Claude: read this before touching anything.*

---

## What This Is
Per-track and per-device CPU monitor for Ableton Live 12 on macOS.
Tells you exactly which track and plugin is killing your CPU — in real time.
Two-part system: a Remote Script inside Ableton + a PyQt6 desktop viewer.

## Re-Entry Phrase
> "Re-entry: TrackTracks"

## Current Status — ✅ Built, needs GitHub
- Remote Script: `remote_script/TrackCpuMonitor/__init__.py` (runs inside Ableton, streams UDP)
- Viewer app: `viewer/main.py` (PyQt6, receives UDP, shows live sparklines)
- Logo: `assets/tracktracks_logo.png`
- Build files: `build/launcher.sh`, `build/Info.plist`
- Zip: `TrackTracks.zip` on Desktop
- GitHub: **not yet set up**

## How It Works
```
Ableton Live 12
  └── Remote Script (Python, inside Ableton)
        reads device.cpu_load for every plugin on every track
        streams JSON over UDP → localhost:7400
              ↓
TrackTracks Viewer (PyQt6 desktop app)
  live sparkline bars, peak hold, per-device drill-down
```

## File Structure
```
track_cpu_monitor/
├── remote_script/
│   └── TrackCpuMonitor/
│       └── __init__.py     ← Runs inside Ableton Live
├── viewer/
│   ├── main.py             ← PyQt6 desktop app
│   └── requirements.txt
├── assets/
│   └── tracktracks_logo.png
├── build/
│   ├── launcher.sh
│   └── Info.plist
└── README.md
```

## What's Next (pick up here)
- [ ] Create GitHub repo (papjamzzz/track-tracks)
- [ ] First commit and push
- [ ] Add .gitignore
- [ ] Add Makefile and launch.command (match kalshi-edge pattern)
- [ ] Review logo — logo also on Desktop as tracktracks_logo.png

## How to Run
```bash
cd ~/track_cpu_monitor/viewer
pip install -r requirements.txt
python main.py
# Also install Remote Script into Ableton's MIDI Remote Scripts folder
```

## Pushing Changes to GitHub (once repo is created)
```bash
cd ~/track_cpu_monitor
git add .
git commit -m "describe what changed"
git push origin main
# Username: papjamzzz
# Password: mac-push token (saved in Notes)
```

---
*Last updated: 2026-03-10*
