#!/usr/bin/env python3
"""
GorgonSurveyTracker — Transparent overlay for Project Gorgon survey maps.

Install deps:   pip install PyQt5
Run:            python survey_tracker.py

Key features:
  • Two always-on-top transparent overlays (map canvas + inventory grid)
  • Drag to reposition, per-overlay opacity
  • Watches ChatLogs for [Status] distance messages → places survey dots
  • First dot: click to calibrate scale; subsequent dots: auto-placed
  • Click during survey → recalibrates using most recent dot
  • Nearest-neighbour route optimisation with guided step-through
  • Watches ChatLogs for "X collected!" → removes from inventory
  • "Flip Dirs" toggle inverts N/S and E/W for areas with reversed coordinates
  • All positions/settings saved to JSON in the same folder as this script
"""

import sys
import os
import re
import json
import math
import time
import ctypes
import threading
import datetime
import subprocess
import tempfile
import webbrowser
import urllib.request

# ── pynput — optional cross-platform input library ────────────────────────────
try:
    import pynput.keyboard as _pynput_kb
    import pynput.mouse    as _pynput_mouse
    _PYNPUT_AVAILABLE = True
except ImportError:
    _pynput_kb        = None
    _pynput_mouse     = None
    _PYNPUT_AVAILABLE = False

