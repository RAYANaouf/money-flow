# Streamlit + ERPNext login, Multi-company picker ("All Companies"), colored lines, TTC by Date
import streamlit as st
import requests
import pandas as pd
import json
from datetime import date
from dateutil.relativedelta import relativedelta

# Optional (nicer chart). If missing, we fall back to st.line_chart.
try:
    import altair as alt
except Exception:
    alt = None

st.set_page_config(page_title="ERPNext Login • TTC by Date (Multi-company)", layout="wide")

BASE = st.secrets["erpnext"]["base_url"].rstrip("/")
VERIFY_SSL = st.secrets["erpnext"].get("verify_ssl", True)

# ---------------- Session State ----------------
if "cookies" not in st.session_state:
    st.session_state.cookies = None
if "user" not in st.session_state:
    st.session_state.user = None
if "date_range" not in st.session_state:
    st.session_state.date_range = (
        date.today() - relativedelta(months=3),
        date.today()
    )

def new_session() -> requests.Session:
    s = requests.Session()
    if st.session_state.cookies:
        s.cookies.update(st.session_state.cookies)
    return s

def login(usr: str, pwd: str) -> bool:
    s = requests.Session()
    r = s.post(
        f"{BASE}/api/method/login",
        data={"usr": usr, "pwd": pwd},
        timeout=30,
        verify=VERIFY_SSL,
    )
    if r.status_code == 200 and ("sid" in s.cookies.get_dict()):
        st.session_state.cookies = s.cookies.get_dict()
        st.session_state.user = usr
        return True
    try:
        st.error(r.json())
    except Exception:
        st.error(f"Login failed (HTTP {r.status_code}).")
    return False

def logout():
    try:
        s = new_session()
        s.get(f"{BASE}/api/method/logout", timeout=15, verify=VERIFY_SSL)
    except Exception:
        pass
    st.session_state.cookies = None
    st.session_state.user = None
    st.cache_data.clear()

def api_get(path: str, **params):
    s = new_session()
    r = s.get(f"{BASE}{path}", params=params or None, timeout=60, verify=VERIFY_SSL)
    if r.status_code in (401, 403):
        st.warning("Session expired or not authorized. Please log in again.")
        logout()
        st.stop()
    r.raise_for_status()
    return r.json()["data"]

# ---------------- Data Fetchers ----------------
@st.cache_data(show_spinner=False)
def list_companies(user_key: str):
    data = api_get(
        "/api/resource/Company",
        fields=json.dumps(["name"]),
        order_by="name asc",
        limit_page_length=1000,
    )
    return [row["name"] for row in data]

@st.cache_data(show_spinner=True)
def fetch_sales_invoices(company: str, start: date, end: date, include_drafts: bool, user_key: str):
    # Ensure order
    if start > end:
        start, end = end, start

    filters = [
        ["company", "=", company],
        ["posting_date", ">=", str(start)],
        ["posting_date", "<=", str(end)],
    ]
    if include_drafts:
        filters.append(["docstatus", "in", [0, 1]])  # drafts + submitted
    else:
        filters.append(["docstatus", "=", 1])        # submitted only

    params = {
        "fields": json.dumps(["name", "posting_date", "company", "base_grand_total", "currency"]),
        "filters": json.dumps(filters),
        "order_by": "posting_date asc, name asc",
        "limit_page_length": 5000,
    }

    rows, start_idx = [], 0
    while True:
        page = api_get("/api/resource/Sales Invoice", **{**params, "limit_start": start_idx})
        rows.extend(page)
        if len(page) < 5000:
            break
        start_idx += 5000

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["posting_date"] = pd.to_datetime(df["posting_date"])
    df["TTC"] = df["base_grand_total"]  # company currency
    return df

# ---------------- UI ----------------
st.title("ERPNext Login • TTC by Date (Multi-company)")

if not st.session_state.user:
    st.subheader("Log in to ERPNext")
    with st.form("login_form", clear_on_submit=False):
        usr = st.text_input("Email / Username", value="", autocomplete="username")
        pwd = st.text_input("Password", type="password", value="", autocomplete="current-password")
        colA, colB = st.columns([1, 1])
        submit = colA.form_submit_button("Log in")
        colB.caption("Your password is not stored; only a session cookie is kept in memory.")
    if submit:
        if login(usr, pwd):
            st.success(f"Logged in as {st.session_state.user}")
            st.rerun()
    st.stop()

