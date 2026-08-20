"""
Keep test runs out of the real ops logs.

logs/exceptions.jsonl and logs/conflicts.jsonl are contract deliverables (SOW
Section 3 exception report / Section 2 conflict log) that Genemco reviews as
evidence. The validator and merge tests deliberately trigger rejections and
conflicts, so without this fixture every `pytest` run would append fixture SKUs
like SIR07 to those files and muddy the real ingestion evidence.
"""
import pytest

from core.config import settings


@pytest.fixture(autouse=True)
def _isolate_ops_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXCEPTIONS_LOG", tmp_path / "exceptions.jsonl")
    monkeypatch.setattr(settings, "CONFLICTS_LOG", tmp_path / "conflicts.jsonl")
    monkeypatch.setattr(settings, "REVIEW_QUEUE", tmp_path / "review_queue.jsonl")
    yield
