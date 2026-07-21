"""
Rateio Sync - loop principal.

Full sync: busca todas as tasks do ClickUp e reescreve as abas de destino.
Delta sync: busca tasks modificadas e atualiza in-place.
"""
import os
import re
import signal
import sys
import time
import logging
import unicodedata
from datetime import date, datetime, timedelta
from typing import Callable, TypeVar
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

# Allows running the script directly from `src/` with `python poll.py`.
if __package__ is None or __package__ == "":
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.config import (
    DELTA_SYNC_INTERVAL_S,
    PROJECTION_SPREADSHEET_ID,
    PROJECTION_SHEET_TAB,
    PROJECTION_GENERATION_SHEET_TAB,
    RATEIO_GENERATION_SHEET_TAB,
    RATEIO_FAVORECIDO_TABS,
    PROJECTION_ROUND_DECIMALS,
    APP_TIMEZONE,
    resolve_rateio_sheet_target,
)
from src.clients.clickup_client import fetch_all_tasks, fetch_tasks, reset_session as reset_clickup_session
from src.clients.powerrev_client import (
    build_invoice_index_for_ucs,
    format_reference_month,
    get_current_reference_month,
    reset_session as reset_powerrev_session,
    reset_caches as reset_powerrev_caches,
)
from src.clients.sheets_manager import (
    CHUNK_SIZE,
    DATA_START_ROW,
    get_worksheet,
    ensure_headers,
    read_all_rows,
    sync_rows_in_place,
    update_rows_in_place,
    _get_spreadsheet_meta,
    _values_get,
    _values_batch_get,
    _values_batch_update,
    _values_update,
    reset_client as reset_sheets_client,
)
from src.core.row_builder import (
    _resolve_dropdown_value,
    slim_task,
    build_row,
    extract_task_uc,
    extract_task_uc_old,
    extract_task_uc_match_candidates,
    extract_task_status,
    extract_task_plan,
    extract_task_target_tab,
    extract_task_favorecido,
    extract_task_invoice_issue_day,
    extract_task_uc_aneel,
    extract_task_helexia_pr_matriz_rateio_months,
    is_task_copel_matriz_august_2026_checked,
    normalize_uc,
)
from src.core.field_map import FIELD_MAP, COLUMN_ORDER, get_headers, TARGET_SHEET_TABS
from src.utils.stats import stats, log_memory, log_sync_stats, force_free_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rateio_sync")

# Shutdown graceful via SIGTERM (Railway deploy)
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    del signum, frame
    global _shutdown_requested
    logger.info("Sinal SIGTERM recebido - shutdown graceful solicitado.")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_sigterm)

_MAX_CONSECUTIVE_ERRORS = 5
_ERROR_BACKOFF_BASE = 30
_ERROR_BACKOFF_MAX = 300
_SHEETS_STEP_MAX_RETRIES = 5
_SHEETS_STEP_BACKOFF_BASE = 15
_FULL_SYNC_DAILY_HOUR = 3
_TZ_FALLBACK_LOGGED = False

_known_task_ids: set[str] = set()
_LOW_PRIORITY_DUPLICATE_STATUSES = {
    "Planejamento - Black",
    "Encerrado - Troca de Plano",
    "Encerrado - Financeiro",
    "A Retirar da Usina - Black",
    "Aguardando Cadastro",
    "Aguardando Cadastro - Usina",
    "Aguardando Cadastro - Em Contingência",
    "Aguardando Cadastro - Em Contingencia",
    "Aguardando Cadastro - em Contingencia",
    "Demitido",
    "Excluido",
    "Retirado da Usina - Inadimpl\u00eancia",
    "Retirado da Usina - Inadimplencia",
    "Demiss\u00e3o - Onboarding",
    "Demissao - Onboarding",
}
_DISTRIBUTOR_TARGET_ALIASES = {
    "COPEL": ("COPEL",),
    "AmE": ("AME", "AM E"),
    "Energisa MS": ("ENERGISA MS", "ENERGISA"),
    "CELESC": ("CELESC",),
}
_RATEIO_HISTORY_TAB = os.getenv("RATEIO_HISTORY_TAB", "Rateio").strip() or "Rateio"
_RATEIO_COPEL_HISTORY_TAB = os.getenv(
    "RATEIO_COPEL_HISTORY_TAB",
    _RATEIO_HISTORY_TAB,
).strip() or _RATEIO_HISTORY_TAB
_FORMULARIO_TABS_BY_DISTRIBUTOR = {
    "COPEL": "Formul\u00e1rio COPEL",
    "AmE": "Formul\u00e1rio AmE",
    "CELESC": "Formul\u00e1rio CELESC",
    "Energisa MS": "Formul\u00e1rio Energisa MS",
}
_FORMULARIO_COPEL_TAB = _FORMULARIO_TABS_BY_DISTRIBUTOR["COPEL"]
_FORMULARIO_COPEL_WRITE_COL_COUNT = 8  # A:H
_FORMULARIO_COPEL_PROJECT_LIST_IDS = (
    "901304117744",
    "901327022900",
)
_FORMULARIO_COPEL_CPF_CNPJ_CF_ID = "6bcbff0f-3228-44e1-b7d7-14efa915fc31"
_FORMULARIO_COPEL_COL_H_CF_ID = "cd8687a7-0393-45b9-8292-f9b878b31512"
_FORMULARIO_RAZAO_SOCIAL_CF_ID = "dfb0de9b-121a-4bf6-977f-dfb5eec523cb"
_FORMULARIO_UC_ANEEL_HEADER = "UC Aneel"
_FORMULARIO_ADDRESS_CF_IDS = {
    "rua": "26d33756-428f-4f28-a1b4-485cb875429e",
    "numero": "6348124b-60d4-40d5-80fe-86019c173d4e",
    "complemento": "6257313d-18da-4ebb-aa03-d07846b5da8d",
    "bairro": "a9ce9f60-cf2b-4eb8-975c-2fe8c7c27591",
    "cidade": "81ba9425-6386-40ea-8a92-e270e8284bd7",
    "estado": "06361a45-790b-4fbe-9e66-427cfb28e7ec",
    "cep": "f24d971c-4037-41a7-a1f1-f92c84569993",
}
_FORMULARIO_ADDRESS_HEADER = "Endere\u00e7o"
_FORMULARIO_RATEIO_HEADER_ALIASES = {
    "usina": {"USINA"},
    "razao_social": {"RAZAO SOCIAL"},
    "uc": {"UC"},
    "nova_uc": {"NOVA UC", "UC ANEEL"},
    "percentual": {"%", "PERCENTUAL", "PORCENTAGEM"},
    "alteracao": {
        "ALTERACAO PARA MES",
        "ALTERACAO PARA O MES",
        "ALTERACAO RATEIO PARA O MES",
    },
}
_RATEIO_WRITE_COL_COUNT = 16  # A:P
_INVOICE_ISSUE_DAY_OUTPUT_COL_INDEX = 13  # N
_INVOICE_ISSUE_DAY_HEADER = "Dia de emiss\u00e3o da fatura da distribuidora"
_FAVORECIDO_OUTPUT_COL_INDEX = 14  # O
_FAVORECIDO_HEADER = "Favorecido"
_UC_ANEEL_OUTPUT_COL_INDEX = 15  # P
_UC_ANEEL_HEADER = "UC Aneel"
_UC_ANEEL_MISSING_VALUE = "Sem UC Aneel"
_RATEIO_CONFIGURATION_TAB = "Configura\u00e7\u00e3o"
_RATEIO_CONFIGURATION_CLOSED_STATUS = "fechado"
_SHEETS_SERIAL_BASE = date(1899, 12, 30)
_RATEIO_FAVORECIDOS_BY_DISTRIBUTOR = {
    "COPEL": ("Sion - Matriz", "Sion - Helexia PR"),
    "Energisa MS": ("Sion - Matriz", "Sion - Helexia MS"),
    "CELESC": ("Sion - Matriz",),
    "AmE": ("Sion - Matriz",),
}
_RATEIO_TARGETS = [
    (distributor, favorecido)
    for distributor in TARGET_SHEET_TABS
    for favorecido in _RATEIO_FAVORECIDOS_BY_DISTRIBUTOR.get(distributor, ())
]
_GENERATION_TOTAL_COLUMNS = {
    "Sion - Matriz": (6, 7),
    "Sion - Helexia PR": (8, 9),
    "Sion - Helexia MS": (10, 11),
}
_GENERATION_TOTAL_MONTH_COL_INDEX = 12
_RATEIO_CONFIGURATION_MONTH_HEADER = "Alteracao Rateio para o mes"
_RATEIO_CONFIGURATION_STATUS_HEADER = "Status"
_RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER = "Coeficiente base especial"
_RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER = "Sobra para Favorecido"
_RATEIO_CONFIGURATION_REMAINDER_UC_HEADER = "Sobra para UC"
_RATEIO_CONFIGURATION_CONTINGENCY_PREFIX = "Coeficiente Contingencia"
_RATEIO_CONFIGURATION_LEGACY_HEADERS = [
    _RATEIO_CONFIGURATION_MONTH_HEADER,
    "Coeficiente Sion - Matriz",
    "Coeficiente Sion - Helexia PR",
    "Coeficiente Sion - Helexia MS",
    _RATEIO_CONFIGURATION_STATUS_HEADER,
    _RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER,
    _RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER,
    _RATEIO_CONFIGURATION_REMAINDER_UC_HEADER,
]
_RATEIO_CONFIGURATION_FAVORECIDOS = [
    "Sion - Matriz",
    "Sion - Helexia PR",
    "Sion - Helexia MS",
]
_MAX_SPECIAL_BASE_COEFFICIENT = Decimal("100")
_SPECIAL_ALLOCATION_DEFAULTS = {
    ("COPEL", "Sion - Matriz"): {
        "remainder_favorecido": "Sion - Helexia PR",
        "remainder_uc": "",
    },
    ("Energisa MS", "Sion - Helexia MS"): {
        "remainder_favorecido": "",
        "remainder_uc": "10/3713101-8",
    },
}
_CONFIGURABLE_CONTINGENCY_COEFFICIENTS = {
    ("Energisa MS", "Sion - Helexia MS"),
}
_FIXED_CONTINGENCY_COEFFICIENTS_BY_MONTH = {
    ("COPEL", "Sion - Matriz"): {
        "01-08-2026": Decimal("0.00000000"),
    },
}
_HELEXIA_PR_MATRIZ_SOURCE_DISTRIBUTOR = "COPEL"
_HELEXIA_PR_MATRIZ_SOURCE_FAVORECIDO = "Sion - Helexia PR"
_HELEXIA_PR_MATRIZ_TARGET_FAVORECIDO = "Sion - Matriz"
_COPEL_MATRIZ_CHECKBOX_RATEIO_MONTH = "01-08-2026"
_DEFAULT_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD = 10
_AME_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD = 7

_T = TypeVar("_T")

_EXCLUDED_STATUS_FROM_PROJECTION_RAW = {
    "Encerrado - Financeiro",
    "Baixo Consumo",
    "A Retirar da Usina - DemissÃƒÆ’Ã‚Â£o",
    "A Retirar da Usina - InadimplÃƒÆ’Ã‚Âªncia",
    "Retirado da Usina - CR",
    "Retirado da Usina - InadimplÃƒÆ’Ã‚Âªncia",
    "Retirado da Usina - DemissÃƒÆ’Ã‚Â£o",
    "A Retirar da Usina - CR",
    "A Retirar da Usina - Black",
    "A Encerrar - Financeiro",
}

_EXCLUDED_STATUS_FROM_RATEIO_RAW = {
    "Planejamento - Black",
    "Aguardando Cadastro",
    "Aguardando Cadastro - Usina",
    "Aguardando Cadastro - Em Contingência",
    "Aguardando Cadastro - Em Contingencia",
    "Aguardando Cadastro - em Contingencia",
    "Demitido",
    "Excluido",
    "Encerrado - Financeiro",
    "Encerrado - Troca de Plano",
    "A Retirar da Usina - Black",
    "A Retirar da Usina - Inadimpl\u00eancia",
    "A Retirar da Usina - Inadimplencia",
    "Retirado da Usina - Black",
    "Retirado da Usina - Inadimpl\u00eancia",
    "Retirado da Usina - Inadimplencia",
    "A Encerrar - Financeiro",
    "Demiss\u00e3o - Onboarding",
    "Demissao - Onboarding",
    "A Retirar da Usina - Demiss\u00e3o",
    "A Retirar da Usina - Demissao",
    "Retirado da Usina - Demiss\u00e3o",
    "Retirado da Usina - Demissao",
}

_NEW_COOPERADO_DELAYED_STATUS_RAW = {
    "Novo Cooperado",
    "Novo Cooperado - Em Contingência",
    "Novo Cooperado - Em Contingencia",
}
_NEW_COOPERADO_RATEIO_DELAY_MONTHS = 2
_NEW_COOPERADO_RATEIO_DELAY_MONTHS_BY_DISTRIBUTOR = {
    "COPEL": 2,
    "AmE": 1,
    "CELESC": 1,
    "Energisa MS": 1,
}


def _add_months(yyyymm: int, offset: int) -> int:
    year = yyyymm // 100
    month = yyyymm % 100
    idx = year * 12 + (month - 1) + offset
    new_year = idx // 12
    new_month = (idx % 12) + 1
    return new_year * 100 + new_month


def _reference_month_window(current_month: int, past: int = 3, future: int = 3) -> list[int]:
    return [_add_months(current_month, i) for i in range(-past, future + 1)]


def _is_rateio_month_frozen(
    rateio_month,
    frozen_rateio_months: set[str] | None = None,
) -> bool:
    month_norm = _normalize_month_reference_any(rateio_month)
    return bool(month_norm and month_norm in (frozen_rateio_months or set()))


def _is_reference_month_frozen(
    month_ref,
    current_month: int,
    frozen_rateio_months: set[str] | None = None,
) -> bool:
    """Converte o m\u00eas de refer\u00eancia (G) no m\u00eas de altera\u00e7\u00e3o (A)."""
    del current_month
    month_int = _month_ref_to_int(month_ref)
    if month_int is None:
        return False
    rateio_month = format_reference_month(_add_months(month_int, 1))
    return _is_rateio_month_frozen(rateio_month, frozen_rateio_months)


def _previous_month_int(month: int) -> int:
    return _add_months(month, -1)


def _collect_target_ucs(tasks: list[dict]) -> set[str]:
    target_ucs: set[str] = set()
    for task in tasks:
        uc_candidates = extract_task_uc_match_candidates(task)
        if not uc_candidates:
            continue
        if _is_plan_excluded_from_rateio(extract_task_plan(task)):
            continue
        if extract_task_target_tab(task) not in TARGET_SHEET_TABS:
            continue
        if not _resolve_supported_favorecido(extract_task_favorecido(task)):
            continue
        target_ucs.update(uc_candidates)

    return target_ucs


def _task_priority_key(task: dict) -> tuple[int, int]:
    status = extract_task_status(task)
    status_priority = 0 if _is_low_priority_duplicate_status(status) else 1
    try:
        updated = int(str(task.get("date_updated") or "0"))
    except (TypeError, ValueError):
        updated = 0
    return status_priority, updated


def _prioritize_tasks_by_uc(tasks: list[dict]) -> list[dict]:
    selected_by_uc: dict[str, dict] = {}
    without_uc: list[dict] = []

    for task in tasks:
        uc = extract_task_uc(task)
        if not uc:
            without_uc.append(task)
            continue

        current = selected_by_uc.get(uc)
        if current is None:
            selected_by_uc[uc] = task
            continue

        if _task_priority_key(task) > _task_priority_key(current):
            selected_by_uc[uc] = task

    prioritized = list(selected_by_uc.values()) + without_uc
    discarded = len(tasks) - len(prioritized)
    if discarded > 0:
        logger.info(
            "ClickUp: %d tasks duplicadas por UC descartadas por prioridade de status.",
            discarded,
        )
    return prioritized


