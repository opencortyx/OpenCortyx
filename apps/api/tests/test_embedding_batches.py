"""The in-process embedder batches by size, so one call cannot take a worker past its memory limit."""
from opencortyx.core.inference.embedding import _char_budget_groups


def test_every_text_is_kept_in_order():
    texts = ["a" * n for n in (10, 2000, 30, 8000, 5, 2000, 2000, 2000, 2000)]
    assert [t for group in _char_budget_groups(texts, 8000) for t in group] == texts


def test_each_group_fits_the_budget():
    texts = ["x" * n for n in (2000,) * 9 + (8000, 100, 100)]
    for group in _char_budget_groups(texts, 8000):
        assert len(group) == 1 or len(group) * max(map(len, group)) <= 8000


def test_a_text_longer_than_the_budget_is_embedded_alone():
    assert _char_budget_groups(["y" * 9000, "z"], 8000) == [["y" * 9000], ["z"]]


def test_short_queries_share_one_batch():
    assert len(_char_budget_groups(["q" * 40] * 100, 8000)) == 1


def test_empty_texts_still_count():
    assert _char_budget_groups(["", "", ""], 2) == [["", ""], [""]]
    assert _char_budget_groups([], 8000) == []


def test_texts_are_embedded_shortest_first_and_returned_in_input_order(monkeypatch):
    import asyncio

    from opencortyx.core.inference import embedding as E
    runs = []

    class Model:
        def embed(self, group, batch_size):
            runs.append(list(group))
            return [[float(len(t))] for t in group]
    monkeypatch.setattr(E, "_get_fastembed_model", lambda model=None: Model())
    labels = [f"label {i}" for i in range(40)]
    texts = ["p" * 1500] + labels[:20] + ["q" * 1400] + labels[20:]
    out = asyncio.run(E._fastembed_embed_batch(texts))
    assert out == [[float(len(t))] for t in texts]
    # Labels are not padded to a passage's length: they run together, the passages apart.
    assert sorted(runs[0]) == sorted(labels) and runs[1:] == [["q" * 1400, "p" * 1500]]
