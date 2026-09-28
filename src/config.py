"""
Central project configuration.
Environment variables and constants.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()

# ClickUp
CLICKUP_TOKEN = os.getenv("CLICKUP_TOKEN", "")
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "9013290037")
CLICKUP_TIMEOUT_S = float(os.getenv("CLICKUP_TIMEOUT_S", "30"))
CLICKUP_PAGE_LIMIT = int(os.getenv("CLICKUP_PAGE_LIMIT", "100"))
CLICKUP_PAGE_PAUSE_S = float(os.getenv("CLICKUP_PAGE_PAUSE_S", "0.3"))
CLICKUP_MAX_RETRIES = max(1, int(os.getenv("CLICKUP_MAX_RETRIES", "4")))

CLICKUP_LIST_IDS = [
    "901322296001",
    "901321549851",
]

# PowerRev
POWERREV_BASE_URL = os.getenv("POWERREV_BASE_URL", "https://api.powerrev.com.br:3400")
POWERREV_AUTH_URL = os.getenv("POWERREV_AUTH_URL", POWERREV_BASE_URL)
POWERREV_ACCOUNT_ID = os.getenv("POWERREV_ACCOUNT_ID", "")
POWERREV_API_KEY = os.getenv("POWERREV_API_KEY", "")
POWERREV_TOKEN = os.getenv("POWERREV_TOKEN", "")
# Backward-compatible with faturamento_sync env names
POWERREV_TIMEOUT_S = int(os.getenv("POWERREV_TIMEOUT_S", os.getenv("POWERREV_TIMEOUT", "30")))
POWERREV_DELAY_S = float(os.getenv("POWERREV_DELAY_S", os.getenv("POWERREV_DELAY", "1.0")))
POWERREV_MAX_RETRIES = int(os.getenv("POWERREV_MAX_RETRIES", "3"))
POWERREV_PAGE_LIMIT = int(os.getenv("POWERREV_PAGE_LIMIT", "100"))
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Sao_Paulo")

# Google Sheets
SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "15SRfC-zfGgb1kxOb3V-yHisN-4NkmOoEoheoXEXmfwQ",
)
SHEET_TAB_NAME = "Rateio"
RATEIO_SHEET_TARGETS = {
    "COPEL": {
        "spreadsheet_id": os.getenv(
            "RATEIO_COPEL_SPREADSHEET_ID",
            "11TW3jDv8bZYJxPA2sOx73NUdY8QiMbFsgnrfeEopOww",
        ).strip(),
        "tab_name": os.getenv("RATEIO_COPEL_SHEET_TAB", "Sion - Matriz").strip() or "Sion - Matriz",
    },
    "AmE": {
        "spreadsheet_id": os.getenv(
            "RATEIO_AME_SPREADSHEET_ID",
            "1VK322_aF3N6_JXpvNX2dFEKOn4QKVB9Rz3lZRj7FpOk",
        ).strip(),
        "tab_name": os.getenv("RATEIO_AME_SHEET_TAB", "Sion - Matriz").strip() or "Sion - Matriz",
    },
    "Energisa MS": {
        "spreadsheet_id": os.getenv(
            "RATEIO_ENERGISA_MS_SPREADSHEET_ID",
            "1usAlgI5WiwLT1aOIy7-yy68EIJ3vFYvcnFwaf22ryyg",
        ).strip(),
        "tab_name": os.getenv("RATEIO_ENERGISA_MS_SHEET_TAB", "Sion - Matriz").strip() or "Sion - Matriz",
    },
    "CELESC": {
        "spreadsheet_id": os.getenv(
            "RATEIO_CELESC_SPREADSHEET_ID",
            "1f3ljN863TAg1joLOnyhoG9Fh125LdIherxpArrxxTsk",
        ).strip(),
        "tab_name": os.getenv("RATEIO_CELESC_SHEET_TAB", "Sion - Matriz").strip() or "Sion - Matriz",
    },
}
RATEIO_FAVORECIDO_TABS = {
    "Sion - Matriz": "Sion - Matriz",
    "Sion - Helexia PR": "Helexia PR",
    "Sion - Helexia MS": "Helexia MS",
}
PROJECTION_SPREADSHEET_ID = os.getenv(
    "PROJECTION_SPREADSHEET_ID",
    "1flNyO53loY__fwO-TDqffAFAOdw7KuyjVM9cYGZmRSI",
)
PROJECTION_SHEET_TAB = os.getenv("PROJECTION_SHEET_TAB", "Projeção de Consumo")
PROJECTION_GENERATION_SHEET_TAB = os.getenv("PROJECTION_GENERATION_SHEET_TAB", "Projeção de Geração")
RATEIO_GENERATION_SHEET_TAB = os.getenv("RATEIO_GENERATION_SHEET_TAB", "Geração Total")
PROJECTION_ROUND_DECIMALS = int(os.getenv("PROJECTION_ROUND_DECIMALS", "0"))

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")


def get_google_credentials_info() -> dict | None:
    if GOOGLE_CREDENTIALS_JSON:
        return json.loads(GOOGLE_CREDENTIALS_JSON)
    if os.path.exists(GOOGLE_CREDENTIALS_FILE):
        with open(GOOGLE_CREDENTIALS_FILE) as f:
            return json.load(f)
    return None


def resolve_rateio_sheet_target(
    distributor_name: str,
    favorecido: str | None = None,
) -> tuple[str, str]:
    target = RATEIO_SHEET_TARGETS.get(distributor_name) or {}
    spreadsheet_id = str(target.get("spreadsheet_id") or SPREADSHEET_ID).strip()
    if distributor_name == "COPEL" and favorecido == "Sion - Helexia PR":
        target_tab_name = RATEIO_FAVORECIDO_TABS["Sion - Matriz"]
    elif favorecido is not None:
        target_tab_name = str(RATEIO_FAVORECIDO_TABS.get(favorecido) or "").strip()
        if not target_tab_name:
            raise ValueError(f"Favorecido sem aba configurada: {favorecido!r}")
    else:
        target_tab_name = (
            str(target.get("tab_name") or distributor_name).strip()
            or distributor_name
        )
    return spreadsheet_id, target_tab_name


# Sync timings
FULL_SYNC_INTERVAL_S = max(86400, int(os.getenv("FULL_SYNC_INTERVAL_S", "86400")))  # 24 h minimum
DELTA_SYNC_INTERVAL_S = int(os.getenv("DELTA_SYNC_INTERVAL_S", "600"))  # 10 min

# Sheets write tuning
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "300"))
CHUNK_PAUSE_S = float(os.getenv("CHUNK_PAUSE_S", "2"))
