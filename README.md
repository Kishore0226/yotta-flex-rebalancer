# YottaFlex Portfolio Rebalancing System

YottaFlex is an educational Python project that compares a portfolio's current allocation with targets, proposes policy-constrained BUY, SELL, or HOLD actions, and simulates their effect without executing trades.

## Features

- Reproducible starter and full synthetic data using seed `20261022`.
- SQLite database with foreign keys, audit events, and anomaly tracking.
- Decimal-based valuation, allocation drift, fees, constraints, and cash checks.
- Append-only proposals, trade recommendations, audit history, and timestamped CSV/JSON exports.
- Read-only post-trade simulation and a six-section Streamlit dashboard.

## Technology and architecture

Python, Streamlit, SQLite, Pandas, NumPy, Plotly, and Pytest. The Streamlit UI calls services for valuation, allocation, rebalancing, constraints, simulation, audit, and exports; services use SQLite repositories. See `docs/architecture.md` and `docs/data_dictionary.md`.

## Windows setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If activation is blocked, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the current shell, then activate again.

## Initialize and generate data

```powershell
python -m src.database.schema
python -m src.services.synthetic_data --mode starter
```

Starter mode creates 100 portfolios, 500 instruments and holdings, and 60 business days of prices.

## Full data, safely separated

The generator is append-only. Do not run full mode against a starter database because overlapping IDs would create a mixed dataset. Use a separate SQLite file:

```powershell
python -m src.services.synthetic_data --mode full --database-path data/yottaflex-full.db --no-export
```

Full mode creates 1,000 portfolios, 5,000 instruments and holdings, and 1,250,000 prices across 250 business days. Business data is reproducible; operational timestamps naturally differ between runs.

## Dashboard and tests

```powershell
python -m streamlit run app.py
python -m pytest
```

The dashboard includes Overview, Portfolio Analysis, Rebalancing, Post-Trade Simulation, Audit History, and Data Export. Proposal creation requires a confirmation checkbox plus button click; refreshes do not create proposals.

## Exports and workflow

```powershell
python -c "from src.services.export import export_database_snapshot; print(export_database_snapshot(mode='starter'))"
```

Each export uses a new timestamped folder with CSV, JSON, and `manifest.json`; decimals are serialized as strings. Anomalies remain separate from operational inputs. A normal workflow is: inspect analysis, explicitly create a proposal, simulate it read-only, review audit history, and export results.

## Limitations and disclaimer

This is an educational simulation, not investment advice or production order-execution software. It uses synthetic—not live—prices and has one held instrument per asset class in its synthetic design. SQLite source money fields are `REAL`, converted to `Decimal` for financial calculations. Generated databases, exports, virtual environments, caches, and secrets are ignored by Git.
