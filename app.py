# ERPNext Dashboard: TTC, Debts (A/R), Customers, Map
# - Map uses ONLY Customer.custom_lat/custom_lon (no Address fallback)
# - Paramétrage to include Suppliers on the Map (Supplier.custom_lat/custom_lon)
# - Persistent "Load data" state and sticky Map settings
# - Nicer tooltips (label + status + phone)
# - NEW: Supplier → Company outbound flows (period)
# - NEW: Ignore (0,0) coordinates everywhere (customers, suppliers, company coords for flows)

import streamlit as st
import requests
import pandas as pd
import json
from datetime import date
from dateutil.relativedelta import relativedelta
import altair as alt
import numpy as np

# Optional charts / maps
try:
    import pydeck as pdk  # noqa
except Exception:
    pdk = None  # imported later only when needed

st.set_page_config(page_title="ERPNext Dashboard", layout="wide")

# ------------------ Global Styles ------------------
st.markdown("""
<style>
    .main .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    .card {
        background: white; border-radius: 10px; padding: 1.5rem;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 1.5rem;
    }
    .metric-card {
        background: linear-gradient(145deg, #f8f9fa 0%, #fff 100%);
        border-radius: 10px; padding: 1.25rem; text-align: center;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: transform 0.2s;
    }
    .metric-card:hover { transform: translateY(-3px); }
    .metric-value { font-size: 1.8rem; font-weight: 700; color: #2563eb; margin: .5rem 0; }
    .metric-label { font-size: .9rem; color: #6b7280; margin: 0; }
    .chart-container {
        background: white; border-radius: 10px; padding: 1.5rem;
        margin-bottom: 1.5rem; box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: #f1f1f1; border-radius: 10px; }
    ::-webkit-scrollbar-thumb { background: #c1c1c1; border-radius: 10px; }
    ::-webkit-scrollbar-thumb:hover { background: #a1a1a1; }
</style>
""", unsafe_allow_html=True)

# ------------------ Secrets / Config (robust) ------------------
def get_secret(path, default=None):
    """Safe getter for nested st.secrets via tuple path, e.g. ('erpnext','base_url')."""
    try:
        cur = st.secrets
        for key in path:
            cur = cur[key]
        return cur
    except Exception:
        return default

BASE = get_secret(("erpnext", "base_url"))
VERIFY_SSL = get_secret(("erpnext", "verify_ssl"), True)
MAPBOX_TOKEN = get_secret(("mapbox", "token"), "")

# Allow inline config so app doesn't crash on Cloud
if not BASE:
    with st.sidebar:
        st.warning("ERPNext settings are missing. Enter them here or add them in **Manage app → Settings → Secrets**.")
        BASE = st.text_input("ERPNext Base URL (e.g. https://erp.example.com)", value="").strip()
        VERIFY_SSL = st.toggle("Verify SSL", value=True)
        MAPBOX_TOKEN = st.text_input("Mapbox token (optional)", value="")
        if BASE == "":
            st.stop()

BASE = BASE.rstrip("/")

# ------------------ Session State ------------------
if "cookies" not in st.session_state: st.session_state.cookies = None
if "user" not in st.session_state: st.session_state.user = None
if "date_range" not in st.session_state:
    st.session_state.date_range = (date.today() - relativedelta(months=3), date.today())
if "nav" not in st.session_state: st.session_state.nav = "TTC"
if "run" not in st.session_state: st.session_state.run = False  # keep dataset "loaded" after first click

# Map defaults (persist across reruns)
MAP_DEFAULTS = {
    "show_customers": True,
    "show_suppliers": False,
    "supplier_status_by_period": True,
    "sold_px": 6,
    "idle_px": 5,
    "supp_px": 7,
    "jitter_on": True,
    "theme": "Positron (Light, no token)",
    "show_all_counts": True,
    # Flow defaults
    "show_supply_flows": False,
    "flow_px": 4,
}
for k, v in MAP_DEFAULTS.items():
    st.session_state.setdefault(k, v)

# ------------------ HTTP helpers ------------------
def new_session() -> requests.Session:
    s = requests.Session()
    if st.session_state.cookies:
        s.cookies.update(st.session_state.cookies)
    return s

def login(usr: str, pwd: str) -> bool:
    s = requests.Session()
    r = s.post(f"{BASE}/api/method/login", data={"usr": usr, "pwd": pwd}, timeout=30, verify=VERIFY_SSL)
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

# ------------------ Data Fetchers (robust pagination) ------------------
@st.cache_data(show_spinner=False)
def list_companies(user_key: str):
    data = api_get("/api/resource/Company", fields=json.dumps(["name"]), order_by="name asc", limit_page_length=1000)
    return [row["name"] for row in data]

# Companies with coordinates (for flows)
@st.cache_data(show_spinner=False)
def list_companies_df(user_key: str=""):
    """Companies with optional custom lat/lon (expects custom fields on Company)."""
    fields = ["name", "custom_lat", "custom_lon"]
    rows, start_idx = [], 0
    params = dict(fields=json.dumps(fields), order_by="name asc", limit_page_length=1000)
    while True:
        page = api_get("/api/resource/Company", **{**params, "limit_start": start_idx})
        if not page:
            break
        rows.extend(page)
        start_idx += len(page)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["custom_lat"] = pd.to_numeric(df.get("custom_lat"), errors="coerce")
    df["custom_lon"] = pd.to_numeric(df.get("custom_lon"), errors="coerce")
    return df

