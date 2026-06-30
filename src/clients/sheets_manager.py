"""
Google Sheets manager via REST API.
No gspread/google-auth dependency at runtime.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import time
import unicodedata
from dataclasses import dataclass

import requests
from pyasn1.codec.der import decoder
from pyasn1_modules import pem
from pyasn1_modules.rfc2437 import RSAPrivateKey
from pyasn1_modules.rfc5208 import PrivateKeyInfo

from src.config import (
    SPREADSHEET_ID,
    SHEET_TAB_NAME,
    CHUNK_SIZE,
    CHUNK_PAUSE_S,
    get_google_credentials_info,
)
from src.core.field_map import COLUMN_ORDER

logger = logging.getLogger(__name__)

COL_COUNT = len(COLUMN_ORDER)
HEADER_ROWS = 1
DATA_START_ROW = HEADER_ROWS + 1

_SESSION: requests.Session | None = None
_ACCESS_TOKEN: str | None = None
_ACCESS_TOKEN_EXPIRES_AT = 0.0
_TOKEN_REFRESH_MARGIN_S = 5 * 60

_PKCS8_MARKER = ("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----")
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


@dataclass
class WorksheetHandle:
    title: str
    sheet_id: int
    row_count: int
    col_count: int
    spreadsheet_id: str = SPREADSHEET_ID


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def reset_client() -> None:
    global _SESSION, _ACCESS_TOKEN, _ACCESS_TOKEN_EXPIRES_AT
    if _SESSION is not None:
        try:
            _SESSION.close()
        except Exception:
            pass
    _SESSION = None
    _ACCESS_TOKEN = None
    _ACCESS_TOKEN_EXPIRES_AT = 0.0
    logger.info("Google Sheets client resetado.")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _read_rsa_private_key_numbers(private_key_pem: str) -> tuple[int, int]:
    marker_id, key_bytes = pem.readPemBlocksFromFile(
        io.StringIO(private_key_pem), _PKCS8_MARKER  # type: ignore[name-defined]
    )
    if marker_id != 0:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON.private_key nao esta em PKCS8 PEM.")

    key_info, remaining = decoder.decode(key_bytes, asn1Spec=PrivateKeyInfo())
    if remaining != b"":
        raise RuntimeError("Bytes extras inesperados no private_key.")

    pkcs1_octets = key_info.getComponentByName("privateKey").asOctets()
    rsa_key, remaining = decoder.decode(pkcs1_octets, asn1Spec=RSAPrivateKey())
    if remaining != b"":
        raise RuntimeError("Bytes extras inesperados no RSAPrivateKey.")

    modulus = int(rsa_key.getComponentByName("modulus"))
    private_exponent = int(rsa_key.getComponentByName("privateExponent"))
    return modulus, private_exponent


def _rsa_sign_sha256(private_key_pem: str, message: bytes) -> bytes:
    n, d = _read_rsa_private_key_numbers(private_key_pem)
    key_size = (n.bit_length() + 7) // 8

    digest = hashlib.sha256(message).digest()
    digest_info = _SHA256_DIGESTINFO_PREFIX + digest
    ps_len = key_size - len(digest_info) - 3
    if ps_len < 8:
        raise RuntimeError("Tamanho de chave RSA invalido para assinatura PKCS#1 v1.5.")

    em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + digest_info
    em_int = int.from_bytes(em, "big")
    sig_int = pow(em_int, d, n)
    return sig_int.to_bytes(key_size, "big")


def _build_service_account_jwt(creds: dict) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive",
        "aud": creds.get("token_uri", "https://oauth2.googleapis.com/token"),
        "iat": now,
        "exp": now + 3600,
    }

    header_b64 = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = _rsa_sign_sha256(creds["private_key"], signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _refresh_access_token() -> str:
    global _ACCESS_TOKEN, _ACCESS_TOKEN_EXPIRES_AT

    creds = get_google_credentials_info()
    if creds is None:
        raise RuntimeError(
            "Google credentials nao configuradas. "
            "Defina GOOGLE_CREDENTIALS_JSON ou GOOGLE_CREDENTIALS_FILE."
        )

    assertion = _build_service_account_jwt(creds)
    token_uri = creds.get("token_uri", "https://oauth2.googleapis.com/token")

    resp = _get_session().post(
        token_uri,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    _ACCESS_TOKEN = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _ACCESS_TOKEN_EXPIRES_AT = time.time() + expires_in
    return _ACCESS_TOKEN


def _get_access_token(force_refresh: bool = False) -> str:
    if force_refresh:
        return _refresh_access_token()
    if _ACCESS_TOKEN is None:
        return _refresh_access_token()
    if time.time() >= (_ACCESS_TOKEN_EXPIRES_AT - _TOKEN_REFRESH_MARGIN_S):
        return _refresh_access_token()
    return _ACCESS_TOKEN


def _request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
    max_retries: int = 4,
):
    for attempt in range(max_retries):
        token = _get_access_token(force_refresh=False)
        headers = {"Authorization": f"Bearer {token}"}
        request_kwargs = {
            "params": params,
            "headers": headers,
            "timeout": 90,
        }
        if payload is not None:
            request_kwargs["json"] = payload

        resp = _get_session().request(method, url, **request_kwargs)

        if resp.status_code == 401 and attempt < max_retries - 1:
            _get_access_token(force_refresh=True)
            continue

        if resp.status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
            wait = 2 ** attempt
            logger.warning("Sheets API %s, retry em %ds", resp.status_code, wait)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()

    raise RuntimeError("Falha inesperada em request para Sheets API.")


def _quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _range_a1(title: str, a1: str) -> str:
    return f"{_quote_sheet_title(title)}!{a1}"


def _col_to_a1(col: int) -> str:
    letters = ""
    n = col
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheets_base_url(spreadsheet_id: str | None = None) -> str:
    sid = spreadsheet_id or SPREADSHEET_ID
    return f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"


def _normalize_sheet_title(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _get_spreadsheet_meta(*, spreadsheet_id: str | None = None) -> dict:
    return _request_json(
        "GET",
        _sheets_base_url(spreadsheet_id),
        params={"includeGridData": "false"},
    )


def _add_sheet(
    title: str,
    rows: int,
    cols: int,
    *,
    spreadsheet_id: str | None = None,
) -> WorksheetHandle:
    sid = spreadsheet_id or SPREADSHEET_ID
    body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": title,
                        "gridProperties": {"rowCount": rows, "columnCount": cols},
                    }
                }
            }
        ]
    }
    data = _request_json(
        "POST",
        f"{_sheets_base_url(sid)}:batchUpdate",
        payload=body,
    )
    reply = (data.get("replies") or [{}])[0]
    props = (reply.get("addSheet") or {}).get("properties") or {}
    sheet_id = int(props.get("sheetId", 0))
    row_count = int((props.get("gridProperties") or {}).get("rowCount", rows))
    col_count = int((props.get("gridProperties") or {}).get("columnCount", cols))
    return WorksheetHandle(
        title=title,
        sheet_id=sheet_id,
        row_count=row_count,
        col_count=col_count,
        spreadsheet_id=sid,
    )


def get_worksheet(
    tab_name: str | None = None,
    *,
    spreadsheet_id: str | None = None,
    create_if_missing: bool = True,
) -> WorksheetHandle:
    from src.utils.stats import stats

    sid = spreadsheet_id or SPREADSHEET_ID
    target_tab = tab_name or SHEET_TAB_NAME
    meta = _get_spreadsheet_meta(spreadsheet_id=sid)
    stats.sheets_read_requests += 1

    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        if props.get("title") != target_tab:
            continue
        grid = props.get("gridProperties") or {}
        return WorksheetHandle(
            title=str(props.get("title") or target_tab),
            sheet_id=int(props.get("sheetId")),
            row_count=int(grid.get("rowCount", 1000)),
            col_count=int(grid.get("columnCount", 10)),
            spreadsheet_id=sid,
        )

    target_tab_normalized = _normalize_sheet_title(target_tab)
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties") or {}
        title = str(props.get("title") or "")
        if _normalize_sheet_title(title) != target_tab_normalized:
            continue
        grid = props.get("gridProperties") or {}
        return WorksheetHandle(
            title=title,
            sheet_id=int(props.get("sheetId")),
            row_count=int(grid.get("rowCount", 1000)),
            col_count=int(grid.get("columnCount", 10)),
            spreadsheet_id=sid,
        )

    if not create_if_missing:
        raise RuntimeError(f"Aba '{target_tab}' nao encontrada na planilha {sid}.")

    ws = _add_sheet(
        title=target_tab,
        rows=1000,
        cols=10,
        spreadsheet_id=sid,
    )
    stats.sheets_write_requests += 1
    logger.info("Aba '%s' criada.", target_tab)
    return ws


def _values_get(
    ws: WorksheetHandle,
    a1: str,
    *,
    spreadsheet_id: str | None = None,
    value_render_option: str = "UNFORMATTED_VALUE",
) -> list[list[str]]:
    sid = spreadsheet_id or ws.spreadsheet_id
    url = f"{_sheets_base_url(sid)}/values/{_range_a1(ws.title, a1)}"
    data = _request_json("GET", url, params={"valueRenderOption": value_render_option})
    values = data.get("values") or []
    return values if isinstance(values, list) else []


def _values_batch_get(
    ws: WorksheetHandle,
    ranges: list[str],
    *,
    spreadsheet_id: str | None = None,
) -> dict[str, list[list[str]]]:
    sid = spreadsheet_id or ws.spreadsheet_id
    if not ranges:
        return {}

    normalized_ranges: list[str] = []
    for a1 in ranges:
        a1_text = str(a1 or "").strip()
        if not a1_text:
            continue
        normalized_ranges.append(a1_text)

    if not normalized_ranges:
        return {}

    url = f"{_sheets_base_url(sid)}/values:batchGet"
    params = {
        "valueRenderOption": "UNFORMATTED_VALUE",
        "ranges": [_range_a1(ws.title, a1) for a1 in normalized_ranges],
    }
    data = _request_json("GET", url, params=params)
    value_ranges = data.get("valueRanges") or []
    if not isinstance(value_ranges, list):
        value_ranges = []

    result: dict[str, list[list[str]]] = {}
    for idx, requested_range in enumerate(normalized_ranges):
        item = value_ranges[idx] if idx < len(value_ranges) and isinstance(value_ranges[idx], dict) else {}
        values = item.get("values") or []
        result[requested_range] = values if isinstance(values, list) else []

    return result


def _values_update(
    ws: WorksheetHandle,
    a1: str,
    values: list[list[str]],
    *,
    spreadsheet_id: str | None = None,
    value_input_option: str = "RAW",
) -> None:
    sid = spreadsheet_id or ws.spreadsheet_id
    url = f"{_sheets_base_url(sid)}/values/{_range_a1(ws.title, a1)}"
    _request_json(
        "PUT",
        url,
        params={"valueInputOption": value_input_option},
        payload={"values": values},
    )


def _values_batch_update(
    ws: WorksheetHandle,
    data_ranges: list[dict],
    *,
    spreadsheet_id: str | None = None,
) -> None:
    sid = spreadsheet_id or ws.spreadsheet_id
    url = f"{_sheets_base_url(sid)}/values:batchUpdate"
    payload = {"valueInputOption": "RAW", "data": data_ranges}
    _request_json("POST", url, payload=payload)


def _resize_rows(
    ws: WorksheetHandle,
    row_count: int,
    *,
    spreadsheet_id: str | None = None,
) -> None:
    body = {
        "requests": [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.sheet_id, "gridProperties": {"rowCount": row_count}},
                    "fields": "gridProperties.rowCount",
                }
            }
        ]
    }
    sid = spreadsheet_id or ws.spreadsheet_id
    _request_json("POST", f"{_sheets_base_url(sid)}:batchUpdate", payload=body)
    ws.row_count = row_count


def remove_all_protected_ranges(spreadsheet_id: str) -> int:
    """Remove every protected range from every tab in a spreadsheet."""
    sid = str(spreadsheet_id or "").strip()
    if not sid:
        return 0

    meta = _request_json(
        "GET",
        _sheets_base_url(sid),
        params={
            "includeGridData": "false",
            "fields": (
                "sheets(properties(sheetId,title),"
                "protectedRanges(protectedRangeId,description,warningOnly,range))"
            ),
        },
    )

    protected_range_ids: list[int] = []
    for sheet in meta.get("sheets", []):
        for pr in sheet.get("protectedRanges", []) or []:
            protected_range_id = pr.get("protectedRangeId")
            if protected_range_id is not None:
                protected_range_ids.append(int(protected_range_id))

    if not protected_range_ids:
        return 0

    body = {
        "requests": [
            {"deleteProtectedRange": {"protectedRangeId": protected_range_id}}
            for protected_range_id in protected_range_ids
        ]
    }
    _request_json("POST", f"{_sheets_base_url(sid)}:batchUpdate", payload=body)
    logger.info(
        "%d intervalos protegidos removidos da planilha %s.",
        len(protected_range_ids),
        sid,
    )
    return len(protected_range_ids)


def ensure_headers(ws: WorksheetHandle) -> None:
    """Compatibility no-op: headers are not protected by the sync."""
    del ws


def read_all_rows(
    ws: WorksheetHandle,
    *,
    spreadsheet_id: str | None = None,
) -> list[list[str]]:
    from src.utils.stats import stats

    all_data = _values_get(ws, "A1:ZZ", spreadsheet_id=spreadsheet_id)
    stats.sheets_read_requests += 1
    return all_data[HEADER_ROWS:] if len(all_data) > HEADER_ROWS else []


def write_all_rows(
    ws: WorksheetHandle,
    rows: list[list],
    *,
    col_count: int | None = None,
) -> None:
    """Reescreve linhas de dados (A..), preservando as duas primeiras linhas."""
    from src.utils.stats import stats

    width = int(col_count or COL_COUNT)
    end_col = _col_to_a1(width)
    existing = _values_get(ws, "A1:ZZ")
    stats.sheets_read_requests += 1
    data_rows_count = len(existing) - HEADER_ROWS if len(existing) > HEADER_ROWS else 0

    needed = len(rows) + HEADER_ROWS if rows else HEADER_ROWS
    if ws.row_count < needed:
        _resize_rows(ws, needed)
        stats.sheets_write_requests += 1

    rows_to_clear = max(data_rows_count, needed - HEADER_ROWS)
    if rows_to_clear > 0:
        blank_row = [""] * width
        for i in range(0, rows_to_clear, CHUNK_SIZE):
            chunk_size = min(CHUNK_SIZE, rows_to_clear - i)
            blank_chunk = [blank_row] * chunk_size
            start_row = i + DATA_START_ROW
            _values_update(ws, f"A{start_row}:{end_col}{start_row + chunk_size - 1}", blank_chunk)
            stats.sheets_write_requests += 1
            if i + CHUNK_SIZE < rows_to_clear:
                time.sleep(CHUNK_PAUSE_S)

    if not rows:
        return

    for i in range(0, len(rows), CHUNK_SIZE):
        raw_chunk = rows[i:i + CHUNK_SIZE]
        chunk: list[list] = []
        for row in raw_chunk:
            r = list(row or [])
            if len(r) < width:
                r.extend([""] * (width - len(r)))
            chunk.append(r[:width])
        start_row = i + DATA_START_ROW
        _values_update(ws, f"A{start_row}:{end_col}{start_row + len(chunk) - 1}", chunk)
        cells = sum(len(r) for r in chunk)
        stats.sheets_write_requests += 1
        stats.sheets_cells_written += cells
        logger.info(
            "Chunk escrito: linhas %d-%d (%d rows, %d cells)",
            start_row,
            start_row + len(chunk) - 1,
            len(chunk),
            cells,
        )
        if i + CHUNK_SIZE < len(rows):
            time.sleep(CHUNK_PAUSE_S)


def update_rows_in_place(
    ws: WorksheetHandle,
    updates: dict[int, list],
    *,
    col_count: int | None = None,
) -> None:
    """Atualiza linhas especificas (A..)."""
    from src.utils.stats import stats

    if not updates:
        return

    width = int(col_count or COL_COUNT)
    end_col = _col_to_a1(width)
    max_sheet_row = max(updates)
    if ws.row_count < max_sheet_row:
        _resize_rows(ws, max_sheet_row)
        stats.sheets_write_requests += 1

    batch: list[dict] = []

    def _norm_row(values: list | None) -> list:
        raw = list(values or [])
        if len(raw) < width:
            raw.extend([""] * (width - len(raw)))
        return [v if v is not None else "" for v in raw[:width]]

    for sheet_row, row_data in updates.items():
        range_name = _range_a1(ws.title, f"A{sheet_row}:{end_col}{sheet_row}")
        batch.append({"range": range_name, "values": [_norm_row(row_data)]})

    for i in range(0, len(batch), CHUNK_SIZE):
        chunk = batch[i:i + CHUNK_SIZE]
        _values_batch_update(ws, chunk)
        cells = sum(len(item["values"][0]) for item in chunk)
        stats.sheets_write_requests += 1
        stats.sheets_cells_written += cells
        if i + CHUNK_SIZE < len(batch):
            time.sleep(CHUNK_PAUSE_S)


def sync_rows_in_place(
    ws: WorksheetHandle,
    rows: list[list],
    *,
    col_count: int | None = None,
    preserve_existing_extras: bool = False,
    spreadsheet_id: str | None = None,
) -> int:
    """
    Sincroniza linhas de dados por diff (A..), preservando cabeÃ§alho.

    - Atualiza apenas linhas alteradas.
    - Limpa linhas excedentes que existiam antes.
    - Retorna quantidade de linhas efetivamente escritas.
    """
    from src.utils.stats import stats

    sid = spreadsheet_id or ws.spreadsheet_id
    width = int(col_count or COL_COUNT)
    end_col = _col_to_a1(width)

    existing = _values_get(ws, f"A1:{end_col}", spreadsheet_id=sid)
    stats.sheets_read_requests += 1
    existing_rows = existing[HEADER_ROWS:] if len(existing) > HEADER_ROWS else []
    existing_count = len(existing_rows)
    target_count = len(rows)
    max_count = max(existing_count, target_count)

    needed_total_rows = max(HEADER_ROWS, HEADER_ROWS + target_count)
    if ws.row_count < needed_total_rows:
        _resize_rows(ws, needed_total_rows, spreadsheet_id=sid)
        stats.sheets_write_requests += 1

    def _norm_row(values: list | None, *, existing_row: list | None = None, is_target: bool = False) -> list:
        raw = list(values or [])
        if (
            is_target
            and preserve_existing_extras
            and existing_row is not None
            and len(raw) < width
            and any(str(v).strip() for v in raw)
        ):
            existing_norm = list(existing_row or [])
            if len(existing_norm) < width:
                existing_norm.extend([""] * (width - len(existing_norm)))
            raw.extend(existing_norm[len(raw):width])
        if len(raw) < width:
            raw.extend([""] * (width - len(raw)))
        return [v if v is not None else "" for v in raw[:width]]

    updates_batch: list[dict] = []
    changed_rows = 0
    for idx in range(max_count):
        existing_row_raw = existing_rows[idx] if idx < existing_count else []
        current_row = _norm_row(existing_row_raw)
        target_row = _norm_row(
            rows[idx] if idx < target_count else [],
            existing_row=existing_row_raw,
            is_target=True,
        )
        if current_row == target_row:
            continue

        sheet_row = DATA_START_ROW + idx
        range_name = _range_a1(ws.title, f"A{sheet_row}:{end_col}{sheet_row}")
        updates_batch.append({"range": range_name, "values": [target_row]})
        changed_rows += 1

    if not updates_batch:
        return 0

    for i in range(0, len(updates_batch), CHUNK_SIZE):
        chunk = updates_batch[i:i + CHUNK_SIZE]
        _values_batch_update(ws, chunk, spreadsheet_id=sid)
        cells = sum(len(item["values"][0]) for item in chunk)
        stats.sheets_write_requests += 1
        stats.sheets_cells_written += cells
        if i + CHUNK_SIZE < len(updates_batch):
            time.sleep(CHUNK_PAUSE_S)

    return changed_rows

