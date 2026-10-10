"""Resolve LLM aliases ("haiku", "sonnet", ...) to API model IDs.

Canonical copy: GitHub\\LLM_Config\\src\\llm_config.py.  distribute_llm_config.bat
ships it, together with llm_mapping.toml (renamed llm_config.toml), into each
consumer's source folder every day.  Edit it here, never in a consumer.

Consumers ask for an alias; the ID comes from the shipped llm_config.toml next
to this file, so changing a floating alias there moves every consumer at once.

    import llm_config
    model_id = llm_config.resolve("sonnet")

Fails loudly rather than guessing: a missing, unreadable or stale (older than
max_age_days) config file, or an unknown alias, raises LLMConfigError.  The
distributor touches the file on every run, so a stale copy means the job has
stopped.  Full model IDs are rejected on purpose - they would bypass the
central control.
"""

from __future__ import annotations

import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

CONFIG_FILE: Path = Path(__file__).with_name("llm_config.toml")
MAX_AGE_DAYS: float = 3
_HINT = r"Run GitHub\LLM_Config\src\distribute_llm_config.bat and check its scheduled task."

_cache: dict[Path, tuple[float, dict[str, str]]] = {}


class LLMConfigError(RuntimeError):
    """The alias could not be resolved from the local llm_config.toml copy."""


def load_models(path: Path | str | None = None, max_age_days: float | None = None) -> dict[str, str]:
    """Return the [models] table, validating that the file is present and fresh."""
    path = Path(path) if path else CONFIG_FILE
    max_age = MAX_AGE_DAYS if max_age_days is None else max_age_days
    try:
        mtime = path.stat().st_mtime
        cached = _cache.get(path)
        if cached and cached[0] == mtime:
            models = cached[1]
        else:
            with path.open("rb") as f:
                models = tomllib.load(f).get("models")
            if not isinstance(models, dict) or not models:
                raise LLMConfigError(f"LLM config has no [models] table: {path}. {_HINT}")
            _cache[path] = (mtime, models)
    except FileNotFoundError:
        raise LLMConfigError(f"LLM config not found: {path}. {_HINT}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LLMConfigError(f"LLM config unreadable: {path}: {exc}") from exc

    age_days = (time.time() - mtime) / 86400
    if age_days > max_age:
        raise LLMConfigError(
            f"LLM config is stale ({age_days:.1f} days old, max {max_age:g}): {path}. {_HINT}"
        )
    return models


def resolve(alias: str, path: Path | str | None = None, max_age_days: float | None = None) -> str:
    """Return the API model ID for *alias*.

    Raises:
        LLMConfigError: file missing/unreadable/stale, or alias unknown.
    """
    models = load_models(path, max_age_days)
    try:
        return models[alias]
    except (KeyError, TypeError):
        raise LLMConfigError(
            f"Unknown LLM alias {alias!r} in {Path(path) if path else CONFIG_FILE}. "
            f"Available: {', '.join(sorted(models))}"
        ) from None