st.success(f"Logged in as {st.session_state.user}")
if st.button("Logout"):
    logout()
    st.rerun()

with st.sidebar:
    st.header("Filters")

    # Companies list
    try:
        companies = list_companies(st.session_state.user or "")
    except Exception as e:
        st.error(f"Cannot load companies: {e}")
        st.stop()
    if not companies:
        st.error("No companies available for this user.")
        st.stop()

    # Multiselect with "All Companies"
    options = ["All Companies"] + companies
    default_selection = ["All Companies"]  # preselect "All" for convenience
    selected = st.multiselect("Companies", options, default=default_selection, key="companies_multi")

    # Resolve "All Companies"
    if "All Companies" in selected:
        selected_companies = companies[:]  # all
    else:
        selected_companies = list(dict.fromkeys([c for c in selected if c in companies]))

    # Date range (stable via session_state)
    start_date, end_date = st.date_input(
        "Date range",
        value=st.session_state.date_range,
        key="date_range",
    )
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        st.error("Please select a valid start and end date.")
        st.stop()
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    include_drafts = st.toggle("Include Draft Invoices (testing)", value=False, key="include_drafts")
    run = st.button("Load data")

if run:
    if not selected_companies:
        st.warning("Please select at least one company.")
        st.stop()

    all_invoices = []  # invoice-level rows for all companies
    all_daily = []     # daily TTC per company

    try:
        for co in selected_companies:
            df_co = fetch_sales_invoices(co, start_date, end_date, include_drafts, st.session_state.user or "")
            if df_co.empty:
                continue
            all_invoices.append(df_co)

            # Daily aggregation for this company
            daily_co = (
                df_co.set_index("posting_date")
                     .resample("D")["TTC"]
                     .sum()
                     .reset_index()
                     .rename(columns={"posting_date": "date"})
            )
            daily_co["company"] = co
            all_daily.append(daily_co)

        if not all_invoices:
            st.info("No invoices found for the selected filters.")
            st.stop()

        invoices_df = pd.concat(all_invoices, ignore_index=True)
        daily_df = pd.concat(all_daily, ignore_index=True) if all_daily else pd.DataFrame(columns=["date","TTC","company"])
        # Ensure correct dtypes
        if not daily_df.empty:
            daily_df["date"] = pd.to_datetime(daily_df["date"])

        # ----- KPIs -----
        total_ttc = float(invoices_df["TTC"].sum())
        inv_count = len(invoices_df)
        c1, c2 = st.columns(2)
        c1.metric("Total TTC (company currency)", f"{total_ttc:,.2f}")
        c2.metric("Invoices", f"{inv_count:,}")

        # Per-company summary
        summary = (
            invoices_df.groupby("company", as_index=False)
                       .agg(TTC=("TTC","sum"), Invoices=("name","count"))
                       .sort_values("TTC", ascending=False)
        )
        with st.expander("Per-company totals"):
            st.dataframe(summary, use_container_width=True)

        # ----- Chart (colored by company) -----
        st.subheader("TTC by Date (per company)")
        if daily_df.empty:
            st.info("No daily data to chart.")
        else:
            if alt:
                chart = (
                    alt.Chart(daily_df)
                       .mark_line()
                       .encode(
                           x=alt.X("date:T", title="Date"),
                           y=alt.Y("TTC:Q", title="TTC"),
                           color=alt.Color("company:N", title="Company"),
                           tooltip=[alt.Tooltip("company:N"), alt.Tooltip("date:T"), alt.Tooltip("TTC:Q")]
                       )
                       .properties(height=380)
                       .interactive()
                )
                st.altair_chart(chart, use_container_width=True)
            else:
                # Fallback: wide format for st.line_chart (auto-colors columns)
                wide = daily_df.pivot(index="date", columns="company", values="TTC").fillna(0)
                st.line_chart(wide)

        # ----- Tables -----
        with st.expander("Daily totals (all companies)"):
            st.dataframe(daily_df.sort_values(["date", "company"]), use_container_width=True)

        with st.expander("Invoice rows"):
            st.dataframe(
                invoices_df[["name", "posting_date", "company", "base_grand_total", "currency"]],
                use_container_width=True
            )

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try:
            st.code(e.response.text)
        except Exception:
            st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")
