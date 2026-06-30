# -*- coding: utf-8 -*-
"""Pure breadcrumb-trail formatting: the scope chain rendered as the rich-text
string the panel's QLabel shows. Qt-free (it only builds an HTML string), so the
escaping and the current-vs-link markup are unit-testable without PySide2."""

SEP = u' ▸ '
LINK_COLOR = '#7ab0e0'       # link blue for clickable scope segments


def _escape(text):
    # & first, or the entities introduced by < / > would be double-escaped
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def format_breadcrumb(labels, link_color=LINK_COLOR, sep=SEP):
    """Render the scope chain (root first) as a rich-text string: every segment
    but the last is a clickable link whose href is its index; the last (current)
    scope is bold. Marker names are arbitrary, so every label is HTML-escaped."""
    last = len(labels) - 1
    parts = []
    for i, label in enumerate(labels):
        esc = _escape(label)
        if i == last:
            parts.append(u'<b>%s</b>' % esc)
        else:
            parts.append(u'<a href="%d" style="color:%s;">%s</a>'
                         % (i, link_color, esc))
    return sep.join(parts)
