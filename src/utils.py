"""Shared utilities: logging setup, log purge, retry decorator,
tag normalization, source-ref hashing, and body truncation.
"""

import functools
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import config

_CST = ZoneInfo("America/Chicago")

T = TypeVar("T")


def _safe_cache_usage(llm: Any) -> tuple[int, int]:
    """Read (cache_read_tokens, cache_creation_tokens) from llm.consume_cache_usage().

    Tolerates test doubles that don't implement this method (e.g. bare MagicMock)
    by falling back to (0, 0) instead of raising on unpacking.
    """
    try:
        cache_read, cache_creation = llm.consume_cache_usage()
        return cache_read or 0, cache_creation or 0
    except Exception:
        return 0, 0


def setup_logging(log_name: str) -> logging.Logger:
    """Configure root logger to a timestamped file in LOG_DIR plus stderr.

    Args:
        log_name: short name embedded in the log filename.

    Returns:
        Configured root logger.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M")
    log_path = config.LOG_DIR / f"{stamp}_{log_name}.txt"

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def purge_old_logs(
    log_dir: Path = config.LOG_DIR, days: int = config.LOG_RETENTION_DAYS
) -> int:
    """Delete .txt log files older than `days`. Returns count removed."""
    if not log_dir.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for path in log_dir.glob("*.txt"):
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            path.unlink()
            removed += 1
    return removed


def retry(
    attempts: int = config.LLM_RETRY_ATTEMPTS,
    base_delay: float = config.LLM_RETRY_BASE_DELAY,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable:
    """Exponential-backoff retry decorator.

    Args:
        attempts: total tries before re-raising.
        base_delay: seconds; delay before retry N = base * 2**N.
        exceptions: exception types that trigger a retry.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == attempts - 1:
                        break
                    delay = base_delay * (2**attempt)
                    logging.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        func.__name__,
                        attempt + 1,
                        attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


def normalize_tag(tag: str) -> str:
    """Normalize a tag/label value for consistent storage and matching.

    Lowercase, trim, collapse hyphens/whitespace to single hyphen.
    Applied at both write time and filter time.
    """
    return re.sub(r"[\s\-]+", "_", tag.strip().lower())


def read_yaml_profile(path: Path) -> dict:
    """Load a YAML profile file into a dict. The single YAML-parse site.

    Validates existence and that the document parses to a mapping. Callers do
    their own schema validation. Raises FileNotFoundError / ValueError.
    """
    try:
        import yaml  # lazy import keeps module usable without the dep in tests
    except ImportError as exc:
        raise RuntimeError("PyYAML is required: pip install PyYAML") from exc

    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Profile {path} did not parse to a mapping")
    return data


def source_ref_hash(source_ref: str) -> str:
    """Return a 16-char hex hash of source_ref for use as an image subdir name."""
    return hashlib.sha256(source_ref.encode()).hexdigest()[:16]


def now_cst() -> datetime:
    """Current datetime in US Central time (CST/CDT), naive (no tzinfo) for DB storage."""
    return datetime.now(_CST).replace(tzinfo=None)


def truncate_body(text: str, cap: int = config.BODY_TEXT_CAP) -> str:
    """Cap body text at `cap` chars, appending a visible marker if truncated."""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n\n[truncated]"


def strip_emoji(text: str) -> str:
    """Replace emoji with their English text description.

    Converts e.g. 🎁 → 'gift' and 🧑‍💻 → 'technologist' using the emoji
    library's canonical short-code names. Other non-ASCII (accented letters,
    punctuation, currency symbols) are left untouched. Collapses any extra
    horizontal whitespace introduced by the substitution but preserves newlines.
    """
    import emoji as _emoji  # lazy import keeps module usable without the dep in tests

    result = _emoji.replace_emoji(
        text,
        replace=lambda chars, data_dict: f" {data_dict['en'].strip(':').replace('_', ' ')} ",
    )
    return re.sub(r"[ \t]{2,}", " ", result)


def resolve_labels(raw_labels: list[str]) -> list[str]:
    """Map LLM label output to canonical BUCKET_LABELS entries, case-insensitively.

    Drops any label the LLM returns that doesn't appear in BUCKET_LABELS.
    Returns ["Miscellaneous"] when nothing matches.
    BUCKET_LABELS must stay in sync with the classifier prompt's label set.
    """
    lookup = {lbl.lower(): lbl for lbl in config.BUCKET_LABELS}
    seen: set[str] = set()
    result: list[str] = []
    for lbl in raw_labels:
        canonical = lookup.get(lbl.strip().lower())
        if canonical and canonical not in seen:
            result.append(canonical)
            seen.add(canonical)
    return result or ["Miscellaneous"]


def parse_confidence(raw: Any, context: str, field: str = "confidence") -> int | None:
    """Coerce a raw LLM confidence value to int 1-5, or None with a warning.

    `context` is a short identity string (source_ref or record_id) for logging.
    `field` names the confidence field for clearer log messages (e.g.
    "summary_confidence", "classification_confidence").
    Accepts int or numeric str; returns None for missing, non-numeric, or out-of-range.
    """
    if raw is None:
        logging.warning("%s missing for %s — storing NULL", field, context)
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logging.warning("%s %r is not an integer for %s — storing NULL", field, raw, context)
        return None
    if not (1 <= value <= 5):
        logging.warning("%s %d out of range 1-5 for %s — storing NULL", field, value, context)
        return None
    return value


