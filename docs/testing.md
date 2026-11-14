# Testing and validation

Run the full suite:

```powershell
python -m pytest
```

Tests cover synthetic generation, foreign keys, targets, valuation, allocation,
fees, constraints, proposals, simulation immutability, audit behavior, exports,
and dashboard-facing selector/allocation integration.

## Full-data validation

```powershell
python -m src.services.synthetic_data --mode full --database-path data/yottaflex-full.db --no-export
python -c "from src.database.connection import get_connection; from pathlib import Path; print(get_connection(Path('data/yottaflex-full.db')).execute('PRAGMA foreign_key_check').fetchall())"
```

Expected baseline counts: 1,000 investors/portfolios, 5,000 instruments and
holdings, 5,000 targets, 1,000 fee schedules, 1,200 constraints, and 1,250,000
prices. Compare deterministic business columns, not audit/creation timestamps.

## Dashboard validation

After installing dependencies, run `streamlit run app.py`. Confirm all six
sections load, selectors work, proposal generation needs confirmation,
simulation does not alter holdings/cash, and exports download.