def _normalize_month_reference(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        year = digits[:4]
        month = digits[4:6]
        return f"01-{month}-{year}"

    if len(digits) == 8:
        # yyyymmdd
        if digits[:4].isdigit() and 1900 <= int(digits[:4]) <= 2100:
            year = digits[:4]
            month = digits[4:6]
            return f"01-{month}-{year}"
        # ddmmyyyy
        year = digits[4:8]
        month = digits[2:4]
        return f"01-{month}-{year}"

    return raw.replace("/", "-")


def _round_projection_value(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    normalized = raw.replace(" ", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")

    try:
        num = Decimal(normalized)
    except InvalidOperation:
        return raw

    decimals = max(0, PROJECTION_ROUND_DECIMALS)
    quant = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    rounded = num.quantize(quant, rounding=ROUND_HALF_UP)

    if decimals == 0:
        return str(int(rounded))
    return format(rounded, f".{decimals}f")


def _to_decimal(value) -> Decimal | None:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None

    normalized = raw.replace(" ", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _to_sheet_number_or_blank(value):
    parsed = _to_decimal(value)
    if parsed is None:
        return ""
    return _decimal_to_sheet_number(parsed)


def _format_decimal_plain(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _contains_contingencia(status_value: str) -> bool:
    text = unicodedata.normalize("NFKD", str(status_value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    ascii_text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return "conting" in ascii_text


def _normalize_status_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_EXCLUDED_STATUS_FROM_PROJECTION_KEYS = {
    _normalize_status_key(status)
    for status in _EXCLUDED_STATUS_FROM_PROJECTION_RAW
}

_EXCLUDED_STATUS_FROM_RATEIO_KEYS = {
    _normalize_status_key(status)
    for status in _EXCLUDED_STATUS_FROM_RATEIO_RAW
}
_LOW_PRIORITY_DUPLICATE_STATUS_KEYS = {
    _normalize_status_key(status)
    for status in _LOW_PRIORITY_DUPLICATE_STATUSES
}

_NEW_COOPERADO_DELAYED_STATUS_KEYS = {
    _normalize_status_key(status)
    for status in _NEW_COOPERADO_DELAYED_STATUS_RAW
}


def _is_status_excluded_from_projection(status_value: str) -> bool:
    return _normalize_status_key(status_value) in _EXCLUDED_STATUS_FROM_PROJECTION_KEYS


def _is_status_excluded_from_rateio(status_value: str) -> bool:
    normalized = _normalize_status_key(status_value)
    return (
        not normalized
        or normalized in _EXCLUDED_STATUS_FROM_RATEIO_KEYS
        or normalized.startswith("aguardando cadastro ")
    )


def _is_low_priority_duplicate_status(status_value: str) -> bool:
    normalized = _normalize_status_key(status_value)
    return (
        _is_status_excluded_from_rateio(status_value)
        or normalized in _LOW_PRIORITY_DUPLICATE_STATUS_KEYS
        or normalized.startswith("aguardando cadastro ")
    )


def _is_new_cooperado_delayed_status(status_value: str) -> bool:
    return _normalize_status_key(status_value) in _NEW_COOPERADO_DELAYED_STATUS_KEYS


def _new_cooperado_rateio_delay_months(distributor: str) -> int:
    return _NEW_COOPERADO_RATEIO_DELAY_MONTHS_BY_DISTRIBUTOR.get(
        str(distributor or "").strip(),
        _NEW_COOPERADO_RATEIO_DELAY_MONTHS,
    )


def _is_rateio_month_blocked_by_new_cooperado_delay(
    status_value: str,
    rateio_month: str,
    current_month: int,
    distributor: str,
) -> bool:
    if not _is_new_cooperado_delayed_status(status_value):
        return False

    rateio_month_int = _month_ref_to_int(rateio_month)
    if rateio_month_int is None:
        return True

    first_allowed_month = _add_months(
        current_month,
        _new_cooperado_rateio_delay_months(distributor),
    )
    return rateio_month_int < first_allowed_month


def _is_plan_excluded_from_rateio(plan_value: str) -> bool:
    return _normalize_status_key(plan_value) == "sem faturamento"


def _resolve_supported_favorecido(value: str) -> str | None:
    normalized = _normalize_status_key(value)
    if not normalized:
        return None
    for favorecido in RATEIO_FAVORECIDO_TABS:
        if normalized == _normalize_status_key(favorecido):
            return favorecido
    return None


def _rateio_favorecidos_for_distributor(distributor: str) -> tuple[str, ...]:
    return _RATEIO_FAVORECIDOS_BY_DISTRIBUTOR.get(str(distributor or "").strip(), ())


def _is_rateio_target_enabled(distributor: str, favorecido: str) -> bool:
    return favorecido in _rateio_favorecidos_for_distributor(distributor)


def _distributor_for_rateio_spreadsheet(spreadsheet_id: str) -> str:
    target_id = str(spreadsheet_id or "").strip()
    if not target_id:
        return ""
    for distributor in TARGET_SHEET_TABS:
        configured_id, _tab = resolve_rateio_sheet_target(distributor)
        if configured_id == target_id:
            return distributor
    return ""


def _special_allocation_defaults(
    distributor: str,
    favorecido: str,
) -> dict[str, str] | None:
    return _SPECIAL_ALLOCATION_DEFAULTS.get((distributor, favorecido))


def _special_allocation_defaults_for_distributor(distributor: str) -> dict[str, str] | None:
    for (configured_distributor, _favorecido), defaults in _SPECIAL_ALLOCATION_DEFAULTS.items():
        if configured_distributor == distributor:
            return defaults
    return None


def _configuration_coefficient_header(favorecido: str) -> str:
    return f"Coeficiente {favorecido}"


def _configuration_contingency_header(favorecido: str) -> str:
    return f"{_RATEIO_CONFIGURATION_CONTINGENCY_PREFIX} {favorecido}"


def _configuration_headers_for_distributor(distributor: str) -> list[str]:
    distributor_name = str(distributor or "").strip()
    enabled_favorecidos = _rateio_favorecidos_for_distributor(distributor_name)
    headers = [_RATEIO_CONFIGURATION_MONTH_HEADER]

    for favorecido in _RATEIO_CONFIGURATION_FAVORECIDOS:
        if favorecido in enabled_favorecidos:
            headers.append(_configuration_coefficient_header(favorecido))

    headers.append(_RATEIO_CONFIGURATION_STATUS_HEADER)

    special_defaults = _special_allocation_defaults_for_distributor(distributor_name)
    if special_defaults:
        headers.append(_RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER)
        if str(special_defaults.get("remainder_favorecido") or "").strip():
            headers.append(_RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER)
        if str(special_defaults.get("remainder_uc") or "").strip():
            headers.append(_RATEIO_CONFIGURATION_REMAINDER_UC_HEADER)

    for configured_distributor, favorecido in sorted(_CONFIGURABLE_CONTINGENCY_COEFFICIENTS):
        if configured_distributor == distributor_name and favorecido in enabled_favorecidos:
            headers.append(_configuration_contingency_header(favorecido))

    return headers


def _configuration_header_indexes(headers: list) -> dict[str, int]:
    return {
        _normalize_status_key(header): index
        for index, header in enumerate(headers or [])
        if str(header or "").strip()
    }


def _configuration_index(
    header_indexes: dict[str, int],
    header: str,
    *,
    fallback: int | None = None,
) -> int | None:
    index = header_indexes.get(_normalize_status_key(header))
    if index is not None:
        return index
    return fallback


def _row_value_by_index(row: list, index: int | None) -> str:
    if index is None or index < 0 or len(row) <= index:
        return ""
    return str(row[index] if row[index] is not None else "").strip()


def _row_at(rows: list[list], index: int) -> list:
    if index < 0 or index >= len(rows):
        return []
    row = rows[index]
    return row if isinstance(row, list) else []


def _config_numeric_cell_text(
    raw_row: list,
    formatted_row: list,
    formula_row: list,
    index: int | None,
) -> str:
    if index is None:
        return ""
    if _is_formula_cell(_row_value_by_index(formula_row, index)):
        return ""

    raw_text = _row_value_by_index(raw_row, index)
    formatted_text = _row_value_by_index(formatted_row, index)
    raw_decimal = _to_decimal(raw_text)
    formatted_decimal = _to_decimal(formatted_text)

    if (
        formatted_text
        and formatted_decimal is not None
        and Decimal("0") <= formatted_decimal <= _MAX_SPECIAL_BASE_COEFFICIENT
        and (
            raw_decimal is None
            or raw_decimal < 0
            or raw_decimal > _MAX_SPECIAL_BASE_COEFFICIENT
        )
    ):
        return formatted_text

    if (
        raw_text
        and raw_decimal is not None
        and Decimal("0") <= raw_decimal <= _MAX_SPECIAL_BASE_COEFFICIENT
    ):
        return raw_text

    return formatted_text if formatted_text else raw_text


def _column_letter(index: int) -> str:
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _default_contingency_coefficient(distributor: str, favorecido: str) -> Decimal:
    distributor_name = str(distributor or "").strip()
    favorecido_name = str(favorecido or "").strip()
    if distributor_name in {"AmE", "CELESC"} or (
        distributor_name == "Energisa MS" and favorecido_name == "Sion - Matriz"
    ):
        return Decimal("1.00000000")
    return Decimal("0.90000000")


def _resolve_task_rateio_target(task: dict) -> tuple[str, str] | None:
    if _is_plan_excluded_from_rateio(extract_task_plan(task)):
        return None
    distributor = extract_task_target_tab(task)
    favorecido = _resolve_supported_favorecido(extract_task_favorecido(task))
    if distributor not in TARGET_SHEET_TABS or favorecido is None:
        return None
    if not _is_rateio_target_enabled(distributor, favorecido):
        return None
    return distributor, favorecido


def _parse_rateio_months_field(value: str) -> set[str]:
    months: set[str] = set()
    for part in str(value or "").split(","):
        month_ref = _normalize_month_reference(part.strip())
        if month_ref:
            months.add(month_ref)
    return months


def _resolve_effective_favorecido_for_rateio_month(
    *,
    task: dict,
    distributor: str,
    favorecido: str,
    rateio_month: str,
) -> str:
    """Resolve routing-only Favorecido exceptions for a specific rateio month."""
    if _is_copel_matriz_checkbox_override(
        task=task,
        distributor=distributor,
        rateio_month=rateio_month,
    ):
        return _HELEXIA_PR_MATRIZ_TARGET_FAVORECIDO

    normalized_rateio_month = _normalize_month_reference_any(rateio_month)
    if (
        distributor == _HELEXIA_PR_MATRIZ_SOURCE_DISTRIBUTOR
        and favorecido == _HELEXIA_PR_MATRIZ_SOURCE_FAVORECIDO
    ):
        exception_months = _parse_rateio_months_field(
            extract_task_helexia_pr_matriz_rateio_months(task)
        )
        if normalized_rateio_month in exception_months:
            return _HELEXIA_PR_MATRIZ_TARGET_FAVORECIDO
    return favorecido


def _is_copel_matriz_checkbox_override(
    *,
    task: dict,
    distributor: str,
    rateio_month: str,
) -> bool:
    return (
        distributor == _HELEXIA_PR_MATRIZ_SOURCE_DISTRIBUTOR
        and _normalize_month_reference_any(rateio_month) == _COPEL_MATRIZ_CHECKBOX_RATEIO_MONTH
        and is_task_copel_matriz_august_2026_checked(task)
    )


def _favorecido_output_for_rateio_month(
    *,
    task: dict,
    distributor: str,
    original_favorecido: str,
    effective_favorecido: str,
    rateio_month: str,
) -> str:
    if _is_copel_matriz_checkbox_override(
        task=task,
        distributor=distributor,
        rateio_month=rateio_month,
    ):
        return effective_favorecido
    return original_favorecido


def _clear_projection_and_balance_fields(row_data: list[str]) -> list[str]:
    row = list(row_data)
    try:
        idx_h = COLUMN_ORDER.index("projecao_consumo")
        idx_i = COLUMN_ORDER.index("saldo_23_24")
    except ValueError:
        return row
    if idx_h < len(row):
        row[idx_h] = ""
    if idx_i < len(row):
        row[idx_i] = ""
    return row


def _coerce_h_i_as_numbers(row_data: list) -> list:
    row = list(row_data)
    try:
        idx_h = COLUMN_ORDER.index("projecao_consumo")
        idx_i = COLUMN_ORDER.index("saldo_23_24")
    except ValueError:
        return row

    if idx_h < len(row):
        row[idx_h] = _to_sheet_number_or_blank(row[idx_h])
    if idx_i < len(row):
        row[idx_i] = _to_sheet_number_or_blank(row[idx_i])
    return row


def _powerrev_invoice_issue_day(value) -> int | None:
    day = _invoice_issue_day(value)
    if day is not None:
        return day

    raw = str(value or "").strip()
    if not raw:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt).day
        except ValueError:
            pass

    match = re.match(r"^\s*\d{4}[-/]\d{1,2}[-/](\d{1,2})", raw)
    if match:
        parsed = int(match.group(1))
        return parsed if 1 <= parsed <= 31 else None

    return None


def _set_invoice_issue_day_output_column(
    row_data: list,
    task: dict,
    powerrev_issue_day=None,
) -> list:
    row = list(row_data)
    if len(row) < _RATEIO_WRITE_COL_COUNT:
        row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
    issue_day = _powerrev_invoice_issue_day(powerrev_issue_day)
    if issue_day is None:
        issue_day = _invoice_issue_day(extract_task_invoice_issue_day(task))
    row[_INVOICE_ISSUE_DAY_OUTPUT_COL_INDEX] = _to_sheet_number_or_blank(
        issue_day if issue_day is not None else ""
    )
    return row


def _set_favorecido_output_column(row_data: list, favorecido: str) -> list:
    row = list(row_data)
    if len(row) < _RATEIO_WRITE_COL_COUNT:
        row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
    row[_FAVORECIDO_OUTPUT_COL_INDEX] = favorecido
    return row


def _set_uc_aneel_output_column(row_data: list, task: dict) -> list:
    row = list(row_data)
    if len(row) < _RATEIO_WRITE_COL_COUNT:
        row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
    uc_aneel = extract_task_uc_aneel(task).strip()
    row[_UC_ANEEL_OUTPUT_COL_INDEX] = uc_aneel or _UC_ANEEL_MISSING_VALUE
    return row


def _ensure_invoice_issue_day_header(ws, *, spreadsheet_id: str) -> None:
    _values_update(
        ws,
        "N1:P1",
        [[_INVOICE_ISSUE_DAY_HEADER, _FAVORECIDO_HEADER, _UC_ANEEL_HEADER]],
        spreadsheet_id=spreadsheet_id,
    )
    stats.sheets_write_requests += 1


def _set_alteracao_rateio_mes_from_month_ref(row_data: list[str]) -> list[str]:
    row = list(row_data)
    try:
        idx_a = COLUMN_ORDER.index("alteracao_rateio_mes")
        idx_g = COLUMN_ORDER.index("mes_referencia")
    except ValueError:
        return row

    month_ref = _normalize_month_reference(row[idx_g] if idx_g < len(row) else "")
    if not month_ref:
        if idx_a < len(row):
            row[idx_a] = ""
        return row

    parts = month_ref.split("-")
    if len(parts) != 3:
        if idx_a < len(row):
            row[idx_a] = ""
        return row

    day, month, year = parts
    if not (day.isdigit() and month.isdigit() and year.isdigit()):
        if idx_a < len(row):
            row[idx_a] = ""
        return row

    shifted = _add_months(int(f"{year}{month}"), 1)
    if idx_a < len(row):
        row[idx_a] = format_reference_month(shifted)
    return row


def _decimal_to_sheet_number(value: Decimal):
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _decimal_to_int_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quantize_coef_2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_coef_2_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _quantize_coef_4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _quantize_coef_8(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _load_generation_projection_goal_by_month(
    spreadsheet_id: str,
    favorecido: str,
) -> dict[str, Decimal]:
    total_columns = _GENERATION_TOTAL_COLUMNS.get(favorecido)
    if total_columns is None:
        raise ValueError(f"Favorecido sem colunas de geração configuradas: {favorecido!r}")
    projected_col_index, _consolidated_col_index = total_columns

    ws = _get_worksheet_with_aliases(
        spreadsheet_id,
        RATEIO_GENERATION_SHEET_TAB,
        ("Geracao Total",),
    )

    rows = read_all_rows(ws, spreadsheet_id=spreadsheet_id)
    goals: dict[str, Decimal] = {}
    invalid = 0

    for row in rows:
        # Esperado na aba Geracao Total:
        # H=GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total Mensal (ProjeÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o), J=MÃƒÆ’Ã‚Âªs de referÃƒÆ’Ã‚Âªncia (GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total)
        month_ref = _normalize_month_reference(
            row[_GENERATION_TOTAL_MONTH_COL_INDEX]
            if len(row) > _GENERATION_TOTAL_MONTH_COL_INDEX
            else ""
        )
        raw_goal = row[projected_col_index] if len(row) > projected_col_index else ""
        goal = _to_decimal(raw_goal)
        if not month_ref:
            invalid += 1
            continue
        # A sincronizacao de Geracao Total representa ausencia de geracao
        # como celula vazia. Como a leitura foi bem-sucedida, esse caso e zero real.
        if goal is None and not str(raw_goal if raw_goal is not None else "").strip():
            goal = Decimal("0")
        if goal is None:
            invalid += 1
            continue
        goals[month_ref] = goal

    logger.info(
        "Geracao Total (%s / %s): %d metas mensais carregadas (%d linhas ignoradas).",
        spreadsheet_id,
        favorecido,
        len(goals),
        invalid,
    )
    return goals


def _validate_generation_goals(
    goals: dict[str, Decimal],
    required_months: set[str],
    *,
    sheet_label: str,
) -> None:
    missing = sorted(
        month for month in required_months if month not in goals
    )
    if missing:
        raise RuntimeError(
            f"Rateio '{sheet_label}' sem metas de geracao validadas para: "
            + ", ".join(missing)
        )


def _required_goal_months_for_rows(
    rows: list[list],
    frozen_rateio_months: set[str] | None = None,
) -> set[str]:
    required: set[str] = set()
    for row in rows:
        rateio_month = _normalize_month_reference_any(row[0] if row else "")
        if not rateio_month or _is_rateio_month_frozen(
            rateio_month,
            frozen_rateio_months,
        ):
            continue
        required.add(rateio_month)
    return required


def _required_goal_months_for_reference_months(
    months: set[str],
    frozen_rateio_months: set[str] | None = None,
) -> set[str]:
    required: set[str] = set()
    for month_ref in months:
        month_int = _month_ref_to_int(month_ref)
        if month_int is None:
            continue
        rateio_month = format_reference_month(_add_months(month_int, 1))
        if not _is_rateio_month_frozen(rateio_month, frozen_rateio_months):
            required.add(rateio_month)
    return required


def _apply_k_l_m_targets(
    rows: list[dict],
    *,
    goals: dict[str, Decimal],
    sheet_label: str,
    current_month: int,
    last_rateio_index: dict[tuple[str, str], str] | None = None,
    existing_m_by_key: dict[tuple[str, str], Decimal] | None = None,
    existing_k_by_key: dict[tuple[str, str], Decimal] | None = None,
    frozen_rateio_months: set[str] | None = None,
    history_invoice_issue_day_threshold: int = _DEFAULT_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD,
    special_allocation_by_month: dict[str, dict[str, str | Decimal]] | None = None,
    configured_coefficient_by_month: dict[str, Decimal] | None = None,
    contingency_coefficient_by_month: dict[str, Decimal] | None = None,
    default_contingency_coefficient: Decimal = Decimal("0.90000000"),
) -> None:
    rows_by_rateio_month: dict[str, list[dict]] = {}
    for r in rows:
        frozen = r.get("frozen") or _is_rateio_month_frozen(
            r.get("rateio_month", ""),
            frozen_rateio_months,
        )
        r["frozen"] = bool(frozen)
        if r.get("skip_calc") or frozen:
            continue
        if not r.get("uc") or not r.get("month_ref") or not r.get("rateio_month"):
            continue
        rateio_month = _normalize_month_reference_any(r["rateio_month"])
        if not rateio_month:
            continue
        r["rateio_month"] = rateio_month
        rows_by_rateio_month.setdefault(rateio_month, []).append(r)

    m_by_key: dict[tuple[str, str], Decimal] = dict(existing_m_by_key or {})
    k_by_key: dict[tuple[str, str], Decimal] = dict(existing_k_by_key or {})

    for rateio_month in sorted(rows_by_rateio_month, key=lambda m: _month_ref_to_int(m) or 0):
        month_rows = rows_by_rateio_month[rateio_month]
        reference_months = sorted(
            {
                _normalize_month_reference_any(r.get("month_ref", ""))
                for r in month_rows
                if _normalize_month_reference_any(r.get("month_ref", ""))
            },
            key=lambda m: _month_ref_to_int(m) or 0,
        )
        reference_label = ",".join(reference_months)
        goal = goals.get(rateio_month)
        if goal is None:
            raise RuntimeError(
                f"Rateio '{sheet_label}' alteracao {rateio_month} "
                f"(referencia {reference_label}) sem meta de geracao validada."
            )

        fixed_rows: list[dict] = []
        adjustable_rows: list[dict] = []

        for r in month_rows:
            uc_key = _normalize_uc_key(r.get("uc", ""))
            month_ref_norm = _normalize_month_reference_any(r.get("month_ref", ""))
            key = (uc_key, month_ref_norm)
            prev_key = (uc_key, _previous_month_reference(month_ref_norm))
            history_month = _history_month_for_last_rateio(
                rateio_month=rateio_month,
                month_ref=month_ref_norm,
                invoice_issue_day=r.get("invoice_issue_day"),
                invoice_issue_day_threshold=history_invoice_issue_day_threshold,
            )
            history_key = (uc_key, history_month)

            history_j = ""
            if last_rateio_index is not None:
                history_j = str(last_rateio_index.get(history_key, "")).strip()

            if history_j:
                j_value = _to_decimal(history_j) or Decimal("0")
                r["j_target"] = j_value
            else:
                previous_m = m_by_key.get(history_key)
                if previous_m is None:
                    j_value = Decimal("0")
                    r["j_target"] = ""
                else:
                    j_value = previous_m
                    r["j_target"] = j_value
            r["j"] = j_value

            source_i = r.get("source_i")
            source_i_has_value = bool(r.get("source_i_has_value", False))
            if source_i_has_value and source_i is not None:
                saldo_base = max(source_i, Decimal("0"))
                r["i_target"] = saldo_base
            else:
                previous_k = k_by_key.get(prev_key)
                if previous_k is None:
                    saldo_base = Decimal("0")
                    r["i_target"] = ""
                else:
                    saldo_base = previous_k
                    r["i_target"] = saldo_base

            if saldo_base < 0:
                saldo_base = Decimal("0")
            r["i"] = saldo_base
            saldo = saldo_base + j_value - r["h"]
            if saldo < 0:
                saldo = Decimal("0")
            r["k_target"] = saldo
            r["saldo_base"] = saldo_base
            r["necessidade_injecao"] = max(r["h"] - saldo_base, Decimal("0"))

            if r.get("is_contingencia"):
                fixed_rows.append(r)
            else:
                adjustable_rows.append(r)

        goal_int = Decimal(max(int(goal), 0))

        def _m_int_for_row(rr: dict, coef: Decimal) -> Decimal:
            m_val = (_m_consumption_for_row(rr) * coef) - rr["k_target"]
            if m_val < 0:
                m_val = Decimal("0")
            return Decimal(_decimal_to_int_half_up(m_val))

        def _total_int_for_rows(target_rows: list[dict], coef: Decimal, *, base_total: Decimal = Decimal("0")) -> Decimal:
            total = base_total
            for rr in target_rows:
                m_val = (_m_consumption_for_row(rr) * coef) - rr["k_target"]
                if m_val < 0:
                    m_val = Decimal("0")
                total += Decimal(_decimal_to_int_half_up(m_val))
            return total

        def _best_coef_under_goal(
            target_rows: list[dict],
            *,
            base_total: Decimal,
            max_coef: Decimal = Decimal("100.00"),
        ) -> tuple[Decimal, Decimal]:
            if not target_rows or base_total >= goal_int:
                return Decimal("0.0000"), base_total

            precision = Decimal("100000000")
            max_units = int(max_coef * precision)
            low = 0
            high = min(int(Decimal("1.00000000") * precision), max_units)
            while high < max_units:
                coef = Decimal(high) / precision
                total = _total_int_for_rows(target_rows, coef, base_total=base_total)
                if total > goal_int:
                    break
                low = high
                high = min(high * 2, max_units)
                if high == low:
                    break

            high_coef = Decimal(high) / precision
            if _total_int_for_rows(target_rows, high_coef, base_total=base_total) <= goal_int:
                low = high
            else:
                search_low = low
                search_high = high
                while search_low < search_high:
                    mid = (search_low + search_high + 1) // 2
                    coef = Decimal(mid) / precision
                    total = _total_int_for_rows(target_rows, coef, base_total=base_total)
                    if total <= goal_int:
                        search_low = mid
                    else:
                        search_high = mid - 1
                low = search_low

            coef = _quantize_coef_8(Decimal(low) / precision)
            return coef, _total_int_for_rows(target_rows, coef, base_total=base_total)

        def _matches_remainder_rule(rr: dict, rule: dict[str, str | Decimal]) -> bool:
            remainder_uc = str(rule.get("remainder_uc") or "").strip()
            if remainder_uc:
                return _normalize_uc_key(rr.get("uc", "")) == _normalize_uc_key(remainder_uc)

            remainder_favorecido = str(rule.get("remainder_favorecido") or "").strip()
            if remainder_favorecido:
                return _normalize_status_key(rr.get("favorecido", "")) == _normalize_status_key(
                    remainder_favorecido
                )

            return False

        def _m_consumption_for_row(rr: dict) -> Decimal:
            consumo = rr.get("m_consumo")
            if isinstance(consumo, Decimal):
                return consumo
            consumo = rr.get("h", Decimal("0"))
            return consumo if isinstance(consumo, Decimal) else Decimal("0")

        def _coef_for_direct_m(rr: dict, m_int: Decimal) -> Decimal:
            consumo = _m_consumption_for_row(rr)
            if consumo <= 0:
                return Decimal("0.00000000")
            saldo_final = rr.get("k_target", Decimal("0"))
            if not isinstance(saldo_final, Decimal):
                saldo_final = Decimal("0")
            return _quantize_coef_8((m_int + saldo_final) / consumo)

        def _apply_special_allocation(
            rule: dict[str, str | Decimal] | None,
        ) -> bool:
            if not rule:
                return False

            base_coef_raw = rule.get("base_coef")
            if not isinstance(base_coef_raw, Decimal):
                return False

            remainder_rows = [
                rr
                for rr in adjustable_rows
                if _matches_remainder_rule(rr, rule)
            ]
            eligible_remainder_rows = [
                rr
                for rr in remainder_rows
                if isinstance(rr.get("h"), Decimal) and rr["h"] > 0
            ]
            if not eligible_remainder_rows:
                logger.warning(
                    (
                        "Rateio '%s' mes %s: regra de alocacao especial ignorada "
                        "porque nao ha linhas elegiveis para a sobra."
                    ),
                    sheet_label,
                    rateio_month,
                )
                return False

            remainder_row_ids = {id(rr) for rr in remainder_rows}
            base_rows = [rr for rr in adjustable_rows if id(rr) not in remainder_row_ids]
            base_coef = _quantize_coef_8(base_coef_raw)
            base_sum_int = _total_int_for_rows(base_rows, base_coef)
            available = goal_int - fixed_sum_int - base_sum_int
            if available < 0:
                logger.warning(
                    (
                        "Rateio '%s' mes %s: coeficiente base especial %s "
                        "ultrapassa a geracao antes da sobra (fixo=%s, base=%s, geracao=%s)."
                    ),
                    sheet_label,
                    rateio_month,
                    _format_decimal_plain(base_coef),
                    _format_decimal_plain(fixed_sum_int),
                    _format_decimal_plain(base_sum_int),
                    _format_decimal_plain(goal_int),
                )
                available = Decimal("0")

            for rr in fixed_rows:
                rr["l_target"] = contingency_coef
                rr["m_target"] = _m_int_for_row(rr, contingency_coef)

            for rr in base_rows:
                rr["l_target"] = base_coef
                rr["m_target"] = _m_int_for_row(rr, base_coef)

            for rr in remainder_rows:
                rr["l_target"] = Decimal("0.00000000")
                rr["m_target"] = Decimal("0")

            available_int = max(int(available), 0)
            row_count = len(eligible_remainder_rows)
            remainder_uc = str(rule.get("remainder_uc") or "").strip()
            if remainder_uc:
                base_share = available_int // row_count
                extra = available_int % row_count
                for idx, rr in enumerate(eligible_remainder_rows):
                    m_int = Decimal(base_share + (1 if idx < extra else 0))
                    rr["m_target"] = m_int
                    rr["l_target"] = _coef_for_direct_m(rr, m_int)
                allocation_mode = "uc_direta"
                remainder_coef = None
            else:
                remainder_coef, total_with_remainder = _best_coef_under_goal(
                    remainder_rows,
                    base_total=fixed_sum_int + base_sum_int,
                )
                for rr in remainder_rows:
                    rr["l_target"] = remainder_coef
                    rr["m_target"] = _m_int_for_row(rr, remainder_coef)
                allocation_mode = "coeficiente_favorecido"
                available = total_with_remainder - fixed_sum_int - base_sum_int

            logger.info(
                (
                    "Rateio '%s' mes %s: alocacao especial aplicada "
                    "(coef_base=%s, base=%s, sobra=%s, linhas_sobra=%d, modo=%s, coef_sobra=%s)."
                ),
                sheet_label,
                rateio_month,
                _format_decimal_plain(base_coef),
                _format_decimal_plain(base_sum_int),
                _format_decimal_plain(available),
                row_count,
                allocation_mode,
                _format_decimal_plain(remainder_coef) if remainder_coef is not None else "",
            )
            return True

        contingency_coef = _quantize_coef_8(
            contingency_coefficient_by_month.get(rateio_month, default_contingency_coefficient)
            if contingency_coefficient_by_month is not None
            else default_contingency_coefficient
        )
        fixed_sum_int = _total_int_for_rows(fixed_rows, contingency_coef)
        global_coef = Decimal("0.00000000")

        if fixed_sum_int > goal_int:
            logger.warning(
                (
                    "Rateio '%s' mes %s: contingencia fixa %s ultrapassa a geracao "
                    "(soma=%s, geracao=%s); coeficiente mantido por regra."
                ),
                sheet_label,
                rateio_month,
                _format_decimal_plain(contingency_coef),
                _format_decimal_plain(fixed_sum_int),
                _format_decimal_plain(goal_int),
            )

        special_rule = (special_allocation_by_month or {}).get(rateio_month)
        special_applied = _apply_special_allocation(special_rule)
        configured_coef = (
            (configured_coefficient_by_month or {}).get(rateio_month)
            if configured_coefficient_by_month is not None
            else None
        )
        configured_applied = False

        if not special_applied and configured_coef is not None:
            global_coef = _quantize_coef_8(configured_coef)
            configured_applied = True
            logger.info(
                "Rateio '%s' mes %s: coeficiente manual configurado aplicado (%s).",
                sheet_label,
                rateio_month,
                _format_decimal_plain(global_coef),
            )
        elif not special_applied and adjustable_rows and fixed_sum_int < goal_int:
            global_coef, _ = _best_coef_under_goal(adjustable_rows, base_total=fixed_sum_int)

        if not special_applied:
            for rr in fixed_rows:
                rr["l_target"] = contingency_coef
                rr["m_target"] = _m_int_for_row(rr, contingency_coef)

            for rr in adjustable_rows:
                rr["l_target"] = global_coef
                rr["m_target"] = _m_int_for_row(rr, global_coef)

        for rr in month_rows:
            rr["_m_int"] = Decimal(_decimal_to_int_half_up(rr.get("m_target", Decimal("0"))))
            rr["m_target"] = rr["_m_int"]
            uc_key = _normalize_uc_key(rr.get("uc", ""))
            month_ref_norm = _normalize_month_reference_any(rr.get("month_ref", ""))
            rateio_month_norm = _normalize_month_reference_any(rr.get("rateio_month", ""))
            if uc_key and rateio_month_norm:
                m_by_key[(uc_key, rateio_month_norm)] = rr["_m_int"]
            if uc_key and month_ref_norm:
                k_by_key[(uc_key, month_ref_norm)] = rr.get("k_target", Decimal("0"))

        total_consumo = Decimal("0")
        total_consumo_m = Decimal("0")
        total_necessidade = Decimal("0")
        final_sum_int = Decimal("0")
        for rr in month_rows:
            total_consumo += rr["h"]
            total_consumo_m += _m_consumption_for_row(rr)
            total_necessidade += rr.get("necessidade_injecao", Decimal("0"))
            final_sum_int += rr.get("_m_int", Decimal("0"))
        diff = goal_int - final_sum_int
        if final_sum_int > goal_int and fixed_sum_int > goal_int:
            logger.warning(
                (
                    "Rateio '%s' mes %s: inviavel nao ultrapassar geracao apenas com ajuste de coeficiente "
                    "(contingencia fixa soma=%s > geracao=%s)."
                ),
                sheet_label,
                rateio_month,
                _format_decimal_plain(fixed_sum_int),
                _format_decimal_plain(goal_int),
            )

        logger.info(
            (
                "Rateio '%s' alteracao %s (referencia %s): consumo_h=%s, consumo_m=%s, necessidade=%s, "
                "geracao=%s, coef_global=%s, soma_novo_rateio=%s, diferenca=%s%s"
            ),
            sheet_label,
            rateio_month,
            reference_label,
            _format_decimal_plain(total_consumo),
            _format_decimal_plain(total_consumo_m),
            _format_decimal_plain(total_necessidade),
            _format_decimal_plain(goal),
            _format_decimal_plain(global_coef),
            _format_decimal_plain(final_sum_int),
            _format_decimal_plain(diff),
            " (manual)" if configured_applied else "",
        )


def _recalculate_k_l_m_with_monthly_goal(
    ws,
    *,
    spreadsheet_id: str,
    favorecido: str,
    current_month: int,
    monthly_goals: dict[str, Decimal] | None = None,
    last_rateio_index: dict[tuple[str, str], str] | None = None,
    frozen_rateio_months: set[str] | None = None,
    projection_index: dict[tuple[str, str], str] | None = None,
    start_row: int = DATA_START_ROW,
    end_row: int | None = None,
) -> tuple[int, int, int]:
    """
    Recalcula colunas:
    - K = max(I + J - H, 0)
    - L = coeficiente com 2 casas
    - M = max(previsao do mes de alteracao * L - K, 0)

    Regras:
    - Saldo (I) sempre considerado no cÃƒÆ’Ã‚Â¡lculo.
    - Base unitÃƒÆ’Ã‚Â¡ria mÃƒÆ’Ã‚Â­nima: I + M >= H (suprimento do cooperado).
    - Meta mensal (GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total) ÃƒÆ’Ã‚Â© ajustada no fechamento por mÃƒÆ’Ã‚Âªs.
    """
    max_row = int(end_row or ws.row_count)
    if max_row < start_row:
        return 0, 0, 0

    goals = (
        monthly_goals
        if monthly_goals is not None
        else _load_generation_projection_goal_by_month(spreadsheet_id, favorecido)
    )
    special_allocation_by_month = _load_special_allocation_by_month(
        spreadsheet_id,
        favorecido,
    )
    configured_coefficient_by_month = _load_configured_coefficient_by_month(
        spreadsheet_id,
        favorecido,
    )
    distributor = _distributor_for_rateio_spreadsheet(spreadsheet_id)
    contingency_coefficient_by_month = _load_contingency_coefficient_by_month(
        spreadsheet_id,
        favorecido,
    )
    default_contingency_coefficient = _default_contingency_coefficient(
        distributor,
        favorecido,
    )

    all_rows: list[dict] = []
    existing_m_by_key: dict[tuple[str, str], Decimal] = {}
    existing_k_by_key: dict[tuple[str, str], Decimal] = {}
    changed_i = 0
    changed_j = 0
    changed_k = 0
    changed_l = 0
    changed_m = 0

    for chunk_start in range(start_row, max_row + 1, CHUNK_SIZE):
        chunk_end = min(max_row, chunk_start + CHUNK_SIZE - 1)
        values = _values_get(ws, f"A{chunk_start}:O{chunk_end}", spreadsheet_id=spreadsheet_id)
        expected_size = chunk_end - chunk_start + 1

        for offset in range(expected_size):
            row_idx = chunk_start + offset
            row = values[offset] if offset < len(values) else []

            # Range A:P => alteracao=A(0), status=B(1), uc=D(3), mes=G(6),
            # H(7), I(8), J(9), K(10), L(11), M(12), dia emissao=N(13),
            # favorecido=O(14), UC Aneel=P(15).
            rateio_month = str(row[0] if len(row) > 0 else "").strip()
            status_value = str(row[1] if len(row) > 1 else "").strip()
            uc = normalize_uc(row[3] if len(row) > 3 else "")
            month_ref = str(row[6] if len(row) > 6 else "").strip()
            val_h = _to_decimal(row[7] if len(row) > 7 else "")
            i_raw = row[8] if len(row) > 8 else ""
            val_i = _to_decimal(i_raw)
            i_has_value = str(i_raw).strip() != ""
            current_j = str(row[9] if len(row) > 9 else "").strip()
            uc_key = _normalize_uc_key(uc)
            val_j = _to_decimal(current_j)
            current_k = str(row[10] if len(row) > 10 else "").strip()
            current_k_num = _to_decimal(current_k)
            current_l = str(row[11] if len(row) > 11 else "").strip()
            current_m = str(row[12] if len(row) > 12 else "").strip()
            invoice_issue_day = row[13] if len(row) > 13 else ""
            favorecido_row = str(row[14] if len(row) > 14 else "").strip()
            month_ref_norm = _normalize_month_reference_any(month_ref)
            rateio_month_norm = _normalize_month_reference_any(rateio_month)
            current_m_num = _to_decimal(current_m)
            h_value = val_h or Decimal("0")
            m_consumo = _new_rateio_consumption_for_alteracao_month(
                uc,
                rateio_month_norm,
                projection_index,
                h_value,
            )
            if uc_key and rateio_month_norm and current_m_num is not None:
                existing_m_by_key[(uc_key, rateio_month_norm)] = current_m_num
            if uc_key and month_ref_norm and current_k_num is not None:
                existing_k_by_key[(uc_key, month_ref_norm)] = current_k_num

            parsed = {
                "row_idx": row_idx,
                "status": status_value,
                "uc": uc,
                "month_ref": month_ref,
                "rateio_month": _normalize_month_reference_any(rateio_month),
                "invoice_issue_day": invoice_issue_day,
                "favorecido": favorecido_row,
                "h": h_value,
                "m_consumo": m_consumo,
                "i": val_i or Decimal("0"),
                "i_has_value": i_has_value,
                "source_i": val_i,
                "source_i_has_value": i_has_value,
                "current_i": str(i_raw).strip(),
                "i_target": str(i_raw).strip(),
                "j": val_j or Decimal("0"),
                "current_j": current_j,
                "j_target": current_j,
                "current_k": current_k,
                "current_k_num": current_k_num,
                "current_l": current_l,
                "current_m": current_m,
                "k_target": Decimal("0"),
                "l_target": None,
                "m_target": None,
                "is_contingencia": _contains_contingencia(status_value),
                "skip_calc": _is_status_excluded_from_projection(status_value),
                "frozen": _is_rateio_month_frozen(
                    rateio_month_norm,
                    frozen_rateio_months,
                ),
            }

            if parsed["frozen"]:
                parsed["i_target"] = parsed["current_i"]
                parsed["j_target"] = parsed["current_j"]
                parsed["k_target"] = parsed["current_k"]
                parsed["l_target"] = parsed["current_l"]
                parsed["m_target"] = parsed["current_m"]
                all_rows.append(parsed)
                continue

            # Linha vazia/invalida: limpa todo bloco calculado para evitar lixo historico.
            if not uc or not month_ref or not parsed["rateio_month"]:
                parsed["i_target"] = ""
                parsed["j_target"] = ""
                parsed["k_target"] = ""
                parsed["l_target"] = ""
                parsed["m_target"] = ""
                all_rows.append(parsed)
                continue

            if parsed["skip_calc"]:
                parsed["i_target"] = ""
                parsed["j_target"] = ""
                parsed["k_target"] = ""
                parsed["l_target"] = ""
                parsed["m_target"] = ""
                all_rows.append(parsed)
                continue

            saldo = parsed["i"] + parsed["j"] - parsed["h"]
            if saldo < 0:
                saldo = Decimal("0")
            parsed["k_target"] = saldo
            all_rows.append(parsed)
    _apply_k_l_m_targets(
        all_rows,
        goals=goals,
        sheet_label=ws.title,
        current_month=current_month,
        last_rateio_index=last_rateio_index,
        existing_m_by_key=existing_m_by_key,
        existing_k_by_key=existing_k_by_key,
        frozen_rateio_months=frozen_rateio_months,
        history_invoice_issue_day_threshold=_history_invoice_issue_day_threshold_for_spreadsheet(
            spreadsheet_id
        ),
        special_allocation_by_month=special_allocation_by_month,
        configured_coefficient_by_month=configured_coefficient_by_month,
        contingency_coefficient_by_month=contingency_coefficient_by_month,
        default_contingency_coefficient=default_contingency_coefficient,
    )

    updates: list[dict] = []
    for r in all_rows:
        if r.get("frozen"):
            continue
        row_idx = int(r["row_idx"])

        # K sempre numÃƒÆ’Ã‚Â©rico para linhas vÃƒÆ’Ã‚Â¡lidas; vazio para invÃƒÆ’Ã‚Â¡lidas
        if (
            r.get("skip_calc")
            or not r["uc"]
            or not r["month_ref"]
            or not r.get("rateio_month")
        ):
            k_target_cell = ""
        else:
            k_target_cell = _decimal_to_sheet_number(r["k_target"])

        i_target_raw = r.get("i_target", "")
        l_target_raw = r["l_target"]
        m_target_raw = r["m_target"]

        if isinstance(i_target_raw, Decimal):
            i_target_cell = _decimal_to_sheet_number(i_target_raw)
        else:
            i_target_cell = i_target_raw

        if isinstance(l_target_raw, Decimal):
            l_target_cell = _decimal_to_sheet_number(l_target_raw)
        else:
            l_target_cell = l_target_raw

        if isinstance(m_target_raw, Decimal):
            m_target_cell = int(m_target_raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            m_target_cell = m_target_raw

        j_target_cell = _to_sheet_number_or_blank(r.get("j_target", ""))
        i_target_cmp = str(i_target_cell).strip()
        k_target_cmp = str(k_target_cell).strip()
        l_target_cmp = str("" if l_target_cell is None else l_target_cell).strip()
        m_target_cmp = str("" if m_target_cell is None else m_target_cell).strip()
        j_target_cmp = str(j_target_cell).strip()
        i_changed = str(r.get("current_i", "")).strip() != i_target_cmp
        j_changed = str(r.get("current_j", "")).strip() != j_target_cmp
        k_changed = r["current_k"] != k_target_cmp
        l_changed = r["current_l"] != l_target_cmp
        m_changed = r["current_m"] != m_target_cmp

        if not i_changed and not j_changed and not k_changed and not l_changed and not m_changed:
            continue

        rng = f"{_quote_sheet_title(ws.title)}!I{row_idx}:M{row_idx}"
        updates.append(
            {
                "range": rng,
                "values": [[i_target_cell, j_target_cell, k_target_cell, l_target_cell, m_target_cell]],
            }
        )
        if i_changed:
            changed_i += 1
        if j_changed:
            changed_j += 1
        if k_changed:
            changed_k += 1
        if l_changed:
            changed_l += 1
        if m_changed:
            changed_m += 1

    if not updates:
        return 0, 0, 0

    for i in range(0, len(updates), CHUNK_SIZE):
        chunk = updates[i:i + CHUNK_SIZE]
        _values_batch_update(ws, chunk, spreadsheet_id=spreadsheet_id)
        if i + CHUNK_SIZE < len(updates):
            time.sleep(0.2)

    if changed_j:
        logger.info("Ultimo rateio (J): %d linhas atualizadas na aba '%s'.", changed_j, ws.title)
    if changed_i:
        logger.info("Saldo (I): %d linhas atualizadas na aba '%s'.", changed_i, ws.title)

    return changed_k, changed_l, changed_m


def _recalculate_k_l_m_for_months(
    ws,
    *,
    spreadsheet_id: str,
    favorecido: str,
    months: set[str],
    current_month: int,
    monthly_goals: dict[str, Decimal] | None = None,
    last_rateio_index: dict[tuple[str, str], str] | None = None,
    frozen_rateio_months: set[str] | None = None,
    projection_index: dict[tuple[str, str], str] | None = None,
) -> tuple[int, int, int]:
    """
    Recalcula K/L/M apenas para os meses impactados.
    """
    if not months:
        return 0, 0, 0

    goals = (
        monthly_goals
        if monthly_goals is not None
        else _load_generation_projection_goal_by_month(spreadsheet_id, favorecido)
    )
    special_allocation_by_month = _load_special_allocation_by_month(
        spreadsheet_id,
        favorecido,
    )
    configured_coefficient_by_month = _load_configured_coefficient_by_month(
        spreadsheet_id,
        favorecido,
    )
    distributor = _distributor_for_rateio_spreadsheet(spreadsheet_id)
    contingency_coefficient_by_month = _load_contingency_coefficient_by_month(
        spreadsheet_id,
        favorecido,
    )
    default_contingency_coefficient = _default_contingency_coefficient(
        distributor,
        favorecido,
    )
    all_rows = read_all_rows(ws, spreadsheet_id=spreadsheet_id)

    target_rows: list[dict] = []
    existing_m_by_key: dict[tuple[str, str], Decimal] = {}
    existing_k_by_key: dict[tuple[str, str], Decimal] = {}
    for idx, row in enumerate(all_rows):
        row_idx = idx + DATA_START_ROW
        rateio_month = str(row[0] if len(row) > 0 else "").strip()
        status_value = str(row[1] if len(row) > 1 else "").strip()
        uc = normalize_uc(row[3] if len(row) > 3 else "")
        month_ref = str(row[6] if len(row) > 6 else "").strip()
        uc_key_all = _normalize_uc_key(uc)
        month_ref_norm = _normalize_month_reference_any(month_ref)
        rateio_month_norm = _normalize_month_reference_any(rateio_month)
        existing_m = _to_decimal(row[12] if len(row) > 12 else "")
        if uc_key_all and rateio_month_norm and existing_m is not None:
            existing_m_by_key[(uc_key_all, rateio_month_norm)] = existing_m
        existing_k = _to_decimal(row[10] if len(row) > 10 else "")
        if uc_key_all and month_ref_norm and existing_k is not None:
            existing_k_by_key[(uc_key_all, month_ref_norm)] = existing_k

        if month_ref not in months or _is_rateio_month_frozen(
            rateio_month_norm,
            frozen_rateio_months,
        ):
            continue

        val_h = _to_decimal(row[7] if len(row) > 7 else "")
        i_raw = row[8] if len(row) > 8 else ""
        val_i = _to_decimal(i_raw)
        i_has_value = str(i_raw).strip() != ""
        source_i_has_value = i_has_value
        current_j = str(row[9] if len(row) > 9 else "").strip()
        val_j = _to_decimal(current_j)
        current_k = str(row[10] if len(row) > 10 else "").strip()
        current_k_num = _to_decimal(current_k)
        current_l = str(row[11] if len(row) > 11 else "").strip()
        current_m = str(row[12] if len(row) > 12 else "").strip()
        invoice_issue_day = row[13] if len(row) > 13 else ""
        favorecido_row = str(row[14] if len(row) > 14 else "").strip()
        h_value = val_h or Decimal("0")
        m_consumo = _new_rateio_consumption_for_alteracao_month(
            uc,
            rateio_month_norm,
            projection_index,
            h_value,
        )

        parsed = {
            "row_idx": row_idx,
            "status": status_value,
            "uc": uc,
            "month_ref": month_ref,
            "rateio_month": _normalize_month_reference_any(rateio_month),
            "invoice_issue_day": invoice_issue_day,
            "favorecido": favorecido_row,
            "h": h_value,
            "m_consumo": m_consumo,
            "i": val_i or Decimal("0"),
            "i_has_value": i_has_value,
            "source_i": val_i,
            "source_i_has_value": source_i_has_value,
            "current_i": str(i_raw).strip(),
            "i_target": str(i_raw).strip(),
            "j": val_j or Decimal("0"),
            "current_j": current_j,
            "j_target": current_j,
            "current_k": current_k,
            "current_k_num": current_k_num,
            "current_l": current_l,
            "current_m": current_m,
            "k_target": Decimal("0"),
            "l_target": current_l,
            "m_target": current_m,
            "is_contingencia": _contains_contingencia(status_value),
            "skip_calc": _is_status_excluded_from_projection(status_value),
            "frozen": False,
        }

        if not uc or not month_ref or not parsed["rateio_month"]:
            parsed["i_target"] = ""
            parsed["j_target"] = ""
            parsed["k_target"] = ""
            parsed["l_target"] = ""
            parsed["m_target"] = ""
        elif parsed["skip_calc"]:
            parsed["i_target"] = ""
            parsed["j_target"] = ""
            parsed["k_target"] = ""
            parsed["l_target"] = ""
            parsed["m_target"] = ""
        else:
            saldo = parsed["i"] + parsed["j"] - parsed["h"]
            if saldo < 0:
                saldo = Decimal("0")
            parsed["k_target"] = saldo

        target_rows.append(parsed)

    if not target_rows:
        return 0, 0, 0

    _apply_k_l_m_targets(
        target_rows,
        goals=goals,
        sheet_label=ws.title,
        current_month=current_month,
        last_rateio_index=last_rateio_index,
        existing_m_by_key=existing_m_by_key,
        existing_k_by_key=existing_k_by_key,
        frozen_rateio_months=frozen_rateio_months,
        history_invoice_issue_day_threshold=_history_invoice_issue_day_threshold_for_spreadsheet(
            spreadsheet_id
        ),
        special_allocation_by_month=special_allocation_by_month,
        configured_coefficient_by_month=configured_coefficient_by_month,
        contingency_coefficient_by_month=contingency_coefficient_by_month,
        default_contingency_coefficient=default_contingency_coefficient,
    )

    updates: list[dict] = []
    changed_i = 0
    changed_j = 0
    changed_k = 0
    changed_l = 0
    changed_m = 0

    for r in target_rows:
        row_idx = int(r["row_idx"])
        if (
            r.get("skip_calc")
            or not r["uc"]
            or not r["month_ref"]
            or not r.get("rateio_month")
        ):
            k_target_cell = ""
        else:
            k_target_cell = _decimal_to_sheet_number(r["k_target"])

        i_target_raw = r.get("i_target", "")
        l_target_raw = r["l_target"]
        m_target_raw = r["m_target"]

        if isinstance(i_target_raw, Decimal):
            i_target_cell = _decimal_to_sheet_number(i_target_raw)
        else:
            i_target_cell = i_target_raw

        if isinstance(l_target_raw, Decimal):
            l_target_cell = _decimal_to_sheet_number(l_target_raw)
        else:
            l_target_cell = l_target_raw

        if isinstance(m_target_raw, Decimal):
            m_target_cell = int(m_target_raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            m_target_cell = m_target_raw

        j_target_cell = _to_sheet_number_or_blank(r.get("j_target", ""))
        i_target_cmp = str(i_target_cell).strip()
        k_target_cmp = str(k_target_cell).strip()
        l_target_cmp = str("" if l_target_cell is None else l_target_cell).strip()
        m_target_cmp = str("" if m_target_cell is None else m_target_cell).strip()
        j_target_cmp = str(j_target_cell).strip()
        i_changed = str(r.get("current_i", "")).strip() != i_target_cmp
        j_changed = str(r.get("current_j", "")).strip() != j_target_cmp
        k_changed = r["current_k"] != k_target_cmp
        l_changed = r["current_l"] != l_target_cmp
        m_changed = r["current_m"] != m_target_cmp
        if not i_changed and not j_changed and not k_changed and not l_changed and not m_changed:
            continue

        rng = f"{_quote_sheet_title(ws.title)}!I{row_idx}:M{row_idx}"
        updates.append(
            {
                "range": rng,
                "values": [[i_target_cell, j_target_cell, k_target_cell, l_target_cell, m_target_cell]],
            }
        )
        if i_changed:
            changed_i += 1
        if j_changed:
            changed_j += 1
        if k_changed:
            changed_k += 1
        if l_changed:
            changed_l += 1
        if m_changed:
            changed_m += 1

    if not updates:
        return 0, 0, 0

    for i in range(0, len(updates), CHUNK_SIZE):
        chunk = updates[i:i + CHUNK_SIZE]
        _values_batch_update(ws, chunk, spreadsheet_id=spreadsheet_id)
        if i + CHUNK_SIZE < len(updates):
            time.sleep(0.2)

    if changed_j:
        logger.info("Ultimo rateio (J): %d linhas atualizadas na aba '%s' (delta).", changed_j, ws.title)
    if changed_i:
        logger.info("Saldo (I): %d linhas atualizadas na aba '%s' (delta).", changed_i, ws.title)
    return changed_k, changed_l, changed_m


def _normalize_text(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_uc_key(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
    raw_s = str(value or "").strip()
    if raw_s.endswith(".0") and raw_s.replace(".", "", 1).isdigit():
        raw_s = raw_s[:-2]
    raw = unicodedata.normalize("NFKD", raw_s)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^A-Za-z0-9]", "", raw)
    return raw.upper()


def _extract_uc_from_client_name(value) -> str:
    text = str(value or "")
    if not text.strip():
        return ""
    m = re.search(r"\bUC\s*([0-9]+)\b", text, flags=re.IGNORECASE)
    if not m:
        return ""
    return _normalize_uc_key(m.group(1))


def _normalize_month_reference_any(value) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        num = float(value)
        if num > 20000:
            dt = _SHEETS_SERIAL_BASE + timedelta(days=int(num))
            return f"01-{dt.month:02d}-{dt.year}"
        maybe = int(num)
        year = maybe // 100
        month = maybe % 100
        if 1900 <= year <= 2100 and 1 <= month <= 12:
            return f"01-{month:02d}-{year}"

    raw = str(value).strip()
    if not raw:
        return ""

    normalized_number = raw.replace(",", ".")
    try:
        numeric_value = Decimal(normalized_number)
    except InvalidOperation:
        numeric_value = None
    if numeric_value is not None and numeric_value == numeric_value.to_integral_value():
        serial_value = int(numeric_value)
        if 20000 < serial_value < 80000:
            dt = _SHEETS_SERIAL_BASE + timedelta(days=serial_value)
            return f"01-{dt.month:02d}-{dt.year}"

    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%Y",
        "%m-%Y",
        "%m/%y",
        "%m-%y",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return f"01-{dt.month:02d}-{dt.year}"
        except ValueError:
            pass

    return _normalize_month_reference(raw)


def _month_ref_to_int(month_ref: str) -> int | None:
    normalized = _normalize_month_reference_any(month_ref)
    if not normalized:
        return None
    parts = normalized.split("-")
    if len(parts) != 3:
        return None
    _day, month, year = parts
    if not (month.isdigit() and year.isdigit()):
        return None
    return int(f"{year}{month}")


def _configuration_header_and_data_rows(rows: list[list]) -> tuple[dict[str, int], list[list], int]:
    if not rows:
        return {}, [], 0
    first_month = _month_ref_to_int(_row_value_by_index(rows[0], 0))
    if first_month is not None:
        return {}, rows, 0
    return _configuration_header_indexes(rows[0]), rows[1:], 1


def _load_frozen_rateio_months(spreadsheet_id: str) -> set[str]:
    """
    Le Configuracao por cabecalho.

    A coluna de status pode variar por distribuidora no layout atual; layouts
    antigos ainda sao aceitos por fallback.
    Somente "Fechado" congela; "Aberto", vazio e outros valores não bloqueiam.
    """
    try:
        ws = get_worksheet(
            _RATEIO_CONFIGURATION_TAB,
            spreadsheet_id=spreadsheet_id,
            create_if_missing=False,
        )
    except RuntimeError as exc:
        if "nao encontrada" not in str(exc).lower():
            raise
        logger.info(
            "Configuração (%s): aba ausente; todos os meses permanecem abertos.",
            spreadsheet_id,
        )
        return set()

    rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
    )
    if not rows:
        return set()

    header_indexes, data_rows, _data_start_index = _configuration_header_and_data_rows(rows)
    month_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_MONTH_HEADER,
        fallback=0,
    )
    status_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_STATUS_HEADER,
        fallback=4,
    )
    legacy_status_index = 2
    frozen: set[str] = set()
    open_months: set[str] = set()
    invalid_statuses: list[tuple[str, str]] = []

    for row in data_rows:
        rateio_month = _normalize_month_reference_any(row[0] if row else "")
        if month_index is not None:
            rateio_month = _normalize_month_reference_any(
                row[month_index] if len(row) > month_index else ""
            )
        status_raw = _row_value_by_index(row, status_index)
        legacy_status = _row_value_by_index(row, legacy_status_index)
        if not status_raw and _normalize_status_key(legacy_status) in {
            "aberto",
            _RATEIO_CONFIGURATION_CLOSED_STATUS,
        }:
            status_raw = legacy_status
        status = _normalize_status_key(status_raw)
        if not rateio_month:
            continue
        if status == _RATEIO_CONFIGURATION_CLOSED_STATUS:
            frozen.add(rateio_month)
        else:
            open_months.add(rateio_month)
            if status and status != "aberto":
                invalid_statuses.append((rateio_month, status_raw))

    if invalid_statuses:
        logger.warning(
            "Configuração (%s): %d status não reconhecidos foram tratados como abertos: %s",
            spreadsheet_id,
            len(invalid_statuses),
            ", ".join(f"{month}={status!r}" for month, status in invalid_statuses[:10]),
        )

    sort_months = lambda values: sorted(
        values,
        key=lambda value: _month_ref_to_int(value) or 0,
    )
    logger.info(
        "Configuração (%s): meses abertos (%d): %s | meses fechados (%d): %s",
        spreadsheet_id,
        len(open_months),
        ", ".join(sort_months(open_months)) or "nenhum",
        len(frozen),
        ", ".join(sort_months(frozen)) or "nenhum",
    )
    return frozen


def _load_special_allocation_by_month(
    spreadsheet_id: str,
    favorecido: str,
) -> dict[str, dict[str, str | Decimal]]:
    distributor = _distributor_for_rateio_spreadsheet(spreadsheet_id)
    defaults = _special_allocation_defaults(distributor, favorecido)
    if defaults is None:
        return {}

    try:
        ws = get_worksheet(
            _RATEIO_CONFIGURATION_TAB,
            spreadsheet_id=spreadsheet_id,
            create_if_missing=False,
        )
    except RuntimeError as exc:
        if "nao encontrada" not in str(exc).lower():
            raise
        return {}

    rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
    )
    formatted_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    formula_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMULA",
    )
    if not rows:
        return {}

    header_indexes, data_rows, data_start_index = _configuration_header_and_data_rows(rows)
    legacy_layout = not header_indexes
    month_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_MONTH_HEADER,
        fallback=0,
    )
    base_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER,
        fallback=5 if legacy_layout else None,
    )
    remainder_favorecido_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER,
        fallback=6 if legacy_layout else None,
    )
    remainder_uc_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_REMAINDER_UC_HEADER,
        fallback=7 if legacy_layout else None,
    )
    rules: dict[str, dict[str, str | Decimal]] = {}
    invalid = 0

    for data_index, row in enumerate(data_rows):
        row_index = data_start_index + data_index
        formatted_row = _row_at(formatted_rows, row_index)
        formula_row = _row_at(formula_rows, row_index)
        rateio_month = _normalize_month_reference_any(_row_value_by_index(row, month_index))
        base_text = _config_numeric_cell_text(
            row,
            formatted_row,
            formula_row,
            base_index,
        )
        base_coef = _to_decimal(base_text)
        if not rateio_month or base_coef is None:
            continue

        if base_coef < 0 or base_coef > _MAX_SPECIAL_BASE_COEFFICIENT:
            invalid += 1
            continue

        remainder_favorecido = _row_value_by_index(row, remainder_favorecido_index)
        remainder_uc = _row_value_by_index(row, remainder_uc_index)
        if not remainder_favorecido:
            remainder_favorecido = str(defaults.get("remainder_favorecido", "")).strip()
        if not remainder_uc:
            remainder_uc = str(defaults.get("remainder_uc", "")).strip()
        if not remainder_favorecido and not remainder_uc:
            invalid += 1
            continue

        supported_favorecido = _resolve_supported_favorecido(remainder_favorecido)
        rules[rateio_month] = {
            "base_coef": _quantize_coef_8(base_coef),
            "remainder_favorecido": supported_favorecido or remainder_favorecido,
            "remainder_uc": remainder_uc,
        }

    if invalid:
        logger.warning(
            "Configuracao (%s / %s): %d regras de alocacao especial ignoradas.",
            spreadsheet_id,
            favorecido,
            invalid,
        )
    if rules:
        logger.info(
            "Configuracao (%s / %s): %d regras de alocacao especial carregadas.",
            spreadsheet_id,
            favorecido,
            len(rules),
        )
    return rules


def _load_contingency_coefficient_by_month(
    spreadsheet_id: str,
    favorecido: str,
) -> dict[str, Decimal]:
    distributor = _distributor_for_rateio_spreadsheet(spreadsheet_id)
    fixed_coefficients = dict(
        _FIXED_CONTINGENCY_COEFFICIENTS_BY_MONTH.get((distributor, favorecido), {})
    )
    if (distributor, favorecido) not in _CONFIGURABLE_CONTINGENCY_COEFFICIENTS:
        return fixed_coefficients

    try:
        ws = get_worksheet(
            _RATEIO_CONFIGURATION_TAB,
            spreadsheet_id=spreadsheet_id,
            create_if_missing=False,
        )
    except RuntimeError as exc:
        if "nao encontrada" not in str(exc).lower():
            raise
        return fixed_coefficients

    rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
    )
    formatted_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    formula_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMULA",
    )
    if not rows:
        return fixed_coefficients

    header_indexes, data_rows, data_start_index = _configuration_header_and_data_rows(rows)
    month_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_MONTH_HEADER,
        fallback=0,
    )
    coef_index = _configuration_index(
        header_indexes,
        _configuration_contingency_header(favorecido),
    )
    if coef_index is None:
        return fixed_coefficients

    result: dict[str, Decimal] = fixed_coefficients
    invalid = 0
    for data_index, row in enumerate(data_rows):
        row_index = data_start_index + data_index
        formatted_row = _row_at(formatted_rows, row_index)
        formula_row = _row_at(formula_rows, row_index)
        rateio_month = _normalize_month_reference_any(_row_value_by_index(row, month_index))
        coef_text = _config_numeric_cell_text(
            row,
            formatted_row,
            formula_row,
            coef_index,
        )
        coef = _to_decimal(coef_text)
        if not rateio_month or coef is None:
            continue
        if coef < 0 or coef > _MAX_SPECIAL_BASE_COEFFICIENT:
            invalid += 1
            continue
        result[rateio_month] = _quantize_coef_8(coef)

    if invalid:
        logger.warning(
            "Configuracao (%s / %s): %d coeficientes de contingencia invalidos ignorados.",
            spreadsheet_id,
            favorecido,
            invalid,
        )
    if result:
        logger.info(
            "Configuracao (%s / %s): %d coeficientes de contingencia carregados.",
            spreadsheet_id,
            favorecido,
            len(result),
        )
    return result


