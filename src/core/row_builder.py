"""
Build rows for the Rateio sheet.
Extracts ClickUp fields and returns values in COLUMN_ORDER.
"""
import logging

from src.core.field_map import (
    FIELD_MAP,
    COLUMN_ORDER,
    DROPDOWN_OPTIONS,
    TAB_ROUTING,
    TAB_BY_OPTION_ID,
)

logger = logging.getLogger(__name__)

# Frequently used custom field IDs
_UC_CF_ID = FIELD_MAP["uc"]["cf_id"]
_PLAN_CF_ID = FIELD_MAP["plano"]["cf_id"]
_ROUTING_CF_ID = TAB_ROUTING["field_id"]
_FAVORECIDO_CF_ID = "0a73e7b8-febe-4982-9263-06efd75612e1"
_INVOICE_ISSUE_DAY_CF_ID = "c4f18991-f556-4019-af84-157c55aada63"
_HELEXIA_PR_MATRIZ_RATEIO_MONTHS_CF_ID = "f53875e8-8275-46bf-a4b4-d685d86546b4"
_COPEL_MATRIZ_AUGUST_2026_CF_ID = "d4acac12-637e-4e38-af10-049aa89fae28"
_UC_ANEEL_CF_ID = "cd8687a7-0393-45b9-8292-f9b878b31512"


def normalize_uc(value) -> str:
    """Normalize UC for comparisons (remove '-' and trim)."""
    if value is None:
        return ""
    return str(value).strip().replace("-", "")


def _get_custom_field(task: dict, cf_id: str) -> dict | None:
    for cf in task.get("custom_fields", []):
        if cf.get("id") == cf_id:
            return cf
    return None


def _resolve_dropdown_from_type_config(cf: dict | None, raw_value) -> str:
    """Resolve dropdown labels from ClickUp type_config options."""
    if cf is None or raw_value is None:
        return ""

    val = str(raw_value)
    type_config = cf.get("type_config") or {}
    options = type_config.get("options") or []
    if not isinstance(options, list) or not options:
        return ""

    # 1) raw value is option id
    for opt in options:
        if str(opt.get("id", "")) == val:
            return str(opt.get("name", ""))

    # 2) raw value is orderindex
    try:
        idx = int(val)
        for opt in options:
            try:
                if int(opt.get("orderindex")) == idx:
                    return str(opt.get("name", ""))
            except (TypeError, ValueError):
                continue
    except (TypeError, ValueError):
        pass

    # 3) fallback: raw value is positional index in ordered options
    try:
        idx = int(val)
        ordered = sorted(options, key=lambda o: int(o.get("orderindex", 0)))
        if 0 <= idx < len(ordered):
            return str(ordered[idx].get("name", ""))
    except (TypeError, ValueError):
        pass

    return ""


def _resolve_dropdown_value(cf_id: str, raw_value, cf: dict | None = None) -> str:
    """Resolve dropdown values to readable label."""
    if raw_value is None:
        return ""

    dynamic = _resolve_dropdown_from_type_config(cf, raw_value)
    if dynamic:
        return dynamic

    # Static fallback map
    val = str(raw_value)
    options = DROPDOWN_OPTIONS.get(cf_id, {})
    if val in options:
        return options[val]

    try:
        idx = int(val)
        sorted_opts = sorted(options.items())
        if 0 <= idx < len(sorted_opts):
            return sorted_opts[idx][1]
    except (TypeError, ValueError):
        pass

    return val


def _get_cf_value(task: dict, cf_id: str) -> str:
    """Extract custom field value by field id."""
    cf = _get_custom_field(task, cf_id)
    if cf is None:
        return ""

    val = cf.get("value")
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("value", val.get("name", val.get("id", ""))))
    return str(val)


def _extract_field_value(task: dict, key: str, invoice_data: dict | None = None) -> str:
    """Extract one field value according to FIELD_MAP definition."""
    field_def = FIELD_MAP.get(key)
    if not field_def:
        return ""

    source = field_def.get("source", "")

    if source == "task_field":
        return str(task.get(field_def["task_key"], ""))

    if source == "custom_field":
        cf_id = field_def["cf_id"]
        cf = _get_custom_field(task, cf_id)
        raw = _get_cf_value(task, cf_id)
        if field_def.get("transform") == "resolve_dropdown":
            return _resolve_dropdown_value(cf_id, raw, cf)
        return raw

    if source == "placeholder":
        return ""

    if source == "invoice_field":
        if not invoice_data:
            return ""
        return str(invoice_data.get(field_def.get("invoice_key", ""), ""))

    return ""


