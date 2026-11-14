# AI usage and engineering decisions

## AI assistance

AI assistance was used to design, implement, explain, test, and document this
candidate project. The owner should review, run, and understand all code before
submission or demonstration.

## Validated in this workspace

- Automated Pytest was run after implementation changes.
- Starter data, a separate full database, and a second isolated full run were
  checked for counts, foreign keys, positive values, target totals, and seed
  reproducibility.
- End-to-end workflow ran on a cloned starter database so historical Proposal 1
  and its five trades stayed unchanged.

## Decisions and trade-offs

- Full data uses a separate SQLite file because generation is append-only and
  starter/full IDs overlap.
- Proposals/audit events are append-only; simulation is in-memory only.
- `Decimal` is used for calculations, while SQLite source money fields remain
  `REAL` for demonstration scope.
- Anomalies are separated from operational engine inputs.

## Known limitations

- This is educational software, not investment advice or order execution.
- Prices are synthetic, and a production system needs authentication, stronger
  monetary storage, migrations, role-based approval, and broker safeguards.
- Streamlit startup must be verified locally after dependencies are installed.
