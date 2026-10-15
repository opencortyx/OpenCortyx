"""Legacy code: kept for reference and reuse, wired into nothing.

Nothing outside this package imports it: no route is registered, no task is scheduled, and no page
calls it. Bringing a piece back means importing it from where it is needed and wiring it deliberately.

  graph_clusters.py   The Leiden community hierarchy the dashboard graph used to draw (~40 clusters
                      per screen, opened in place). Replaced by the browse tree
                      (`knowledge/graph_tree.py`) on 2026-09-28. Needs `graspologic-native`.
  cluster_routes.py   Its HTTP routes (/graph/clusters…), not included in the API router.
  cluster_tasks.py    Its rebuild task and debounce, not included in the worker.

The tables it wrote (`graph_cluster*`, migration 0025, `domain/graph_clusters.py`) are left in place:
dropping them is a data migration, to be decided on its own.
"""
