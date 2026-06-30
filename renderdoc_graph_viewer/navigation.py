# -*- coding: utf-8 -*-
"""Navigation state for scope drilling, breadcrumb jumps and Back."""

from collections import deque

NAV_HISTORY_LIMIT = 50   # back-navigation snapshots kept (bounded)


class NavigationState(object):
    def __init__(self, history_limit=NAV_HISTORY_LIMIT):
        self.scope_stack = []          # [{label, path, range}], root = empty
        self._history = deque(maxlen=history_limit)

    # ---- queries --------------------------------------------------------
    @property
    def can_back(self):
        return bool(self._history)

    def current_scope(self):
        """-> (scope_path, scope_range): what the graph is built for; the
        whole-frame root ((), None) when the stack is empty."""
        if self.scope_stack:
            cur = self.scope_stack[-1]
            return cur['path'], cur['range']
        return (), None

    def labels(self):
        """Breadcrumb labels below the root, outermost first."""
        return [s['label'] for s in self.scope_stack]

    # ---- transitions ----------------------------------------------------
    def push_history(self, view_state):
        """Snapshot the current scope stack and viewpoint so Back can restore
        both. Each snapshot keeps its own copy of the stack."""
        self._history.append({'stack': list(self.scope_stack),
                              'view': view_state})

    def drill(self, node, view_state):
        """Enter a drillable node, one level deeper."""
        self.push_history(view_state)
        self.scope_stack.append({
            'label': node.name,
            'path': tuple(node.marker_path),
            'range': (node.first_eid, node.last_eid),
        })

    def navigate(self, index, view_state):
        """Jump to a breadcrumb segment: index 0 is the whole-frame root, 1 the
        first scope, and so on -- everything from there down is dropped."""
        self.push_history(view_state)
        del self.scope_stack[max(0, index):]

    def jump(self, chain, view_state):
        """Replace the stack with a resolved scope chain (a portal jump to an
        external scope's ancestry)."""
        self.push_history(view_state)
        self.scope_stack = list(chain)

    def back(self):
        """Pop the last snapshot and return its saved view-state."""
        snap = self._history.pop()
        self.scope_stack = snap['stack']
        return snap['view']

    def reset(self):
        """Clear all navigation state (capture switch / full refresh)."""
        del self.scope_stack[:]
        self._history.clear()
