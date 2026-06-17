# -*- coding: utf-8 -*-
"""Interactive node-graph canvas. UI thread only. Requires PySide2.

Each view shows one level of children inside the current scope (a marker
instance). Double-click a drillable node to enter it; the breadcrumb
navigates back out."""

import collections

from PySide2 import QtCore, QtGui, QtWidgets

from . import config as _config
from . import graph_layout
from .graph_model import (
    READ, WRITE,
    CAT_GRAPHICS, CAT_COMPUTE, CAT_TRANSFER, CAT_PRESENT, CAT_SCOPE, CAT_PORTAL,
    RES_COLOR, RES_DEPTH, RES_UAV_TEX, RES_SWAPCHAIN, RES_BUFFER, RES_SAMPLED,
)
from .i18n import tr

def _mix(a, b, t):
    """Linear blend a->b by t."""
    return QtGui.QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t))


# Category base colours. Pass kinds get distinct hues (telling them apart
# matters); resource kinds collapse into one blue-grey family except the
# warm accents marking the frame's key outputs. Dark themes lighten the
# bases. Loudness hierarchy: graphics > compute > resources.
_CAT_BASE = {
    CAT_GRAPHICS: '#E53935',   # vivid red - loudest on screen
    CAT_COMPUTE: '#7B1FA2',    # deep purple, one step quieter
    CAT_SCOPE: '#1976D2',      # vivid blue - drillable aggregate scopes
    CAT_TRANSFER: '#78909C',   # blue grey, neutral helper
    CAT_PRESENT: '#2E7D32',    # green - clear against blue/purple
    CAT_PORTAL: '#AEC0C8',     # light grey; lighter than depth so the two
                             # greys read apart under the 45% pass title tint
    RES_COLOR: '#C0773B',      # ochre - warm but muted
    RES_DEPTH: '#2E3C44',      # dark slate; deep enough to stay darker than
                             # portal grey under the 28% res title tint
    RES_UAV_TEX: '#78909C',    # blue-grey, middle step
    RES_SWAPCHAIN: '#F9A825',  # bright gold - frame's final output, one/frame
    RES_BUFFER: '#5C6BC0',     # indigo - non-grey so buffers don't blur into
                             # depth / portal
    RES_SAMPLED: '#B0BEC5',    # blue-grey, lightest - read-only assets
}

# read/write edge hues: teal vs rose, maximally separable inside this node
# palette (no pink in the fills, so rose write never collides). Light mode
# shows these verbatim; dark theme lightens both by 135%.
_EDGE_BASE = {
    READ: '#00897B',
    WRITE: '#d74e80',
}


class Theme(object):
    """Canvas colours derive from the application palette so the extension
    follows RenderDoc's light/dark theme."""

    def __init__(self, palette):
        base = palette.color(QtGui.QPalette.Base)
        window = palette.color(QtGui.QPalette.Window)
        text = palette.color(QtGui.QPalette.WindowText)
        self.dark = window.lightnessF() < 0.5
        # canvas = panel (Window), nodes = content (Base); pushed away from
        # the node face so nodes pop at any zoom
        self.canvas = (window.lighter(118) if self.dark
                       else window.darker(120))
        self.node_bg = base
        self.shadow = QtGui.QColor(0, 0, 0, 150 if self.dark else 105)
        self.node_border = palette.color(QtGui.QPalette.Mid)
        self.text = text
        self.sub_text = _mix(text, base, 0.42)
        self.highlight = palette.color(QtGui.QPalette.Highlight)

        def cat(name):
            col = QtGui.QColor(_CAT_BASE[name])
            return col.lighter(130) if self.dark else col

        self.pass_colors = dict(
            (k, cat(k))
            for k in (CAT_GRAPHICS, CAT_COMPUTE, CAT_SCOPE, CAT_TRANSFER,
                      CAT_PRESENT, CAT_PORTAL))
        self.res_bar_colors = dict(
            (k, cat(k))
            for k in (RES_COLOR, RES_DEPTH, RES_UAV_TEX, RES_SWAPCHAIN, RES_BUFFER,
                      RES_SAMPLED))

        # near-opaque; the slight translucency makes parallel bundles read
        # as a density gradient instead of a solid blob
        self.read = QtGui.QColor(_EDGE_BASE[READ])
        self.write = QtGui.QColor(_EDGE_BASE[WRITE])
        if self.dark:
            self.read = self.read.lighter(135)
            self.write = self.write.lighter(135)
        self.read.setAlpha(245)
        self.write.setAlpha(245)
        # edges outside a selected node's subgraph go neutral grey; keeping
        # the dimmed hue made crossing lines cluttered
        self.edge_muted = _mix(self.sub_text, self.canvas, 0.58)

    def title_tint(self, cat, strong=True):
        """Category wash for the node title row (Event Browser marker-row
        language). Passes 45% colour, resources 28%."""
        t = 0.55 if strong else 0.72
        return _mix(cat, self.node_bg, t)

    def cat_border(self, cat, strong=True):
        """Pass borders draw full category colour; resource borders blend
        halfway into the neutral border."""
        return cat if strong else _mix(cat, self.node_border, 0.5)


# below ~0.3 white node faces vanish into the light canvas while edges stay
# visible (lines look severed); keep unrelated nodes legible
DIM_OPACITY = 0.4
FILTER_DIM_OPACITY = 0.35
# muted context edges stay more present than dimmed nodes so grey lines
# remain traceable
MUTED_EDGE_OPACITY = 0.9
# selection-focus edge tiers: direct in/out bold and on top; indirect keep
# their hue but faded; unrelated muted grey at the bottom
EDGE_W_BASE = 1.6
EDGE_W_DIRECT = 2.8
EDGE_W_INDIRECT = 1.5
EDGE_INDIRECT_OPACITY = 0.55
# z-order low -> high: muted edge < base node < indirect edge
#                      < direct edge < highlighted node
Z_EDGE_BASE = -1.0
Z_NODE_BASE = 0.0
Z_EDGE_INDIRECT = 1.0
Z_EDGE_DIRECT = 2.0
Z_NODE_HI = 3.0
# 'flat' | 'folder' remain for style probes
SCOPE_SHAPE = 'stack'
_SCOPE_STACK_OFF = (6.0, 3.0)
_SCOPE_TAB_W = 46.0
_SCOPE_TAB_H = 7.0
THUMB_W = 192
THUMB_H = 108
ICON_SZ = 14.0   # eye / range hit square
PAD = 8.0
TITLE_H = 20.0
BUNDLE_ROW_H = 15.0   # one row per bundled member name
BUNDLE_MAX_ROWS = 24  # beyond this an overflow row summarises the rest
PASS_BUNDLE_ROWS_Y = 42.0  # member rows start below the title + EID rows
CLICK_SLOP = 6.0    # px of drag before a press stops counting as a click
BADGE_W = 26.0      # version-badge box reserved on a resource title row
TOOLTIP_LIST_LIMIT = 20   # list rows shown in a tooltip before a '+N more' line
EDGE_TOOLTIP_EID_LIMIT = 10  # per-usage EIDs shown in an edge tooltip
_LAYOUT_CACHE_CAP = 48    # LRU layouts kept so revisiting a view stays free

_CRUMB_SEP = u' ▸ '
_CRUMB_LINK = '#7ab0e0'       # link blue for clickable scope segments


def _text_width(fm, text):
    if hasattr(fm, 'horizontalAdvance'):
        return fm.horizontalAdvance(text)
    return fm.width(text)


def _measure_member_rows(fm, members, w0):
    """Widen w0 to fit up to BUNDLE_MAX_ROWS member names and report the row
    count (plus one overflow row when truncated). Shared by the pass / resource
    bundle sizing so paint and _node_size stay in step."""
    shown = members[:BUNDLE_MAX_ROWS]
    w = w0
    for nm in shown:
        w = max(w, _text_width(fm, nm) + 4)
    rows = len(shown) + (1 if len(members) > BUNDLE_MAX_ROWS else 0)
    return max(w + 2 * PAD + 12, 170.0), rows


def _paint_member_rows(painter, fm, thm, members, w, y0):
    """Draw up to BUNDLE_MAX_ROWS elided member names down from y0, then a
    '+N more' overflow row. Shared by the pass-bundle and resource-bundle
    paint paths."""
    y = y0
    painter.setPen(thm.text)
    for nm in members[:BUNDLE_MAX_ROWS]:
        row = fm.elidedText(nm, QtCore.Qt.ElideMiddle, int(w - 2 * PAD - 6))
        painter.drawText(
            QtCore.QRectF(PAD + 6, y, w - 2 * PAD - 6, BUNDLE_ROW_H),
            QtCore.Qt.AlignVCenter, row)
        y += BUNDLE_ROW_H
    if len(members) > BUNDLE_MAX_ROWS:
        painter.setPen(thm.sub_text)
        painter.drawText(
            QtCore.QRectF(PAD + 6, y, w - 2 * PAD - 6, BUNDLE_ROW_H),
            QtCore.Qt.AlignVCenter,
            tr('… +%d more') % (len(members) - BUNDLE_MAX_ROWS))


