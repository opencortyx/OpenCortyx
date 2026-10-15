"""Services: graph views and graph queries."""



def trim_props(props: dict) -> dict:
    """Compact a node's properties for hover tooltips (cap big text, keep it small)."""
    out = {}
    for k, v in (props or {}).items():
        if isinstance(v, str):
            out[k] = v[:240]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = v[:8]
        else:
            out[k] = str(v)[:240]
    return out


_LIGHT_PROP_KEYS = ("__source", "__concept", "__template", "domain")


def light_props(props: dict) -> dict:
    """Minimal per-node properties for the dashboard overview — just the identity flags the client
    filters/styles by. Full properties come from getNode() when a node is actually opened."""
    p = props or {}
    return {k: p[k] for k in _LIGHT_PROP_KEYS if p.get(k) is not None}