def _load_configured_coefficient_by_month(
    spreadsheet_id: str,
    favorecido: str,
) -> dict[str, Decimal]:
    try:
        ws = get_worksheet(
            _RATEIO_CONFIGURATION_TAB,
            spreadsheet_id=spreadsheet_id,
            create_if_missing=False,
        )
    except RuntimeError as exc:
        if "nao encontrada" not in str(exc).lower():
            raise
        return {}

    rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
    )
    formatted_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    formula_rows = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMULA",
    )
    if not rows:
        return {}

    header_indexes, data_rows, data_start_index = _configuration_header_and_data_rows(rows)
    month_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_MONTH_HEADER,
        fallback=0,
    )
    coef_index = _configuration_index(
        header_indexes,
        _configuration_coefficient_header(favorecido),
    )
    if coef_index is None:
        return {}

    result: dict[str, Decimal] = {}
    invalid = 0
    for data_index, row in enumerate(data_rows):
        row_index = data_start_index + data_index
        formatted_row = _row_at(formatted_rows, row_index)
        formula_row = _row_at(formula_rows, row_index)
        rateio_month = _normalize_month_reference_any(_row_value_by_index(row, month_index))
        coef_text = _config_numeric_cell_text(
            row,
            formatted_row,
            formula_row,
            coef_index,
        )
        coef = _to_decimal(coef_text)
        if not rateio_month or coef is None:
            continue
        if coef < 0 or coef > _MAX_SPECIAL_BASE_COEFFICIENT:
            invalid += 1
            continue
        result[rateio_month] = _quantize_coef_8(coef)

    if invalid:
        logger.warning(
            "Configuracao (%s / %s): %d coeficientes manuais invalidos ignorados.",
            spreadsheet_id,
            favorecido,
            invalid,
        )
    if result:
        logger.info(
            "Configuracao (%s / %s): %d coeficientes manuais carregados.",
            spreadsheet_id,
            favorecido,
            len(result),
        )
    return result


