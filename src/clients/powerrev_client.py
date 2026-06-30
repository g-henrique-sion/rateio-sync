"""
PowerRev client for Rateio Sync.

Replica a lÃ³gica do faturamento_sync:
  - AutenticaÃ§Ã£o via /sign (accountId + apiKey)
  - Carregamento de consumer units via /consumer-unit
  - Busca de invoices de consumo via /invoice?nuAnoMes=YYYYMM
  - Detalhe da fatura via /invoice/{idFaturaConsumo}
  - ResoluÃ§Ã£o de UC via /consumer-unit (idUnidadeConsumo â†’ nuInstalacao)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from src.config import (
    POWERREV_BASE_URL,
    POWERREV_AUTH_URL,
    POWERREV_ACCOUNT_ID,
    POWERREV_API_KEY,
    POWERREV_TOKEN,
    POWERREV_TIMEOUT_S,
    POWERREV_DELAY_S,
    POWERREV_MAX_RETRIES,
    APP_TIMEZONE,
)
from src.utils.stats import stats

logger = logging.getLogger(__name__)

_SESSION: requests.Session | None = None
_TOKEN: str | None = None

# Caches de consumer units (mesmo padrÃ£o do faturamento_sync)
_UC_BY_ID: dict[str, dict] = {}
_UC_BY_INSTALLATION: dict[str, dict] = {}
_UC_BY_CODE: dict[str, dict] = {}
_INVOICE_SALDO_CACHE: dict[str, dict] = {}

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DETAIL_CACHE_FILE = os.getenv(
    "POWERREV_DETAIL_CACHE_FILE",
    os.path.join(_PROJECT_ROOT, ".powerrev_invoice_detail_cache.json"),
)
_USE_BATCH_EXPORT = os.getenv("POWERREV_USE_BATCH_EXPORT", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
_BATCH_EXPORT_IDS_PER_JOB = max(1, int(os.getenv("POWERREV_BATCH_EXPORT_IDS_PER_JOB", "400")))
_BATCH_EXPORT_POLL_INTERVAL_S = max(1.0, float(os.getenv("POWERREV_BATCH_EXPORT_POLL_INTERVAL_S", "4.0")))
_BATCH_EXPORT_POLL_MAX_ATTEMPTS = max(1, int(os.getenv("POWERREV_BATCH_EXPORT_POLL_MAX_ATTEMPTS", "45")))
_BATCH_EXPORT_JOB_PAUSE_S = max(0.0, float(os.getenv("POWERREV_BATCH_EXPORT_JOB_PAUSE_S", "1.0")))
_BATCH_EXPORT_DOWNLOAD_TIMEOUT_S = max(
    5.0,
    float(os.getenv("POWERREV_BATCH_EXPORT_DOWNLOAD_TIMEOUT_S", str(POWERREV_TIMEOUT_S))),
)
_BATCH_EXPORT_FALLBACK_MAX_IDS = max(0, int(os.getenv("POWERREV_BATCH_EXPORT_FALLBACK_MAX_IDS", "0")))
_DETAIL_MIN_INTERVAL_S = max(0.0, float(os.getenv("POWERREV_DETAIL_MIN_INTERVAL_S", "1.0")))
_REQUEST_MIN_INTERVAL_S = max(0.0, float(os.getenv("POWERREV_REQUEST_MIN_INTERVAL_S", str(POWERREV_DELAY_S))))

_PERSISTED_SALDO_CACHE: dict[str, dict] = {}
_PERSISTED_CACHE_LOADED = False
_PERSISTED_CACHE_DIRTY = False
_LAST_DETAIL_REQUEST_TS = 0.0
_LAST_REQUEST_TS = 0.0

_TZ_FALLBACK_LOGGED = False
_BATCH_JOB_DONE_STATUSES = {"done", "success", "completed", "finished", "ready"}
_BATCH_JOB_FAILED_STATUSES = {"failed", "error", "canceled", "cancelled"}


def _normalize_uc(value) -> str:
    """Normalize UC for matching (remove '-' and trim)."""
    if value is None:
        return ""
    return str(value).strip().replace("-", "")


def _format_reference_month(value) -> str:
    """Format YYYYMM into DD-MM-YYYY using day 01."""
    if value is None:
        return ""

    raw = "".join(ch for ch in str(value).strip() if ch.isdigit())
    if len(raw) >= 6:
        year = raw[:4]
        month = raw[4:6]
        try:
            month_num = int(month)
            if 1 <= month_num <= 12:
                return f"01-{month}-{year}"
        except ValueError:
            pass
    return str(value)


def format_reference_month(value) -> str:
    """Public helper for month formatting used by sync flows."""
    return _format_reference_month(value)


def _format_currency(value) -> str:
    """Formata nÃºmeros no padrÃ£o BR para a coluna de saldo."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
        if num.is_integer():
            return str(int(num))
        return f"{num:.2f}".replace(".", ",")
    except (ValueError, TypeError):
        return str(value)


