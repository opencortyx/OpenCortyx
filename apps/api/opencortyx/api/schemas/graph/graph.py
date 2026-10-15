"""Request and response models: graph views and graph queries."""
from typing import Any, List, Optional, Union

from pydantic import BaseModel


class GraphQueryBody(BaseModel):
    query: Optional[Union[str, dict]] = None
    steps: Optional[List[Union[dict, Any]]] = None
    limit: int = 50
    returns: Optional[List[str]] = None
    collection_id: Optional[str] = None
