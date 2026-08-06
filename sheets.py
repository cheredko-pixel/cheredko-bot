import os
from datetime import date

import requests

from phone import normalize_phone

WEBHOOK_URL = os.environ["SHEETS_WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["SHEETS_WEBHOOK_TOKEN"]


def fetch_upcoming_surgeries(today: date) -> list[dict]:
    response = requests.get(WEBHOOK_URL, params={"token": WEBHOOK_TOKEN}, timeout=15)
    response.raise_for_status()
    rows = response.json()

    if isinstance(rows, dict) and rows.get("error"):
        raise RuntimeError(f"Sheets webhook error: {rows['error']}")

    surgeries = []
    for row in rows:
        surgery_date = date.fromisoformat(row["surgery_date"])
        if surgery_date < today:
            continue

        phone = normalize_phone(row["phone"])
        full_name = row["full_name"].strip()
        if not phone or not full_name:
            continue

        surgeries.append({"full_name": full_name, "phone": phone, "surgery_date": surgery_date})

    return surgeries