def _configuration_coefficient_formula(
    tab_name: str,
    *,
    config_row: int,
    source_last_row: int,
    exclude_uc_cell: str = "",
) -> str:
    escaped_tab = tab_name.replace("'", "''")
    source_last_row = max(int(source_last_row), DATA_START_ROW)
    month_range = f"'{escaped_tab}'!$A$2:$A${source_last_row}"
    status_range = f"'{escaped_tab}'!$B$2:$B${source_last_row}"
    uc_range = f"'{escaped_tab}'!$D$2:$D${source_last_row}"
    coefficient_range = f"'{escaped_tab}'!$L$2:$L${source_last_row}"
    month_cell = f"$A{config_row}"
    extra_criteria = ""
    if exclude_uc_cell:
        extra_criteria = f';{uc_range};"<>"&{exclude_uc_cell}'
    return (
        f'=IF(COUNTIFS({month_range};{month_cell};{status_range};"<>*Conting*"{extra_criteria})=0;'
        f'"";MAXIFS({coefficient_range};{month_range};{month_cell};'
        f'{status_range};"<>*Conting*"{extra_criteria}))'
    )


def _is_formula_cell(value: object) -> bool:
    return isinstance(value, str) and value.lstrip().startswith("=")


def _sanitize_special_base_coefficient(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    parsed = _to_decimal(text)
    if parsed is None:
        return ""
    if parsed < 0 or parsed > _MAX_SPECIAL_BASE_COEFFICIENT:
        return ""
    return text


def _sync_rateio_configuration_layout(
    spreadsheet_id: str,
    *,
    current_month: int,
    distributor: str | None = None,
) -> int:
    """
    Organiza o range gerenciado da Configuracao e preserva dados manuais.

    A lista de meses acompanha a mesma janela do rateio. Colunas e linhas fora
    do range gerenciado nao sao limpas, pois a aba pode conter controles manuais.
    """
    ws = get_worksheet(
        _RATEIO_CONFIGURATION_TAB,
        spreadsheet_id=spreadsheet_id,
        create_if_missing=False,
    )
    distributor_name = distributor or _distributor_for_rateio_spreadsheet(spreadsheet_id)
    headers = _configuration_headers_for_distributor(distributor_name)
    last_col_letter = _column_letter(len(headers) - 1)

    existing = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
    )
    existing_formatted = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    existing_formulas = _values_get(
        ws,
        f"A1:Z{ws.row_count}",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMULA",
    )
    stats.sheets_read_requests += 3

    header_indexes, existing_data_rows, data_start_index = _configuration_header_and_data_rows(
        existing
    )
    legacy_layout = not header_indexes
    month_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_MONTH_HEADER,
        fallback=0,
    )
    status_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_STATUS_HEADER,
        fallback=4,
    )
    legacy_status_index = 2
    special_base_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER,
        fallback=5 if legacy_layout else None,
    )
    remainder_favorecido_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER,
        fallback=6 if legacy_layout else None,
    )
    remainder_uc_index = _configuration_index(
        header_indexes,
        _RATEIO_CONFIGURATION_REMAINDER_UC_HEADER,
        fallback=7 if legacy_layout else None,
    )
    contingency_indexes = {
        favorecido: _configuration_index(
            header_indexes,
            _configuration_contingency_header(favorecido),
        )
        for configured_distributor, favorecido in _CONFIGURABLE_CONTINGENCY_COEFFICIENTS
        if configured_distributor == distributor_name
    }
    coefficient_indexes = {
        favorecido: _configuration_index(
            header_indexes,
            _configuration_coefficient_header(favorecido),
        )
        for favorecido in _RATEIO_CONFIGURATION_FAVORECIDOS
    }

    status_by_month: dict[str, str] = {}
    coefficient_by_month: dict[tuple[str, str], str] = {}
    special_by_month: dict[str, tuple[str, str, str]] = {}
    contingency_by_month: dict[tuple[str, str], str] = {}
    for data_index, row in enumerate(existing_data_rows):
        month_ref = _normalize_month_reference_any(_row_value_by_index(row, month_index))
        if not month_ref:
            continue
        row_index = data_start_index + data_index
        formatted_row = _row_at(existing_formatted, row_index)
        formula_row = existing_formulas[row_index] if row_index < len(existing_formulas) else []
        status_value = _row_value_by_index(row, status_index)
        legacy_status = _row_value_by_index(row, legacy_status_index)
        if not status_value and _normalize_status_key(legacy_status) in {
            "aberto",
            _RATEIO_CONFIGURATION_CLOSED_STATUS,
        }:
            status_value = legacy_status
        status_by_month[month_ref] = status_value
        for favorecido, coef_index in coefficient_indexes.items():
            if coef_index is None:
                continue
            coef_value = _config_numeric_cell_text(
                row,
                formatted_row,
                formula_row,
                coef_index,
            )
            sanitized_coef = _sanitize_special_base_coefficient(coef_value)
            if coef_value and not sanitized_coef:
                logger.warning(
                    (
                        "Configuracao (%s): coeficiente manual suspeito '%s' "
                        "ignorado no mes %s."
                    ),
                    spreadsheet_id,
                    coef_value,
                    month_ref,
                )
            if sanitized_coef:
                coefficient_by_month[(month_ref, favorecido)] = sanitized_coef

        special_coef = _config_numeric_cell_text(
            row,
            formatted_row,
            formula_row,
            special_base_index,
        )
        sanitized_coef = _sanitize_special_base_coefficient(special_coef)
        if special_coef and not sanitized_coef:
            logger.warning(
                (
                    "Configuracao (%s): coeficiente base especial suspeito '%s' "
                    "ignorado no mes %s."
                ),
                spreadsheet_id,
                special_coef,
                month_ref,
            )
        special_coef = sanitized_coef
        special_by_month[month_ref] = (
            special_coef,
            _row_value_by_index(row, remainder_favorecido_index),
            _row_value_by_index(row, remainder_uc_index),
        )
        for favorecido, coef_index in contingency_indexes.items():
            if coef_index is None:
                continue
            coef_value = _config_numeric_cell_text(
                row,
                formatted_row,
                formula_row,
                coef_index,
            )
            sanitized_coef = _sanitize_special_base_coefficient(coef_value)
            if coef_value and not sanitized_coef:
                logger.warning(
                    (
                        "Configuracao (%s): coeficiente de contingencia suspeito '%s' "
                        "ignorado no mes %s."
                    ),
                    spreadsheet_id,
                    coef_value,
                    month_ref,
                )
            if sanitized_coef:
                contingency_by_month[(month_ref, favorecido)] = sanitized_coef

    meta = _get_spreadsheet_meta(spreadsheet_id=spreadsheet_id)
    stats.sheets_read_requests += 1
    row_count_by_tab: dict[str, int] = {}
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        title = str(props.get("title") or "")
        grid = props.get("gridProperties") or {}
        row_count_by_tab[title] = int(grid.get("rowCount", 1000))

    rateio_months = [
        format_reference_month(_add_months(month, 1))
        for month in _reference_month_window(current_month)
    ]
    enabled_favorecidos = set(_rateio_favorecidos_for_distributor(distributor_name))
    special_defaults = _special_allocation_defaults_for_distributor(distributor_name) or {}
    target_rows: list[list] = []
    for offset, rateio_month in enumerate(rateio_months, start=2):
        special_coef, special_favorecido, special_uc = special_by_month.get(
            rateio_month,
            ("", "", ""),
        )
        if not special_favorecido:
            special_favorecido = str(special_defaults.get("remainder_favorecido", "")).strip()
        if not special_uc:
            special_uc = str(special_defaults.get("remainder_uc", "")).strip()
        values_by_header = {
            _RATEIO_CONFIGURATION_MONTH_HEADER: rateio_month,
            _RATEIO_CONFIGURATION_STATUS_HEADER: status_by_month.get(rateio_month, "Aberto"),
        }
        if _RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER in headers:
            values_by_header[_RATEIO_CONFIGURATION_SPECIAL_BASE_HEADER] = special_coef
        if _RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER in headers:
            values_by_header[
                _RATEIO_CONFIGURATION_REMAINDER_FAVORECIDO_HEADER
            ] = special_favorecido
        if _RATEIO_CONFIGURATION_REMAINDER_UC_HEADER in headers:
            values_by_header[_RATEIO_CONFIGURATION_REMAINDER_UC_HEADER] = special_uc

        remainder_uc_header_index = (
            headers.index(_RATEIO_CONFIGURATION_REMAINDER_UC_HEADER)
            if _RATEIO_CONFIGURATION_REMAINDER_UC_HEADER in headers
            else None
        )
        for favorecido in _RATEIO_CONFIGURATION_FAVORECIDOS:
            if enabled_favorecidos and favorecido not in enabled_favorecidos:
                continue
            favorecido_special_defaults = _special_allocation_defaults(
                distributor_name,
                favorecido,
            ) or {}
            exclude_uc_cell = (
                f"${_column_letter(remainder_uc_header_index)}{offset}"
                if (
                    remainder_uc_header_index is not None
                    and special_uc
                    and str(favorecido_special_defaults.get("remainder_uc") or "").strip()
                )
                else ""
            )
            tab_name = RATEIO_FAVORECIDO_TABS[favorecido]
            coefficient_header = _configuration_coefficient_header(favorecido)
            manual_coefficient = coefficient_by_month.get((rateio_month, favorecido))
            values_by_header[coefficient_header] = (
                manual_coefficient
                if manual_coefficient
                else _configuration_coefficient_formula(
                    tab_name,
                    config_row=offset,
                    source_last_row=row_count_by_tab.get(tab_name, 1000),
                    exclude_uc_cell=exclude_uc_cell,
                )
            )

        for configured_distributor, favorecido in _CONFIGURABLE_CONTINGENCY_COEFFICIENTS:
            if configured_distributor != distributor_name:
                continue
            header = _configuration_contingency_header(favorecido)
            if header in headers:
                default_contingency_coef = _format_decimal_plain(
                    _default_contingency_coefficient(distributor_name, favorecido)
                )
                values_by_header[header] = contingency_by_month.get(
                    (rateio_month, favorecido),
                    default_contingency_coef,
                )

        target_rows.append([values_by_header.get(header, "") for header in headers])

    _values_update(
        ws,
        f"A1:{last_col_letter}1",
        [headers],
        spreadsheet_id=spreadsheet_id,
    )
    stats.sheets_write_requests += 1
    stats.sheets_cells_written += len(headers)

    if target_rows:
        last_row = DATA_START_ROW + len(target_rows) - 1
        _values_update(
            ws,
            f"A2:A{last_row}",
            [[row[0]] for row in target_rows],
            spreadsheet_id=spreadsheet_id,
            value_input_option="USER_ENTERED",
        )
        stats.sheets_write_requests += 1
        stats.sheets_cells_written += len(target_rows)

        formula_headers = {
            _configuration_coefficient_header(favorecido)
            for favorecido in _RATEIO_CONFIGURATION_FAVORECIDOS
        }
        for col_index, header in enumerate(headers):
            if header not in formula_headers:
                continue
            col_letter = _column_letter(col_index)
            for formula_pass in (True, False):
                chunk_start: int | None = None
                chunk_values: list[list[str]] = []

                def flush_coefficient_chunk() -> None:
                    nonlocal chunk_start, chunk_values
                    if chunk_start is None or not chunk_values:
                        return
                    chunk_end = chunk_start + len(chunk_values) - 1
                    kwargs = {
                        "spreadsheet_id": spreadsheet_id,
                    }
                    if formula_pass:
                        kwargs["value_input_option"] = "USER_ENTERED"
                    _values_update(
                        ws,
                        f"{col_letter}{chunk_start}:{col_letter}{chunk_end}",
                        chunk_values,
                        **kwargs,
                    )
                    stats.sheets_write_requests += 1
                    stats.sheets_cells_written += len(chunk_values)
                    chunk_start = None
                    chunk_values = []

                for sheet_row, row in enumerate(target_rows, start=DATA_START_ROW):
                    value = row[col_index]
                    value_text = str(value if value is not None else "").strip()
                    if not value_text or _is_formula_cell(value_text) != formula_pass:
                        flush_coefficient_chunk()
                        continue
                    if chunk_start is None:
                        chunk_start = sheet_row
                    chunk_values.append([value])
                flush_coefficient_chunk()

        for col_index, header in enumerate(headers):
            if col_index == 0 or header in formula_headers:
                continue
            col_letter = _column_letter(col_index)
            chunk_start: int | None = None
            chunk_values: list[list[str]] = []

            def flush_manual_chunk() -> None:
                nonlocal chunk_start, chunk_values
                if chunk_start is None or not chunk_values:
                    return
                chunk_end = chunk_start + len(chunk_values) - 1
                _values_update(
                    ws,
                    f"{col_letter}{chunk_start}:{col_letter}{chunk_end}",
                    chunk_values,
                    spreadsheet_id=spreadsheet_id,
                )
                stats.sheets_write_requests += 1
                stats.sheets_cells_written += len(chunk_values)
                chunk_start = None
                chunk_values = []

            for sheet_row, row in enumerate(target_rows, start=DATA_START_ROW):
                value = row[col_index]
                if not str(value if value is not None else "").strip():
                    flush_manual_chunk()
                    continue
                if chunk_start is None:
                    chunk_start = sheet_row
                chunk_values.append([value])
            flush_manual_chunk()

    logger.info(
        "Configuracao (%s): layout A:%s sincronizado para %d meses.",
        spreadsheet_id,
        last_col_letter,
        len(target_rows),
    )
    return len(target_rows)


