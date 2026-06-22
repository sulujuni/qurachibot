"""Core i18n engine — loads JSON locale files and provides translation lookup."""

import json
import os
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED_LANGUAGES = ["en", "ru", "uz"]
DEFAULT_LANGUAGE = "en"

# In-memory cache: { "en": { "key": "value", ... }, "ru": {...}, ... }
_translations: dict[str, dict[str, str]] = {}


def _load_translations() -> None:
    """Load all locale JSON files into memory."""
    for lang in SUPPORTED_LANGUAGES:
        lang_dir = LOCALES_DIR / lang
        _translations[lang] = {}
        if not lang_dir.exists():
            continue
        for json_file in sorted(lang_dir.glob("*.json")):
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                _translations[lang].update(data)


# Load on import
_load_translations()


def get_text(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs: Any) -> str:
    """Get a translated string by key, with optional format arguments.

    Falls back to English if key not found in the requested language,
    then falls back to the raw key if not found anywhere.
    """
    # Try requested language
    text = _translations.get(lang, {}).get(key)

    # Fallback to English
    if text is None and lang != DEFAULT_LANGUAGE:
        text = _translations.get(DEFAULT_LANGUAGE, {}).get(key)

    # Fallback to raw key
    if text is None:
        text = key

    # Format with kwargs if provided
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass

    return text


# Convenience alias
_ = get_text


def get_language(lang_code: str | None) -> str:
    """Normalize a language code to a supported language."""
    if lang_code and lang_code[:2] in SUPPORTED_LANGUAGES:
        return lang_code[:2]
    return DEFAULT_LANGUAGE


def set_language(lang: str) -> str:
    """Validate and return a supported language code."""
    if lang in SUPPORTED_LANGUAGES:
        return lang
    return DEFAULT_LANGUAGE


def reload_translations() -> None:
    """Reload all translations from disk (useful for development)."""
    _translations.clear()
    _load_translations()