BASE_FIELDS = [
    "name", "posting_date", "company", "customer", "due_date",
    "base_grand_total", "grand_total", "currency",
    "outstanding_amount", "status", "conversion_rate",
]

@st.cache_data(show_spinner=True)
def fetch_invoices(companies, start: date, end: date, include_drafts: bool,
                   extra_filters=None, fields_add=None, user_key: str=""):
    if not companies:
        return pd.DataFrame()
    if start and end and start > end:
        start, end = end, start

    fields = BASE_FIELDS[:] + (fields_add or [])
    all_rows = []

    for co in companies:
        filters = [["company", "=", co]]
        if start and end:
            filters += [["posting_date", ">=", str(start)], ["posting_date", "<=", str(end)]]
        filters.append(["docstatus", "in", [0, 1]] if include_drafts else ["docstatus", "=", 1])
        if extra_filters: filters += extra_filters

        params = {
            "fields": json.dumps(list(dict.fromkeys(fields))),
            "filters": json.dumps(filters),
            "order_by": "posting_date asc, name asc",
            "limit_page_length": 1000,
        }

        start_idx = 0
        while True:
            page = api_get("/api/resource/Sales Invoice", **{**params, "limit_start": start_idx})
            if not page:
                break
            all_rows.extend(page)
            start_idx += len(page)

    df = pd.DataFrame(all_rows)
    if df.empty: return df

    if "posting_date" in df.columns: df["posting_date"] = pd.to_datetime(df["posting_date"])
    if "due_date" in df.columns: df["due_date"] = pd.to_datetime(df["due_date"], errors="coerce")

    df["TTC"] = df.get("base_grand_total", 0)
    df["conversion_rate"] = pd.to_numeric(df.get("conversion_rate", 1), errors="coerce").fillna(1.0)
    df["outstanding_amount"] = pd.to_numeric(df.get("outstanding_amount", 0), errors="coerce").fillna(0.0)
    df["base_outstanding"] = df["outstanding_amount"] * df["conversion_rate"]
    return df

@st.cache_data(show_spinner=True)
def fetch_outstanding_invoices(companies, user_key: str=""):
    return fetch_invoices(
        companies=companies,
        start=None, end=None,
        include_drafts=False,
        extra_filters=[["outstanding_amount", ">", 0]],
        fields_add=[],
        user_key=user_key
    )

@st.cache_data(show_spinner=True)
def list_customers():
    fields = ["name", "customer_name", "mobile_no", "territory", "custom_lat", "custom_lon"]
    all_rows, start_idx = [], 0
    params = dict(fields=json.dumps(fields), order_by="name asc", limit_page_length=1000)

    while True:
        page = api_get("/api/resource/Customer", **{**params, "limit_start": start_idx})
        if not page:
            break
        all_rows.extend(page)
        start_idx += len(page)

    df = pd.DataFrame(all_rows)
    if df.empty: return df

    # Robust display name fallback
    disp = df.get("customer_name").fillna("")
    disp = disp.replace("", np.nan)
    df["display_customer"] = disp.fillna(df.get("name"))

    df["custom_lat"] = pd.to_numeric(df.get("custom_lat"), errors="coerce")
    df["custom_lon"] = pd.to_numeric(df.get("custom_lon"), errors="coerce")
    return df

# ------------------ Suppliers & Purchase Invoices ------------------
@st.cache_data(show_spinner=True)
def list_suppliers():
    """Suppliers with coords (uses Supplier.custom_lat/custom_lon)."""
    fields = ["name", "supplier_name", "supplier_group", "mobile_no", "custom_lat", "custom_lon"]
    all_rows, start_idx = [], 0
    params = dict(fields=json.dumps(fields), order_by="name asc", limit_page_length=1000)

    while True:
        page = api_get("/api/resource/Supplier", **{**params, "limit_start": start_idx})
        if not page:
            break
        all_rows.extend(page)
        start_idx += len(page)

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    disp = df.get("supplier_name").fillna("")
    disp = disp.replace("", np.nan)
    df["display_supplier"] = disp.fillna(df.get("name"))

    df["custom_lat"] = pd.to_numeric(df.get("custom_lat"), errors="coerce")
    df["custom_lon"] = pd.to_numeric(df.get("custom_lon"), errors="coerce")
    return df

PURCHASE_FIELDS = [
    "name","posting_date","company","supplier","base_grand_total","grand_total","currency","status","docstatus"
]

@st.cache_data(show_spinner=True)
def fetch_purchase_invoices(companies, start: date, end: date, include_drafts=False, user_key: str=""):
    if not companies:
        return pd.DataFrame()
    if start and end and start > end:
        start, end = end, start

    fields = PURCHASE_FIELDS[:]
    all_rows = []

    for co in companies:
        filters = [["company", "=", co]]
        if start and end:
            filters += [["posting_date", ">=", str(start)], ["posting_date", "<=", str(end)]]
        filters.append(["docstatus", "in", [0, 1]] if include_drafts else ["docstatus", "=", 1])

        params = {
            "fields": json.dumps(list(dict.fromkeys(fields))),
            "filters": json.dumps(filters),
            "order_by": "posting_date asc, name asc",
            "limit_page_length": 1000,
        }

        start_idx = 0
        while True:
            page = api_get("/api/resource/Purchase Invoice", **{**params, "limit_start": start_idx})
            if not page:
                break
            all_rows.extend(page)
            start_idx += len(page)

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df
    if "posting_date" in df.columns:
        df["posting_date"] = pd.to_datetime(df["posting_date"])
    return df