def _previous_month_reference(month_ref: str) -> str:
    month_int = _month_ref_to_int(month_ref)
    if month_int is None:
        return ""
    return format_reference_month(_add_months(month_int, -1))


def _invoice_issue_day(value) -> int | None:
    parsed = _to_decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    day = int(parsed)
    return day if 1 <= day <= 31 else None


def _history_invoice_issue_day_threshold_for_spreadsheet(spreadsheet_id: str) -> int:
    ame_spreadsheet_id, _ = resolve_rateio_sheet_target("AmE")
    if str(spreadsheet_id or "").strip() == ame_spreadsheet_id:
        return _AME_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD
    return _DEFAULT_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD


def _history_month_for_last_rateio(
    *,
    rateio_month: str,
    month_ref: str,
    invoice_issue_day,
    invoice_issue_day_threshold: int = _DEFAULT_HISTORY_INVOICE_ISSUE_DAY_THRESHOLD,
) -> str:
    threshold = int(invoice_issue_day_threshold)
    rateio_month_int = _month_ref_to_int(rateio_month)
    if rateio_month_int is not None:
        offset = -2 if (_invoice_issue_day(invoice_issue_day) or 32) <= threshold else -1
        return format_reference_month(_add_months(rateio_month_int, offset))

    month_ref_norm = _normalize_month_reference_any(month_ref)
    if (_invoice_issue_day(invoice_issue_day) or 32) <= threshold:
        return _previous_month_reference(month_ref_norm)
    return month_ref_norm


