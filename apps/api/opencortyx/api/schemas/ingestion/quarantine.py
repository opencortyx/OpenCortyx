"""Request and response models: extractions whose model output could not be parsed."""
from pydantic import BaseModel


class QuarantineRetrySpec(BaseModel):
    json_data: str
