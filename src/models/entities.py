"""Dataclasses mirroring the project's ten core business entities.

They describe data only; database saving and calculations belong elsewhere.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class InvestorProfile:
    investor_id: Optional[int]
    external_id: Optional[str]
    full_name: str
    email: str
    risk_profile: str
    investment_horizon_years: int


@dataclass
class Portfolio:
    portfolio_id: Optional[int]
    external_id: Optional[str]
    investor_id: int
    portfolio_name: str
    base_currency: str = "USD"
    available_cash: float = 0.0


@dataclass
class Instrument:
    instrument_id: Optional[int]
    external_id: Optional[str]
    ticker: str
    instrument_name: str
    asset_class: str
    currency: str = "USD"


@dataclass
class Holding:
    holding_id: Optional[int]
    external_id: Optional[str]
    portfolio_id: int
    instrument_id: int
    quantity: float
    average_cost: Optional[float] = None


@dataclass
class MarketPrice:
    market_price_id: Optional[int]
    instrument_id: int
    price_date: str
    closing_price: float
    average_daily_volume: Optional[float] = None


@dataclass
class TargetAllocation:
    target_allocation_id: Optional[int]
    portfolio_id: int
    instrument_id: int
    target_date: str
    target_weight: float


@dataclass
class Trade:
    trade_id: Optional[int]
    portfolio_id: int
    instrument_id: int
    trade_action: str
    recommended_quantity: float
    rationale: str
    proposal_id: Optional[int] = None


@dataclass
class FeeSchedule:
    fee_schedule_id: Optional[int]
    portfolio_id: int
    schedule_name: str
    fixed_fee: float
    percentage_fee: float
    effective_from: str


@dataclass
class Constraint:
    constraint_id: Optional[int]
    portfolio_id: int
    constraint_type: str
    description: str
    instrument_id: Optional[int] = None
    numeric_value: Optional[float] = None


@dataclass
class RebalanceProposal:
    proposal_id: Optional[int]
    portfolio_id: int
    proposal_date: str
    total_portfolio_value: float
    status: str = "draft"
