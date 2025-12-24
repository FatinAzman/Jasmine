import os
import json
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta

import streamlit as st
import requests
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# =============================
# App config
# =============================
st.set_page_config(page_title="Jasmine", layout="centered")

APP_FOLDER_NAME = "Jasmine"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
LOGIN_TTL_DAYS = 30

CLIENT_ID = st.secrets["google_oauth"]["client_id"]
CLIENT_SECRET = st.secrets["google_oauth"]["client_secret"]
REDIRECT_URI = st.secrets["google_oauth"]["redirect_uri"]

# =============================
# Styling
# =============================
st.markdown("""
<style>
div[data-baseweb="segmented-control"] button[aria-checked="true"] {
    background-color:#FB7185 !important;
    border-color:#FB7185 !important;
    color:white !important;
}
.big-number { font-size:40px; font-weight:600; }
.muted { color:#9CA3AF; }
</style>
""", unsafe_allow_html=True)

# =============================
# FX
# =============================
@st.cache_data(ttl=6 * 60 * 60)
def fetch_rates():
    r = requests.get("https://api.exchangerate-api.com/v4/latest/MYR", timeout=20)
    return r.json()["rates"]

rates = fetch_rates()

def to_myr(amount, currency):
    return amount if currency == "MYR" else amount / rates.get(currency, 1)

# =============================
# OAuth helpers
# =============================
TOKEN_CACHE = Path.home() / ".jasmine_token.json"

def build_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

def load_creds():
    if TOKEN_CACHE.exists():
        data = json.loads(TOKEN_CACHE.read_text())
        if datetime.now() - datetime.fromisoformat(data["saved"]) < timedelta(days=LOGIN_TTL_DAYS):
            return Credentials(**data["creds"])
    return None

