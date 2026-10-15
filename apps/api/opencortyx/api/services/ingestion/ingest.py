"""Services: file and JSON uploads, supported formats and ingestion integrity."""
import json

from fastapi import HTTPException


def parse_anchor_form(anchors: str | None, asset_path: str | None) -> dict:
    """A multipart `anchors` field → the extras every ingestion route folds into its options blob.

    Compiled here so a malformed glob is a 400 on the upload the user just made, rather than a
    per-file error in a worker log they will never read. Returns `{}` when no rules were sent, so
    callers can splat it unconditionally and an upload without anchors carries no empty keys.
    """
    out: dict = {}
    if anchors:
        from opencortyx.ingest.extraction import anchors as _anchors
        try:
            rules = json.loads(anchors)
            _anchors.parse_rules(rules)
        except _anchors.AnchorRuleError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"anchors is not valid JSON: {e}")
        if rules:
            out["anchors"] = rules
    if asset_path:
        out["asset_path"] = asset_path
    return out
