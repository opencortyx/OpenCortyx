"""A run's result is written by two tasks: the ingest half, and the deferred half it queues.

The ingest half queues the deferred half and THEN records its own result, so a small document's
deferred half can finish first. Its report must survive the ingest half's write.
"""
from opencortyx.ingest.runs.progress import _keep_deferred_progress


def test_a_finished_deferred_half_is_not_overwritten_by_the_ingest_result():
    old = {"extraction": "done", "custom": 3}
    new = {"extraction": "deferred", "source_id": "s", "reader": "docling"}
    merged = _keep_deferred_progress(old, new)
    assert merged == {"extraction": "done", "custom": 3, "source_id": "s", "reader": "docling"}


def test_the_ingest_result_stands_when_nothing_was_deferred_or_reported():
    assert _keep_deferred_progress({}, {"extraction": "deferred", "reader": "docling"}) == \
        {"extraction": "deferred", "reader": "docling"}
    assert _keep_deferred_progress({"extraction": "done"}, {"status": "ok"}) == {"status": "ok"}
