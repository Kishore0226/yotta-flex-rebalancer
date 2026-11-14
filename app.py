# """Streamlit dashboard for the YottaFlex portfolio rebalancing demonstration."""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from src.config import EXPORT_DIRECTORY, SYNTHETIC_AS_OF_DATE
from src.database.connection import get_connection
from src.database.repositories import list_portfolios, list_proposals
from src.services.allocation import analyze_allocation
from src.services.audit import list_audit_events
from src.services.export import export_database_snapshot
from src.services.rebalancer import generate_rebalance_proposal
from src.services.simulation import simulate_proposal


def money(value: object) -> str:
    """Format database/service numbers as readable USD amounts."""
    return f"USD {Decimal(str(value or 0)):,.2f}"


def percent(value: object) -> str:
    """Format a fractional allocation as a percentage."""
    return f"{Decimal(str(value or 0)) * 100:,.2f}%"


def rows_to_frame(rows: list[object]) -> pd.DataFrame:
    """Convert dataclasses or dictionaries to a DataFrame with Decimal strings."""
    records = [asdict(row) if not isinstance(row, dict) else row for row in rows]
    return pd.DataFrame(json.loads(json.dumps(records, default=lambda item: format(item, "f") if isinstance(item, Decimal) else item.isoformat())))


def show_table(data: pd.DataFrame) -> None:
    """Render a safe HTML table without Streamlit's PyArrow serializer.

    Some managed Windows computers block PyArrow's native DLL. Converting the
    small dashboard tables to HTML lets the app work without that blocked DLL.
    """
    if data.empty:
        st.info("No rows are available.")
        return
    st.markdown(data.to_html(index=False, escape=True, classes="yottaflex-table"), unsafe_allow_html=True)


def select_portfolio(key: str) -> int | None:
    """Render a shared portfolio selector and return its database ID."""
    portfolios = list_portfolios()
    if not portfolios:
        st.info("No portfolios are available. Generate starter data before using the dashboard.")
        return None
    mapping = {f"{item['portfolio_id']} — {item['portfolio_name']}": item["portfolio_id"] for item in portfolios}
    label = st.selectbox("Portfolio", list(mapping), key=key)
    return mapping[label]


def allocation_frame(analysis) -> pd.DataFrame:
    """Create display-friendly allocation/drift rows without changing calculations."""
    frame = rows_to_frame(list(analysis.rows))
    if frame.empty:
        return frame
    frame["current_value"] = frame["current_value"].map(money)
    for column in ("current_weight", "target_weight", "drift"):
        frame[column] = frame[column].map(percent)
    return frame.rename(columns={
        "asset_class": "Asset class", "current_value": "Current value", "current_weight": "Current weight",
        "target_weight": "Target weight", "drift": "Drift", "status": "Status",
    })


