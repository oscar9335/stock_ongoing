from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import mimetypes
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
CACHE_DIR = ROOT / ".cache" / "http"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = "Taiwan Stock Volume Chart"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 "
    f"{APP_NAME}/1.0"
)

TWSE_HISTORY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_DAILY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_HISTORY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
TPEX_HISTORY_REFERER = "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html"
MIS_REGULAR_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_ODD_URL = "https://mis.twse.com.tw/stock/api/getOddInfo.jsp"

JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
HTTP_MEMORY_CACHE: dict[str, tuple[float, str]] = {}
HTTP_LOCK = threading.Lock()

SSL_CONTEXT = ssl.create_default_context()
if hasattr(ssl, "VERIFY_X509_STRICT"):
    SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Range:
    start: date
    end: date


def today_local() -> date:
    return datetime.now().date()


def clamp_months(raw: str | None) -> int:
    try:
        months = int(raw or "6")
    except ValueError:
        months = 6
    return max(1, min(months, 24))


def range_for_months(months: int) -> Range:
    end = today_local()
    month = end.month - months
    year = end.year
    while month <= 0:
        month += 12
        year -= 1
    start = date(year, month, 1)
    return Range(start=start, end=end)


def month_starts(rng: Range) -> list[date]:
    current = date(rng.start.year, rng.start.month, 1)
    last = date(rng.end.year, rng.end.month, 1)
    months: list[date] = []
    while current <= last:
        months.append(current)
        year = current.year + (1 if current.month == 12 else 0)
        month = 1 if current.month == 12 else current.month + 1
        current = date(year, month, 1)
    return months


