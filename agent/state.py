from typing import TypedDict
from langgraph.graph import MessagesState


class HoldingState(TypedDict):
    ticker: str
    isin: str
    asset_type: str          # "etf_acc" | "etf_dist" | "stock" | "savings"
    units: float
    avg_purchase_price: float
    purchase_date: str
    current_price: float
    current_value: float
    unrealised_gain: float
    unrealised_gain_pct: float


class TaxPosition(TypedDict):
    ticker: str
    asset_type: str
    unrealised_gain: float
    tax_if_sold_now: float
    effective_rate_pct: float
    vorabpauschale_annual: float
    note: str


class TaxState(TypedDict):
    sparerpauschbetrag_used: float
    sparerpauschbetrag_remaining: float
    vorabpauschale_total_estimate: float
    exit_tax_warning: bool
    positions: list[TaxPosition]


class AgentState(MessagesState):
    # Flow control
    user_id: str
    current_node: str
    intake_step: int

    # User profile
    savings_total: float
    emergency_fund_floor: float
    investable_surplus: float
    monthly_investment_budget: float
    risk_profile: str
    tax_bracket: float
    is_married: bool

    # Portfolio
    holdings: list[HoldingState]
    prices: dict                 # {ticker: live_price} — set by the fetch_prices node
    total_invested: float
    total_current_value: float
    total_unrealised_gain: float
    allocation: dict

    # Tax
    tax: TaxState

    # Tool-node scratch space (RAG + projection), consumed by answer_node
    retrieved_context: str
    projection_context: str

    # Strategy
    approved_strategy: dict
    monthly_split: dict
    strategy_saved: bool
