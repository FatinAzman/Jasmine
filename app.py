import os
import json
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta

import requests
import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request


# ----------------------------
# App config
# ----------------------------
st.set_page_config(page_title="jasmine", layout="centered")

APP_FOLDER_NAME = "jasmine"
SPREADSHEET_NAME = "jasmine_receipts_metadata"
TAB_RECEIPTS = "receipts"
TAB_SPLITS = "splits"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

CLIENT_ID = st.secrets["google_oauth"]["client_id"]
CLIENT_SECRET = st.secrets["google_oauth"]["client_secret"]
REDIRECT_URI = st.secrets["google_oauth"]["redirect_uri"]

# Local token cache (7 days)
TOKEN_CACHE_DIR = Path.home() / ".jasmine"
TOKEN_CACHE_PATH = TOKEN_CACHE_DIR / "token_cache.json"
LOGIN_TTL_DAYS = 7


# ----------------------------
# Small utils
# ----------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def format_dt(s: str) -> str:
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# ----------------------------
# OAuth helpers
# ----------------------------
def build_flow():
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)

def get_query_params():
    if hasattr(st, "query_params"):
        return st.query_params
    return st.experimental_get_query_params()

def clear_query_params():
    if hasattr(st, "query_params"):
        st.query_params.clear()
    else:
        st.experimental_set_query_params()

def load_cached_creds():
    """Load cached creds if not older than LOGIN_TTL_DAYS."""
    if not TOKEN_CACHE_PATH.exists():
        return None

    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(data.get("saved_at"))
        if datetime.now() - saved_at > timedelta(days=LOGIN_TTL_DAYS):
            # expired cache
            try:
                TOKEN_CACHE_PATH.unlink()
            except Exception:
                pass
            return None

        creds_info = data.get("creds")
        if not creds_info:
            return None

        creds = Credentials(**creds_info)
        return creds
    except Exception:
        return None

def save_cached_creds(creds: Credentials):
    TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": now_iso(),
        "creds": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
    }
    TOKEN_CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def ensure_valid_creds(creds: Credentials) -> Credentials:
    """Refresh token if needed."""
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_cached_creds(creds)
    return creds


# ----------------------------
# Google API clients
# ----------------------------
def get_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)

def get_sheets_service(creds: Credentials):
    return build("sheets", "v4", credentials=creds)


# ----------------------------
# Drive helpers
# ----------------------------
def get_or_create_folder(drive, parent_id, name):
    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{name}' and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"

    res = drive.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]

    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]

def list_child_folders(drive, parent_id):
    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"'{parent_id}' in parents and trashed=false"
    )
    res = drive.files().list(q=q, fields="files(id,name)", pageSize=1000).execute()
    files = res.get("files", [])
    return sorted(files, key=lambda x: x["name"].lower())

def ensure_base_structure(drive):
    root = get_or_create_folder(drive, None, APP_FOLDER_NAME)

    receipts_root = get_or_create_folder(drive, root, "Receipts")
    tax_root = get_or_create_folder(drive, root, "Tax")
    splits_root = get_or_create_folder(drive, root, "Splits")

    receipts_bucket_ids = {
        "Spending": get_or_create_folder(drive, receipts_root, "Spending"),
        "Debt": get_or_create_folder(drive, receipts_root, "Debt"),
        "Etc": get_or_create_folder(drive, receipts_root, "Etc"),
    }

    tax_bucket_ids = {
        "Health": get_or_create_folder(drive, tax_root, "Health"),
        "Zakat": get_or_create_folder(drive, tax_root, "Zakat"),
        "IT": get_or_create_folder(drive, tax_root, "IT"),
    }

    return {
        "root": root,
        "receipts_root": receipts_root,
        "tax_root": tax_root,
        "splits_root": splits_root,
        "receipts_buckets": receipts_bucket_ids,
        "tax_buckets": tax_bucket_ids,
    }

