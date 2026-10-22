"""Booking the browse tree's index pass: debounced, and spaced by how long the last pass took."""
import pytest

from opencortyx.tasks import graph_tree


class _Redis:
    def __init__(self):
        self.keys = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True


@pytest.fixture
def booked(monkeypatch):
    import redis
    fake, delays = _Redis(), []
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url: fake))
    monkeypatch.setattr(graph_tree.v2_index_graph_tree, "apply_async",
                        lambda args, countdown: delays.append(countdown))
    monkeypatch.setattr(graph_tree.settings, "GRAPH_TREE_DEBOUNCE_S", 20)
    return fake, delays


def test_a_change_books_one_pass_after_the_debounce(booked):
    fake, delays = booked
    assert graph_tree.schedule_tree_index("w") and not graph_tree.schedule_tree_index("w")
    assert delays == [20]


def test_a_follow_up_waits_for_a_multiple_of_the_last_pass_capped(booked):
    fake, delays = booked
    graph_tree.schedule_tree_index("a", delay=90 * graph_tree.FOLLOW_UP_FACTOR)
    graph_tree.schedule_tree_index("b", delay=10 ** 6)
    graph_tree.schedule_tree_index("c", delay=3)
    assert delays == [450, 30 * 60, 20]


def test_now_skips_the_wait(booked):
    fake, delays = booked
    graph_tree.schedule_tree_index("w", now=True, delay=900)
    assert delays == [0]
