"""Structured JSONL logging helpers used across the whole pipeline."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _append_jsonl(path: Path, payload: dict) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def log_exception(sku: str | None, file: str | None, reason: str, **extra) -> None:
    """SOW Section 3 — exception report for unparseable/corrupt/locked PDFs etc."""
    _append_jsonl(settings.EXCEPTIONS_LOG, {"sku": sku, "file": file, "reason": reason, **extra})


def log_conflict(sku: str, field: str, candidates: list, **extra) -> None:
    """SOW Section 2 — merge_meta.conflicts mirror log for human review."""
    _append_jsonl(settings.CONFLICTS_LOG, {"sku": sku, "field": field, "candidates": candidates, **extra})


def log_review(sku: str | None, file: str | None, reason: str, payload: dict | None = None) -> None:
    """Low-confidence nameplate extractions routed to review queue."""
    _append_jsonl(settings.REVIEW_QUEUE, {"sku": sku, "file": file, "reason": reason, "payload": payload})


def log_delay(event: str, detail: str) -> None:
    """Section 7 delay log — document Genemco-side delays from day one."""
    _append_jsonl(settings.DELAY_LOG, {"event": event, "detail": detail})


def log_adaptability_hours(task: str, hours: float) -> None:
    """Section 2 — running log against the 5-hour adaptability cap."""
    _append_jsonl(settings.ADAPTABILITY_LOG, {"task": task, "hours": hours})