# Global hotkeys do not work under Wayland; detect it early.
_WAYLAND = (
    sys.platform.startswith('linux')
    and os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
)
# Feature gate: True when a cross-platform listener can be started.
_HOTKEY_SUPPORTED = _PYNPUT_AVAILABLE and not _WAYLAND
from collections import Counter
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QSlider, QSpinBox,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFrame, QGroupBox,
    QFileDialog, QMessageBox, QSizeGrip, QProgressDialog,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QSizePolicy
)
from PyQt5.QtCore  import Qt, QTimer, QPoint, QSize, pyqtSignal, QObject
from PyQt5.QtGui   import (
    QPainter, QColor, QPen, QBrush, QFont, QCursor, QMouseEvent
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
GRID_COLS   = 10
GRID_ROWS   = 8
SLOT_SIZE   = 50          # px
SLOT_GAP    = 2           # px
HEADER_H    = 28          # px — header height for both overlays

# Qt.Key_* → human-readable label (platform-neutral; used by HotkeyCaptureDialog)
# Populated after Qt is imported so Qt.Key_* constants are available.
def _build_qt_key_label_map():
    return {
        Qt.Key_Backspace: 'Backspace', Qt.Key_Tab:     'Tab',
        Qt.Key_Return:    'Enter',     Qt.Key_Escape:  'Esc',
        Qt.Key_Space:     'Space',
        Qt.Key_PageUp:    'PgUp',      Qt.Key_PageDown: 'PgDn',
        Qt.Key_End:       'End',       Qt.Key_Home:    'Home',
        Qt.Key_Left:      'Left',      Qt.Key_Up:      'Up',
        Qt.Key_Right:     'Right',     Qt.Key_Down:    'Down',
        Qt.Key_Insert:    'Insert',    Qt.Key_Delete:  'Delete',
        Qt.Key_F1:  'F1',  Qt.Key_F2:  'F2',  Qt.Key_F3:  'F3',  Qt.Key_F4:  'F4',
        Qt.Key_F5:  'F5',  Qt.Key_F6:  'F6',  Qt.Key_F7:  'F7',  Qt.Key_F8:  'F8',
        Qt.Key_F9:  'F9',  Qt.Key_F10: 'F10', Qt.Key_F11: 'F11', Qt.Key_F12: 'F12',
        Qt.Key_Asterisk: 'Num*', Qt.Key_Plus: 'Num+',
        Qt.Key_Minus:    'Num-', Qt.Key_Period: 'Num.', Qt.Key_Slash: 'Num/',
    }

_QT_KEY_LABEL_MAP  = None   # initialised lazily on first use (Qt must be imported first)

# Qt modifier-only keys — ignore during hotkey capture
_QT_MODIFIER_KEYS = None    # initialised lazily

def _get_qt_key_label_map():
    global _QT_KEY_LABEL_MAP
    if _QT_KEY_LABEL_MAP is None:
        _QT_KEY_LABEL_MAP = _build_qt_key_label_map()
    return _QT_KEY_LABEL_MAP

def _get_qt_modifier_keys():
    global _QT_MODIFIER_KEYS
    if _QT_MODIFIER_KEYS is None:
        _QT_MODIFIER_KEYS = {
            Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta,
            Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock,
        }
    return _QT_MODIFIER_KEYS

# Windows VK → (qt_key_int, qt_mods_int) — used only for migrating old settings
def _build_vk_to_qt():
    kp = int(Qt.KeypadModifier)
    return {
        0x60: (int(Qt.Key_0), kp), 0x61: (int(Qt.Key_1), kp),
        0x62: (int(Qt.Key_2), kp), 0x63: (int(Qt.Key_3), kp),
        0x64: (int(Qt.Key_4), kp), 0x65: (int(Qt.Key_5), kp),
        0x66: (int(Qt.Key_6), kp), 0x67: (int(Qt.Key_7), kp),
        0x68: (int(Qt.Key_8), kp), 0x69: (int(Qt.Key_9), kp),
        0x70: (int(Qt.Key_F1),  0), 0x71: (int(Qt.Key_F2),  0),
        0x72: (int(Qt.Key_F3),  0), 0x73: (int(Qt.Key_F4),  0),
        0x74: (int(Qt.Key_F5),  0), 0x75: (int(Qt.Key_F6),  0),
        0x76: (int(Qt.Key_F7),  0), 0x77: (int(Qt.Key_F8),  0),
        0x78: (int(Qt.Key_F9),  0), 0x79: (int(Qt.Key_F10), 0),
        0x7A: (int(Qt.Key_F11), 0), 0x7B: (int(Qt.Key_F12), 0),
        0x2D: (int(Qt.Key_Insert),   0), 0x2E: (int(Qt.Key_Delete),   0),
        0x24: (int(Qt.Key_Home),     0), 0x23: (int(Qt.Key_End),      0),
        0x21: (int(Qt.Key_PageUp),   0), 0x22: (int(Qt.Key_PageDown), 0),
        0x25: (int(Qt.Key_Left),     0), 0x26: (int(Qt.Key_Up),       0),
        0x27: (int(Qt.Key_Right),    0), 0x28: (int(Qt.Key_Down),     0),
    }

def _migrate_vk_config(hk: dict) -> dict:
    """Convert an old {'vk': <win_vk>, ...} hotkey config to the new Qt-key format."""
    vk    = hk.get('vk', 0x60)
    label = hk.get('label', 'Num0')
    mods  = hk.get('modifiers', [])
    qt_key, qt_mods = _build_vk_to_qt().get(vk, (int(Qt.Key_0), int(Qt.KeypadModifier)))
    mod_flags = 0
    if 'ctrl'  in mods: mod_flags |= int(Qt.ControlModifier)
    if 'shift' in mods: mod_flags |= int(Qt.ShiftModifier)
    if 'alt'   in mods: mod_flags |= int(Qt.AltModifier)
    qt_mods |= mod_flags
    return {'qt_key': qt_key, 'qt_mods': qt_mods, 'modifiers': mods, 'label': label}

SETTINGS_PATH = (
    Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
) / "survey_tracker_settings.json"

_GORGON_CHAT_DEFAULT = (
    Path(os.environ.get('LOCALAPPDATA', '~')).parent
    / 'LocalLow' / 'Elder Game' / 'Project Gorgon' / 'ChatLogs'
)

# ─────────────────────────────────────────────────────────────────────────────
# Version
# ─────────────────────────────────────────────────────────────────────────────
def _resource_path(relative: str) -> Path:
    """Return absolute path to a bundled resource (works for PyInstaller --onefile)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative

_ver_file = _resource_path("version.txt")
APP_VERSION = _ver_file.read_text().strip() if _ver_file.exists() else "dev"

# ─────────────────────────────────────────────────────────────────────────────
# Update check — queries GitHub Releases for a newer version.
# ─────────────────────────────────────────────────────────────────────────────
_UPDATE_REPO       = "kaeus/GorgonSurveyTracker"
_UPDATE_API_URL    = f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest"
_UPDATE_PAGE_URL   = f"https://github.com/{_UPDATE_REPO}/releases/latest"
_UPDATE_ASSET_NAME = "GorgonSurveyTracker.exe"


def _parse_version(s):
    """Parse 'v1.20.0' / '1.20.0' → (1, 20, 0). Returns (0,0,0) for unparseable strings like 'dev'."""
    if not s:
        return (0, 0, 0)
    s = s.strip().lstrip('vV')
    parts = []
    for p in s.split('.'):
        m = re.match(r'(\d+)', p)
        if not m:
            return (0, 0, 0) if not parts else tuple(parts)
        parts.append(int(m.group(1)))
    return tuple(parts) if parts else (0, 0, 0)


def _is_frozen_windows():
    return sys.platform == 'win32' and getattr(sys, 'frozen', False)

# ─────────────────────────────────────────────────────────────────────────────
# Log-parsing helpers
# ─────────────────────────────────────────────────────────────────────────────
_DIST_RE         = re.compile(r'(\d+(?:\.\d+)?)m\s+(west|east|north|south)', re.IGNORECASE)
_COLLECT_RE      = re.compile(r'\[Status\]\s+(.+?)\s+(?:x\d+\s+)?collected!')
_SURVEY_CHAT_RE  = re.compile(r'\[Status\]\s+The\s+(.+?)\s+is\s+(.+)', re.IGNORECASE)
_ML_DIST_RE      = re.compile(r'\[Status\]\s+The treasure is (\d+(?:\.\d+)?) meters from here\.', re.IGNORECASE)
_ML_COLLECT_RE   = re.compile(r'\[Status\]\s+(?:.+?) Metal Slab x\d+ added to inventory\.', re.IGNORECASE)
_XP_RE           = re.compile(r'\[Status\]\s+You earned ([\d,]+) XP in (.+?)\.', re.IGNORECASE)
_INV_ADD_RE      = re.compile(r'\[Status\]\s+(.+?)\s+x(\d+)\s+added to inventory\.', re.IGNORECASE)
_BONUS_RE        = re.compile(r'Also found (.+?)(?:\s+x(\d+))?\s+\(speed bonus', re.IGNORECASE)
_XP_SKILLS       = frozenset({'surveying', 'mining', 'geology'})
_ENTER_AREA_RE   = re.compile(r'\*{3,}\s*Entering Area:\s*(.+?)\s*$')

# Zones whose in-game coordinates are reversed relative to the map image.
# Flip Dirs auto-toggles ON when entering one of these, OFF otherwise.
FLIPPED_ZONES    = frozenset({'Kur Mountains'})


def parse_enter_area_line(line: str):
    """Return the area name from an 'Entering Area:' chat line, or None."""
    m = _ENTER_AREA_RE.search(line)
    return m.group(1).strip() if m else None


def parse_chat_survey_line(line: str):
    """Return (name, offset_dict) or None — parses chat [Status] distance messages."""
    m = _SURVEY_CHAT_RE.search(line)
    if not m:
        return None
    name = m.group(1).strip()
    desc = m.group(2)
    east = north = 0.0
    for dm in _DIST_RE.finditer(desc):
        dist, direction = float(dm.group(1)), dm.group(2).lower()
        if   direction == 'east':  east  += dist
        elif direction == 'west':  east  -= dist
        elif direction == 'north': north += dist
        elif direction == 'south': north -= dist
    if east == 0.0 and north == 0.0:
        return None
    return name, {'east': east, 'north': north}


def parse_collect_line(line: str):
    """Return item name string or None."""
    m = _COLLECT_RE.search(line)
    return m.group(1).strip() if m else None


def parse_ml_dist_line(line: str):
    """Return float distance in metres from a motherlode survey line, or None."""
    m = _ML_DIST_RE.search(line)
    return float(m.group(1)) if m else None


def parse_ml_collect_line(line: str) -> bool:
    """Return True if line is a motherlode Metal Slab collection event."""
    return bool(_ML_COLLECT_RE.search(line))


def clean_name(name: str) -> str:
    return re.sub(r'\s+(is here|found)[.!]?\s*$', '', name, flags=re.IGNORECASE).strip()


def pt_dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def trilaterate(p1, r1, p2, r2, p3, r3):
    """Find intersection point of 3 circles (centres in pixels, radii in pixels).

    Subtracts circle equations pairwise to form two linear equations, then
    solves the 2×2 system analytically.
    Returns (x, y) or None if positions are collinear (|det| < 1e-6).
    """
    x1, y1 = p1;  x2, y2 = p2;  x3, y3 = p3
    A1 = 2 * (x2 - x1);  B1 = 2 * (y2 - y1)
    C1 = r1*r1 - r2*r2 - x1*x1 + x2*x2 - y1*y1 + y2*y2
    A2 = 2 * (x3 - x1);  B2 = 2 * (y3 - y1)
    C2 = r1*r1 - r3*r3 - x1*x1 + x3*x3 - y1*y1 + y3*y3
    det = A1 * B2 - A2 * B1
    if abs(det) < 1e-6:
        return None
    return ((C1 * B2 - C2 * B1) / det,
            (A1 * C2 - A2 * C1) / det)


def ml_solve_scale(positions, surveys):
    """Analytically derive px/metre scale from motherlode circle convergence.

    For each motherlode the trilateration result T_j = (s²·αx+βx, s²·αy+βy).
    Substituting back into circle-1's equation gives Pj·u² + Qj·u + Rj = 0
    where u = s².  Summing over all motherlodes gives one quadratic in u.

    Returns positive float scale, or None if the solve fails / is implausible.
    Requires positions to be placed with proportionally correct pixel spacing.
    """
    if len(positions) < 3:
        return None
    p1, p2, p3 = positions[0], positions[1], positions[2]
    A1 = 2*(p2[0]-p1[0]);  B1 = 2*(p2[1]-p1[1])
    A2 = 2*(p3[0]-p1[0]);  B2 = 2*(p3[1]-p1[1])
    det = A1*B2 - A2*B1
    if abs(det) < 1e-6:
        return None

    P_tot = Q_tot = R_tot = 0.0
    valid = 0
    for entry in surveys:
        dsts = entry['distances']
        if len(dsts) < 3:
            continue
        d1, d2, d3 = dsts[0], dsts[1], dsts[2]
        if d1 == 0 or d2 == 0 or d3 == 0:
            continue

        K1 = d1*d1 - d2*d2
        L1 = p2[0]*p2[0] - p1[0]*p1[0] + p2[1]*p2[1] - p1[1]*p1[1]
        K2 = d1*d1 - d3*d3
        L2 = p3[0]*p3[0] - p1[0]*p1[0] + p3[1]*p3[1] - p1[1]*p1[1]

        ax = (K1*B2 - K2*B1) / det
        bx = (L1*B2 - L2*B1) / det
        ay = (A1*K2 - A2*K1) / det
        by = (A1*L2 - A2*L1) / det

        gx = bx - p1[0]
        gy = by - p1[1]

        P_tot += ax*ax + ay*ay
        Q_tot += 2*(ax*gx + ay*gy) - d1*d1
        R_tot += gx*gx + gy*gy
        valid += 1

    if valid == 0:
        return None

    # Solve P_tot·u² + Q_tot·u + R_tot = 0  (u = s²)
    if abs(P_tot) < 1e-12:
        if abs(Q_tot) < 1e-12:
            return None
        u = -R_tot / Q_tot
        candidates = [u] if u > 1e-6 else []
    else:
        disc = Q_tot*Q_tot - 4*P_tot*R_tot
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        candidates = [u for u in ((-Q_tot + sq) / (2*P_tot),
                                   (-Q_tot - sq) / (2*P_tot)) if u > 1e-6]

    if not candidates:
        return None

    # Pick the root that gives the most plausible scale (0.05 – 200 px/m)
    plausible = [math.sqrt(u) for u in candidates if 0.0025 <= u <= 40000]
    if not plausible:
        return None
    return plausible[0]


# ─────────────────────────────────────────────────────────────────────────────
# Click-through helpers  (Windows + macOS via PyObjC)
# ─────────────────────────────────────────────────────────────────────────────
_GWL_EXSTYLE     = -20
_WS_EX_LAYERED   = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020


def _set_click_through(hwnd_int: int, enabled: bool):
    if sys.platform == 'win32':
        try:
            user32 = ctypes.windll.user32
            style  = user32.GetWindowLongW(hwnd_int, _GWL_EXSTYLE)
            if enabled:
                style |= (_WS_EX_LAYERED | _WS_EX_TRANSPARENT)
            else:
                style &= ~_WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd_int, _GWL_EXSTYLE, style)
        except Exception:
            pass
    elif sys.platform == 'darwin':
        try:
            from AppKit import NSView  # PyObjC
            ns_view   = NSView(hwnd_int)
            ns_window = ns_view.window()
            if ns_window:
                ns_window.setIgnoresMouseEvents_(bool(enabled))
        except ImportError:
            pass  # PyObjC not available
        except Exception:
            pass
    elif sys.platform.startswith('linux'):
        try:
            from Xlib import display, X
            from Xlib.ext import shape
            d   = display.Display()
            win = d.create_resource_object('window', hwnd_int)
            if enabled:
                # Empty input rectangle list → all clicks pass through
                win.shape_rectangles(shape.SO.Set, shape.SK.Input,
                                     X.Unsorted, 0, 0, [])
            else:
                # Clear override → input region reverts to bounding box
                win.shape_mask(shape.SO.Set, shape.SK.Input, 0, 0, X.NONE)
            d.sync()
        except ImportError:
            pass  # python-xlib not installed
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Hotkey Button overload
# ─────────────────────────────────────────────────────────────────────────────
class HotkeySignal(QObject):
    right_clicked = pyqtSignal()

class HotkeyButton(QPushButton):
    def __init__(self, text):
        super(HotkeyButton, self).__init__()
        self.signal_emitter = HotkeySignal()
        self.setText(text)

    def mousePressEvent(self, QMouseEvent):
        if QMouseEvent.button() == Qt.LeftButton:
            self.clicked.emit()
        elif QMouseEvent.button() == Qt.RightButton:
            self.signal_emitter.right_clicked.emit()


# ─────────────────────────────────────────────────────────────────────────────
# Survey state
# ─────────────────────────────────────────────────────────────────────────────
class SurveyState:
    def __init__(self):
        self.phase         = 'idle'   # idle | set_player | calibrating | surveying | routing
        self.player_pos    = None     # (x, y) canvas coords
        self.scale         = None     # px / metre
        self.items         = []
        self.pending_calib = None
        self.route_order   = []       # item ids in optimised order
        self.route_idx     = -1
        self._next_id      = 0
        self.survey_count  = 0        # 0 = show only found items; >0 = user-set total

        # ── Motherlode mode ───────────────────────────────────────────────
        self.ml_mode        = False     # True = motherlode mode active
        self.ml_round       = 0         # 0/1/2 — which round is being collected
        self.ml_phase       = 'set_pos' # 'set_pos' | 'survey'
        self.ml_positions   = []        # [(px,py), ...] canvas coords per committed round
        self.ml_surveys     = []        # [{id, distances, estimated_pos, collected, route_order}, ...]
        self.ml_pending     = []        # distances gathered this round (uncommitted)
        self._ml_next_id    = 0
        self.ml_route_order = []        # entry ids in optimised order (after trilateration)
        self.ml_route_idx   = -1        # current position in route

    def ml_add_entry(self):
        """Append a blank motherlode entry and return it."""
        self._ml_next_id += 1
        entry = {
            'id':            self._ml_next_id,
            'distances':     [],
            'estimated_pos': None,
            'collected':     False,
            'route_order':   -1,
        }
        self.ml_surveys.append(entry)
        return entry

    @property
    def ml_active_id(self):
        if (self.ml_round >= 3
                and 0 <= self.ml_route_idx < len(self.ml_route_order)):
            return self.ml_route_order[self.ml_route_idx]
        return None

    def add_item(self, name, offset):
        self._next_id += 1
        uncollected = [i for i in self.items if not i['collected']]
        item = {
            'id':              self._next_id,
            'name':            name,
            'offset':          offset,        # {'east': float, 'north': float}
            'pixel_pos':       None,          # (x, y) in canvas coords  (y = 0 at canvas top)
            'pixel_estimates': [],            # all auto-placed estimates, averaged for precision
            'grid_index':      len(uncollected),
            'collected':       False,
            'skipped':         False,
            'route_order':     -1,
        }
        self.items.append(item)
        return item

    def reindex(self):
        for idx, item in enumerate(i for i in self.items if not i['collected']):
            item['grid_index'] = idx

    def player_to_pixel(self, offset, canvas_w, canvas_h, invert_dirs=False):
        if not self.player_pos or not self.scale:
            return None
        sign = -1 if invert_dirs else 1
        px = self.player_pos[0] + sign * offset['east']  *  self.scale
        py = self.player_pos[1] - sign * offset['north'] *  self.scale  # north = up = –y
        return (max(6.0, min(canvas_w - 6.0, px)),
                max(6.0, min(canvas_h - 6.0, py)))

    def optimise_route(self):
        candidates = [i for i in self.items if not i['collected'] and i['pixel_pos']]
        if not candidates:
            return
        start     = self.player_pos or (0.0, 0.0)
        remaining = list(candidates)
        route     = []
        current   = start
        while remaining:
            nearest = min(remaining, key=lambda i: pt_dist(current, i['pixel_pos']))
            route.append(nearest['id'])
            current = nearest['pixel_pos']
            remaining.remove(nearest)
        self.route_order = self._two_opt(route)
        for idx, iid in enumerate(self.route_order):
            item = next((i for i in self.items if i['id'] == iid), None)
            if item:
                item['route_order'] = idx

    def _two_opt(self, route: list) -> list:
        """Improve a route with 2-opt edge swaps until no swap reduces total distance."""
        if len(route) < 4:
            return route
        pos   = {i['id']: i['pixel_pos'] for i in self.items}
        start = self.player_pos or (0.0, 0.0)
        pts   = [start] + [pos[iid] for iid in route]
        ids   = [None]  + list(route)
        n     = len(pts)
        improved = True
        while improved:
            improved = False
            for i in range(n - 2):
                for j in range(i + 2, n - 1):
                    d_old = pt_dist(pts[i], pts[i+1]) + pt_dist(pts[j], pts[j+1])
                    d_new = pt_dist(pts[i], pts[j])   + pt_dist(pts[i+1], pts[j+1])
                    if d_new < d_old - 1e-9:
                        pts[i+1:j+1] = pts[i+1:j+1][::-1]
                        ids[i+1:j+1] = ids[i+1:j+1][::-1]
                        improved = True
        return ids[1:]

    @property
    def active_id(self):
        if self.phase == 'routing' and 0 <= self.route_idx < len(self.route_order):
            return self.route_order[self.route_idx]
        return None

    def uncollected(self):
        return [i for i in self.items if not i['collected']]


# ─────────────────────────────────────────────────────────────────────────────
# Drag-mixin  — shared by both overlays
# ─────────────────────────────────────────────────────────────────────────────
class DragMixin:
    """Add drag-by-header behaviour.  Call _drag_press/move/release in mouse events."""

    def _drag_init(self):
        self._dragging   = False
        self._drag_start = QPoint()
        self._drag_origin = QPoint()

    def _drag_press(self, event, header_height=HEADER_H):
        if event.button() == Qt.LeftButton and event.y() < header_height:
            self._dragging    = True
            self._drag_start  = event.globalPos()
            self._drag_origin = self.pos()

    def _drag_move(self, event):
        if self._dragging:
            delta = event.globalPos() - self._drag_start
            new_pos = self._drag_origin + delta
            # Keep top at least below system bar
            new_pos.setY(max(0, new_pos.y()))
            self.move(new_pos)

    def _drag_release(self, event):
        if self._dragging:
            self._dragging = False
            self._on_drag_finished()

    def _on_drag_finished(self):
        """Override to save position."""
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Resize grip  (bottom-right corner of frameless window)
# ─────────────────────────────────────────────────────────────────────────────
class ResizeGrip(QWidget):
    SIZE = 12

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.SizeFDiagCursor)
        self._resizing   = False
        self._start_gpos = QPoint()
        self._start_size = QSize()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._resizing   = True
            self._start_gpos = e.globalPos()
            self._start_size = self.window().size()

    def mouseMoveEvent(self, e):
        if self._resizing:
            delta = e.globalPos() - self._start_gpos
            w = max(220, self._start_size.width()  + delta.x())
            h = max(100, self._start_size.height() + delta.y())
            self.window().resize(w, h)

    def mouseReleaseEvent(self, e):
        self._resizing = False
        if hasattr(self.window(), '_on_drag_finished'):
            self.window()._on_drag_finished()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(QColor(180, 180, 180, 90))
        for r in range(3):
            for c in range(3 - r):
                px = 3 + c * 4
                py = 3 + (2 - r) * 4 + r * 4 - c
                # simple dot pattern
                p.drawPoint(self.SIZE - 4 - c * 3, self.SIZE - 4 - r * 3)


# ─────────────────────────────────────────────────────────────────────────────
# Shared overlay helpers
# ─────────────────────────────────────────────────────────────────────────────
def _draw_lock_icon(p: QPainter, cx: int, cy: int, locked: bool):
    """Draw a small padlock icon centred at (cx, cy).

    locked=True  → closed orange lock  (pass-through ON / inventory locked)
    locked=False → open   green  lock  (interactive / inventory unlocked)
    """
    bw, bh = 8, 6          # body dimensions
    bx = cx - bw // 2
    by = cy + 1            # body top-left y

    if locked:
        body_c    = QColor(210, 95, 25, 230)
        shackle_c = QColor(240, 155, 55, 230)
    else:
        body_c    = QColor(40, 175, 75, 190)
        shackle_c = QColor(70, 215, 105, 190)

    p.save()
    p.setRenderHint(QPainter.Antialiasing)

    # Body
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(body_c))
    p.drawRoundedRect(bx, by, bw, bh, 2, 2)

    # Shackle
    sw = 4                 # shackle inner width / arc diameter
    sx = cx - sw // 2      # shackle left x
    arc_top = by - sw      # arc bounding-rect top

    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(shackle_c, 1.5, Qt.SolidLine, Qt.RoundCap))

    if locked:
        # Closed: full semicircle + both legs
        p.drawArc(sx, arc_top, sw, sw, 0, 180 * 16)
        p.drawLine(sx,      by - sw // 2, sx,      by)
        p.drawLine(sx + sw, by - sw // 2, sx + sw, by)
    else:
        # Open: quarter-arc (right side only) + right leg in body + left leg raised
        p.drawArc(sx, arc_top, sw, sw, 0, 90 * 16)
        p.drawLine(sx + sw, by - sw // 2, sx + sw, by)
        p.drawLine(sx, arc_top - 2,       sx, by - sw // 2)

    p.restore()


class LockButton(QWidget):
    """Small always-on-top window that draws the lock icon and stays clickable
    even when its parent overlay has pass-through enabled.

    Positioned over the parent overlay's header via sync().
    """
    SIZE = 24

    clicked = pyqtSignal()

    def __init__(self, parent_overlay, state_getter):
        super().__init__()
        self._parent_overlay = parent_overlay
        self._state_getter   = state_getter  # callable → bool (locked?)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        if sys.platform == 'darwin':
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        _draw_lock_icon(p, self.SIZE // 2, self.SIZE // 2, self._state_getter())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def sync(self):
        """Reposition over the parent overlay's lock area and repaint. 
        Call raise only when already visible, to avoid stealing focus on show."""
        po = self._parent_overlay
        if not po.isVisible():
            self.hide()
            return
        icon_cx = po.width() - 14
        icon_cy = HEADER_H // 2
        top_left = po.mapToGlobal(QPoint(icon_cx - self.SIZE // 2,
                                         icon_cy - self.SIZE // 2))
        self.move(top_left)
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()


# ─────────────────────────────────────────────────────────────────────────────
# Map Overlay
# ─────────────────────────────────────────────────────────────────────────────
class MapOverlay(DragMixin, QWidget):
    """
    Semi-transparent, always-on-top, frameless window.
    Draws player marker, survey dots, and route lines via QPainter.
    """

    canvas_clicked = pyqtSignal(float, float)   # x, y in canvas coords

    def __init__(self, state: SurveyState, app: 'SurveyApp'):
        super().__init__()
        self.state = state
        self.app   = app
        self._drag_init()
        self._bg_alpha      = 0.18
        self._click_through = False
        self._show_labels   = 1   # 0=Off 1=Name 2=Slot# 3=Both

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        if sys.platform == 'darwin':   # Qt.Tool → NSPanel which auto-hides on deactivation
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(200, 120)
        self.resize(460, 460)

        # Resize grip
        self._grip = ResizeGrip(self)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)

        # Floating lock button — stays clickable even when pass-through is on
        self.lock_btn = LockButton(self, lambda: not self._click_through)
        self.lock_btn.clicked.connect(lambda: self.app.toggle_map_click_through())

    # ── geometry helpers ─────────────────────────────────────────────────────
    @property
    def canvas_h(self):
        return self.height() - HEADER_H

    def canvas_rect(self):
        return (0, HEADER_H, self.width(), self.canvas_h)

    def canvas_coords(self, gx, gy):
        """Convert global mouse pos → canvas (x, y)."""
        local = self.mapFromGlobal(QPoint(int(gx), int(gy)))
        return float(local.x()), float(local.y() - HEADER_H)

    # ── painting ─────────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # ── header ──
        p.fillRect(0, 0, w, HEADER_H, QColor(0, 0, 0, 185))

        # title
        p.setPen(QColor(180, 200, 220))
        p.setFont(QFont('Segoe UI', 9, QFont.Bold))
        p.drawText(8, 0, w - 30, HEADER_H, Qt.AlignVCenter, 'Survey Map')

        # lock icon is drawn by the floating LockButton window (see self.lock_btn)

        # ── canvas background ──
        cy = HEADER_H
        p.fillRect(0, cy, w, h - cy, QColor(10, 10, 20, int(self._bg_alpha * 255)))

        # border
        p.setPen(QPen(QColor(100, 170, 255, 180), 1.5))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 5, 5)

        # ── route lines ──
        if (self.app._route_lines_visible
                and self.state.phase == 'routing' and len(self.state.route_order) >= 1):
            pen = QPen(QColor(255, 210, 50, int(self.app._route_alpha * 255)), 2.5, Qt.DashLine)
            pen.setDashPattern([6, 3])
            p.setPen(pen)
            pts = []
            if self.state.player_pos:
                pts.append(self.state.player_pos)
            for iid in self.state.route_order:
                item = next((i for i in self.state.items if i['id'] == iid), None)
                if item and item['pixel_pos'] and not item['collected']:
                    pts.append(item['pixel_pos'])
            for i in range(len(pts) - 1):
                x1, y1 = pts[i];     x2, y2 = pts[i + 1]
                p.drawLine(int(x1), int(y1 + cy), int(x2), int(y2 + cy))

        # ── player marker (drawn first so survey dots appear on top) ──
        if self.state.player_pos:
            px, py_ = self.state.player_pos
            py_s = py_ + cy
            p.setBrush(QBrush(QColor(0, 230, 118, 220)))
            p.setPen(QPen(QColor(255, 255, 255, 220), 2))
            p.drawEllipse(int(px) - 5, int(py_s) - 5, 10, 10)

        # ── survey dots ──
        p.setFont(QFont('Segoe UI', 8))
        for item in self.state.items:
            if item['pixel_pos'] is None:
                if item is self.state.pending_calib:
                    # pulse / show at player pos
                    pp = self.state.player_pos
                    if pp:
                        self._draw_dot(p, pp[0], pp[1] + cy, 'pending', item)
                continue

            dx, dy = item['pixel_pos']
            dy_screen = dy + cy

            if item['collected']:
                kind = 'collected'
            elif item.get('skipped'):
                continue  # hidden from map; still tracked in inventory
            elif item['id'] == self.state.active_id:
                kind = 'active'
            else:
                kind = 'placed'

            self._draw_dot(p, dx, dy_screen, kind, item)

        # ── cursor hint ──
        if self.state.phase in ('set_player', 'calibrating'):
            p.setPen(QColor(255, 200, 60, 180))
            p.setFont(QFont('Segoe UI', 8))
            hint = 'Click to set your position' if self.state.phase == 'set_player' \
                   else 'Click to place survey dot (calibrate scale)'
            p.drawText(4, h - 6, hint)

        # ── motherlode overlay ──
        if self.state.ml_mode:
            self._draw_ml_overlay(p, cy)

    def _draw_dot(self, p, dx, dy, kind, item):
        DOT_R = 4
        colours = {
            'pending':   (QColor(255, 193,  7, 230), QColor(255, 255, 255, 200)),
            'placed':    (QColor( 79, 195, 247, 210), QColor(255, 255, 255, 160)),
            'active':    (QColor(255,  82,  82, 230), QColor(255, 255, 255, 200)),
            'collected': (QColor(120, 120, 120,  70), QColor(120, 120, 120,  80)),
        }
        fill, stroke = colours.get(kind, colours['placed'])
        p.setBrush(QBrush(fill))
        p.setPen(QPen(stroke, 2))
        p.drawEllipse(int(dx) - DOT_R, int(dy) - DOT_R, DOT_R * 2, DOT_R * 2)

        if kind == 'collected':
            return

        if not self._show_labels:
            return

        # label — 1=Name, 2=Slot#, 3=Both
        if self._show_labels == 2:
            label = str(item['grid_index'] + 1)
        elif self._show_labels == 3:
            label = f"{item['grid_index'] + 1}. {clean_name(item['name'])}"
        else:
            label_parts = [clean_name(item['name'])]
            if item['route_order'] >= 0 and self.state.phase == 'routing':
                label_parts.insert(0, f"{item['route_order'] + 1}.")
            label = ' '.join(label_parts)
        p.setPen(QColor(220, 220, 220, 190))
        p.setFont(QFont('Segoe UI', 8))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(label)
        bx = int(dx) - tw // 2
        by = int(dy) + DOT_R + 1
        p.fillRect(bx - 2, by, tw + 4, fm.height(), QColor(0, 0, 0, 110))
        p.drawText(bx, by + fm.ascent(), label)

    _ML_HUES = [0, 30, 60, 120, 180, 210, 270, 300]

    def _draw_ml_overlay(self, p, cy):
        state = self.state
        w, h  = self.width(), self.height()

        pos_colors = [QColor(220, 60, 60, 220),
                      QColor(60, 100, 220, 220),
                      QColor(60, 200, 80, 220)]
        pos_labels = ['P1', 'P2', 'P3']
        # ── Always draw position markers (visible even before scale is known) ──
        for i, pos in enumerate(state.ml_positions):
            px_, py_ = pos
            py_s = py_ + cy
            col = pos_colors[i]
            p.setBrush(QBrush(col))
            p.setPen(QPen(QColor(255, 255, 255, 180), 1.5))
            p.drawRect(int(px_) - 5, int(py_s) - 5, 10, 10)
            p.setPen(col)
            p.setFont(QFont('Segoe UI', 7, QFont.Bold))
            p.drawText(int(px_) + 8, int(py_s) + 4, pos_labels[i])

        # ── Cursor hint ──
        if state.ml_phase == 'set_pos' and state.ml_round < 3:
            p.setPen(QColor(255, 200, 60, 180))
            p.setFont(QFont('Segoe UI', 8))
            p.drawText(4, h - 6, f'Click map to set Position {state.ml_round + 1}')

        # ── Estimated positions require scale ──
        if not state.scale:
            p.setPen(QColor(255, 140, 0, 200))
            p.setFont(QFont('Segoe UI', 8))
            p.drawText(4, h - (22 if state.ml_phase == 'set_pos' and state.ml_round < 3 else 6),
                       'Awaiting scale… (auto-computed after round 3)')
            return

        active_id = state.ml_active_id

        # ── Route lines (dashed yellow, last position → estimated targets in order) ──
        if self.app._route_lines_visible and state.ml_route_order and state.ml_positions:
            pen = QPen(QColor(255, 210, 50, int(self.app._route_alpha * 255)), 2.5, Qt.DashLine)
            pen.setDashPattern([6, 3])
            p.setPen(pen)
            pts = []
            start = state.player_pos or (state.ml_positions[-1] if state.ml_positions else None)
            if start:
                pts.append((start[0], start[1] + cy))
            for eid in state.ml_route_order:
                entry = next((e for e in state.ml_surveys if e['id'] == eid), None)
                if entry and entry.get('estimated_pos') and not entry['collected']:
                    ex_, ey_ = entry['estimated_pos']
                    pts.append((ex_, ey_ + cy))
            for i in range(len(pts) - 1):
                x1, y1 = pts[i];  x2, y2 = pts[i + 1]
                p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # ── Estimated position dots ──
        p.setFont(QFont('Segoe UI', 8))
        for e_idx, entry in enumerate(state.ml_surveys):
            ep = entry.get('estimated_pos')
            if not ep:
                continue
            ex_, ey_ = ep
            ey_s = ey_ + cy
            is_active = (entry['id'] == active_id)

            if entry['collected']:
                dot_col  = QColor(120, 120, 120, 70)
                dot_pen  = QColor(120, 120, 120, 80)
                dot_r    = 5
            elif is_active:
                dot_col  = QColor(255, 82, 82, 230)
                dot_pen  = QColor(255, 255, 255, 200)
                dot_r    = 7
            else:
                hue      = self._ML_HUES[e_idx % len(self._ML_HUES)]
                dot_col  = QColor.fromHsv(hue, 220, 240, 210)
                dot_pen  = QColor(255, 255, 255, 180)
                dot_r    = 6

            p.setBrush(QBrush(dot_col))
            p.setPen(QPen(dot_pen, 1.5))
            p.drawEllipse(int(ex_) - dot_r, int(ey_s) - dot_r,
                          dot_r * 2, dot_r * 2)

            if self._show_labels > 0 and not entry['collected']:
                order_str = (f'{entry["route_order"] + 1}. ' if entry.get('route_order', -1) >= 0 else '')
                label = f'{order_str}Treasure {entry["id"]}'
                p.setPen(QColor(220, 220, 180, 200))
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label)
                p.fillRect(int(ex_) - tw // 2 - 2, int(ey_s) + dot_r + 1,
                           tw + 4, fm.height(), QColor(0, 0, 0, 130))
                p.drawText(int(ex_) - tw // 2, int(ey_s) + dot_r + 1 + fm.ascent(), label)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.hide()

    # ── mouse events ─────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        self._drag_press(event)

    def mouseMoveEvent(self, event):
        self._drag_move(event)

    def mouseReleaseEvent(self, event):
        self._drag_release(event)
        if event.y() >= HEADER_H and event.button() == Qt.LeftButton and not self._dragging:
            # Canvas click
            cx = float(event.x())
            cy = float(event.y() - HEADER_H)
            if cy >= 0:
                self.canvas_clicked.emit(cx, cy)

    def _on_drag_finished(self):
        self.app.save_settings()

    # ── click-through toggle ─────────────────────────────────────────────────
    def set_click_through(self, enabled: bool):
        self._click_through = enabled
        _set_click_through(int(self.winId()), enabled)

    def refresh(self):
        # NOTE: do NOT call lock_btn.sync() here — refresh() is driven by the
        # 600 ms _blink_timer, and a sync() on every tick used to trigger
        # raise_() which steals focus from the game on X11. The lock button
        # is re-synced only on real events (show/move/resize) below.
        self.update()
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()


# ─────────────────────────────────────────────────────────────────────────────
# Inventory slot widget
# ─────────────────────────────────────────────────────────────────────────────
class MlSlotWidget(QFrame):
    """Slot widget for a single motherlode entry in the inventory overlay."""

    def __init__(self, entry=None, pending_dist=None, slot_num=1, is_active=False, parent=None):
        super().__init__(parent)
        self._entry       = entry        # committed survey dict or None
        self._pending     = pending_dist  # float distance from current round, or None
        self._slot_num    = slot_num
        self._is_active   = is_active
        self.setFrameShape(QFrame.NoFrame)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        entry     = self._entry
        collected = entry['collected'] if entry else False
        n_committed = len(entry['distances']) if entry else 0
        has_pending = self._pending is not None
        is_new      = (entry is None and has_pending)  # first-round, not yet committed

        # Background + border
        if collected:
            p.fillRect(0, 0, w, h, QColor(20, 20, 20, 180))
            p.setPen(QPen(QColor(100, 100, 100, 100), 1))
        elif self._is_active:
            p.fillRect(0, 0, w, h, QColor(60, 45, 5, 200))
            p.setPen(QPen(QColor(255, 193, 7, 200), 2))    # gold = active route target
        elif is_new:
            p.fillRect(0, 0, w, h, QColor(35, 25, 12, 200))
            p.setPen(QPen(QColor(255, 200, 60, 160), 1))   # gold = uncommitted
        else:
            p.fillRect(0, 0, w, h, QColor(35, 25, 12, 200))
            p.setPen(QPen(QColor(100, 170, 255, 150), 1))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 2, 2)

        # "T{N}" label
        text_col = QColor(80, 80, 80) if collected else QColor(220, 200, 160)
        p.setPen(text_col)
        p.setFont(QFont('Segoe UI', 7, QFont.Bold))
        p.drawText(2, 2, w - 4, h // 2, Qt.AlignTop | Qt.AlignHCenter,
                   f'T{self._slot_num}')

        # Most recent distance
        all_dists = (entry['distances'] if entry else []) + \
                    ([self._pending] if has_pending else [])
        if all_dists:
            p.setPen(QColor(80, 80, 80) if collected else QColor(150, 210, 160))
            p.setFont(QFont('Segoe UI', 6))
            p.drawText(2, h // 2 - 2, w - 4, h // 2,
                       Qt.AlignTop | Qt.AlignHCenter, f'{all_dists[-1]:.0f}m')

        # Round indicator dots (3 dots along bottom)
        dot_r   = 2
        dot_y   = h - 6
        spacing = (w - 8) / 3
        for i in range(3):
            dot_x = int(4 + spacing * i + spacing / 2)
            if i < n_committed:
                col = QColor(100, 200, 100, 80 if collected else 200)
            elif i == n_committed and has_pending:
                col = QColor(255, 200, 60, 200)  # gold = in-progress
            else:
                col = QColor(60, 60, 60, 180)
            p.setBrush(QBrush(col))
            p.setPen(Qt.NoPen)
            p.drawEllipse(dot_x - dot_r, dot_y - dot_r, dot_r * 2, dot_r * 2)

        # Top-right: route order number if assigned, else ✓ for estimated
        if not collected:
            ro = entry.get('route_order', -1) if entry else -1
            if ro >= 0:
                p.setPen(QColor(255, 193, 7, 220))
                p.setFont(QFont('Segoe UI', 7, QFont.Bold))
                p.drawText(0, 2, w - 3, 14, Qt.AlignRight, str(ro + 1))
            elif entry and entry.get('estimated_pos'):
                p.setPen(QColor(100, 220, 100, 200))
                p.setFont(QFont('Segoe UI', 7, QFont.Bold))
                p.drawText(0, 2, w - 3, 14, Qt.AlignRight, '\u2713')


class SlotWidget(QFrame):
    clicked = pyqtSignal(object)   # emits item dict

    def __init__(self, item=None, parent=None):
        super().__init__(parent)
        self.item = item
        self.setFrameShape(QFrame.NoFrame)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        if not self.item:
            # empty slot
            p.fillRect(0, 0, w, h, QColor(35, 25, 12, 180))
            p.setPen(QPen(QColor(100, 170, 255, 100), 1))
            p.drawRoundedRect(0, 0, w - 1, h - 1, 2, 2)
            return

        item = self.item
        skipped = item.get('skipped', False)

        # background
        if skipped:
            p.fillRect(0, 0, w, h, QColor(20, 20, 20, 180))
            p.setPen(QPen(QColor(100, 100, 100, 100), 1))
        elif self.property('active_route'):
            p.fillRect(0, 0, w, h, QColor(60, 45, 5, 200))
            p.setPen(QPen(QColor(255, 193, 7, 200), 2))
        else:
            p.fillRect(0, 0, w, h, QColor(35, 25, 12, 200))
            p.setPen(QPen(QColor(100, 170, 255, 150), 1))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 2, 2)

        # item name
        name = clean_name(item['name'])
        p.setPen(QColor(120, 110, 90) if skipped else QColor(220, 200, 160))
        p.setFont(QFont('Segoe UI', 7))
        p.drawText(2, 2, w - 4, h - 14, Qt.AlignTop | Qt.AlignHCenter | Qt.TextWordWrap, name)

        # slot number (bottom-left)
        p.setPen(QColor(80, 80, 80, 180) if skipped else QColor(120, 120, 120, 180))
        p.setFont(QFont('Segoe UI', 7))
        p.drawText(3, h - 11, str(item['grid_index'] + 1))

        # skipped marker (top-right) or route order
        if skipped:
            p.setPen(QColor(160, 80, 80, 200))
            p.setFont(QFont('Segoe UI', 7, QFont.Bold))
            p.drawText(0, 2, w - 3, 14, Qt.AlignRight, '–')
        elif item['route_order'] >= 0:
            p.setPen(QColor(255, 193, 7, 220))
            p.setFont(QFont('Segoe UI', 7, QFont.Bold))
            ro_text = str(item['route_order'] + 1)
            p.drawText(0, 2, w - 3, 14, Qt.AlignRight, ro_text)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.item:
            self.clicked.emit(self.item)


class DummySlot(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # background
        p.fillRect(0, 0, w, h, QColor(0, 0, 0, 200))
        p.setPen(QPen(QColor(100, 170, 255, 150), 1))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 2, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Inventory Overlay
# ─────────────────────────────────────────────────────────────────────────────
class InventoryOverlay(DragMixin, QWidget):
    def __init__(self, state: SurveyState, app: 'SurveyApp'):
        super().__init__()
        self.state = state
        self.app   = app
        self._drag_init()
        self._bg_alpha = 0.35
        self._slots    = []

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        if sys.platform == 'darwin':   # Qt.Tool → NSPanel which auto-hides on deactivation
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(SLOT_SIZE * 3 + SLOT_GAP * 2 + 12,
                            SLOT_SIZE + HEADER_H + 12)

        grid_w = GRID_COLS * SLOT_SIZE + (GRID_COLS - 1) * SLOT_GAP + 12
        grid_h = 4 * SLOT_SIZE + 3 * SLOT_GAP + 12
        self.resize(grid_w, HEADER_H + grid_h)

        self._build_ui()

        # Resize grip (must be added after _build_ui so it renders on top)
        self._grip = ResizeGrip(self)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)
        self._grip.raise_()

        # Floating lock button — stays clickable even when inv is locked (pass-through)
        self.lock_btn = LockButton(self, lambda: self.app._inv_locked)
        self.lock_btn.clicked.connect(lambda: self.app.toggle_inv_lock())

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header spacer (painted by paintEvent)
        outer.addSpacing(HEADER_H)

        # Grid container
        self._grid_container = QWidget(self)
        self._grid_container.setObjectName('grid_container')
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setContentsMargins(6, 6, 6, 6)
        self._grid_layout.setSpacing(SLOT_GAP)
        outer.addWidget(self._grid_container, 0, Qt.AlignTop | Qt.AlignLeft)

        self._rebuild_grid()

    def _rebuild_grid(self):
        # Clear old slots
        while self._grid_layout.count():
            w = self._grid_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._slots.clear()

        if self.state.ml_mode:
            self._rebuild_ml_grid()
            return

        uncollected = self.state.uncollected()
        active_id   = self.state.active_id

        sc    = self.state.survey_count
        total = max(sc, len(uncollected)) if sc > 0 else len(uncollected)
        if total == 0:
            total = GRID_COLS - self.app._offset_slots  # show a placeholder row before any items are found

        # Compute slot width so 10 columns fill the full overlay width evenly
        slot_w = max(28, (self.width() - 12 - SLOT_GAP * (GRID_COLS - 1)) // GRID_COLS)

        for d in range(self.app._offset_slots):
            slot = DummySlot(self._grid_container)
            slot.setFixedSize(slot_w, slot_w)
            row, col = divmod(d, GRID_COLS)
            self._grid_layout.addWidget(slot, row, col)
            self._slots.append(slot)

        for i in range(total):
            item = uncollected[i] if i < len(uncollected) else None
            slot = SlotWidget(item, self._grid_container)
            if item and item['id'] == active_id:
                slot.setProperty('active_route', True)
            slot.clicked.connect(self.app.on_inventory_click)
            slot.setFixedSize(slot_w, slot_w)
            row, col = divmod(i + self.app._offset_slots, GRID_COLS)
            self._grid_layout.addWidget(slot, row, col)
            self._slots.append(slot)

    def _rebuild_ml_grid(self):
        """Render slot-per-motherlode grid, including current round's pending distances."""
        state       = self.state
        surveys     = state.ml_surveys
        pending     = state.ml_pending
        n_committed = len(surveys)
        n_pending   = len(pending)
        n_total     = max(n_committed, n_pending)

        if n_total == 0:
            lbl = QLabel('Scan motherlodes to see them here.')
            lbl.setStyleSheet('color:#556; font-size:11px; padding:4px;')
            self._grid_layout.addWidget(lbl, 0, 0)
            return

        slot_w = max(28, (self.width() - 12 - SLOT_GAP * (GRID_COLS - 1)) // GRID_COLS)

        # After trilateration, hide collected entries like regular survey does
        if state.ml_round >= 3:
            surveys = [e for e in surveys if not e.get('collected')]
            n_committed = len(surveys)
            n_total = max(n_committed, n_pending)

        active_id = self.state.ml_active_id
        for i in range(n_total):
            entry        = surveys[i]   if i < n_committed else None
            pending_dist = pending[i]   if i < n_pending   else None
            is_active    = entry is not None and entry['id'] == active_id
            slot_num     = entry['id'] if entry else i + 1
            slot = MlSlotWidget(entry, pending_dist, slot_num, is_active, self._grid_container)
            slot.setFixedSize(slot_w, slot_w)
            row, col = divmod(i, GRID_COLS)
            self._grid_layout.addWidget(slot, row, col)
            self._slots.append(slot)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # canvas bg
        p.fillRect(0, HEADER_H, w, h - HEADER_H, QColor(10, 10, 20, int(self._bg_alpha * 255)))

        # header
        p.fillRect(0, 0, w, HEADER_H, QColor(0, 0, 0, 185))
        p.setPen(QColor(180, 200, 220))
        p.setFont(QFont('Segoe UI', 9, QFont.Bold))
        title = 'Motherlode Survey' if self.state.ml_mode else 'Survey Inventory'
        p.drawText(8, 0, w - 30, HEADER_H, Qt.AlignVCenter, title)

        # lock icon is drawn by the floating LockButton window (see self.lock_btn)

        # border
        p.setPen(QPen(QColor(100, 170, 255, 180), 1.5))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 5, 5)

    def mousePressEvent(self, event):
        self._drag_press(event)

    def mouseMoveEvent(self, event):
        self._drag_move(event)

    def mouseReleaseEvent(self, event):
        self._drag_release(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)
        if hasattr(self, '_grid_layout'):
            self._rebuild_grid()
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, 'lock_btn'):
            self.lock_btn.hide()

    def _on_drag_finished(self):
        self.app.save_settings()

    def refresh(self):
        # NOTE: do NOT call lock_btn.sync() here — refresh() runs on every
        # state change; sync() used to trigger raise_() on each call, which
        # steals focus from the game on X11. The lock button is re-synced on
        # real events only (show/move/resize).
        self._rebuild_grid()
        self.update()
        if hasattr(self, 'lock_btn'):
            self.lock_btn.sync()