# ------------------ Utility: filter (0,0) and NaNs ------------------
def filter_valid_coords(df: pd.DataFrame, lat_col: str, lon_col: str) -> pd.DataFrame:
    """Keep rows with real coordinates: both present and not equal to (0,0)."""
    if df is None or df.empty:
        return df
    d = df.copy()
    d[lat_col] = pd.to_numeric(d.get(lat_col), errors="coerce")
    d[lon_col] = pd.to_numeric(d.get(lon_col), errors="coerce")
    m = d[lat_col].notna() & d[lon_col].notna() & ~((d[lat_col] == 0) & (d[lon_col] == 0))
    return d.loc[m]

# ------------------ UI: Login ------------------
st.title("ERPNext Dashboard")
if not st.session_state.user:
    st.subheader("Log in to ERPNext")
    with st.form("login_form", clear_on_submit=False):
        usr = st.text_input("Email / Username", value="", autocomplete="username")
        pwd = st.text_input("Password", type="password", value="", autocomplete="current-password")
        colA, colB = st.columns([1, 1])
        submit = colA.form_submit_button("Log in")
        colB.caption("Your password is not stored; only a session cookie is used.")
    if submit and login(usr, pwd):
        st.success(f"Logged in as {st.session_state.user}")
        st.rerun()
    st.stop()

st.success(f"Logged in as {st.session_state.user}")
if st.button("Logout"):
    logout()
    st.rerun()

# ------------------ Sidebar: NAV (buttons) + Filters ------------------
SIDEBAR_CSS = """
<style>
.sidebar-nav { display: flex; flex-direction: column; gap: .6rem; margin-bottom: .75rem; }
.sidebar-nav .stButton > button {
  width: 100%; display: flex; align-items: center; gap: .6rem;
  padding: .55rem .75rem; border-radius: 12px;
  border: 1px solid rgba(200,200,200,.25); background: rgba(255,255,255,.02);
  font-weight: 600; position: relative; transition: transform .12s ease, box-shadow .15s ease, background .15s ease, border-color .15s ease;
}
.sidebar-nav .stButton > button:hover { background: rgba(255,255,255,.06); border-color: rgba(180,180,255,.45); }
@keyframes glowPulse { 0%,100%{box-shadow:0 0 0 2px rgba(90,120,255,.18) inset,0 0 14px rgba(90,120,255,.30),0 0 0 rgba(90,120,255,0);} 50%{box-shadow:0 0 0 2px rgba(90,120,255,.22) inset,0 0 26px rgba(90,120,255,.60),0 0 24px rgba(90,120,255,.30);} }
</style>
"""
st.sidebar.markdown(SIDEBAR_CSS, unsafe_allow_html=True)

