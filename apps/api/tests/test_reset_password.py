"""`opencortyx reset-password <email>`: the way back in for a locked-out admin.

Offline: the database calls are replaced. What is pinned is that nothing changes unless the new
password was typed, twice, at a hidden prompt, and that a wrong email says which ones exist.
"""
import pytest

from opencortyx import reset_password as rp


@pytest.fixture
def terminal(monkeypatch):
    """A terminal whose hidden prompt answers with `answers`, in order, and a database that records
    what it was asked to change."""
    calls = {"answers": [], "reset": []}

    class _Stdin:
        @staticmethod
        def isatty():
            return True

    async def _reset(email, password):
        calls["reset"].append((email, password))
        return email == "admin@example.com"

    async def _emails():
        return ["admin@example.com"]

    monkeypatch.setattr(rp.sys, "stdin", _Stdin())
    monkeypatch.setattr(rp.getpass, "getpass", lambda prompt="": calls["answers"].pop(0))
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "_emails", _emails)
    return calls


def test_the_password_is_set_when_typed_twice(terminal, capsys):
    terminal["answers"] = ["s3cret-pass", "s3cret-pass"]
    assert rp.main(["admin@example.com"]) == 0
    assert terminal["reset"] == [("admin@example.com", "s3cret-pass")]
    assert "Password changed" in capsys.readouterr().out


@pytest.mark.parametrize("answers,why", [
    (["short"], "at least 6 characters"),
    (["s3cret-pass", "different"], "differ"),
])
def test_nothing_changes_on_a_bad_password(terminal, capsys, answers, why):
    terminal["answers"] = answers
    assert rp.main(["admin@example.com"]) == 1
    assert terminal["reset"] == []
    assert why in capsys.readouterr().err


def test_an_unknown_email_lists_the_accounts(terminal, capsys):
    terminal["answers"] = ["s3cret-pass", "s3cret-pass"]
    assert rp.main(["who@example.com"]) == 1
    assert "Accounts here: admin@example.com" in capsys.readouterr().err


def test_the_password_is_never_an_argument_and_needs_a_terminal(monkeypatch, capsys):
    assert rp.main([]) == 2 and rp.main(["a@b.c", "password"]) == 2       # usage, no password argv

    class _Pipe:
        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(rp.sys, "stdin", _Pipe())
    assert rp.main(["admin@example.com"]) == 2
    assert "hidden prompt" in capsys.readouterr().err
