# ERPNext Dashboard: TTC, Debts (A/R), Customers, Map
# Map uses ONLY Customer.custom_lat/custom_lon (no Address fallback)

import streamlit as st
import requests
import pandas as pd
import json
from datetime import date
from dateutil.relativedelta import relativedelta
import altair as alt
import numpy as np

# Optional charts
try:
    import pydeck as pdk  # noqa
except Exception:
    pdk = None  # will import inside Map screen

st.set_page_config(page_title="ERPNext Dashboard", layout="wide")

st.markdown("""
<style>
    /* Main page styling */
    .main .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
    }
    
    /* Card styling */
    .card {
        background: white;
        border-radius: 10px;
        padding: 1.5rem;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
        margin-bottom: 1.5rem;
    }
    
    /* Metric styling */
    .metric-card {
        background: linear-gradient(145deg, #f8f9fa 0%, #fff 100%);
        border-radius: 10px;
        padding: 1.25rem;
        text-align: center;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        transition: transform 0.2s;
    }
    
    .metric-card:hover {
        transform: translateY(-3px);
    }
    
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #2563eb;
        margin: 0.5rem 0;
    }
    
    .metric-label {
        font-size: 0.9rem;
        color: #6b7280;
        margin: 0;
    }
    
    /* Chart containers */
    .chart-container {
        background: white;
        border-radius: 10px;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    
    /* Custom scrollbar */
    ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
    }
    
    ::-webkit-scrollbar-track {
        background: #f1f1f1;
        border-radius: 10px;
    }
    
    ::-webkit-scrollbar-thumb {
        background: #c1c1c1;
        border-radius: 10px;
    }
    
    ::-webkit-scrollbar-thumb:hover {
        background: #a1a1a1;
    }
</style>
""", unsafe_allow_html=True)

# ---------- Secrets ----------
BASE = st.secrets["erpnext"]["base_url"].rstrip("/")
VERIFY_SSL = st.secrets["erpnext"].get("verify_ssl", True)
MAPBOX_TOKEN = st.secrets.get("mapbox", {}).get("token", "")

# ---------- Optional map token for pydeck ----------
try:
    import pydeck as pdk  # noqa
    if MAPBOX_TOKEN:
        pdk.settings.mapbox_api_key = MAPBOX_TOKEN
except Exception:
    pdk = None  # will import inside Map screen

# ---------- Session State ----------
if "cookies" not in st.session_state:
    st.session_state.cookies = None
if "user" not in st.session_state:
    st.session_state.user = None
if "date_range" not in st.session_state:
    st.session_state.date_range = (date.today() - relativedelta(months=3), date.today())
if "nav" not in st.session_state:
    st.session_state.nav = "TTC"

# ---------- HTTP helpers ----------
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

# ---------- Data Fetchers ----------
@st.cache_data(show_spinner=False)
def list_companies(user_key: str):
    data = api_get("/api/resource/Company", fields=json.dumps(["name"]), order_by="name asc", limit_page_length=1000)
    return [row["name"] for row in data]

# Do NOT request base_outstanding_amount (forbidden in list API)
BASE_FIELDS = [
    "name", "posting_date", "company", "customer", "due_date",
    "base_grand_total", "grand_total", "currency",
    "outstanding_amount", "status", "conversion_rate",
]

@st.cache_data(show_spinner=True)
def fetch_invoices(
    companies, start: date, end: date, include_drafts: bool,
    extra_filters=None, fields_add=None, user_key: str=""
):
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
        if include_drafts:
            filters.append(["docstatus", "in", [0, 1]])
        else:
            filters.append(["docstatus", "=", 1])
        if extra_filters:
            filters += extra_filters

        params = {
            "fields": json.dumps(list(dict.fromkeys(fields))),
            "filters": json.dumps(filters),
            "order_by": "posting_date asc, name asc",
            "limit_page_length": 5000,
        }

        start_idx = 0
        while True:
            page = api_get("/api/resource/Sales Invoice", **{**params, "limit_start": start_idx})
            all_rows.extend(page)
            if len(page) < 5000:
                break
            start_idx += 5000

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    if "posting_date" in df.columns:
        df["posting_date"] = pd.to_datetime(df["posting_date"])
    if "due_date" in df.columns:
        df["due_date"] = pd.to_datetime(df["due_date"], errors="coerce")

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

