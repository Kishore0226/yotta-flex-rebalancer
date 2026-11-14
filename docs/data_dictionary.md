# Data dictionary

| Table | Purpose | Key relationship |
| --- | --- | --- |
| `investor_profiles` | Investor identity and risk profile. | One investor has many portfolios. |
| `portfolios` | Account currency and available cash. | Belongs to an investor. |
| `instruments` | Synthetic tradable asset metadata. | Used by holdings, prices, targets, and trades. |
| `holdings` | Instrument quantity/cost in a portfolio. | Unique by portfolio/instrument. |
| `market_prices` | Dated close and liquidity proxy. | Unique by instrument/date. |
| `target_allocations` | Desired dated instrument weights. | Aggregates to asset-class targets. |
| `fee_schedules` | Fixed and percentage trade fees. | Effective for a portfolio/date. |
| `constraints` | Policy rules such as exclusions/caps. | Belongs to a portfolio. |
| `rebalance_proposals` | Saved recommendation set. | Belongs to a portfolio. |
| `trades` | BUY, SELL, or HOLD recommendation. | Links proposal, portfolio, instrument. |
| `audit_events` | Append-only operational history. | Optional portfolio/proposal/trade links. |
| `anomalies` | Separate labelled exception data. | Never engine input. |

Integer primary keys enforce relationships. Human-readable IDs support demos:
`INV-000001`, `PORT-INV-000001-01`, `INS-000001`, and
`HOLD-<portfolio-id>-<instrument-id>`.

SQLite source money fields are `REAL`; financial services convert via text to
`Decimal`, round currency to cents, and round trade quantities down to four
decimal places.
