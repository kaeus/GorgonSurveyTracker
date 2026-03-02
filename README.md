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
5. Click **▶ Start Survey** and begin surveying in-game. Each `[Status] The X is Ym DIR` chat message places a dot on the overlay.
6. On the first area, click it on the map to calibrate the scale. Subsequent dots appear automatically, but can be adjusted by clicking the map.
7. Use **🗺 Optimize Route** to step through the nearest-neighbour path to collect each item efficiently.
8. Collect items in-game — they are removed from the inventory grid automatically when the chat log shows `"X collected!"`. Click **→ Skip to Next** to skip a node if necessary.
9. Profit!

> **Tip:** The inventory grid layout (`cols`, `slot_size`, `slot_gap`) can be tweaked by editing the `grid` section in `survey_tracker_settings.json` if you dont want to use 10 columns, or your inferace is scaled differently.

## Usage (Motherlode)

1. Coming in a future update!

## Build Requirements

- Python 3.8+
- PyQt5

```
pip install PyQt5
```

## Running from source

```
python survey_tracker.py
```