def summarize_content(
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    context: str,
) -> tuple[str, str | None, int | None, str | None]:
    """Run the summarize step (Haiku, no escalation) against body_text.

    Returns (summary, executive_summary, summary_confidence, summary_rationale).
    summary_rationale is truncated to 1000 chars; executive_summary, confidence, or
    rationale may be None if the model omits them.
    """
    llm.reset_usage()
    result = llm.call_json("summarize", user_prompt, system=system_prompt)
    summary = result.get("summary", "")
    executive_summary = result.get("executive_summary") or None
    summary_confidence = parse_confidence(
        result.get("summary_confidence"), context, field="summary_confidence"
    )
    summary_rationale = (result.get("summary_rationale") or "")[:1000] or None
    _, summary_tokens = llm.consume_usage()
    cache_read, cache_creation = _safe_cache_usage(llm)
    logging.info(
        "[summarize] %s → summary_confidence=%s len(summary)=%d has_exec_summary=%s "
        "tokens=%d cache_read=%d cache_creation=%d",
        context,
        summary_confidence,
        len(summary),
        executive_summary is not None,
        summary_tokens,
        cache_read,
        cache_creation,
    )
    return summary, executive_summary, summary_confidence, summary_rationale


def classify_with_escalation(
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    context: str,
) -> tuple[dict, int | None, bool, str | None, int | None]:
    """Run classify; escalate to the escalation model when confidence is low, label is
    Miscellaneous, or `actionable` may have been missed on genuine trade content.

    Reads the classify-step `classification_confidence` field. Escalation triggers
    when any condition holds (OR):
    - classification_confidence is not NULL and is at or below CLASSIFY_ESCALATION_THRESHOLD (set to 0 to disable)
    - any assigned label is Miscellaneous (always escalates, regardless of threshold)
    - label includes Business, `actionable` was withheld, and a high-precision trade-signal
      tag from config.CLASSIFY_ACTIONABLE_REVIEW_TAGS is present (see config for rationale)

    Returns (classify_result, classification_confidence, escalated, classify_model, classify_tokens).
    classify_model is the full model ID of the final call; classify_tokens is the
    total input+output tokens across all classify calls (including escalation if triggered).
    """
    llm.reset_usage()

    result = llm.call_json("classify", user_prompt, system=system_prompt)
    confidence = parse_confidence(
        result.get("classification_confidence"), context, field="classification_confidence"
    )
    initial_labels = result.get("labels", [])
    initial_tags = [tag.lower() for tag in result.get("tags", [])]

    logging.info(
        "[classify] %s → labels=%s classification_confidence=%s",
        context,
        initial_labels,
        confidence,
    )

    threshold = config.CLASSIFY_ESCALATION_THRESHOLD
    raw_labels = [lbl.lower() for lbl in initial_labels]

    escalate_low_conf = threshold > 0 and confidence is not None and confidence <= threshold
    escalate_misc = "miscellaneous" in raw_labels
    missed_actionable_tags = (
        set(initial_tags) & config.CLASSIFY_ACTIONABLE_REVIEW_TAGS
        if "business" in raw_labels and "actionable" not in initial_tags
        else set()
    )
    escalate_missed_actionable = bool(missed_actionable_tags)

    if escalate_low_conf or escalate_misc or escalate_missed_actionable:
        reasons = []
        if escalate_low_conf:
            reasons.append(f"low confidence ({confidence} <= {threshold})")
        if escalate_misc:
            reasons.append("Miscellaneous label")
        if escalate_missed_actionable:
            reasons.append(f"possible missed actionable (tags={sorted(missed_actionable_tags)})")
        escalation_model = config.CLASSIFY_ESCALATION_MODEL
        logging.info(
            "[escalate] %s → triggering %s re-classify: %s",
            context,
            escalation_model,
            " + ".join(reasons),
        )
        result = llm.call_json(
            "classify", user_prompt, system=system_prompt, model_override=escalation_model
        )
        new_confidence = parse_confidence(
            result.get("classification_confidence"), context, field="classification_confidence"
        )
        classify_model, classify_tokens = llm.consume_usage()
        cache_read, cache_creation = _safe_cache_usage(llm)
        logging.info(
            "[escalate] %s → done: labels %s → %s  confidence %s → %s  model=%s tokens=%s "
            "cache_read=%d cache_creation=%d",
            context,
            initial_labels,
            result.get("labels", []),
            confidence,
            new_confidence,
            classify_model,
            classify_tokens,
            cache_read,
            cache_creation,
        )
        return result, new_confidence, True, classify_model, classify_tokens or None

    classify_model, classify_tokens = llm.consume_usage()
    cache_read, cache_creation = _safe_cache_usage(llm)
    logging.info(
        "[classify] %s → no escalation (labels=%s classification_confidence=%s threshold=%s "
        "model=%s tokens=%s cache_read=%d cache_creation=%d)",
        context,
        initial_labels,
        confidence,
        threshold,
        classify_model,
        classify_tokens,
        cache_read,
        cache_creation,
    )
    return result, confidence, False, classify_model, classify_tokens or None


def is_trash_excluded(labels: list[str], tags: list[str]) -> bool:
    """Return True if this record matches any rule in config.TRASH_EXCLUSIONS.

    A rule matches when the record carries ALL labels AND ALL tags listed in
    that rule. Rules are OR'd: any single match excludes the record from trash.
    """
    label_set = set(labels)
    tag_set = set(tags)
    for rule in config.TRASH_EXCLUSIONS:
        rule_labels = set(rule.get("labels", []))
        rule_tags = set(rule.get("tags", []))
        if rule_labels.issubset(label_set) and rule_tags.issubset(tag_set):
            return True
    return False