# Customers (includes ONLY custom_lat/custom_lon)
@st.cache_data(show_spinner=True)
def list_customers():
    fields = ["name", "customer_name", "mobile_no", "territory", "custom_lat", "custom_lon"]
    all_rows = []
    start_idx = 0
    while True:
        page = api_get(
            "/api/resource/Customer",
            fields=json.dumps(fields),
            order_by="name asc",
            limit_page_length=5000,
            limit_start=start_idx
        )
        all_rows.extend(page)
        if len(page) < 5000:
            break
        start_idx += 5000
    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    # Robust display name fallback
    disp = df.get("customer_name").fillna("")
    disp = disp.replace("", np.nan)
    df["display_customer"] = disp.fillna(df.get("name"))

    df["custom_lat"] = pd.to_numeric(df.get("custom_lat"), errors="coerce")
    df["custom_lon"] = pd.to_numeric(df.get("custom_lon"), errors="coerce")
    return df

# ---------- UI: Login ----------
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

# ---------- Sidebar: NAV (buttons) + Filters ----------
SIDEBAR_CSS = """
<style>
.sidebar-nav { display: flex; flex-direction: column; gap: .6rem; margin-bottom: .75rem; }

.sidebar-nav .stButton > button {
  width: 100%;
  display: flex; align-items: center; gap: .6rem;
  padding: .55rem .75rem;
  border-radius: 12px;
  border: 1px solid rgba(200,200,200,.25);
  background: rgba(255,255,255,.02);
  font-weight: 600;
  position: relative;
  transition: transform .12s ease, box-shadow .15s ease, background .15s ease, border-color .15s ease;
}
.sidebar-nav .stButton > button:hover { background: rgba(255,255,255,.06); border-color: rgba(180,180,255,.45); }

@keyframes glowPulse {
  0%, 100% {
    box-shadow:
      0 0 0 2px rgba(90,120,255,.18) inset,
      0 0 14px rgba(90,120,255,.30),
      0 0 0 rgba(90,120,255,0);
  }
  50% {
    box-shadow:
      0 0 0 2px rgba(90,120,255,.22) inset,
      0 0 26px rgba(90,120,255,.60),
      0 0 24px rgba(90,120,255,.30);
  }
}
</style>
"""
st.sidebar.markdown(SIDEBAR_CSS, unsafe_allow_html=True)

