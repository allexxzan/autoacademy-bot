import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def get_gspread_client():
    creds_json_str = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json_str:
        raise Exception("❌ GOOGLE_CREDENTIALS не найдена в переменных окружения")

    creds_dict = json.loads(creds_json_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

    gc = gspread.authorize(creds)
    return gc

def get_worksheet():
    gc = get_gspread_client()
    spreadsheet_id = "1FkVk2-nkRlgo7lOCmAOPWo0s-YPZKL0p3zZ2JmbbkII"
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.worksheet("Лист1")
    return worksheet

def get_all_students():
    worksheet = get_worksheet()
    # Получаем все строки таблицы как список списков
    rows = worksheet.get_all_values()
    # Обычно первая строка - заголовки
    headers = rows[0]
    data_rows = rows[1:]
    # Можно собрать словари по строкам, чтобы удобнее было работать
    students = [dict(zip(headers, row)) for row in data_rows]
    return students

from datetime import datetime

def log_action(username, name, action):
    worksheet = get_worksheet()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    # Добавляем новую строку в конец таблицы
    worksheet.append_row([username, name, action, timestamp], value_input_option="USER_ENTERED")