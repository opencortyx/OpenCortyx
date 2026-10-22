"""An upload is not failed for good by a moment of infrastructure trouble.

A graph store that restarted for a few seconds failed every file the worker picked up meanwhile,
each after two retries 30 s apart. Transient errors now back off over ~16 minutes; a fault of the
file itself keeps the task's usual retry.
"""
from types import SimpleNamespace

from celery.exceptions import SoftTimeLimitExceeded

from opencortyx.stores.graph.exceptions import GraphStoreConnectionError, GraphStoreOperationError
from opencortyx.tasks._shared import TRANSIENT_RETRIES, is_transient, retry_policy


def _task(retries: int):
    return SimpleNamespace(request=SimpleNamespace(retries=retries))


def test_store_hiccups_are_transient():
    assert is_transient(GraphStoreOperationError("[nebulagraph] Storage Error: RPC failure, probably timeout."))
    assert is_transient(GraphStoreConnectionError("cannot open NebulaGraph session pool"))
    assert is_transient(ConnectionResetError())
    # The graph service under memory pressure, and a vector store restarting, recover on their own.
    assert is_transient(GraphStoreOperationError("Used memory hits the high watermark(0.800000) of total system memory."))
    assert is_transient(GraphStoreOperationError("GraphMemoryExceeded: (-2600)"))
    assert is_transient(RuntimeError("Qdrant request failed: [Errno 111] Connection refused"))


def test_faults_of_the_file_are_not():
    assert not is_transient(ValueError("no parser matched"))
    assert not is_transient(SoftTimeLimitExceeded())


def test_transient_errors_back_off_and_others_keep_the_usual_retry():
    hiccup = GraphStoreOperationError("RPC failure")
    assert [retry_policy(_task(n), hiccup)[1] for n in range(6)] == [15, 30, 60, 120, 240, 480]
    assert retry_policy(_task(0), hiccup)[0] == TRANSIENT_RETRIES
    assert retry_policy(_task(0), ValueError("bad file"), 2) == (2, 30)