def upload_file(drive, local_path, filename, folder_id):
    meta = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    f = drive.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
    return f["id"], f["webViewLink"]


# ----------------------------
# FX: MYR base
# ----------------------------
@st.cache_data(ttl=6 * 60 * 60)
def fetch_rates_base_myr():
    url = "https://api.exchangerate-api.com/v4/latest/MYR"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("rates", {}), data.get("date", "")

def to_myr(amount: float, currency: str, rates_base_myr: dict) -> float:
    if currency == "MYR":
        return amount
    rate = rates_base_myr.get(currency)
    if rate is None or rate == 0:
        raise ValueError(f"Missing FX rate for {currency}.")
    return amount / float(rate)


# ----------------------------
# Sheets helpers
# ----------------------------
def find_sheet_in_folder(drive, folder_id, spreadsheet_name):
    q = (
        "mimeType='application/vnd.google-apps.spreadsheet' and "
        f"name='{spreadsheet_name}' and trashed=false and '{folder_id}' in parents"
    )
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def create_sheet_in_folder(drive, sheets, folder_id, spreadsheet_name):
    ss = sheets.spreadsheets().create(body={"properties": {"title": spreadsheet_name}}).execute()
    ss_id = ss["spreadsheetId"]
    drive.files().update(fileId=ss_id, addParents=folder_id, fields="id, parents").execute()
    return ss_id

def ensure_tab_exists(sheets, spreadsheet_id, tab_name):
    ss = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in ss.get("sheets", [])}
    if tab_name in existing:
        return
    req = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=req).execute()

def ensure_header(sheets, spreadsheet_id, tab_name, header):
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1:Z1"
    ).execute()
    values = res.get("values", [])
    if not values or not any(values[0]):
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()

def append_row(sheets, spreadsheet_id, tab_name, row):
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A:Z",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

def append_rows(sheets, spreadsheet_id, tab_name, rows):
    if not rows:
        return
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A:Z",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

def fetch_rows(sheets, spreadsheet_id, tab_name, max_rows=500):
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1:Z{max_rows}"
    ).execute()
    values = res.get("values", [])
    if not values or len(values) < 2:
        return []
    header = values[0]
    rows = []
    for r in values[1:]:
        d = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        rows.append(d)
    rows.reverse()
    return rows


# ----------------------------
# UI
# ----------------------------
st.title("jasmine 🌸")
st.caption("Personal receipt tracker (Drive + Sheets).")

# ----------------------------
# Login (persist 7 days)
# ----------------------------
if "creds_obj" not in st.session_state:
    st.session_state.creds_obj = None

# Try cached creds first
if st.session_state.creds_obj is None:
    cached = load_cached_creds()
    if cached:
        st.session_state.creds_obj = ensure_valid_creds(cached)

params = get_query_params()

# If still not logged in, start OAuth
if st.session_state.creds_obj is None and "code" not in params:
    flow = build_flow()
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        include_granted_scopes="true",
        access_type="offline"
    )
    st.link_button("Sign in with Google", auth_url)
    st.stop()

# Handle OAuth redirect
if st.session_state.creds_obj is None and "code" in params:
    flow = build_flow()
    code_val = params["code"]
    code = code_val[0] if isinstance(code_val, list) else str(code_val)

    flow.fetch_token(code=code)
    creds = flow.credentials
    save_cached_creds(creds)
    st.session_state.creds_obj = creds
    clear_query_params()
    st.rerun()

creds = ensure_valid_creds(st.session_state.creds_obj)

drive = get_drive_service(creds)
sheets = get_sheets_service(creds)

# Base folders
base = ensure_base_structure(drive)

# Spreadsheet
spreadsheet_id = find_sheet_in_folder(drive, base["root"], SPREADSHEET_NAME)
if not spreadsheet_id:
    spreadsheet_id = create_sheet_in_folder(drive, sheets, base["root"], SPREADSHEET_NAME)

