"""Formatting helpers will be added when results are displayed."""


def format_currency(value: float, currency: str = "USD") -> str:
    """Format a number for human-readable financial output."""
    return f"{currency} {value:,.2f}"