def _build_last_rateio_index_from_rows(
    rows: list[list],
    *,
    uc_col: int,
    month_col: int,
    value_col: int,
    source_label: str,
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    skipped = 0

    for row in rows:
        month_ref = _normalize_month_reference_any(
            row[month_col] if len(row) > month_col else ""
        )
        value = _round_projection_value(row[value_col] if len(row) > value_col else "")
        uc_key = _normalize_uc_key(row[uc_col] if len(row) > uc_col else "")
        if not uc_key or not month_ref or not str(value).strip():
            skipped += 1
            continue
        result[(uc_key, month_ref)] = str(value).strip()

    logger.info(
        "%s: %d pares UC+Mes carregados (%d linhas ignoradas).",
        source_label,
        len(result),
        skipped,
    )
    return result


def _build_distributor_last_rateio_index(
    distributor: str,
) -> dict[tuple[str, str], str]:
    distributor_name = str(distributor or "").strip()
    spreadsheet_id, _tab_name = resolve_rateio_sheet_target(distributor_name)
    if not spreadsheet_id:
        return {}

    is_copel = distributor_name == "COPEL"
    source_tab = _RATEIO_COPEL_HISTORY_TAB if is_copel else _RATEIO_HISTORY_TAB
    ws = get_worksheet(
        source_tab,
        spreadsheet_id=spreadsheet_id,
        create_if_missing=False,
    )

    # COPEL: D=UC, H=alteracao, I=historico.
    # Demais distribuidoras: C=UC, H=alteracao, J=historico.
    source_range = "D2:I" if is_copel else "C2:J"
    rows = _values_get(
        ws,
        source_range,
        spreadsheet_id=spreadsheet_id,
    )
    return _build_last_rateio_index_from_rows(
        rows,
        uc_col=0,
        month_col=4 if is_copel else 5,
        value_col=5 if is_copel else 7,
        source_label=f"Historico de Rateio {distributor_name}",
    )


def _build_copel_last_rateio_index() -> dict[tuple[str, str], str]:
    return _build_distributor_last_rateio_index("COPEL")


def _clickup_custom_field_to_text(task: dict, field_id: str) -> str:
    for field in task.get("custom_fields", []) or []:
        if str(field.get("id") or "") != field_id:
            continue
        value = field.get("value")
        if value is None:
            return ""
        if isinstance(value, dict):
            for key in ("value", "name", "label", "id"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate).strip()
            return ""
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    part = ""
                    for key in ("value", "name", "label", "id"):
                        candidate = item.get(key)
                        if candidate not in (None, ""):
                            part = str(candidate).strip()
                            break
                    if part:
                        parts.append(part)
                elif item not in (None, ""):
                    parts.append(str(item).strip())
            return ", ".join(part for part in parts if part)
        return _resolve_dropdown_value(field_id, value, field).strip()
    return ""


def _slim_task_with_formulario_copel_fields(task: dict) -> dict:
    slim = slim_task(task)
    existing_cf_ids = {
        str(field.get("id") or "")
        for field in slim.get("custom_fields", []) or []
    }
    extra_fields = []
    for field in task.get("custom_fields", []) or []:
        field_id = str(field.get("id") or "")
        if field_id in {
            _FORMULARIO_COPEL_CPF_CNPJ_CF_ID,
            _FORMULARIO_COPEL_COL_H_CF_ID,
            _FORMULARIO_RAZAO_SOCIAL_CF_ID,
            *_FORMULARIO_ADDRESS_CF_IDS.values(),
        } and field_id not in existing_cf_ids:
            extra_fields.append(field)

    if extra_fields:
        slim["custom_fields"] = list(slim.get("custom_fields", []) or []) + extra_fields
    return slim


def _formulario_copel_project_name_base(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    text = re.sub(r"\s*[-–—]\s*UC\s*[A-Za-z0-9./\-\s]+$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _formulario_copel_project_key(value) -> str:
    return _normalize_text(_formulario_copel_project_name_base(value))


def _fetch_clickup_tasks_from_lists(
    list_ids: tuple[str, ...],
    *,
    transform: Callable[[dict], dict] | None = slim_task,
) -> list[dict]:
    tasks: list[dict] = []
    seen_task_ids: set[str] = set()
    for list_id in list_ids:
        list_tasks = fetch_tasks(
            list_id,
            include_closed=True,
            transform=transform,
        )
        for task in list_tasks:
            task_id = str(task.get("id") or "").strip()
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            tasks.append(task)
    return tasks


def _build_formulario_copel_field_by_uc(tasks: list[dict], field_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for task in sorted(_prioritize_tasks_by_uc(tasks), key=_task_priority_key):
        uc = extract_task_uc(task)
        uc_key = _normalize_uc_key(uc)
        if uc_key:
            result[uc_key] = _clickup_custom_field_to_text(task, field_id)
    return result


def _build_formulario_copel_cpf_cnpj_by_uc(tasks: list[dict]) -> dict[str, str]:
    return _build_formulario_copel_field_by_uc(
        tasks,
        _FORMULARIO_COPEL_CPF_CNPJ_CF_ID,
    )


def _build_formulario_address(task: dict) -> str:
    values = {
        field: re.sub(
            r"\s+",
            " ",
            _clickup_custom_field_to_text(task, field_id),
        ).strip()
        for field, field_id in _FORMULARIO_ADDRESS_CF_IDS.items()
    }

    address = ", ".join(
        value
        for value in (
            values["rua"],
            values["numero"],
            values["complemento"],
        )
        if value
    )
    city_state = "-".join(
        value
        for value in (values["cidade"], values["estado"])
        if value
    )
    if city_state:
        location = " - ".join(
            value for value in (values["bairro"], city_state) if value
        )
        address = f"{address} - {location}" if address else location
    elif values["bairro"]:
        address = f"{address} - {values['bairro']}" if address else values["bairro"]
    if values["cep"]:
        address = f"{address} {values['cep']}" if address else values["cep"]
    return address


def _build_formulario_address_by_uc(tasks: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for task in sorted(_prioritize_tasks_by_uc(tasks), key=_task_priority_key):
        uc_key = _normalize_uc_key(extract_task_uc(task))
        if uc_key:
            result[uc_key] = _build_formulario_address(task)
    return result


def _build_formulario_project_values_by_project_name(
    tasks: list[dict],
) -> dict[str, tuple[str, str]]:
    selected: dict[str, tuple[int, str, str]] = {}

    for task in tasks:
        project_key = _formulario_copel_project_key(task.get("name", ""))
        uc = extract_task_uc(task)
        uc_aneel = extract_task_uc_aneel(task)
        if not project_key or (not uc and not uc_aneel):
            continue
        try:
            updated = int(str(task.get("date_updated") or "0"))
        except (TypeError, ValueError):
            updated = 0

        current = selected.get(project_key)
        if current is None or updated > current[0]:
            selected[project_key] = (updated, uc, uc_aneel)

    return {
        project_key: (uc, uc_aneel)
        for project_key, (_updated, uc, uc_aneel) in selected.items()
    }


def _build_formulario_copel_uc_by_project_name(tasks: list[dict]) -> dict[str, str]:
    return {
        project_key: uc
        for project_key, (uc, _uc_aneel) in _build_formulario_project_values_by_project_name(tasks).items()
    }


def _build_formulario_uc_aneel_by_project_name(tasks: list[dict]) -> dict[str, str]:
    return {
        project_key: uc_aneel
        for project_key, (_uc, uc_aneel) in _build_formulario_project_values_by_project_name(tasks).items()
    }


def _format_formulario_copel_month(value) -> str:
    normalized = _normalize_month_reference_any(value)
    parts = normalized.split("-")
    if len(parts) == 3:
        day, month, year = parts
        if day.isdigit() and month.isdigit() and year.isdigit():
            return f"{day.zfill(2)}/{month.zfill(2)}/{year.zfill(4)}"
    return normalized


def _formulario_rateio_column_indexes(headers: list) -> dict[str, int]:
    normalized_headers: dict[str, int] = {}
    for index, header in enumerate(headers):
        raw_header = str(header or "").strip()
        normalized = _normalize_text(raw_header)
        if raw_header == "%":
            normalized = "%"
        if normalized and normalized not in normalized_headers:
            normalized_headers[normalized] = index

    indexes: dict[str, int] = {}
    missing: list[str] = []
    for field, aliases in _FORMULARIO_RATEIO_HEADER_ALIASES.items():
        index = next(
            (normalized_headers[alias] for alias in aliases if alias in normalized_headers),
            None,
        )
        if index is None:
            missing.append(field)
        else:
            indexes[field] = index

    if missing:
        raise RuntimeError(
            "Formulario: cabecalhos obrigatorios ausentes na aba Rateio: "
            + ", ".join(missing)
        )
    return indexes


def _build_formulario_copel_rows(
    rateio_rows: list[list],
    cpf_cnpj_by_uc: dict[str, str],
    uc_by_project_name: dict[str, str],
    uc_aneel_by_project_name: dict[str, str] | None = None,
    col_h_by_uc: dict[str, str] | None = None,
    razao_social_by_uc: dict[str, str] | None = None,
    address_by_uc: dict[str, str] | None = None,
    rateio_headers: list | None = None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    uc_aneel_by_project_name = uc_aneel_by_project_name or {}
    col_h_by_uc = col_h_by_uc or {}
    razao_social_by_uc = razao_social_by_uc or {}
    address_by_uc = address_by_uc or {}
    column_indexes = (
        _formulario_rateio_column_indexes(rateio_headers)
        if rateio_headers is not None
        else {
            "usina": 0,
            "razao_social": 2,
            "uc": 3,
            "nova_uc": 4,
            "percentual": 6,
            "alteracao": 7,
        }
    )

    def _source_value(row: list, field: str):
        index = column_indexes[field]
        return row[index] if len(row) > index else ""

    for source_row in rateio_rows:
        row = list(source_row or [])
        source_usina = _source_value(row, "usina")
        source_razao_social = _source_value(row, "razao_social")
        source_uc = _source_value(row, "uc")
        source_uc_aneel = _source_value(row, "nova_uc")
        source_percentual = _source_value(row, "percentual")
        source_alteracao = _format_formulario_copel_month(_source_value(row, "alteracao"))

        mapped_values = [
            source_razao_social,
            source_uc,
            source_percentual,
            source_usina,
            source_alteracao,
            source_uc_aneel,
        ]
        if not any(str(value or "").strip() for value in mapped_values):
            continue

        uc_key = _normalize_uc_key(source_uc)
        project_key = _formulario_copel_project_key(source_usina)
        razao_social = razao_social_by_uc.get(uc_key, "") or source_razao_social
        output_row = [
            razao_social,
            cpf_cnpj_by_uc.get(uc_key, ""),
            source_uc,
            source_percentual,
            uc_by_project_name.get(project_key, ""),
            source_usina,
            source_alteracao,
            col_h_by_uc.get(uc_key, "") or source_uc_aneel,
            address_by_uc.get(uc_key, ""),
            uc_aneel_by_project_name.get(project_key, ""),
        ]
        rows.append(output_row)

    return rows


def _load_formulario_clickup_indexes() -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    rateio_tasks = fetch_all_tasks(
        include_closed=True,
        transform=_slim_task_with_formulario_copel_fields,
    )
    project_tasks = _fetch_clickup_tasks_from_lists(_FORMULARIO_COPEL_PROJECT_LIST_IDS)
    return (
        _build_formulario_copel_cpf_cnpj_by_uc(rateio_tasks),
        _build_formulario_copel_uc_by_project_name(project_tasks),
        _build_formulario_uc_aneel_by_project_name(project_tasks),
        _build_formulario_copel_field_by_uc(rateio_tasks, _FORMULARIO_COPEL_COL_H_CF_ID),
        _build_formulario_copel_field_by_uc(rateio_tasks, _FORMULARIO_RAZAO_SOCIAL_CF_ID),
        _build_formulario_address_by_uc(rateio_tasks),
    )


def _sync_formulario_for_distributor(
    distributor: str,
    *,
    cpf_cnpj_by_uc: dict[str, str],
    uc_by_project_name: dict[str, str],
    uc_aneel_by_project_name: dict[str, str],
    col_h_by_uc: dict[str, str],
    razao_social_by_uc: dict[str, str],
    address_by_uc: dict[str, str],
) -> int:
    target_tab = _FORMULARIO_TABS_BY_DISTRIBUTOR.get(distributor)
    if not target_tab:
        raise ValueError(f"Distribuidora sem aba de formulario configurada: {distributor!r}")

    spreadsheet_id, _tab_name = resolve_rateio_sheet_target(distributor)
    if not spreadsheet_id:
        return 0

    source_tab = _RATEIO_COPEL_HISTORY_TAB if distributor == "COPEL" else _RATEIO_HISTORY_TAB
    source_ws = get_worksheet(
        source_tab,
        spreadsheet_id=spreadsheet_id,
        create_if_missing=False,
    )
    target_ws = get_worksheet(
        target_tab,
        spreadsheet_id=spreadsheet_id,
        create_if_missing=False,
    )

    render_option = "UNFORMATTED_VALUE" if distributor == "COPEL" else "FORMATTED_VALUE"
    rateio_data = _values_get(
        source_ws,
        "A1:K",
        spreadsheet_id=spreadsheet_id,
        value_render_option=render_option,
    )
    rateio_headers = rateio_data[0] if rateio_data else []
    rateio_rows = rateio_data[1:] if len(rateio_data) > 1 else []
    rows = _build_formulario_copel_rows(
        rateio_rows,
        cpf_cnpj_by_uc,
        uc_by_project_name,
        uc_aneel_by_project_name,
        col_h_by_uc,
        razao_social_by_uc=razao_social_by_uc,
        address_by_uc=address_by_uc,
        rateio_headers=rateio_headers,
    )
    write_col_count = 10
    current_header = _values_get(
        target_ws,
        "I1",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    current_header_value = str(
        current_header[0][0]
        if current_header and current_header[0]
        else ""
    ).strip()
    if current_header_value != _FORMULARIO_ADDRESS_HEADER:
        _values_update(
            target_ws,
            "I1",
            [[_FORMULARIO_ADDRESS_HEADER]],
            spreadsheet_id=spreadsheet_id,
        )
    current_uc_aneel_header = _values_get(
        target_ws,
        "J1",
        spreadsheet_id=spreadsheet_id,
        value_render_option="FORMATTED_VALUE",
    )
    current_uc_aneel_header_value = str(
        current_uc_aneel_header[0][0]
        if current_uc_aneel_header and current_uc_aneel_header[0]
        else ""
    ).strip()
    if current_uc_aneel_header_value != _FORMULARIO_UC_ANEEL_HEADER:
        _values_update(
            target_ws,
            "J1",
            [[_FORMULARIO_UC_ANEEL_HEADER]],
            spreadsheet_id=spreadsheet_id,
        )
    changed_rows = sync_rows_in_place(
        target_ws,
        rows,
        col_count=write_col_count,
        spreadsheet_id=spreadsheet_id,
    )
    logger.info(
        "%s: reconstruido com %d linhas (%d linhas alteradas).",
        target_tab,
        len(rows),
        changed_rows,
    )
    return changed_rows


def _sync_formulario_copel() -> int:
    (
        cpf_cnpj_by_uc,
        uc_by_project_name,
        uc_aneel_by_project_name,
        col_h_by_uc,
        razao_social_by_uc,
        address_by_uc,
    ) = _load_formulario_clickup_indexes()
    return _sync_formulario_for_distributor(
        "COPEL",
        cpf_cnpj_by_uc=cpf_cnpj_by_uc,
        uc_by_project_name=uc_by_project_name,
        uc_aneel_by_project_name=uc_aneel_by_project_name,
        col_h_by_uc=col_h_by_uc,
        razao_social_by_uc=razao_social_by_uc,
        address_by_uc=address_by_uc,
    )


def _sync_all_formularios() -> int:
    (
        cpf_cnpj_by_uc,
        uc_by_project_name,
        uc_aneel_by_project_name,
        col_h_by_uc,
        razao_social_by_uc,
        address_by_uc,
    ) = _load_formulario_clickup_indexes()
    changed_rows = 0
    for distributor, target_tab in _FORMULARIO_TABS_BY_DISTRIBUTOR.items():
        changed_rows += _run_sheets_step_with_retry(
            target_tab,
            lambda distributor_name=distributor: _sync_formulario_for_distributor(
                distributor_name,
                cpf_cnpj_by_uc=cpf_cnpj_by_uc,
                uc_by_project_name=uc_by_project_name,
                uc_aneel_by_project_name=uc_aneel_by_project_name,
                col_h_by_uc=col_h_by_uc,
                razao_social_by_uc=razao_social_by_uc,
                address_by_uc=address_by_uc,
            ),
        )
    return changed_rows


def _last_rateio_index_for_distributor(
    distributor: str,
    default_index: dict[tuple[str, str], str],
    by_distributor: dict[str, dict[tuple[str, str], str]] | None = None,
) -> dict[tuple[str, str], str]:
    distributor_index = (by_distributor or {}).get(str(distributor or "").strip())
    return distributor_index if distributor_index is not None else default_index


def _resolve_target_tab_from_distributor(distributor_name) -> str | None:
    normalized = _normalize_text(distributor_name)
    if not normalized:
        return None

    for tab_name, aliases in _DISTRIBUTOR_TARGET_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalize_text(alias)
            if not alias_norm:
                continue
            if normalized == alias_norm or alias_norm in normalized:
                return tab_name

    return None


def _get_worksheet_with_aliases(
    spreadsheet_id: str,
    primary_tab: str,
    aliases: list[str] | tuple[str, ...] = (),
):
    candidates: list[str] = []
    for candidate in [primary_tab, *aliases]:
        name = str(candidate or "").strip()
        if name and name not in candidates:
            candidates.append(name)

    last_exc: Exception | None = None
    for tab_name in candidates:
        try:
            return get_worksheet(
                tab_name,
                spreadsheet_id=spreadsheet_id,
                create_if_missing=False,
            )
        except requests.RequestException:
            raise
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc:
        raise last_exc

    raise RuntimeError(f"Nenhuma aba candidata foi informada para a planilha {spreadsheet_id}.")


def _run_sheets_step_with_retry(step_label: str, fn: Callable[[], _T]) -> _T:
    attempt = 0
    while True:
        try:
            return fn()
        except requests.RequestException as exc:
            attempt += 1
            if attempt >= _SHEETS_STEP_MAX_RETRIES:
                raise
            wait_s = _SHEETS_STEP_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning(
                (
                    "Sheets timeout/erro de rede em '%s' (tentativa %d/%d): %s. "
                    "Reconectando e retomando em %ds..."
                ),
                step_label,
                attempt,
                _SHEETS_STEP_MAX_RETRIES,
                exc,
                wait_s,
            )
            reset_sheets_client()
            time.sleep(wait_s)


def _build_generation_rows_by_tab() -> dict[str, list[list[str]]]:
    rows_by_tab: dict[str, list[list[str]]] = {tab: [] for tab in TARGET_SHEET_TABS}
    if not PROJECTION_SPREADSHEET_ID.strip():
        return rows_by_tab

    source_ws = _get_worksheet_with_aliases(
        PROJECTION_SPREADSHEET_ID,
        PROJECTION_GENERATION_SHEET_TAB,
        ("ProjeÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o de GeraÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o", "Projecao de Geracao"),
    )
    source_rows = read_all_rows(source_ws, spreadsheet_id=PROJECTION_SPREADSHEET_ID)

    ignored_without_distributor = 0
    ignored_without_favorecido = 0
    ignored_empty = 0

    for row in source_rows:
        if len(row) <= 4:
            continue

        usina = str(row[0] if len(row) > 0 else "").strip()
        status_usina = str(row[1] if len(row) > 1 else "").strip()
        uc = str(row[2] if len(row) > 2 else "").strip()
        distribuidora = str(row[3] if len(row) > 3 else "").strip()
        mes_referencia = _normalize_month_reference(row[4] if len(row) > 4 else "")
        geracao_consolidada = _round_projection_value(row[5] if len(row) > 5 else "")
        geracao_projetada = _round_projection_value(row[6] if len(row) > 6 else "")
        favorecido = _resolve_supported_favorecido(
            row[7] if len(row) > 7 else ""
        )

        if not usina and not uc and not mes_referencia:
            ignored_empty += 1
            continue

        target_tab = _resolve_target_tab_from_distributor(distribuidora)
        if target_tab not in rows_by_tab:
            ignored_without_distributor += 1
            continue
        if favorecido is None:
            enabled_favorecidos = _rateio_favorecidos_for_distributor(target_tab)
            if len(enabled_favorecidos) == 1:
                favorecido = enabled_favorecidos[0]
            else:
                ignored_without_favorecido += 1

        rows_by_tab[target_tab].append(
            [
                usina,
                status_usina,
                uc,
                mes_referencia,
                geracao_projetada,
                geracao_consolidada,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                favorecido or "",
            ]
        )

    for tab_name in rows_by_tab:
        month_totals_proj: dict[tuple[str, str], Decimal] = {}
        month_totals_cons: dict[tuple[str, str], Decimal] = {}
        for row in rows_by_tab[tab_name]:
            month_ref = str(row[3] if len(row) > 3 else "").strip()
            favorecido = str(row[13] if len(row) > 13 else "").strip()
            if not month_ref or favorecido not in RATEIO_FAVORECIDO_TABS:
                continue
            value_e = _to_decimal(row[4] if len(row) > 4 else "")
            if value_e is not None:
                key = (favorecido, month_ref)
                month_totals_proj[key] = (
                    month_totals_proj.get(key, Decimal("0")) + value_e
                )
            value_f = _to_decimal(row[5] if len(row) > 5 else "")
            if value_f is not None:
                key = (favorecido, month_ref)
                month_totals_cons[key] = (
                    month_totals_cons.get(key, Decimal("0")) + value_f
                )

        rows_by_tab[tab_name].sort(
            key=lambda r: (
                _normalize_text(r[0]),
                _normalize_month_reference(r[3]),
                normalize_uc(r[2]),
            )
        )

        emitted_months: set[str] = set()
        for row in rows_by_tab[tab_name]:
            month_ref = str(row[3] if len(row) > 3 else "").strip()
            del row[13]
            for col_index in range(6, 12):
                row[col_index] = ""
            if not month_ref or month_ref in emitted_months:
                row[12] = ""
                continue
            for favorecido, (projected_col, consolidated_col) in _GENERATION_TOTAL_COLUMNS.items():
                row[projected_col] = _round_projection_value(
                    month_totals_proj.get((favorecido, month_ref), Decimal("0"))
                )
                row[consolidated_col] = _round_projection_value(
                    month_totals_cons.get((favorecido, month_ref), Decimal("0"))
                )
            row[12] = month_ref
            emitted_months.add(month_ref)

    if ignored_without_favorecido:
        logger.warning(
            (
                "Projecao de Geracao: %d linhas sem Favorecido suportado "
                "ficaram fora dos totais."
            ),
            ignored_without_favorecido,
        )

    logger.info(
        (
            "ProjeÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o de GeraÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o: %d linhas vÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¡lidas carregadas (%d sem distribuidora mapeada, %d vazias)."
        ),
        sum(len(v) for v in rows_by_tab.values()),
        ignored_without_distributor,
        ignored_empty,
    )
    return rows_by_tab


def _sync_generation_total_tabs() -> None:
    generation_rows_by_tab = _build_generation_rows_by_tab()
    for tab_name in TARGET_SHEET_TABS:
        spreadsheet_id, _ = resolve_rateio_sheet_target(tab_name)
        rows = generation_rows_by_tab.get(tab_name, [])

        def _write_generation_tab():
            ws_local = _get_worksheet_with_aliases(
                spreadsheet_id,
                RATEIO_GENERATION_SHEET_TAB,
                ("GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total", "Geracao Total"),
            )
            ensure_headers(ws_local)
            changed = sync_rows_in_place(
                ws_local,
                rows,
                col_count=13,
                spreadsheet_id=spreadsheet_id,
            )
            return ws_local, changed

        ws, changed_rows = _run_sheets_step_with_retry(
            f"GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total [{tab_name}]",
            _write_generation_tab,
        )
        logger.info(
            "GeraÃƒÆ’Ã‚Â§ÃƒÆ’Ã‚Â£o Total: planilha %s / aba '%s' sincronizada com %d linhas (%d alteradas)",
            spreadsheet_id,
            ws.title,
            len(rows),
            changed_rows,
        )

def _build_projection_index() -> dict[tuple[str, str], str]:
    if not PROJECTION_SPREADSHEET_ID.strip():
        return {}

    ws = _get_worksheet_with_aliases(
        PROJECTION_SPREADSHEET_ID,
        PROJECTION_SHEET_TAB,
        ("ProjeÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o de Consumo", "Projecao de Consumo"),
    )
    rows = read_all_rows(ws, spreadsheet_id=PROJECTION_SPREADSHEET_ID)

    projection: dict[tuple[str, str], str] = {}
    for row in rows:
        if len(row) <= 5:
            continue

        uc = normalize_uc(row[2] if len(row) > 2 else "")
        month_ref = _normalize_month_reference(row[5] if len(row) > 5 else "")
        value_g = _round_projection_value(row[6] if len(row) > 6 else "")
        value_h = _round_projection_value(row[7] if len(row) > 7 else "")
        value = value_g if str(value_g).strip() else value_h

        if not uc or not month_ref:
            continue
        projection[(uc, month_ref)] = value

    logger.info(
        "ProjeÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â£o de Consumo: %d pares UC+MÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Âªs referÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Âªncia carregados da planilha externa.",
        len(projection),
    )
    return projection


def _inject_projection_value(
    uc: str,
    payload: dict[str, str],
    projection_index: dict[tuple[str, str], str],
    *,
    lookup_ucs: list[str] | None = None,
) -> dict[str, str]:
    month_ref = _normalize_month_reference(payload.get("nuMesReferencia", ""))
    proj_value = ""
    candidates = lookup_ucs or [uc]
    for candidate in candidates:
        proj_value = projection_index.get((normalize_uc(candidate), month_ref), "")
        if str(proj_value).strip():
            break
    enriched = dict(payload)
    enriched["projecao_consumo"] = proj_value
    return enriched


def _new_rateio_consumption_for_alteracao_month(
    uc: str,
    rateio_month: str,
    projection_index: dict[tuple[str, str], str] | None,
    fallback: Decimal,
) -> Decimal:
    """Projection used only by Novo Rateio (M): UC + Alteracao Rateio para o mes."""
    month_ref = _normalize_month_reference_any(rateio_month)
    if projection_index and uc and month_ref:
        projected = _to_decimal(projection_index.get((normalize_uc(uc), month_ref), ""))
        if projected is not None:
            return projected
    return fallback


def _has_projection_value(value) -> bool:
    return str(value if value is not None else "").strip() != ""


def _build_powerrev_indexes(
    tasks: list[dict],
) -> tuple[int, list[int], dict[int, dict[str, dict[str, str]]]]:
    current_month = get_current_reference_month()
    months = _reference_month_window(current_month)
    target_ucs = _collect_target_ucs(tasks)

    if not target_ucs:
        return current_month, months, {}

    invoice_by_month: dict[int, dict[str, dict[str, str]]] = {}
    # The earliest displayed month needs the preceding invoice to obtain
    # its opening balance, so fetch one extra historical month.
    normalized_invoice_months: list[int] = []
    month = _add_months(months[0], -1)
    while month <= current_month:
        normalized_invoice_months.append(month)
        month = _add_months(month, 1)

    for month in normalized_invoice_months:
        logger.info(
            "PowerRev: buscando faturas do mes %s para %d UCs.",
            month,
            len(target_ucs),
        )
        invoice_by_month[month] = build_invoice_index_for_ucs(
            target_ucs,
            reference_month=month,
        )
    return current_month, months, invoice_by_month


def _latest_powerrev_invoice_issue_for_month(
    uc: str,
    month: int,
    invoice_by_month: dict[int, dict[str, dict[str, str]]],
    *,
    lookup_ucs: list[str] | None = None,
) -> str:
    candidates = [normalize_uc(candidate) for candidate in (lookup_ucs or [uc]) if normalize_uc(candidate)]
    if not candidates:
        return ""

    for candidate_month in sorted(
        (m for m in invoice_by_month if int(m) <= int(month)),
        reverse=True,
    ):
        invoices = invoice_by_month.get(candidate_month, {})
        for normalized_uc in candidates:
            invoice = invoices.get(normalized_uc) or {}
            issue_value = invoice.get("dtEmissao") or invoice.get("invoice_issue_day") or ""
            if _powerrev_invoice_issue_day(issue_value) is not None:
                return str(issue_value).strip()

    return ""


def _build_open_month_payload(
    uc: str,
    month: int,
    invoice_by_month: dict[int, dict[str, dict[str, str]]],
    *,
    lookup_ucs: list[str] | None = None,
) -> dict[str, str]:
    """
    Monta a linha aberta do mês X usando o saldo final real de X-1.

    O saldo PowerRev já representa o saldo depois da injeção/consumo do mês
    anterior, portanto entra diretamente como saldo inicial do mês seguinte.
    """
    previous_month = _previous_month_int(month)
    previous_invoice = {}
    for candidate in lookup_ucs or [uc]:
        previous_invoice = invoice_by_month.get(previous_month, {}).get(normalize_uc(candidate)) or {}
        if previous_invoice:
            break
    issue_value = _latest_powerrev_invoice_issue_for_month(
        uc,
        month,
        invoice_by_month,
        lookup_ucs=lookup_ucs,
    )
    issue_day = _powerrev_invoice_issue_day(issue_value)
    return {
        "nuMesReferencia": format_reference_month(month),
        "saldo_23_24": previous_invoice.get("saldo_23_24", ""),
        "dtEmissao": issue_value,
        "invoice_issue_day": str(issue_day) if issue_day is not None else "",
    }


def _load_sheet_state() -> tuple[
    dict[tuple[str, str], object],
    dict[tuple[str, str], dict[tuple[str, str], int]],
    dict[tuple[str, str], list[list]],
]:
    """Load worksheet handles and (UC, month)->row map for each target tab."""
    uc_col = COLUMN_ORDER.index("uc")
    month_col = COLUMN_ORDER.index("mes_referencia")
    worksheets: dict[tuple[str, str], object] = {}
    uc_month_rows_by_target: dict[
        tuple[str, str],
        dict[tuple[str, str], int],
    ] = {}
    rows_by_target: dict[tuple[str, str], list[list]] = {}

    for target in _RATEIO_TARGETS:
        distributor, favorecido = target
        spreadsheet_id, target_tab_name = resolve_rateio_sheet_target(
            distributor,
            favorecido,
        )
        ws = get_worksheet(
            target_tab_name,
            spreadsheet_id=spreadsheet_id,
            create_if_missing=False,
        )
        ensure_headers(ws)
        _ensure_invoice_issue_day_header(ws, spreadsheet_id=spreadsheet_id)
        rows = read_all_rows(ws, spreadsheet_id=spreadsheet_id)

        uc_month_rows: dict[tuple[str, str], int] = {}
        for i, row in enumerate(rows):
            if len(row) <= max(uc_col, month_col):
                continue
            uc = normalize_uc(row[uc_col])
            month_ref = _normalize_month_reference(row[month_col])
            if uc and month_ref:
                uc_month_rows[(uc, month_ref)] = i + DATA_START_ROW

        worksheets[target] = ws
        uc_month_rows_by_target[target] = uc_month_rows
        rows_by_target[target] = rows

    return worksheets, uc_month_rows_by_target, rows_by_target


def _merge_frozen_and_open_rows(
    existing_rows: list[list],
    open_rows: list[list],
    *,
    current_month: int,
    frozen_rateio_months: set[str] | None = None,
) -> tuple[list[list], int]:
    """
    Preserva integralmente linhas com G <= mês atual e substitui apenas futuras.

    As linhas continuam agrupadas por UC: histórico congelado primeiro e meses
    futuros recalculados depois.
    """
    uc_idx = COLUMN_ORDER.index("uc")
    month_idx = COLUMN_ORDER.index("mes_referencia")
    frozen_by_uc: dict[str, list[list]] = {}
    open_by_uc: dict[str, list[list]] = {}
    uc_order: list[str] = []
    seen_ucs: set[str] = set()

    def _register_uc(uc_key: str) -> None:
        if uc_key and uc_key not in seen_ucs:
            seen_ucs.add(uc_key)
            uc_order.append(uc_key)

    frozen_count = 0
    for raw_row in existing_rows:
        row = list(raw_row or [])
        uc_key = _normalize_uc_key(row[uc_idx] if uc_idx < len(row) else "")
        month_ref = row[month_idx] if month_idx < len(row) else ""
        if not uc_key or not _is_reference_month_frozen(
            month_ref,
            current_month,
            frozen_rateio_months,
        ):
            continue
        _register_uc(uc_key)
        if len(row) < _RATEIO_WRITE_COL_COUNT:
            row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
        frozen_by_uc.setdefault(uc_key, []).append(row[:_RATEIO_WRITE_COL_COUNT])
        frozen_count += 1

    for raw_row in open_rows:
        row = list(raw_row or [])
        uc_key = _normalize_uc_key(row[uc_idx] if uc_idx < len(row) else "")
        month_ref = row[month_idx] if month_idx < len(row) else ""
        if not uc_key or _is_reference_month_frozen(
            month_ref,
            current_month,
            frozen_rateio_months,
        ):
            continue
        _register_uc(uc_key)
        if len(row) < _RATEIO_WRITE_COL_COUNT:
            row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
        open_by_uc.setdefault(uc_key, []).append(row[:_RATEIO_WRITE_COL_COUNT])

    merged: list[list] = []
    for uc_key in uc_order:
        merged.extend(frozen_by_uc.get(uc_key, []))
        merged.extend(open_by_uc.get(uc_key, []))
    return merged, frozen_count


def _build_open_row_updates(
    existing_rows: list[list],
    open_rows: list[list],
    *,
    current_month: int,
    frozen_rateio_months: set[str] | None = None,
) -> tuple[dict[int, list], int, int]:
    """
    Monta escritas somente para linhas futuras, sem tocar nas congeladas.

    Linhas abertas sao compactadas nos slots disponiveis, preservando apenas
    os meses fechados/congelados na posicao original.
    """
    uc_idx = COLUMN_ORDER.index("uc")
    month_idx = COLUMN_ORDER.index("mes_referencia")
    blank_row = [""] * _RATEIO_WRITE_COL_COUNT
    all_open_slots: list[int] = []
    frozen_count = 0

    for offset, raw_row in enumerate(existing_rows):
        sheet_row = DATA_START_ROW + offset
        row = list(raw_row or [])
        uc_key = _normalize_uc_key(row[uc_idx] if uc_idx < len(row) else "")
        month_ref = _normalize_month_reference(
            row[month_idx] if month_idx < len(row) else ""
        )
        if _is_reference_month_frozen(
            month_ref,
            current_month,
            frozen_rateio_months,
        ):
            frozen_count += 1
            continue

        all_open_slots.append(sheet_row)

    updates: dict[int, list] = {}
    append_row = DATA_START_ROW + len(existing_rows)
    appended_count = 0
    target_open_rows: list[list] = []

    for raw_row in open_rows:
        row = list(raw_row or [])
        if len(row) < _RATEIO_WRITE_COL_COUNT:
            row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
        row = row[:_RATEIO_WRITE_COL_COUNT]

        uc_key = _normalize_uc_key(row[uc_idx] if uc_idx < len(row) else "")
        month_ref = _normalize_month_reference(
            row[month_idx] if month_idx < len(row) else ""
        )
        if (
            not uc_key
            or not month_ref
            or _is_reference_month_frozen(
                month_ref,
                current_month,
                frozen_rateio_months,
            )
        ):
            continue

        target_open_rows.append(row)

    for idx, row in enumerate(target_open_rows):
        if idx < len(all_open_slots):
            sheet_row = all_open_slots[idx]
        else:
            sheet_row = append_row
            append_row += 1
            appended_count += 1
        updates[sheet_row] = row

    for sheet_row in all_open_slots[len(target_open_rows):]:
        updates[sheet_row] = blank_row

    return updates, frozen_count, appended_count


def _apply_updates_to_existing_rows(
    existing_rows: list[list],
    updates: dict[int, list],
) -> list[list]:
    rows = [list(row or []) for row in existing_rows]
    for sheet_row, row_data in updates.items():
        idx = sheet_row - DATA_START_ROW
        if idx < 0:
            continue
        while len(rows) <= idx:
            rows.append([])
        row = list(row_data or [])
        if len(row) < _RATEIO_WRITE_COL_COUNT:
            row.extend([""] * (_RATEIO_WRITE_COL_COUNT - len(row)))
        rows[idx] = row[:_RATEIO_WRITE_COL_COUNT]
    return rows


def _is_blank_rateio_row(row: list | None) -> bool:
    return not any(str(value or "").strip() for value in list(row or [])[:_RATEIO_WRITE_COL_COUNT])


def full_sync() -> None:
    global _known_task_ids
    stats.reset()

    logger.info("=== FULL SYNC inicio ===")
    log_memory("FULL SYNC inicio")
    t0 = time.time()

    # 1. Fetch ClickUp (slim)
    tasks_raw = fetch_all_tasks(include_closed=True, transform=slim_task)
    logger.info("Total tasks recebidas: %d", len(tasks_raw))
    log_memory("Pos-fetch ClickUp (slim)")

    _known_task_ids = {t.get("id", "") for t in tasks_raw if t.get("id")}
    tasks = _prioritize_tasks_by_uc(tasks_raw)
    current_month, months_window, invoice_by_month = _build_powerrev_indexes(tasks)
    projection_index = _build_projection_index()
    last_rateio_index_by_distributor = {
        distributor: _run_sheets_step_with_retry(
            f"Historico ultimo rateio {distributor}",
            lambda distributor_name=distributor: _build_distributor_last_rateio_index(
                distributor_name
            ),
        )
        for distributor in TARGET_SHEET_TABS
    }

    # 2. Build rows por distribuidora + favorecido.
    rows_by_target: dict[tuple[str, str], list[list[str]]] = {
        target: [] for target in _RATEIO_TARGETS
    }
    without_uc = 0
    without_distributor = 0
    without_favorecido = 0
    without_rateio_target = 0
    without_rateio_status = 0
    without_billing_plan = 0
    without_projection = 0
    delayed_new_cooperado_rows = 0
    future_rows = 0

    for task in tasks:
        uc = extract_task_uc(task)
        if not uc:
            without_uc += 1
            continue
        lookup_ucs = extract_task_uc_match_candidates(task) or [uc]

        distributor = extract_task_target_tab(task)
        if distributor not in TARGET_SHEET_TABS:
            without_distributor += 1
            continue
        favorecido_original = _resolve_supported_favorecido(extract_task_favorecido(task))
        if favorecido_original is None:
            without_favorecido += 1
            continue
        status_value = extract_task_status(task)
        if _is_status_excluded_from_rateio(status_value):
            without_rateio_status += 1
            continue
        if _is_plan_excluded_from_rateio(extract_task_plan(task)):
            without_billing_plan += 1
            continue
        skip_projection = _is_status_excluded_from_projection(status_value)

        for month in months_window:
            rateio_month = format_reference_month(_add_months(month, 1))
            if _is_rateio_month_blocked_by_new_cooperado_delay(
                status_value,
                rateio_month,
                current_month,
                distributor,
            ):
                delayed_new_cooperado_rows += 1
                continue
            base_payload = _build_open_month_payload(
                uc,
                month,
                invoice_by_month,
                lookup_ucs=lookup_ucs,
            )
            favorecido = _resolve_effective_favorecido_for_rateio_month(
                task=task,
                distributor=distributor,
                favorecido=favorecido_original,
                rateio_month=rateio_month,
            )
            target = (distributor, favorecido)
            if not _is_rateio_target_enabled(distributor, favorecido):
                without_rateio_target += 1
                continue

            enriched_payload = _inject_projection_value(
                uc,
                base_payload,
                projection_index,
                lookup_ucs=lookup_ucs,
            )
            if not _has_projection_value(enriched_payload.get("projecao_consumo", "")):
                without_projection += 1
                continue

            row_data = build_row(task, enriched_payload)
            if skip_projection:
                row_data = _clear_projection_and_balance_fields(row_data)
            row_data = _set_alteracao_rateio_mes_from_month_ref(row_data)
            row_data = _coerce_h_i_as_numbers(row_data)
            row_data = _set_invoice_issue_day_output_column(
                row_data,
                task,
                enriched_payload.get("invoice_issue_day", ""),
            )
            row_data = _set_favorecido_output_column(
                row_data,
                _favorecido_output_for_rateio_month(
                    task=task,
                    distributor=distributor,
                    original_favorecido=favorecido_original,
                    effective_favorecido=favorecido,
                    rateio_month=rateio_month,
                ),
            )
            row_data = _set_uc_aneel_output_column(row_data, task)
            rows_by_target[target].append(row_data)
            future_rows += 1

    total_rows = sum(len(rows) for rows in rows_by_target.values())
    logger.info("Total linhas geradas: %d", total_rows)
    if without_distributor:
        logger.warning(
            "FULL SYNC: %d tasks com UC ficaram sem distribuidora valida.",
            without_distributor,
        )
    if without_favorecido:
        logger.warning(
            "FULL SYNC: %d tasks com UC ficaram sem Favorecido suportado.",
            without_favorecido,
        )
    if without_rateio_target:
        logger.info(
            "FULL SYNC: %d combinacoes task/mes ignoradas por aba fora da logica.",
            without_rateio_target,
        )
    if without_uc:
        logger.info("FULL SYNC: %d tasks sem UC foram ignoradas.", without_uc)
    if without_rateio_status:
        logger.info("FULL SYNC: %d tasks ignoradas por status fora do rateio.", without_rateio_status)
    if without_billing_plan:
        logger.info(
            "FULL SYNC: %d tasks ignoradas por Plano de Adesao SEM FATURAMENTO.",
            without_billing_plan,
        )
    if without_projection:
        logger.info(
            "FULL SYNC: %d combinacoes task/mes foram ignoradas por ausencia de previsao de consumo.",
            without_projection,
        )
    if delayed_new_cooperado_rows:
        logger.info(
            (
                "FULL SYNC: %d combinacoes task/mes de Novo Cooperado foram ignoradas "
                "por estarem antes de %d meses a frente."
            ),
            delayed_new_cooperado_rows,
            _NEW_COOPERADO_RATEIO_DELAY_MONTHS,
        )
    logger.info(
        "FULL SYNC: %d linhas geradas na janela de 3 meses anteriores, atual e 3 posteriores.",
        future_rows,
    )

    del tasks_raw, tasks
    force_free_memory()

    _sync_generation_total_tabs()

    frozen_months_by_distributor: dict[str, set[str]] = {}
    for distributor in TARGET_SHEET_TABS:
        spreadsheet_id, _ = resolve_rateio_sheet_target(distributor)
        frozen_months_by_distributor[distributor] = _run_sheets_step_with_retry(
            f"Configuracao [{distributor}]",
            lambda sid=spreadsheet_id: _load_frozen_rateio_months(sid),
        )

    # Preflight: valida todas as metas antes de escrever qualquer aba de rateio.
    goals_by_target: dict[tuple[str, str], dict[str, Decimal]] = {}
    for target in _RATEIO_TARGETS:
        distributor, favorecido = target
        spreadsheet_id, _target_tab_name = resolve_rateio_sheet_target(
            distributor,
            favorecido,
        )
        target_label = f"{distributor} / {favorecido}"
        goals = _run_sheets_step_with_retry(
            f"Preflight metas geracao [{target_label}]",
            lambda sid=spreadsheet_id, fav=favorecido: _load_generation_projection_goal_by_month(
                sid,
                fav,
            ),
        )
        _validate_generation_goals(
            goals,
            _required_goal_months_for_rows(
                rows_by_target[target],
                frozen_months_by_distributor[distributor],
            ),
            sheet_label=target_label,
        )
        goals_by_target[target] = goals

    # 3. Escrever cada aba de Favorecido de forma isolada.
    for target in _RATEIO_TARGETS:
        distributor, favorecido = target
        spreadsheet_id, target_tab_name = resolve_rateio_sheet_target(
            distributor,
            favorecido,
        )
        target_label = f"{distributor} / {favorecido}"
        frozen_rateio_months = frozen_months_by_distributor[distributor]

        def _write_rateio_tab():
            ws_local = get_worksheet(
                target_tab_name,
                spreadsheet_id=spreadsheet_id,
                create_if_missing=False,
            )
            ensure_headers(ws_local)
            _ensure_invoice_issue_day_header(ws_local, spreadsheet_id=spreadsheet_id)
            existing_rows = read_all_rows(ws_local, spreadsheet_id=spreadsheet_id)
            merged_rows, frozen_count = _merge_frozen_and_open_rows(
                existing_rows,
                rows_by_target[target],
                current_month=current_month,
                frozen_rateio_months=frozen_rateio_months,
            )
            changed_rows = sync_rows_in_place(
                ws_local,
                merged_rows,
                col_count=_RATEIO_WRITE_COL_COUNT,
                spreadsheet_id=spreadsheet_id,
            )
            appended_count = max(0, len(merged_rows) - len(existing_rows))
            return ws_local, changed_rows, frozen_count, appended_count

        ws, written_rows, frozen_rows, appended_rows = _run_sheets_step_with_retry(
            f"Rateio [{target_label}]",
            _write_rateio_tab,
        )
        goals = goals_by_target[target]
        target_last_rateio_index = _last_rateio_index_for_distributor(
            distributor,
            {},
            last_rateio_index_by_distributor,
        )
        k_changes, l_changes, m_changes = _run_sheets_step_with_retry(
            f"Recalculo K/L/M [{target_label}]",
            lambda ws_local=ws, sid=spreadsheet_id, fav=favorecido, g=goals, j_idx=target_last_rateio_index, proj=projection_index: _recalculate_k_l_m_with_monthly_goal(
                ws_local,
                spreadsheet_id=sid,
                favorecido=fav,
                current_month=current_month,
                monthly_goals=g,
                last_rateio_index=j_idx,
                frozen_rateio_months=frozen_rateio_months,
                projection_index=proj,
            ),
        )
        logger.info(
            (
                "FULL SYNC: planilha %s / aba '%s' escrita com %d linhas "
                "(%d congeladas e nao tocadas; %d novas; K recalculada em %d linhas, "
                "L recalculada em %d linhas, M recalculada em %d linhas)"
            ),
            spreadsheet_id,
            ws.title,
            written_rows,
            frozen_rows,
            appended_rows,
            k_changes,
            l_changes,
            m_changes,
        )

    for distributor in TARGET_SHEET_TABS:
        spreadsheet_id, _ = resolve_rateio_sheet_target(distributor)
        _run_sheets_step_with_retry(
            f"Layout Configuracao [{distributor}]",
            lambda sid=spreadsheet_id: _sync_rateio_configuration_layout(
                sid,
                current_month=current_month,
                distributor=distributor,
            ),
        )

    _sync_all_formularios()

    elapsed = time.time() - t0
    logger.info("=== FULL SYNC concluido em %.1fs - %d linhas ===", elapsed, total_rows)

    del rows_by_target
    force_free_memory()
    log_sync_stats("FULL SYNC")
    log_memory("Pos-gc final")


def delta_sync(last_updated_ts: int) -> int:
    global _known_task_ids
    stats.reset()
    now_ms = int(time.time() * 1000)

    # A projecao de geracao muda independentemente das tasks do ClickUp.
    # Sincronize-a em todo delta antes de carregar as metas usadas nos calculos.
    _sync_generation_total_tabs()

    tasks = fetch_all_tasks(
        include_closed=True,
        date_updated_gt=last_updated_ts,
        transform=slim_task,
    )

    if not tasks:
        _sync_all_formularios()
        log_sync_stats("DELTA SYNC (sem alteracoes)")
        return now_ms

    new_tasks: list[dict] = []
    updated_tasks: list[dict] = []
    for task in tasks:
        tid = task.get("id", "")
        if tid in _known_task_ids:
            updated_tasks.append(task)
        else:
            new_tasks.append(task)
            _known_task_ids.add(tid)

    logger.info("Delta ClickUp: %d atualizadas, %d novas", len(updated_tasks), len(new_tasks))

    if updated_tasks:
        updated_tasks = _prioritize_tasks_by_uc(updated_tasks)
        current_month, months_window, invoice_by_month = _build_powerrev_indexes(updated_tasks)
        projection_index = _build_projection_index()
        last_rateio_index_by_distributor = {
            distributor: _run_sheets_step_with_retry(
                f"Historico ultimo rateio {distributor} (delta)",
                lambda distributor_name=distributor: _build_distributor_last_rateio_index(
                    distributor_name
                ),
            )
            for distributor in TARGET_SHEET_TABS
        }
        worksheets, uc_month_rows_by_target, rows_by_target = _load_sheet_state()

        frozen_months_by_distributor: dict[str, set[str]] = {}
        for distributor in TARGET_SHEET_TABS:
            spreadsheet_id, _ = resolve_rateio_sheet_target(distributor)
            frozen_months_by_distributor[distributor] = _run_sheets_step_with_retry(
                f"Configuracao delta [{distributor}]",
                lambda sid=spreadsheet_id: _load_frozen_rateio_months(sid),
            )

        updates_by_target: dict[tuple[str, str], dict[int, list[str]]] = {
            target: {} for target in _RATEIO_TARGETS
        }
        impacted_months_by_target: dict[tuple[str, str], set[str]] = {
            target: set() for target in _RATEIO_TARGETS
        }

        updated_rows = 0
        cleared_wrong_target = 0
        unresolved = 0
        without_distributor = 0
        without_favorecido = 0
        removed_without_projection = 0
        removed_by_status = 0
        removed_by_plan = 0
        removed_by_route = 0
        removed_by_new_cooperado_delay = 0
        blank_row = [""] * _RATEIO_WRITE_COL_COUNT

        for task in updated_tasks:
            uc = extract_task_uc(task)
            if not uc:
                continue
            lookup_ucs = extract_task_uc_match_candidates(task) or [uc]

            distributor = extract_task_target_tab(task)
            favorecido_original = _resolve_supported_favorecido(extract_task_favorecido(task))
            target: tuple[str, str] | None = None
            if distributor not in TARGET_SHEET_TABS:
                without_distributor += 1
            elif favorecido_original is None:
                without_favorecido += 1

            status_value = extract_task_status(task)
            exclude_from_rateio = _is_status_excluded_from_rateio(status_value)
            exclude_from_plan = _is_plan_excluded_from_rateio(extract_task_plan(task))
            skip_projection = _is_status_excluded_from_projection(status_value)

            for month in months_window:
                month_ref = format_reference_month(month)
                rateio_month = format_reference_month(_add_months(month, 1))
                keys = [(candidate, month_ref) for candidate in lookup_ucs]
                if distributor in TARGET_SHEET_TABS and favorecido_original is not None:
                    favorecido = _resolve_effective_favorecido_for_rateio_month(
                        task=task,
                        distributor=distributor,
                        favorecido=favorecido_original,
                        rateio_month=rateio_month,
                    )
                    if _is_rateio_target_enabled(distributor, favorecido):
                        target = (distributor, favorecido)
                    else:
                        target = None
                else:
                    favorecido = ""
                    target = None

                row_data: list = blank_row
                should_clear = True
                clear_reason = "route"
                blocked_by_new_cooperado_delay = _is_rateio_month_blocked_by_new_cooperado_delay(
                    status_value,
                    rateio_month,
                    current_month,
                    distributor,
                )

                if (
                    target is not None
                    and not exclude_from_rateio
                    and not exclude_from_plan
                    and not blocked_by_new_cooperado_delay
                ):
                    base_payload = _build_open_month_payload(
                        uc,
                        month,
                        invoice_by_month,
                        lookup_ucs=lookup_ucs,
                    )
                    enriched_payload = _inject_projection_value(
                        uc,
                        base_payload,
                        projection_index,
                        lookup_ucs=lookup_ucs,
                    )
                    if _has_projection_value(enriched_payload.get("projecao_consumo", "")):
                        row_data = build_row(task, enriched_payload)
                        if skip_projection:
                            row_data = _clear_projection_and_balance_fields(row_data)
                        row_data = _set_alteracao_rateio_mes_from_month_ref(row_data)
                        row_data = _coerce_h_i_as_numbers(row_data)
                        row_data = _set_invoice_issue_day_output_column(
                            row_data,
                            task,
                            enriched_payload.get("invoice_issue_day", ""),
                        )
                        row_data = _set_favorecido_output_column(
                            row_data,
                            _favorecido_output_for_rateio_month(
                                task=task,
                                distributor=distributor,
                                original_favorecido=favorecido_original,
                                effective_favorecido=favorecido,
                                rateio_month=rateio_month,
                            ),
                        )
                        row_data = _set_uc_aneel_output_column(row_data, task)
                        should_clear = False
                        clear_reason = ""
                    else:
                        clear_reason = "projection"
                elif exclude_from_rateio:
                    clear_reason = "status"
                elif exclude_from_plan:
                    clear_reason = "plan"
                elif target is not None and blocked_by_new_cooperado_delay:
                    clear_reason = "new_cooperado_delay"

                existing_targets = [
                    existing_target
                    for existing_target in _RATEIO_TARGETS
                    if any(key in uc_month_rows_by_target[existing_target] for key in keys)
                ]
                target_was_updated = False
                cleared_for_item = 0

                for existing_target in existing_targets:
                    existing_distributor, _existing_favorecido = existing_target
                    if _is_rateio_month_frozen(
                        rateio_month,
                        frozen_months_by_distributor[existing_distributor],
                    ):
                        continue

                    row_idx = next(
                        uc_month_rows_by_target[existing_target][key]
                        for key in keys
                        if key in uc_month_rows_by_target[existing_target]
                    )
                    if existing_target == target and not should_clear:
                        updates_by_target[existing_target][row_idx] = row_data
                        impacted_months_by_target[existing_target].add(month_ref)
                        target_was_updated = True
                    else:
                        updates_by_target[existing_target][row_idx] = blank_row
                        impacted_months_by_target[existing_target].add(month_ref)
                        cleared_for_item += 1
                        if target is not None and existing_target != target:
                            cleared_wrong_target += 1
                    updated_rows += 1

                if cleared_for_item:
                    if clear_reason == "status":
                        removed_by_status += cleared_for_item
                    elif clear_reason == "plan":
                        removed_by_plan += cleared_for_item
                    elif clear_reason == "projection":
                        removed_without_projection += cleared_for_item
                    elif clear_reason == "route":
                        removed_by_route += cleared_for_item
                    elif clear_reason == "new_cooperado_delay":
                        removed_by_new_cooperado_delay += cleared_for_item

                if target is not None and not should_clear and not target_was_updated:
                    target_row = next(
                        (
                            uc_month_rows_by_target[target].get(key)
                            for key in keys
                            if key in uc_month_rows_by_target[target]
                        ),
                        None,
                    )
                    target_is_frozen = _is_rateio_month_frozen(
                        rateio_month,
                        frozen_months_by_distributor[target[0]],
                    )
                    if not target_row and not target_is_frozen:
                        unresolved += 1

        for target, updates in updates_by_target.items():
            if not updates:
                continue
            distributor, favorecido = target
            ws = worksheets[target]
            target_label = f"{distributor} / {favorecido}"
            if any(_is_blank_rateio_row(row) for row in updates.values()):
                effective_rows = _apply_updates_to_existing_rows(
                    rows_by_target.get(target, []),
                    updates,
                )
                updates, _frozen_count, _appended_count = _build_open_row_updates(
                    effective_rows,
                    effective_rows,
                    current_month=current_month,
                    frozen_rateio_months=frozen_months_by_distributor[distributor],
                )
            goals = _run_sheets_step_with_retry(
                f"Preflight metas geracao delta [{target_label}]",
                lambda sid=ws.spreadsheet_id, fav=favorecido: _load_generation_projection_goal_by_month(
                    sid,
                    fav,
                ),
            )
            _validate_generation_goals(
                goals,
                _required_goal_months_for_reference_months(
                    impacted_months_by_target[target],
                    frozen_months_by_distributor[distributor],
                ),
                sheet_label=target_label,
            )
            update_rows_in_place(ws, updates, col_count=_RATEIO_WRITE_COL_COUNT)
            target_last_rateio_index = _last_rateio_index_for_distributor(
                distributor,
                {},
                last_rateio_index_by_distributor,
            )
            k_changes, l_changes, m_changes = _run_sheets_step_with_retry(
                f"Recalculo K/L/M delta [{target_label}]",
                lambda ws_local=ws, sid=ws.spreadsheet_id, fav=favorecido, months=impacted_months_by_target[target], g=goals, j_idx=target_last_rateio_index, current=current_month, frozen=frozen_months_by_distributor[distributor], proj=projection_index: _recalculate_k_l_m_for_months(
                    ws_local,
                    spreadsheet_id=sid,
                    favorecido=fav,
                    months=months,
                    current_month=current,
                    monthly_goals=g,
                    last_rateio_index=j_idx,
                    frozen_rateio_months=frozen,
                    projection_index=proj,
                ),
            )
            logger.info(
                "Delta ClickUp: aba '%s' atualizada em %d linhas",
                ws.title,
                len(updates),
            )
            logger.info(
                "Delta ClickUp: aba '%s' K=%d, L=%d, M=%d linhas recalculadas",
                ws.title,
                k_changes,
                l_changes,
                m_changes,
            )

        if updated_rows:
            logger.info("Delta ClickUp: %d linhas atualizadas/limpas no total", updated_rows)
        if cleared_wrong_target:
            logger.info(
                "Delta ClickUp: %d linhas de rotas antigas foram limpas sem escrever em aba incorreta.",
                cleared_wrong_target,
            )
        if unresolved:
            logger.info(
                "Delta ClickUp: %d combinacoes task/mes ainda nao existiam na aba correta; o proximo full sync as inclui.",
                unresolved,
            )
        if removed_without_projection:
            logger.info(
                "Delta ClickUp: %d linhas foram limpas por ausencia de previsao de consumo.",
                removed_without_projection,
            )
        if removed_by_status:
            logger.info(
                "Delta ClickUp: %d linhas foram limpas por status fora do rateio.",
                removed_by_status,
            )
        if removed_by_plan:
            logger.info(
                "Delta ClickUp: %d linhas foram limpas por Plano de Adesao SEM FATURAMENTO.",
                removed_by_plan,
            )
        if removed_by_route:
            logger.info(
                "Delta ClickUp: %d linhas foram limpas por distribuidora/Favorecido sem rota suportada.",
                removed_by_route,
            )
        if removed_by_new_cooperado_delay:
            logger.info(
                (
                    "Delta ClickUp: %d linhas foram limpas por status Novo Cooperado "
                    "antes de %d meses a frente."
                ),
                removed_by_new_cooperado_delay,
                _NEW_COOPERADO_RATEIO_DELAY_MONTHS,
            )
        if without_distributor:
            logger.warning(
                "Delta ClickUp: %d tasks com UC ficaram sem distribuidora valida.",
                without_distributor,
            )
        if without_favorecido:
            logger.warning(
                "Delta ClickUp: %d tasks com UC ficaram sem Favorecido suportado.",
                without_favorecido,
            )

    if new_tasks:
        with_valid_target = 0
        without_valid_target = 0
        with_excluded_status = 0
        with_excluded_plan = 0
        for task in new_tasks:
            uc = extract_task_uc(task)
            if not uc:
                continue
            if _is_status_excluded_from_rateio(extract_task_status(task)):
                with_excluded_status += 1
                continue
            if _is_plan_excluded_from_rateio(extract_task_plan(task)):
                with_excluded_plan += 1
                continue
            if _resolve_task_rateio_target(task) is not None:
                with_valid_target += 1
            else:
                without_valid_target += 1

        if with_valid_target:
            logger.info(
                "Delta ClickUp: %d tasks novas com UC/rota serao incluidas no proximo full sync.",
                with_valid_target,
            )
        if without_valid_target:
            logger.warning(
                "Delta ClickUp: %d tasks novas com UC estao sem distribuidora/Favorecido suportado.",
                without_valid_target,
            )
        if with_excluded_status:
            logger.info(
                "Delta ClickUp: %d tasks novas ignoradas por status fora do rateio.",
                with_excluded_status,
            )
        if with_excluded_plan:
            logger.info(
                "Delta ClickUp: %d tasks novas ignoradas por Plano de Adesao SEM FATURAMENTO.",
                with_excluded_plan,
            )

    _sync_all_formularios()

    del tasks, updated_tasks, new_tasks
    log_sync_stats("DELTA SYNC")
    force_free_memory()

    return now_ms

def _reset_all_sessions(reason: str = "") -> None:
    prefix = f" ({reason})" if reason else ""
    logger.warning("Reset total de sessions%s", prefix)
    reset_clickup_session()
    reset_powerrev_session()
    reset_powerrev_caches()
    reset_sheets_client()
    force_free_memory()


def _interruptible_sleep(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end and not _shutdown_requested:
        time.sleep(min(1.0, end - time.time()))


def _get_app_now() -> datetime:
    global _TZ_FALLBACK_LOGGED

    tz_name = (APP_TIMEZONE or "").strip()
    if not tz_name:
        return datetime.now()

    try:
        return datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        if not _TZ_FALLBACK_LOGGED:
            logger.warning(
                "Timezone '%s' indisponivel neste ambiente. "
                "Usando fallback UTC-03:00 para agendamento do full sync.",
                tz_name,
            )
            _TZ_FALLBACK_LOGGED = True
        return datetime.now().astimezone().replace(tzinfo=None)


def _next_daily_full_sync_after(
    now: datetime | None = None,
    *,
    hour: int = _FULL_SYNC_DAILY_HOUR,
) -> datetime:
    current = now or _get_app_now()
    scheduled = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if current >= scheduled:
        scheduled += timedelta(days=1)
    return scheduled


def _run_full_sync_until_success(reason: str) -> bool:
    attempt = 0
    while not _shutdown_requested:
        attempt += 1
        try:
            full_sync()
            return True
        except MemoryError:
            logger.exception(
                "MemoryError no full sync %s (tentativa %d).",
                reason,
                attempt,
            )
        except Exception:
            logger.exception(
                "Full sync %s cancelado (tentativa %d); nenhuma execucao parcial "
                "sera aceita.",
                reason,
                attempt,
            )

        _reset_all_sessions(f"full sync {reason} incompleto")
        backoff = min(
            _ERROR_BACKOFF_BASE * (2 ** min(attempt - 1, 4)),
            _ERROR_BACKOFF_MAX,
        )
        logger.warning(
            "Full sync %s sera reiniciado integralmente em %ds.",
            reason,
            backoff,
        )
        _interruptible_sleep(backoff)

    return False


def main() -> None:
    global _shutdown_requested

    logger.info("Rateio Sync iniciando (PID %d)...", os.getpid())
    log_memory("Boot")

    # Nenhum delta e iniciado antes de um full sync integralmente valido.
    if not _run_full_sync_until_success("inicial"):
        logger.info("Shutdown antes da conclusao do full sync inicial.")
        return

    next_full_at = _next_daily_full_sync_after()
    logger.info(
        "Proximo full sync diario agendado para %s.",
        next_full_at.strftime("%d/%m/%Y %H:%M:%S"),
    )
    last_delta_ts = int(time.time() * 1000)
    consecutive_errors = 0
    cycle_count = 0
    boot_time = time.time()

    while not _shutdown_requested:
        try:
            _interruptible_sleep(DELTA_SYNC_INTERVAL_S)
            if _shutdown_requested:
                break

            now = time.time()
            now_local = _get_app_now()
            cycle_count += 1

            if cycle_count % 10 == 0:
                uptime_h = (now - boot_time) / 3600
                logger.info(
                    "Heartbeat - ciclo %d, uptime %.1fh, RSS %.1f MB, erros consecutivos: %d",
                    cycle_count,
                    uptime_h,
                    stats.get_memory_mb_safe(),
                    consecutive_errors,
                )

            if now_local >= next_full_at:
                if not _run_full_sync_until_success("programado"):
                    break
                next_full_at = _next_daily_full_sync_after(_get_app_now())
                logger.info(
                    "Proximo full sync diario agendado para %s.",
                    next_full_at.strftime("%d/%m/%Y %H:%M:%S"),
                )
                last_delta_ts = int(time.time() * 1000)
            else:
                last_delta_ts = delta_sync(last_delta_ts)

            consecutive_errors = 0

        except KeyboardInterrupt:
            logger.info("Ctrl+C - encerrando.")
            break

        except MemoryError:
            consecutive_errors += 1
            logger.critical("MemoryError no ciclo %d (erro consecutivo #%d)!", cycle_count, consecutive_errors)
            force_free_memory()
            _reset_all_sessions("MemoryError")
            _interruptible_sleep(60)

        except Exception:
            consecutive_errors += 1
            backoff = min(_ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)), _ERROR_BACKOFF_MAX)
            logger.exception(
                "Erro no ciclo %d (erro consecutivo #%d), retry em %ds...",
                cycle_count,
                consecutive_errors,
                backoff,
            )

            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                logger.warning("Atingiu %d erros consecutivos - reset total de sessions.", consecutive_errors)
                _reset_all_sessions("escalation por erros consecutivos")
                consecutive_errors = 0

            _interruptible_sleep(backoff)

    logger.info(
        "Shutdown graceful - %d ciclos executados, uptime %.1fh",
        cycle_count,
        (time.time() - boot_time) / 3600,
    )
    log_memory("Shutdown")


if __name__ == "__main__":
    main()


