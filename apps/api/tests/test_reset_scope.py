"""D8: an organization's reset stops ITS tasks only — it never purges the shared broker queue."""
import importlib
import inspect

from opencortyx.api.routes.system import maintenance

worker = importlib.import_module("opencortyx.core.worker.celery_app")   # the module, not the Celery app


def test_revoke_tasks_touches_exactly_the_given_ids(monkeypatch):
    calls, purged = [], []
    monkeypatch.setattr(worker.celery_app.control, "revoke", lambda ids, terminate: calls.append((ids, terminate)))
    monkeypatch.setattr(worker.celery_app.control, "purge", lambda: purged.append(True))
    out = worker.revoke_tasks(["a", None, "b", ""])
    assert calls == [(["a", "b"], True)] and not purged
    assert out == {"revoked": 2, "errors": []}


def test_revoke_tasks_with_nothing_to_stop_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.celery_app.control, "revoke", lambda *a, **k: calls.append(a))
    assert worker.revoke_tasks([]) == {"revoked": 0, "errors": []} and not calls


def test_reset_is_scoped_to_the_callers_org_and_admin_only():
    src = inspect.getsource(maintenance.reset_database)
    assert "purge_task_queue(" not in src                      # no broker-wide purge
    assert "revoke_tasks" in src and "IngestRun.workspace_id == user.workspace_id" in src
    assert "TypeRegistry.workspace_id == user.workspace_id" in src         # only this org's ontology candidates
    sig = inspect.signature(maintenance.reset_database)
    assert sig.parameters["user"].default.dependency is maintenance.require_admin
