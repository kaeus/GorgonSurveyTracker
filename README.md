# GorgonSurveyTracker

A transparent overlay tool for **Project Gorgon** that helps players track and collect survey map items in real time.

## Overview

- Monitors the game's ChatLogs folder for `[Status]` distance messages and automatically places survey item dots on the map
- First dot requires a manual click to calibrate the pixel-to-meter scale; subsequent dots are placed automatically
- Nearest-neighbour route optimisation with guided step-through
- Detects `"X collected!"` messages from the same chat log and removes items from the inventory grid
- Two always-on-top transparent overlays — map canvas and inventory grid
- Per-overlay opacity control and click-through toggle
- Drag-to-reposition overlays; window positions and settings persist across sessions

## Screenshots

![Tracker Window](/ReadmeScreens/TrackerWindow.png?raw=true "Main Tracker Window")

![Overlays](/ReadmeScreens/Overlay.png?raw=true "Example Overlays")

## In game setup

The program relies on chat logs. To enable these, go to the Game Settings, under the V.I.P. category, and make sure you tick 'Enabled' for 'Save Chat Logs' under the Chat Logs section. The program will automatically attempt to locate the chat log folder based on it's default location, but should that not work, this section also has an 'Open Folder' button that should show you where they are saved to.

## Usage (Normal Surveys)

1. Open the **Control Panel**, click **💬 ChatLogs folder**, and select your Project Gorgon `ChatLogs` directory. The program will attempt to locate the default directory.
2. Drag the Map and Inventory overlays over you map, and you bags. Both of these should be resizeable windows you can click and drag in the bottom right of their section. The boxes should roughly align with your inventory slots.
3. Set your survey count (number of maps you are running). Make sure these start in the top left of your inventory and go without any non survey maps inbetween.
4. Click **📍 Set My Position** and click your character's position on the map overlay to establish the origin.
5. Begin surveying in-game. Each `[Status] The X is Ym DIR` chat message places a dot on the overlay.
6. On the first area, click it on the map to calibrate the scale. Subsequent dots appear automatically, but can be adjusted by clicking the map.
7. Use **🗺 Optimize Route** to step through the nearest-neighbour path to collect each item efficiently.
8. Collect items in-game — they are removed from the inventory grid automatically when the chat log shows `"X collected!"`. Click **→ Skip to Next** to skip a node if necessary.
9. Profit!

> **Tip:** The inventory grid layout (`cols`, `slot_size`, `slot_gap`) can be tweaked by editing the `grid` section in `survey_tracker_settings.json` if you dont want to use 10 columns, or your inferace is scaled differently.

## Usage (Motherlode)

Motherlode surveys give only a raw distance (`The treasure is 2733 meters from here`) with no bearing. The tracker uses three surveying positions to trilaterate each treasure's location.

### Setup

1. Click **Motherlode Survey** in the control panel to switch modes. The regular survey controls are replaced with motherlode controls.
2. Make sure your ChatLogs folder is set (same as Normal Surveys).

### Surveying (3 rounds)

3. Click your current position on the map overlay. The status label will confirm **Position 1 set**.
4. In-game, use the survey tool to scan every motherlode in the area. Each `The treasure is X meters from here` message is recorded automatically — the inventory overlay fills up with slots as distances arrive.
5. When you have scanned all motherlodes from Position 1, click **Next Position** in the control panel.
6. Walk to a new location (ideally not in a straight line from Position 1), click the map to set **Position 2**, then scan all motherlodes again and click **Next Position**.
7. Repeat for **Position 3**.

> **Important:** The three positions you click on the map must be placed with **proportionally correct spacing** relative to each other. If you walked twice as far between P1→P2 as between P2→P3, the canvas clicks should reflect that ratio. Use in-game landmarks or known distances as a guide.

> **Tip:** Scan motherlodes in the same order each round — the tracker matches distances positionally (first distance = Treasure 1, second = Treasure 2, etc.).

### Collecting

8. After round 3, the tracker automatically computes the scale and trilaterated locations. Estimated positions appear as coloured dots on the map overlay, and a dashed route line guides you through them in an optimised order.
9. Collect each motherlode in-game. The `X Metal Slab added to inventory` message triggers automatic progression — the collected slot disappears from the inventory, the player marker moves to that location, and the next target is highlighted.
10. Click **Skip** to skip the current target and advance to the next one if needed.
11. Click **Reset Motherlode** to start a fresh survey run.

> **Tip:** The computed pixel/metre scale is shown in the control panel after trilateration. A fit quality warning appears if the estimated circles don't converge well — this usually means the three positions weren't placed proportionally, or you dont have enough samples to computer well.

## Build Requirements

- Python 3.8+
- PyQt5

```
pip install PyQt5
```

## macOS / Linux Support

The overlay runs on macOS and Linux (X11) with some additional optional dependencies for click-through support.

### macOS

Install PyObjC to enable the click-through toggle on macOS:

```
pip install pyobjc-framework-Cocoa
```

Without this, the overlay will display correctly but the click-through toggle will have no effect.

### Linux (X11)

Install python-xlib to enable click-through on X11:

```
pip install python-xlib
```

> **Note:** Overlay might have additional issues if running under some sort of Windows emulation

## Running from source

```
python survey_tracker.py
```
