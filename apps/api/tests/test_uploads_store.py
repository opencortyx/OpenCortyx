"""Uploads travel to workers as references to a stored file, never as bytes in the queue message.

Redis keeps every queued message in memory. With file bytes inside them, a few hundred files dropped
on Add data filled its container and the kernel killed it, losing the queue.
"""
import base64
import os
import time

import pytest

from opencortyx.core import uploads
from opencortyx.core.config import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_STORE", str(tmp_path))
    return tmp_path


def test_a_job_carries_a_short_reference_and_reads_the_bytes_back(store):
    content = os.urandom(3 * 1024 * 1024)
    ref = uploads.put(content)
    assert ref.startswith(uploads.PREFIX) and len(ref) < 100
    assert uploads.load(ref) == content


def test_the_same_file_is_stored_once(store):
    a, b = uploads.put(b"same bytes"), uploads.put(b"same bytes")
    assert a == b
    assert sum(1 for p in store.rglob("*") if p.is_file()) == 1


def test_jobs_queued_by_an_older_version_still_read(store):
    assert uploads.load(base64.b64encode(b"queued before the update").decode()) == b"queued before the update"


def test_a_removed_file_says_to_upload_it_again(store):
    ref = uploads.put(b"gone")
    for p in store.rglob("*"):
        if p.is_file():
            p.unlink()
    with pytest.raises(FileNotFoundError, match="Upload it again"):
        uploads.load(ref)


def test_the_sweep_keeps_what_an_unfinished_run_needs(store):
    old_needed, old_unneeded, fresh = uploads.put(b"a"), uploads.put(b"b"), uploads.put(b"c")
    past = time.time() - 10 * 24 * 3600
    for ref in (old_needed, old_unneeded):
        path = store / ref[len(uploads.PREFIX):][:2] / ref[len(uploads.PREFIX):]
        os.utime(path, (past, past))
    keep = uploads.digests_in({"args": ["ws", old_needed, "x.pdf"], "kwargs": {}})
    out = uploads.sweep(keep, older_than_hours=24)
    assert out["removed"] == 1
    assert uploads.load(old_needed) == b"a" and uploads.load(fresh) == b"c"
    with pytest.raises(FileNotFoundError):
        uploads.load(old_unneeded)


def test_uploads_have_their_own_rate_limit_budget():
    """Uploads shared the questions' 30 a minute, so every file after the 30th of a folder failed."""
    from opencortyx.core.security import ratelimit
    upload = ratelimit._class_of("/api/v2/sources/upload")
    question = ratelimit._class_of("/api/v2/context")
    assert upload[0] == "upload" and question[0] == "heavy"
    assert upload[1] >= 300
