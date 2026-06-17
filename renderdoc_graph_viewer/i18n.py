# -*- coding: utf-8 -*-
"""Minimal i18n. Source strings are English; tr() falls back to the source
when a language table has no entry, so adding a language is purely additive
and the default UI is English (matching RenderDoc, which ships no i18n).
Active language follows the system locale (QLocale); _TRANSLATIONS is empty
so every locale currently shows English.
"""

# Add a language by inserting a {source: translation} table keyed by locale,
# e.g. 'zh': {'Refresh': u'刷新', 'Config': u'配置', ...}; untranslated strings
# fall back to the English source (see tr()).
_TRANSLATIONS = {}

_lang = None


def _detect_lang():
    try:
        from PySide2 import QtCore
        return QtCore.QLocale.system().name().split('_')[0].lower()
    except Exception:
        return 'en'


def set_language(lang):
    """Override the detected language; also the test hook."""
    global _lang
    _lang = lang


def tr(s):
    """Translate a source string for the active language, falling back to
    the source itself when untranslated."""
    global _lang
    if _lang is None:
        _lang = _detect_lang()
    return _TRANSLATIONS.get(_lang, {}).get(s, s)
    