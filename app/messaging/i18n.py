"""i18n — template loading and variable substitution for multilingual messages."""
from __future__ import annotations

from pathlib import Path

import yaml

TEMPLATES_DIR = Path(__file__).parent / "templates"

_cache: dict[str, dict[str, str]] = {}


def _load_lang(lang: str) -> dict[str, str]:
    """Load and cache a language file."""
    if lang not in _cache:
        path = TEMPLATES_DIR / f"{lang}.yaml"
        if not path.exists():
            path = TEMPLATES_DIR / "en.yaml"  # fallback
        with open(path) as f:
            _cache[lang] = yaml.safe_load(f) or {}
    return _cache[lang]


def t(key: str, lang: str = "en", **kwargs) -> str:
    """Get a translated template string with variable substitution.

    Usage: t("welcome_new", lang="ta", business_name="Style Studio")
    """
    templates = _load_lang(lang)
    template = templates.get(key, "")

    if not template:
        # Fallback to English
        templates = _load_lang("en")
        template = templates.get(key, f"[missing: {key}]")

    try:
        return template.format(**kwargs).strip()
    except KeyError:
        return template.strip()


def reload_templates():
    """Clear template cache. Call after editing YAML files."""
    _cache.clear()