def _smooth_path(points):
    """Cubic spline through points with horizontal tangents."""
    path = QtGui.QPainterPath(points[0])
    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]
        dx = max(40.0, abs(p2.x() - p1.x()) / 2.0)
        path.cubicTo(QtCore.QPointF(p1.x() + dx, p1.y()),
                     QtCore.QPointF(p2.x() - dx, p2.y()), p2)
    return path


def _eye_rect_at(w):
    """Tri-state eye icon square on the title row's right edge."""
    return QtCore.QRectF(w - ICON_SZ - 6.0, (TITLE_H - ICON_SZ) / 2.0,
                         ICON_SZ, ICON_SZ)


def _badge_rect_at(w, has_eye):
    """Write-version (#n) badge box on the title row, right-aligned; sits
    left of the eye icon when present."""
    right = w - PAD
    if has_eye:
        right = _eye_rect_at(w).left() - 4.0
    return QtCore.QRectF(right - BADGE_W, 0, BADGE_W, TITLE_H)


def _version_badge(node):
    """'#n' write-version badge text for a resource node, or '' for none.
    A single-write resource has no badge (only earns space when sibling
    versions need disambiguating)."""
    count = getattr(node, 'version_count', getattr(node, 'version', 1))
    if count >= 2:
        return '#%d' % getattr(node, 'version', 1)
    return ''


def _pass_title(node):
    """Display title for a pass-side node: just its name."""
    return node.name