# ─────────────────────────────────────────────────────────────────────────────
# Summary Window
# ─────────────────────────────────────────────────────────────────────────────
class SummaryWindow(QDialog):
    """Post-session summary: stats, XP gains, and items-found table."""

    _BASE_STYLE = (
        'QDialog, QWidget { background:#0e0e1e; color:#cde; }'
        'QLabel  { color:#cde; }'
        'QPushButton { background:#1a3a6a; color:#cde; border:1px solid #446; '
        '  padding:5px 14px; border-radius:4px; font-size:12px; font-weight:600; }'
        'QPushButton:hover { background:#2a4a8a; }'
        'QTableWidget { background:#0a0a18; color:#cde; '
        '  border:1px solid #334; gridline-color:#1e1e30; }'
        'QHeaderView::section { background:#12122a; color:#8ab; '
        '  border:1px solid #334; padding:4px 6px; font-weight:700; font-size:11px; }'
        'QTableWidget::item { padding:3px 6px; }'
        'QScrollBar:vertical { background:#0a0a18; width:10px; }'
        'QScrollBar::handle:vertical { background:#334; border-radius:4px; }'
    )

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Survey Session Summary')
        self.setMinimumWidth(500)
        self.setStyleSheet(self._BASE_STYLE)
        self._build_ui(data)

    def _section_label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet('font-size:12px; font-weight:700; color:#8ab; '
                         'padding-top:4px; padding-bottom:2px;')
        return lb

    def _stat_row(self, grid: QGridLayout, row: int, label: str, value: str,
                  value_color: str = '#cde'):
        lbl = QLabel(label + ':')
        lbl.setStyleSheet('color:#778; font-size:11px;')
        val = QLabel(value)
        val.setStyleSheet(f'color:{value_color}; font-size:12px; font-weight:600;')
        grid.addWidget(lbl, row, 0)
        grid.addWidget(val, row, 1)

    def _build_ui(self, data: dict):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title = QLabel('Survey Session Summary')
        title.setStyleSheet('font-size:15px; font-weight:700; color:#9bc;')
        root.addWidget(title)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color:#334;'); root.addWidget(sep)

        # ── Core stats ────────────────────────────────────────────────────────
        stats_grid = QGridLayout()
        stats_grid.setColumnStretch(1, 1)
        stats_grid.setHorizontalSpacing(16)
        stats_grid.setVerticalSpacing(4)
        self._stat_row(stats_grid, 0, 'Maps Completed', str(data['maps_completed']), '#9fc')
        self._stat_row(stats_grid, 1, 'Start Time',     data['start_str'])
        self._stat_row(stats_grid, 2, 'End Time',       data['end_str'])
        self._stat_row(stats_grid, 3, 'Duration',       data['duration_str'],    '#fca')
        self._stat_row(stats_grid, 4, 'Avg Survey Time',data['avg_time_str'],    '#fca')
        root.addLayout(stats_grid)

        # ── XP Gained (Surveying / Mining / Geology only) ────────────────────
        xp = data.get('xp', {})
        if xp:
            sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine)
            sep2.setStyleSheet('color:#334;'); root.addWidget(sep2)
            root.addWidget(self._section_label('XP Gained'))
            xp_grid = QGridLayout()
            xp_grid.setColumnStretch(1, 1)
            xp_grid.setHorizontalSpacing(16)
            xp_grid.setVerticalSpacing(3)
            for r, skill in enumerate(['Surveying', 'Mining', 'Geology']):
                if skill in xp:
                    self._stat_row(xp_grid, r, skill, f'{xp[skill]:,}', '#9fc')
            root.addLayout(xp_grid)

        # ── Items Found table ─────────────────────────────────────────────────
        sep3 = QFrame(); sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet('color:#334;'); root.addWidget(sep3)
        root.addWidget(self._section_label('Items Found'))

        items = data.get('items', [])
        table = QTableWidget(len(items), 3)
        table.setHorizontalHeaderLabels(['Item', 'Count', '% of Total'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        table.setColumnWidth(2, 160)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(
            self._BASE_STYLE +
            'QTableWidget { alternate-background-color:#0d0d22; }'
        )

        max_count = max((c for _, c, _ in items), default=1)
        for row, (name, count, pct) in enumerate(items):
            table.setItem(row, 0, QTableWidgetItem(name))
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 1, count_item)

            bar = QProgressBar()
            bar.setRange(0, max(max_count, 1))
            bar.setValue(count)
            bar.setFormat(f'{pct:.1f}%')
            bar.setTextVisible(True)
            bar.setStyleSheet(
                'QProgressBar { background:#12122a; border:1px solid #334; '
                '  border-radius:3px; text-align:center; color:#cde; font-size:10px; }'
                'QProgressBar::chunk { background:#1a4a2a; border-radius:2px; }'
            )
            table.setCellWidget(row, 2, bar)

        table.resizeRowsToContents()
        max_visible = min(len(items), 12)
        if max_visible > 0:
            row_h = table.rowHeight(0)
            header_h = table.horizontalHeader().height()
            table.setMaximumHeight(header_h + row_h * max_visible + 4)
        root.addWidget(table)

        # ── Close button ──────────────────────────────────────────────────────
        sep4 = QFrame(); sep4.setFrameShape(QFrame.HLine)
        sep4.setStyleSheet('color:#334;'); root.addWidget(sep4)
        btn_close = QPushButton('Close')
        btn_close.setFixedWidth(90)
        btn_close.clicked.connect(self.accept)
        root.addWidget(btn_close, 0, Qt.AlignRight)


# ─────────────────────────────────────────────────────────────────────────────
# Hotkey capture dialog
# ─────────────────────────────────────────────────────────────────────────────
class HotkeyCaptureDialog(QDialog):
    """Modal dialog that captures a single keypress (+ modifiers) as a hotkey."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.result_qt_key  = None   # int — Qt.Key_* value
        self.result_qt_mods = 0      # int — Qt.KeyboardModifiers flags
        self.result_mods    = []     # list[str] — ['ctrl','shift','alt']
        self.result_label   = ''
        self.setWindowTitle('Set Hotkey')
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet('color:#cde; font-size:11px; padding:12px;')
        layout = QVBoxLayout(self)
        layout.addWidget(lbl)
        self.setStyleSheet('background:#1a1a2e;')
        self.resize(340, 120)

    def keyPressEvent(self, event):
        qt_key = event.key()
        if qt_key == Qt.Key_Escape:
            self.reject()
            return
        if qt_key in _get_qt_modifier_keys():
            return   # ignore bare modifier presses
        mods = []
        if event.modifiers() & Qt.ControlModifier:
            mods.append('ctrl')
        if event.modifiers() & Qt.ShiftModifier:
            mods.append('shift')
        if event.modifiers() & Qt.AltModifier:
            mods.append('alt')
        is_numpad = bool(event.modifiers() & Qt.KeypadModifier)
        # Build label
        key_name = _get_qt_key_label_map().get(qt_key)
        if key_name is None:
            if Qt.Key_0 <= qt_key <= Qt.Key_9:
                key_name = f'Num{chr(qt_key)}' if is_numpad else chr(qt_key)
            elif Qt.Key_A <= qt_key <= Qt.Key_Z:
                key_name = chr(qt_key)
            else:
                key_name = f'Key{qt_key:#06x}'
        elif is_numpad and Qt.Key_0 <= qt_key <= Qt.Key_9:
            key_name = f'Num{chr(qt_key)}'
        mod_labels = [m.capitalize() for m in mods]
        self.result_qt_key  = qt_key
        self.result_qt_mods = int(event.modifiers())
        self.result_mods    = mods
        self.result_label   = '+'.join(mod_labels + [key_name])
        self.accept()


# ─────────────────────────────────────────────────────────────────────────────
# Control Panel
# ─────────────────────────────────────────────────────────────────────────────
class ControlPanel(QWidget):
    def __init__(self, app: 'SurveyApp'):
        super().__init__()
        self.app = app
        self.setWindowTitle(f'Gorgon Survey Tracker v{APP_VERSION}')
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(480)
        self._move_save_timer = QTimer(self)
        self._move_save_timer.setSingleShot(True)
        self._move_save_timer.timeout.connect(self.app.save_settings)
        self._build_ui()
        self.refresh()

    def moveEvent(self, event):
        super().moveEvent(event)
        # Debounce: save position 400 ms after the user stops dragging.
        self._move_save_timer.start(400)

    def _btn(self, text, callback, color='#1a3a6a'):
        b = QPushButton(text)
        b.clicked.connect(callback)
        b.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:5px 10px; border-radius:4px; font-size:12px; font-weight:600; }}'
            f'QPushButton:hover {{ background: #2a4a8a; }}'
            f'QPushButton:disabled {{ background:#222; color:#555; }}'
        )
        return b

    def _small_btn(self, text, callback, color='#1a1a2e'):
        b = QPushButton(text)
        b.clicked.connect(callback)
        b.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ background: #2a3a5a; }}'
        )
        return b

    def _hotkey_btn(self, text, callback, alt_callback, color='#1a1a2e'):
        b = HotkeyButton(text)
        b.clicked.connect(callback)
        b.signal_emitter.right_clicked.connect(alt_callback)
        b.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ background: #2a3a5a; }}'
        )
        b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return b

    def _label(self, text, color='#778'):
        lb = QLabel(text)
        lb.setStyleSheet(f'color:{color}; font-size:11px;')
        return lb

    def _btn_style(self, bg: str) -> str:
        return (
            f'QPushButton {{ background:{bg}; color:#cde; border:1px solid #446; '
            f'padding:5px 10px; border-radius:4px; font-size:12px; font-weight:600; }}'
            f'QPushButton:hover {{ background: #2a4a8a; }}'
            f'QPushButton:disabled {{ background:#222; color:#555; }}'
        )

    def _small_btn_style(self, bg: str) -> str:
        return (
            f'QPushButton {{ background:{bg}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ background: #2a3a5a; }}'
            f'QPushButton:disabled {{ background:#222; color:#555; }}'
        )

    def refresh_update_button(self):
        """Show the clickable 'New Version' text only when a new (non-skipped) version has been detected."""
        latest  = getattr(self.app, '_latest_version', None)
        skipped = getattr(self.app, '_skip_update_version', None)
        print(f'Latest version: {latest}, skipped version: {skipped}')
        new_available = bool(
            latest
            and _parse_version(latest) > _parse_version(APP_VERSION)
            and latest != skipped
        )
        if new_available:
            self.btn_update.setText(f'🔔 New Version v{latest}')
            self.btn_update.setToolTip(f'Click to update to v{latest}.')
        self.btn_update.setVisible(new_available)

    def _build_ui(self):
        self.setStyleSheet('QWidget { background:#0e0e1e; color:#cde; }')
        main = QVBoxLayout(self)
        main.setContentsMargins(12, 10, 12, 10)
        main.setSpacing(7)

        # Title row
        row_title = QHBoxLayout()
        title = QLabel('🗺  Gorgon Survey Tracker')
        title.setStyleSheet('font-size:14px; font-weight:700; color:#9bc;')
        row_title.addWidget(title)
        row_title.addStretch()
        self.btn_update = QPushButton('New Version')
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self.btn_update.setFlat(True)
        self.btn_update.setStyleSheet(
            'QPushButton { background:transparent; border:none; color:#f0a020; '
            'font-size:12px; font-weight:700; padding:0 4px; }'
            'QPushButton:hover { color:#ffc040; text-decoration:underline; }'
        )
        self.btn_update.clicked.connect(self.app._on_update_button_click)
        self.btn_update.setVisible(False)
        row_title.addWidget(self.btn_update)
        main.addLayout(row_title)

        # (Toggles — Labels / Route / Overlays are constructed later in the Toggles group)
        self.btn_labels       = self._small_btn('Labels: Name', self.app.toggle_map_labels, '#1a2a3a')
        self.btn_route_lines  = self._small_btn('Route: ON',    self.app.toggle_route_lines, '#1a2a3a')
        self.btn_overlays_map = self._small_btn('Map: ON', self.app.toggle_map_overlay,    '#1a3a1a')
        self.btn_overlays_inv = self._small_btn('Inv: ON', self.app.toggle_inv_overlay,    '#1a3a1a')
        self.btn_labels.setToolTip(
            'Cycles between display options for labelling the points on the '
            'map as you survey.'
        )
        self.btn_route_lines.setToolTip(
            'Toggles the route lines that guide you once a path has been set.'
        )
        self.btn_overlays_map.setToolTip(
            'Toggles visibility of the map overlay. Right-click to remove the binding.'
        )
        self.btn_overlays_inv.setToolTip(
            'Toggles visibility of the inventory overlay. Right-click to remove the binding.'
        )

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color:#334;')
        main.addWidget(sep)

        # ── Mode toggle ────────────────────────────────────────────────────
        row_mode = QHBoxLayout()
        self.btn_mode_regular = self._btn('Regular Survey',    self.app.exit_ml_mode,  '#1a3a6a')
        self.btn_mode_ml      = self._btn('Motherlode Survey', self.app.enter_ml_mode, '#4a1a4a')
        self.btn_hotkey       = self._hotkey_btn('Survey: Num0', self.app.set_hotkey_binding, self.app.remove_hotkey_binding, '#1a3a2a')
        self.btn_mapkey       = self._hotkey_btn('Map: M', self.app.set_mapkey_binding, self.app.remove_mapkey_binding, '#1a3a2a')
        self.btn_invkey       = self._hotkey_btn('Inv: I', self.app.set_invkey_binding, self.app.remove_invkey_binding, '#1a3a2a')
        _hotkey_tip = 'Click to set a new binding. Right-click to clear.'
        self.btn_hotkey.setToolTip(_hotkey_tip)
        self.btn_mapkey.setToolTip(_hotkey_tip)
        self.btn_invkey.setToolTip(_hotkey_tip)
        row_mode.addWidget(self.btn_mode_regular)
        row_mode.addWidget(self.btn_mode_ml)
        row_mode.addStretch()
        hotkey_buttons = QVBoxLayout()
        hotkey_buttons.setContentsMargins(5,0,5,0)
        hotkey_buttons.setSpacing(2)
        hotkey_buttons.addWidget(self.btn_hotkey)
        if _HOTKEY_SUPPORTED:
            hotkey_buttons.addWidget(self.btn_mapkey)
            hotkey_buttons.addWidget(self.btn_invkey)
        row_mode.addLayout(hotkey_buttons)
        main.addLayout(row_mode)

        # ── Files row ──────────────────────────────────────────────────────
        row = QHBoxLayout()
        row.addWidget(self._label('Files:'))
        self.btn_chat = self._btn('💬 ChatLogs folder', self.app.select_chat_dir)
        row.addWidget(self.btn_chat)
        self.lbl_file_status = self._label('No chat dir set', '#556')
        row.addWidget(self.lbl_file_status)
        row.addStretch()
        main.addLayout(row)

        # ── Collapsible section (hidden until ChatLogs folder is selected) ──
        self._survey_section = QWidget()
        sec = QVBoxLayout(self._survey_section)
        sec.setContentsMargins(0, 0, 0, 0)
        sec.setSpacing(7)

        # ── Status row ────────────────────────────────────────────────────
        row2 = QHBoxLayout()
        row2.addWidget(self._label('Status:'))
        self.lbl_phase = QLabel('Idle')
        self.lbl_phase.setStyleSheet(
            'background:#1a1a2a; color:#778; padding:2px 8px; '
            'border:1px solid #334; border-radius:4px; font-size:11px; font-weight:700;'
        )
        row2.addWidget(self.lbl_phase)
        self.lbl_scale = self._label('Scale: uncalibrated', '#556')
        row2.addWidget(self.lbl_scale)
        self.lbl_count = self._label('0 items', '#556')
        row2.addWidget(self.lbl_count)
        row2.addStretch()
        sec.addLayout(row2)

        # ── Survey controls (regular mode) ───────────────────────────────
        self._regular_controls = QWidget()
        self._regular_controls.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        vert_layout = QVBoxLayout(self._regular_controls)
        vert_layout.setContentsMargins(0, 0, 0, 0)
        vert_layout.setSpacing(3)
        rc_layout = QHBoxLayout()
        rc_layout.setContentsMargins(0, 0, 0, 0)
        rc_layout.addWidget(self._label('Surveys:'))
        self.sb_count = QSpinBox()
        self.sb_count.setRange(0, 999)
        self.sb_count.setValue(0)
        self.sb_count.setSpecialValueText('0')
        self.sb_count.setToolTip('How many survey maps you have (0 = auto grow with matches surveys)')
        self.sb_count.setMaximumWidth(60)
        self.sb_count.setStyleSheet(
            'QSpinBox { background:#1a1a2e; color:#cde; border:1px solid #446; '
            'padding:2px 4px; border-radius:4px; font-size:12px; }'
            'QSpinBox::up-button, QSpinBox::down-button { width:14px; }'
        )
        self.sb_count.valueChanged.connect(self.app.on_survey_count_changed)
        rc_layout.addWidget(self.sb_count)
        self.btn_set_pos = self._btn('📍 Set My Position', self.app.enter_set_player, '#1a4a2a')
        self.btn_start   = self._btn('▶ Start Survey',     self.app.start_surveying,  '#1a3a5a')
        self.btn_done    = self._btn('🗺 Optimize Route',   self.app.done_surveying,   '#5a4a00')
        self.btn_next    = self._btn('→ Skip to Next',      self.app.advance_route,    '#1a3a5a')
        self.btn_mark    = self._btn('✔ Mark Complete',     self.app.mark_complete,    '#2a4a1a')
        self.btn_reset   = self._btn('🗑 Reset',            self.app.reset_survey,     '#5a1a1a')
        self.btn_summary = self._btn('📊 View Summary',     self.app.show_summary,     '#1a3a4a')
        for b in (self.btn_set_pos, self.btn_start, self.btn_done, self.btn_next, self.btn_mark, self.btn_reset, self.btn_summary):
            rc_layout.addWidget(b)
        rc_layout.addStretch()
        vert_layout.addLayout(rc_layout)
        # ── Add selector for empty offset control ───────────
        oc_layout = QHBoxLayout()
        oc_layout.setContentsMargins(0, 0, 0, 0)
        oc_layout.addWidget(self._label('1st Row Offset:'))
        self.offset_count = QSpinBox()
        self.offset_count.setRange(0, GRID_COLS - 1)
        self.offset_count.setValue(0)
        self.offset_count.setSpecialValueText('0')
        self.offset_count.setToolTip('How many inventory slots to offset in the first row?')
        self.offset_count.setMaximumWidth(60)
        self.offset_count.setStyleSheet(
            'QSpinBox { background:#1a1a2e; color:#cde; border:1px solid #446; '
            'padding:2px 4px; border-radius:4px; font-size:12px; }'
            'QSpinBox::up-button, QSpinBox::down-button { width:14px; }'
        )
        self.offset_count.valueChanged.connect(self.app.on_offset_count_changed)
        oc_layout.addWidget(self.offset_count)
        oc_layout.addStretch()
        vert_layout.addLayout(oc_layout)
        sec.addWidget(self._regular_controls)

        # ── Motherlode controls (shown only in motherlode mode) ───────────
        self._ml_section = QWidget()
        ml_layout = QVBoxLayout(self._ml_section)
        ml_layout.setContentsMargins(0, 0, 0, 0)
        ml_layout.setSpacing(4)

        self.lbl_ml_status = self._label('Round 1: Click map to set Position 1', '#bc8')
        ml_layout.addWidget(self.lbl_ml_status)

        row_ml = QHBoxLayout()
        self.lbl_ml_count = self._label('0 distances this round', '#556')
        row_ml.addWidget(self.lbl_ml_count)
        self.btn_ml_next  = self._btn('Next Position', self.app.ml_next_position, '#2a2a5a')
        row_ml.addWidget(self.btn_ml_next)
        self.btn_ml_skip  = self._btn('Skip', self.app.ml_skip_next, '#2a3a2a')
        self.btn_ml_skip.setVisible(False)
        row_ml.addWidget(self.btn_ml_skip)
        self.btn_ml_reset = self._btn('Reset Motherlode', self.app.reset_ml, '#5a1a1a')
        row_ml.addWidget(self.btn_ml_reset)
        row_ml.addStretch()
        ml_layout.addLayout(row_ml)

        self.lbl_ml_scale = self._label('Scale: pending', '#556')
        self.lbl_ml_fit   = self._label('', '#f80')
        ml_layout.addWidget(self.lbl_ml_scale)
        ml_layout.addWidget(self.lbl_ml_fit)

        sec.addWidget(self._ml_section)

        # ── Opacity group + Toggles group ──────────────────────────────────
        _group_style = (
            'QGroupBox { color:#8ab; font-size:11px; font-weight:600; '
            'border:1px solid #334; border-radius:4px; '
            'margin-top:8px; padding:6px 6px 4px 6px; } '
            'QGroupBox::title { subcontrol-origin:margin; '
            'subcontrol-position: top left; left:8px; padding:0 4px; }'
        )

        grp_opacity = QGroupBox('Opacity')
        grp_opacity.setStyleSheet(_group_style)
        slider_col = QVBoxLayout(grp_opacity)
        slider_col.setSpacing(3)
        slider_col.setContentsMargins(8, 4, 8, 4)

        row_ms = QHBoxLayout()
        row_ms.addWidget(self._label('Map:'))
        self.sl_map_opacity = QSlider(Qt.Horizontal)
        self.sl_map_opacity.setRange(3, 85)
        self.sl_map_opacity.setValue(18)
        self.sl_map_opacity.setMaximumWidth(100)
        self.sl_map_opacity.valueChanged.connect(
            lambda v: self.app.set_overlay_opacity('map', v))
        row_ms.addWidget(self.sl_map_opacity)
        slider_col.addLayout(row_ms)

        row_is = QHBoxLayout()
        row_is.addWidget(self._label('Inv:'))
        self.sl_inv_opacity = QSlider(Qt.Horizontal)
        self.sl_inv_opacity.setRange(3, 95)
        self.sl_inv_opacity.setValue(35)
        self.sl_inv_opacity.setMaximumWidth(100)
        self.sl_inv_opacity.valueChanged.connect(
            lambda v: self.app.set_overlay_opacity('inv', v))
        row_is.addWidget(self.sl_inv_opacity)
        slider_col.addLayout(row_is)

        row_rs = QHBoxLayout()
        row_rs.addWidget(self._label('Route:'))
        self.sl_route_opacity = QSlider(Qt.Horizontal)
        self.sl_route_opacity.setRange(10, 100)
        self.sl_route_opacity.setValue(82)
        self.sl_route_opacity.setMaximumWidth(100)
        self.sl_route_opacity.valueChanged.connect(self.app.set_route_opacity)
        row_rs.addWidget(self.sl_route_opacity)
        slider_col.addLayout(row_rs)

        grp_toggles = QGroupBox('Toggles')
        grp_toggles.setStyleSheet(_group_style)
        toggles_row = QHBoxLayout(grp_toggles)
        toggles_row.setSpacing(6)
        toggles_row.setContentsMargins(8, 4, 8, 4)

        toggles_col1 = QVBoxLayout()
        toggles_col1.setSpacing(3)
        toggles_col2 = QVBoxLayout()
        toggles_col2.setSpacing(3)

        self.btn_click_through = self._small_btn('Map Pass-Thru: OFF',
                                                 self.app.toggle_map_click_through, '#3a2a0a')
        self.btn_click_through.setToolTip(
            'Turning this off allows you to interact with the overlay to set '
            'your position and calibrate the points.'
        )
        toggles_col1.addWidget(self.btn_click_through)

        self.btn_inv_lock = self._small_btn('Inv: Unlocked',
                                            self.app.toggle_inv_lock, '#1a3a1a')
        self.btn_inv_lock.setToolTip(
            'Locking this allows you to interact with the inventory slots '
            'once you have positioned the window.'
        )
        toggles_col1.addWidget(self.btn_inv_lock)

        self.btn_invert_dirs = self._small_btn('Flip Dirs: OFF',
                                               self.app.toggle_invert_dirs, '#3a1a3a')
        self.btn_invert_dirs.setToolTip(
            'Used for areas like Kur Mountains where surveying returns inverted directions.'
            '\nWill be auto set if we can detect your location in such an area based on the chat logs.'
        )
        toggles_col1.addWidget(self.btn_invert_dirs)

        toggles_col2.addWidget(self.btn_labels)
        toggles_col2.addWidget(self.btn_route_lines)
        overlays_toggles = QHBoxLayout()
        overlays_toggles.setContentsMargins(0,0,0,0)
        overlays_toggles.addWidget(self.btn_overlays_map)
        overlays_toggles.addWidget(self.btn_overlays_inv)
        toggles_col2.addLayout(overlays_toggles)

        toggles_row.addLayout(toggles_col1)
        toggles_row.addLayout(toggles_col2)

        row4 = QHBoxLayout()
        row4.addWidget(grp_opacity)
        row4.addSpacing(10)
        row4.addWidget(grp_toggles)
        row4.addStretch()
        sec.addLayout(row4)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet('color:#223;')
        sec.addWidget(sep2)

        main.addWidget(self._survey_section)

        # ── Log display ───────────────────────────────────────────────────
        self.lbl_log = QLabel('Select a ChatLogs folder above to begin.')
        self.lbl_log.setWordWrap(True)
        self.lbl_log.setStyleSheet('color:#9ab; font-size:11px; padding:2px 0;')
        main.addWidget(self.lbl_log)

    def set_log(self, msg: str):
        self.lbl_log.setText(msg)

    def refresh(self):
        state = self.app.state

        phase_styles = {
            'idle':        ('Idle',              '#778', '#1a1a2a', '#334'),
            'set_player':  ('Setting Position',  '#0d6', '#0a2a1a', '#0d6'),
            'calibrating': ('Calibrating Scale', '#fa0', '#2a1a00', '#fa0'),
            'surveying':   ('Surveying',         '#3c8', '#0a2a1a', '#3c8'),
            'routing':     ('Routing',           '#79f', '#0a1a2a', '#79f'),
        }
        text, fg, bg, border = phase_styles.get(state.phase, phase_styles['idle'])
        self.lbl_phase.setText(text)
        self.lbl_phase.setStyleSheet(
            f'background:{bg}; color:{fg}; padding:2px 8px; '
            f'border:1px solid {border}; border-radius:4px; '
            f'font-size:11px; font-weight:700;'
        )

        scale_txt = (f'Scale: {state.scale:.2f} px/m' if state.scale else 'Scale: uncalibrated')
        self.lbl_scale.setText(scale_txt)

        total   = len(state.items)
        active  = sum(1 for i in state.items if not i['collected'])
        done    = total - active
        self.lbl_count.setText(
            f'{active} active{f", {done} collected" if done else ""}' if total else '0 items'
        )

        ml           = state.ml_mode
        has_items    = bool(state.items)
        has_pos      = state.player_pos is not None
        has_chat_dir = getattr(self.app, '_chat_dir', None) is not None
        placed       = any(i.get('pixel_pos') for i in state.uncollected())

        was_visible = self._survey_section.isVisible()
        self._survey_section.setVisible(has_chat_dir)
        if has_chat_dir != was_visible:
            self.adjustSize()

        # Mode toggle button highlights
        self.btn_mode_regular.setStyleSheet(self._btn_style('#1a3a6a' if not ml else '#111133'))
        self.btn_mode_ml.setStyleSheet(self._btn_style('#4a1a4a' if ml else '#111133'))

        # Regular controls visibility
        self._regular_controls.setVisible(not ml)
        if not ml:
            self.btn_set_pos.setVisible(
                state.phase not in ('routing',) and (not has_pos or state.phase == 'set_player')
            )
            self.btn_start.setVisible(
                state.phase == 'idle' and has_pos and has_chat_dir
            )
            self.btn_done.setVisible(
                state.phase in ('surveying', 'calibrating') and placed
            )
            self.btn_next.setVisible(state.phase == 'routing')
            self.btn_mark.setVisible(state.phase == 'routing')
            self.btn_reset.setVisible(has_items or state.phase != 'idle')
            self.btn_summary.setVisible(
                getattr(self.app, '_summary_data', None) is not None
            )

        # Motherlode controls visibility and content
        self._ml_section.setVisible(ml and has_chat_dir)
        if ml:
            if state.ml_round >= 3:
                self.lbl_ml_status.setText('Trilateration complete — mine the motherlodes!')
            elif state.ml_phase == 'set_pos':
                self.lbl_ml_status.setText(
                    f'Round {state.ml_round + 1}: Click map to set Position {state.ml_round + 1}'
                )
            else:
                self.lbl_ml_status.setText(
                    f'Round {state.ml_round + 1}: Scan motherlodes, then click "Next Position"'
                )
            self.lbl_ml_count.setText(
                f'{len(state.ml_pending)} distance(s) collected this round'
            )
            if state.scale:
                self.lbl_ml_scale.setText(f'Scale: {state.scale:.2f} px/m')
            else:
                self.lbl_ml_scale.setText('Scale: pending (auto-computed after round 3)')
            fit = self.app._ml_fit_quality() if state.ml_round >= 3 else None
            if fit is not None and fit > 50:
                self.lbl_ml_fit.setText(f'⚠ Fit residual {fit:.1f}px — positions may not be proportional')
            else:
                self.lbl_ml_fit.setText(f'Fit: {fit:.1f}px avg' if fit is not None else '')
            self.btn_ml_next.setEnabled(
                state.ml_phase == 'survey'
                and len(state.ml_pending) > 0
                and state.ml_round < 3
            )
            in_routing = state.ml_round >= 3 and bool(state.ml_route_order)
            has_more = in_routing and any(
                not e.get('collected')
                for e in state.ml_surveys
                if e['id'] != state.ml_active_id
            )
            self.btn_ml_skip.setVisible(in_routing)
            self.btn_ml_skip.setEnabled(has_more)

        _lbl_names = {0: 'Off', 1: 'Name', 2: 'Slot#', 3: 'Both'}
        lbl_state = getattr(self.app.map_overlay, '_show_labels', 1)
        lbl_color = '#5a1a1a' if lbl_state == 0 else '#1a2a3a'
        self.btn_labels.setText(f'Labels: {_lbl_names.get(lbl_state, "Name")}')
        self.btn_labels.setStyleSheet(
            f'QPushButton {{ background:{lbl_color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )

        rl = getattr(self.app, '_route_lines_visible', True)
        rl_label = 'ON' if rl else 'OFF'
        rl_color  = '#1a2a3a' if rl else '#5a1a1a'
        self.btn_route_lines.setText(f'Route: {rl_label}')
        self.btn_route_lines.setStyleSheet(
            f'QPushButton {{ background:{rl_color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )

        inv = getattr(self.app, '_invert_dirs', False)
        self.btn_invert_dirs.setText(f'Flip Dirs: {"ON" if inv else "OFF"}')
        self.btn_invert_dirs.setStyleSheet(
            f'QPushButton {{ background:{"#5a1a5a" if inv else "#3a1a3a"}; color:#cde; '
            f'border:1px solid #446; padding:2px 6px; border-radius:3px; '
            f'font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )

        vis = getattr(self.app, '_map_visible', True)
        label = 'ON' if vis else 'OFF'
        color = '#1a3a1a' if vis else '#5a1a1a'
        self.btn_overlays_map.setText(f'Map: {label}')
        self.btn_overlays_map.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )

        vis = getattr(self.app, '_inv_visible', True)
        label = 'ON' if vis else 'OFF'
        color = '#1a3a1a' if vis else '#5a1a1a'
        self.btn_overlays_inv.setText(f'Inv: {label}')
        self.btn_overlays_inv.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )


# ─────────────────────────────────────────────────────────────────────────────
# Hotkey signal bridge (pynput thread → Qt main thread)
# ─────────────────────────────────────────────────────────────────────────────
class _HotkeySignalBridge(QObject):
    """Emits a Qt signal from the pynput listener thread so the main thread stays safe."""
    triggered = pyqtSignal()


# ─────────────────────────────────────────────────────────────────────────────
# Update checker (worker thread → Qt main thread)
# ─────────────────────────────────────────────────────────────────────────────
class _UpdateChecker(QObject):
    """Queries GitHub Releases in a daemon thread and emits the result on the main thread."""
    result = pyqtSignal(dict)

    def check(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        try:
            req = urllib.request.Request(
                _UPDATE_API_URL,
                headers={
                    'User-Agent': f'GorgonSurveyTracker/{APP_VERSION}',
                    'Accept':     'application/vnd.github+json',
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            tag = (data.get('tag_name') or '').strip()
            dl  = None
            for asset in data.get('assets', []) or []:
                if asset.get('name') == _UPDATE_ASSET_NAME:
                    dl = asset.get('browser_download_url')
                    break
            latest = tag.lstrip('vV') if tag else None
            self.result.emit({
                'ok':           bool(latest),
                'latest':       latest,
                'download_url': dl,
                'error':        None if latest else 'No tag_name in response',
            })
        except Exception as e:
            self.result.emit({
                'ok':           False,
                'latest':       None,
                'download_url': None,
                'error':        f'{type(e).__name__}: {e}',
            })


# ─────────────────────────────────────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────────────────────────────────────
class SurveyApp:
    def __init__(self):
        self.state = SurveyState()

        # Update-check state (must exist before ControlPanel._build_ui reads it via refresh_update_button).
        self._latest_version       = None
        self._latest_download_url  = None
        self._skip_update_version  = None
        self._offset_slots = 0

        self.map_overlay  = MapOverlay(self.state, self)
        self.inv_overlay  = InventoryOverlay(self.state, self)
        self.control      = ControlPanel(self)

        self.map_overlay.canvas_clicked.connect(self._on_map_canvas_click)

        self._chat_dir         = None
        self._chat_file        = None
        self._chat_offset      = 0
        self._collect_last     = 0.0  # time.monotonic() of last collection
        self._ml_collect_last  = 0.0  # time.monotonic() of last motherlode collection
        self._click_through    = False
        self._inv_locked       = False
        # self._overlays_visible = True
        self._inv_visible      = True
        self._map_visible      = True
        self._route_lines_visible = True
        self._route_alpha         = 0.82   # 0.0–1.0; applied to route-line pen
        self._invert_dirs      = False
        # ── Session summary tracking ──────────────────────────────────────────
        self._survey_start_time     = None   # datetime when Optimize Route clicked
        self._survey_end_time       = None   # datetime when last item collected
        self._collection_timestamps = []     # datetime of each collection event
        self._xp_gained             = {}     # skill_name -> total XP int
        self._inv_items             = {}     # item_name -> total count (primary + bonus)
        self._tracking_xp           = False  # True while routing session is live
        self._summary_data          = None   # dict; set when session completes

        # Polling timer (0.5 s)
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._timer.start(500)

        # Blink timer for pending dot
        self._blink_timer = QTimer()
        self._blink_timer.timeout.connect(lambda: self.map_overlay.refresh())
        self._blink_timer.start(600)

        # Configurable survey-slot hotkey (default: Numpad 0)
        # Config stores Qt.Key_* int + Qt.KeyboardModifiers int (platform-neutral).
        self._capturing_hotkey = False
        self._hotkey_config = {
            'qt_key':    int(Qt.Key_0),
            'qt_mods':   int(Qt.KeypadModifier),
            'modifiers': [],
            'label':     'Num0',
        }
        self._hotkey_down      = False
        self._held_modifiers   = set()   # modifier keys currently held (written by pynput thread)
        self._invkey_config = {
            'qt_key':    int(Qt.Key_I),
            'qt_mods':   int(Qt.KeypadModifier),
            'modifiers': [],
            'label':     'I',
        }
        self._invhotkey_down   = False
        self._mapkey_config = {
            'qt_key':    int(Qt.Key_M),
            'qt_mods':   int(Qt.KeypadModifier),
            'modifiers': [],
            'label':     'M',
        }
        self._maphotkey_down   = False
        self._kb_listener      = None

        if _HOTKEY_SUPPORTED:
            self._hk_bridge = _HotkeySignalBridge()
            self._hk_bridge.triggered.connect(self._trigger_survey_slot)
            self._maphk_bridge = _HotkeySignalBridge()
            self._maphk_bridge.triggered.connect(self.toggle_map_overlay)
            self._invhk_bridge = _HotkeySignalBridge()
            self._invhk_bridge.triggered.connect(self.toggle_inv_overlay)
            self._start_kb_listener()
        elif sys.platform == 'win32':
            # Legacy fallback when pynput is not installed
            self._hotkey_timer = QTimer()
            self._hotkey_timer.timeout.connect(self._poll_hotkeys)
            self._hotkey_timer.start(50)

        self._load_settings()
        self.control.refresh()  # update section visibility after settings are loaded
        self.control.refresh_update_button()

        # Update checker: initial check shortly after startup, then every 5 minutes.
        self._update_checker = _UpdateChecker()
        self._update_checker.result.connect(self._on_update_check_result)
        self._cleanup_stale_update_files()
        QTimer.singleShot(1000, self._check_for_updates)
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._check_for_updates)
        self._update_timer.start(5 * 60 * 1000)

        if self._map_visible:
            self.map_overlay.show()
            _macos_raise_overlay(self.map_overlay)
        if self._inv_visible:
            self.inv_overlay.show()
            _macos_raise_overlay(self.inv_overlay)

        self.control.show()

    # ── file selection ────────────────────────────────────────────────────────
    def select_chat_dir(self):
        start = str(_GORGON_CHAT_DEFAULT) if _GORGON_CHAT_DEFAULT.is_dir() else ''
        path = QFileDialog.getExistingDirectory(self.control, 'Select ChatLogs folder', start)
        if not path:
            return
        self._chat_dir    = path
        self._chat_file   = None
        self._chat_offset = 0
        self.control.lbl_file_status.setText('Chat dir loaded')
        self._set_log('ChatLogs folder selected — monitoring for survey markers and collections.')
        self.save_settings()
        self._refresh_all()

    # ── phase transitions ─────────────────────────────────────────────────────
    def enter_set_player(self):
        self.state.phase = 'set_player'
        self._refresh_all()
        self._set_log('Click anywhere on the map overlay to mark your current in-game position.')

    def start_surveying(self):
        if not self.state.player_pos:
            self._set_log('⚠ Set your position on the map first.')
            return
        self.state.phase = 'surveying'
        self._refresh_all()
        self._set_log('Watching chat log for survey markers. Survey your maps in-game!')

    def done_surveying(self):
        uncollected = self.state.uncollected()
        placed = [i for i in uncollected if i['pixel_pos']]
        if not placed:
            self._set_log('No placed items to route yet.')
            return
        self.state.optimise_route()
        self.state.phase     = 'routing'
        self.state.route_idx = 0
        # ── Start session tracking ────────────────────────────────────────────
        self._survey_start_time     = datetime.datetime.now()
        self._survey_end_time       = None
        self._collection_timestamps = []
        self._xp_gained             = {}
        self._inv_items             = {}
        self._tracking_xp           = True
        self._summary_data          = None
        # ─────────────────────────────────────────────────────────────────────
        self._refresh_all()
        first = next((i for i in self.state.items if i['id'] == self.state.active_id), None)
        self._set_log(
            f'🗺 Route ready — {len(placed)} stops. First: '
            f'{clean_name(first["name"]) if first else "?"}'
            f' (slot {first["grid_index"] + 1 if first else "?"})'
        )

    def advance_route(self):
        # Mark the current target as skipped — removes it from the map
        current_id = self.state.active_id
        if current_id is not None:
            cur = next((i for i in self.state.items if i['id'] == current_id), None)
            if cur:
                cur['skipped'] = True

        remaining = [
            (idx, iid) for idx, iid in enumerate(self.state.route_order)
            if idx > self.state.route_idx
            and not next((i for i in self.state.items if i['id'] == iid), {}).get('collected')
            and not next((i for i in self.state.items if i['id'] == iid), {}).get('skipped')
        ]
        if not remaining:
            self._set_log('🎉 All stops visited!')
            self.state.phase = 'idle'
            self._refresh_all()
            return
        self.state.route_idx = remaining[0][0]
        self._refresh_all()
        item = next((i for i in self.state.items if i['id'] == self.state.active_id), None)
        self._set_log(
            f'➡ Next: {clean_name(item["name"]) if item else "?"}'
            f' — slot {item["grid_index"] + 1 if item else "?"}'
        )

    def mark_complete(self):
        # Manually mark the current route target as collected (for survey types
        # with no chat collection message), then advance to the next stop.
        state = self.state
        if state.phase != 'routing' or state.active_id is None:
            return
        target = next((i for i in state.items if i['id'] == state.active_id), None)
        if not target or target['collected']:
            return

        if target['pixel_pos']:
            state.player_pos = target['pixel_pos']
            self.map_overlay.refresh()

        target['collected'] = True
        if self._tracking_xp:
            self._collection_timestamps.append(datetime.datetime.now())
        if state.survey_count > 0:
            state.survey_count -= 1
            self.control.sb_count.blockSignals(True)
            self.control.sb_count.setValue(state.survey_count)
            self.control.sb_count.blockSignals(False)
        state.reindex()
        self._set_log(f'✔ {clean_name(target["name"])} marked complete — removed from inventory.')

        remaining = [
            (idx, iid) for idx, iid in enumerate(state.route_order)
            if idx > state.route_idx
            and not next((i for i in state.items if i['id'] == iid), {}).get('collected')
            and not next((i for i in state.items if i['id'] == iid), {}).get('skipped')
        ]
        if not remaining:
            self._set_log('🎉 All survey items collected — surveying complete!')
            state.phase = 'idle'
            self._survey_end_time = datetime.datetime.now()
            self._summary_data = self._build_summary_data()
        else:
            state.route_idx = remaining[0][0]
            item = next((i for i in state.items if i['id'] == state.active_id), None)
            self._set_log(
                f'➡ Next: {clean_name(item["name"]) if item else "?"}'
                f' — slot {item["grid_index"] + 1 if item else "?"}'
            )
        self._refresh_all()

    def reset_survey(self):
        if self.state.items:
            ans = QMessageBox.question(
                self.control, 'Reset Survey',
                'Reset all survey data?',
                QMessageBox.Yes | QMessageBox.No
            )
            if ans != QMessageBox.Yes:
                return
        self.state = SurveyState()
        self.map_overlay.state  = self.state
        self.inv_overlay.state  = self.state
        self.control.sb_count.blockSignals(True)
        self.control.sb_count.setValue(0)
        self.control.sb_count.blockSignals(False)
        self._survey_start_time     = None
        self._survey_end_time       = None
        self._collection_timestamps = []
        self._xp_gained             = {}
        self._inv_items             = {}
        self._tracking_xp           = False
        self._summary_data          = None
        self._refresh_all()
        self._set_log('Survey reset. Set your position and start a new survey.')

    # ── map canvas click ──────────────────────────────────────────────────────
    def _on_map_canvas_click(self, cx: float, cy: float):
        state = self.state

        # ── Motherlode mode: map click sets the current round's player position ──
        if state.ml_mode and state.ml_phase == 'set_pos' and state.ml_round < 3:
            round_idx = state.ml_round
            if len(state.ml_positions) <= round_idx:
                state.ml_positions.append((cx, cy))
            else:
                state.ml_positions[round_idx] = (cx, cy)
            state.ml_phase = 'survey'
            self._set_log(
                f'Position {round_idx + 1} set. '
                f'Scan motherlodes in-game, then click "Next Position".'
            )
            self._refresh_all()
            return

        if state.phase == 'set_player':
            state.player_pos = (cx, cy)
            if state.pending_calib is not None:
                # Position was repositioned mid-calibration — resume calibration
                state.phase = 'calibrating'
                self._set_log('Position updated. Click the map where the pending item appears to calibrate scale.')
            elif self._chat_dir:
                # Auto-advance: skip the manual "Start Survey" button click
                state.phase = 'surveying'
                self._set_log('Position set — watching for survey markers. Survey your maps in-game!')
            else:
                state.phase = 'idle'
                self._set_log('Position set. Select a ChatLogs folder to start surveying.')
            self._refresh_all()
            self.save_settings()
            return

        if state.phase == 'calibrating' and state.pending_calib:
            item = state.pending_calib
            item['pixel_pos']   = (cx, cy)
            state.pending_calib = None

            dx = cx - state.player_pos[0]
            dy = cy - state.player_pos[1]
            px_dist = math.sqrt(dx * dx + dy * dy)
            m_dist  = math.sqrt(item['offset']['east'] ** 2 + item['offset']['north'] ** 2)

            if m_dist > 0 and px_dist > 2:
                state.scale = px_dist / m_dist
                self._set_log(
                    f'Scale set: {state.scale:.2f} px/m. '
                    f'Subsequent items will be auto-placed.'
                )
            else:
                state.scale = 1.0
                self._set_log('⚠ Could not determine scale — item too close. Adjust manually.')

            state.phase = 'surveying'
            self._refresh_all()
            return

        # During survey: recalibrate using most recent placed item
        if state.phase == 'surveying':
            last = next(
                (i for i in reversed(state.items) if not i['collected'] and i['pixel_pos']),
                None
            )
            if not last or not state.player_pos:
                return
            dx = cx - state.player_pos[0]
            dy = cy - state.player_pos[1]
            px_dist = math.sqrt(dx * dx + dy * dy)
            m_dist  = math.sqrt(last['offset']['east'] ** 2 + last['offset']['north'] ** 2)
            if m_dist > 0 and px_dist > 2:
                state.scale           = px_dist / m_dist
                last['pixel_pos'] = (cx, cy)
                self._refresh_all()
                self._set_log(
                    f'Scale recalibrated: {state.scale:.2f} px/m '
                    f'(using {clean_name(last["name"])}).'
                )

    # ── log events ────────────────────────────────────────────────────────────
    def _on_survey_item(self, name: str, offset: dict):
        state = self.state
        if state.phase not in ('surveying', 'calibrating'):
            return
        if not state.player_pos:
            self._set_log('⚠ No player position set — click "Set My Position" first.')
            return

        # Deduplication: same name AND similar coordinates = same map clicked twice
        # Different items of the same type (e.g. two "Deer Antler" surveys) will have
        # offsets far enough apart that they won't match within the tolerance.
        _TOL = 2.0  # metres
        existing = next(
            (i for i in state.items
             if not i['collected']
             and i['name'] == name
             and abs(i['offset']['east']  - offset['east'])  < _TOL
             and abs(i['offset']['north'] - offset['north']) < _TOL),
            None
        )
        if existing:
            # Improve position estimate by averaging pixel placements
            if state.scale is not None and existing['pixel_pos'] is not None \
                    and existing['pixel_estimates']:
                cw = self.map_overlay.width()
                ch = self.map_overlay.canvas_h
                new_px = state.player_to_pixel(offset, cw, ch, self._invert_dirs)
                if new_px:
                    existing['pixel_estimates'].append(new_px)
                    n = len(existing['pixel_estimates'])
                    avg_x = sum(e[0] for e in existing['pixel_estimates']) / n
                    avg_y = sum(e[1] for e in existing['pixel_estimates']) / n
                    existing['pixel_pos'] = (avg_x, avg_y)
                    self._set_log(
                        f'📍 {clean_name(name)} refined ({n} readings).'
                    )
                    self._refresh_all()
            return  # duplicate — do not add a new item

        item = state.add_item(name, offset)

        if state.scale is None:
            # Need calibration
            state.phase         = 'calibrating'
            state.pending_calib = item
            self._set_log(
                f'📍 {clean_name(name)} found! '
                f'Click the map where it appears to calibrate the scale.'
            )
        else:
            # Auto-place and record the estimate
            cw = self.map_overlay.width()
            ch = self.map_overlay.canvas_h
            px = state.player_to_pixel(offset, cw, ch, self._invert_dirs)
            item['pixel_pos'] = px
            if px:
                item['pixel_estimates'].append(px)
            self._set_log(
                f'✅ {clean_name(name)} auto-placed — slot {item["grid_index"] + 1}.'
            )

        self._refresh_all()

    def _on_item_collected(self, collected_name: str):
        state     = self.state
        name_low  = collected_name.lower()

        print(f'[collect] "{collected_name}" | phase={state.phase} | route_idx={state.route_idx} | time={datetime.datetime.now()}s')

        # Deduplicate: ignore repeated collect events within 0.5 s
        now = time.monotonic()
        if now - self._collect_last < 0.5:
            print(f'[collect]   DEDUP — ignored (within 0.5 s)')
            return
        self._collect_last = now

        # Prefer current route target
        target = None
        if state.phase == 'routing' and state.active_id:
            cur = next((i for i in state.items if i['id'] == state.active_id), None)
            cur_name = clean_name(cur['name']) if cur else '?'
            if cur and not cur['collected'] and clean_name(cur['name']).lower() == name_low:
                target = cur
                print(f'[collect]   matched route target "{cur_name}" (id={state.active_id})')

        if not target:
            print(f'[collect]   no target found — ignored')
            return

        # Move player marker to the collected item's location (you were there to grab it)
        if target['pixel_pos']:
            state.player_pos = target['pixel_pos']
            self.map_overlay.refresh()

        target['collected'] = True
        if self._tracking_xp:
            self._collection_timestamps.append(datetime.datetime.now())
        if state.survey_count > 0:
            state.survey_count -= 1
            self.control.sb_count.blockSignals(True)
            self.control.sb_count.setValue(state.survey_count)
            self.control.sb_count.blockSignals(False)
        state.reindex()
        self._set_log(f'✔ {clean_name(target["name"])} collected — removed from inventory.')

        if state.phase == 'routing':
            if target['id'] == state.active_id:
                # Collected the current route target — advance to the next stop
                remaining = [
                    (idx, iid) for idx, iid in enumerate(state.route_order)
                    if idx > state.route_idx
                    and not next((i for i in state.items if i['id'] == iid), {}).get('collected')
                    and not next((i for i in state.items if i['id'] == iid), {}).get('skipped')
                ]
                if not remaining:
                    self._set_log('🎉 All survey items collected — surveying complete!')
                    state.phase = 'idle'
                    self._survey_end_time = datetime.datetime.now()
                    self._summary_data = self._build_summary_data()
                else:
                    state.route_idx = remaining[0][0]
                    item = next((i for i in state.items if i['id'] == state.active_id), None)
                    print(f'[collect]   advanced route_idx → {state.route_idx} (next: "{clean_name(item["name"]) if item else "?"}")')
                    self._set_log(
                        f'➡ Next: {clean_name(item["name"]) if item else "?"}'
                        f' — slot {item["grid_index"] + 1 if item else "?"}'
                    )
            else:
                cur = next((i for i in state.items if i['id'] == state.active_id), None)
                print(f'[collect]   out-of-order — route_idx stays at {state.route_idx} (target still "{clean_name(cur["name"]) if cur else "?"}")')

        self._refresh_all()

    # ── polling ───────────────────────────────────────────────────────────────
    # ── Motherlode mode ───────────────────────────────────────────────────────
    def enter_ml_mode(self):
        self.state.ml_mode = True
        self._set_log('Motherlode mode: click map to set Position 1.')
        self._refresh_all()

    def exit_ml_mode(self):
        self.state.ml_mode = False
        self._set_log('Returned to Regular Survey mode.')
        self._refresh_all()

    def ml_next_position(self):
        state = self.state
        if not state.ml_mode:
            return
        if state.ml_phase != 'survey' or not state.ml_pending:
            self._set_log('Scan at least one motherlode first.')
            return

        round_idx = state.ml_round
        pending   = list(state.ml_pending)

        # Grow ml_surveys to match the number of distances collected this round
        while len(state.ml_surveys) < len(pending):
            state.ml_add_entry()

        # Commit each pending distance to its matching motherlode entry
        for i, dist in enumerate(pending):
            entry = state.ml_surveys[i]
            # Pad any skipped prior rounds with 0
            while len(entry['distances']) < round_idx:
                entry['distances'].append(0.0)
            if len(entry['distances']) == round_idx:
                entry['distances'].append(dist)

        state.ml_pending.clear()
        state.ml_round += 1

        if state.ml_round >= 3:
            computed = self._ml_compute_scale()
            if computed:
                state.scale = computed
            self._ml_trilaterate_all()
            self._ml_optimise_route()
            fit = self._ml_fit_quality()
            fit_msg = f'  Fit: {fit:.1f}px avg' if fit is not None else ''
            if computed:
                scale_msg = f'  Scale: {computed:.2f} px/m (auto)'
            elif state.scale:
                scale_msg = f'  Scale: {state.scale:.2f} px/m (from Regular Survey)'
            else:
                scale_msg = '  ⚠ No scale — calibrate via Regular Survey first.'
            self._set_log(f'Trilateration complete.{scale_msg}{fit_msg}')
        else:
            state.ml_phase = 'set_pos'
            self._set_log(
                f'Round {round_idx + 1} committed ({len(pending)} distances). '
                f'Move to Position {state.ml_round + 1} and click the map.'
            )
        self._refresh_all()

    def _ml_compute_scale(self):
        """Solve for scale using the quadratic-in-s² convergence algorithm.
        Updates state.scale on success. Returns the computed scale or None."""
        result = ml_solve_scale(self.state.ml_positions, self.state.ml_surveys)
        return result

    def _ml_trilaterate_all(self):
        state = self.state
        if len(state.ml_positions) < 3 or not state.scale:
            return
        p1, p2, p3 = state.ml_positions[0], state.ml_positions[1], state.ml_positions[2]
        for entry in state.ml_surveys:
            dsts = entry['distances']
            if len(dsts) < 3 or any(d == 0.0 for d in dsts[:3]):
                entry['estimated_pos'] = None
                continue
            r1 = dsts[0] * state.scale
            r2 = dsts[1] * state.scale
            r3 = dsts[2] * state.scale
            entry['estimated_pos'] = trilaterate(p1, r1, p2, r2, p3, r3)

    def _ml_fit_quality(self):
        """Return average circle residual in pixels, or None if uncalculable."""
        state = self.state
        if not state.scale or len(state.ml_positions) < 3:
            return None
        total = 0.0
        count = 0
        for entry in state.ml_surveys:
            ep = entry.get('estimated_pos')
            if not ep:
                continue
            for i, pos in enumerate(state.ml_positions[:3]):
                if i >= len(entry['distances']):
                    break
                d = entry['distances'][i]
                if d == 0:
                    continue
                r_expected = d * state.scale
                r_actual   = pt_dist(ep, pos)
                total += abs(r_actual - r_expected)
                count += 1
        return total / count if count else None

    def _ml_optimise_route(self):
        """Nearest-neighbour + 2-opt route through uncollected estimated positions."""
        state      = self.state
        candidates = [e for e in state.ml_surveys
                      if not e['collected'] and e.get('estimated_pos')]
        if not candidates:
            return
        start     = state.ml_positions[-1] if state.ml_positions else (0.0, 0.0)
        remaining = list(candidates)
        route     = []
        current   = start
        while remaining:
            nearest = min(remaining, key=lambda e: pt_dist(current, e['estimated_pos']))
            route.append(nearest['id'])
            current = nearest['estimated_pos']
            remaining.remove(nearest)
        route = self._ml_two_opt(route)
        state.ml_route_order = route
        state.ml_route_idx   = 0
        for idx, eid in enumerate(route):
            entry = next((e for e in state.ml_surveys if e['id'] == eid), None)
            if entry:
                entry['route_order'] = idx

    def _ml_two_opt(self, route: list) -> list:
        pos   = {e['id']: e['estimated_pos'] for e in self.state.ml_surveys}
        start = self.state.ml_positions[-1] if self.state.ml_positions else (0.0, 0.0)
        pts   = [start] + [pos[eid] for eid in route]
        ids   = [None]  + list(route)
        n     = len(pts)
        improved = True
        while improved:
            improved = False
            for i in range(n - 2):
                for j in range(i + 2, n - 1):
                    d_old = pt_dist(pts[i], pts[i+1]) + pt_dist(pts[j], pts[j+1])
                    d_new = pt_dist(pts[i], pts[j])   + pt_dist(pts[i+1], pts[j+1])
                    if d_new < d_old - 1e-9:
                        pts[i+1:j+1] = pts[i+1:j+1][::-1]
                        ids[i+1:j+1] = ids[i+1:j+1][::-1]
                        improved = True
        return ids[1:]

    def ml_skip_next(self):
        """Skip the current target and advance to the next uncollected."""
        state = self.state
        remaining = [
            (idx, eid) for idx, eid in enumerate(state.ml_route_order)
            if idx > state.ml_route_idx
            and not next((e for e in state.ml_surveys if e['id'] == eid), {}).get('collected')
        ]
        if not remaining:
            self._set_log('All motherlodes visited!')
            self._refresh_all()
            return
        state.ml_route_idx = remaining[0][0]
        self._refresh_all()
        entry = next((e for e in state.ml_surveys
                      if e['id'] == state.ml_active_id), None)
        self._set_log(f'Next: Treasure {entry["id"]}' if entry else 'Route complete.')

    def reset_ml(self):
        state = self.state
        state.ml_round       = 0
        state.ml_phase       = 'set_pos'
        state.ml_positions.clear()
        state.ml_surveys.clear()
        state.ml_pending.clear()
        state._ml_next_id    = 0
        state.ml_route_order = []
        state.ml_route_idx   = -1
        self._set_log('Motherlode reset. Click map to set Position 1.')
        self._refresh_all()

    def _on_ml_collected(self):
        """Mark the next uncollected entry in route order as collected.
        Deduplicates: multiple slab lines from the same mine event are
        suppressed within a 5-second window after the first is processed."""
        now = time.monotonic()
        if now - self._ml_collect_last < 5.0:
            return
        self._ml_collect_last = now
        state = self.state

        # Prefer the current route target; fall back to first uncollected
        active = state.ml_active_id
        target = next((e for e in state.ml_surveys if e['id'] == active), None) \
                 if active else None
        if target is None:
            target = next((e for e in state.ml_surveys if not e['collected']), None)
        if target is None:
            return

        target['collected'] = True
        if target.get('estimated_pos'):
            self.state.player_pos = tuple(target['estimated_pos'])
        self._set_log(f'Motherlode Treasure {target["id"]} collected.')

        # Advance route index to next uncollected entry
        remaining = [
            (idx, eid) for idx, eid in enumerate(state.ml_route_order)
            if idx > state.ml_route_idx
            and not next((e for e in state.ml_surveys if e['id'] == eid), {}).get('collected')
        ]
        if remaining:
            state.ml_route_idx = remaining[0][0]
            nxt = next((e for e in state.ml_surveys
                        if e['id'] == state.ml_active_id), None)
            if nxt:
                self._set_log(
                    f'Treasure {target["id"]} collected.  Next: Treasure {nxt["id"]}')
        self._refresh_all()

    def _poll(self):
        self._poll_chat_log()

    def _poll_chat_log(self):
        if not self._chat_dir:
            return
        try:
            chat_dir = Path(self._chat_dir)
            # Rediscover latest .log each poll (handles date rollover)
            if not self._chat_file:
                latest, latest_mtime = None, 0
                for p in chat_dir.glob('*.log'):
                    mt = p.stat().st_mtime
                    if mt > latest_mtime:
                        latest_mtime = mt
                        latest       = p
                if latest:
                    self._chat_file   = latest
                    self._chat_offset = latest.stat().st_size  # start from end
                    self._apply_last_known_zone()

            if not self._chat_file:
                return

            path = Path(self._chat_file)
            size = path.stat().st_size
            if size < self._chat_offset:   # file rolled over
                self._chat_file   = None
                self._chat_offset = 0
                return
            if size > self._chat_offset:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(self._chat_offset)
                    new_text = f.read()
                self._chat_offset = size
                lines = new_text.splitlines()
                # Populate _inv_items BEFORE the main loop so that if the last
                # collected! line triggers _build_summary_data(), _inv_items is ready.
                if self._tracking_xp and not self.state.ml_mode:
                    self._track_summary_items(lines)
                for line in lines:
                    area = parse_enter_area_line(line)
                    if area is not None:
                        self._apply_zone_flip(area)
                    if self.state.ml_mode:
                        dist = parse_ml_dist_line(line)
                        if dist is not None and self.state.ml_phase == 'survey' and self.state.ml_round < 3:
                            self.state.ml_pending.append(dist)
                            n = len(self.state.ml_pending)
                            self._set_log(
                                f'Round {self.state.ml_round + 1}: {n} distance(s) collected '
                                f'(latest: {dist:.0f}m). Click "Next Position" when done scanning.'
                            )
                            self.control.refresh()
                            self.map_overlay.refresh()
                            self.inv_overlay.refresh()
                        if parse_ml_collect_line(line):
                            self._on_ml_collected()
                    else:
                        result = parse_chat_survey_line(line)
                        if result:
                            self._on_survey_item(*result)
                        name = parse_collect_line(line)
                        if name:
                            self._on_item_collected(name)
                        if self._tracking_xp:
                            xm = _XP_RE.search(line)
                            if xm:
                                skill = xm.group(2).strip()
                                if skill.lower() in _XP_SKILLS:
                                    amount = int(xm.group(1).replace(',', ''))
                                    self._xp_gained[skill] = (
                                        self._xp_gained.get(skill, 0) + amount
                                    )
        except Exception:
            pass

    # ── inventory slot click ──────────────────────────────────────────────────
    def on_inventory_click(self, item: dict):
        """Flash the corresponding dot on the map."""
        # A brief highlight via a one-shot timer
        self.map_overlay._flash_item_id = item['id']
        self.map_overlay.refresh()
        QTimer.singleShot(500, lambda: self._clear_flash())

    def _clear_flash(self):
        self.map_overlay._flash_item_id = None
        self.map_overlay.refresh()

    # ── Survey-slot hotkey ────────────────────────────────────────────────────
    def _start_kb_listener(self):
        """Start (or restart) the pynput keyboard listener in a daemon thread."""
        if not _HOTKEY_SUPPORTED:
            return
        self._stop_kb_listener()

        def on_press(key):
            # Track held modifiers
            if key in (_pynput_kb.Key.ctrl, _pynput_kb.Key.ctrl_l, _pynput_kb.Key.ctrl_r):
                self._held_modifiers.add('ctrl')
            elif key in (_pynput_kb.Key.shift, _pynput_kb.Key.shift_l, _pynput_kb.Key.shift_r):
                self._held_modifiers.add('shift')
            elif key in (_pynput_kb.Key.alt, _pynput_kb.Key.alt_l, _pynput_kb.Key.alt_r):
                self._held_modifiers.add('alt')

            if self._capturing_hotkey or self._hotkey_down or self._maphotkey_down or self._invhotkey_down:
                return

            if self._pynput_matches(key, self._hotkey_config):
                self._hotkey_down = True
                self._hk_bridge.triggered.emit()
            if self._pynput_matches(key, self._mapkey_config):
                self._maphotkey_down = True
                self._maphk_bridge.triggered.emit()
            if self._pynput_matches(key, self._invkey_config):
                self._invhotkey_down = True
                self._invhk_bridge.triggered.emit()


        def on_release(key):
            if key in (_pynput_kb.Key.ctrl, _pynput_kb.Key.ctrl_l, _pynput_kb.Key.ctrl_r):
                self._held_modifiers.discard('ctrl')
            elif key in (_pynput_kb.Key.shift, _pynput_kb.Key.shift_l, _pynput_kb.Key.shift_r):
                self._held_modifiers.discard('shift')
            elif key in (_pynput_kb.Key.alt, _pynput_kb.Key.alt_l, _pynput_kb.Key.alt_r):
                self._held_modifiers.discard('alt')

            if self._pynput_matches(key, self._hotkey_config):
                self._hotkey_down = False
            if self._pynput_matches(key, self._invkey_config):
                self._invhotkey_down = False
            if self._pynput_matches(key, self._mapkey_config):
                self._maphotkey_down = False

        try:
            listener = _pynput_kb.Listener(on_press=on_press, on_release=on_release)
            listener.daemon = True
            listener.start()
            self._kb_listener = listener
        except Exception:
            self._kb_listener = None
            self._set_log(
                '⚠ Hotkey listener failed to start. '
                'On macOS grant Accessibility permission; on Linux check X11/Wayland.'
            )

    def _stop_kb_listener(self):
        if self._kb_listener is not None:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
            self._kb_listener = None

    # Windows VK codes for numpad digits (used in _pynput_matches)
    _WIN32_NUMPAD_VK = {
        int(Qt.Key_0): 0x60, int(Qt.Key_1): 0x61, int(Qt.Key_2): 0x62,
        int(Qt.Key_3): 0x63, int(Qt.Key_4): 0x64, int(Qt.Key_5): 0x65,
        int(Qt.Key_6): 0x66, int(Qt.Key_7): 0x67, int(Qt.Key_8): 0x68,
        int(Qt.Key_9): 0x69,
    }

    # macOS CGKeyCodes for numpad digits
    _DARWIN_NUMPAD_VK = {
        int(Qt.Key_0): 82, int(Qt.Key_1): 83, int(Qt.Key_2): 84,
        int(Qt.Key_3): 85, int(Qt.Key_4): 86, int(Qt.Key_5): 87,
        int(Qt.Key_6): 88, int(Qt.Key_7): 89, int(Qt.Key_8): 91,
        int(Qt.Key_9): 92,
    }

    # Qt special key → pynput Key enum (built lazily when pynput is available)
    _QT_TO_PYNPUT_SPECIAL = None

    def _get_qt_to_pynput_special(self):
        if self._QT_TO_PYNPUT_SPECIAL is None and _PYNPUT_AVAILABLE:
            K = _pynput_kb.Key
            SurveyApp._QT_TO_PYNPUT_SPECIAL = {
                int(Qt.Key_F1):  K.f1,  int(Qt.Key_F2):  K.f2,
                int(Qt.Key_F3):  K.f3,  int(Qt.Key_F4):  K.f4,
                int(Qt.Key_F5):  K.f5,  int(Qt.Key_F6):  K.f6,
                int(Qt.Key_F7):  K.f7,  int(Qt.Key_F8):  K.f8,
                int(Qt.Key_F9):  K.f9,  int(Qt.Key_F10): K.f10,
                int(Qt.Key_F11): K.f11, int(Qt.Key_F12): K.f12,
                int(Qt.Key_Insert):   K.insert,   int(Qt.Key_Delete): K.delete,
                int(Qt.Key_Home):     K.home,     int(Qt.Key_End):    K.end,
                int(Qt.Key_PageUp):   K.page_up,  int(Qt.Key_PageDown): K.page_down,
                int(Qt.Key_Left):     K.left,     int(Qt.Key_Right):  K.right,
                int(Qt.Key_Up):       K.up,       int(Qt.Key_Down):   K.down,
                int(Qt.Key_Backspace): K.backspace, int(Qt.Key_Tab):  K.tab,
                int(Qt.Key_Return):   K.enter,    int(Qt.Key_Space):  K.space,
                int(Qt.Key_Escape):   K.esc,
            }
        return SurveyApp._QT_TO_PYNPUT_SPECIAL or {}

    def _pynput_matches(self, key, hk: dict) -> bool:
        """Return True if the pynput key event matches the given hotkey config."""
        qt_key  = hk.get('qt_key',  0)
        qt_mods = hk.get('qt_mods', int(Qt.NoModifier))
        mods    = hk.get('modifiers', [])
        is_numpad = bool(qt_mods & int(Qt.KeypadModifier))

        for m in mods:
            if m not in self._held_modifiers:
                return False

        special = self._get_qt_to_pynput_special()
        if qt_key in special:
            return key == special[qt_key]

        if is_numpad and int(Qt.Key_0) <= qt_key <= int(Qt.Key_9):
            if sys.platform == 'win32':
                expected_vk = self._WIN32_NUMPAD_VK.get(qt_key)
                return expected_vk is not None and getattr(key, 'vk', None) == expected_vk
            elif sys.platform == 'darwin':
                expected_vk = self._DARWIN_NUMPAD_VK.get(qt_key)
                return expected_vk is not None and getattr(key, 'vk', None) == expected_vk
            return getattr(key, 'char', None) == chr(qt_key)

        if int(Qt.Key_A) <= qt_key <= int(Qt.Key_Z):
            return getattr(key, 'char', None) == chr(qt_key).lower()
        if int(Qt.Key_0) <= qt_key <= int(Qt.Key_9):
            return getattr(key, 'char', None) == chr(qt_key)

        return False


    # Legacy Windows-only polling (used when pynput is not installed)
    def _poll_hotkeys(self):
        if self._capturing_hotkey:
            return
        user32  = ctypes.windll.user32
        hk      = self._hotkey_config
        # Support both old vk-based configs and new qt_key configs on Windows
        if 'vk' in hk:
            vk = hk['vk']
        else:
            # Derive VK from qt_key for the legacy path on Windows
            qt_key  = hk.get('qt_key', int(Qt.Key_0))
            qt_mods = hk.get('qt_mods', int(Qt.KeypadModifier))
            is_kp   = bool(qt_mods & int(Qt.KeypadModifier))
            vk = self._WIN32_NUMPAD_VK.get(qt_key) if is_kp else None
            if vk is None:
                # Fall back: try reading the VK for the character directly
                vk = qt_key if 0x30 <= qt_key <= 0x5A else 0x60
        mods   = hk.get('modifiers', [])
        mod_ok = all(
            user32.GetAsyncKeyState({'ctrl': 0x11, 'shift': 0x10, 'alt': 0x12}[m]) & 0x8000
            for m in mods if m in ('ctrl', 'shift', 'alt')
        )
        pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000) and mod_ok
        if pressed and not self._hotkey_down:
            self._hotkey_down = True
            self._trigger_survey_slot()
        elif not pressed:
            self._hotkey_down = False

    def _trigger_survey_slot(self):
        """Dispatch hotkey press to the right action based on current phase."""
        phase = self.state.phase
        if phase == 'routing':
            self._click_active_route_slot()
        elif phase in ('surveying', 'calibrating'):
            self._click_next_survey_slot()

    def _do_click(self, x: int, y: int):
        """Move cursor to (x, y) and perform one left click — cross-platform.
        x, y are Qt logical coordinates (from mapToGlobal)."""
        # On Windows, Win32 and pynput both use physical pixels, so scale from
        # Qt logical coords by the screen's device pixel ratio (fixes 4K/HiDPI).
        if sys.platform == 'win32':
            screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
            dpr = screen.devicePixelRatio()
            x, y = round(x * dpr), round(y * dpr)
        if _PYNPUT_AVAILABLE:
            m = _pynput_mouse.Controller()
            m.position = (x, y)
            m.press(_pynput_mouse.Button.left)
            m.release(_pynput_mouse.Button.left)
        elif sys.platform == 'win32':
            u = ctypes.windll.user32
            u.SetCursorPos(x, y)
            u.mouse_event(0x0002, 0, 0, 0, 0)
            u.mouse_event(0x0004, 0, 0, 0, 0)

    def _click_active_route_slot(self):
        """Double-click the active route survey item's inventory slot."""
        state = self.state
        active_id = state.active_id
        if active_id is None:
            return
        item = next((i for i in state.items if i['id'] == active_id), None)
        if item is None:
            return
        grid_idx = item['grid_index'] + self._offset_slots
        slots = self.inv_overlay._slots
        if grid_idx < len(slots):
            slot = slots[grid_idx]
            center = slot.mapToGlobal(QPoint(slot.width() // 2, slot.height() // 2))
        else:
            center = self._inv_slot_global_pos(grid_idx)
            if center is None:
                return
        x, y = center.x(), center.y()
        self._do_click(x, y)
        QTimer.singleShot(120, lambda: self._second_click(x, y))

    def _click_next_survey_slot(self):
        """Double-click the next empty inventory slot during the surveying phase."""
        next_idx = len(self.state.uncollected()) + self._offset_slots
        slots = self.inv_overlay._slots
        if next_idx < len(slots):
            slot = slots[next_idx]
            center = slot.mapToGlobal(QPoint(slot.width() // 2, slot.height() // 2))
        else:
            center = self._inv_slot_global_pos(next_idx)
            if center is None:
                return
        x, y = center.x(), center.y()
        self._do_click(x, y)
        QTimer.singleShot(120, lambda: self._second_click(x, y))

    def _inv_slot_global_pos(self, idx):
        """Compute the global screen centre of inventory slot at index idx,
        even if that slot widget hasn't been rendered yet."""
        inv = self.inv_overlay
        slot_w = max(28, (inv.width() - 12 - SLOT_GAP * (GRID_COLS - 1)) // GRID_COLS)
        row, col = divmod(idx, GRID_COLS)
        margin = 6   # _grid_layout ContentsMargins
        x_off = margin + col * (slot_w + SLOT_GAP) + slot_w // 2
        y_off = margin + row * (slot_w + SLOT_GAP) + slot_w // 2
        return inv._grid_container.mapToGlobal(QPoint(x_off, y_off))

    def _second_click(self, x, y):
        self._do_click(x, y)

    # ── overlay visibility ────────────────────────────────────────────────────
    def toggle_route_lines(self):
        self._route_lines_visible = not self._route_lines_visible
        self.map_overlay.refresh()
        self.control.refresh()
        self.save_settings()

    def toggle_map_overlay(self):
        self._map_visible = not self._map_visible
        if self._map_visible:
            self.map_overlay.show()
            self.map_overlay.set_click_through(self._click_through)
            _macos_raise_overlay(self.map_overlay)
        else:
            self.map_overlay.hide()
        self.control.refresh()
        self.save_settings()

    def toggle_inv_overlay(self):
        self._inv_visible = not self._inv_visible
        if self._inv_visible:
            self.inv_overlay.show()
            _set_click_through(int(self.inv_overlay.winId()), self._inv_locked)
            _macos_raise_overlay(self.inv_overlay)
        else:
            self.inv_overlay.hide()
        self.control.refresh()
        self.save_settings()


    # ── opacity / click-through ───────────────────────────────────────────────
    def set_overlay_opacity(self, which: str, value: int):
        alpha = value / 100.0
        if which == 'map':
            self.map_overlay._bg_alpha = alpha
            self.map_overlay.refresh()
        else:
            self.inv_overlay._bg_alpha = alpha
            self.inv_overlay.refresh()
        self.save_settings()

    def set_route_opacity(self, value: int):
        self._route_alpha = value / 100.0
        self.map_overlay.refresh()
        self.save_settings()

    def on_survey_count_changed(self, value: int):
        self.state.survey_count = value
        self.inv_overlay.refresh()
        self.save_settings()

    def on_offset_count_changed(self, value: int):
        self._offset_slots = value
        self.inv_overlay.refresh()
        self.save_settings()

    def toggle_map_click_through(self):
        self._click_through = not self._click_through
        self.map_overlay.set_click_through(self._click_through)
        label = 'ON' if self._click_through else 'OFF'
        color = '#1a4a1a' if self._click_through else '#3a2a0a'
        self.control.btn_click_through.setText(f'Map Pass-Thru: {label}')
        self.control.btn_click_through.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )
        self.map_overlay.refresh()
        self.save_settings()

    def toggle_inv_lock(self):
        self._inv_locked = not self._inv_locked
        _set_click_through(int(self.inv_overlay.winId()), self._inv_locked)
        if self._inv_locked:
            self.control.btn_inv_lock.setText('Inv: Locked')
            self.control.btn_inv_lock.setStyleSheet(
                'QPushButton { background:#3a1a00; color:#cde; border:1px solid #446; '
                'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }'
                'QPushButton:hover { border-color: #8ab; }'
            )
        else:
            self.control.btn_inv_lock.setText('Inv: Unlocked')
            self.control.btn_inv_lock.setStyleSheet(
                'QPushButton { background:#1a3a1a; color:#cde; border:1px solid #446; '
                'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }'
                'QPushButton:hover { border-color: #8ab; }'
            )
        self.inv_overlay.refresh()
        self.save_settings()

    def toggle_map_labels(self):
        self.map_overlay._show_labels = (self.map_overlay._show_labels + 1) % 4
        self.map_overlay.refresh()
        self.control.refresh()
        self.save_settings()

    def set_hotkey_binding(self):
        self._capturing_hotkey = True
        lbl = "Press the desired key combination (Esc = cancel)\n\nDuring Surveying: clicks the next empty Inventory slot\nDuring Routing: double-clicks the active slot to collect it"
        dlg = HotkeyCaptureDialog(lbl, self.control)
        if dlg.exec_() == QDialog.Accepted and dlg.result_qt_key is not None:
            self._hotkey_config = {
                'qt_key':    dlg.result_qt_key,
                'qt_mods':   dlg.result_qt_mods,
                'modifiers': dlg.result_mods,
                'label':     dlg.result_label,
            }
            self.control.btn_hotkey.setText(f'Survey: {dlg.result_label}')
            self.save_settings()
            # if _HOTKEY_SUPPORTED:
            #     self._start_kb_listener()
        self._capturing_hotkey = False
        self.control.adjustSize()
        self.control.adjustSize()

    def remove_hotkey_binding(self):
        self._hotkey_config = {
            'qt_key':    0,
            'qt_mods':   0,
            'modifiers': [],
            'label':     "--",
        }
        self.control.btn_hotkey.setText(f'Survey: --')
        self.save_settings()
        self.control.adjustSize()
        self.control.adjustSize()

    def set_mapkey_binding(self):
        self._capturing_hotkey = True
        lbl = "Press the desired key combination (Esc = cancel)\n\nUse: Click to toggle the Map overlay.\nYou can assign the same key as the Inventory toggle."
        dlg = HotkeyCaptureDialog(lbl, self.control)
        if dlg.exec_() == QDialog.Accepted and dlg.result_qt_key is not None:
            self._mapkey_config = {
                'qt_key':    dlg.result_qt_key,
                'qt_mods':   dlg.result_qt_mods,
                'modifiers': dlg.result_mods,
                'label':     dlg.result_label,
            }
            self.control.btn_mapkey.setText(f'Map: {dlg.result_label}')
            self.save_settings()
            # if _HOTKEY_SUPPORTED:
            #     self._start_kb_listener()
        self._capturing_hotkey = False
        self.control.adjustSize()
        self.control.adjustSize()

    def remove_mapkey_binding(self):
        self._mapkey_config = {
            'qt_key':    0,
            'qt_mods':   0,
            'modifiers': [],
            'label':     "--",
        }
        self.control.btn_mapkey.setText(f'Map: --')
        self.save_settings()
        self.control.adjustSize()
        self.control.adjustSize()


    def set_invkey_binding(self):
        self._capturing_hotkey = True
        lbl = " Press the desired key combination (Esc = cancel)\n\nUse: Click to toggle the inventory overlay.\nYou can assign the same key as the Map toggle."
        dlg = HotkeyCaptureDialog(lbl, self.control)
        if dlg.exec_() == QDialog.Accepted and dlg.result_qt_key is not None:
            self._invkey_config = {
                'qt_key':    dlg.result_qt_key,
                'qt_mods':   dlg.result_qt_mods,
                'modifiers': dlg.result_mods,
                'label':     dlg.result_label,
            }
            self.control.btn_invkey.setText(f'Inv: {dlg.result_label}')
            self.save_settings()
            # if _HOTKEY_SUPPORTED:
            #     self._start_kb_listener()
        self._capturing_hotkey = False
        self.control.adjustSize()
        self.control.adjustSize()

    def remove_invkey_binding(self):
        self._invkey_config = {
            'qt_key':    0,
            'qt_mods':   0,
            'modifiers': [],
            'label':     "--",
        }
        self.control.btn_invkey.setText(f'Inv: --')
        self.save_settings()
        self.control.adjustSize()
        self.control.adjustSize()

    def toggle_invert_dirs(self):
        self._invert_dirs = not self._invert_dirs
        self._recompute_dot_positions()
        self.save_settings()

    def _apply_last_known_zone(self):
        """Scan recent ChatLogs for the most recent 'Entering Area:' line and
        apply it, so Flip Dirs is correct on startup even if the user missed
        the live zone-transition message. Skipped when survey points already
        exist on the map — flipping dirs would mis-place them."""
        if not self._chat_dir:
            return
        if self.state.items or self.state.ml_surveys:
            return
        try:
            chat_dir = Path(self._chat_dir)
            logs = sorted(chat_dir.glob('*.log'),
                          key=lambda p: p.stat().st_mtime,
                          reverse=True)
        except Exception:
            return
        for p in logs:
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.read().splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                area = parse_enter_area_line(line)
                if area is not None:
                    self._apply_zone_flip(area)
                    return

    def _apply_zone_flip(self, area_name: str):
        """Auto-set Flip Dirs based on the area the player just entered."""
        should_flip = area_name in FLIPPED_ZONES
        if should_flip == self._invert_dirs:
            return
        self._invert_dirs = should_flip
        self._recompute_dot_positions()
        self.save_settings()

    def _recompute_dot_positions(self):
        """Re-run player_to_pixel for all auto-placed items using current inversion flag."""
        cw = self.map_overlay.width()
        ch = self.map_overlay.canvas_h
        for item in self.state.items:
            if item['pixel_estimates']:  # only auto-placed dots
                px = self.state.player_to_pixel(
                    item['offset'], cw, ch, self._invert_dirs
                )
                if px:
                    item['pixel_pos'] = px
        self._refresh_all()

    def save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            mg = self.map_overlay.geometry()
            ig = self.inv_overlay.geometry()
            cp = self.control.pos()
            data = {
                'map':  {
                    'x': mg.x(), 'y': mg.y(), 'w': mg.width(), 'h': mg.height(),
                    'opacity': int(self.map_overlay._bg_alpha * 100),
                },
                'inv':  {
                    'x': ig.x(), 'y': ig.y(), 'w': ig.width(), 'h': ig.height(),
                    'opacity': int(self.inv_overlay._bg_alpha * 100),
                },
                'control': {'x': cp.x(), 'y': cp.y()},
                'chat_dir':     self._chat_dir,
                'survey_count': self.state.survey_count,
                'offset_slots': self._offset_slots,
                'map_labels':   self.map_overlay._show_labels,
                'hotkey':       self._hotkey_config,
                'mapkey':       self._mapkey_config,
                'invkey':       self._invkey_config,
                'inv_locked':   self._inv_locked,
                'map_click_through': self._click_through,
                'map_visible':          self._map_visible,
                'inv_visible':          self._inv_visible,
                'route_lines_visible':  self._route_lines_visible,
                'route_alpha':          int(self._route_alpha * 100),
                'invert_dirs':          self._invert_dirs,
                'grid': {
                    'cols': GRID_COLS,
                },
                'skip_update_version': self._skip_update_version,
            }
            st = self.state
            data['survey_state'] = {
                'phase':       st.phase if st.phase != 'calibrating' else 'surveying',
                'player_pos':  list(st.player_pos) if st.player_pos else None,
                'scale':       st.scale,
                'next_id':     st._next_id,
                'route_order': st.route_order,
                'route_idx':   st.route_idx,
                'items': [
                    {
                        'id':              i['id'],
                        'name':            i['name'],
                        'offset':          i['offset'],
                        'pixel_pos':       list(i['pixel_pos']) if i['pixel_pos'] else None,
                        'pixel_estimates': [list(e) for e in i['pixel_estimates']],
                        'grid_index':      i['grid_index'],
                        'collected':       i['collected'],
                        'skipped':         i['skipped'],
                        'route_order':     i['route_order'],
                    }
                    for i in st.items
                ],
            }
            ml = self.state
            data['motherlode_state'] = {
                'ml_mode':        ml.ml_mode,
                'ml_round':       ml.ml_round,
                'ml_phase':       ml.ml_phase,
                'ml_positions':   [list(p) for p in ml.ml_positions],
                'ml_pending':     list(ml.ml_pending),
                'ml_next_id':     ml._ml_next_id,
                'ml_route_order': list(ml.ml_route_order),
                'ml_route_idx':   ml.ml_route_idx,
                'ml_surveys': [
                    {
                        'id':            e['id'],
                        'distances':     list(e['distances']),
                        'estimated_pos': list(e['estimated_pos']) if e['estimated_pos'] else None,
                        'collected':     e['collected'],
                        'route_order':   e.get('route_order', -1),
                    }
                    for e in ml.ml_surveys
                ],
            }
            if self._summary_data:
                # Serialise items list (tuples → lists, fine for JSON round-trip)
                sd = dict(self._summary_data)
                sd['items'] = [list(row) for row in sd['items']]
                data['summary_data'] = sd
            SETTINGS_PATH.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_settings(self):
        try:
            if not SETTINGS_PATH.exists():
                return
            data = json.loads(SETTINGS_PATH.read_text())

            for key, overlay in (('map', self.map_overlay), ('inv', self.inv_overlay)):
                s = data.get(key, {})
                if 'x' in s and 'y' in s:
                    overlay.move(s['x'], s['y'])
                if 'w' in s and 'h' in s:
                    overlay.resize(s['w'], s['h'])
                if 'opacity' in s:
                    overlay._bg_alpha = s['opacity'] / 100.0

            cs = data.get('control', {})
            if 'x' in cs and 'y' in cs:
                self.control.move(cs['x'], cs['y'])

            # Sync slider / spinbox values (block signals to avoid triggering save_settings
            # before log_path / chat_dir have been restored)
            for sl, val in (
                (self.control.sl_map_opacity,   int(self.map_overlay._bg_alpha * 100)),
                (self.control.sl_inv_opacity,   int(self.inv_overlay._bg_alpha * 100)),
                (self.control.sl_route_opacity, int(self._route_alpha * 100)),
            ):
                sl.blockSignals(True)
                sl.setValue(val)
                sl.blockSignals(False)
            if 'survey_count' in data:
                sc = int(data['survey_count'])
                self.state.survey_count = sc
                self.control.sb_count.blockSignals(True)
                self.control.sb_count.setValue(sc)
                self.control.sb_count.blockSignals(False)

            if 'offset_slots' in data:
                os_val = max(0, min(GRID_COLS - 1, int(data['offset_slots'])))
                self._offset_slots = os_val
                self.control.offset_count.blockSignals(True)
                self.control.offset_count.setValue(os_val)
                self.control.offset_count.blockSignals(False)

            if 'map_labels' in data:
                raw = data['map_labels']
                # Backward compat: old bool → 1 (Name) or 0 (Off)
                if isinstance(raw, bool):
                    self.map_overlay._show_labels = 1 if raw else 0
                else:
                    self.map_overlay._show_labels = int(raw)

            if 'hotkey' in data:
                hk = data['hotkey']
                if isinstance(hk, dict):
                    if 'qt_key' in hk:
                        self._hotkey_config = hk
                    elif 'vk' in hk and sys.platform == 'win32':
                        self._hotkey_config = _migrate_vk_config(hk)
                    # else: non-Windows with old vk format → keep default
                    lbl = self._hotkey_config.get('label', 'Num0')
                    self.control.btn_hotkey.setText(f'Survey: {lbl}')

            if 'mapkey' in data:
                hk = data['mapkey']
                if isinstance(hk, dict):
                    if 'qt_key' in hk:
                        self._mapkey_config = hk
                    lbl = self._mapkey_config.get('label', 'M')
                    self.control.btn_mapkey.setText(f'Map: {lbl}')

            if 'invkey' in data:
                hk = data['invkey']
                if isinstance(hk, dict):
                    if 'qt_key' in hk:
                        self._invkey_config = hk
                    lbl = self._invkey_config.get('label', 'I')
                    self.control.btn_invkey.setText(f'Inv: {lbl}')

            # Restart pynput listener with the loaded config
            if _HOTKEY_SUPPORTED:
                self._start_kb_listener()

            if 'inv_locked' in data:
                self._inv_locked = bool(data['inv_locked'])
                _set_click_through(int(self.inv_overlay.winId()), self._inv_locked)
                label = 'Locked' if self._inv_locked else 'Unlocked'
                color = '#3a1a00' if self._inv_locked else '#1a3a1a'
                self.control.btn_inv_lock.setText(f'Inv: {label}')
                self.control.btn_inv_lock.setStyleSheet(
                    f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
                    f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
                    f'QPushButton:hover {{ border-color: #8ab; }}'
                )

            if 'map_click_through' in data:
                self._click_through = bool(data['map_click_through'])
                self.map_overlay.set_click_through(self._click_through)
                label = 'ON' if self._click_through else 'OFF'
                color = '#1a4a1a' if self._click_through else '#3a2a0a'
                self.control.btn_click_through.setText(f'Map Pass-Thru: {label}')
                self.control.btn_click_through.setStyleSheet(
                    f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
                    f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
                    f'QPushButton:hover {{ border-color: #8ab; }}'
                )

            if data.get('chat_dir') and Path(data['chat_dir']).is_dir():
                self._chat_dir = data['chat_dir']
                self.control.lbl_file_status.setText('Chat dir (auto)')
            elif _GORGON_CHAT_DEFAULT.is_dir():
                self._chat_dir = str(_GORGON_CHAT_DEFAULT)
                self.control.lbl_file_status.setText('Chat dir (auto)')

            if 'route_lines_visible' in data:
                self._route_lines_visible = bool(data['route_lines_visible'])

            if 'route_alpha' in data:
                self._route_alpha = max(0.0, min(1.0, int(data['route_alpha']) / 100.0))

            if 'skip_update_version' in data:
                sv = data.get('skip_update_version')
                self._skip_update_version = sv if isinstance(sv, str) and sv else None

            if 'invert_dirs' in data:
                self._invert_dirs = bool(data['invert_dirs'])

            if 'map_visible' in data:
                self._map_visible = bool(data['map_visible'])
                if not self._map_visible:
                    self.map_overlay.hide()

            if 'inv_visible' in data:
                self._inv_visible = bool(data['inv_visible'])
                if not self._inv_visible:
                    self.inv_overlay.hide()

            ss = data.get('survey_state')
            if ss and ss.get('scale') is not None:
                self.state.scale = ss['scale']
            if ss and (ss.get('items') or ss.get('phase', 'idle') != 'idle'):
                st = self.state
                st._next_id    = ss.get('next_id', 0)
                st.player_pos  = tuple(ss['player_pos']) if ss.get('player_pos') else None
                st.route_order = ss.get('route_order', [])
                st.route_idx   = ss.get('route_idx', -1)
                items = []
                for d in ss.get('items', []):
                    items.append({
                        'id':              d['id'],
                        'name':            d['name'],
                        'offset':          d['offset'],
                        'pixel_pos':       tuple(d['pixel_pos']) if d.get('pixel_pos') else None,
                        'pixel_estimates': [tuple(e) for e in d.get('pixel_estimates', [])],
                        'grid_index':      d['grid_index'],
                        'collected':       d['collected'],
                        'skipped':         d.get('skipped', False),
                        'route_order':     d.get('route_order', -1),
                    })
                st.items = items
                phase = ss.get('phase', 'idle')
                if phase == 'set_player' and st.player_pos is None:
                    phase = 'idle'
                st.phase = phase
                # Refresh overlays directly (not via _refresh_all) to avoid a redundant save
                self.map_overlay.refresh()
                self.inv_overlay.refresh()

            mls = data.get('motherlode_state')
            if mls:
                st = self.state
                st.ml_mode      = bool(mls.get('ml_mode', False))
                st.ml_round     = int(mls.get('ml_round', 0))
                st.ml_phase     = mls.get('ml_phase', 'set_pos')
                st.ml_positions = [tuple(p) for p in mls.get('ml_positions', [])]
                st.ml_pending   = list(mls.get('ml_pending', []))
                st._ml_next_id  = int(mls.get('ml_next_id', 0))
                st.ml_route_order = list(mls.get('ml_route_order', []))
                st.ml_route_idx   = int(mls.get('ml_route_idx', -1))
                st.ml_surveys   = []
                for d in mls.get('ml_surveys', []):
                    st.ml_surveys.append({
                        'id':            d['id'],
                        'distances':     list(d.get('distances', [])),
                        'estimated_pos': tuple(d['estimated_pos']) if d.get('estimated_pos') else None,
                        'collected':     bool(d.get('collected', False)),
                        'route_order':   int(d.get('route_order', -1)),
                    })
                # Rebuild route if estimated positions exist but route_order was lost
                if (st.ml_round >= 3
                        and not st.ml_route_order
                        and any(e.get('estimated_pos') for e in st.ml_surveys)):
                    self._ml_optimise_route()

            sd = data.get('summary_data')
            if sd:
                # items were serialised as lists; keep as-is (SummaryWindow handles both)
                self._summary_data = sd

        except Exception:
            pass

    # ── update check / one-click update ───────────────────────────────────────
    def _cleanup_stale_update_files(self):
        """Remove any leftover GorgonSurveyTracker.exe.new from a previous failed update."""
        if not _is_frozen_windows():
            return
        try:
            stale = Path(sys.executable).parent / (_UPDATE_ASSET_NAME + '.new')
            if stale.exists():
                stale.unlink()
        except Exception:
            pass

    def _on_update_button_click(self):
        """The button is only visible when a new version is known — show the dialog."""
        self._show_update_dialog()

    def _check_for_updates(self):
        self._update_checker.check()

    def _on_update_check_result(self, result: dict):
        self._latest_version      = result.get('latest')
        self._latest_download_url = result.get('download_url')
        self.control.refresh_update_button()
        if not result.get('ok'):
            err = result.get('error') or 'unknown error'
            print(f'[update] check failed: {err}', file=sys.stderr)

    def _show_update_dialog(self):
        latest = self._latest_version
        if not latest:
            return
        box = QMessageBox(self.control)
        box.setWindowTitle('Update available')
        box.setIcon(QMessageBox.Information)
        action_label = 'Update' if _is_frozen_windows() and self._latest_download_url else 'Open Releases Page'
        box.setText(
            f'<b>Gorgon Survey Tracker v{latest}</b> is available.<br>'
            f'You are running v{APP_VERSION}.'
        )
        btn_update = box.addButton(action_label, QMessageBox.AcceptRole)
        btn_later  = box.addButton('Later',              QMessageBox.RejectRole)
        btn_skip   = box.addButton('Skip this version',  QMessageBox.DestructiveRole)
        box.setDefaultButton(btn_update)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is btn_update:
            self._start_update_download()
        elif clicked is btn_skip:
            self._skip_update_version = latest
            self.save_settings()
            self.control.refresh_update_button()
        # Later: no-op

    def _start_update_download(self):
        """Trigger the one-click update. Only replaces the exe on frozen Windows; else opens browser."""
        if _is_frozen_windows() and self._latest_download_url:
            self._do_windows_update()
        else:
            webbrowser.open(_UPDATE_PAGE_URL)

    def _do_windows_update(self):
        """Download the new .exe, write a self-deleting batch that swaps it in, and quit."""
        url     = self._latest_download_url
        old_exe = Path(sys.executable)
        new_exe = old_exe.with_name(old_exe.name + '.new')

        # Clean any stale .new file from a prior attempt.
        try:
            if new_exe.exists():
                new_exe.unlink()
        except Exception:
            pass

        # Download with a progress dialog on the main thread. Reads in chunks and
        # pumps the Qt event loop so the UI stays responsive.
        progress = QProgressDialog('Downloading update…', 'Cancel', 0, 0, self.control)
        progress.setWindowTitle('Updating Gorgon Survey Tracker')
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()
        QApplication.processEvents()

        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': f'GorgonSurveyTracker/{APP_VERSION}'},
            )
            with urllib.request.urlopen(req, timeout=30) as resp, new_exe.open('wb') as out:
                total = 0
                try:
                    total = int(resp.headers.get('Content-Length') or 0)
                except Exception:
                    total = 0
                if total > 0:
                    progress.setMaximum(total)
                downloaded = 0
                chunk = 64 * 1024
                while True:
                    if progress.wasCanceled():
                        raise RuntimeError('cancelled')
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    out.write(buf)
                    downloaded += len(buf)
                    if total > 0:
                        progress.setValue(downloaded)
                    QApplication.processEvents()
            # Verify the download completed. urllib won't raise if the TCP
            # connection drops mid-stream — a short read silently becomes an
            # empty chunk, so we check the byte count ourselves.
            if total > 0 and downloaded != total:
                raise RuntimeError(
                    f'download truncated: got {downloaded:,} of {total:,} bytes'
                )
            # Sanity-check: a PyInstaller --onefile build is tens of MB. Anything
            # under 1 MB is almost certainly a broken download or an error page.
            actual_size = new_exe.stat().st_size
            if actual_size < 1_000_000:
                raise RuntimeError(
                    f'downloaded file is too small ({actual_size:,} bytes) — likely corrupted'
                )
        except Exception as e:
            progress.close()
            try:
                if new_exe.exists():
                    new_exe.unlink()
            except Exception:
                pass
            if str(e) != 'cancelled':
                QMessageBox.warning(
                    self.control, 'Update failed',
                    f'Could not download the update:\n{type(e).__name__}: {e}',
                )
            return

        progress.close()

        # Write a self-deleting batch that waits for this process to release the exe,
        # swaps the new one in, launches it, and deletes itself.
        bat_path = Path(tempfile.gettempdir()) / f'gorgon_update_{os.getpid()}.bat'
        bat = (
            '@echo off\r\n'
            'setlocal\r\n'
            'set "OLD={old}"\r\n'
            'set "NEW={new}"\r\n'
            ':wait\r\n'
            'del "%OLD%" >nul 2>&1\r\n'
            'if exist "%OLD%" ( timeout /t 1 /nobreak >nul & goto wait )\r\n'
            'move /y "%NEW%" "%OLD%" >nul\r\n'
            'start "" "%OLD%"\r\n'
            '(goto) 2>nul & del "%~f0"\r\n'
        ).format(old=str(old_exe), new=str(new_exe))
        try:
            bat_path.write_text(bat, encoding='ascii')
        except Exception as e:
            QMessageBox.warning(
                self.control, 'Update failed',
                f'Could not stage the update script:\n{type(e).__name__}: {e}',
            )
            try:
                new_exe.unlink()
            except Exception:
                pass
            return

        # Strip PyInstaller bootloader env vars before spawning the batch.
        # When we're running as a --onefile exe, the bootloader sets vars like
        # _MEIPASS2 and _PYI_APPLICATION_HOME_DIR pointing at OUR extraction dir.
        # If those leak into the freshly-launched new exe, its bootloader gets
        # confused (tries to load DLLs from the deleted old dir, or errors about
        # the var being "not defined" when it's set to empty).
        clean_env = {k: v for k, v in os.environ.items()
                     if not (k.startswith('_MEI') or k.startswith('_PYI_'))}

        try:
            creationflags = 0
            creationflags |= getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
            creationflags |= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
            subprocess.Popen(
                ['cmd', '/c', str(bat_path)],
                creationflags=creationflags,
                close_fds=True,
                env=clean_env,
            )
        except Exception as e:
            QMessageBox.warning(
                self.control, 'Update failed',
                f'Could not launch the update script:\n{type(e).__name__}: {e}',
            )
            return

        self.save_settings()
        QApplication.quit()

    # ── summary ───────────────────────────────────────────────────────────────
    def _track_summary_items(self, lines: list):
        """Update _inv_items from a batch of log lines.

        For each 'collected!' line, look back in the same batch for an
        '{item} xN added to inventory' line to get the speed-bonus quantity,
        falling back to 1 for a normal (no-bonus) collection.  Bonus items
        are parsed from the 'Also found X xN' tail of the collected line.
        """
        for i, line in enumerate(lines):
            name = parse_collect_line(line)
            if not name:
                continue
            clean = clean_name(name)
            # Look back up to 10 lines for a quantity-specified inventory-add
            # for the primary item (speed-bonus case: "Item xN added to inventory.")
            primary_qty = 1
            for prev in lines[max(0, i - 10):i]:
                im = _INV_ADD_RE.search(prev)
                if im and im.group(1).strip().lower() == clean.lower():
                    primary_qty = int(im.group(2))
                    break
            self._inv_items[clean] = self._inv_items.get(clean, 0) + primary_qty
            # Bonus items from "Also found X [xN] (speed bonus!)"
            # Count is optional — missing means x1
            for bm in _BONUS_RE.finditer(line):
                bn = bm.group(1).strip()
                bq = int(bm.group(2)) if bm.group(2) else 1
                self._inv_items[bn] = self._inv_items.get(bn, 0) + bq

    def _build_summary_data(self) -> dict:
        state = self.state
        start = self._survey_start_time
        end   = self._survey_end_time or datetime.datetime.now()

        start_str    = start.strftime('%Y-%m-%d %H:%M:%S') if start else 'N/A'
        end_str      = end.strftime('%Y-%m-%d %H:%M:%S')   if end   else 'N/A'

        if start and end:
            total_secs = int((end - start).total_seconds())
            h, rem     = divmod(total_secs, 3600)
            m, s       = divmod(rem, 60)
            duration_str = f'{h}h {m}m {s}s' if h else f'{m}m {s}s'
        else:
            duration_str = 'N/A'

        times = self._collection_timestamps
        if len(times) >= 2:
            deltas  = [(times[i+1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            avg_sec = sum(deltas) / len(deltas)
            avg_m, avg_s = divmod(int(avg_sec), 60)
            avg_time_str = f'{avg_m}m {avg_s}s' if avg_m else f'{avg_s}s'
        else:
            avg_time_str = 'N/A'

        # Use live inventory-add tracking for accurate counts (primary + bonus quantities).
        # Fall back to state.items if no inv-add lines were captured (e.g. log not running).
        if self._inv_items:
            name_counts = Counter(self._inv_items)
        else:
            name_counts = Counter(clean_name(i['name']) for i in state.items if i['collected'])
        total_items = sum(name_counts.values())
        items = sorted(
            [(name, count, count / total_items * 100 if total_items else 0.0)
             for name, count in name_counts.items()],
            key=lambda x: (-x[1], x[0])
        )

        maps_completed = sum(1 for i in state.items if i['collected'])

        return {
            'maps_completed': maps_completed,
            'start_str':      start_str,
            'end_str':        end_str,
            'duration_str':   duration_str,
            'avg_time_str':   avg_time_str,
            'xp':             dict(self._xp_gained),
            'items':          items,
        }

    def show_summary(self):
        data = self._summary_data or self._build_summary_data()
        win  = SummaryWindow(data, self.control)
        win.exec_()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _set_log(self, msg: str):
        self.control.set_log(msg)

    def _refresh_all(self):
        self.map_overlay.refresh()
        self.inv_overlay.refresh()
        self.control.refresh()
        self.save_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def _apply_grid_config():
    """Load GRID_COLS from settings before overlays are created."""
    global GRID_COLS
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text())
            g = data.get('grid', {})
            if 'cols' in g:
                GRID_COLS = max(1, int(g['cols']))
    except Exception:
        pass


def _macos_activate():
    """Make the process behave like a regular app on macOS (show Dock icon, receive focus)."""
    if sys.platform != 'darwin':
        return
    try:
        from AppKit import NSApplication  # PyObjC — available in most conda/system Pythons
        ns_app = NSApplication.sharedApplication()
        ns_app.setActivationPolicy_(0)       # NSApplicationActivationPolicyRegular
        ns_app.activateIgnoringOtherApps_(True)
    except ImportError:
        pass  # PyObjC not installed — skip silently
    except Exception:
        pass


_MACOS_OVERLAY_LEVEL = 25   # NSStatusWindowLevel — above normal & floating windows
_MACOS_JOIN_ALL      = 1    # NSWindowCollectionBehaviorCanJoinAllSpaces


def _macos_raise_overlay(widget):
    """Raise the overlay above normal windows and pin it to all Spaces (macOS only)."""
    if sys.platform != 'darwin':
        return
    try:
        from AppKit import NSView
        ns_view   = NSView(int(widget.winId()))
        ns_window = ns_view.window()
        if ns_window:
            ns_window.setLevel_(_MACOS_OVERLAY_LEVEL)
            ns_window.setCollectionBehavior_(_MACOS_JOIN_ALL)
    except ImportError:
        pass
    except Exception:
        pass


def main():
    # Enable high-DPI scaling before QApplication is created so Qt handles
    # logical-to-physical pixel mapping on 4K / HiDPI monitors automatically.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    _macos_activate()   # must be called after QApplication initialises Cocoa
    app.setStyle('Fusion')

    # Dark palette
    from PyQt5.QtGui import QPalette
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(14,  14,  30))
    palette.setColor(QPalette.WindowText,      QColor(200, 220, 240))
    palette.setColor(QPalette.Base,            QColor(10,  10,  20))
    palette.setColor(QPalette.AlternateBase,   QColor(20,  20,  35))
    palette.setColor(QPalette.ToolTipBase,     QColor(0,   0,   0))
    palette.setColor(QPalette.ToolTipText,     QColor(200, 220, 240))
    palette.setColor(QPalette.Text,            QColor(200, 220, 240))
    palette.setColor(QPalette.Button,          QColor(30,  30,  50))
    palette.setColor(QPalette.ButtonText,      QColor(200, 220, 240))
    palette.setColor(QPalette.BrightText,      QColor(255, 50,  50))
    palette.setColor(QPalette.Link,            QColor(80, 160, 255))
    palette.setColor(QPalette.Highlight,       QColor(40,  80, 160))
    palette.setColor(QPalette.HighlightedText, QColor(230, 240, 255))
    app.setPalette(palette)

    _apply_grid_config()
    survey = SurveyApp()   # noqa — keeps windows alive
    app.aboutToQuit.connect(survey._stop_kb_listener)
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