def sidebar_nav_buttons():
    st.sidebar.markdown('<div class="sidebar-nav">', unsafe_allow_html=True)
    b1 = st.sidebar.button("📈  TTC", key="nav_ttc_btn", use_container_width=True)
    b2 = st.sidebar.button("💳  Debts", key="nav_debts_btn", use_container_width=True)
    b3 = st.sidebar.button("👥  Customers", key="nav_customers_btn", use_container_width=True)
    b4 = st.sidebar.button("🗺️  Map", key="nav_map_btn", use_container_width=True)
    if b1: st.session_state.nav = "TTC"; st.rerun()
    if b2: st.session_state.nav = "Debts"; st.rerun()
    if b3: st.session_state.nav = "Customers"; st.rerun()
    if b4: st.session_state.nav = "Map"; st.rerun()

    active_idx = {"TTC": 1, "Debts": 2, "Customers": 3, "Map": 4}[st.session_state.nav]
    st.sidebar.markdown(f"""
    <style>
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button {{
      border-color: rgba(90,120,255,.65) !important;
      background: linear-gradient(180deg, rgba(90,120,255,.12), rgba(90,120,255,.06)) !important;
      transform: translateZ(0) scale(1.01);
      animation: glowPulse 1.6s ease-in-out infinite;
      box-shadow: 0 0 0 2px rgba(90,120,255,.18) inset, 0 0 18px rgba(90,120,255,.45), 0 0 36px rgba(90,120,255,.25);
    }}
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button:before {{
      content: ""; position: absolute; left: 6px; top: 8px; bottom: 8px; width: 4px;
      border-radius: 6px; background: linear-gradient(180deg, rgba(90,120,255,1), rgba(90,120,255,.55));
      box-shadow: 0 0 10px rgba(90,120,255,.55);
    }}
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button:after {{
      content: ""; position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
      width: 8px; height: 8px; border-radius: 9999px; background: rgba(90,120,255,.95);
      box-shadow: 0 0 10px rgba(90,120,255,.8), 0 0 18px rgba(90,120,255,.45);
    }}
    </style>
    """, unsafe_allow_html=True)
    st.sidebar.markdown('</div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("Navigation")
    sidebar_nav_buttons()

    st.header("Filters")
    try:
        companies = list_companies(st.session_state.user or "")
    except Exception as e:
        st.error(f"Cannot load companies: {e}")
        st.stop()
    if not companies:
        st.error("No companies available.")
        st.stop()

    company_options = ["All Companies"] + companies
    selected = st.multiselect("Companies", company_options, default=["All Companies"], key="companies_multi")
    selected_companies = companies[:] if ("All Companies" in selected or not selected) else [c for c in selected if c in companies]

    start_date, end_date = st.date_input("Date range", value=st.session_state.date_range, key="date_range")
    if isinstance(start_date, tuple): start_date, end_date = start_date[0], start_date[1]
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        st.error("Please select a valid start and end date."); st.stop()
    if start_date > end_date: start_date, end_date = end_date, start_date

    include_drafts = st.toggle("Include Draft Invoices (for TTC only)", value=False, key="include_drafts")

    if st.button("Load data", key="load_btn"):
        st.session_state.run = True

    # Auto refresh caches when filters change
    if st.session_state.get("run"):
        if st.session_state.get("last_filters") != (tuple(selected_companies), str(start_date), str(end_date), st.session_state.include_drafts):
            st.cache_data.clear()
            st.session_state.last_filters = (tuple(selected_companies), str(start_date), str(end_date), st.session_state.include_drafts)

# ------------------ Helpers ------------------
def download_button(df: pd.DataFrame, filename: str, label: str):
    if df.empty: return
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(label=label, data=csv, file_name=filename, mime="text/csv")

# ------------------ Screens ------------------
if st.session_state.run and st.session_state.nav == "TTC":
    st.subheader("💰 Revenue Analytics")
    try:
        inv = fetch_invoices(selected_companies, start_date, end_date, include_drafts, user_key=st.session_state.user or "")
        if inv.empty: st.info("No invoices found."); st.stop()

        # Daily data
        all_daily = []
        for co, df_co in inv.groupby("company"):
            d = (df_co.set_index("posting_date")["TTC"].resample("D").sum().reset_index().rename(columns={"posting_date": "date"}))
            d["company"] = co
            all_daily.append(d)
        daily_df = pd.concat(all_daily, ignore_index=True)

        total_ttc = float(inv["TTC"].sum())
        inv_count = len(inv)
        avg_invoice = total_ttc / inv_count if inv_count > 0 else 0

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Total Revenue</p>
                <p class="metric-value">{total_ttc:,.2f}</p>
                <p class="metric-label">across {inv_count:,} invoices</p>
            </div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Avg. Invoice</p>
                <p class="metric-value">{avg_invoice:,.2f}</p>
                <p class="metric-label">per transaction</p>
            </div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Date Range</p>
                <p class="metric-value">{len(daily_df['date'].dt.date.unique())} days</p>
                <p class="metric-label">{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}</p>
            </div>""", unsafe_allow_html=True)

        st.markdown("### Revenue Trends")
        chart = alt.Chart(daily_df).mark_area(
            interpolate='monotone',
            line={'color':'#4C78A8'},
            color=alt.Gradient(
                gradient='linear',
                stops=[alt.GradientStop(color='white', offset=0), alt.GradientStop(color='#4C78A8', offset=1)],
                x1=1, x2=1, y1=1, y2=0
            )
        ).encode(
            x=alt.X('date:T', title='Date', axis=alt.Axis(format='%b %Y')),
            y=alt.Y('sum(TTC):Q', title='Revenue'),
            color=alt.Color('company:N', title='Company', scale=alt.Scale(scheme='tableau20')),
            tooltip=[alt.Tooltip('date:T', title='Date', format='%b %d, %Y'),
                     alt.Tooltip('company:N', title='Company'),
                     alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')]
        ).properties(height=400, title='Daily Revenue by Company').interactive()
        st.altair_chart(chart, use_container_width=True)

        st.markdown("### Monthly Breakdown")
        monthly_df = daily_df.copy()
        monthly_df['month'] = monthly_df['date'].dt.strftime('%Y-%m')
        monthly_chart = alt.Chart(monthly_df).mark_bar(
            cornerRadiusTopLeft=3, cornerRadiusTopRight=3
        ).encode(
            x=alt.X('month:O', title='Month', sort='x'),
            y=alt.Y('sum(TTC):Q', title='Revenue'),
            color=alt.Color('company:N', title='Company', scale=alt.Scale(scheme='tableau20')),
            tooltip=[alt.Tooltip('month:O', title='Month'),
                     alt.Tooltip('company:N', title='Company'),
                     alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')]
        ).properties(height=400, title='Monthly Revenue by Company')
        st.altair_chart(monthly_chart, use_container_width=True)

        st.markdown("### Top Customers")
        top_customers = inv.groupby('customer')['TTC'].sum().nlargest(10).reset_index()
        customer_chart = alt.Chart(top_customers).mark_bar(
            cornerRadiusTopRight=3, cornerRadiusBottomRight=3
        ).encode(
            x=alt.X('sum(TTC):Q', title='Revenue'),
            y=alt.Y('customer:N', title='', sort='-x'),
            color=alt.Color('sum(TTC):Q', scale=alt.Scale(scheme='blues'), legend=None),
            tooltip=[alt.Tooltip('customer:N', title='Customer'),
                     alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')]
        ).properties(height=400, title='Top 10 Customers by Revenue')
        st.altair_chart(customer_chart, use_container_width=True)

        with st.expander("📊 View Detailed Data"):
            tab1, tab2 = st.tabs(["Daily Totals", "Invoice Details"])
            with tab1:
                st.dataframe(daily_df.sort_values(["date", "company"]), use_container_width=True, height=400)
                download_button(daily_df, "ttc_daily.csv", "📥 Download Daily Data")
            with tab2:
                st.dataframe(inv[["name", "posting_date", "company", "customer", "base_grand_total", "currency"]],
                             use_container_width=True, height=400)
                download_button(inv, "ttc_invoices.csv", "📥 Download Invoice Data")

    except requests.HTTPError as e:
        st.error("❌ ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"❌ Error: {e}")

elif st.session_state.run and st.session_state.nav == "Debts":
    st.subheader("💳  Debts (A/R)")
    try:
        open_inv = fetch_outstanding_invoices(selected_companies, user_key=st.session_state.user or "")
        if open_inv.empty: st.info("No open debts found."); st.stop()

        today = pd.Timestamp(date.today())
        open_inv["days_overdue"] = (today - open_inv["due_date"]).dt.days
        open_inv["days_overdue"] = open_inv["days_overdue"].fillna(0).astype(int)

        total_outstanding = float(open_inv.get("base_outstanding", pd.Series()).fillna(0).sum())
        count_open = len(open_inv)
        c1, c2 = st.columns(2)
        c1.metric("Total Outstanding (company currency)", f"{total_outstanding:,.2f}")
        c2.metric("Open Invoices", f"{count_open:,}")

        per_cust = (
            open_inv.groupby(["company","customer"], as_index=False)
                    .agg(Outstanding=("base_outstanding","sum"),
                         Invoices=("name","count"),
                         MaxOverdue=("days_overdue","max"))
                    .sort_values(["Outstanding"], ascending=False)
        )

        st.markdown("Top customers by outstanding")
        topN = per_cust.nlargest(15, "Outstanding")
        if not topN.empty:
            chart = (alt.Chart(topN).mark_bar().encode(
                x=alt.X("Outstanding:Q", title="Outstanding"),
                y=alt.Y("customer:N", sort="-x", title="Customer"),
                color=alt.Color("company:N", title="Company"),
                tooltip=["company:N","customer:N","Outstanding:Q","Invoices:Q","MaxOverdue:Q"]
            ).properties(height=400))
            st.altair_chart(chart, use_container_width=True)

        cols = ["name","posting_date","due_date","company","customer","base_outstanding","currency","status","days_overdue","outstanding_amount","conversion_rate"]
        cols = [c for c in cols if c in open_inv.columns]
        with st.expander("Open invoice rows"):
            st.dataframe(open_inv[cols].sort_values(["company","customer","due_date"]), use_container_width=True)
            download_button(open_inv[cols], "debts_open_invoices.csv", "Download open invoices CSV")

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")

elif st.session_state.run and st.session_state.nav == "Customers":
    st.subheader("👥  Customers Overview")
    try:
        inv_period = fetch_invoices(selected_companies, start_date, end_date, include_drafts=False, user_key=st.session_state.user or "")
        open_inv = fetch_outstanding_invoices(selected_companies, user_key=st.session_state.user or "")

        if inv_period.empty and open_inv.empty: st.info("No data for the selected filters."); st.stop()

        if not inv_period.empty:
            agg_period = (inv_period.groupby(["company","customer"], as_index=False)
                          .agg(Sales_TTC=("TTC","sum"), Invoices=("name","count"), Last_Invoice=("posting_date","max")))
        else:
            agg_period = pd.DataFrame(columns=["company","customer","Sales_TTC","Invoices","Last_Invoice"])

        if not open_inv.empty:
            agg_open = (open_inv.groupby(["company","customer"], as_index=False)
                        .agg(Outstanding=("base_outstanding","sum")))
        else:
            agg_open = pd.DataFrame(columns=["company","customer","Outstanding"])

        customers = pd.merge(agg_period, agg_open, on=["company","customer"], how="outer")
        for col in ["Sales_TTC","Invoices","Outstanding"]:
            if col in customers.columns: customers[col] = customers[col].fillna(0)
        if "Last_Invoice" in customers.columns:
            customers["Last_Invoice"] = pd.to_datetime(customers["Last_Invoice"])

        total_sales = float(customers.get("Sales_TTC", pd.Series()).sum())
        total_outstanding = float(customers.get("Outstanding", pd.Series()).sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Sales TTC (period)", f"{total_sales:,.2f}")
        c2.metric("Total Outstanding (now)", f"{total_outstanding:,.2f}")
        c3.metric("Customers (rows)", f"{len(customers):,}")

        q = st.text_input("Search customer")
        if q:
            customers = customers[customers["customer"].fillna("").str.contains(q, case=False, na=False)]

        st.dataframe(customers.sort_values(["company","Sales_TTC"], ascending=[True, False]), use_container_width=True)
        download_button(customers, "customers_overview.csv", "Download customers CSV")

        topC = customers.nlargest(20, "Sales_TTC")
        st.markdown("Top customers by sales (period)")
        if not topC.empty:
            chart = (alt.Chart(topC).mark_bar().encode(
                x=alt.X("Sales_TTC:Q", title="Sales TTC (period)"),
                y=alt.Y("customer:N", sort="-x", title="Customer"),
                color=alt.Color("company:N", title="Company"),
                tooltip=["company:N","customer:N","Sales_TTC:Q","Invoices:Q","Outstanding:Q","Last_Invoice:T"]
            ).properties(height=400))
            st.altair_chart(chart, use_container_width=True)

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")

elif st.session_state.run and st.session_state.nav == "Map":
    st.subheader("🗺️  Sales & Suppliers Map (from custom_lat/custom_lon only)")
    st.caption("Customers: Blue = bought in selected period, Gray = no sale. Suppliers: Green = purchased from in period, Light green = no purchase in period. Flows: Supplier → Company for purchases in the selected period.")

    # ---- Paramétrage ----
    with st.expander("⚙️ Paramétrage (Map Settings)", expanded=True):
        colp1, colp2, colp3 = st.columns([1,1,1])
        with colp1:
            st.checkbox("Show Customers", key="show_customers")
        with colp2:
            st.checkbox("Show Suppliers", key="show_suppliers")
        with colp3:
            st.checkbox(
                "Color suppliers by period activity",
                key="supplier_status_by_period",
                help="If on, suppliers with Purchase Invoices in the selected period are darker green."
            )

        colz1, colz2, colz3, colz4 = st.columns([1,1,1,1])
        with colz1:
            st.slider("Sold (customers) point size (px)", 2, 16, key="sold_px")
        with colz2:
            st.slider("No-sale (customers) point size (px)", 2, 16, key="idle_px")
        with colz3:
            st.slider("Suppliers point size (px)", 2, 16, key="supp_px")
        with colz4:
            st.toggle("Anti-overlap jitter", key="jitter_on", help="Adds tiny random offsets so nearby points don't overlap.")

        # Flow controls
        st.toggle(
            "Show Supplier → Company flows (period)",
            key="show_supply_flows",
            help="Draw arcs from supplier to the company that purchased during the selected period."
        )
        st.slider("Flow line width (px)", 1, 10, key="flow_px")

        st.selectbox(
            "Basemap theme",
            [
                "Positron (Light, no token)",
                "Voyager (Labels, no token)",
                "Mapbox Light (token)",
                "Mapbox Streets (token)",
                "Dark Matter (no token)"
            ],
            key="theme",
        )

    # counts toggle (outside the expander) with sticky key
    st.toggle("Show counts", key="show_all_counts")

    # Read sticky values from session_state
    show_customers = st.session_state.show_customers
    show_suppliers = st.session_state.show_suppliers
    supplier_status_by_period = st.session_state.supplier_status_by_period
    sold_px = st.session_state.sold_px
    idle_px = st.session_state.idle_px
    supp_px = st.session_state.supp_px
    jitter_on = st.session_state.jitter_on
    theme = st.session_state.theme
    show_all_counts = st.session_state.show_all_counts

    # ---- Basemap dict ----
    THEME_URLS = {
        "Positron (Light, no token)": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        "Voyager (Labels, no token)": "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        "Dark Matter (no token)":     "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        "Mapbox Light (token)":       "mapbox://styles/mapbox/light-v11",
        "Mapbox Streets (token)":     "mapbox://styles/mapbox/streets-v12",
    }
    style_url = THEME_URLS[theme]

    try:
        layers = []
        center_lats, center_lons = [], []

        # ================= Customers =================
        sold_n = nosale_n = 0
        total_customers = with_coords_c = 0
        if show_customers:
            inv_period = fetch_invoices(
                selected_companies, start_date, end_date,
                include_drafts=False, fields_add=[],
                user_key=st.session_state.user or ""
            )
            buyers_period = set(inv_period["customer"].dropna()) if not inv_period.empty else set()

            cust_df = list_customers()
            if not cust_df.empty:
                data_c = cust_df.copy()
                data_c["status"] = np.where(data_c["name"].isin(buyers_period), "Sold (period)", "No sale in period")
                data_c.rename(columns={"display_customer": "customer"}, inplace=True)

                total_customers = len(data_c)
                valid_c = filter_valid_coords(data_c, "custom_lat", "custom_lon")
                with_coords_c = int(len(valid_c))

                plot_c = valid_c.copy()

                # ---- Tooltip-friendly columns ----
                plot_c["label"] = "Customer: " + plot_c["customer"].fillna(plot_c["name"])
                if "mobile_no" in plot_c.columns:
                    plot_c["phone"] = plot_c["mobile_no"].fillna("")
                else:
                    plot_c["phone"] = ""

                if jitter_on:
                    def dj(seed):
                        h = abs(hash(str(seed))) % 10_000
                        return (h / 10_000.0 - 0.5) * 0.00005
                    plot_c["lat"] = plot_c["custom_lat"] + plot_c["name"].map(dj)
                    plot_c["lon"] = plot_c["custom_lon"] + plot_c["name"].map(lambda x: dj(str(x)+"x"))
                else:
                    plot_c["lat"] = plot_c["custom_lat"]
                    plot_c["lon"] = plot_c["custom_lon"]

                sold_df = plot_c[plot_c["status"] == "Sold (period)"]
                idle_df = plot_c[plot_c["status"] == "No sale in period"]
                sold_n = int(len(sold_df))
                nosale_n = int(len(idle_df))

                center_lats.extend(plot_c["lat"].tolist())
                center_lons.extend(plot_c["lon"].tolist())

                import pydeck as pdk
                # customers layers
                if not idle_df.empty:
                    layers.append(pdk.Layer(
                        "ScatterplotLayer",
                        data=idle_df,
                        get_position='[lon, lat]',
                        get_radius=idle_px,
                        radius_units="pixels",
                        radius_min_pixels=idle_px,
                        radius_max_pixels=idle_px,
                        stroked=True,
                        get_line_color=[255,255,255,200],
                        line_width_min_pixels=1,
                        pickable=True,
                        get_fill_color=[160,160,160,160],
                    ))
                if not sold_df.empty:
                    layers.append(pdk.Layer(
                        "ScatterplotLayer",
                        data=sold_df,
                        get_position='[lon, lat]',
                        get_radius=sold_px,
                        radius_units="pixels",
                        radius_min_pixels=sold_px,
                        radius_max_pixels=sold_px,
                        stroked=True,
                        get_line_color=[255,255,255,220],
                        line_width_min_pixels=1,
                        pickable=True,
                        get_fill_color=[0,140,255,200],  # blue
                    ))

        # ================= Suppliers =================
        supp_n = supp_active_n = 0
        total_suppliers = with_coords_s = 0
        # Store pinv_period for flows regardless of status coloring
        pinv_period = pd.DataFrame()
        if show_suppliers:
            # active suppliers (had purchase invoices in period)
            pinv_period = fetch_purchase_invoices(
                selected_companies, start_date, end_date,
                include_drafts=False, user_key=st.session_state.user or ""
            )
            active_suppliers = set(pinv_period["supplier"].dropna()) if not pinv_period.empty else set()

            supp_df = list_suppliers()
            if not supp_df.empty:
                data_s = supp_df.copy()
                data_s["status"] = np.where(
                    data_s["name"].isin(active_suppliers),
                    "Supplier active (period)",
                    "Supplier inactive (period)"
                )
                data_s.rename(columns={"display_supplier": "supplier"}, inplace=True)

                total_suppliers = len(data_s)
                valid_s = filter_valid_coords(data_s, "custom_lat", "custom_lon")
                with_coords_s = int(len(valid_s))

                plot_s = valid_s.copy()

                # ---- Tooltip-friendly columns ----
                plot_s["label"] = "Supplier: " + plot_s["supplier"].fillna(plot_s["name"])
                if "mobile_no" in plot_s.columns:
                    plot_s["phone"] = plot_s["mobile_no"].fillna("")
                else:
                    plot_s["phone"] = ""

                if jitter_on:
                    def dj(seed):
                        h = abs(hash("S"+str(seed))) % 10_000
                        return (h / 10_000.0 - 0.5) * 0.00005
                    plot_s["lat"] = plot_s["custom_lat"] + plot_s["name"].map(dj)
                    plot_s["lon"] = plot_s["custom_lon"] + plot_s["name"].map(lambda x: dj(str(x)+"x"))
                else:
                    plot_s["lat"] = plot_s["custom_lat"]
                    plot_s["lon"] = plot_s["custom_lon"]

                import pydeck as pdk
                if supplier_status_by_period:
                    active_s = plot_s[plot_s["status"] == "Supplier active (period)"]
                    inactive_s = plot_s[plot_s["status"] == "Supplier inactive (period)"]
                    supp_active_n = int(len(active_s))
                    supp_n = int(len(plot_s))

                    center_lats.extend(plot_s["lat"].tolist())
                    center_lons.extend(plot_s["lon"].tolist())

                    if not inactive_s.empty:
                        layers.append(pdk.Layer(
                            "ScatterplotLayer",
                            data=inactive_s,
                            get_position='[lon, lat]',
                            get_radius=supp_px,
                            radius_units="pixels",
                            radius_min_pixels=supp_px,
                            radius_max_pixels=supp_px,
                            stroked=True,
                            get_line_color=[255,255,255,200],
                            line_width_min_pixels=1,
                            pickable=True,
                            get_fill_color=[120,200,120,140],  # light green
                        ))
                    if not active_s.empty:
                        layers.append(pdk.Layer(
                            "ScatterplotLayer",
                            data=active_s,
                            get_position='[lon, lat]',
                            get_radius=supp_px,
                            radius_units="pixels",
                            radius_min_pixels=supp_px,
                            radius_max_pixels=supp_px,
                            stroked=True,
                            get_line_color=[255,255,255,220],
                            line_width_min_pixels=1,
                            pickable=True,
                            get_fill_color=[0,160,70,200],  # green
                        ))
                else:
                    # one uniform supplier layer
                    supp_n = int(len(plot_s))
                    center_lats.extend(plot_s["lat"].tolist())
                    center_lons.extend(plot_s["lon"].tolist())

                    layers.append(pdk.Layer(
                        "ScatterplotLayer",
                        data=plot_s,
                        get_position='[lon, lat]',
                        get_radius=supp_px,
                        radius_units="pixels",
                        radius_min_pixels=supp_px,
                        radius_max_pixels=supp_px,
                        stroked=True,
                        get_line_color=[255,255,255,220],
                        line_width_min_pixels=1,
                        pickable=True,
                        get_fill_color=[0,160,70,200],  # green
                    ))

        # ================= Supplier -> Company FLOWS =================
        if show_suppliers and st.session_state.get("show_supply_flows", False):
            # 1) Companies with coords (exclude 0,0)
            comp_df = list_companies_df(st.session_state.user or "")
            comp_coords = filter_valid_coords(comp_df, "custom_lat", "custom_lon").rename(
                columns={"custom_lat":"comp_lat","custom_lon":"comp_lon","name":"company"}
            )

            # 2) Suppliers with coords (exclude 0,0)
            if 'supp_df' not in locals():
                supp_df = list_suppliers()
            supp_coords = filter_valid_coords(supp_df, "custom_lat", "custom_lon").rename(
                columns={"custom_lat":"sup_lat","custom_lon":"sup_lon","name":"supplier"}
            )

            # 3) Purchase links in the period (sum amount per supplier->company)
            flows = pd.DataFrame()
            if 'pinv_period' in locals() and not pinv_period.empty and not comp_coords.empty and not supp_coords.empty:
                tmp = (pinv_period[["supplier","company","base_grand_total"]]
                       .dropna(subset=["supplier","company"]))
                flows = (tmp.groupby(["supplier","company"], as_index=False)
                            .agg(amount=("base_grand_total","sum")))
                flows = (flows
                         .merge(supp_coords[["supplier","sup_lat","sup_lon"]], on="supplier", how="inner")
                         .merge(comp_coords[["company","comp_lat","comp_lon"]], on="company", how="inner"))

            if not flows.empty:
                center_lats.extend(flows["sup_lat"].tolist() + flows["comp_lat"].tolist())
                center_lons.extend(flows["sup_lon"].tolist() + flows["comp_lon"].tolist())

                amt = flows["amount"].fillna(0)
                base_width = float(st.session_state.flow_px)
                if amt.max() > 0:
                    flows["width_px"] = (amt / amt.max() * (base_width * 2)).clip(lower=base_width, upper=base_width*2).astype(float)
                else:
                    flows["width_px"] = base_width

                flows["label"] = flows.apply(
                    lambda r: f"Supplier → Company<br>{r['supplier']} → {r['company']}<br>Amount: {r['amount']:,.2f}",
                    axis=1
                )

                import pydeck as pdk
                layers.append(pdk.Layer(
                    "ArcLayer",
                    data=flows,
                    get_source_position='[sup_lon, sup_lat]',
                    get_target_position='[comp_lon, comp_lat]',
                    get_width="width_px",
                    get_tilt=15,
                    pickable=True,
                    great_circle=True,
                    get_source_color=[0, 160, 70, 160],   # green-ish at supplier
                    get_target_color=[0, 120, 255, 180],  # blue-ish at company
                ))

        # ================= KPIs =================
        if st.session_state.show_all_counts:
            c1, c2, c3, c4 = st.columns(4)
            if show_customers:
                c1.metric("Customers (total / with coords)", f"{(total_customers or 0):,} / {(with_coords_c or 0):,}")
                c2.metric("Customers (sold / no sale)", f"{(sold_n or 0):,} / {(nosale_n or 0):,}")
            else:
                c1.empty(); c2.empty()
            if show_suppliers:
                if supplier_status_by_period:
                    c3.metric("Suppliers (with coords)", f"{(with_coords_s or 0):,}")
                    c4.metric("Suppliers (active / total)", f"{(supp_active_n or 0):,} / {(supp_n or 0):,}")
                else:
                    c3.metric("Suppliers (total / with coords)", f"{(total_suppliers or 0):,} / {(with_coords_s or 0):,}")
                    c4.metric("Suppliers plotted", f"{(supp_n or 0):,}")
            else:
                c3.empty(); c4.empty()
        else:
            c1, c2 = st.columns(2)
            if show_customers:
                c1.metric("Customers with sales (period)", f"{(sold_n or 0):,}")
            if show_suppliers:
                c2.metric(
                    "Suppliers active (period)" if supplier_status_by_period else "Suppliers plotted",
                    f"{((supp_active_n if supplier_status_by_period else supp_n) or 0):,}"
                )

        # ================= Deck & Basemap =================
        if not center_lats or not center_lons:
            st.info("No points to plot with current selection.")
            st.stop()

        import pydeck as pdk
        center_lat = float(np.mean(center_lats))
        center_lon = float(np.mean(center_lons))

        view = pdk.ViewState(
            latitude=center_lat,
            longitude=center_lon,
            zoom=5.5,
            min_zoom=3,
            max_zoom=18,
            pitch=35,
            bearing=-20,
        )

        # Upgraded tooltip
        tooltip = {
            "html": "<b>{label}</b><br>Status: {status}<br>{phone}",
            "style": {"backgroundColor": "rgba(20,20,20,0.85)", "color": "white"}
        }

        deck = pdk.Deck(
            layers=layers,
            initial_view_state=view,
            tooltip=tooltip,
            parameters={"cull": True},
        )

        THEME_URLS = {
            "Positron (Light, no token)": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            "Voyager (Labels, no token)": "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
            "Dark Matter (no token)":     "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
            "Mapbox Light (token)":       "mapbox://styles/mapbox/light-v11",
            "Mapbox Streets (token)":     "mapbox://styles/mapbox/streets-v12",
        }
        style_url = THEME_URLS[theme]

        if style_url.startswith("mapbox://"):
            if MAPBOX_TOKEN:
                deck.map_style = style_url
                deck.map_provider = "mapbox"
            else:
                st.info("Mapbox token not set; falling back to Positron light basemap.")
                deck.map_style = THEME_URLS["Positron (Light, no token)"]
        else:
            deck.map_style = style_url

        st.pydeck_chart(deck, use_container_width=True)

        # Tables
        with st.expander("📋 Plotted points (tables)"):
            if 'plot_c' in locals() and show_customers and not plot_c.empty:
                st.markdown("**Customers**")
                st.dataframe(
                    plot_c[["customer","status","custom_lat","custom_lon","phone"]].sort_values(["status","customer"]),
                    use_container_width=True
                )
                download_button(plot_c[["customer","status","custom_lat","custom_lon","phone"]], "map_customers.csv", "Download customers CSV")

            if 'plot_s' in locals() and show_suppliers and not plot_s.empty:
                st.markdown("**Suppliers**")
                scols = ["supplier","status","custom_lat","custom_lon","phone"]
                st.dataframe(
                    plot_s[scols].sort_values(["status","supplier"]),
                    use_container_width=True
                )
                download_button(plot_s[scols], "map_suppliers.csv", "Download suppliers CSV")

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")