class NodeItem(QtWidgets.QGraphicsItem):
    def __init__(self, panel, node, is_pass, w, h):
        super(NodeItem, self).__init__()
        self.panel = panel
        self.node = node
        self.is_pass = is_pass
        self.w = float(w)
        self.h = float(h)
        self.pixmap = None
        self.thumb_state = 'idle'   # 'idle' | 'loading' | 'failed'
        self.selected_style = False
        self._press_scene = None
        self.setToolTip(panel.tooltip_for(node))
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsScenePositionChanges, True)

    def boundingRect(self):
        # scope decorations (stack plates / folder tab) overhang top/right;
        # extents are constant so no prepareGeometryChange needed
        top = right = 0.0
        if self.is_pass and getattr(self.node, 'drillable', False):
            if SCOPE_SHAPE == 'stack':
                top = right = _SCOPE_STACK_OFF[0] + 1.0
            elif SCOPE_SHAPE == 'folder':
                top = _SCOPE_TAB_H + 1.0
        return QtCore.QRectF(-1, -1 - top, self.w + 2 + right,
                             self.h + 2 + top)

    def set_pixmap(self, pm):
        self.pixmap = pm
        self.update()

    def set_thumb_state(self, state):
        # 'idle' | 'loading' | 'failed'
        if self.thumb_state != state:
            self.thumb_state = state
            self.update()

    def _paint_thumb_placeholder(self, painter, thm, y):
        w = min(THUMB_W, self.w - 2 * PAD)
        rect = QtCore.QRectF((self.w - w) / 2.0, y, w, THUMB_H)
        painter.save()
        pen = QtGui.QPen(thm.sub_text, 1.0, QtCore.Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(rect, 2.0, 2.0)
        msg = tr('Loading…') if self.thumb_state == 'loading' \
            else tr('No preview')
        painter.drawText(rect, QtCore.Qt.AlignCenter, msg)
        painter.restore()

    def _has_eye(self):
        # bundle nodes have no single content to preview
        return (not self.is_pass and
                getattr(self.node, 'res_kind', '') != RES_BUFFER and
                not getattr(self.node, 'bundle_members', None))

    def _eye_rect(self):
        return _eye_rect_at(self.w)

    def _icon_hit(self, pos):
        if not self._has_eye():
            return None
        if self._eye_rect().contains(pos):
            return 'eye'
        return None

    def set_selected_style(self, on):
        if self.selected_style != on:
            self.selected_style = on
            self.update()

    def anchor_out(self, frac):
        return self.scenePos() + QtCore.QPointF(self.w, self.h * frac)

    def anchor_in(self, frac):
        return self.scenePos() + QtCore.QPointF(0.0, self.h * frac)

    def paint(self, painter, option, widget=None):
        thm = self.panel.theme
        rect = QtCore.QRectF(0, 0, self.w, self.h)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 2.0, 2.0)

        is_scope = self.is_pass and getattr(self.node, 'drillable', False)
        if self.is_pass:
            # scope nodes own a colour: graphics/compute hue would echo the
            # "any draw wins" priority, not what the container is
            if is_scope and CAT_SCOPE in thm.pass_colors:
                bar = thm.pass_colors[CAT_SCOPE]
            else:
                bar = thm.pass_colors.get(self.node.kind,
                                          thm.pass_colors[CAT_GRAPHICS])
        else:
            bar = thm.res_bar_colors.get(self.node.res_kind,
                                         thm.res_bar_colors[RES_COLOR])

        if is_scope and SCOPE_SHAPE == 'stack':
            # offset back-plates: a deck of cards = "more inside"
            for off in _SCOPE_STACK_OFF:
                bp = QtGui.QPainterPath()
                bp.addRoundedRect(
                    QtCore.QRectF(off, -off, self.w, self.h), 2.0, 2.0)
                painter.fillPath(bp, thm.title_tint(bar, False))
                pen = QtGui.QPen(thm.cat_border(bar, False), 1.0)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawPath(bp)
        elif is_scope and SCOPE_SHAPE == 'folder':
            # folder tab above the title row = "open me"
            tab = QtGui.QPainterPath()
            tab.addRoundedRect(
                QtCore.QRectF(0, -_SCOPE_TAB_H,
                              min(_SCOPE_TAB_W, self.w * 0.5),
                              _SCOPE_TAB_H + 3.0), 2.0, 2.0)
            painter.fillPath(tab, thm.title_tint(bar, True))
            pen = QtGui.QPen(thm.cat_border(bar, True), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawPath(tab)

        painter.fillPath(path, thm.node_bg)
        # swapchain gets pass-level prominence as the frame's final output
        strong = (self.is_pass or
                  getattr(self.node, 'res_kind', '') == RES_SWAPCHAIN)
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(QtCore.QRectF(0, 0, self.w, TITLE_H),
                         thm.title_tint(bar, strong))
        if (not self.is_pass) and getattr(self.node, 'imported', False):
            # external content: hatch the whole info area below the title
            hatch = QtGui.QColor(thm.sub_text)
            hatch.setAlpha(70)
            painter.fillRect(
                QtCore.QRectF(0, TITLE_H, self.w, self.h - TITLE_H),
                QtGui.QBrush(hatch, QtCore.Qt.BDiagPattern))
        painter.restore()

        font = painter.font()
        if self.is_pass:
            painter.setPen(thm.text)
            font.setBold(True)
            painter.setFont(font)
            fm = QtGui.QFontMetrics(font)
            title = fm.elidedText(_pass_title(self.node),
                                  QtCore.Qt.ElideMiddle,
                                  int(self.w - 2 * PAD))
            painter.drawText(QtCore.QRectF(PAD, 0, self.w - 2 * PAD,
                                           TITLE_H),
                             QtCore.Qt.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(thm.sub_text)
            if self.node.kind == CAT_PORTAL:
                sub = tr('External scope EID %d-%d') % (
                    self.node.first_eid, self.node.last_eid)
            else:
                sub = 'EID %d-%d  (%d)' % (self.node.first_eid,
                                           self.node.last_eid,
                                           self.node.action_count)
            members = getattr(self.node, 'bundle_members', None)
            sub_h = 18.0 if members else self.h - TITLE_H
            painter.drawText(QtCore.QRectF(PAD, TITLE_H,
                                           self.w - 2 * PAD, sub_h),
                             QtCore.Qt.AlignVCenter, sub)
            if members:
                _paint_member_rows(painter, fm, thm, members, self.w,
                                   PASS_BUNDLE_ROWS_Y)
        elif getattr(self.node, 'bundle_members', None):
            fm = QtGui.QFontMetrics(font)
            painter.setPen(thm.text)
            font.setBold(True)
            painter.setFont(font)
            title = fm.elidedText(self.node.name, QtCore.Qt.ElideMiddle,
                                  int(self.w - 2 * PAD))
            painter.drawText(QtCore.QRectF(PAD, 0, self.w - 2 * PAD,
                                           TITLE_H),
                             QtCore.Qt.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            members = self.node.bundle_members
            _paint_member_rows(painter, fm, thm, members, self.w,
                               TITLE_H + 2.0)
        else:
            painter.setPen(thm.text)
            fm = QtGui.QFontMetrics(font)
            reserved = int(ICON_SZ + 6) if self._has_eye() else 0
            badge = _version_badge(self.node)
            if badge:
                reserved += int(BADGE_W)
            name_w = max(int(self.w - 2 * PAD - reserved), 10)
            label = fm.elidedText(self.node.name, QtCore.Qt.ElideMiddle,
                                  name_w)
            painter.drawText(QtCore.QRectF(PAD, 0, self.w - 2 * PAD,
                                           TITLE_H),
                             QtCore.Qt.AlignVCenter, label)
            if badge:
                painter.setPen(thm.sub_text)
                painter.drawText(
                    _badge_rect_at(self.w, self._has_eye()),
                    QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, badge)
            painter.setPen(thm.sub_text)
            info = self.node.info or {}
            sub = info.get('dims', '')
            fmt = info.get('format', '')
            if fmt and fmt != 'buffer':
                sub = '%s %s' % (sub, fmt) if sub else fmt
            if getattr(self.node, 'scope_input', False):
                sub = (sub + u'  ' + tr('[scope input]')) if sub \
                    else tr('[scope input]')
            elif self.node.imported:
                sub = (sub + u'  ' + tr('[external]')) if sub \
                    else tr('[external]')
            sub = fm.elidedText(sub, QtCore.Qt.ElideRight,
                                int(self.w - 2 * PAD))
            painter.drawText(QtCore.QRectF(PAD, TITLE_H,
                                           self.w - 2 * PAD, 18),
                             QtCore.Qt.AlignVCenter, sub)
            thumb_y = self.h - THUMB_H - 6
            if self.pixmap is not None:
                tx = (self.w - self.pixmap.width()) / 2.0
                painter.drawPixmap(QtCore.QPointF(tx, thumb_y), self.pixmap)
            elif self.thumb_state in ('loading', 'failed'):
                self._paint_thumb_placeholder(painter, thm, thumb_y)
            if self._has_eye():
                key = thumb_key(self.node)
                # eye tri-state by colour: collapsed = dim hollow pupil,
                # raw = bright filled, fitted = accent filled
                state = 0
                if key is not None and self.panel.is_expanded(key):
                    state = 2 if self.panel.autofit_of(key) else 1
                if state == 0:
                    col = thm.sub_text
                elif state == 1:
                    col = thm.text
                else:
                    col = thm.highlight
                er = self._eye_rect()
                painter.save()
                ep = QtGui.QPen(col, 1.3)
                ep.setCosmetic(True)
                painter.setPen(ep)
                painter.setBrush(QtCore.Qt.NoBrush)
                c = er.center()
                w2 = er.width() / 2.0
                h2 = er.height() / 3.2
                eye = QtGui.QPainterPath()
                eye.moveTo(c.x() - w2, c.y())
                eye.quadTo(c.x(), c.y() - h2 * 2, c.x() + w2, c.y())
                eye.quadTo(c.x(), c.y() + h2 * 2, c.x() - w2, c.y())
                painter.drawPath(eye)
                pr = 2.6
                if state == 0:
                    painter.setBrush(QtCore.Qt.NoBrush)
                    painter.setPen(QtGui.QPen(col, 1.1))
                else:
                    painter.setBrush(col)
                    painter.setPen(QtCore.Qt.NoPen)
                painter.drawEllipse(c, pr, pr)
                painter.restore()

        pen = QtGui.QPen(
            thm.highlight if self.selected_style
            else thm.cat_border(bar, strong),
            2.2 if self.selected_style else 1.4)
        # cosmetic: border stays 1 device pixel at any zoom so outlines
        # stay visible zoomed out
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawPath(path)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemScenePositionHasChanged:
            self.panel.on_node_moved(self.node.id)
        return super(NodeItem, self).itemChange(change, value)

    def mousePressEvent(self, event):
        if self._icon_hit(event.pos()) == 'eye':
            self.panel.on_eye_clicked(self.node)
            self._press_scene = None   # release must not also select/drag
            event.accept()
            return
        self._press_scene = event.scenePos()
        super(NodeItem, self).mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._press_scene is not None:
            delta = event.scenePos() - self._press_scene
            if delta.manhattanLength() <= CLICK_SLOP:
                self.panel.on_node_clicked(self.node)
        self._press_scene = None
        super(NodeItem, self).mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._icon_hit(event.pos()):
            event.accept()
            return
        members = getattr(self.node, 'bundle_members', None)
        if members:
            y0 = PASS_BUNDLE_ROWS_Y if self.is_pass else TITLE_H + 2.0
            idx = int((event.pos().y() - y0) // BUNDLE_ROW_H)
            if 0 <= idx < min(len(members), BUNDLE_MAX_ROWS):
                self.panel.on_bundle_member_double_clicked(self.node, idx)
                event.accept()
                return
        self.panel.on_node_double_clicked(self.node)
        event.accept()


class EdgeItem(QtWidgets.QGraphicsPathItem):
    def __init__(self, edge, src_item, dst_item):
        super(EdgeItem, self).__init__()
        self.edge = edge
        self.src_item = src_item
        self.dst_item = dst_item
        self.src_frac = 0.5
        self.dst_frac = 0.5
        thm = src_item.panel.theme
        color = thm.read if edge.kind == READ else thm.write
        self._base_color = QtGui.QColor(color)
        self._emphasis = 'normal'
        pen = QtGui.QPen(color, EDGE_W_BASE)
        # cosmetic: edges keep device-pixel width at any zoom
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setZValue(Z_EDGE_BASE)
        self._arrow = QtGui.QPolygonF()
        self.apply_style()

    def apply_style(self):
        """Dash the line when the unused-binding scan confirmed the
        shader never references this bound resource."""
        unused = (getattr(self.edge, 'unused_binding', False) and
                  self.src_item.panel.act_unused.isChecked())
        pen = self.pen()
        pen.setStyle(QtCore.Qt.DashLine if unused else QtCore.Qt.SolidLine)
        self.setPen(pen)
        tip = _edge_tooltip(self.edge)
        if unused:
            tip += tr('\n[bound but unused by shader - no data flows]')
        self.setToolTip(tip)

    def set_emphasis(self, mode):
        """Selection-focus tier: 'normal' | 'direct' (touches selection,
        bold, top z) | 'indirect' (in closure, faded) | 'muted' (outside
        closure, grey, bottom z)."""
        if self._emphasis == mode:
            return
        self._emphasis = mode
        thm = self.src_item.panel.theme
        pen = self.pen()
        if mode == 'direct':
            pen.setColor(self._base_color)
            pen.setWidthF(EDGE_W_DIRECT)
            self.setZValue(Z_EDGE_DIRECT)
        elif mode == 'indirect':
            pen.setColor(self._base_color)   # faded via opacity
            pen.setWidthF(EDGE_W_INDIRECT)
            self.setZValue(Z_EDGE_INDIRECT)
        elif mode == 'muted':
            pen.setColor(thm.edge_muted)
            pen.setWidthF(EDGE_W_BASE)
            self.setZValue(Z_EDGE_BASE)
        else:  # normal
            pen.setColor(self._base_color)
            pen.setWidthF(EDGE_W_BASE)
            self.setZValue(Z_EDGE_BASE)
        self.setPen(pen)
        self.update()

    def boundingRect(self):
        # include the arrowhead, which lies slightly off the path
        return super(EdgeItem, self).boundingRect().adjusted(-9, -9, 9, 9)

    def rebuild(self):
        p1 = self.src_item.anchor_out(self.src_frac)
        p2 = self.dst_item.anchor_in(self.dst_frac)
        self.setPath(_smooth_path([p1, p2]))
        a = 7.0
        self._arrow = QtGui.QPolygonF([
            p2,
            QtCore.QPointF(p2.x() - a, p2.y() - a * 0.55),
            QtCore.QPointF(p2.x() - a, p2.y() + a * 0.55),
        ])

    def sort_y_out(self):
        return self.dst_item.scenePos().y() + self.dst_item.h / 2.0

    def sort_y_in(self):
        return self.src_item.scenePos().y() + self.src_item.h / 2.0

    def paint(self, painter, option, widget=None):
        super(EdgeItem, self).paint(painter, option, widget)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(self.pen().color())
        painter.drawPolygon(self._arrow)


def _edge_tooltip(edge):
    names = {}
    for eid, uname in edge.usages:
        names.setdefault(uname, []).append(eid)
    lines = ['[%s]' % edge.kind]
    for uname in sorted(names):
        eids = names[uname]
        shown = ', '.join(str(e) for e in eids[:EDGE_TOOLTIP_EID_LIMIT])
        if len(eids) > EDGE_TOOLTIP_EID_LIMIT:
            shown += ', ...(%d total)' % len(eids)
        lines.append('%s @ EID %s' % (uname, shown))
    return '\n'.join(lines)


class GraphView(QtWidgets.QGraphicsView):
    def __init__(self, panel, scene):
        super(GraphView, self).__init__(scene)
        self.panel = panel
        self._zoom = 1.0
        self.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # left button = select / move nodes; right button pans manually
        # (no ScrollHandDrag) so panning never resets the selection
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        self._panning = False
        self._pan_last = None
        self._lmb_blank = False
        self._lmb_press = None
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(panel.theme.canvas)

    def reset_zoom(self):
        self._zoom = 1.0
        self.resetTransform()

    def apply_fit(self, rect):
        self.reset_zoom()
        if rect.isEmpty():
            return
        vp = self.viewport().rect()
        if vp.width() < 10 or vp.height() < 10:
            return
        sx = vp.width() / max(1.0, rect.width() + 80.0)
        sy = vp.height() / max(1.0, rect.height() + 80.0)
        s = min(1.0, sx, sy)
        s = max(0.1, s)
        self._zoom = s
        self.scale(s, s)
        self.centerOn(rect.center())

    def wheelEvent(self, event):
        if event.modifiers() & QtCore.Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
            nz = self._zoom * factor
            if 0.1 <= nz <= 4.0:
                self._zoom = nz
                self.scale(factor, factor)
            event.accept()
            return
        super(GraphView, self).wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.BackButton:
            self.panel.callbacks['back']()
            event.accept()
            return
        if event.button() == QtCore.Qt.RightButton:
            self._panning = True
            self._pan_last = event.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
            return
        self._lmb_blank = (event.button() == QtCore.Qt.LeftButton and
                           self.itemAt(event.pos()) is None)
        self._lmb_press = event.pos()
        super(GraphView, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_last is not None:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            h = self.horizontalScrollBar()
            v = self.verticalScrollBar()
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
            event.accept()
            return
        super(GraphView, self).mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.RightButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.unsetCursor()
            event.accept()
            return
        # a left click (not drag) on empty canvas clears the selection
        if self._lmb_blank and event.button() == QtCore.Qt.LeftButton:
            self._lmb_blank = False
            moved = CLICK_SLOP + 1.0
            if self._lmb_press is not None:
                moved = (event.pos() - self._lmb_press).manhattanLength()
            if moved <= CLICK_SLOP:
                self.panel.on_background_clicked()
        super(GraphView, self).mouseReleaseEvent(event)


def thumb_key(node):
    """Stable thumbnail cache key (res_key, eid). Node ids are re-assigned
    per scope, so caching by id would paste stale pixmaps after navigation;
    this pair survives scope switches."""
    eid = node.last_write_eid
    if eid is None:
        eid = node.first_read_eid
    if eid is None:
        return None
    return (node.res_key, eid)


class GraphPanel(QtWidgets.QWidget):
    """Whole window content: breadcrumb + toolbar + config band + canvas.
    Pure UI; talks back through the callbacks dict."""

    def __init__(self, callbacks, parent=None):
        super(GraphPanel, self).__init__(parent)
        self.callbacks = callbacks
        # theme switching needs a RenderDoc restart anyway, so sample once
        self.theme = Theme(self.palette())
        self.graph = None
        self.node_items = {}
        self.edge_items = []
        self._incident = {}
        self._building = False
        self.selected_id = None
        self.thumb_paths = {}   # thumb_key -> jpg path (stable across scopes)
        self._thumb_nodes = {}  # thumb_key -> node id in the current graph
        # {thumb_key: autofit_bool}; membership means "expanded". Survives
        # scope changes; cleared only on capture switch.
        self.expanded = {}
        self._layout_cache = collections.OrderedDict()  # input -> result
        # layout warnings for the CURRENT view only; recomputed each set_graph
        # so revisiting a view (back, display toggles) never inflates the count
        self.layout_warnings = []
        self.hidden_counts = {'orphans': 0, 'external': 0, 'internal': 0}
        self._episode_totals = {}
        self._match_text = ''   # search state: Enter cycles the matches
        self._match_idx = -1

        # persisted config: every toggle mirrors a self.cfg key, saves on change
        self.cfg = _config.load()
        self._cfg_boxes = {}        # cfg key -> QCheckBox
        self._applied_candidates = None  # candidate set of the live bundle
        # display filters batch too: the rendered graph follows this
        # snapshot, the checkboxes hold pending edits
        self._applied_display = _config.display_of(self.cfg)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)

        # chrome pins a CJK-capable UI font (qrenderdoc's Segoe UI default
        # has no Chinese glyphs); canvas keeps the application font so node
        # metrics stay untouched
        self._chrome_font = QtGui.QFont('Microsoft YaHei UI', 9)

        topbar = QtWidgets.QWidget()
        bar = QtWidgets.QHBoxLayout(topbar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.refresh_btn = QtWidgets.QPushButton(tr('Refresh'))
        self.back_btn = QtWidgets.QPushButton(tr('⬅ Back'))
        self.back_btn.setEnabled(False)
        self.back_btn.setToolTip(tr(
            'Back to the previous view (where you were before drilling '
            'in or jumping); the mouse back button works too'))
        self.crumb_lbl = QtWidgets.QLabel()
        self.crumb_lbl.setTextFormat(QtCore.Qt.RichText)
        self.crumb_lbl.setTextInteractionFlags(
            QtCore.Qt.TextBrowserInteraction)
        self.crumb_lbl.linkActivated.connect(
            lambda href: self.callbacks['navigate'](int(href)))
        self.crumb_lbl.setWordWrap(True)   # overflow wraps to more lines
        # Ignored width + heightForWidth: never report a content-driven
        # minimum width, so a long path wraps instead of widening the dock
        _sp = self.crumb_lbl.sizePolicy()
        _sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        _sp.setHeightForWidth(True)
        self.crumb_lbl.setSizePolicy(_sp)
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText(tr('Filter node names…'))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMinimumWidth(220)  # floor; grows elastically
        self.config_btn = QtWidgets.QToolButton()
        self.config_btn.setText(tr('⚙ Config'))
        self.config_btn.setCheckable(True)
        self.config_btn.setToolTip(tr(
            'Toggle the config bar: display filters, analysis, feature '
            'switches and resource-candidate rules'))

        self.status_lbl = QtWidgets.QLabel('')
        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.back_btn)
        bar.addWidget(self.filter_edit, 1)  # elastic: takes toolbar slack
        bar.addWidget(self.config_btn)
        # breadcrumb gets its own full-width row below the toolbar so a long
        # path wraps across lines instead of being starved or widening the dock
        topbar.setFont(self._chrome_font)
        root.addWidget(topbar)

        self.crumb_lbl.setFont(self._chrome_font)
        self.crumb_lbl.setContentsMargins(2, 0, 2, 0)
        root.addWidget(self.crumb_lbl)

        self.config_band = self._build_config_band()
        self.config_band.setFont(self._chrome_font)
        self.config_band.setVisible(False)
        root.addWidget(self.config_band)

        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = GraphView(self, self.scene)
        root.addWidget(self.view, 1)

        # status line under the canvas: node/edge counts, parse time,
        # warnings tooltip
        self.status_lbl.setFont(self._chrome_font)
        self.status_lbl.setContentsMargins(4, 1, 4, 1)
        root.addWidget(self.status_lbl)

        self.refresh_btn.clicked.connect(lambda: self.callbacks['refresh']())
        self.back_btn.clicked.connect(lambda: self.callbacks['back']())
        self.filter_edit.textChanged.connect(lambda _t: self._apply_visual_state())
        self.filter_edit.returnPressed.connect(self.focus_next_match)
        self.config_btn.toggled.connect(self.config_band.setVisible)
        self._wire_config_signals()

    # ------------------------------------------------------ config band

    def _build_config_band(self):
        """Top configuration band. Display/parse/feature columns apply
        instantly; the candidates block batches behind [Apply & re-analyze]
        (a replay round-trip re-reads the capture)."""
        cfg = self.cfg
        pal = self.palette()
        sub = _mix(pal.color(QtGui.QPalette.WindowText),
                   pal.color(QtGui.QPalette.Window), 0.45)

        def cb(text, key, tip=''):
            box = QtWidgets.QCheckBox(text)
            box.setChecked(bool(cfg.get(key, _config.DEFAULTS[key])))
            if tip:
                box.setToolTip(tip)
            self._cfg_boxes[key] = box
            return box

        def header(text, spaced=False):
            lbl = QtWidgets.QLabel(text)
            f = lbl.font()
            f.setPointSizeF(f.pointSizeF() * 0.92)
            if spaced:
                f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.5)
            lbl.setFont(f)
            p = lbl.palette()
            p.setColor(QtGui.QPalette.WindowText, sub)
            lbl.setPalette(p)
            return lbl

        def vsep():
            f = QtWidgets.QFrame()
            f.setFrameShape(QtWidgets.QFrame.VLine)
            f.setFrameShadow(QtWidgets.QFrame.Sunken)
            return f

        def col(title, boxes):
            w = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(4)
            v.addWidget(header(title, spaced=True))
            for b in boxes:
                v.addWidget(b)
            v.addStretch(1)
            return w

        band = QtWidgets.QFrame()
        band.setAutoFillBackground(True)
        lay = QtWidgets.QHBoxLayout(band)
        lay.setContentsMargins(14, 8, 14, 10)
        lay.setSpacing(24)

        self.act_external = cb(
            tr('External inputs'), _config.KEY_SHOW_EXTERNAL,
            tr('Resources only read, never written this frame '
               '([external] hatched nodes) - content comes from '
               'outside the frame: a previous frame\'s history buffers, '
               'asset textures, engine-resident data. "Scope inputs" '
               '(written elsewhere this frame, read in this scope) '
               'always show.'))
        self.act_internal = cb(
            tr('Internal working sets'), _config.KEY_SHOW_INTERNAL,
            tr('Resources read and written by one node with no other '
               'consumer (working sets)'))
        self.act_orphans = cb(
            tr('Orphan nodes'), _config.KEY_SHOW_ORPHANS,
            tr('Nodes with no RT input or output (texture uploads, '
               'staging copies, etc.)'))
        self.act_portals = cb(
            tr('Scope portals'), _config.KEY_SHOW_PORTALS,
            tr('⧉ nodes standing in for outside scopes that touch this '
               'scope\'s resources; double-click to jump there'))
        lay.addWidget(col(tr('Show'), (self.act_external, self.act_orphans,
                                       self.act_portals, self.act_internal)))
        lay.addWidget(vsep())

        self.bundle_cb = cb(
            tr('Bundle identical nodes'), _config.KEY_BUNDLING,
            tr('Merge nodes that behave identically into one: resources '
               'by their writer/reader sets, passes by their read/write '
               'resource sets (matched on edge structure + a name '
               'heuristic, 3 or more). Typical: a group of Mesh_* '
               'skinning buffers, or a run of vkCmdCopyBuffer writing the '
               'same buffer. Members are listed per row; double-click a '
               'row to jump.'))
        self.act_unused = cb(
            tr('Parse shader source (experimental)'), _config.KEY_PARSE_SHADER,
            tr('Experimental: parse shader bytecode (DXBC / SPIR-V / DXIL) to tell '
               'whether each read-write resource (UAV / RWBuffer / storage image) is '
               'actually read-only, write-only or read-write, and drop the spurious '
               'edge accordingly. One background static pass over the whole frame, '
               'reconstructed from the capture with no per-event replay; APIs or '
               'cases it cannot resolve statically (e.g. OpenGL, bindless) keep both '
               'edges. Needs RenderDoc >= 1.33.'))
        lay.addWidget(col(tr('Features'), (self.bundle_cb, self.act_unused)))
        lay.addWidget(vsep())

        # candidates block: header, texture/buffer grid, buttons
        cand = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(cand)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        cand_lbl = header(tr('Resource candidates'), spaced=True)
        cand_lbl.setToolTip(tr(
            'Which resource categories may enter the graph (by creation '
            'flags). After a change, click "Apply & re-analyze" for one '
            'replay round-trip; widening the candidates costs more '
            'analysis time, but resources no event touches never become '
            'nodes.'))
        head.addWidget(cand_lbl)
        head.addStretch(1)
        cv.addLayout(head)

        self.cand_tex_color = cb(tr('Color targets'), _config.KEY_TEX_COLOR,
                                 tr('Textures with the ColorTarget '
                                    'creation flag'))
        self.cand_tex_depth = cb(tr('Depth targets'), _config.KEY_TEX_DEPTH,
                                 tr('Textures with the DepthTarget '
                                    'creation flag'))
        self.cand_tex_rw = cb(tr('Read-write'), _config.KEY_TEX_RW,
                              tr('ShaderReadWrite (storage image / UAV) '
                                 'textures'))
        self.cand_tex_swap = cb(tr('Swapchain'), _config.KEY_TEX_SWAP,
                                tr('SwapBuffer back buffers'))
        self.cand_tex_other = cb(
            tr('Other'), _config.KEY_TEX_OTHER,
            tr('Textures with none of the above flags (mostly '
               'sampled-only asset textures, staging). Read-only assets '
               'become external inputs and stay hidden by the '
               'External inputs switch by default; categories you turned off '
               'do not re-enter through this switch.'))
        self.cand_buf_rw = cb(tr('Read-write'), _config.KEY_BUF_RW,
                              tr('Buffers with the ReadWrite (SSBO/UAV) '
                                 'creation flag'))
        self.cand_buf_indirect = cb(tr('Indirect'), _config.KEY_BUF_INDIRECT,
                                    tr('Indirect draw/dispatch argument '
                                       'buffers'))
        self.cand_buf_vi = cb(tr('Vertex / index'), _config.KEY_BUF_VERTEX_INDEX,
                              tr('Buffers with the Vertex / Index '
                                 'creation flag'))
        self.cand_buf_const = cb(tr('Constants'), _config.KEY_BUF_CONSTANTS,
                                 tr('Buffers with the Constants (uniform) '
                                    'creation flag'))
        self.cand_buf_noflags = cb(
            tr('No flags'), _config.KEY_BUF_NOFLAGS,
            tr('Buffers with no creation flags - copy targets / readback '
               'staging, etc. (invisible to every category mask; only '
               'this switch lets them through)'))

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        grid.addWidget(header(tr('Textures')), 0, 0)
        for i, w in enumerate((self.cand_tex_color, self.cand_tex_depth,
                               self.cand_tex_rw, self.cand_tex_swap,
                               self.cand_tex_other)):
            grid.addWidget(w, 0, i + 1)
        grid.addWidget(header(tr('Buffers')), 1, 0)
        for i, w in enumerate((self.cand_buf_rw,
                               self.cand_buf_indirect, self.cand_buf_vi,
                               self.cand_buf_const, self.cand_buf_noflags)):
            grid.addWidget(w, 1, i + 1)
        grid.setColumnStretch(7, 1)
        cv.addLayout(grid)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        self.cand_hint = QtWidgets.QLabel('')
        self.apply_btn = QtWidgets.QPushButton(tr('Apply'))
        self.apply_btn.setEnabled(False)
        self.apply_btn.setToolTip(tr(
            'Commit display and candidate changes: display-only changes '
            're-render instantly; candidate changes re-extract (a replay '
            'round-trip, seconds), both keeping your navigation position'))
        self.reset_btn = QtWidgets.QPushButton(tr('Reset to defaults'))
        self.reset_btn.setToolTip(tr(
            'Reset all settings to defaults (feature switches take effect '
            'at once; display and candidate changes still need Apply)'))
        btns.addWidget(self.cand_hint)
        btns.addWidget(self.apply_btn)
        btns.addWidget(self.reset_btn)
        cv.addLayout(btns)
        # absorb surplus height (like col()) so header + grid stay pinned to
        # the top, aligned row-for-row with the Show / Features columns
        cv.addStretch(1)
        lay.addWidget(cand, 1)
        return band

    _CANDIDATE_BOX_KEYS = (_config.KEY_TEX_COLOR, _config.KEY_TEX_DEPTH,
                           _config.KEY_TEX_RW, _config.KEY_TEX_SWAP,
                           _config.KEY_TEX_OTHER, _config.KEY_BUF_RW,
                           _config.KEY_BUF_INDIRECT, _config.KEY_BUF_VERTEX_INDEX,
                           _config.KEY_BUF_CONSTANTS, _config.KEY_BUF_NOFLAGS)

    def _wire_config_signals(self):
        # states were set in _build_config_band before any connect, so
        # construction never fires callbacks into the half-built window
        def wire(box, key, after=None):
            def on_toggled(on, key=key, after=after):
                self.cfg[key] = bool(on)
                _config.save(self.cfg)
                if after is not None:
                    after(on)
            box.toggled.connect(on_toggled)

        # display filters batch behind apply
        for key in (_config.KEY_SHOW_EXTERNAL, _config.KEY_SHOW_INTERNAL,
                    _config.KEY_SHOW_ORPHANS, _config.KEY_SHOW_PORTALS):
            wire(self._cfg_boxes[key], key,
                 lambda _on: self._update_dirty())
        wire(self.bundle_cb, _config.KEY_BUNDLING,
             lambda on: self.callbacks['bundling_toggled'](on))
        wire(self.act_unused, _config.KEY_PARSE_SHADER,
             lambda on: self.callbacks['unused_toggled'](on))

        for key in self._CANDIDATE_BOX_KEYS:
            wire(self._cfg_boxes[key], key,
                 lambda _on: self._update_dirty())

        self.apply_btn.clicked.connect(self._on_apply)
        self.reset_btn.clicked.connect(self._reset_defaults)

    def candidate_config(self):
        """Snapshot of the extract-affecting switches, shaped for
        extract_bundle(candidates=...)."""
        return _config.candidates_of(self.cfg)

    def set_candidates_applied(self, cands):
        """Record the candidate set of the landed bundle; dirty state
        compares against it so a failed extraction keeps the pending hint."""
        self._applied_candidates = dict(cands)
        self._update_dirty()

    def _dirty_state(self):
        cand = (self._applied_candidates is not None and
                _config.candidates_of(self.cfg) !=
                self._applied_candidates)
        disp = _config.display_of(self.cfg) != self._applied_display
        return cand, disp

    def _update_dirty(self):
        cand_dirty, disp_dirty = self._dirty_state()
        self.apply_btn.setEnabled(cand_dirty or disp_dirty)
        # only candidate changes need the replay round-trip
        self.apply_btn.setText(tr('Apply & re-analyze') if cand_dirty
                               else tr('Apply'))
        self.cand_hint.setText(tr('modified →') if (cand_dirty or disp_dirty)
                               else '')

    def _on_apply(self):
        cand_dirty, disp_dirty = self._dirty_state()
        if disp_dirty:
            # commit display snapshot first so a following re-extraction
            # rebuild renders with it too
            self._applied_display = _config.display_of(self.cfg)
        if cand_dirty:
            self.callbacks['candidates_apply']()
        elif disp_dirty:
            if self.graph is not None:
                self.set_graph(self.graph, fit=False)
            self.callbacks['display_changed']()
        self._update_dirty()

    def _reset_defaults(self):
        for key, box in self._cfg_boxes.items():
            box.setChecked(bool(_config.DEFAULTS[key]))

    # ------------------------------------------------------------- state

    def bundling_enabled(self):
        return self.bundle_cb.isChecked()

    def set_back_enabled(self, enabled):
        self.back_btn.setEnabled(bool(enabled))

    def set_breadcrumb(self, labels):
        """labels: scope chain from the root. Each non-current segment is a
        clickable scope link; the current scope is bold."""
        last = len(labels) - 1
        parts = []
        for i, label in enumerate(labels):
            esc = (label.replace('&', '&amp;').replace('<', '&lt;')
                   .replace('>', '&gt;'))
            if i == last:
                parts.append(u'<b>%s</b>' % esc)
            else:
                parts.append(u'<a href="%d" style="color:%s;">%s</a>'
                             % (i, _CRUMB_LINK, esc))
        self.crumb_lbl.setText(_CRUMB_SEP.join(parts))

    def _capture_view_state(self):
        if not self.node_items:
            return None
        center = self.view.mapToScene(self.view.viewport().rect().center())
        return (QtGui.QTransform(self.view.transform()),
                QtCore.QPointF(center))

    def _restore_view_state(self, state):
        transform, center = state
        self.view.setTransform(transform)
        scale = transform.m11()
        self.view._zoom = scale if scale > 0 else 1.0
        self.view.centerOn(center)

    def capture_view_state(self):
        """Snapshot pan/zoom for navigation history."""
        return self._capture_view_state()

    def restore_view_state(self, state):
        if state is not None:
            self._restore_view_state(state)

    def has_thumbnail(self, key):
        return key in self.thumb_paths

    def set_status(self, text, warnings=None):
        self.status_lbl.setText(text)
        tip = ''
        if warnings:
            tip = '\n'.join(warnings[:TOOLTIP_LIST_LIMIT])
            if len(warnings) > TOOLTIP_LIST_LIMIT:
                tip += '\n... (%d more)' % (len(warnings) - TOOLTIP_LIST_LIMIT)
        self.status_lbl.setToolTip(tip)

    def show_message(self, text):
        self._building = True
        self.scene.clear()
        self._building = False
        self.node_items = {}
        self.edge_items = []
        self._incident = {}
        self.selected_id = None
        item = self.scene.addSimpleText(text)
        item.setBrush(self.theme.sub_text)
        self.scene.setSceneRect(item.boundingRect().adjusted(-40, -40, 40, 40))
        self.view.reset_zoom()

    # ------------------------------------------------------------- build

    def _node_size(self, node, is_pass, fm, fm_bold):
        # titles paint bold, so measure them bold too or the node sizes a
        # few percent short and elides forever
        if is_pass:
            if node.kind == CAT_PORTAL:
                prefix = u'⧉ '
            elif node.kind == CAT_COMPUTE:
                prefix = u'⚙ '
            else:
                prefix = ''
            suffix = u' ▸' if getattr(node, 'drillable', False) else ''
            w = max(_text_width(fm_bold, prefix + node.name + suffix),
                    _text_width(fm, 'EID %d-%d  (%d)' % (
                        node.first_eid, node.last_eid, node.action_count)))
            members = getattr(node, 'bundle_members', None)
            if members:
                w, rows = _measure_member_rows(fm, members, w)
                return (w, PASS_BUNDLE_ROWS_Y + rows * BUNDLE_ROW_H + 6.0)
            w = max(w + 2 * PAD + 6, 130.0)
            return (w, 46.0)
        members = getattr(node, 'bundle_members', None)
        if members:
            w, rows = _measure_member_rows(fm, members,
                                           _text_width(fm_bold, node.name))
            return (w, TITLE_H + rows * BUNDLE_ROW_H + 8.0)
        info = node.info or {}
        sub = '%s %s' % (info.get('dims', ''), info.get('format', ''))
        w = max(_text_width(fm, node.label()), _text_width(fm, sub))
        if getattr(node, 'version', 1) >= 2:
            w += BADGE_W  # episode badge shares the title row
        w = max(w + 2 * PAD + 12, 130.0)
        h = 42.0
        if node.res_kind != RES_BUFFER and self.is_expanded(thumb_key(node)):
            w = max(w, THUMB_W + 2 * PAD)
            h += THUMB_H + 8
        return (w, h)

    def set_graph(self, graph, fit=True):
        """fit=False keeps the current pan/zoom (display-option toggles
        within a scope); fit=True re-fits the whole graph (new scope,
        refresh)."""
        view_state = None if fit else self._capture_view_state()
        self.graph = graph
        self._building = True
        self.scene.clear()
        self._building = False
        self.node_items = {}
        self.edge_items = []
        self._incident = {}
        self.selected_id = None
        self._match_text = ''
        self._match_idx = -1
        self.hidden_counts = {'orphans': 0, 'external': 0, 'internal': 0}
        self.layout_warnings = []
        if graph is None:
            self.show_message(tr(
                'Load a capture, then click Refresh to build the '
                'dependency graph'))
            return

        self._episode_totals = {}
        for rnode in graph.resources:
            cur = self._episode_totals.get(rnode.res_key, 0)
            if rnode.version > cur:
                self._episode_totals[rnode.res_key] = rnode.version

        vis_passes, vis_resources = self._visible_nodes(graph)

        if not vis_passes:
            if self.hidden_counts['orphans']:
                self.show_message(
                    tr('All %d nodes have no RT dependencies (enable them '
                       'in the Show menu)')
                    % self.hidden_counts['orphans'])
            else:
                self.show_message(tr('No analyzable passes in this scope'))
            return

        fm = QtGui.QFontMetrics(self.font())
        fbold = QtGui.QFont(self.font())
        fbold.setBold(True)
        fm_bold = QtGui.QFontMetrics(fbold)
        sizes = {}
        for p in vis_passes:
            sizes[p.id] = self._node_size(p, True, fm, fm_bold)
        for rnode in vis_resources:
            sizes[rnode.id] = self._node_size(rnode, False, fm, fm_bold)

        vis_nodes = list(vis_passes) + list(vis_resources)
        positions, lay_warns = self._layout_for(vis_nodes, graph, sizes)
        # hold layout warnings on the panel (current view) instead of mutating
        # graph.warnings; the controller unions them when it reports status, so
        # repeated set_graph calls on the same graph object never accumulate
        self.layout_warnings = list(lay_warns)
        self._populate_scene(vis_nodes, vis_resources, graph, positions, sizes,
                             view_state)

    def _visible_nodes(self, graph):
        """Apply the committed display-filter snapshot to graph's nodes,
        updating hidden_counts; -> (vis_passes, vis_resources). Checkbox edits
        stay pending until apply commits them into _applied_display."""
        disp = self._applied_display
        orphan_ids = getattr(graph, 'orphan_pass_ids', set())
        if disp[_config.KEY_SHOW_ORPHANS]:
            vis_passes = list(graph.passes)
        else:
            vis_passes = [p for p in graph.passes if p.id not in orphan_ids]
        self.hidden_counts['orphans'] = len(graph.passes) - len(vis_passes)
        if not disp[_config.KEY_SHOW_PORTALS]:
            vis_passes = [p for p in vis_passes if p.kind != CAT_PORTAL]

        show_external = disp[_config.KEY_SHOW_EXTERNAL]
        show_internal = disp[_config.KEY_SHOW_INTERNAL]
        vis_resources = []
        for rnode in graph.resources:
            scope_input = getattr(rnode, 'scope_input', False)
            if not show_external and rnode.imported and not scope_input:
                self.hidden_counts['external'] += 1
                continue
            if not show_internal and getattr(rnode, 'internal', False):
                # working set: content never leaves its single toucher
                self.hidden_counts['internal'] += 1
                continue
            vis_resources.append(rnode)
        return vis_passes, vis_resources

    def _layout_for(self, vis_nodes, graph, sizes):
        """Memoise layout by exact input content so revisiting a view (back,
        display toggles, portal jumps) is free; -> (positions, lay_warns)."""
        rank_edges = getattr(graph, 'rank_edges', None) or None
        cache_key = (
            tuple(n.id for n in vis_nodes),
            tuple((e.src_id, e.dst_id) for e in graph.edges),
            (tuple((e.src_id, e.dst_id) for e in rank_edges)
             if rank_edges is not None else None),
            tuple(sizes[n.id] for n in vis_nodes),
        )
        cached = self._layout_cache.get(cache_key)
        if cached is not None:
            self._layout_cache.move_to_end(cache_key)
            return dict(cached[0]), list(cached[1])
        positions, lay_warns, _routes, _stubs, _sp = \
            graph_layout.compute_layout_auto(
                vis_nodes, graph.edges, sizes, rank_edges=rank_edges)
        self._layout_cache[cache_key] = (dict(positions), list(lay_warns))
        while len(self._layout_cache) > _LAYOUT_CACHE_CAP:
            self._layout_cache.popitem(last=False)
        return positions, lay_warns

    def _populate_scene(self, vis_nodes, vis_resources, graph, positions, sizes,
                        view_state):
        """Build NodeItems / EdgeItems into the scene, fit or restore the view,
        and restore expanded thumbnails + visual state."""
        self._building = True
        for node in vis_nodes:
            is_pass = hasattr(node, 'first_eid')
            item = NodeItem(self, node, is_pass,
                            sizes[node.id][0], sizes[node.id][1])
            item.setPos(positions[node.id][0], positions[node.id][1])
            shadow = QtWidgets.QGraphicsDropShadowEffect()
            shadow.setBlurRadius(14.0)
            shadow.setOffset(0.0, 3.0)
            shadow.setColor(self.theme.shadow)
            item.setGraphicsEffect(shadow)
            # PySide2: QGraphicsItem is not a QObject and does not own the
            # effect; without this ref it is GC'd and vanishes
            item._shadow = shadow
            self.scene.addItem(item)
            self.node_items[node.id] = item
        self._building = False

        for e in graph.edges:
            src = self.node_items.get(e.src_id)
            dst = self.node_items.get(e.dst_id)
            if src is None or dst is None:
                continue
            item = EdgeItem(e, src, dst)
            self.edge_items.append(item)
            self.scene.addItem(item)
            self._incident.setdefault(e.src_id, []).append(item)
            self._incident.setdefault(e.dst_id, []).append(item)

        self._assign_anchors()

        rect = self.scene.itemsBoundingRect()
        self.scene.setSceneRect(rect.adjusted(-200, -200, 200, 200))
        if view_state is not None:
            self._restore_view_state(view_state)
        else:
            self.view.apply_fit(rect)
        self._thumb_nodes = {}
        for rnode in vis_resources:
            key = thumb_key(rnode)
            if key is not None:
                self._thumb_nodes[key] = rnode.id
        # restore expanded previews: cached pixmap shows instantly, a miss
        # re-requests a single-node grab
        cb = self.callbacks.get('request_thumb')
        for key in list(self.expanded.keys()):
            if key not in self._thumb_nodes:
                continue
            path = self.thumb_paths.get(key)
            if path:
                self._apply_pixmap(key, path)
            elif cb:
                cb(key, self.expanded[key])
        self._apply_visual_state()

    def focus_next_match(self):
        """Enter in the filter box: center + select the next node whose
        label contains the text, cycling left-to-right."""
        text = self.filter_edit.text().strip().lower()
        if not text or self.graph is None:
            return
        matches = []
        for nid, item in self.node_items.items():
            label = item.node.name if item.is_pass else item.node.label()
            if text in label.lower():
                matches.append((item.scenePos().x(),
                                item.scenePos().y(), nid))
        if not matches:
            self.set_status(tr('No node name contains "%s"') %
                            self.filter_edit.text().strip())
            return
        matches.sort()
        if text != self._match_text:
            self._match_text = text
            self._match_idx = -1
        self._match_idx = (self._match_idx + 1) % len(matches)
        nid = matches[self._match_idx][2]
        item = self.node_items[nid]
        if self.view.transform().m11() < 0.8:
            self.view.reset_zoom()
        self.view.centerOn(item)
        self.selected_id = nid
        self._apply_visual_state()
        self.set_status(tr('match %d/%d: %s') %
                        (self._match_idx + 1, len(matches),
                         item.node.name if item.is_pass
                         else item.node.label()))

    def focus_event(self, eid):
        """Center, zoom and highlight the pass containing eid, after a
        node-portal jump."""
        target = None
        for item in self.node_items.values():
            n = item.node
            if (not item.is_pass or getattr(n, 'kind', '') == CAT_PORTAL or
                    not hasattr(n, 'first_eid')):
                continue
            if n.first_eid <= eid <= n.last_eid:
                target = item
                break
        if target is None:
            return False
        if self.view.transform().m11() < 0.8:
            self.view.reset_zoom()  # readable 1:1
        self.view.centerOn(target)
        self.selected_id = target.node.id
        self._apply_visual_state()
        return True

    def _assign_anchors(self):
        """Fan edge anchors along each node side, sorted by the other end's
        vertical position."""
        out_by_node = {}
        in_by_node = {}
        for item in self.edge_items:
            k = (item.edge.src_id, item.edge.dst_id, item.edge.kind)
            out_by_node.setdefault(item.edge.src_id, []).append(
                (item.sort_y_out(), k, item))
            in_by_node.setdefault(item.edge.dst_id, []).append(
                (item.sort_y_in(), k, item))

        for lists, attr in ((out_by_node, 'src_frac'), (in_by_node, 'dst_frac')):
            for nid, entries in lists.items():
                entries.sort(key=lambda t: (t[0], t[1]))
                n = len(entries)
                for i, (_y, _k, item) in enumerate(entries):
                    setattr(item, attr, (i + 1.0) / (n + 1.0))

        for item in self.edge_items:
            item.rebuild()

    # ----------------------------------------------------------- thumbs

    def refresh_edge_styles(self):
        """Re-apply solid/dashed styling after a binding scan or the
        unused-binding toggle."""
        for item in self.edge_items:
            item.apply_style()

    def clear_thumbnails(self):
        self.thumb_paths = {}
        for item in self.node_items.values():
            if not item.is_pass:
                item.set_pixmap(None)

    def set_thumbnail(self, key, path):
        self.thumb_paths[key] = path
        self._apply_pixmap(key, path)   # no-op if the node isn't on screen

    def set_thumb_loading(self, keys):
        """Show a "loading" placeholder on nodes about to be grabbed."""
        for key in keys:
            item = self.node_items.get(self._thumb_nodes.get(key))
            if item is not None and not item.is_pass:
                item.set_thumb_state('loading')

    def set_thumb_failed(self, key):
        item = self.node_items.get(self._thumb_nodes.get(key))
        if item is not None and not item.is_pass:
            item.set_thumb_state('failed')

    def is_expanded(self, key):
        return key in self.expanded

    def autofit_of(self, key):
        return bool(self.expanded.get(key, False))

    def clear_expanded(self):
        self.expanded = {}

    def on_eye_clicked(self, node):
        self.cycle_preview(thumb_key(node))

    def cycle_preview(self, key):
        # cycle collapsed -> raw -> fitted -> collapsed. Entering an
        # expanded state drops the cached pixmap so the grab uses the right
        # range (set_graph's restore loop re-issues it).
        if key is None:
            return
        if key not in self.expanded:
            self.expanded[key] = False             # collapsed -> raw
            self.thumb_paths.pop(key, None)
        elif not self.expanded[key]:
            self.expanded[key] = True              # raw -> fitted
            self.thumb_paths.pop(key, None)
        else:
            del self.expanded[key]                 # -> collapsed
        if self.graph is not None:
            self.set_graph(self.graph, fit=False)

    def _apply_pixmap(self, key, path):
        item = self.node_items.get(self._thumb_nodes.get(key))
        if item is None or item.is_pass:
            return
        pm = QtGui.QPixmap(path)
        if pm.isNull():
            return
        pm = pm.scaled(THUMB_W, THUMB_H, QtCore.Qt.KeepAspectRatio,
                       QtCore.Qt.SmoothTransformation)
        item.set_pixmap(pm)
        item.set_thumb_state('idle')   # clear any loading state

    # ------------------------------------------------------ interaction

    def on_node_moved(self, node_id):
        if self._building:
            return
        for item in self._incident.get(node_id, ()):
            item.rebuild()

    def on_node_clicked(self, node):
        self.selected_id = node.id
        self._apply_visual_state()
        item = self.node_items.get(node.id)
        if item is not None and item.is_pass:
            self.callbacks['pass_clicked'](node)

    def on_node_double_clicked(self, node):
        item = self.node_items.get(node.id)
        if item is None:
            return
        if item.is_pass:
            # portal_path may be the empty tuple (whole-frame root), so test
            # kind, not the path's truthiness
            if getattr(node, 'kind', '') == CAT_PORTAL:
                self.callbacks['jump_scope'](node)
            elif getattr(node, 'drillable', False):
                self.callbacks['drill'](node)
        else:
            self.callbacks['resource_double_clicked'](node.res_key)

    def on_bundle_member_double_clicked(self, node, index):
        if getattr(node, 'kind', '') == CAT_PORTAL:
            # a merged portal row still travels through the portal, landing
            # focused on that member's event in the parent view
            eids = getattr(node, 'bundle_member_eids', [])
            if 0 <= index < len(eids):
                self.callbacks['jump_scope'](node, eids[index])
            return
        if hasattr(node, 'first_eid'):
            # pass bundle rows jump the whole UI to that member's event
            eids = getattr(node, 'bundle_member_eids', [])
            if 0 <= index < len(eids):
                self.callbacks['member_event_jump'](eids[index])
            return
        keys = getattr(node, 'bundle_member_keys', [])
        if 0 <= index < len(keys):
            self.callbacks['resource_double_clicked'](keys[index])

    def on_background_clicked(self):
        if self.selected_id is not None:
            self.selected_id = None
            self._apply_visual_state()

    def _closure(self, start_id):
        fwd = {}
        rev = {}
        for item in self.edge_items:
            e = item.edge
            fwd.setdefault(e.src_id, []).append(e.dst_id)
            rev.setdefault(e.dst_id, []).append(e.src_id)
        keep = set([start_id])
        for adj in (fwd, rev):
            stack = [start_id]
            while stack:
                nid = stack.pop()
                for m in adj.get(nid, ()):
                    if m not in keep:
                        keep.add(m)
                        stack.append(m)
        return keep

    def _apply_visual_state(self):
        ftext = self.filter_edit.text().strip().lower()
        keep = self._closure(self.selected_id) if self.selected_id else None
        sel_res_key = None
        if self.selected_id is not None:
            sel_item = self.node_items.get(self.selected_id)
            if sel_item is not None and not sel_item.is_pass:
                sel_res_key = sel_item.node.res_key
        for nid, item in self.node_items.items():
            op = 1.0
            if ftext:
                label = item.node.name if item.is_pass else item.node.label()
                if ftext not in label.lower():
                    op = min(op, FILTER_DIM_OPACITY)
            if keep is not None and nid not in keep:
                op = min(op, DIM_OPACITY)
            item.setOpacity(op)
            # selected closure floats above unrelated nodes
            item.setZValue(Z_NODE_HI if (keep is not None and nid in keep)
                           else Z_NODE_BASE)
            # twins of the same logical resource outline together
            is_sel = (nid == self.selected_id or
                      (sel_res_key is not None and not item.is_pass and
                       item.node.res_key == sel_res_key))
            item.set_selected_style(is_sel)
        sel = self.selected_id
        for eitem in self.edge_items:
            e = eitem.edge
            src_item = self.node_items.get(e.src_id)
            dst_item = self.node_items.get(e.dst_id)
            if src_item is None or dst_item is None:
                continue
            edge_op = min(src_item.opacity(), dst_item.opacity())
            if keep is None:
                eitem.set_emphasis('normal')
                eitem.setOpacity(edge_op)
            elif e.src_id == sel or e.dst_id == sel:
                eitem.set_emphasis('direct')
                eitem.setOpacity(edge_op)
            elif e.src_id in keep and e.dst_id in keep:
                eitem.set_emphasis('indirect')
                eitem.setOpacity(min(edge_op, EDGE_INDIRECT_OPACITY))
            else:
                eitem.set_emphasis('muted')
                eitem.setOpacity(min(edge_op, MUTED_EDGE_OPACITY))

    # ---------------------------------------------------------- tooltip

    def tooltip_for(self, node):
        if hasattr(node, 'first_eid'):  # PassNode vs ResourceNode
            lines = [node.name, 'kind: %s' % node.kind,
                     'EID %d - %d' % (node.first_eid, node.last_eid),
                     'actions: %d' % node.action_count]
            members = getattr(node, 'bundle_members', None)
            if members:
                lines.append(
                    tr('Bundle of %d identical nodes (double-click a row '
                       'to jump to its event):') % len(members))
                for nm in members[:TOOLTIP_LIST_LIMIT]:
                    lines.append(u'  · %s' % nm)
                if len(members) > TOOLTIP_LIST_LIMIT:
                    lines.append(tr('  … +%d more')
                                 % (len(members) - TOOLTIP_LIST_LIMIT))
            if node.marker_path:
                lines.append('marker: %s' % ' / '.join(node.marker_path))
            if getattr(node, 'kind', '') == CAT_PORTAL:
                if getattr(node, 'portal_focus_eid', None) is not None:
                    where = (u' / '.join(node.portal_path)
                             if node.portal_path else tr('Whole frame'))
                    lines.append(
                        tr('[external node - double-click to jump to "%s" '
                           'and locate it]') % where)
                else:
                    lines.append(
                        tr('[external scope - double-click to jump '
                           'there]'))
            elif getattr(node, 'drillable', False):
                lines.append(tr('[double-click to enter]'))
            return '\n'.join(lines)
        info = node.info or {}
        lines = [node.name, 'kind: %s' % node.res_kind]
        members = getattr(node, 'bundle_members', None)
        if members:
            lines.append(
                tr('Bundle of %d identical resources:') % len(members))
            for nm in members[:TOOLTIP_LIST_LIMIT]:
                lines.append(u'  · %s' % nm)
            if len(members) > TOOLTIP_LIST_LIMIT:
                lines.append(tr('  … +%d more')
                             % (len(members) - TOOLTIP_LIST_LIMIT))
        gens = getattr(node, 'generations', 1)
        if gens > 1:
            lines.append(
                tr('%d write generations collapsed: every generation\'s '
                   'read/write edges meet at this node; an early-gen '
                   'reader linked to a late-gen writer is a temporal '
                   'artifact') % gens)
        total = self._episode_totals.get(node.res_key, 1)
        if total > 1:
            lines.append(tr('write version: #%d / %d') %
                         (node.version, total))
        if info.get('dims'):
            lines.append(info['dims'])
        if info.get('format') and info['format'] != 'buffer':
            lines.append(info['format'])
        if getattr(node, 'scope_input', False):
            lines.append(
                tr('[scope input - written from outside the current '
                   'scope]'))
        elif node.imported:
            lines.append(
                tr('[external - never written this frame; content '
                   'from outside it]'))
        if getattr(node, 'internal', False):
            lines.append(tr('[internal - content never leaves this node]'))
        out_r = getattr(node, 'outside_readers', 0)
        out_w = getattr(node, 'outside_writers', 0)
        if out_r or out_w:
            lines.append(tr('outside scope: %d reads / %d writes') %
                         (out_r, out_w))
        lines.append('writers: %d   readers: %d' %
                     (len(node.writer_ids), len(node.reader_ids)))
        if node.res_kind != RES_BUFFER and not members:
            lines.append(
                tr('eye: click to cycle preview - collapsed -> raw '
                   'range -> fitted range'))
        return '\n'.join(lines)
