# Architecture and table relationships

## Layers

The project separates responsibilities so that a beginner can change one part
without accidentally breaking another:

- `models/` describes the business concepts as Python dataclasses.
- `database/` opens SQLite safely and creates the database schema.
- `services/` will contain calculations and business rules in later phases.
- `app.py` will eventually start the Streamlit dashboard.

## Entity relationships

```text
InvestorProfile (1) ──< Portfolio (many)
Portfolio (1) ──< Holding (many) >── (1) Instrument
Instrument (1) ──< MarketPrice (many)
Portfolio (1) ──< TargetAllocation (many) >── (1) Instrument
Portfolio (1) ──< FeeSchedule (many)
Portfolio (1) ──< Constraint (many) >── (0..1) Instrument
Portfolio (1) ──< RebalanceProposal (many) ──< Trade (many)
Portfolio (1) ──< Trade (many) >── (1) Instrument
AuditEvent may refer to a Portfolio, RebalanceProposal, and/or Trade.
Anomaly may refer to a Portfolio, Instrument, and/or RebalanceProposal.
```

## Database integrity

Every main table has an integer primary key. Foreign keys stop records from
pointing to non-existent parents. SQLite foreign-key enforcement is enabled in
`src/database/connection.py` for every connection. `CHECK` constraints reject
invalid values such as negative quantities, invalid trade actions, or target
weights outside zero to one.

`audit_events` preserves an event history. `anomalies` records labelled
business exceptions without preventing the system from explaining them later.

## Audit and export boundary

`services/audit.py` only appends to `audit_events`. Since the Phase 1 table has
no dedicated entity columns, `entity_type` and `entity_id` are stored in the
JSON `event_details` field; portfolio, proposal, and trade foreign keys use the
existing nullable columns.

`services/export.py` creates a new timestamped folder for each export. It never
reuses a folder name, so older evidence remains intact. Each table is exported
to matching CSV and JSON files, with a manifest recording timestamp, seed,
mode, source, and row counts. `anomalies` remains separate exception data.

## Allocation, simulation, and dashboard

`services/allocation.py` combines valuation with the latest target allocation
on or before the selected date, returning current weights, targets, drift, and
`OVERWEIGHT`, `UNDERWEIGHT`, or `ON_TARGET` statuses.

`services/simulation.py` applies eligible saved SELLs before BUYs in memory,
rechecks price status, quantities, cash, fees, exclusions, and trade caps, and
never changes holdings, cash, prices, or trade records. `app.py` is a thin
Streamlit presentation layer using the existing services.

## Full-data isolation

The generator uses `INSERT OR IGNORE`, so it appends rather than resets. Run
full mode in a separate SQLite file; starter/full deterministic IDs overlap.
Generated databases and bulk exports are excluded from version control.