def _parse_number(value) -> float | None:
    """Converte valores numÃ©ricos vindos como nÃºmero/string para float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip().replace(" ", "")
    if not raw:
        return None

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")

    try:
        return float(raw)
    except ValueError:
        return None


def _parse_invoice_datetime(value) -> datetime:
    """Parse PowerRev date fields for duplicate invoice ordering."""
    if value is None:
        return datetime.min

    raw = str(value).strip()
    if not raw:
        return datetime.min

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(f"{raw[:-1]}+00:00")

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass

    return datetime.min


def _parse_invoice_id(value) -> int:
    raw = str(value or "").strip()
    try:
        return int(raw)
    except ValueError:
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else 0


def _invoice_recency_key(invoice: dict) -> tuple[datetime, int]:
    """Return ordering key for duplicate invoices of the same UC/month."""
    return (
        _parse_invoice_datetime(invoice.get("dtEmissao")),
        _parse_invoice_id(invoice.get("invoiceId")),
    )


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.timeout = POWERREV_TIMEOUT_S
    return _SESSION


def reset_session() -> None:
    """Fecha session HTTP e limpa token. PrÃ³xima chamada re-autentica."""
    global _SESSION, _TOKEN
    if _SESSION is not None:
        try:
            _SESSION.close()
        except Exception:
            pass
        _SESSION = None
    _TOKEN = None
    logger.info("PowerRev session resetada.")


def reset_caches() -> None:
    """Limpa caches em memÃ³ria (consumer units e saldos de invoice)."""
    global _UC_BY_ID
    global _UC_BY_INSTALLATION
    global _UC_BY_CODE
    global _INVOICE_SALDO_CACHE
    global _PERSISTED_SALDO_CACHE
    global _PERSISTED_CACHE_LOADED
    global _PERSISTED_CACHE_DIRTY
    _UC_BY_ID = {}
    _UC_BY_INSTALLATION = {}
    _UC_BY_CODE = {}
    _INVOICE_SALDO_CACHE = {}
    _PERSISTED_SALDO_CACHE = {}
    _PERSISTED_CACHE_LOADED = False
    _PERSISTED_CACHE_DIRTY = False


def _authenticate() -> str:
    global _TOKEN
    session = _get_session()

    for attempt in range(POWERREV_MAX_RETRIES):
        try:
            resp = session.post(
                f"{POWERREV_AUTH_URL}/sign",
                json={"accountId": POWERREV_ACCOUNT_ID, "apiKey": POWERREV_API_KEY},
                headers={"Content-Type": "application/json"},
            )
            stats.powerrev_requests += 1
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token") or data.get("accessToken")
            if token:
                _TOKEN = token
                logger.info("PowerRev: autenticaÃ§Ã£o OK.")
                return token
            raise RuntimeError("Token nÃ£o retornado pela API PowerRev.")
        except requests.RequestException as exc:
            logger.warning(
                "PowerRev auth tentativa %d/%d: %s",
                attempt + 1,
                POWERREV_MAX_RETRIES,
                exc,
            )
            if attempt < POWERREV_MAX_RETRIES - 1:
                time.sleep(POWERREV_DELAY_S * (attempt + 1))
            else:
                raise
    raise RuntimeError("Falha na autenticaÃ§Ã£o PowerRev.")


def _get_headers() -> dict[str, str]:
    global _TOKEN
    if _TOKEN is None:
        if POWERREV_ACCOUNT_ID and POWERREV_API_KEY:
            _authenticate()
        elif POWERREV_TOKEN and POWERREV_TOKEN.strip():
            _TOKEN = POWERREV_TOKEN.strip()
            logger.info("PowerRev: usando token estÃ¡tico via POWERREV_TOKEN.")
        else:
            raise RuntimeError(
                "PowerRev sem credenciais. "
                "Defina POWERREV_ACCOUNT_ID + POWERREV_API_KEY ou POWERREV_TOKEN."
            )
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {_TOKEN}",
    }


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Request com retry, re-auth em 401, backoff em 429/5xx.
    Mesma lÃ³gica do faturamento_sync.
    """
    global _TOKEN
    global _LAST_REQUEST_TS
    session = _get_session()

    attempt = 0
    while attempt < POWERREV_MAX_RETRIES:
        try:
            if _REQUEST_MIN_INTERVAL_S > 0:
                now = time.monotonic()
                wait = _REQUEST_MIN_INTERVAL_S - (now - _LAST_REQUEST_TS)
                if wait > 0:
                    time.sleep(wait)

            if "timeout" not in kwargs:
                kwargs["timeout"] = POWERREV_TIMEOUT_S
            kwargs["headers"] = _get_headers()
            resp = session.request(method, url, **kwargs)
            _LAST_REQUEST_TS = time.monotonic()
            stats.powerrev_requests += 1

            if resp.status_code == 401:
                logger.warning("PowerRev 401, re-autenticando...")
                _TOKEN = None
                _authenticate()
                continue  # nÃ£o incrementa attempt

            if resp.status_code == 429:
                attempt += 1
                retry_after = int(resp.headers.get("Retry-After", "30"))
                logger.warning(
                    "PowerRev rate limit 429 (tentativa %d/%d), aguardando %ds",
                    attempt,
                    POWERREV_MAX_RETRIES,
                    retry_after,
                )
                if attempt < POWERREV_MAX_RETRIES:
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()

            if resp.status_code in (500, 502, 503):
                attempt += 1
                wait = 2 ** attempt
                logger.warning(
                    "PowerRev %s (tentativa %d/%d), retry em %ds",
                    resp.status_code,
                    attempt,
                    POWERREV_MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            attempt += 1
            logger.warning(
                "PowerRev tentativa %d/%d: %s",
                attempt,
                POWERREV_MAX_RETRIES,
                exc,
            )
            if attempt < POWERREV_MAX_RETRIES:
                time.sleep(POWERREV_DELAY_S * attempt)
            else:
                raise
    raise RuntimeError("Falha na requisiÃ§Ã£o PowerRev.")


# â”€â”€ Consumer Unit loading & resolution (mesmo padrÃ£o faturamento) â”€â”€


def _load_consumer_units() -> None:
    """Carrega todas as consumer units e indexa por id, instalaÃ§Ã£o e cÃ³digo."""
    global _UC_BY_ID, _UC_BY_INSTALLATION, _UC_BY_CODE
    if _UC_BY_ID:
        return

    resp = _request("GET", f"{POWERREV_BASE_URL}/consumer-unit")
    payload = resp.json()
    units = payload if isinstance(payload, list) else []

    _UC_BY_ID = {}
    _UC_BY_INSTALLATION = {}
    _UC_BY_CODE = {}

    for u in units:
        id_uc = u.get("idUnidadeConsumo")
        if id_uc is not None:
            _UC_BY_ID[str(id_uc).strip()] = u

        inst = _normalize_uc(u.get("nuInstalacao"))
        if inst:
            _UC_BY_INSTALLATION[inst] = u

        code = _normalize_uc(u.get("codUnidadeConsumo"))
        if code:
            _UC_BY_CODE[code] = u

    logger.info("PowerRev: %d UCs carregadas.", len(units))


def _resolve_uc_installation(item: dict) -> str | None:
    """Resolve o nuInstalacao a partir dos dados do invoice.
    Mesma lÃ³gica do faturamento_sync.
    """
    # Prioridade: usar UC diretamente do /invoice, sem depender de /consumer-unit.
    for field in ("nuInstalacao", "cdInstalacao"):
        direct_uc = _normalize_uc(item.get(field))
        if direct_uc:
            return direct_uc

    # Fallback: resolver por chaves com /consumer-unit.
    keys: list[str] = []

    for field in (
        "idUnidadeConsumo",
        "codUnidadeConsumo",
        "cdChaveExterna",
        "noRecurso",
    ):
        val = item.get(field)
        if val is not None and str(val).strip():
            keys.append(str(val).strip())

    consumer_units_raw = item.get("consumerUnits")
    if isinstance(consumer_units_raw, list):
        for v in consumer_units_raw:
            if isinstance(v, dict):
                recurso = (
                    v.get("recurso") if isinstance(v.get("recurso"), dict) else None
                )
                if recurso:
                    for field in ("idUnidadeConsumo", "cdChaveExterna", "noRecurso"):
                        val = recurso.get(field)
                        if val is not None and str(val).strip():
                            keys.append(str(val).strip())
                for field in ("idUnidadeConsumo", "cdChaveExterna", "noRecurso"):
                    val = v.get(field)
                    if val is not None and str(val).strip():
                        keys.append(str(val).strip())
    elif consumer_units_raw is not None:
        raw = str(consumer_units_raw).strip()
        if raw:
            keys.append(raw)

    if keys:
        # Remove duplicados preservando ordem.
        keys = list(dict.fromkeys(keys))
    else:
        return None

    _load_consumer_units()

    for key in keys:
        normalized_key = _normalize_uc(key)
        uc = (
            _UC_BY_ID.get(str(key).strip())
            or _UC_BY_INSTALLATION.get(normalized_key)
            or _UC_BY_CODE.get(normalized_key)
        )
        if uc and uc.get("nuInstalacao"):
            return _normalize_uc(uc["nuInstalacao"])

    return None


# â”€â”€ Invoice fetching (/invoice) + detalhe (/invoice/{id}) â”€â”€


def _normalize_items(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("content", "data", "items", "results", "responseList", "invoices"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def fetch_invoices_for_month(reference_month: int | str) -> list[dict]:
    """Busca invoices de consumo de um mÃªs via /invoice?nuAnoMes=YYYYMM.
    Resolve UC via /consumer-unit. Retorna lista de dicts com UC e id da fatura.
    Mesma lÃ³gica do faturamento_sync.
    """
    ref = str(reference_month)

    resp = _request(
        "GET",
        f"{POWERREV_BASE_URL}/invoice",
        params={"nuAnoMes": ref},
    )
    payload = resp.json()
    items = _normalize_items(payload)

    resolved: list[dict] = []
    for item in items:
        uc = _resolve_uc_installation(item)
        inv_id = item.get("idFaturaConsumo")
        invoice_id = str(inv_id).strip() if inv_id is not None else ""
        month = item.get("nuMesReferencia") or ref
        resolved.append({
            "uc": _normalize_uc(uc),
            "invoiceId": invoice_id,
            "nuMesReferencia": str(month),
            "dtEmissao": str(item.get("dtEmissao") or "").strip(),
            "updatedAt": str(item.get("dtAtualizacao") or "").strip(),
            "status": str(item.get("status", "")),
        })

    if resolved:
        logger.info("  -> %d invoices para mÃªs %s", len(resolved), ref)
    else:
        logger.info("  -> sem dados para mÃªs %s", ref)
    return resolved


def _is_cache_entry_valid(entry: dict, updated_at: str) -> bool:
    if not isinstance(entry, dict):
        return False

    if "saldo23_24" not in entry:
        return False

    if not updated_at:
        return True

    cached_updated = str(entry.get("updatedAt") or "").strip()
    return bool(cached_updated and cached_updated == updated_at)


def _load_persisted_saldo_cache() -> None:
    global _PERSISTED_SALDO_CACHE, _PERSISTED_CACHE_LOADED
    if _PERSISTED_CACHE_LOADED:
        return

    try:
        if os.path.exists(_DETAIL_CACHE_FILE):
            with open(_DETAIL_CACHE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
                if isinstance(payload, dict):
                    _PERSISTED_SALDO_CACHE = payload
    except Exception as exc:
        logger.warning("PowerRev: falha ao carregar cache persistente (%s).", exc)
        _PERSISTED_SALDO_CACHE = {}

    _PERSISTED_CACHE_LOADED = True


def _save_persisted_saldo_cache() -> None:
    global _PERSISTED_CACHE_DIRTY
    if not _PERSISTED_CACHE_DIRTY:
        return

    try:
        parent = os.path.dirname(_DETAIL_CACHE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{_DETAIL_CACHE_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_PERSISTED_SALDO_CACHE, f, ensure_ascii=True, separators=(",", ":"))
        os.replace(tmp_path, _DETAIL_CACHE_FILE)
        _PERSISTED_CACHE_DIRTY = False
    except Exception as exc:
        logger.warning("PowerRev: falha ao salvar cache persistente (%s).", exc)


def _get_cached_invoice_fields(invoice_id: str, updated_at: str) -> tuple[bool, float | None, str]:
    _load_persisted_saldo_cache()

    key = str(invoice_id).strip()
    if not key:
        return False, None, ""

    local_entry = _INVOICE_SALDO_CACHE.get(key)
    if local_entry and _is_cache_entry_valid(local_entry, updated_at):
        return (
            True,
            _parse_number(local_entry.get("saldo23_24")),
            str(local_entry.get("dtEmissao") or "").strip(),
        )

    persisted_entry = _PERSISTED_SALDO_CACHE.get(key)
    if persisted_entry and _is_cache_entry_valid(persisted_entry, updated_at):
        _INVOICE_SALDO_CACHE[key] = persisted_entry
        return (
            True,
            _parse_number(persisted_entry.get("saldo23_24")),
            str(persisted_entry.get("dtEmissao") or "").strip(),
        )

    return False, None, ""


def _get_cached_saldo(invoice_id: str, updated_at: str) -> tuple[bool, float | None]:
    hit, saldo, _dt_emissao = _get_cached_invoice_fields(invoice_id, updated_at)
    return hit, saldo


def _set_cached_saldo(
    invoice_id: str,
    updated_at: str,
    saldo: float | None,
    dt_emissao: str = "",
) -> None:
    global _PERSISTED_CACHE_DIRTY
    _load_persisted_saldo_cache()

    key = str(invoice_id).strip()
    if not key:
        return

    entry = {
        "updatedAt": updated_at or "",
        "saldo23_24": saldo,
    }
    if dt_emissao:
        entry["dtEmissao"] = dt_emissao
    _INVOICE_SALDO_CACHE[key] = entry
    _PERSISTED_SALDO_CACHE[key] = entry
    _PERSISTED_CACHE_DIRTY = True


def _throttle_invoice_detail_requests() -> None:
    global _LAST_DETAIL_REQUEST_TS
    if _DETAIL_MIN_INTERVAL_S <= 0:
        return

    now = time.monotonic()
    wait = _DETAIL_MIN_INTERVAL_S - (now - _LAST_DETAIL_REQUEST_TS)
    if wait > 0:
        time.sleep(wait)
    _LAST_DETAIL_REQUEST_TS = time.monotonic()


def _fetch_invoice_detail(invoice_id: str) -> dict:
    """Busca detalhe da fatura por idFaturaConsumo."""
    key = str(invoice_id).strip()
    if not key:
        return {}

    _throttle_invoice_detail_requests()
    resp = _request("GET", f"{POWERREV_BASE_URL}/invoice/{key}")
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _extract_saldo_23_24(detail: dict) -> float | None:
    """Soma itens de id 23 e 24 do detalhe da fatura."""
    items = detail.get("itens")
    if not isinstance(items, list):
        return None

    total = 0.0
    found = False
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if item_id not in (23, 24):
            continue

        value = _parse_number(item.get("value"))
        if value is None:
            continue

        total += value
        found = True

    return total if found else None


def _extract_invoice_issue_date(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("dtEmissao") or "").strip()


def _invoice_index_entry(month_ref: str, saldo, dt_emissao: str) -> dict[str, str]:
    return {
        "nuMesReferencia": _format_reference_month(month_ref),
        "saldo_23_24": _format_currency(saldo),
        "dtEmissao": str(dt_emissao or "").strip(),
    }


def _extract_batch_job_id(payload: object) -> str:
    def _walk(node: object, depth: int) -> str:
        if depth > 5:
            return ""
        if not isinstance(node, dict):
            return ""

        raw = node.get("jobId") or node.get("job_id")
        if raw is not None and str(raw).strip():
            return str(raw).strip()

        # Alguns retornos usam apenas "id" para o job.
        if (
            node.get("id") is not None
            and str(node.get("id")).strip()
            and "idFaturaConsumo" not in node
            and "invoiceIds" not in node
        ):
            return str(node.get("id")).strip()

        for key in ("job", "data", "result", "payload"):
            nested = node.get(key)
            nested_id = _walk(nested, depth + 1)
            if nested_id:
                return nested_id

        return ""

    return _walk(payload, 0)


def _extract_batch_status(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    raw = payload.get("status")
    if raw is not None and str(raw).strip():
        return str(raw).strip().lower()

    for key in ("job", "data", "result", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_status = _extract_batch_status(nested)
            if nested_status:
                return nested_status
    return ""


def _extract_batch_result_url(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    raw_url = payload.get("url")
    if isinstance(raw_url, str) and raw_url.strip().startswith("http"):
        return raw_url.strip()

    result = payload.get("result")
    if isinstance(result, dict):
        nested_url = result.get("url")
        if isinstance(nested_url, str) and nested_url.strip().startswith("http"):
            return nested_url.strip()

    for key in ("data", "payload", "job"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_url = _extract_batch_result_url(nested)
            if nested_url:
                return nested_url

    return ""


def _parse_batch_export_details(payload: object) -> dict[str, dict]:
    items = _normalize_items(payload)
    if not items and isinstance(payload, dict) and payload.get("idFaturaConsumo") is not None:
        items = [payload]

    details: dict[str, dict] = {}
    for item in items:
        inv_id = item.get("idFaturaConsumo")
        if inv_id is None:
            continue
        key = str(inv_id).strip()
        if not key:
            continue
        details[key] = item

    return details


def _submit_batch_export_job(invoice_ids: list[str]) -> object:
    payload = {"invoiceIds": invoice_ids}
    resp = _request(
        "POST",
        f"{POWERREV_BASE_URL}/invoice/batch-export",
        json=payload,
    )
    try:
        return resp.json()
    except ValueError:
        return {}


def _poll_batch_export_job(job_id: str) -> object:
    key = str(job_id).strip()
    if not key:
        raise RuntimeError("Job ID do batch-export vazio.")

    for attempt in range(1, _BATCH_EXPORT_POLL_MAX_ATTEMPTS + 1):
        resp = _request("GET", f"{POWERREV_BASE_URL}/invoice/batch-export/{key}")
        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        status = _extract_batch_status(payload)
        has_result = bool(_extract_batch_result_url(payload))
        if status in _BATCH_JOB_DONE_STATUSES or (not status and has_result):
            return payload
        if status in _BATCH_JOB_FAILED_STATUSES:
            raise RuntimeError(
                f"Batch-export job {key} retornou status de falha: {status or 'desconhecido'}"
            )

        if attempt < _BATCH_EXPORT_POLL_MAX_ATTEMPTS:
            time.sleep(_BATCH_EXPORT_POLL_INTERVAL_S)

    raise TimeoutError(
        f"Batch-export job {key} nÃ£o concluiu apÃ³s {_BATCH_EXPORT_POLL_MAX_ATTEMPTS} tentativas."
    )


def _download_batch_export_payload(result_url: str) -> object:
    url = str(result_url or "").strip()
    if not url:
        return {}

    session = _get_session()
    resp = session.get(url, timeout=_BATCH_EXPORT_DOWNLOAD_TIMEOUT_S)
    stats.powerrev_requests += 1
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        logger.warning("PowerRev: resultado do batch-export nÃ£o retornou JSON.")
        return {}


def _fetch_invoice_details_batch_export(invoice_ids: list[str]) -> dict[str, dict]:
    unique_ids = [i for i in dict.fromkeys(str(v).strip() for v in invoice_ids) if i]
    if not unique_ids:
        return {}

    submit_payload = _submit_batch_export_job(unique_ids)

    # Compatibilidade: alguns ambientes podem retornar conteÃºdo jÃ¡ pronto no POST.
    direct_details = _parse_batch_export_details(submit_payload)
    if direct_details:
        return direct_details

    job_id = _extract_batch_job_id(submit_payload)
    if not job_id:
        raise RuntimeError("PowerRev: POST /invoice/batch-export nÃ£o retornou jobId.")

    job_payload = _poll_batch_export_job(job_id)
    result_url = _extract_batch_result_url(job_payload)
    if result_url:
        result_payload = _download_batch_export_payload(result_url)
        details = _parse_batch_export_details(result_payload)
        if details:
            return details

    # Fallback: em alguns cenÃ¡rios os detalhes podem vir no prÃ³prio payload do job.
    details = _parse_batch_export_details(job_payload)
    if details:
        return details

    return {}


# â”€â”€ Timezone / reference month â”€â”€


def get_current_reference_month() -> int:
    return _get_current_reference_month_from_timezone()


def _get_current_reference_month_from_timezone() -> int:
    global _TZ_FALLBACK_LOGGED

    tz_name = (APP_TIMEZONE or "").strip()
    if not tz_name:
        return int(datetime.now().strftime("%Y%m"))

    try:
        now = datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        if not _TZ_FALLBACK_LOGGED:
            logger.warning(
                "Timezone '%s' indisponÃ­vel neste ambiente (tzdata ausente). "
                "Usando fallback UTC-03:00 para mÃªs de referÃªncia.",
                tz_name,
            )
            _TZ_FALLBACK_LOGGED = True
        now = datetime.now(timezone(timedelta(hours=-3)))

    return int(now.strftime("%Y%m"))


# â”€â”€ Ãndice por UC para enriquecimento das rows â”€â”€


def build_invoice_index_for_ucs(
    ucs: set[str],
    *,
    reference_month: int | None = None,
) -> dict[str, dict[str, str]]:
    """Indexa por UC os dados de fatura de um mês."""
    normalized_ucs = {_normalize_uc(uc) for uc in ucs if _normalize_uc(uc)}
    if not normalized_ucs:
        return {}

    month = reference_month or get_current_reference_month()
    logger.info("PowerRev: buscando faturas do mês %s para %d UCs.", month, len(normalized_ucs))
    invoices = fetch_invoices_for_month(month)

    result: dict[str, dict[str, str]] = {}
    target_invoice_by_uc: dict[str, dict] = {}

    details_ok = 0
    details_failed = 0
    details_cached = 0
    batch_jobs_submitted = 0
    batch_jobs_completed = 0
    batch_jobs_failed = 0

    for inv in invoices:
        uc = _normalize_uc(inv.get("uc", ""))
        if not uc or uc not in normalized_ucs:
            continue
        current = target_invoice_by_uc.get(uc)
        if current is None or _invoice_recency_key(inv) > _invoice_recency_key(current):
            target_invoice_by_uc[uc] = inv

    pending_details: list[tuple[str, str, str, str, str]] = []
    for uc, inv in target_invoice_by_uc.items():
        month_ref = str(inv.get("nuMesReferencia") or month)
        invoice_id = str(inv.get("invoiceId") or "").strip()
        list_issue_date = _extract_invoice_issue_date(inv)
        if not invoice_id:
            result[uc] = _invoice_index_entry(month_ref, "", list_issue_date)
            continue

        updated_at = str(inv.get("updatedAt") or "").strip()
        hit, cached_saldo, cached_issue_date = _get_cached_invoice_fields(invoice_id, updated_at)
        if hit:
            details_cached += 1
            issue_date = list_issue_date or cached_issue_date
            result[uc] = _invoice_index_entry(month_ref, cached_saldo, issue_date)
            continue

        pending_details.append((uc, invoice_id, month_ref, updated_at, list_issue_date))

    pending_records_by_invoice: dict[str, list[tuple[str, str, str, str]]] = {}
    for uc, invoice_id, month_ref, updated_at, list_issue_date in pending_details:
        pending_records_by_invoice.setdefault(invoice_id, []).append(
            (uc, month_ref, updated_at, list_issue_date)
        )
    pending_invoice_ids = list(pending_records_by_invoice.keys())

    resolved_saldo_by_invoice: dict[str, float | None] = {}
    resolved_issue_date_by_invoice: dict[str, str] = {}
    unresolved_ids: list[str] = []

    if pending_invoice_ids and _USE_BATCH_EXPORT:
        for start in range(0, len(pending_invoice_ids), _BATCH_EXPORT_IDS_PER_JOB):
            chunk = pending_invoice_ids[start:start + _BATCH_EXPORT_IDS_PER_JOB]
            batch_jobs_submitted += 1
            try:
                details_by_id = _fetch_invoice_details_batch_export(chunk)
                batch_jobs_completed += 1
            except (requests.RequestException, RuntimeError, TimeoutError) as exc:
                logger.warning(
                    "PowerRev: falha no batch-export (%d IDs, mês %s): %s",
                    len(chunk),
                    month,
                    exc,
                )
                batch_jobs_failed += 1
                details_by_id = {}

            for invoice_id in chunk:
                detail = details_by_id.get(invoice_id)
                if detail is None:
                    unresolved_ids.append(invoice_id)
                    continue
                saldo = _extract_saldo_23_24(detail)
                _uc, _month_ref, updated_at, list_issue_date = pending_records_by_invoice[
                    invoice_id
                ][0]
                issue_date = _extract_invoice_issue_date(detail) or list_issue_date
                _set_cached_saldo(invoice_id, updated_at, saldo, issue_date)
                resolved_saldo_by_invoice[invoice_id] = saldo
                resolved_issue_date_by_invoice[invoice_id] = issue_date
                details_ok += 1

            if _BATCH_EXPORT_JOB_PAUSE_S > 0 and start + _BATCH_EXPORT_IDS_PER_JOB < len(
                pending_invoice_ids
            ):
                time.sleep(_BATCH_EXPORT_JOB_PAUSE_S)
    else:
        unresolved_ids = list(pending_invoice_ids)

    unresolved_ids = list(dict.fromkeys(unresolved_ids))

    if unresolved_ids:
        if _USE_BATCH_EXPORT:
            fallback_limit = min(len(unresolved_ids), _BATCH_EXPORT_FALLBACK_MAX_IDS)
        else:
            fallback_limit = len(unresolved_ids)

        if fallback_limit > 0:
            fallback_ids = unresolved_ids[:fallback_limit]
            logger.info(
                "PowerRev: fallback por ID ativo para %d faturas (intervalo mínimo %.2fs).",
                len(fallback_ids),
                _DETAIL_MIN_INTERVAL_S,
            )

            for invoice_id in fallback_ids:
                _uc, _month_ref, updated_at, list_issue_date = pending_records_by_invoice[
                    invoice_id
                ][0]
                hit, cached_saldo, cached_issue_date = _get_cached_invoice_fields(
                    invoice_id,
                    updated_at,
                )
                if hit:
                    resolved_saldo_by_invoice[invoice_id] = cached_saldo
                    resolved_issue_date_by_invoice[invoice_id] = (
                        list_issue_date or cached_issue_date
                    )
                    details_cached += 1
                    continue

                try:
                    detail = _fetch_invoice_detail(invoice_id)
                    saldo = _extract_saldo_23_24(detail)
                    issue_date = _extract_invoice_issue_date(detail) or list_issue_date
                    _set_cached_saldo(invoice_id, updated_at, saldo, issue_date)
                    resolved_saldo_by_invoice[invoice_id] = saldo
                    resolved_issue_date_by_invoice[invoice_id] = issue_date
                    details_ok += 1
                except requests.RequestException:
                    logger.warning("PowerRev: falha ao buscar detalhe da fatura %s", invoice_id)
                    details_failed += 1

            unresolved_ids = unresolved_ids[fallback_limit:]

        details_failed += len(unresolved_ids)

    for uc, invoice_id, month_ref, _updated_at, list_issue_date in pending_details:
        issue_date = resolved_issue_date_by_invoice.get(invoice_id) or list_issue_date
        result[uc] = _invoice_index_entry(
            month_ref,
            resolved_saldo_by_invoice.get(invoice_id),
            issue_date,
        )

    _save_persisted_saldo_cache()

    if details_cached:
        logger.info(
            "PowerRev: cache de detalhes reaproveitou %d faturas (arquivo: %s).",
            details_cached,
            _DETAIL_CACHE_FILE,
        )

    if pending_invoice_ids:
        logger.info(
            "PowerRev: detalhes em batch-export com lotes de %d IDs "
            "(jobs=%d, concluídos=%d, falhas=%d, pause %.2fs).",
            _BATCH_EXPORT_IDS_PER_JOB,
            batch_jobs_submitted,
            batch_jobs_completed,
            batch_jobs_failed,
            _BATCH_EXPORT_JOB_PAUSE_S,
        )

    logger.info(
        "PowerRev: %d invoices candidatas para %d UCs alvo; %d detalhes pendentes após cache.",
        len(target_invoice_by_uc),
        len(normalized_ucs),
        len(pending_invoice_ids),
    )

    for uc, inv in target_invoice_by_uc.items():
        if uc in result:
            continue
        result[uc] = _invoice_index_entry(
            str(inv.get("nuMesReferencia") or month),
            "",
            _extract_invoice_issue_date(inv),
        )

    logger.info(
        "PowerRev: %d invoices no mês %s, %d UCs alvo, %d UCs com dados enriquecidos "
        "(%d detalhes OK, %d detalhes com falha, %d cache)",
        len(invoices),
        month,
        len(normalized_ucs),
        len(result),
        details_ok,
        details_failed,
        details_cached,
    )
    return result
