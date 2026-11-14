# Labelled exception scenarios

These scenarios are intentionally documented in Phase 1. The detection logic
will be implemented with the valuation, rebalancing, and constraint services.

| Label | Meaning | Intended behaviour |
| --- | --- | --- |
| `missing_price` | An instrument has no usable latest price. | Do not value or trade it; log an anomaly. |
| `excluded_instrument` | A policy forbids the instrument. | Block the recommendation; explain why. |
| `minimum_trade_size` | A trade is too small under policy. | Change the recommendation to HOLD. |
| `liquidity_limit` | The trade is above an allowed liquidity cap. | Reduce or block the trade; log an anomaly. |
| `missing_target` | No target allocation exists. | Do not infer a target; flag the portfolio. |
| `zero_value_portfolio` | Portfolio value is zero. | Avoid division by zero; issue a labelled warning. |
| `insufficient_cash` | Cash cannot cover buy value plus fee. | Reduce or block BUY recommendations. |
| `invalid_fee_schedule` | No active fee schedule or invalid fee values. | Block the proposal with a clear reason. |
| `maximum_trade_value` | Trade exceeds an applicable dollar cap. | Treat as a liquidity-limit block. |

## Audit treatment

Constraint violations create append-only `CONSTRAINT_VIOLATION` audit events.
Anomalies remain in their separate database/export files; they are never fed
back into pricing, allocation, rebalancing, or simulation calculations.
