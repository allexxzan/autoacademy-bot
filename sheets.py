import os
import json
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Авторизация и подключение к Google Sheets
def get_worksheet():
    creds_json_str = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json_str:
        raise Exception("❌ Переменная GOOGLE_CREDENTIALS не найдена.")

    creds_dict = json.loads(creds_json_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

    gc = gspread.authorize(creds)
    spreadsheet_id = "1FkVk2-nkRlgo7lOCmAOPWo0s-YPZKL0p3zZ2JmbbkII"
    sh = gc.open_by_key(spreadsheet_id)
    return sh.worksheet("Лист1")

# Логируем вступление и срок подписки
def log_subscription(username, full_name, activated_at, valid_until):
    worksheet = get_worksheet()
    worksheet.append_row(
        [
            username,
            full_name,
            activated_at.strftime("%Y-%m-%d %H:%M:%S"),
            valid_until.strftime("%Y-%m-%d %H:%M:%S")
        ],
        value_input_option="USER_ENTERED"
    )