ensure_tab_exists(sheets, spreadsheet_id, TAB_RECEIPTS)
ensure_tab_exists(sheets, spreadsheet_id, TAB_SPLITS)

HEADER_RECEIPTS = [
    "created_at", "scope", "bucket", "split_group",
    "purchase_date", "merchant",
    "amount", "currency", "amount_myr", "notes",
    "file_name", "file_id", "file_link"
]
HEADER_SPLITS = [
    "created_at", "receipt_file_id", "split_group",
    "split_method",
    "split_to", "split_amount", "split_percent"
]
ensure_header(sheets, spreadsheet_id, TAB_RECEIPTS, HEADER_RECEIPTS)
ensure_header(sheets, spreadsheet_id, TAB_SPLITS, HEADER_SPLITS)

# FX
fx_ok = True
rates, fx_date = {}, ""
try:
    rates, fx_date = fetch_rates_base_myr()
except Exception:
    fx_ok = False

tab_add, tab_history = st.tabs(["➕ Add", "📜 History"])

# ----------------------------
# Add tab
# ----------------------------
with tab_add:
    scope = st.sidebar.radio("Scope", ["Receipt", "Tax", "Split"], index=0)

    bucket = ""
    if scope == "Receipt":
        bucket = st.sidebar.radio("Category", ["Spending", "Debt", "Etc"])
    elif scope == "Tax":
        bucket = st.sidebar.radio("Category", ["Health", "Zakat", "IT"])

    split_group_name = ""
    if scope == "Split":
        st.sidebar.subheader("Split folder (inside jasmine/Splits)")
        existing_groups = list_child_folders(drive, base["splits_root"])
        existing_names = [f["name"] for f in existing_groups]
        choice = st.sidebar.selectbox("Select existing group", ["(Create new...)"] + existing_names)
        if choice == "(Create new...)":
            split_group_name = st.sidebar.text_input("New group name", placeholder="e.g. FriendsTrip / Family / Roommate")
        else:
            split_group_name = choice
        split_group_name = (split_group_name or "").strip()
        if not split_group_name:
            st.sidebar.warning("Enter or select a Split group name to continue.")

    if fx_ok and fx_date:
        st.sidebar.caption(f"FX base: MYR • last updated: {fx_date}")

    mode = st.radio("Add receipt via", ["Upload file", "Take photo"], horizontal=True)
    uploaded = None
    if mode == "Upload file":
        uploaded = st.file_uploader("Upload receipt (JPG/PNG/PDF)", type=["jpg", "jpeg", "png", "pdf"])
    else:
        cam = st.camera_input("Take a photo")
        if cam is not None:
            uploaded = cam

    purchase_date = st.date_input("Purchase date", value=date.today())
    merchant = st.text_input("Merchant / Store")

    col1, col2 = st.columns(2)
    with col1:
        amount = st.number_input("Amount", min_value=0.0, step=1.0)
    with col2:
        currency = st.selectbox("Currency", ["MYR", "SAR", "USD", "EUR"])

    amount_myr = None
    if fx_ok:
        try:
            amount_myr = to_myr(float(amount), currency, rates)
            st.caption(f"**Converted (MYR):** {amount_myr:,.2f} MYR")
        except Exception:
            st.caption("**Converted (MYR):** (unavailable)")
    else:
        st.caption("**Converted (MYR):** (unavailable)")

    notes = st.text_area("Notes (optional)")

    # Split UI
    splits = []
    split_method = None

    if scope == "Split":
        st.subheader("Split payment")

        split_method = st.radio("Split method", ["Equal split", "By amount", "By percent"], horizontal=True)
        num_splits = st.number_input("Number of splits", min_value=1, max_value=10, value=3, step=1)
        total_amount = float(amount)

        if split_method == "Equal split":
            names = []
            for i in range(int(num_splits)):
                names.append(st.text_input(f"Split to #{i+1}", key=f"split_to_{i}"))

            if total_amount > 0:
                per = round(total_amount / int(num_splits), 2)
                amounts = [per] * int(num_splits)
                remainder = round(total_amount - sum(amounts), 2)
                if amounts:
                    amounts[0] = round(amounts[0] + remainder, 2)

                for i in range(int(num_splits)):
                    splits.append((names[i], float(amounts[i]), ""))

                st.metric("Split total", f"{sum(amounts):,.2f} {currency}")
                st.metric("Remaining balance", f"{round(total_amount - sum(amounts), 2):,.2f} {currency}")
            else:
                st.caption("Enter Amount > 0 to calculate equal split.")

        elif split_method == "By amount":
            key_amt_list = f"split_amounts_{int(num_splits)}"
            if key_amt_list not in st.session_state or len(st.session_state[key_amt_list]) != int(num_splits):
                st.session_state[key_amt_list] = [0.0] * int(num_splits)

            names = []
            amounts = [0.0] * int(num_splits)
            prev_sum = 0.0

            for i in range(int(num_splits)):
                c1, c2 = st.columns([2, 1])
                with c1:
                    nm = st.text_input(f"Split to #{i+1}", key=f"split_to_{i}")
                    names.append(nm)

                with c2:
                    if i < int(num_splits) - 1:
                        val = st.number_input(
                            f"Amount #{i+1}",
                            min_value=0.0,
                            step=1.0,
                            key=f"split_amt_{i}",
                            value=float(st.session_state[key_amt_list][i]),
                        )
                        st.session_state[key_amt_list][i] = float(val)
                        amounts[i] = float(val)
                    else:
                        prev_sum = round(sum(st.session_state[key_amt_list][0:int(num_splits)-1]), 2)
                        remaining_val = round(total_amount - prev_sum, 2)
                        if remaining_val < 0:
                            remaining_val = 0.0
                        amounts[i] = remaining_val
                        st.number_input(
                            f"Amount #{i+1} (auto)",
                            value=float(remaining_val),
                            step=1.0,
                            disabled=True,
                            key=f"split_amt_last_display_{int(num_splits)}",
                        )

            for i in range(int(num_splits)):
                splits.append((names[i], float(amounts[i]), ""))

            split_total = round(sum(amounts), 2)
            remaining = round(total_amount - split_total, 2)
            st.metric("Split total", f"{split_total:,.2f} {currency}")
            st.metric("Remaining balance", f"{remaining:,.2f} {currency}")

            if prev_sum > total_amount:
                st.warning("Your first splits exceed the total amount. Please adjust the earlier amounts.")

        else:  # By percent
            key_pct_list = f"split_pcts_{int(num_splits)}"
            if key_pct_list not in st.session_state or len(st.session_state[key_pct_list]) != int(num_splits):
                st.session_state[key_pct_list] = [0.0] * int(num_splits)

            names = []
            pcts = [0.0] * int(num_splits)
            prev_pct = 0.0

            for i in range(int(num_splits)):
                c1, c2 = st.columns([2, 1])
                with c1:
                    nm = st.text_input(f"Split to #{i+1}", key=f"split_to_{i}")
                    names.append(nm)

                with c2:
                    if i < int(num_splits) - 1:
                        pct = st.number_input(
                            f"% #{i+1}",
                            min_value=0.0,
                            max_value=100.0,
                            step=1.0,
                            key=f"split_pct_{i}",
                            value=float(st.session_state[key_pct_list][i]),
                        )
                        st.session_state[key_pct_list][i] = float(pct)
                        pcts[i] = float(pct)
                    else:
                        prev_pct = round(sum(st.session_state[key_pct_list][0:int(num_splits)-1]), 2)
                        remaining_pct = round(100.0 - prev_pct, 2)
                        if remaining_pct < 0:
                            remaining_pct = 0.0
                        pcts[i] = remaining_pct
                        st.number_input(
                            f"% #{i+1} (auto)",
                            value=float(remaining_pct),
                            step=1.0,
                            disabled=True,
                            key=f"split_pct_last_display_{int(num_splits)}",
                        )

            total_pct = round(sum(pcts), 2)
            remaining_pct = round(100.0 - total_pct, 2)
            st.metric("Total %", f"{total_pct:.2f}%")
            st.metric("Remaining %", f"{remaining_pct:.2f}%")

            if prev_pct > 100.0:
                st.warning("Your first percents exceed 100%. Please adjust the earlier percentages.")

            for i in range(int(num_splits)):
                splits.append((names[i], "", float(pcts[i])))

    def get_target_folder_id():
        if scope == "Receipt":
            return base["receipts_buckets"][bucket], ""
        if scope == "Tax":
            return base["tax_buckets"][bucket], ""
        # Split -> jasmine/Splits/<GroupName>
        if not split_group_name:
            return None, split_group_name
        split_group_id = get_or_create_folder(drive, base["splits_root"], split_group_name)
        return split_group_id, split_group_name

    target_folder_id, split_group = get_target_folder_id()

    def validate_before_save():
        errors = []
        if uploaded is None:
            errors.append("Please upload a file or take a photo.")
        if (merchant or "").strip() == "":
            errors.append("Merchant / Store cannot be empty.")
        if float(amount) <= 0:
            errors.append("Amount must be greater than 0.")
        if scope == "Split":
            if not split_group_name:
                errors.append("Please select or enter a Split group name.")
            if split_method == "By amount":
                key_amt_list = f"split_amounts_{int(num_splits)}"
                prev_sum = round(sum(st.session_state[key_amt_list][0:int(num_splits)-1]), 2)
                if prev_sum > float(amount):
                    errors.append("Split amounts exceed total amount. Fix the earlier amounts.")
            if split_method == "By percent":
                key_pct_list = f"split_pcts_{int(num_splits)}"
                prev_pct = round(sum(st.session_state[key_pct_list][0:int(num_splits)-1]), 2)
                if prev_pct > 100.0:
                    errors.append("Split percentages exceed 100%. Fix the earlier percentages.")
        return errors

    if uploaded is not None:
        if scope == "Receipt":
            st.info(f"Saving into: jasmine/Receipts/{bucket}")
        elif scope == "Tax":
            st.info(f"Saving into: jasmine/Tax/{bucket}")
        else:
            st.info(f"Saving into: jasmine/Splits/{split_group}")

    if st.button("Save"):
        errs = validate_before_save()
        if errs:
            for e in errs:
                st.error(e)
            st.stop()

        if hasattr(uploaded, "name") and getattr(uploaded, "name", "") and "." in uploaded.name:
            ext = "." + uploaded.name.split(".")[-1].lower()
        else:
            ext = ".jpg"

        safe_merchant = (merchant.strip() or "Unknown").replace(" ", "_")

        if scope == "Split":
            filename = f"{purchase_date.isoformat()}_{safe_merchant}_{float(amount):.2f}{currency}_{split_group}{ext}"
        else:
            filename = f"{purchase_date.isoformat()}_{safe_merchant}_{float(amount):.2f}{currency}_{bucket}{ext}"

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(uploaded.getbuffer())
            tmp_path = tmp.name

        file_id, link = upload_file(drive, tmp_path, filename, target_folder_id)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        created_at = now_iso()

        receipt_row = [
            created_at,
            scope,
            bucket if scope != "Split" else "",
            split_group if scope == "Split" else "",
            purchase_date.isoformat(),
            merchant,
            float(amount),
            currency,
            float(amount_myr) if amount_myr is not None else "",
            notes,
            filename,
            file_id,
            link,
        ]
        append_row(sheets, spreadsheet_id, TAB_RECEIPTS, receipt_row)

        if scope == "Split":
            split_rows = []
            if split_method == "By percent":
                for split_to, _, pct in splits:
                    pct_val = float(pct or 0)
                    split_amt = (float(amount) * pct_val / 100.0)
                    split_rows.append([created_at, file_id, split_group, split_method, split_to, split_amt, pct_val])
            else:
                for split_to, split_amt, _ in splits:
                    split_rows.append([created_at, file_id, split_group, split_method, split_to, float(split_amt or 0), ""])
            append_rows(sheets, spreadsheet_id, TAB_SPLITS, split_rows)

        st.success("Saved ✅")
        st.link_button("Open receipt in Drive", link)
        st.link_button("Open metadata sheet", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}")