def overview() -> None:
    st.title("Portfolio overview")
    portfolio_id = select_portfolio("overview_portfolio")
    if portfolio_id is None:
        return
    analysis = analyze_allocation(portfolio_id, SYNTHETIC_AS_OF_DATE)
    with get_connection() as connection:
        cash = connection.execute("SELECT available_cash FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)).fetchone()[0]
        proposal_count = connection.execute("SELECT COUNT(*) FROM rebalance_proposals WHERE portfolio_id = ?", (portfolio_id,)).fetchone()[0]
        fees = connection.execute("SELECT COALESCE(SUM(estimated_fee), 0) FROM trades WHERE portfolio_id = ?", (portfolio_id,)).fetchone()[0]
        instrument_count = connection.execute("SELECT COUNT(DISTINCT instrument_id) FROM holdings WHERE portfolio_id = ?", (portfolio_id,)).fetchone()[0]
    columns = st.columns(5)
    columns[0].metric("Holdings value", money(analysis.valuation.total_portfolio_value))
    columns[1].metric("Account cash", money(cash))
    columns[2].metric("Holdings", len(analysis.valuation.holdings))
    columns[3].metric("Instruments", instrument_count)
    columns[4].metric("Proposal fees", money(fees))
    st.caption(f"Saved proposals: {proposal_count}. Holdings value excludes separate account cash.")
    rows = rows_to_frame(list(analysis.rows))
    if not rows.empty:
        st.plotly_chart(px.pie(rows, values="current_value", names="asset_class", title="Current allocation by asset class"), use_container_width=True)
    st.subheader("Recent audit events")
    events = list_audit_events()[-10:]
    if events:
        show_table(pd.DataFrame([{"Time": event.created_at, "Type": event.event_type, "Details": json.dumps(event.event_details)} for event in events]))
    else:
        st.info("No audit events are available yet.")


def portfolio_analysis() -> None:
    st.title("Portfolio analysis")
    portfolio_id = select_portfolio("analysis_portfolio")
    if portfolio_id is None:
        return
    valuation_date = st.date_input("Valuation date", SYNTHETIC_AS_OF_DATE, key="analysis_date")
    analysis = analyze_allocation(portfolio_id, valuation_date)
    valuation = analysis.valuation
    if valuation.missing_price_count:
        st.error(f"{valuation.missing_price_count} holding(s) have missing prices. Allocation may be incomplete.")
    if valuation.stale_price_count:
        st.warning(f"{valuation.stale_price_count} holding(s) use stale prices.")
    st.metric("Price coverage", f"{valuation.price_coverage_percentage}%")
    raw = rows_to_frame(list(analysis.rows))
    if raw.empty:
        st.info("No allocation data is available for this portfolio/date.")
        return
    chart = raw.melt(id_vars="asset_class", value_vars=["current_weight", "target_weight"], var_name="Measure", value_name="Weight")
    st.plotly_chart(px.bar(chart, x="asset_class", y="Weight", color="Measure", barmode="group", title="Current versus target allocation"), use_container_width=True)
    st.plotly_chart(px.bar(raw, x="asset_class", y="drift", color="status", title="Allocation drift"), use_container_width=True)
    show_table(allocation_frame(analysis))


def rebalancing() -> None:
    st.title("Rebalancing recommendations")
    st.warning("Recommendations are simulated policy outputs. They do not execute real trades or alter holdings/cash.")
    portfolio_id = select_portfolio("rebalance_portfolio")
    if portfolio_id is None:
        return
    valuation_date = st.date_input("Proposal valuation date", SYNTHETIC_AS_OF_DATE, key="rebalance_date")
    confirmed = st.checkbox("I confirm that I want to create a new saved proposal.", key="proposal_confirm")
    if st.button("Generate new proposal", type="primary", disabled=not confirmed):
        try:
            result = generate_rebalance_proposal(portfolio_id, valuation_date)
            st.session_state["latest_proposal"] = result
            st.success(f"Created Proposal {result.proposal_id}. No trade was executed.")
        except Exception as error:
            st.error(f"Proposal could not be created: {error}")
    proposals = list_proposals(portfolio_id)
    if not proposals:
        st.info("No saved proposals exist for this portfolio yet.")
        return
    proposal_map = {f"Proposal {row['proposal_id']} — {row['proposal_date']} ({row['status']})": row["proposal_id"] for row in proposals}
    proposal_id = proposal_map[st.selectbox("View saved proposal", list(proposal_map), key="proposal_view")]
    with get_connection() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM trades WHERE proposal_id = ? ORDER BY trade_id", (proposal_id,))]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        show_table(frame)
        st.subheader("Blocked recommendations")
        blocked = frame[frame["trade_status"] == "blocked"]
        show_table(blocked if not blocked.empty else pd.DataFrame({"Message": ["No recommendations were blocked."]}))


def post_trade_simulation() -> None:
    st.title("Post-trade simulation")
    st.info("This is an in-memory simulation. Original database holdings and account cash remain unchanged.")
    portfolio_id = select_portfolio("simulation_portfolio")
    if portfolio_id is None:
        return
    proposals = list_proposals(portfolio_id)
    if not proposals:
        st.info("Create a proposal first, then return here to simulate it.")
        return
    proposal_map = {f"Proposal {row['proposal_id']} — {row['proposal_date']}": row["proposal_id"] for row in proposals}
    proposal_id = proposal_map[st.selectbox("Saved proposal", list(proposal_map), key="simulation_proposal")]
    valuation_date = st.date_input("Simulation valuation date", SYNTHETIC_AS_OF_DATE, key="simulation_date")
    if st.button("Run read-only simulation"):
        try:
            st.session_state["simulation_result"] = simulate_proposal(portfolio_id, proposal_id, valuation_date)
        except Exception as error:
            st.error(f"Simulation could not run: {error}")
    result = st.session_state.get("simulation_result")
    if result is None or result.portfolio_id != portfolio_id or result.proposal_id != proposal_id:
        return
    metrics = st.columns(4)
    metrics[0].metric("Original value", money(result.original_portfolio_value))
    metrics[1].metric("Simulated value", money(result.simulated_portfolio_value))
    metrics[2].metric("Remaining cash", money(result.simulated_remaining_cash))
    metrics[3].metric("Total fees", money(result.total_fees))
    frame = rows_to_frame(list(result.holdings))
    if not frame.empty:
        show_table(frame)
    if result.blocked_trades:
        st.warning("Some recommendations were blocked during simulation.")
        show_table(rows_to_frame(list(result.blocked_trades)))
    else:
        st.success("All proposed BUY/SELL recommendations passed the simulation checks.")


def audit_history() -> None:
    st.title("Audit history")
    events = list_audit_events()
    if not events:
        st.info("No audit events are available yet.")
        return
    types = sorted({event.event_type for event in events})
    selected_types = st.multiselect("Event types", types, default=types)
    portfolio_text = st.text_input("Portfolio ID (optional)")
    after_date = st.date_input("Events on or after", value=None)
    filtered = []
    for event in events:
        if event.event_type not in selected_types:
            continue
        if portfolio_text and str(event.portfolio_id) != portfolio_text.strip():
            continue
        if after_date and event.created_at[:10] < after_date.isoformat():
            continue
        filtered.append({"Event ID": event.audit_event_id, "Time": event.created_at, "Type": event.event_type, "Portfolio": event.portfolio_id, "Proposal": event.proposal_id, "Trade": event.trade_id, "Details": json.dumps(event.event_details)})
    show_table(pd.DataFrame(filtered))


def data_export() -> None:
    st.title("Data export")
    st.caption("Exports always use a new timestamped folder; existing files are never overwritten.")
    if st.button("Create database snapshot"):
        try:
            st.success(f"Created {export_database_snapshot(mode='starter')}")
        except Exception as error:
            st.error(f"Snapshot could not be created: {error}")
    folders = sorted((path for path in EXPORT_DIRECTORY.iterdir() if path.is_dir()), reverse=True) if EXPORT_DIRECTORY.exists() else []
    if not folders:
        st.info("No timestamped export folders are available yet.")
        return
    folder = st.selectbox("Export folder", folders, format_func=lambda path: path.name)
    st.write("Database snapshots contain source tables; operational results contain valuation, recommendations, and simulation outputs.")
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".csv", ".json"}:
            continue
        st.download_button(f"Download {path.name}", data=path.read_bytes(), file_name=path.name, key=f"download_{folder.name}_{path.name}")


