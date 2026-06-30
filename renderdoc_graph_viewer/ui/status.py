# -*- coding: utf-8 -*-
"""Pure status-bar line formatting for the Graph Viewer controller. Qt-free and
i18n-aware, so the stats / hidden-count / warning composition is unit-testable
even though the controller that uses it only runs inside qrenderdoc."""

from ..i18n import tr

WARNING_TOOLTIP_LIMIT = 20   # warning tooltip detail limit


def format_warning_tooltip(warnings, limit=WARNING_TOOLTIP_LIMIT):
    """The status-bar tooltip body: the first `limit` warning lines, then a
    '... (N more)' summary when there are more; '' when there are none."""
    if not warnings:
        return ''
    tip = '\n'.join(warnings[:limit])
    if len(warnings) > limit:
        tip += '\n... (%d more)' % (len(warnings) - limit)
    return tip


def format_status(stats, hidden_counts, warnings, extra=''):
    """Compose the status-bar text. stats: {passes, resources, edges, seconds};
    hidden_counts: {orphans, external, internal}; warnings: the already-unioned
    list (its length is shown; the caller passes it on for the tooltip)."""
    text = '%d passes · %d resources · %d edges · %.2fs' % (
        stats.get('passes', 0), stats.get('resources', 0),
        stats.get('edges', 0), stats.get('seconds', 0.0))
    parts = []
    for key, fmt in (('orphans', 'orphans %d'), ('external', 'external %d'),
                     ('internal', 'internal %d')):
        if hidden_counts.get(key):
            parts.append(tr(fmt) % hidden_counts[key])
    if parts:
        text += tr(' · hidden: ') + u' / '.join(parts)
    if warnings:
        text += u' · %d warnings' % len(warnings)
    if extra:
        text += ' · ' + extra
    return text