def sidebar_nav_buttons():
    st.sidebar.markdown('<div class="sidebar-nav">', unsafe_allow_html=True)

    b1 = st.sidebar.button("📈  TTC", key="nav_ttc_btn", use_container_width=True)
    b2 = st.sidebar.button("💳  Debts", key="nav_debts_btn", use_container_width=True)
    b3 = st.sidebar.button("👥  Customers", key="nav_customers_btn", use_container_width=True)
    b4 = st.sidebar.button("🗺️  Map", key="nav_map_btn", use_container_width=True)

    if b1:
        st.session_state.nav = "TTC"; st.rerun()
    if b2:
        st.session_state.nav = "Debts"; st.rerun()
    if b3:
        st.session_state.nav = "Customers"; st.rerun()
    if b4:
        st.session_state.nav = "Map"; st.rerun()

    active_idx = {"TTC": 1, "Debts": 2, "Customers": 3, "Map": 4}[st.session_state.nav]

    st.sidebar.markdown(f"""
    <style>
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button {{
      border-color: rgba(90,120,255,.65) !important;
      background: linear-gradient(180deg, rgba(90,120,255,.12), rgba(90,120,255,.06)) !important;
      transform: translateZ(0) scale(1.01);
      animation: glowPulse 1.6s ease-in-out infinite;
      box-shadow:
        0 0 0 2px rgba(90,120,255,.18) inset,
        0 0 18px rgba(90,120,255,.45),
        0 0 36px rgba(90,120,255,.25);
    }}
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button:before {{
      content: "";
      position: absolute; left: 6px; top: 8px; bottom: 8px; width: 4px;
      border-radius: 6px;
      background: linear-gradient(180deg, rgba(90,120,255,1), rgba(90,120,255,.55));
      box-shadow: 0 0 10px rgba(90,120,255,.55);
    }}
    .sidebar-nav .stButton:nth-of-type({active_idx}) > button:after {{
      content: "";
      position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
      width: 8px; height: 8px; border-radius: 9999px;
      background: rgba(90,120,255,.95);
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
    if "All Companies" in selected or not selected:
        selected_companies = companies[:]
    else:
        selected_companies = [c for c in selected if c in companies]

    start_date, end_date = st.date_input("Date range", value=st.session_state.date_range, key="date_range")
    if isinstance(start_date, tuple):
        start_date, end_date = start_date[0], start_date[1]
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        st.error("Please select a valid start and end date.")
        st.stop()
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    include_drafts = st.toggle("Include Draft Invoices (for TTC only)", value=False, key="include_drafts")
    run = st.button("Load data")

# ---------- Helpers ----------
def download_button(df: pd.DataFrame, filename: str, label: str):
    if df.empty:
        return
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(label=label, data=csv, file_name=filename, mime="text/csv")

# ---------- Screens ----------
if run and st.session_state.nav == "TTC":
    st.subheader("💰 Revenue Analytics")
    try:
        inv = fetch_invoices(selected_companies, start_date, end_date, include_drafts, user_key=st.session_state.user or "")
        if inv.empty:
            st.info("No invoices found."); st.stop()

        # Create daily data
        all_daily = []
        for co, df_co in inv.groupby("company"):
            d = (
                df_co.set_index("posting_date")["TTC"]
                .resample("D").sum()
                .reset_index()
                .rename(columns={"posting_date": "date"})
            )
            d["company"] = co
            all_daily.append(d)
        daily_df = pd.concat(all_daily, ignore_index=True)

        # Metrics
        total_ttc = float(inv["TTC"].sum())
        inv_count = len(inv)
        avg_invoice = total_ttc / inv_count if inv_count > 0 else 0
        
        # Create metric cards
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Total Revenue</p>
                <p class="metric-value">{total_ttc:,.2f}</p>
                <p class="metric-label">across {inv_count:,} invoices</p>
            </div>
            """, unsafe_allow_html=True)
            
        with col2:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Avg. Invoice</p>
                <p class="metric-value">{avg_invoice:,.2f}</p>
                <p class="metric-label">per transaction</p>
            </div>
            """, unsafe_allow_html=True)
            
        with col3:
            st.markdown(f"""
            <div class="metric-card">
                <p class="metric-label">Date Range</p>
                <p class="metric-value">{len(daily_df['date'].dt.date.unique())} days</p>
                <p class="metric-label">{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}</p>
            </div>
            """, unsafe_allow_html=True)

        # Revenue Trends Chart
        st.markdown("### Revenue Trends")
        chart = alt.Chart(daily_df).mark_area(
            interpolate='monotone',
            line={'color':'#4C78A8'},
            color=alt.Gradient(
                gradient='linear',
                stops=[
                    alt.GradientStop(color='white', offset=0),
                    alt.GradientStop(color='#4C78A8', offset=1)
                ],
                x1=1, x2=1, y1=1, y2=0
            )
        ).encode(
            x=alt.X('date:T', title='Date', axis=alt.Axis(format='%b %Y')),
            y=alt.Y('sum(TTC):Q', title='Revenue'),
            color=alt.Color('company:N', title='Company', scale=alt.Scale(scheme='tableau20')),
            tooltip=[
                alt.Tooltip('date:T', title='Date', format='%b %d, %Y'),
                alt.Tooltip('company:N', title='Company'),
                alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')
            ]
        ).properties(
            height=400,
            title='Daily Revenue by Company'
        ).interactive()
        
        st.altair_chart(chart, use_container_width=True)
        
        # Monthly Breakdown
        st.markdown("### Monthly Breakdown")
        monthly_df = daily_df.copy()
        monthly_df['month'] = monthly_df['date'].dt.strftime('%Y-%m')
        
        monthly_chart = alt.Chart(monthly_df).mark_bar(
            cornerRadiusTopLeft=3,
            cornerRadiusTopRight=3
        ).encode(
            x=alt.X('month:O', title='Month', sort='x'),
            y=alt.Y('sum(TTC):Q', title='Revenue'),
            color=alt.Color('company:N', title='Company', scale=alt.Scale(scheme='tableau20')),
            tooltip=[
                alt.Tooltip('month:O', title='Month'),
                alt.Tooltip('company:N', title='Company'),
                alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')
            ]
        ).properties(
            height=400,
            title='Monthly Revenue by Company'
        )
        
        st.altair_chart(monthly_chart, use_container_width=True)
        
        # Top Customers
        st.markdown("### Top Customers")
        top_customers = inv.groupby('customer')['TTC'].sum().nlargest(10).reset_index()
        
        customer_chart = alt.Chart(top_customers).mark_bar(
            cornerRadiusTopRight=3,
            cornerRadiusBottomRight=3
        ).encode(
            x=alt.X('sum(TTC):Q', title='Revenue'),
            y=alt.Y('customer:N', title='', sort='-x'),
            color=alt.Color('sum(TTC):Q', scale=alt.Scale(scheme='blues'), legend=None),
            tooltip=[
                alt.Tooltip('customer:N', title='Customer'),
                alt.Tooltip('sum(TTC):Q', title='Revenue', format=',.2f')
            ]
        ).properties(
            height=400,
            title='Top 10 Customers by Revenue'
        )
        
        st.altair_chart(customer_chart, use_container_width=True)
        
        # Data tables in expanders
        with st.expander("📊 View Detailed Data"):
            tab1, tab2 = st.tabs(["Daily Totals", "Invoice Details"])
            
            with tab1:
                st.dataframe(
                    daily_df.sort_values(["date", "company"]), 
                    use_container_width=True,
                    height=400
                )
                download_button(daily_df, "ttc_daily.csv", "📥 Download Daily Data")
                
            with tab2:
                st.dataframe(
                    inv[["name", "posting_date", "company", "customer", "base_grand_total", "currency"]],
                    use_container_width=True,
                    height=400
                )
                download_button(inv, "ttc_invoices.csv", "📥 Download Invoice Data")

    except requests.HTTPError as e:
        st.error("❌ ERPNext API error.")
        try: 
            st.code(e.response.text)
        except Exception: 
            st.write(e)
    except Exception as e:
        st.error(f"❌ Error: {e}")

elif run and st.session_state.nav == "Debts":
    st.subheader("💳  Debts (A/R)")
    try:
        open_inv = fetch_outstanding_invoices(selected_companies, user_key=st.session_state.user or "")
        if open_inv.empty:
            st.info("No open debts found."); st.stop()

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
        if alt and not topN.empty:
            chart = (
                alt.Chart(topN)
                .mark_bar()
                .encode(
                    x=alt.X("Outstanding:Q", title="Outstanding"),
                    y=alt.Y("customer:N", sort="-x", title="Customer"),
                    color=alt.Color("company:N", title="Company"),
                    tooltip=["company:N","customer:N","Outstanding:Q","Invoices:Q","MaxOverdue:Q"]
                )
                .properties(height=400)
            )
            st.altair_chart(chart, use_container_width=True)
        elif not topN.empty:
            st.bar_chart(topN.set_index("customer")["Outstanding"])

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

elif run and st.session_state.nav == "Customers":
    st.subheader("👥  Customers Overview")
    try:
        inv_period = fetch_invoices(selected_companies, start_date, end_date, include_drafts=False, user_key=st.session_state.user or "")
        open_inv = fetch_outstanding_invoices(selected_companies, user_key=st.session_state.user or "")

        if inv_period.empty and open_inv.empty:
            st.info("No data for the selected filters."); st.stop()

        if not inv_period.empty:
            agg_period = (
                inv_period.groupby(["company","customer"], as_index=False)
                          .agg(Sales_TTC=("TTC","sum"), Invoices=("name","count"), Last_Invoice=("posting_date","max"))
            )
        else:
            agg_period = pd.DataFrame(columns=["company","customer","Sales_TTC","Invoices","Last_Invoice"])

        if not open_inv.empty:
            agg_open = (
                open_inv.groupby(["company","customer"], as_index=False)
                        .agg(Outstanding=("base_outstanding","sum"))
            )
        else:
            agg_open = pd.DataFrame(columns=["company","customer","Outstanding"])

        customers = pd.merge(agg_period, agg_open, on=["company","customer"], how="outer")
        for col in ["Sales_TTC","Invoices","Outstanding"]:
            if col in customers.columns:
                customers[col] = customers[col].fillna(0)
        if "Last_Invoice" in customers.columns:
            customers["Last_Invoice"] = pd.to_datetime(customers["Last_Invoice"])

        total_sales = float(customers.get("Sales_TTC", pd.Series()).sum())
        total_outstanding = float(customers.get("Outstanding", pd.Series()).sum())
        c1, c2 = st.columns(2)
        c1.metric("Total Sales TTC (period)", f"{total_sales:,.2f}")
        c2.metric("Total Outstanding (now)", f"{total_outstanding:,.2f}")

        q = st.text_input("Search customer")
        if q:
            customers = customers[customers["customer"].fillna("").str.contains(q, case=False, na=False)]

        st.dataframe(
            customers.sort_values(["company","Sales_TTC"], ascending=[True, False]),
            use_container_width=True
        )
        download_button(customers, "customers_overview.csv", "Download customers CSV")

        topC = customers.nlargest(20, "Sales_TTC")
        st.markdown("Top customers by sales (period)")
        if alt and not topC.empty:
            chart = (
                alt.Chart(topC)
                .mark_bar()
                .encode(
                    x=alt.X("Sales_TTC:Q", title="Sales TTC (period)"),
                    y=alt.Y("customer:N", sort="-x", title="Customer"),
                    color=alt.Color("company:N", title="Company"),
                    tooltip=["company:N","customer:N","Sales_TTC:Q","Invoices:Q","Outstanding:Q","Last_Invoice:T"]
                )
                .properties(height=400)
            )
            st.altair_chart(chart, use_container_width=True)
        elif not topC.empty:
            st.bar_chart(topC.set_index("customer")["Sales_TTC"])

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")

elif run and st.session_state.nav == "Map":
    st.subheader("🗺️  Clients Sales Map (from Customer.custom_lat/custom_lon only)")
    st.caption("Blue = customers who bought in the selected period; Gray = customers who did not.")

    # ---- Small UI controls for size/readability ----
    colz1, colz2, colz3 = st.columns([1,1,1])
    with colz1:
        sold_px = st.slider("Sold point size (px)", 2, 16, 6)
    with colz2:
        idle_px = st.slider("No-sale point size (px)", 2, 16, 5)
    with colz3:
        jitter_on = st.toggle("Anti-overlap jitter", value=True, help="Adds tiny random offsets so nearby customers don't overlap.")

    try:
        # Buyers in selected period (coloring only)
        inv_period = fetch_invoices(
            selected_companies, start_date, end_date,
            include_drafts=False,
            fields_add=[],
            user_key=st.session_state.user or ""
        )
        buyers_period = set(inv_period["customer"].dropna()) if not inv_period.empty else set()

        # All customers with coords
        cust_df = list_customers()
        if cust_df.empty:
            st.info("No customers found."); st.stop()

        data = cust_df.copy()
        data["status"] = np.where(data["name"].isin(buyers_period), "Sold (period)", "No sale in period")
        data.rename(columns={"display_customer": "customer"}, inplace=True)

        # Keep only rows with valid coordinates
        plot_df = data.dropna(subset=["custom_lat", "custom_lon"]).copy()
        if plot_df.empty:
            st.warning("No customers have coordinates (custom_lat/custom_lon). Add them on Customer records.")
            st.stop()

        # Optional: tiny deterministic jitter (so close points don't fully overlap)
        if jitter_on:
            # jitter size ~ a few meters; ~1e-5 deg ≈ 1.1 m
            def dj(seed):
                h = abs(hash(str(seed))) % 10_000
                return (h / 10_000.0 - 0.5) * 0.00005  # ~±0.000025 deg ≈ ±2–3 m
            plot_df["lat"] = plot_df["custom_lat"] + plot_df["name"].map(dj)
            plot_df["lon"] = plot_df["custom_lon"] + plot_df["name"].map(lambda x: dj(str(x)+"x"))
        else:
            plot_df["lat"] = plot_df["custom_lat"]
            plot_df["lon"] = plot_df["custom_lon"]

        # KPIs
        sold_n = int((plot_df["status"] == "Sold (period)").sum())
        nosale_n = int((plot_df["status"] == "No sale in period").sum())
        c1, c2 = st.columns(2)
        c1.metric("Customers with sales (period)", f"{sold_n:,}")
        c2.metric("Customers with no sales (period)", f"{nosale_n:,}")

        # Map
        import pydeck as pdk
        center_lat = float(plot_df["lat"].mean())
        center_lon = float(plot_df["lon"].mean())

        sold_df = plot_df[plot_df["status"] == "Sold (period)"]
        idle_df = plot_df[plot_df["status"] == "No sale in period"]

        # Use pixel-based radii so size is consistent at any zoom
        layer_sold = pdk.Layer(
            "ScatterplotLayer",
            data=sold_df,
            get_position='[lon, lat]',
            get_radius=sold_px,
            radius_units="pixels",
            radius_min_pixels=sold_px,
            radius_max_pixels=sold_px,
            stroked=True,
            get_line_color=[255, 255, 255, 220],
            line_width_min_pixels=1,
            pickable=True,
            get_fill_color=[0, 140, 255, 200],
        )
        layer_idle = pdk.Layer(
            "ScatterplotLayer",
            data=idle_df,
            get_position='[lon, lat]',
            get_radius=idle_px,
            radius_units="pixels",
            radius_min_pixels=idle_px,
            radius_max_pixels=idle_px,
            stroked=True,
            get_line_color=[255, 255, 255, 200],
            line_width_min_pixels=1,
            pickable=True,
            get_fill_color=[160, 160, 160, 160],
        )

        view = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=5.2)
        tooltip = {"text": "{customer}\nStatus: {status}"}

        deck = pdk.Deck(layers=[layer_idle, layer_sold], initial_view_state=view, tooltip=tooltip)
        if MAPBOX_TOKEN:
            deck.map_style = "mapbox://styles/mapbox/light-v9"
        st.pydeck_chart(deck, use_container_width=True)

        with st.expander("Mapped customers (table)"):
            show_cols = ["customer","status","custom_lat","custom_lon"]
            st.dataframe(plot_df[show_cols].sort_values(["status","customer"]), use_container_width=True)
            download_button(plot_df[show_cols], "customers_map.csv", "Download CSV")

    except requests.HTTPError as e:
        st.error("ERPNext API error.")
        try: st.code(e.response.text)
        except Exception: st.write(e)
    except Exception as e:
        st.error(f"Error: {e}")
