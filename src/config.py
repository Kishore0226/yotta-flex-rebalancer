"""Central project settings used across the application."""

from datetime import date
from pathlib import Path

# A fixed seed makes future synthetic data repeatable for demos and tests.
RANDOM_SEED = 20261022

# A fixed date is used alongside the seed so generated price histories are
# identical regardless of the day on which the generator is run.
SYNTHETIC_AS_OF_DATE = date(2026, 10, 22)

# A price older than this number of calendar days is still shown, but is
# explicitly labelled STALE in a valuation result.
STALE_PRICE_THRESHOLD_DAYS = 5

# Small differences inside 0.5 percentage points do not justify a trade.
ALLOCATION_DRIFT_TOLERANCE = 0.005

# This is the yottaflex-rebalancer directory, calculated from this file's location.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIRECTORY = PROJECT_ROOT / "data"
EXPORT_DIRECTORY = DATA_DIRECTORY / "exports"
DATABASE_PATH = DATA_DIRECTORY / "yottaflex.db"
