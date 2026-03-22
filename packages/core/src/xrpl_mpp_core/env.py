from __future__ import annotations

import os


def clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = value.strip()
    if not cleaned or cleaned.startswith("#"):
        return None

    for separator in (" #", "\t#"):
        comment_index = cleaned.find(separator)
        if comment_index != -1:
            cleaned = cleaned[:comment_index].rstrip()
            break

    return cleaned or None


def getenv_clean(name: str, default: str | None = None) -> str | None:
    value = clean_env_value(os.getenv(name))
    if value is not None:
        return value
    return default