def weekdays(rng: Range) -> list[date]:
    days: list[date] = []
    current = rng.start
    while current <= rng.end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def ymd_slash(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def iso(d: date) -> str:
    return d.isoformat()


def roc_slash(d: date) -> str:
    return f"{d.year - 1911:03d}/{d.month:02d}/{d.day:02d}"


def roc_compact_to_date(value: str) -> date | None:
    text = value.strip()
    if not re.fullmatch(r"\d{7}", text):
        return None
    year = int(text[:3]) + 1911
    month = int(text[3:5])
    day = int(text[5:7])
    try:
        return date(year, month, day)
    except ValueError:
        return None


def roc_slash_to_date(value: str) -> date | None:
    match = re.fullmatch(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*", value)
    if not match:
        return None
    year = int(match.group(1)) + 1911
    month = int(match.group(2))
    day = int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("+", "").strip()
    if not text or text in {"---", "--", "----", "除權息"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_number(value)
    if number is None or math.isnan(number):
        return None
    return int(round(number))


def cache_key(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def fetch_text(url: str, ttl_seconds: int = 300, timeout: int = 20) -> str:
    now = time.time()
    with HTTP_LOCK:
        cached = HTTP_MEMORY_CACHE.get(url)
        if cached and cached[0] > now:
            return cached[1]

    path = cache_key(url)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("expires", 0) > now:
                text = payload["text"]
                with HTTP_LOCK:
                    HTTP_MEMORY_CACHE[url] = (payload["expires"], text)
                return text
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    raw: bytes | None = None
    last_error: Exception | None = None
    for _ in range(3):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            raise DataError(f"HTTP {exc.code} from {url}") from exc
        except http.client.IncompleteRead as exc:
            last_error = exc
            time.sleep(0.5)
        except urllib.error.URLError as exc:
            last_error = exc
            time.sleep(0.5)

    if raw is None:
        if isinstance(last_error, urllib.error.URLError):
            raise DataError(f"無法連線到資料來源：{last_error.reason}") from last_error
        raise DataError(f"資料來源讀取不完整：{last_error}") from last_error

    text = raw.decode("utf-8-sig", errors="replace")
    expires = now + ttl_seconds
    with HTTP_LOCK:
        HTTP_MEMORY_CACHE[url] = (expires, text)
    try:
        path.write_text(
            json.dumps({"expires": expires, "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return text


def fetch_json(url: str, ttl_seconds: int = 300, timeout: int = 20) -> Any:
    text = fetch_text(url, ttl_seconds=ttl_seconds, timeout=timeout).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError("資料來源回傳不是有效 JSON") from exc


def fetch_form_text(
    url: str,
    form: dict[str, str],
    ttl_seconds: int = 300,
    timeout: int = 20,
    referer: str | None = None,
) -> str:
    encoded = urllib.parse.urlencode(form)
    cache_id = f"POST {url} {encoded}"
    now = time.time()
    with HTTP_LOCK:
        cached = HTTP_MEMORY_CACHE.get(cache_id)
        if cached and cached[0] > now:
            return cached[1]

    path = cache_key(cache_id)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("expires", 0) > now:
                text = payload["text"]
                with HTTP_LOCK:
                    HTTP_MEMORY_CACHE[cache_id] = (payload["expires"], text)
                return text
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/javascript,*/*;q=0.01",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        "Connection": "close",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": urllib.parse.urlsplit(url).scheme + "://" + urllib.parse.urlsplit(url).netloc,
    }
    if referer:
        headers["Referer"] = referer

    raw: bytes | None = None
    last_error: Exception | None = None
    for _ in range(3):
        request = urllib.request.Request(
            url,
            data=encoded.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            raise DataError(f"HTTP {exc.code} from {url}") from exc
        except http.client.IncompleteRead as exc:
            last_error = exc
            time.sleep(0.5)
        except urllib.error.URLError as exc:
            last_error = exc
            time.sleep(0.5)

    if raw is None:
        if isinstance(last_error, urllib.error.URLError):
            raise DataError(f"無法連線到資料來源：{last_error.reason}") from last_error
        raise DataError(f"資料來源讀取不完整：{last_error}") from last_error

    text = raw.decode("utf-8-sig", errors="replace")
    expires = now + ttl_seconds
    with HTTP_LOCK:
        HTTP_MEMORY_CACHE[cache_id] = (expires, text)
    try:
        path.write_text(
            json.dumps({"expires": expires, "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return text


def fetch_form_json(
    url: str,
    form: dict[str, str],
    ttl_seconds: int = 300,
    timeout: int = 20,
    referer: str | None = None,
) -> Any:
    text = fetch_form_text(
        url,
        form=form,
        ttl_seconds=ttl_seconds,
        timeout=timeout,
        referer=referer,
    ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError("資料來源回傳不是有效 JSON") from exc


def twse_name_from_title(title: str) -> str:
    # Example: 115年05月 2330 台積電           各日成交資訊
    match = re.search(r"\d{3}年\d{2}月\s+\S+\s+(.+?)\s+各日成交資訊", title)
    return match.group(1).strip() if match else ""


def normalize_rows(rows: list[dict[str, Any]], rng: Range) -> list[dict[str, Any]]:
    filtered = [row for row in rows if rng.start <= date.fromisoformat(row["date"]) <= rng.end]
    by_date = {row["date"]: row for row in filtered}
    return sorted(by_date.values(), key=lambda item: item["date"])


def fetch_twse_history(symbol: str, months: int) -> dict[str, Any]:
    rng = range_for_months(months)
    rows: list[dict[str, Any]] = []
    name = ""
    source_notes: list[str] = []

    for month_start in month_starts(rng):
        query = urllib.parse.urlencode(
            {"response": "json", "date": ymd(month_start), "stockNo": symbol}
        )
        data = fetch_json(f"{TWSE_HISTORY_URL}?{query}", ttl_seconds=3600)
        if data.get("stat") != "OK" or not isinstance(data.get("data"), list):
            continue
        name = name or twse_name_from_title(str(data.get("title", "")))
        for note in data.get("notes", []) or []:
            if note not in source_notes:
                source_notes.append(note)
        for item in data["data"]:
            if len(item) < 9:
                continue
            row_date = roc_slash_to_date(str(item[0]))
            open_price = parse_number(item[3])
            high = parse_number(item[4])
            low = parse_number(item[5])
            close = parse_number(item[6])
            volume = parse_int(item[1])
            if not row_date or None in {open_price, high, low, close, volume}:
                continue
            rows.append(
                {
                    "date": iso(row_date),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volumeShares": volume,
                    "amount": parse_int(item[2]),
                    "transactions": parse_int(item[8]),
                    "change": parse_number(item[7]),
                    "source": "TWSE 盤後",
                    "isRealtime": False,
                }
            )

    rows = normalize_rows(rows, rng)
    if not rows:
        raise DataError(f"TWSE 查無 {symbol} 在最近 {months} 個月的日成交資料")

    return {
        "market": "twse",
        "symbol": symbol,
        "name": name,
        "rows": rows,
        "notes": source_notes,
        "officialVolumeUnit": "share",
    }


def tpex_month_query_date(month_start: date) -> str:
    return ymd_slash(date(month_start.year, month_start.month, 1))


def tpex_value_by_field(fields: list[str], row: list[Any], *names: str) -> Any:
    normalized = {field.replace(" ", ""): idx for idx, field in enumerate(fields)}
    for name in names:
        index = normalized.get(name.replace(" ", ""))
        if index is not None and index < len(row):
            return row[index]
    return None


def parse_tpex_month_row(fields: list[str], raw: list[Any]) -> dict[str, Any] | None:
    row_date = roc_slash_to_date(str(tpex_value_by_field(fields, raw, "日 期", "日期") or ""))
    open_price = parse_number(tpex_value_by_field(fields, raw, "開盤"))
    high = parse_number(tpex_value_by_field(fields, raw, "最高"))
    low = parse_number(tpex_value_by_field(fields, raw, "最低"))
    close = parse_number(tpex_value_by_field(fields, raw, "收盤"))
    volume_lots = parse_int(tpex_value_by_field(fields, raw, "成交張數"))
    if not row_date or None in {open_price, high, low, close, volume_lots}:
        return None
    amount_thousand = parse_int(tpex_value_by_field(fields, raw, "成交仟元"))
    return {
        "date": iso(row_date),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volumeShares": volume_lots * 1000,
        "volumeSharesEstimated": True,
        "regularVolumeLots": volume_lots,
        "amount": amount_thousand * 1000 if amount_thousand is not None else None,
        "transactions": parse_int(tpex_value_by_field(fields, raw, "筆數")),
        "change": parse_number(tpex_value_by_field(fields, raw, "漲跌")),
        "source": "TPEx 個股月資料",
        "isRealtime": False,
    }


def fetch_tpex_month(symbol: str, month_start: date) -> tuple[list[dict[str, Any]], str, list[str]]:
    data = fetch_form_json(
        TPEX_HISTORY_URL,
        {
            "code": symbol,
            "date": tpex_month_query_date(month_start),
            "response": "json",
        },
        ttl_seconds=3600,
        timeout=25,
        referer=f"{TPEX_HISTORY_REFERER}?code={urllib.parse.quote(symbol)}",
    )
    if not isinstance(data, dict):
        return [], "", []
    if data.get("stat") and data.get("stat") != "ok":
        return [], str(data.get("name", "")).strip(), []

    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        return [], str(data.get("name", "")).strip(), []
    table = tables[0]
    fields = table.get("fields")
    table_rows = table.get("data")
    if not isinstance(fields, list) or not isinstance(table_rows, list):
        return [], str(data.get("name", "")).strip(), []

    rows = []
    for item in table_rows:
        if not isinstance(item, list):
            continue
        row = parse_tpex_month_row([str(field) for field in fields], item)
        if row:
            rows.append(row)

    notes = [str(note) for note in table.get("notes", []) or []]
    return rows, str(data.get("name", "")).strip(), notes


def apply_tpex_latest_exact_volume(symbol: str, rows: list[dict[str, Any]]) -> bool:
    data = fetch_json(f"{TPEX_DAILY_URL}?l=zh-tw", ttl_seconds=1800, timeout=25)
    if not isinstance(data, list):
        return False
    latest = next(
        (item for item in data if str(item.get("SecuritiesCompanyCode", "")).strip() == symbol),
        None,
    )
    if not latest:
        return False
    latest_date = roc_compact_to_date(str(latest.get("Date", "")))
    latest_volume = parse_int(latest.get("TradingShares"))
    if not latest_date or latest_volume is None:
        return False
    latest_iso = iso(latest_date)
    for row in rows:
        if row["date"] == latest_iso:
            row["volumeShares"] = latest_volume
            row["volumeSharesEstimated"] = False
            row["amount"] = parse_int(latest.get("TransactionAmount")) or row.get("amount")
            row["transactions"] = parse_int(latest.get("TransactionNumber")) or row.get("transactions")
            row["source"] = "TPEx OpenAPI 盤後"
            return True
    return False


def fetch_tpex_history(symbol: str, months: int) -> dict[str, Any]:
    capped_months = min(months, 12)
    rng = range_for_months(capped_months)
    rows: list[dict[str, Any]] = []
    name = ""
    source_notes: list[str] = []

    for month_start in month_starts(rng):
        monthly_rows, month_name, month_notes = fetch_tpex_month(symbol, month_start)
        name = name or month_name
        rows.extend(monthly_rows)
        for note in month_notes:
            if note not in source_notes:
                source_notes.append(note)

    rows = normalize_rows(rows, rng)
    if not rows:
        raise DataError(f"TPEx 查無 {symbol} 在最近 {capped_months} 個月的日成交資料")

    latest_exact = False
    try:
        latest_exact = apply_tpex_latest_exact_volume(symbol, rows)
    except DataError:
        latest_exact = False

    notes = [
        "TPEx 個股歷史頁提供「成交張數」，本系統換算為成交股數（成交張數 * 1000）；最近一個 TPEx OpenAPI 盤後交易日若可取得，會改用 OpenAPI 的精準 TradingShares。",
    ]
    if latest_exact:
        notes.append("最近一個 TPEx OpenAPI 盤後交易日已使用精準成交股數。")
    if months > capped_months:
        notes.append("目前上櫃歷史查詢先限制 12 個月，避免一次對官方端點發出過多請求。")
    notes.extend(source_notes)

    return {
        "market": "tpex",
        "symbol": symbol,
        "name": name,
        "rows": rows,
        "notes": notes,
        "officialVolumeUnit": "share",
    }


def market_channel(market: str, symbol: str) -> str:
    prefix = "otc" if market == "tpex" else "tse"
    return f"{prefix}_{symbol}.tw"


def fetch_mis_snapshot(symbol: str, market: str, include_odd_lot: bool) -> dict[str, Any] | None:
    channel = market_channel(market, symbol)
    regular_query = urllib.parse.urlencode({"ex_ch": channel, "json": "1", "delay": "0"})
    regular = fetch_json(f"{MIS_REGULAR_URL}?{regular_query}", ttl_seconds=5, timeout=15)
    regular_items = regular.get("msgArray") if isinstance(regular, dict) else None
    if not regular_items:
        return None
    raw = regular_items[0]

    trade_date = None
    if re.fullmatch(r"\d{8}", str(raw.get("d", ""))):
        trade_date = datetime.strptime(str(raw["d"]), "%Y%m%d").date()

    volume_lots = parse_int(raw.get("v"))
    latest = parse_number(raw.get("z"))
    open_price = parse_number(raw.get("o"))
    high = parse_number(raw.get("h"))
    low = parse_number(raw.get("l"))
    previous_close = parse_number(raw.get("y"))

    snapshot: dict[str, Any] = {
        "date": iso(trade_date) if trade_date else None,
        "time": str(raw.get("t", "")).strip(),
        "name": str(raw.get("n", "")).strip(),
        "open": open_price,
        "high": high,
        "low": low,
        "last": latest,
        "previousClose": previous_close,
        "regularVolumeLots": volume_lots,
        "regularVolumeShares": volume_lots * 1000 if volume_lots is not None else None,
        "oddLotVolumeShares": None,
        "estimatedVolumeShares": volume_lots * 1000 if volume_lots is not None else None,
        "source": "TWSE MIS 即時",
    }

    if include_odd_lot:
        odd_query = urllib.parse.urlencode({"ex_ch": channel, "json": "1", "delay": "0"})
        odd = fetch_json(f"{MIS_ODD_URL}?{odd_query}", ttl_seconds=5, timeout=15)
        odd_items = odd.get("msgArray") if isinstance(odd, dict) else None
        if odd_items:
            odd_raw = odd_items[0]
            odd_volume = parse_int(odd_raw.get("v"))
            snapshot["oddLotVolumeShares"] = odd_volume
            if snapshot["regularVolumeShares"] is not None and odd_volume is not None:
                snapshot["estimatedVolumeShares"] = snapshot["regularVolumeShares"] + odd_volume

    return snapshot


def append_realtime_row(payload: dict[str, Any], include_odd_lot: bool) -> dict[str, Any]:
    market = payload["market"]
    symbol = payload["symbol"]
    snapshot = fetch_mis_snapshot(symbol, market, include_odd_lot)
    payload["realtime"] = snapshot
    if not snapshot:
        payload.setdefault("warnings", []).append("盤中即時資料目前無法取得。")
        return payload

    required = ["date", "open", "high", "low", "last", "estimatedVolumeShares"]
    if any(snapshot.get(key) is None for key in required):
        if snapshot.get("last") is None:
            payload.setdefault("warnings", []).append(
                "MIS 目前未回傳當盤成交價，不使用昨日收盤價替代現價；本次未併入盤中 K 線。"
            )
        else:
            payload.setdefault("warnings", []).append("盤中即時資料尚未形成完整 OHLC，未併入 K 線。")
        return payload

    realtime_row = {
        "date": snapshot["date"],
        "open": snapshot["open"],
        "high": snapshot["high"],
        "low": snapshot["low"],
        "close": snapshot["last"],
        "volumeShares": snapshot["estimatedVolumeShares"],
        "amount": None,
        "transactions": None,
        "change": None,
        "source": "MIS 盤中估算",
        "isRealtime": True,
    }
    rows = [row for row in payload["rows"] if row["date"] != realtime_row["date"]]
    rows.append(realtime_row)
    rows.sort(key=lambda item: item["date"])
    payload["rows"] = rows
    return payload


def load_twse_symbols() -> list[dict[str, str]]:
    data = fetch_json(TWSE_DAILY_ALL_URL, ttl_seconds=1800, timeout=25)
    symbols: list[dict[str, str]] = []
    if not isinstance(data, list):
        return symbols
    for row in data:
        code = str(row.get("Code", "")).strip()
        name = str(row.get("Name", "")).strip()
        if code and name:
            symbols.append({"symbol": code, "name": name, "market": "twse"})
    return symbols


def load_tpex_symbols() -> list[dict[str, str]]:
    data = fetch_json(f"{TPEX_DAILY_URL}?l=zh-tw", ttl_seconds=1800, timeout=25)
    symbols: list[dict[str, str]] = []
    if not isinstance(data, list):
        return symbols
    for row in data:
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        name = str(row.get("CompanyName", "")).strip()
        if code and name:
            symbols.append({"symbol": code, "name": name, "market": "tpex"})
    return symbols


def search_symbols(query: str, market: str = "auto", limit: int = 30) -> list[dict[str, str]]:
    q = query.strip().lower()
    if not q:
        return []
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    if market in {"auto", "twse"}:
        try:
            candidates.extend(load_twse_symbols())
        except DataError as exc:
            errors.append(str(exc))
    if market in {"auto", "tpex"}:
        try:
            candidates.extend(load_tpex_symbols())
        except DataError as exc:
            errors.append(str(exc))

    def score(item: dict[str, str]) -> tuple[int, str]:
        symbol = item["symbol"].lower()
        name = item["name"].lower()
        if symbol == q or name == q:
            return (0, symbol)
        if symbol.startswith(q):
            return (1, symbol)
        if name.startswith(q):
            return (2, symbol)
        return (3, symbol)

    filtered = [
        item
        for item in candidates
        if q in item["symbol"].lower() or q in item["name"].lower()
    ]
    filtered.sort(key=score)
    for item in filtered:
        item["label"] = f"{item['symbol']} {item['name']} ({'上市' if item['market'] == 'twse' else '上櫃'})"
    if not filtered and errors:
        raise DataError("；".join(errors))
    return filtered[:limit]


def resolve_symbol(query: str, market: str) -> tuple[str, str]:
    text = query.strip()
    if not text:
        raise DataError("請輸入股票代號或名稱")
    if re.fullmatch(r"[0-9A-Za-z]{2,8}", text):
        return text.upper(), market

    matches = search_symbols(text, market=market, limit=1)
    if not matches:
        raise DataError(f"找不到符合「{text}」的股票")
    match = matches[0]
    return match["symbol"], match["market"] if market == "auto" else market


def history_response(params: dict[str, list[str]]) -> dict[str, Any]:
    query = params.get("symbol", [""])[0]
    requested_market = params.get("market", ["auto"])[0].lower()
    if requested_market not in {"auto", "twse", "tpex"}:
        requested_market = "auto"
    months = clamp_months(params.get("months", ["6"])[0])
    include_realtime = params.get("realtime", ["1"])[0] == "1"
    include_odd_lot = params.get("oddlot", ["1"])[0] == "1"

    symbol, resolved_market = resolve_symbol(query, requested_market)

    errors: list[str] = []
    attempts = ["twse", "tpex"] if resolved_market == "auto" else [resolved_market]
    payload: dict[str, Any] | None = None
    for market in attempts:
        try:
            payload = fetch_twse_history(symbol, months) if market == "twse" else fetch_tpex_history(symbol, months)
            break
        except DataError as exc:
            errors.append(str(exc))

    if payload is None:
        raise DataError("；".join(errors) or "查無資料")

    payload.setdefault("warnings", [])
    if errors:
        payload["warnings"].extend(errors)
    if include_realtime:
        try:
            append_realtime_row(payload, include_odd_lot)
        except DataError as exc:
            payload["warnings"].append(str(exc))

    payload["query"] = query
    payload["months"] = months
    payload["volumePolicy"] = (
        "上市盤後正式資料直接使用 TWSE 官方成交股數；上櫃歷史資料依 TPEx 個股頁的成交張數換算為股數，"
        "最近一個 TPEx OpenAPI 盤後交易日可取得時改用精準 TradingShares；盤中即時量以整股累積張數 * 1000 "
        "+ 盤中零股累積股數估算，並標記為 MIS 盤中估算。"
    )
    return payload


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path == "/":
            return str(PUBLIC_DIR / "index.html")
        safe = Path(os.path.normpath(urllib.parse.unquote(path).lstrip("/")))
        return str(PUBLIC_DIR / safe)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_HEADERS["Content-Type"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            params = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == "/api/history":
                    self.send_json(history_response(params))
                    return
                if parsed.path == "/api/search":
                    query = params.get("q", [""])[0]
                    market = params.get("market", ["auto"])[0].lower()
                    if market not in {"auto", "twse", "tpex"}:
                        market = "auto"
                    self.send_json({"items": search_symbols(query, market=market)})
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "API not found")
            except DataError as exc:
                self.send_json({"error": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": f"伺服器錯誤：{exc}"}, status=500)
            return

        if parsed.path != "/":
            mime, _ = mimetypes.guess_type(parsed.path)
            if mime:
                self.extensions_map[Path(parsed.path).suffix] = mime
        super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"{APP_NAME} running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
