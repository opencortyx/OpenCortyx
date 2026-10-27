"""Write-back: a fact an answer found through SQL becomes knowledge, with the query as its evidence.

Off by default (Settings > Retrieval > Answer write-back). When on, an answer that came from the data
route and passed the grounding checks is written once as a `Fact` node - idempotent by question - and
its evidence points at every asset the query read, with the SQL as the quote. The next time, the
graph answers it directly, and the fact still says where it came from and when.
"""
from __future__ import annotations

import hashlib
import uuid

from opencortyx.core.observability import get_logger

log = get_logger(__name__)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/answer-fact")


async def write_back(workspace_id, question: str, answer: str, data: dict) -> str | None:
    from opencortyx.core.dataplane import evidence
    from opencortyx.core.substrate import NodeRepo
    if not data or not data.get("sql") or not answer:
        return None
    nid = uuid.uuid5(_NS, f"{workspace_id}:{' '.join(question.lower().split())}")
    sql = str(data["sql"])
    key = "sql:" + hashlib.sha256(sql.encode()).hexdigest()[:16]
    try:
        await NodeRepo().upsert_node(node_id=nid, workspace_id=workspace_id, type="Fact", label=question[:200],
                                     properties={"statement": answer, "text": answer, "question": question,
                                                 "__source": "answer", "sql": sql})
        await evidence.record(workspace_id, [(nid, [
            evidence.Pointer(asset_id=a["asset_id"], unit_kind="row", unit_key=key,
                             version_ref=a.get("version_ref"), quote=sql[:1000], extractor="answer write-back")
            for a in data.get("assets") or [] if a.get("asset_id")])])
        return str(nid)
    except Exception as e:                                    # noqa: BLE001 - never fails an answer
        log.warning("writeback.failed", error=str(e)[:200])
        return None