def save_creds(creds):
    TOKEN_CACHE.write_text(json.dumps({
        "saved": datetime.now().isoformat(),
        "creds": {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
    }))

# =============================
# Google Drive helpers
# =============================
def drive_service():
    return build("drive", "v3", credentials=st.session_state.creds)

def get_or_create_folder(service, name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    res = service.files().list(q=q, fields="files(id)").execute()
    if res["files"]:
        return res["files"][0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def init_drive_structure():
    service = drive_service()

    root = get_or_create_folder(service, APP_FOLDER_NAME)

    defaults = {
        "Spending Tracker": ["Spending", "Debt", "Etc"],
        "Income Tax Tracker": ["Health", "Zakat", "Electronics", "Insurance", "Others"],
        "Split Spending": []
    }

    folder_map = {}
    for tracker, cats in defaults.items():
        t_id = get_or_create_folder(service, tracker, root)
        folder_map[tracker] = {"_id": t_id}
        for c in cats:
            folder_map[tracker][c] = get_or_create_folder(service, c, t_id)

    st.session_state["drive_folders"] = folder_map

def upload_receipt(file, tracker, category, meta):
    service = drive_service()
    folder_id = st.session_state["drive_folders"][tracker][category]

    suffix = Path(file.name).suffix
    filename = f"{meta['date']}_{meta['merchant']}_{meta['amount']}_{meta['currency']}{suffix}"

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(file.getbuffer())
        media = MediaFileUpload(tmp.name, resumable=True)

    service.files().create(
        media_body=media,
        body={"name": filename, "parents": [folder_id]},
        fields="id"
    ).execute()

# =============================
# Header
# =============================
st.title("Jasmine 🌸")
st.caption("Spend smart. Split easy.")

# =============================
# Login
# =============================
if "creds" not in st.session_state:
    st.session_state.creds = load_creds()

params = st.query_params

if st.session_state.creds is None and "code" not in params:
    flow = build_flow()
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    st.link_button("Sign in with Google", url)
    st.stop()

if st.session_state.creds is None and "code" in params:
    flow = build_flow()
    flow.fetch_token(code=params["code"])
    st.session_state.creds = flow.credentials
    save_creds(flow.credentials)
    st.query_params.clear()
    st.rerun()

# Init Drive folders once
if "drive_folders" not in st.session_state:
    init_drive_structure()

tab_add, tab_history = st.tabs(["➕ Add", "📜 History"])

# =============================
# ADD TAB
# =============================
with tab_add:
    st.markdown("## Tracker")
    tracker = st.segmented_control(
        "",
        ["Spending Tracker", "Income Tax Tracker", "Split Spending"],
        default="Spending Tracker",
    )

    st.markdown("## Sub Category")

    defaults = list(st.session_state["drive_folders"][tracker].keys())
    defaults.remove("_id")
    if tracker != "Income Tax Tracker":
        defaults.append("➕ Create New")

    choice = st.selectbox("", defaults)

    if choice == "➕ Create New":
        subcategory = st.text_input("Create New Sub Category Name:")
        if subcategory:
            service = drive_service()
            parent = st.session_state["drive_folders"][tracker]["_id"]
            cid = get_or_create_folder(service, subcategory, parent)
            st.session_state["drive_folders"][tracker][subcategory] = cid
            st.success("Folder created in Google Drive")
            st.rerun()
    else:
        subcategory = choice

    st.divider()

    uploaded = st.file_uploader("Upload receipt (JPG / PNG / PDF)")
    purchase_date = st.date_input("Purchase date", date.today())
    merchant = st.text_input("Merchant / Store")

    c1, c2 = st.columns(2)
    with c1:
        amount = st.number_input("Amount", min_value=0.0)
    with c2:
        currency = st.selectbox("Currency", ["MYR","SAR","USD","EUR","TRY","GBP","AED","THB","SGD","IDR"])

    if amount > 0:
        st.markdown(
            f"<div class='big-number'>{to_myr(amount, currency):,.2f} MYR</div>",
            unsafe_allow_html=True
        )

    # =============================
    # SPLIT LOGIC
    # =============================
    if tracker == "Split Spending" and currency:
        st.divider()

        split_mode = st.radio(
            "Split Method",
            ["Split Equally", "Split By Amount", "Split By Percentage"],
            horizontal=True
        )

        st.caption(f"Splitting in {currency}")

        n = st.number_input("Number of people", 1, 20, 2)

        split_total = 0.0
        remaining = amount  # use selected currency amount


        for i in range(n):
            c1, c2 = st.columns([2, 1])
            with c1:
                name = st.text_input(f"Split to #{i+1}", key=f"name{i}")

            with c2:
                if split_mode == "Split Equally":
                    share = round(amount / n, 2) if amount else 0
                    st.number_input(
                        f"Amount #{i+1}",
                        value=share,
                        disabled=True,
                        key=f"eq{i}"
                    )
                else:
                    with c2:
                        if split_mode == "Split Equally":
                            share = round(amount / n, 2) if amount else 0
                            st.number_input(
                                f"Amount #{i+1}",
                                value=share,
                                disabled=True,
                                key=f"eq{i}"
                            )

                        elif split_mode == "Split By Amount":
                            share = st.number_input(
                                f"Amount #{i+1}",
                                min_value=0.0,
                                key=f"amt{i}"
                            )

                        else:  # Split By Percentage
                            pct_key = f"pct{i}"
                            amt_key = f"pct_amt{i}"

                            pct = st.number_input(
                                f"% #{i+1}",
                                min_value=0.0,
                                max_value=100.0,
                                key=pct_key
                            )

                            # Calculate share
                            share = round((pct / 100) * amount, 2)

                            # Store in session_state so UI updates
                            st.session_state[amt_key] = share

                            st.number_input(
                                f"Amount #{i+1}",
                                value=st.session_state[amt_key],
                                disabled=True,
                                key=amt_key
                            )

                split_total += share
                remaining -= share

        # =============================
        # SPLIT SUMMARY
        # =============================
        st.markdown("---")

        col1, col2 = st.columns(2)

        with col1:
            st.markdown('<div class="muted">Split total</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="big-number">{split_total:,.2f} {currency}</div>', unsafe_allow_html=True)

        if split_mode != "Split Equally":
            with col2:
                st.markdown('<div class="muted">Remaining balance</div>', unsafe_allow_html=True)
                st.markdown(f'<div class="big-number">{remaining:,.2f} {currency}</div>', unsafe_allow_html=True)

    can_save = uploaded is not None and subcategory.strip() != ""

    save_clicked = st.button("Save", disabled=not can_save)

    if save_clicked:
        meta = {
            "date": purchase_date.isoformat(),
            "merchant": merchant.replace(" ", "_"),
            "amount": f"{amount:.2f}",
            "currency": currency
        }

        upload_receipt(uploaded, tracker, subcategory, meta)

        st.success("Saved to Google Drive 💾")

        # -------- RESET INPUTS --------
        for k in [
            "uploaded_file",
            "purchase_date",
            "merchant",
            "amount",
            "currency"
        ]:
            if k in st.session_state:
                del st.session_state[k]

        # Rerun app with clean state
        st.rerun()


# =============================
# HISTORY TAB
# =============================
with tab_history:
    st.info("Spending history will appear here 💸")
