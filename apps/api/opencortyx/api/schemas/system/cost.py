"""Request and response models: LLM token spend and the monthly budget."""
from pydantic import BaseModel


class BudgetSpec(BaseModel):
    monthly_token_budget: int = 0
    alert_threshold: float = 0.8
    hard_cap: bool = False
    usd_per_1k_tokens: float = 0.0
