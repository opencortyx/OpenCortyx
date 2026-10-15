"""Routes: glossary terms, synonyms, business rules and business context."""
import uuid

from fastapi import APIRouter, Depends

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.glossary import (
    BusinessContextSpec,
    GlossarySpec,
    RuleSpec,
    SynonymSpec,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.knowledge import authoring

router = APIRouter()


@router.post("/glossary")
async def add_glossary_ep(spec: GlossarySpec, user: CurrentUser = Depends(require_write)):
    res = await authoring.add_glossary(user.workspace_id, term=spec.term, definition=spec.definition,
                                       applies_to=spec.applies_to, targets=spec.targets, scope=spec.scope,
                                       status=spec.status, owner=spec.owner, source=spec.source,
                                       valid_from=spec.valid_from, valid_until=spec.valid_until,
                                       examples=spec.examples, author=user.user_id)
    await audit(user.workspace_id, user.user_id, "curate/glossary",
                node_id=(uuid.UUID(res["id"]) if isinstance(res, dict) and res.get("id") else None),
                meta={"term": spec.term})
    return res


@router.post("/rules")
async def add_rule_ep(spec: RuleSpec, user: CurrentUser = Depends(require_write)):
    res = await authoring.add_rule(user.workspace_id, title=spec.title, content=spec.content,
                                   applies_to=spec.applies_to, targets=spec.targets, severity=spec.severity,
                                   kind=spec.kind, scope=spec.scope, status=spec.status, owner=spec.owner,
                                   source=spec.source, valid_from=spec.valid_from, valid_until=spec.valid_until,
                                   author=user.user_id)
    await audit(user.workspace_id, user.user_id, "curate/rule",
                node_id=(uuid.UUID(res["id"]) if isinstance(res, dict) and res.get("id") else None),
                meta={"title": spec.title})
    return res


@router.post("/synonyms")
async def add_synonym_ep(spec: SynonymSpec, user: CurrentUser = Depends(require_write)):
    res = await authoring.add_synonym(user.workspace_id, term=spec.term, target_id=spec.target_id,
                                      targets=spec.targets, kind=spec.kind, scope=spec.scope,
                                      status=spec.status, owner=spec.owner, source=spec.source,
                                      author=user.user_id)
    await audit(user.workspace_id, user.user_id, "curate/synonym",
                node_id=(uuid.UUID(res["id"]) if isinstance(res, dict) and res.get("id") else None),
                meta={"term": spec.term, "target": spec.target_id})
    return res


@router.get("/business-context")
async def list_business_context_ep(user: CurrentUser = Depends(require_auth)):
    """Org/collection-level business context (mission, domain, standing assumptions). The always-on
    items are prepended to every answer's context as a frame (Settings → Retrieval)."""
    return {"items": await authoring.list_business_context(user.workspace_id)}


@router.post("/business-context")
async def add_business_context_ep(spec: BusinessContextSpec, user: CurrentUser = Depends(require_write)):
    res = await authoring.add_business_context(user.workspace_id, title=spec.title, content=spec.content,
                                               scope=spec.scope, collection_id=spec.collection_id,
                                               always_on=spec.always_on, kind=spec.kind,
                                               owner=spec.owner, source=spec.source, author=user.user_id)
    await audit(user.workspace_id, user.user_id, "curate/context",
                node_id=(uuid.UUID(res["id"]) if isinstance(res, dict) and res.get("id") else None),
                meta={"title": spec.title, "always_on": spec.always_on})
    return res
