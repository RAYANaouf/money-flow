# Streamlit ERPNext login + TTC by date (company currency)
import streamlit as st
import requests
import pandas as pd
import json
from datetime import date
from dateutil.relativedelta import relativedelta

st.set_page_config(page_title="ERPNext Login • TTC by Date", layout="wide")

BASE = st.secrets["erpnext"]["base_url"].rstrip("/")
VERIFY_SSL = st.secrets["erpnext"].get("verify_ssl", True)

# ---------- Session State ----------
if "cookies" not in st.session_state:
    st.session_state.cookies = None
if "user" not in st.session_state:
    st.session_state.user = None

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

# ---------- Data Fetchers ----------
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
    filters = [
        ["company", "=", company],
        ["posting_date", ">=", str(start)],
        ["posting_date", "<=", str(end)],
    ]
    if include_drafts:
        filters.append(["docstatus", "in", [0, 1]])
    else:
        filters.append(["docstatus", "=", 1])

    params = {
        "fields": json.dumps(["name", "posting_date", "base_grand_total", "currency"]),
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
    df["TTC"] = df["base_grand_total"]
    return df

# ---------- UI ----------
st.title("ERPNext Login • TTC by Date")

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
    try:
        companies = list_companies(st.session_state.user or "")
    except Exception as e:
        st.error(f"Cannot load companies: {e}")
        st.stop()

    company = st.selectbox("Company", companies)
    end_date = st.date_input("End date", value=date.today())
    start_date = st.date_input("Start date", value=end_date - relativedelta(months=3))
    include_drafts = st.toggle("Include Draft Invoices (testing)", value=False)
    run = st.button("Load data")

if run:
    try:
        df = fetch_sales_invoices(company, start_date, end_date, include_drafts, st.session_state.user or "")
        if df.empty:
            st.info("No invoices found. Try a wider date range or include drafts.")
        else:
           # Aggregate by calendar day using resample (robust and always gives a date column)
            daily = (
                df.set_index("posting_date")
                  .resample("D")["TTC"]
                  .sum()
                  .reset_index()
                  .rename(columns={"posting_date": "date"})
            )

            st.subheader("TTC by Date")
            st.line_chart(daily.set_index("date")["TTC"])

            with st.expander("Daily totals"):
                st.dataframe(daily, use_container_width=True)

            total_ttc = float(df["TTC"].sum())
            inv_count = len(df)

            c1, c2 = st.columns(2)
            c1.metric("Total TTC (company currency)", f"{total_ttc:,.2f}")
            c2.metric("Invoices", f"{inv_count:,}")

            with st.expander("Invoice rows"):
                st.dataframe(
                    df[["name", "posting_date", "base_grand_total", "currency"]],
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
