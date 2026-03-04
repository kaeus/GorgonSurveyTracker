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
  • All positions/settings saved to JSON in the same folder as this script
"""

import sys
import os
import re
import json
import math
import time
import ctypes
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QSlider, QSpinBox,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFrame,
    QFileDialog, QMessageBox, QSizeGrip,
)
from PyQt5.QtCore  import Qt, QTimer, QPoint, QSize, pyqtSignal
from PyQt5.QtGui   import (
    QPainter, QColor, QPen, QBrush, QFont, QCursor,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
GRID_COLS   = 10
GRID_ROWS   = 8
SLOT_SIZE   = 50          # px
SLOT_GAP    = 2           # px
HEADER_H    = 28          # px — header height for both overlays

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
# Log-parsing helpers
# ─────────────────────────────────────────────────────────────────────────────
_DIST_RE         = re.compile(r'(\d+(?:\.\d+)?)m\s+(west|east|north|south)', re.IGNORECASE)
_COLLECT_RE      = re.compile(r'\[Status\]\s+(.+?)\s+collected!')
_SURVEY_CHAT_RE  = re.compile(r'\[Status\]\s+The\s+(.+?)\s+is\s+(.+)', re.IGNORECASE)


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


def clean_name(name: str) -> str:
    return re.sub(r'\s+(is here|found)[.!]?\s*$', '', name, flags=re.IGNORECASE).strip()


def pt_dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


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

    def player_to_pixel(self, offset, canvas_w, canvas_h):
        if not self.player_pos or not self.scale:
            return None
        px = self.player_pos[0] + offset['east']  *  self.scale
        py = self.player_pos[1] - offset['north'] *  self.scale  # north = up = –y
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
        self._show_labels   = True

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        if sys.platform == 'darwin':   # Qt.Tool → NSPanel which auto-hides on deactivation
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(200, 120)
        self.resize(460, 460)

        # Resize grip
        self._grip = ResizeGrip(self)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)

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

        # lock / pass-through indicator
        # closed = overlay is interactive (capturing clicks); open = clicks pass through
        _draw_lock_icon(p, w - 14, HEADER_H // 2, not self._click_through)

        # ── canvas background ──
        cy = HEADER_H
        p.fillRect(0, cy, w, h - cy, QColor(10, 10, 20, int(self._bg_alpha * 255)))

        # border
        p.setPen(QPen(QColor(100, 170, 255, 180), 1.5))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 5, 5)

        # ── route lines ──
        if self.state.phase == 'routing' and len(self.state.route_order) >= 1:
            pen = QPen(QColor(255, 210, 50, 210), 2.5, Qt.DashLine)
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
            p.drawEllipse(int(px) - 7, int(py_s) - 7, 14, 14)

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

        # label
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._grip.move(self.width() - ResizeGrip.SIZE, self.height() - ResizeGrip.SIZE)

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
        self.update()


# ─────────────────────────────────────────────────────────────────────────────
# Inventory slot widget
# ─────────────────────────────────────────────────────────────────────────────
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

        uncollected = self.state.uncollected()
        active_id   = self.state.active_id

        sc    = self.state.survey_count
        total = max(sc, len(uncollected)) if sc > 0 else len(uncollected)
        if total == 0:
            total = GRID_COLS   # show a placeholder row before any items are found

        # Compute slot width so 10 columns fill the full overlay width evenly
        slot_w = max(28, (self.width() - 12 - SLOT_GAP * (GRID_COLS - 1)) // GRID_COLS)

        for i in range(total):
            item = uncollected[i] if i < len(uncollected) else None
            slot = SlotWidget(item, self._grid_container)
            slot.setFixedSize(slot_w, slot_w)
            if item and item['id'] == active_id:
                slot.setProperty('active_route', True)
            slot.clicked.connect(self.app.on_inventory_click)
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
        p.drawText(8, 0, w - 30, HEADER_H, Qt.AlignVCenter, 'Survey Inventory')

        # lock / inventory-locked indicator
        _draw_lock_icon(p, w - 14, HEADER_H // 2, self.app._inv_locked)

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

    def _on_drag_finished(self):
        self.app.save_settings()

    def refresh(self):
        self._rebuild_grid()
        self.update()


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
        self._build_ui()
        self.refresh()

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

    def _label(self, text, color='#778'):
        lb = QLabel(text)
        lb.setStyleSheet(f'color:{color}; font-size:11px;')
        return lb

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
        self.btn_overlays = self._small_btn('Overlays: ON', self.app.toggle_overlays, '#1a3a1a')
        row_title.addWidget(self.btn_overlays)
        main.addLayout(row_title)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color:#334;')
        main.addWidget(sep)

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

        # ── Survey controls ───────────────────────────────────────────────
        row3 = QHBoxLayout()
        row3.addWidget(self._label('Surveys:'))
        self.sb_count = QSpinBox()
        self.sb_count.setRange(0, 999)
        self.sb_count.setValue(0)
        self.sb_count.setSpecialValueText('0')   # 0 displays as "—" meaning "auto"
        self.sb_count.setToolTip('How many survey maps you have (0 = auto grow with matches surveys)')
        self.sb_count.setMaximumWidth(60)
        self.sb_count.setStyleSheet(
            'QSpinBox { background:#1a1a2e; color:#cde; border:1px solid #446; '
            'padding:2px 4px; border-radius:4px; font-size:12px; }'
            'QSpinBox::up-button, QSpinBox::down-button { width:14px; }'
        )
        self.sb_count.valueChanged.connect(self.app.on_survey_count_changed)
        row3.addWidget(self.sb_count)
        self.btn_set_pos = self._btn('📍 Set My Position', self.app.enter_set_player, '#1a4a2a')
        self.btn_start   = self._btn('▶ Start Survey',     self.app.start_surveying,  '#1a3a5a')
        self.btn_done    = self._btn('🗺 Optimize Route',   self.app.done_surveying,   '#5a4a00')
        self.btn_next    = self._btn('→ Skip to Next',      self.app.advance_route,    '#1a3a5a')
        self.btn_reset   = self._btn('🗑 Reset',            self.app.reset_survey,     '#5a1a1a')
        for b in (self.btn_set_pos, self.btn_start, self.btn_done, self.btn_next, self.btn_reset):
            row3.addWidget(b)
        row3.addStretch()
        sec.addLayout(row3)

        # ── Opacity (stacked) + toggle buttons ───────────────────────────
        slider_col = QVBoxLayout()
        slider_col.setSpacing(3)

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

        row4 = QHBoxLayout()
        row4.addLayout(slider_col)
        row4.addSpacing(10)
        self.btn_click_through = self._small_btn('Map Pass-Thru: OFF',
                                                 self.app.toggle_map_click_through, '#3a2a0a')
        row4.addWidget(self.btn_click_through)
        row4.addSpacing(4)
        self.btn_inv_lock = self._small_btn('Inv: Unlocked',
                                            self.app.toggle_inv_lock, '#1a3a1a')
        row4.addWidget(self.btn_inv_lock)
        row4.addSpacing(4)
        self.btn_labels = self._small_btn('Labels: ON',
                                          self.app.toggle_map_labels, '#1a2a3a')
        row4.addWidget(self.btn_labels)
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

        has_items    = bool(state.items)
        has_pos      = state.player_pos is not None
        has_chat_dir = getattr(self.app, '_chat_dir', None) is not None
        placed       = any(i.get('pixel_pos') for i in state.uncollected())

        was_visible = self._survey_section.isVisible()
        self._survey_section.setVisible(has_chat_dir)
        if has_chat_dir != was_visible:
            self.adjustSize()

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
        self.btn_reset.setVisible(has_items or state.phase != 'idle')

        vis = getattr(self.app, '_overlays_visible', True)
        label = 'ON' if vis else 'OFF'
        color = '#1a3a1a' if vis else '#5a1a1a'
        self.btn_overlays.setText(f'Overlays: {label}')
        self.btn_overlays.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────────────────────────────────────
class SurveyApp:
    def __init__(self):
        self.state = SurveyState()

        self.map_overlay  = MapOverlay(self.state, self)
        self.inv_overlay  = InventoryOverlay(self.state, self)
        self.control      = ControlPanel(self)

        self.map_overlay.canvas_clicked.connect(self._on_map_canvas_click)

        self._chat_dir         = None
        self._chat_file        = None
        self._chat_offset      = 0
        self._collect_last     = {}   # name_low → time.monotonic() of last collection
        self._click_through    = False
        self._inv_locked       = False
        self._overlays_visible = True

        # Polling timer (0.5 s)
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._timer.start(500)

        # Blink timer for pending dot
        self._blink_timer = QTimer()
        self._blink_timer.timeout.connect(lambda: self.map_overlay.refresh())
        self._blink_timer.start(600)

        self._load_settings()
        self.control.refresh()  # update section visibility after settings are loaded

        self.map_overlay.show()
        self.inv_overlay.show()
        self.control.show()
        _macos_raise_overlay(self.map_overlay)
        _macos_raise_overlay(self.inv_overlay)

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
        self._refresh_all()
        self._set_log('Survey reset. Set your position and start a new survey.')

    # ── map canvas click ──────────────────────────────────────────────────────
    def _on_map_canvas_click(self, cx: float, cy: float):
        state = self.state

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
                new_px = state.player_to_pixel(offset, cw, ch)
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
            px = state.player_to_pixel(offset, cw, ch)
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

        print(f'[collect] "{collected_name}" | phase={state.phase} | route_idx={state.route_idx}')

        # Deduplicate: ignore repeated collect events for the same name within 3 s.
        # The game occasionally emits duplicate "X collected!" messages, which would
        # otherwise consume two consecutive same-named route targets from one pickup.
        now = time.monotonic()
        if now - self._collect_last.get(name_low, 0) < 3.0:
            print(f'[collect]   DEDUP — ignored (same name within 3 s)')
            return
        self._collect_last[name_low] = now

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
                for line in new_text.splitlines():
                    result = parse_chat_survey_line(line)
                    if result:
                        self._on_survey_item(*result)
                    name = parse_collect_line(line)
                    if name:
                        self._on_item_collected(name)
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

    # ── overlay visibility ────────────────────────────────────────────────────
    def toggle_overlays(self):
        self._overlays_visible = not self._overlays_visible
        if self._overlays_visible:
            self.map_overlay.show()
            self.inv_overlay.show()
            _macos_raise_overlay(self.map_overlay)
            _macos_raise_overlay(self.inv_overlay)
        else:
            self.map_overlay.hide()
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

    def on_survey_count_changed(self, value: int):
        self.state.survey_count = value
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
        self.map_overlay._show_labels = not self.map_overlay._show_labels
        label = 'ON' if self.map_overlay._show_labels else 'OFF'
        color = '#1a2a3a' if self.map_overlay._show_labels else '#2a2a2a'
        self.control.btn_labels.setText(f'Labels: {label}')
        self.control.btn_labels.setStyleSheet(
            f'QPushButton {{ background:{color}; color:#cde; border:1px solid #446; '
            f'padding:2px 6px; border-radius:3px; font-size:10px; font-weight:600; }}'
            f'QPushButton:hover {{ border-color: #8ab; }}'
        )
        self.map_overlay.refresh()
        self.save_settings()

    def save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            mg = self.map_overlay.geometry()
            ig = self.inv_overlay.geometry()
            data = {
                'map':  {
                    'x': mg.x(), 'y': mg.y(), 'w': mg.width(), 'h': mg.height(),
                    'opacity': int(self.map_overlay._bg_alpha * 100),
                },
                'inv':  {
                    'x': ig.x(), 'y': ig.y(), 'w': ig.width(), 'h': ig.height(),
                    'opacity': int(self.inv_overlay._bg_alpha * 100),
                },
                'chat_dir':     self._chat_dir,
                'survey_count': self.state.survey_count,
                'map_labels':   self.map_overlay._show_labels,
                'inv_locked':   self._inv_locked,
                'map_click_through': self._click_through,
                'overlays_visible': self._overlays_visible,
                'grid': {
                    'cols':      GRID_COLS,
                    'slot_size': SLOT_SIZE,
                    'slot_gap':  SLOT_GAP,
                },
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

            # Sync slider / spinbox values (block signals to avoid triggering save_settings
            # before log_path / chat_dir have been restored)
            for sl, val in (
                (self.control.sl_map_opacity, int(self.map_overlay._bg_alpha * 100)),
                (self.control.sl_inv_opacity, int(self.inv_overlay._bg_alpha * 100)),
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

            if 'map_labels' in data:
                self.map_overlay._show_labels = bool(data['map_labels'])
                label = 'ON' if self.map_overlay._show_labels else 'OFF'
                self.control.btn_labels.setText(f'Labels: {label}')

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

            if 'overlays_visible' in data:
                self._overlays_visible = bool(data['overlays_visible'])
                if not self._overlays_visible:
                    self.map_overlay.hide()
                    self.inv_overlay.hide()

            ss = data.get('survey_state')
            if ss and (ss.get('items') or ss.get('phase', 'idle') != 'idle'):
                st = self.state
                st._next_id    = ss.get('next_id', 0)
                st.scale       = ss.get('scale')
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

        except Exception:
            pass

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
    """Load GRID_COLS / SLOT_SIZE / SLOT_GAP from settings before overlays are created."""
    global GRID_COLS, SLOT_SIZE, SLOT_GAP
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text())
            g = data.get('grid', {})
            if 'cols'      in g: GRID_COLS = max(1,  int(g['cols']))
            if 'slot_size' in g: SLOT_SIZE  = max(16, int(g['slot_size']))
            if 'slot_gap'  in g: SLOT_GAP   = max(0,  int(g['slot_gap']))
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
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
