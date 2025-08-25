import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime

APP_TITLE = "Money Movement Tracker (DA)"
DATA_PATH = Path("data")
LEDGER_FILE = DATA_PATH / "ledger.csv"

COLUMNS = ["date", "person", "amount", "category", "note", "recorded_by"]

# ----------------------------- Utilities -----------------------------
def format_da(x: float) -> str:
    try:
        return f"{x:,.0f} DA".replace(",", " ")
    except Exception:
        return f"{x} DA"

@st.cache_data(show_spinner=False)
def load_ledger() -> pd.DataFrame:
    """Load ledger. If missing, create an EMPTY ledger (start fresh)."""
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    if LEDGER_FILE.exists():
        df = pd.read_csv(LEDGER_FILE)
    else:
        df = pd.DataFrame(columns=COLUMNS)
        save_ledger(df)
    # Normalize
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    if "amount" in df:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    # Ensure all columns exist
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[COLUMNS]

@st.cache_data(show_spinner=False)
def ledger_total(df: pd.DataFrame) -> float:
    return float(df.get("amount", pd.Series(dtype=float)).sum())

def save_ledger(df: pd.DataFrame) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    df.to_csv(LEDGER_FILE, index=False)

# ----------------------------- UI -----------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")

# ===== Modern & responsive look =====
st.markdown(
    """
    <style>
      .block-container {max-width: 1180px; padding-top: 2.5rem; padding-bottom: 2rem;}
      .app-header {font-size: 34px; font-weight: 900; margin: 20px 0 8px 0; color:#111827;}
      .subtitle {color: #374151; margin-bottom: 20px; font-size:15px}
      .card {border-radius: 18px; padding: 20px; background: linear-gradient(135deg,#0ea5e9, #22d3ee); color: white; box-shadow: 0 10px 25px rgba(0,0,0,0.08);} 
      .card h3 {margin: 0; font-size: 14px; opacity: .9; letter-spacing: .4px}
      .card .big {font-size: 40px; font-weight: 800; margin-top: 6px}
      .pill {display:inline-block; padding:4px 10px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:12px; font-weight:700}
      .hint {color:#6b7280; font-size:12px}
      @media (max-width: 900px) {
        .app-header {font-size: 28px}
        .card {padding: 16px; border-radius: 16px}
        .card .big {font-size: 32px}
      }
      @media (max-width: 640px) {
        .app-header {font-size: 22px}
        .subtitle {font-size: 13px}
        .card .big {font-size: 28px}
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="app-header">💸 Money Movement Tracker</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Track capital and expenses — Algerian dinar (DA)</div>', unsafe_allow_html=True)

# Data
ledger = load_ledger().copy()

# ===== Balance card only =====
with st.container():
    st.markdown(
        f"""
        <div class='card'>
            <h3>Total Balance</h3>
            <div class='big'>{format_da(ledger_total(ledger))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(" ")

# ===== Tabs =====
tab_add, tab_filter, tab_ledger, tab_backup = st.tabs(["➕ Add Movement", "🔍 Filter", "📄 Ledger", "🗂️ Backup"])

with tab_add:
    st.markdown("<span class='pill'>New entry</span>", unsafe_allow_html=True)
    st.write("")

    c1, c2 = st.columns([1, 1])
    with c1:
        date = st.date_input("Date", value=datetime.now().date())
        person = st.text_input("Person (who gives/pays)", value="Rayan")
        category = st.selectbox("Category", ["Capital", "Salary", "Purchase", "Services", "Rent", "Other"], index=0)
    with c2:
        flow = st.radio("Type", options=["Inflow", "Expense"], horizontal=True)
        base_amount = st.number_input("Amount", value=0, step=1000, format="%d")
        recorded_by = st.text_input("Recorded by", value="Rayan")
    note = st.text_area("Note", placeholder="e.g., Pay Farid (developer)")
    add = st.button("Add movement", type="primary")

    if add:
        signed = int(base_amount) if flow == "Inflow" else -abs(int(base_amount))
        new_row = {
            "date": date,
            "person": person.strip() or "-",
            "amount": float(signed),
            "category": category,
            "note": note.strip(),
            "recorded_by": recorded_by.strip() or "-",
        }
        ledger = pd.concat([ledger, pd.DataFrame([new_row])], ignore_index=True)
        save_ledger(ledger)
        st.success("✅ Movement added and saved.")
        st.cache_data.clear()
        st.rerun()

with tab_filter:
    st.markdown("<span class='pill'>Filters</span>", unsafe_allow_html=True)
    st.write("")
    f1, f2 = st.columns([1,1])
    with f1:
        start = st.date_input("From", value=None)
        who = st.text_input("Person contains")
    with f2:
        end = st.date_input("To", value=None)
        text = st.text_input("Note contains")

    mask = pd.Series([True]*len(ledger))
    if start:
        mask &= pd.to_datetime(ledger["date"]).dt.date >= start
    if end:
        mask &= pd.to_datetime(ledger["date"]).dt.date <= end
    if who:
        mask &= ledger["person"].str.contains(who, case=False, na=False)
    if text:
        mask &= ledger["note"].str.contains(text, case=False, na=False)

    st.session_state["_filtered_mask"] = mask
    st.info("Filters applied. Go to the **Ledger** tab to view results.")

with tab_ledger:
    st.markdown("<span class='pill'>All movements</span>", unsafe_allow_html=True)
    st.write("")

    mask = st.session_state.get("_filtered_mask", pd.Series([True]*len(ledger)))
    filtered = ledger[mask].copy()

    show_cols = ["date", "person", "category", "note", "amount", "recorded_by"]
    if not filtered.empty:
        filtered = filtered.sort_values(["date"]).reset_index(drop=True)
        st.dataframe(
            filtered[show_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "amount": st.column_config.NumberColumn(
                    "Amount (DA)", format="%d", step=1000, help="Positive = inflow, Negative = expense"
                ),
                "date": st.column_config.DateColumn("Date"),
            },
        )
        st.caption("Amounts shown in DA. Positive = money in, Negative = money out.")
    else:
        st.info("No rows to display with current filters.")

with tab_backup:
    st.markdown("<span class='pill'>Backup & Restore</span>", unsafe_allow_html=True)
    st.write("")
    colA, colB = st.columns(2)
    with colA:
        st.download_button(
            label="⬇️ Download CSV",
            data=ledger.to_csv(index=False).encode("utf-8"),
            file_name=f"ledger_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )
    with colB:
        uploaded = st.file_uploader("Upload CSV to replace ledger", type=["csv"], accept_multiple_files=False)
        if uploaded is not None:
            new_df = pd.read_csv(uploaded)
            # Basic validation
            required = set(COLUMNS)
            if not required.issubset(set(new_df.columns)):
                st.error(f"CSV must contain columns: {sorted(required)}")
            else:
                new_df["date"] = pd.to_datetime(new_df["date"]).dt.date
                new_df["amount"] = pd.to_numeric(new_df["amount"], errors="coerce").fillna(0.0)
                new_df = new_df[COLUMNS]
                save_ledger(new_df)
                st.success("Ledger replaced from uploaded CSV.")
                st.cache_data.clear()
                st.rerun()

# ===== No chart is drawn by request =====
# (Graph removed deliberately)