def main() -> None:
    """Configure the professional UI and route to the selected page."""
    st.set_page_config(page_title="YottaFlex Rebalancer", page_icon="📈", layout="wide")
    st.markdown("""<style>
        .stApp {background: #f6f8fb; color: #1f2937;}
        .stApp h1, .stApp h2, .stApp h3 {color: #0f2d52;}
        .stApp p, .stApp label, .stApp span, .stApp li {color: #334155;}
        [data-testid='stMetric'] {background: white; padding: 14px; border-radius: 10px;}
        [data-testid='stMetricLabel'] {color: #2563eb;}
        [data-testid='stMetricValue'] {color: #0f2d52;}
        [data-testid='stSidebar'] {background: #0f2d52;}
        [data-testid='stSidebar'] *, [data-testid='stSidebar'] p, [data-testid='stSidebar'] span {color: #eaf2ff;}
        .yottaflex-table {width:100%; border-collapse:collapse; background:white; color:#1f2937;}
        .yottaflex-table th,.yottaflex-table td {padding:8px; border-bottom:1px solid #e5e7eb; text-align:left; font-size:0.9rem;}
        .yottaflex-table th {background:#eff6ff; color:#0f2d52;}
    </style>""", unsafe_allow_html=True)
    st.sidebar.title("YottaFlex AI")
    page = st.sidebar.radio("Navigation", ["Overview", "Portfolio Analysis", "Rebalancing", "Post-Trade Simulation", "Audit History", "Data Export"])
    try:
        {"Overview": overview, "Portfolio Analysis": portfolio_analysis, "Rebalancing": rebalancing, "Post-Trade Simulation": post_trade_simulation, "Audit History": audit_history, "Data Export": data_export}[page]()
    except Exception as error:
        st.error(f"The dashboard could not load this view: {error}")


if __name__ == "__main__":
    main()