# ----------------------------
# History tab (with split breakdown)
# ----------------------------
with tab_history:
    st.subheader("History")
    st.caption("Latest entries from your Google Sheet (newest first).")

    h_scope = st.selectbox("Scope", ["All", "Receipt", "Tax", "Split"], index=0)
    h_limit = st.slider("Show last N receipts", min_value=10, max_value=200, value=50, step=10)

    # Fetch both tabs once
    receipts_rows = fetch_rows(sheets, spreadsheet_id, TAB_RECEIPTS, max_rows=800)
    splits_rows = fetch_rows(sheets, spreadsheet_id, TAB_SPLITS, max_rows=2000)

    # Build index: receipt_file_id -> list of splits
    splits_by_receipt = {}
    for s in splits_rows:
        rid = s.get("receipt_file_id", "")
        if not rid:
            continue
        splits_by_receipt.setdefault(rid, []).append(s)

    # Filter receipts
    filtered = []
    for r in receipts_rows:
        if h_scope != "All" and r.get("scope", "") != h_scope:
            continue
        filtered.append(r)

    filtered = filtered[:h_limit]

    if not filtered:
        st.info("No history yet (or filter is too strict).")
    else:
        for r in filtered:
            scope_val = r.get("scope", "")
            bucket_val = r.get("bucket", "")
            group_val = r.get("split_group", "")

            label_mid = bucket_val if scope_val != "Split" else group_val
            title = f"{scope_val} • {label_mid} • {r.get('merchant','')}"

            with st.expander(title, expanded=False):
                st.write("**Created:**", format_dt(r.get("created_at", "")))
                st.write("**Purchase date:**", r.get("purchase_date", ""))
                st.write("**Amount:**", r.get("amount", ""), r.get("currency", ""))
                st.write("**MYR:**", r.get("amount_myr", ""))
                if group_val:
                    st.write("**Split group:**", group_val)
                if r.get("notes"):
                    st.write("**Notes:**", r.get("notes", ""))

                if r.get("file_link"):
                    st.link_button("Open receipt in Drive", r.get("file_link"))

                # ✅ Split breakdown
                if scope_val == "Split":
                    rid = r.get("file_id", "")
                    items = splits_by_receipt.get(rid, [])
                    if not items:
                        st.info("No split breakdown found for this receipt (splits tab empty).")
                    else:
                        # determine method (should be consistent)
                        method = items[0].get("split_method", "")
                        st.markdown("### Split breakdown")
                        st.caption(f"Method: **{method}**")

                        total = safe_float(r.get("amount", 0))
                        currency = r.get("currency", "")

                        show_rows = []
                        sum_amt = 0.0
                        sum_pct = 0.0
                        for it in items:
                            name = it.get("split_to", "")
                            amt = safe_float(it.get("split_amount", 0))
                            pct = it.get("split_percent", "")
                            sum_amt += amt
                            sum_pct += safe_float(pct, 0.0)
                            show_rows.append((name, amt, pct))

                        for name, amt, pct in show_rows:
                            if method == "By percent":
                                st.write(f"- **{name}**: {pct}%  →  {amt:,.2f} {currency}")
                            else:
                                st.write(f"- **{name}**: {amt:,.2f} {currency}")

                        remaining = round(total - sum_amt, 2)
                        if remaining < 0:
                            remaining = 0.0  # no negative

                        st.markdown("---")
                        st.write(f"**Split total:** {sum_amt:,.2f} {currency}")
                        st.write(f"**Remaining balance:** {remaining:,.2f} {currency}")
