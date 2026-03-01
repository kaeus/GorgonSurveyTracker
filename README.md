# GorgonSurveyTracker

A transparent overlay tool for **Project Gorgon** that helps players track and collect survey map items in real time.

## Features

- Two always-on-top transparent overlays — map canvas and inventory grid
- Monitors `player.log` for `ProcessMapFx` events and automatically places survey item dots on the map
- First dot requires a manual click to calibrate the pixel-to-meter scale; subsequent dots are placed automatically
- Nearest-neighbour route optimisation with guided step-through
- Watches ChatLogs for `"X collected!"` messages and removes items from the inventory grid
- Per-overlay opacity control and click-through toggle
- Drag-to-reposition overlays; window positions and settings persist across sessions

## Requirements

- Python 3.8+
- PyQt5

```
pip install PyQt5
```

## Running from source

```
python survey_tracker.py
```

## Usage

1. Open the **Control Panel** and set your survey count (number of maps you are running).
2. Click **Set Player Position** and click your character's position on the map overlay to establish the origin.
3. Start surveying in-game. Each `ProcessMapFx` log entry will place a dot on the overlay.
4. On the first dot, click it on the map to calibrate the scale. Subsequent dots appear automatically.
5. Use **Start Route** to step through the nearest-neighbour path to collect each item efficiently.
6. Collect items in-game — they are removed from the inventory grid automatically via chat log monitoring.

## Building the exe

The project uses [PyInstaller](https://pyinstaller.org/) to produce a single `GorgonSurveyTracker.exe`.

```
pip install pyinstaller
pyinstaller --onefile --windowed --name GorgonSurveyTracker --add-data "version.txt;." survey_tracker.py
```

The built exe is found in `dist/GorgonSurveyTracker.exe`.

## Automated Builds (GitHub Actions)

Every push to `main` automatically:

1. Increments the minor version in `version.txt` (e.g. `1.0.0` → `1.1.0`).
2. Builds `GorgonSurveyTracker.exe` with PyInstaller on a Windows runner.
3. Creates a tagged GitHub Release and attaches the exe as a downloadable asset.

## Versioning

Versions follow `MAJOR.MINOR.PATCH`. The CI pipeline bumps the **minor** component on every build. To reset or change the base version, edit `version.txt` directly and push to `main`.