def extract_task_uc(task: dict) -> str:
    """Extract UC from task."""
    return normalize_uc(_get_cf_value(task, _UC_CF_ID))


def extract_task_target_tab(task: dict) -> str:
    """Resolve target sheet tab from routing custom field."""
    cf = _get_custom_field(task, _ROUTING_CF_ID)
    raw = _get_cf_value(task, _ROUTING_CF_ID).strip()
    if not raw:
        return ""

    # raw value can be option id
    tab = TAB_BY_OPTION_ID.get(raw)
    if tab:
        return tab

    # or a resolved dropdown label
    label = _resolve_dropdown_value(_ROUTING_CF_ID, raw, cf).strip()
    if label in TAB_ROUTING["options"]:
        return label

    return ""


def extract_task_status(task: dict) -> str:
    """Resolve status label from ClickUp status custom field."""
    status_cf_id = FIELD_MAP["status"]["cf_id"]
    cf = _get_custom_field(task, status_cf_id)
    raw = _get_cf_value(task, status_cf_id)
    if not raw:
        return ""
    return _resolve_dropdown_value(status_cf_id, raw, cf).strip()


def extract_task_plan(task: dict) -> str:
    """Resolve adhesion plan label."""
    cf = _get_custom_field(task, _PLAN_CF_ID)
    raw = _get_cf_value(task, _PLAN_CF_ID)
    if not raw:
        return ""
    return _resolve_dropdown_value(_PLAN_CF_ID, raw, cf).strip()


def extract_task_favorecido(task: dict) -> str:
    """Resolve favorecido dropdown label."""
    cf = _get_custom_field(task, _FAVORECIDO_CF_ID)
    raw = _get_cf_value(task, _FAVORECIDO_CF_ID)
    if not raw:
        return ""
    return _resolve_dropdown_value(_FAVORECIDO_CF_ID, raw, cf).strip()


def extract_task_invoice_issue_day(task: dict) -> str:
    """Return the distributor invoice issue day stored in ClickUp."""
    return _get_cf_value(task, _INVOICE_ISSUE_DAY_CF_ID).strip()


def extract_task_uc_aneel(task: dict) -> str:
    """Return the UC Aneel stored in ClickUp."""
    return _get_cf_value(task, _UC_ANEEL_CF_ID).strip()


def extract_task_helexia_pr_matriz_rateio_months(task: dict) -> str:
    """Return rateio months where COPEL/Helexia PR must route to Sion - Matriz."""
    return _get_cf_value(task, _HELEXIA_PR_MATRIZ_RATEIO_MONTHS_CF_ID).strip()


def is_task_copel_matriz_august_2026_checked(task: dict) -> bool:
    """Return whether the one-off COPEL Matriz routing checkbox is checked."""
    cf = _get_custom_field(task, _COPEL_MATRIZ_AUGUST_2026_CF_ID)
    if cf is None:
        return False
    value = cf.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"true", "1", "sim", "yes", "checked"}


def slim_task(task: dict) -> dict:
    """Return slim task payload with only needed fields."""
    needed_cf_ids = {
        f["cf_id"]
        for f in FIELD_MAP.values()
        if f.get("source") == "custom_field"
    }
    needed_cf_ids.add(_ROUTING_CF_ID)
    needed_cf_ids.add(_FAVORECIDO_CF_ID)
    needed_cf_ids.add(_INVOICE_ISSUE_DAY_CF_ID)
    needed_cf_ids.add(_HELEXIA_PR_MATRIZ_RATEIO_MONTHS_CF_ID)
    needed_cf_ids.add(_COPEL_MATRIZ_AUGUST_2026_CF_ID)
    needed_cf_ids.add(_UC_ANEEL_CF_ID)

    slim_cfs = [
        cf for cf in task.get("custom_fields", [])
        if cf.get("id") in needed_cf_ids
    ]
    return {
        "id": task.get("id", ""),
        "name": task.get("name", ""),
        "custom_fields": slim_cfs,
        "date_updated": task.get("date_updated", ""),
        "list": task.get("list", {}),
    }


def build_row(task: dict, invoice_data: dict | None = None) -> list[str]:
    """Build one sheet row in COLUMN_ORDER."""
    return [_extract_field_value(task, key, invoice_data) for key in COLUMN_ORDER]
