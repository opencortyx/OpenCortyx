"""The dashboard's cluster hierarchy — pure computation over plain lists, no DB.

What the page relies on, and what these tests pin: no screen ever shows more than FANOUT things,
every node is reachable from the top, the same graph gives the same cluster ids (so the page can keep
its place across a rebuild), and edges are counted exactly once — inside a cluster, or on the link
between the two sibling clusters where they are drawn.
"""
import random

from opencortyx.legacy.graph_clusters import build_hierarchy


def _clique(prefix, n, etype="REL"):
    ids = [f"{prefix}{i}" for i in range(n)]
    return ids, [(a, b, etype, 1.0) for i, a in enumerate(ids) for b in ids[i + 1:]]


def _planted(n_nodes=3000, n_comm=60, n_edges=12000, pairs=200, isolated=50, seed=3):
    r = random.Random(seed)
    nodes = {f"n{i}": (f"Node {i}", f"T{i % 4}") for i in range(n_nodes)}
    members = {}
    for i in range(n_nodes):
        members.setdefault(i % n_comm, []).append(i)
    edges = []
    for _ in range(n_edges):
        a = r.randrange(n_nodes)
        b = r.choice(members[a % n_comm]) if r.random() < 0.9 else r.randrange(n_nodes)
        edges.append((f"n{a}", f"n{b}", "REL", 1.0))
    for i in range(pairs):                        # many tiny disconnected components
        nodes[f"p{i}a"], nodes[f"p{i}b"] = (f"P{i}a", "Pair"), (f"P{i}b", "Pair")
        edges.append((f"p{i}a", f"p{i}b", "REL", 1.0))
    for i in range(isolated):
        nodes[f"iso{i}"] = (f"Iso {i}", "Orphan")
    return nodes, edges


def test_two_communities_split_along_the_weak_edge():
    a, ea = _clique("a", 6)
    b, eb = _clique("b", 6)
    nodes = {n: (n.upper(), "Concept") for n in a + b}
    h = build_hierarchy(nodes, ea + eb + [("a0", "b0", "BRIDGE", 1.0)], fanout=10)

    assert len(h.roots) == 2
    groups = sorted(sorted(h.clusters[r].members) for r in h.roots)
    assert groups == [sorted(a), sorted(b)]
    (link,) = h.links.values()
    assert link["parent"] == "" and link["edges"] == 1 and link["edge_types"] == {"BRIDGE": 1}


def test_no_level_is_wider_than_fanout_and_every_node_is_placed():
    nodes, edges = _planted()
    h = build_hierarchy(nodes, edges, fanout=40)

    assert len(h.roots) <= 40
    for c in h.clusters.values():
        assert len(c.children) <= 40
        assert not (c.children and c.members), "a cluster holds clusters or nodes, never both"
        assert c.size == len(c.members) + sum(h.clusters[k].size for k in c.children)
    assert set(h.member_of) == set(nodes)
    assert sum(h.clusters[r].size for r in h.roots) == len(nodes)
    assert h.depth <= 6, "bundling must grow with log(count), not with the count"


def test_nodes_without_edges_are_grouped_by_type():
    nodes = {"x": ("X", "Person"), "y": ("Y", "Person"), "z": ("Z", "Place")}
    h = build_hierarchy(nodes, [], fanout=10)
    by_label = {c.label: sorted(c.members) for c in h.clusters.values()}
    assert by_label == {"Unlinked · Person": ["x", "y"], "Unlinked · Place": ["z"]}


def test_every_edge_is_counted_once():
    nodes, edges = _planted(n_nodes=800, n_comm=20, n_edges=3000, pairs=30, isolated=5)
    h = build_hierarchy(nodes, edges, fanout=12)
    distinct = sum(1 for a, b, _, _ in edges if a != b)
    at_top = sum(h.clusters[r].internal for r in h.roots)
    between_top = sum(l["edges"] for l in h.links.values() if l["parent"] == "")
    assert at_top + between_top == distinct


def test_same_graph_same_ids():
    nodes, edges = _planted(n_nodes=600, n_comm=15, n_edges=2400, pairs=20, isolated=3)
    first = build_hierarchy(nodes, edges, fanout=10)
    second = build_hierarchy(nodes, list(reversed(edges)), fanout=10)
    assert sorted(first.clusters) == sorted(second.clusters)
    assert first.member_of == second.member_of


def test_cluster_is_named_after_its_best_connected_member():
    hub = [("hub", f"s{i}", "REL", 1.0) for i in range(8)]
    nodes = {"hub": ("Machine Learning", "Concept"), **{f"s{i}": (f"Spoke {i}", "Concept") for i in range(8)}}
    h = build_hierarchy(nodes, hub, fanout=20)
    (root,) = h.roots
    assert h.clusters[root].label == "Machine Learning"
    assert h.clusters[root].preview[0] == (8, "Machine Learning")
