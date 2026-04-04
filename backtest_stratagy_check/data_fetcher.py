# data_fetcher.py
# 台股資料取得模組 — TWSE / TPEX API 呼叫與快取管理

import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)

# 連續請求失敗計數（模組層級，跨呼叫共享）
_consecutive_failures = 0

# ============================================================
# 快取工具函式
# ============================================================

def _cache_path(filename: str) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, filename)


def _load_cache(filename: str, ttl: int) -> Optional[object]:
    """讀取 JSON 快取，若超過 TTL 則回傳 None。"""
    path = _cache_path(filename)
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > ttl:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(filename: str, data: object) -> None:
    """將資料寫入 JSON 快取。"""
    path = _cache_path(filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("快取寫入失敗 %s: %s", path, e)


# ============================================================
# HTTP 工具函式
# ============================================================

def _get(url: str, params: Optional[Dict] = None, delay: float = None) -> Optional[dict]:
    """
    HTTP GET，含以下保護機制：
    - 隨機 Jitter：每次延遲 API_DELAY + random(0, 0.4) 秒，避免固定節奏被識別
    - 自動重試：失敗後指數退讓（2s → 4s → 8s），最多重試 3 次
    - 連續失敗退讓：連續失敗 5 次時暫停 30 秒再繼續，保護 IP 不被封鎖
    """
    global _consecutive_failures

    if delay is None:
        delay = config.API_DELAY

    max_retries = 3
    backoff_base = 2.0

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url, params=params,
                headers=config.REQUEST_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()

            # 成功：加入隨機 jitter 再延遲，讓請求間隔不規律
            jitter = random.uniform(0.0, 0.4)
            time.sleep(delay + jitter)

            data = resp.json()
            _consecutive_failures = 0   # 重置連續失敗計數
            return data

        except requests.RequestException as e:
            logger.warning("HTTP 請求失敗（第 %d/%d 次）%s: %s", attempt, max_retries, url, e)
        except ValueError as e:
            logger.warning("JSON 解析失敗（第 %d/%d 次）%s: %s", attempt, max_retries, url, e)

        # 還有重試機會：指數退讓後再試
        if attempt < max_retries:
            wait = backoff_base ** attempt   # 2s, 4s
            logger.info("退讓等待 %.0f 秒後重試...", wait)
            time.sleep(wait)

    # 所有重試均失敗
    _consecutive_failures += 1
    logger.warning("請求最終失敗（連續失敗 %d 次）: %s", _consecutive_failures, url)

    if _consecutive_failures >= 5:
        logger.warning("連續失敗達 %d 次，暫停 30 秒保護 IP...", _consecutive_failures)
        time.sleep(30)
        _consecutive_failures = 0

    return None


# ============================================================
# 資料清洗工具函式
# ============================================================

def _to_float(value: str) -> Optional[float]:
    """將台灣數字字串（含千分位逗號）轉為 float，失敗回傳 None。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if s in ("--", "-", "", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _roc_to_date(roc_str: str) -> Optional[date]:
    """民國年日期字串 (YYY/MM/DD 或 YYY年MM月DD日) 轉為西元 date。"""
    s = str(roc_str).strip()
    # 格式：113/04/01
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                year = int(parts[0]) + 1911
                month = int(parts[1])
                day = int(parts[2])
                return date(year, month, day)
            except (ValueError, OverflowError):
                pass
    # 格式：113年04月01日
    if "年" in s:
        try:
            s2 = s.replace("年", "/").replace("月", "/").replace("日", "")
            parts = s2.split("/")
            year = int(parts[0]) + 1911
            month = int(parts[1])
            day = int(parts[2])
            return date(year, month, day)
        except (ValueError, OverflowError):
            pass
    return None


def _gregorian_to_roc(d: date) -> str:
    """西元 date 轉為民國年查詢字串 (YYY/MM)，供 TPEX API 使用。"""
    roc_year = d.year - 1911
    return f"{roc_year}/{d.month:02d}"


# ============================================================
# 全市場每日資料
# ============================================================

def fetch_twse_all_stocks(trade_date: Optional[str] = None):
    """
    取得 TWSE 上市股票當日全市場資料。

    注意：STOCK_DAY_ALL 不支援指定歷史日期，永遠回傳最新交易日資料。
          trade_date 參數無效，僅保留相容性。

    Returns:
        (stocks: List[Dict], actual_date_iso: str)
        stocks 每筆包含: code, name, open, high, low, close, change, volume,
                        amount, prev_close, gain_pct, exchange
        actual_date_iso: 資料實際交易日 (YYYY-MM-DD)，空字串表示無法解析
    """
    params = {"response": "json"}

    data = _get(config.TWSE_DAY_ALL_URL, params)
    if not data or data.get("stat") != "OK":
        logger.warning("TWSE 全市場資料取得失敗（stat != OK）")
        return [], ""

    # 解析 API 回傳的實際日期（格式 YYYYMMDD）
    raw_date = data.get("date", "")
    actual_date_iso = ""
    if raw_date and len(raw_date) == 8:
        try:
            from datetime import datetime as _dt
            actual_date_iso = _dt.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

    raw_fields = data.get("fields", [])
    raw_data = data.get("data", [])
    if not raw_data:
        return [], actual_date_iso

    # TWSE STOCK_DAY_ALL 標準欄位
    default_fields = [
        "證券代號", "名稱", "成交股數", "成交金額",
        "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數"
    ]
    fields = raw_fields if raw_fields else default_fields

    results = []
    for row in raw_data:
        if len(row) < 9:
            continue
        row_dict = dict(zip(fields, row))

        code = str(row_dict.get("證券代號", "")).strip()
        name = str(row_dict.get("名稱", "")).strip()

        # 只保留純 4 碼數字（排除 ETF、債券等）
        if not code.isdigit() or len(code) != 4:
            continue

        close  = _to_float(row_dict.get("收盤價"))
        change = _to_float(row_dict.get("漲跌價差"))
        volume = _to_float(row_dict.get("成交股數"))
        amount = _to_float(row_dict.get("成交金額"))
        open_p = _to_float(row_dict.get("開盤價"))
        high   = _to_float(row_dict.get("最高價"))
        low    = _to_float(row_dict.get("最低價"))

        if close is None or change is None:
            continue

        prev_close = round(close - change, 2)
        gain_pct   = round(change / prev_close * 100, 2) if prev_close != 0 else 0.0

        results.append({
            "code":       code,
            "name":       name,
            "open":       open_p,
            "high":       high,
            "low":        low,
            "close":      close,
            "change":     change,
            "volume":     volume,
            "amount":     amount,
            "prev_close": prev_close,
            "gain_pct":   gain_pct,
            "exchange":   "TWSE",
        })

    logger.info("TWSE 全市場資料：共 %d 支股票（交易日 %s）", len(results), actual_date_iso)
    return results, actual_date_iso


def fetch_tpex_all_stocks() -> List[Dict]:
    """
    取得 TPEX 上櫃股票當日全市場資料。

    Returns:
        與 fetch_twse_all_stocks 相同格式的 list of dict，
        另加 next_limit_up, last_ask_vol 欄位供漲停判定。
    """
    params = {"l": "zh-tw", "o": "json"}
    data = _get(config.TPEX_DAY_ALL_URL, params)
    if not data:
        logger.warning("TPEX 全市場資料取得失敗")
        return []

    # TPEX 回應格式：data 在 aaData 或 tables[0].aaData
    rows = data.get("aaData") or []
    if not rows and "tables" in data:
        try:
            rows = data["tables"][0].get("aaData", [])
        except (IndexError, KeyError):
            pass

    if not rows:
        logger.warning("TPEX 回應無資料")
        return []

    # TPEX stk_wn1430 欄位順序（依實際回應）
    TPEX_COLS = [
        "code", "name", "close", "change", "open", "high", "low",
        "vol_k", "amount", "trades",
        "last_bid", "last_bid_vol", "last_ask", "last_ask_vol",
        "shares_issued", "next_limit_up", "next_limit_down"
    ]

    results = []
    for row in rows:
        if len(row) < 7:
            continue

        padded = list(row) + [None] * (len(TPEX_COLS) - len(row))
        d = dict(zip(TPEX_COLS, padded))

        code = str(d["code"]).strip()
        name = str(d["name"]).strip()

        # TPEX 代號為 4~5 碼數字
        if not code.isdigit() or not (4 <= len(code) <= 5):
            continue

        close  = _to_float(d["close"])
        change = _to_float(d["change"])
        volume_k = _to_float(d["vol_k"])  # 千股單位

        if close is None or change is None:
            continue

        volume = volume_k * 1000 if volume_k is not None else None
        prev_close = round(close - change, 2)
        gain_pct   = round(change / prev_close * 100, 2) if prev_close != 0 else 0.0

        results.append({
            "code":          code,
            "name":          name,
            "open":          _to_float(d["open"]),
            "high":          _to_float(d["high"]),
            "low":           _to_float(d["low"]),
            "close":         close,
            "change":        change,
            "volume":        volume,
            "amount":        _to_float(d["amount"]),
            "prev_close":    prev_close,
            "gain_pct":      gain_pct,
            "exchange":      "TPEX",
            "next_limit_up": _to_float(d["next_limit_up"]),
            "last_ask_vol":  _to_float(d["last_ask_vol"]),
        })

    logger.info("TPEX 全市場資料：共 %d 支股票", len(results))
    return results


# ============================================================
# 個股月歷史資料
# ============================================================

_FAILED_SENTINEL = {"_fetch_failed": True}   # 寫入快取用的失敗標記
_SENTINEL_TTL   = 3600                        # 失敗標記有效期 1 小時，避免同一 session 反覆打同一 URL


def _is_sentinel(data) -> bool:
    """判斷快取內容是否為失敗標記（而非正常空陣列）。"""
    return isinstance(data, dict) and data.get("_fetch_failed") is True


def _fetch_twse_month(code: str, year: int, month: int) -> List[Dict]:
    """
    取得 TWSE 個股單月歷史資料（含快取）。

    快取邏輯：
    - 正常資料（含空月份）→ 依月份 TTL 永久/1hr 快取
    - API/網路完全失敗（3 次重試均失敗）→ 寫入 1hr 失敗標記，
      避免同一 session 內對同一月份反覆呼叫 API；
      1hr 後快取失效，下次執行自動重試。
    """
    now = datetime.now()
    is_current = (year == now.year and month == now.month)
    data_ttl    = config.CACHE_CURRENT_MONTH_TTL if is_current else config.CACHE_PAST_MONTH_TTL
    cache_name  = f"twse_{code}_{year}{month:02d}.json"

    cached = _load_cache(cache_name, max(data_ttl, _SENTINEL_TTL))
    if cached is not None:
        if _is_sentinel(cached):
            return []      # 上次失敗，標記仍在有效期內，直接跳過
        return cached

    date_str = f"{year}{month:02d}01"
    data = _get(config.TWSE_STOCK_DAY_URL, {"response": "json", "date": date_str, "stockNo": code})

    if not data or data.get("stat") != "OK":
        # 網路 / API 失敗：寫入短 TTL 失敗標記（1hr 後自動重試）
        _save_cache(cache_name, _FAILED_SENTINEL)
        return []

    raw_fields = data.get("fields", [
        "日期", "成交股數", "成交金額", "開盤價", "最高價",
        "最低價", "收盤價", "漲跌價差", "成交筆數"
    ])
    raw_data = data.get("data", [])

    records = []
    for row in raw_data:
        if len(row) < 7:
            continue
        row_dict = dict(zip(raw_fields, row))

        d = _roc_to_date(row_dict.get("日期", ""))
        if d is None:
            continue

        records.append({
            "date":   d.isoformat(),
            "open":   _to_float(row_dict.get("開盤價")),
            "high":   _to_float(row_dict.get("最高價")),
            "low":    _to_float(row_dict.get("最低價")),
            "close":  _to_float(row_dict.get("收盤價")),
            "volume": _to_float(row_dict.get("成交股數")),
            "change": _to_float(row_dict.get("漲跌價差")),
        })

    _save_cache(cache_name, records)
    return records


def _fetch_tpex_month(code: str, year: int, month: int) -> List[Dict]:
    """取得 TPEX 個股單月歷史資料（含快取）。同 TWSE 版本，失敗時寫入 1hr 標記。"""
    now = datetime.now()
    is_current = (year == now.year and month == now.month)
    data_ttl   = config.CACHE_CURRENT_MONTH_TTL if is_current else config.CACHE_PAST_MONTH_TTL
    cache_name = f"tpex_{code}_{year}{month:02d}.json"

    cached = _load_cache(cache_name, max(data_ttl, _SENTINEL_TTL))
    if cached is not None:
        if _is_sentinel(cached):
            return []
        return cached

    roc_date = _gregorian_to_roc(date(year, month, 1))
    data = _get(config.TPEX_STOCK_DAY_URL, {
        "l": "zh-tw", "o": "json", "d": roc_date, "s": code
    })

    if not data:
        _save_cache(cache_name, _FAILED_SENTINEL)
        return []

    rows = data.get("aaData") or []
    if not rows and "tables" in data:
        try:
            rows = data["tables"][0].get("aaData", [])
        except (IndexError, KeyError):
            pass

    if not rows:
        # API 正常回應但該月無資料（例如股票停牌整月）→ 儲存空陣列
        _save_cache(cache_name, [])
        return []

    # TPEX 歷史欄位：日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 成交筆數
    HIST_COLS = ["date_roc", "volume", "amount", "open", "high", "low", "close", "change", "trades"]

    records = []
    for row in rows:
        if len(row) < 7:
            continue
        padded = list(row) + [None] * (len(HIST_COLS) - len(row))
        d_roc = padded[0]
        d = _roc_to_date(d_roc) if d_roc else None
        if d is None:
            continue

        records.append({
            "date":   d.isoformat(),
            "open":   _to_float(padded[3]),
            "high":   _to_float(padded[4]),
            "low":    _to_float(padded[5]),
            "close":  _to_float(padded[6]),
            "volume": _to_float(padded[1]),
            "change": _to_float(padded[7]),
        })

    _save_cache(cache_name, records)
    return records


def _step_back_month(ref: date, steps: int) -> date:
    """從 ref 的年月往前推 steps 個月，回傳該月 1 日。"""
    m = ref.month - steps
    y = ref.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def get_stock_history(
    code: str,
    exchange: str,
    months: int = 2,
    end_date: Optional[date] = None,
) -> List[Dict]:
    """
    取得個股歷史資料，從 end_date 所在月份往前取 months 個月，依日期升冪排序。

    Args:
        code:      股票代號
        exchange:  'TWSE' 或 'TPEX'
        months:    要取幾個月（預設 2）
        end_date:  基準日期；None 則使用今日（供回測指定歷史結束日使用）

    Returns:
        list of dict，每筆含 date(str ISO), open, high, low, close, volume, change
    """
    ref = end_date if end_date is not None else date.today()
    all_records: List[Dict] = []

    for i in range(months):
        target = _step_back_month(ref, i)   # 正確逐月步退，不用 timedelta 近似
        y, m = target.year, target.month

        if exchange == "TWSE":
            records = _fetch_twse_month(code, y, m)
        else:
            records = _fetch_tpex_month(code, y, m)

        all_records.extend(records)

    # 去重、排序
    seen = set()
    unique = []
    for r in all_records:
        if r["date"] not in seen and r["close"] is not None:
            seen.add(r["date"])
            unique.append(r)

    unique.sort(key=lambda x: x["date"])
    return unique


# ============================================================
# 三大法人
# ============================================================

def fetch_institution_flows(trade_date: Optional[str] = None) -> Dict[str, float]:
    """
    取得三大法人買賣超資料 (TWSE T86)。

    Args:
        trade_date: 'YYYYMMDD'，None 則用今日

    Returns:
        dict: {stock_code: net_shares}，正值=買超，負值=賣超
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    # 歷史資料：永久快取（不會再變）；當天：短暫快取
    today_str = datetime.now().strftime("%Y%m%d")
    is_today = (trade_date == today_str)
    ttl = config.CACHE_CURRENT_MONTH_TTL if is_today else config.CACHE_PAST_MONTH_TTL
    cache_name = f"institution_{trade_date}.json"

    cached = _load_cache(cache_name, ttl)
    if cached is not None:
        return {k: float(v) for k, v in cached.items()}

    data = _get(config.TWSE_INSTITUTION_URL, {
        "response": "json",
        "date": trade_date,
        "selectType": "ALLBUT0999"
    })

    if not data or data.get("stat") != "OK":
        logger.warning("三大法人資料取得失敗")
        return {}

    raw_fields = data.get("fields", [])
    raw_data   = data.get("data", [])

    if not raw_data:
        return {}

    # 找出「三大法人買賣超股數」欄位位置
    net_col_idx = None
    for i, f in enumerate(raw_fields):
        if "三大法人" in f and "買賣超" in f:
            net_col_idx = i
            break

    # 若找不到，預設最後一欄
    if net_col_idx is None:
        net_col_idx = len(raw_fields) - 1

    result = {}
    for row in raw_data:
        if not row:
            continue
        code = str(row[0]).strip()
        if not code.isdigit():
            continue
        net = _to_float(row[net_col_idx]) if net_col_idx < len(row) else None
        if net is not None:
            result[code] = net

    logger.info("三大法人：共 %d 支", len(result))
    if result:
        _save_cache(cache_name, result)
    return result


# ============================================================
# 當沖率資料
# ============================================================

def fetch_day_trade_data(trade_date: Optional[str] = None) -> Dict[str, float]:
    """
    取得 TWSE 當沖交易量 (TWTB4U)。

    注意：此資料有 T+2 延遲，查詢當日資料可能為空。

    Returns:
        dict: {stock_code: day_trade_shares}
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    today_str = datetime.now().strftime("%Y%m%d")
    is_today = (trade_date == today_str)
    ttl = config.CACHE_CURRENT_MONTH_TTL if is_today else config.CACHE_PAST_MONTH_TTL
    cache_name = f"daytrade_{trade_date}.json"

    cached = _load_cache(cache_name, ttl)
    if cached is not None:
        return {k: float(v) for k, v in cached.items()}

    data = _get(config.TWSE_DAY_TRADE_URL, {
        "response": "json",
        "date": trade_date,
        "selectType": "ALL"
    })

    if not data or data.get("stat") != "OK":
        logger.warning("當沖資料取得失敗（可能為 T+2 延遲）")
        return {}

    raw_fields = data.get("fields", [])
    raw_data   = data.get("data", [])

    if not raw_data:
        return {}

    # 找買進與賣出欄位
    buy_idx = sell_idx = None
    for i, f in enumerate(raw_fields):
        if "買進" in f and "股數" in f:
            buy_idx = i
        if "賣出" in f and "股數" in f:
            sell_idx = i

    result = {}
    for row in raw_data:
        if not row:
            continue
        code = str(row[0]).strip()
        if not code.isdigit():
            continue

        buy  = _to_float(row[buy_idx])  if buy_idx  is not None and buy_idx  < len(row) else None
        sell = _to_float(row[sell_idx]) if sell_idx is not None and sell_idx < len(row) else None

        if buy is not None and sell is not None:
            result[code] = (buy + sell) / 2.0
        elif buy is not None:
            result[code] = buy
        elif sell is not None:
            result[code] = sell

    logger.info("當沖資料：共 %d 支", len(result))
    if result:
        _save_cache(cache_name, result)
    return result


# ============================================================
# 公司股本資料
# ============================================================

def fetch_company_capital(include_tpex: bool = False) -> Dict[str, int]:
    """
    取得公司實收資本額，30 天快取。

    Returns:
        dict: {stock_code: capital_ntd (int)}
    """
    cache_name = "company_capital.json"
    cached = _load_cache(cache_name, config.CACHE_COMPANY_TTL)
    if cached is not None:
        logger.info("公司股本資料：使用快取（%d 家）", len(cached))
        return {k: int(v) for k, v in cached.items()}

    result = {}

    endpoints = [(config.TWSE_COMPANY_L_URL, "TWSE")]
    if include_tpex:
        endpoints.append((config.TPEX_COMPANY_O_URL, "TPEX"))

    for url, market in endpoints:
        try:
            resp = requests.get(url, headers=config.REQUEST_HEADERS, timeout=30)
            resp.raise_for_status()
            companies = resp.json()
            time.sleep(config.API_DELAY)

            for c in companies:
                # 找代號欄位
                code = None
                for key in ("公司代號", "Code", "代號"):
                    if key in c:
                        code = str(c[key]).strip()
                        break

                # 找資本額欄位
                capital_raw = None
                for key in c:
                    if "資本額" in key:
                        capital_raw = c[key]
                        break

                if code and capital_raw:
                    cap = _to_float(capital_raw)
                    if cap is not None and cap > 0:
                        result[code] = int(cap)

            logger.info("公司股本 (%s)：%d 家", market, len(result))

        except Exception as e:
            logger.warning("公司股本資料取得失敗 (%s): %s", market, e)

    if result:
        _save_cache(cache_name, result)

    return result


# ============================================================
# 回測專用：股票名稱對照表 & 批次歷史資料
# ============================================================

def fetch_stock_name_map() -> Dict[str, str]:
    """
    取得 TWSE 股票代號 → 名稱 對照表。
    使用 1 小時快取。

    Returns:
        dict: {code: name}
    """
    cache_name = "stock_name_map.json"
    cached = _load_cache(cache_name, config.CACHE_CURRENT_MONTH_TTL)
    if cached is not None:
        logger.info("股票名稱對照表：使用快取（%d 支）", len(cached))
        return cached

    stocks, _ = fetch_twse_all_stocks()
    name_map = {s["code"]: s["name"] for s in stocks}

    if name_map:
        _save_cache(cache_name, name_map)
        logger.info("股票名稱對照表：已更新（%d 支）", len(name_map))

    return name_map


def fetch_bulk_histories(
    code_list: List[str],
    exchange: str = "TWSE",
    months: int = 5,
    progress: bool = True,
    end_date: Optional[date] = None,
) -> Dict[str, List[Dict]]:
    """
    批次取得多支股票的歷史資料。

    首次執行時間較長（~1000 支股票約 18 分鐘），之後完全使用快取。
    快取分月存放，已快取的月份不會重複下載。

    Args:
        code_list: 股票代號清單
        exchange:  'TWSE' 或 'TPEX'
        months:    要取幾個月的歷史（建議 bt_months + 3 以確保 MA lookback 足夠）
        progress:  是否顯示進度（輸出到 stderr）
        end_date:  基準結束日；往前推 months 個月。None 代表今日（適用於 screener 即時使用）。

    Returns:
        dict: {code: history_list}，history_list 依日期升冪排序
    """
    total = len(code_list)
    result: Dict[str, List[Dict]] = {}

    for idx, code in enumerate(code_list, 1):
        if progress:
            sys.stderr.write(f"\r歷史資料下載進度：{idx}/{total} [{code}]        ")
            sys.stderr.flush()

        history = get_stock_history(code, exchange, months, end_date=end_date)
        if history:
            result[code] = history

    if progress:
        sys.stderr.write(f"\r歷史資料下載完成：{len(result)}/{total} 支有資料          \n")
        sys.stderr.flush()

    logger.info("批次歷史資料：%d / %d 支有資料", len(result), total)
    return result
