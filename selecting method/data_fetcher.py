# -*- coding: utf-8 -*-
"""
資料擷取層：TWSE OpenAPI → RWD JSON → BeautifulSoup 爬蟲 → yfinance
每個 public 函式回傳 FetchResult，絕不向上拋出例外。
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup

import yfinance as yf

from config import (
    ENDPOINTS, SECTOR_INDEX_MAP, CONCEPT_GROUPS,
    REQUEST_HEADERS, REQUEST_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF,
    _SECTOR_CODE_RANGES, TAIEX_TICKER, TAIEX_FALLBACK,
    THRESHOLDS,
)

logger = logging.getLogger(__name__)

SourceTag = Literal["openapi", "rwd_json", "scraped", "yfinance", "derived", "empty"]

# 台灣國定假日（2025-2026），非交易日
_TW_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 27), date(2025, 1, 28),
    date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),
    date(2025, 2, 28), date(2025, 4, 4), date(2025, 4, 5),
    date(2025, 5, 1), date(2025, 6, 7), date(2025, 9, 3),
    date(2025, 10, 10),
    date(2026, 1, 1), date(2026, 1, 26), date(2026, 1, 27),
    date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30),
    date(2026, 2, 28), date(2026, 4, 4), date(2026, 4, 6),
    date(2026, 5, 1), date(2026, 6, 20), date(2026, 9, 3),
    date(2026, 10, 9),
}


# ── 回傳型別 ──────────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    df:       pd.DataFrame
    source:   SourceTag
    warnings: list[str] = field(default_factory=list)
    ts:       pd.Timestamp = field(default_factory=pd.Timestamp.now)

    @property
    def ok(self) -> bool:
        return not self.df.empty


def _empty(source: SourceTag = "empty", *warnings: str) -> FetchResult:
    return FetchResult(pd.DataFrame(), source, list(warnings))


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _numeric(series: pd.Series) -> pd.Series:
    """把含逗號的字串數字轉成 float；'--' 視為 NaN。"""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).str.strip().replace("--", np.nan),
        errors="coerce",
    )


def _roc_to_gregorian(roc_date: str) -> pd.Timestamp:
    """'1150421' → pd.Timestamp('2026-04-21')"""
    s = str(roc_date).strip()
    year  = int(s[:3]) + 1911
    month = int(s[3:5])
    day   = int(s[5:7])
    return pd.Timestamp(year=year, month=month, day=day)


def get_last_trading_day(offset: int = 0) -> str:
    """
    回傳最近第 offset 個交易日（0=今天或上個交易日）的 YYYYMMDD 字串。
    跳過週末與 _TW_HOLIDAYS。
    """
    d = date.today()
    count = 0
    while True:
        if d.weekday() < 5 and d not in _TW_HOLIDAYS:
            if count == offset:
                return d.strftime("%Y%m%d")
            count += 1
        d -= timedelta(days=1)


def _retry_request(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    max_retries: int = MAX_RETRIES,
) -> requests.Response:
    """帶指數退避的 HTTP GET；5xx/timeout 重試，4xx 直接失敗。"""
    hdrs = {**REQUEST_HEADERS, **(headers or {})}
    last_exc: Exception = RuntimeError("未嘗試任何請求")
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=hdrs, timeout=REQUEST_TIMEOUT)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF ** attempt
            logger.warning("請求失敗（第 %d 次），%.0fs 後重試：%s", attempt + 1, wait, exc)
            time.sleep(wait)
    raise last_exc


def _is_json_response(resp: requests.Response) -> bool:
    """偵測 TWSE 是否回傳維護頁（HTTP 200 但 body 為 HTML）。"""
    ct = resp.headers.get("Content-Type", "")
    if "json" in ct:
        return True
    try:
        json.loads(resp.text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _parse_twse_table(soup: BeautifulSoup, table_index: int = 0) -> pd.DataFrame:
    """通用 TWSE HTML table 解析器（處理 rowspan/colspan、逗號數字、'--' → NaN）。"""
    tables = soup.find_all("table")
    if table_index >= len(tables):
        return pd.DataFrame()
    table = tables[table_index]
    rows = table.find_all("tr")
    if not rows:
        return pd.DataFrame()

    data: list[list[str]] = []
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
        if cells:
            data.append(cells)

    if len(data) < 2:
        return pd.DataFrame()

    # 第一列作為欄位名稱（有時第二列才是真正欄位名）
    headers = data[0]
    body    = data[1:]
    # 長度不一致時截斷/補齊
    n = len(headers)
    rows_out = [row[:n] + [""] * max(0, n - len(row)) for row in body if any(c for c in row)]
    return pd.DataFrame(rows_out, columns=headers)


# ── Layer 1：TWSE OpenAPI ──────────────────────────────────────────────────────

def fetch_sector_indices() -> FetchResult:
    """
    GET /v1/exchangeReport/MI_INDEX
    回傳類股指數收盤價與漲跌幅 DataFrame。
    注意：此端點無成交量資料。
    """
    try:
        resp = _retry_request(ENDPOINTS["sector_index"])
        if not _is_json_response(resp):
            raise ValueError("回傳 HTML，非 JSON")
        raw = resp.json()
        if not raw:
            return _empty("openapi", "MI_INDEX 回傳空陣列（可能非交易日）")

        df = pd.DataFrame(raw)
        # 只保留 SECTOR_INDEX_MAP 定義的類股
        known = set(SECTOR_INDEX_MAP.keys())
        if "指數" in df.columns:
            df = df[df["指數"].isin(known)].copy()
            df["sector_zh"] = df["指數"].map(lambda x: SECTOR_INDEX_MAP.get(x, (x, x))[1])
            df["sector_en"] = df["指數"].map(lambda x: SECTOR_INDEX_MAP.get(x, (x, x))[0])
        else:
            # 欄位名稱版本差異 fallback
            idx_col = next((c for c in df.columns if "指數" in c), None)
            if idx_col is None:
                return _empty("openapi", f"MI_INDEX 欄位異常：{list(df.columns)}")
            df = df.rename(columns={idx_col: "指數"})

        # 數值型別轉換
        for col in ["收盤指數", "漲跌點數", "漲跌百分比"]:
            if col in df.columns:
                df[col] = _numeric(df[col])

        df = df.rename(columns={
            "收盤指數":    "close_idx",
            "漲跌點數":    "chg_pts",
            "漲跌百分比":  "chg_pct",
        })
        return FetchResult(df.reset_index(drop=True), "openapi")

    except Exception as exc:
        logger.warning("fetch_sector_indices 主路徑失敗：%s，改用爬蟲", exc)
        return fetch_sector_indices_scrape()


def fetch_all_stocks_today() -> FetchResult:
    """
    GET /v1/exchangeReport/STOCK_DAY_ALL
    回傳當日全市場個股 code/name/trade_volume/trade_value/OHLC/change。
    這是計算類股成交比重的核心資料來源。
    """
    try:
        resp = _retry_request(ENDPOINTS["stock_day_all"])
        if not _is_json_response(resp):
            raise ValueError("回傳 HTML，非 JSON")
        raw = resp.json()
        if not raw:
            return _empty("openapi", "STOCK_DAY_ALL 回傳空陣列（可能非交易日）")

        df = pd.DataFrame(raw)

        # 統一欄位名稱（TWSE 偶有大小寫/底線差異）
        col_map = {}
        for c in df.columns:
            cl = c.strip()
            if cl in ("Code", "code"):
                col_map[c] = "code"
            elif cl in ("Name", "name"):
                col_map[c] = "name"
            elif "TradeVolume" in cl or "成交股數" in cl:
                col_map[c] = "trade_volume"
            elif "TradeValue" in cl or "成交金額" in cl:
                col_map[c] = "trade_value"
            elif cl in ("Open", "open", "開盤價"):
                col_map[c] = "open"
            elif cl in ("High", "high", "最高價"):
                col_map[c] = "high"
            elif cl in ("Low", "low", "最低價"):
                col_map[c] = "low"
            elif cl in ("Close", "close", "收盤價"):
                col_map[c] = "close"
            elif cl in ("Change", "change", "漲跌價差"):
                col_map[c] = "change"
        df = df.rename(columns=col_map)

        # 移除空白/非數字代碼列
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.strip()
            df = df[df["code"].str.match(r"^\d{4,6}$")]

        # 數值轉換
        for col in ["trade_volume", "trade_value", "open", "high", "low", "close", "change"]:
            if col in df.columns:
                df[col] = _numeric(df[col])

        df = df.dropna(subset=["trade_value"])
        df = df[df["trade_value"] > 0]
        return FetchResult(df.reset_index(drop=True), "openapi")

    except Exception as exc:
        logger.error("fetch_all_stocks_today 失敗：%s", exc)
        return _empty("empty", f"無法取得個股成交資料：{exc}")


def fetch_market_total_turnover() -> FetchResult:
    """
    GET /v1/exchangeReport/FMTQIK
    回傳大盤總成交值（用作 Money Flow 分母）。
    失敗時以 fetch_all_stocks_today() 加總取代。
    """
    try:
        resp = _retry_request(ENDPOINTS["market_total"])
        if not _is_json_response(resp):
            raise ValueError("回傳 HTML，非 JSON")
        raw = resp.json()
        if not raw:
            raise ValueError("FMTQIK 回傳空陣列")

        df = pd.DataFrame(raw)
        # 取最新一列（通常只有一列）
        row = df.iloc[-1]
        # 嘗試找成交值欄位
        val_col = next(
            (c for c in df.columns if "TradeValue" in c or "成交金額" in c or "Value" in c),
            None,
        )
        if val_col is None:
            raise ValueError(f"FMTQIK 找不到成交值欄位：{list(df.columns)}")

        total_value = float(str(row[val_col]).replace(",", ""))
        result = {"total_value": total_value, "date": row.get("Date", "")}
        df_out = pd.DataFrame([result])
        return FetchResult(df_out, "openapi")

    except Exception as exc:
        logger.warning("fetch_market_total_turnover 失敗：%s，改用加總 fallback", exc)
        stocks = fetch_all_stocks_today()
        if stocks.ok and "trade_value" in stocks.df.columns:
            total = float(stocks.df["trade_value"].sum())
            df_out = pd.DataFrame([{"total_value": total, "date": get_last_trading_day()}])
            return FetchResult(df_out, "derived", ["市場總成交值由個股加總推算"])
        return _empty("empty", f"無法取得大盤總成交值：{exc}")


# ── Layer 2：TWSE RWD JSON（三大法人） ─────────────────────────────────────────

def fetch_institutional_investors(date_str: str | None = None) -> FetchResult:
    """
    GET https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=YYYYMMDD&selectType=ALL
    回傳三大法人各股淨買超資料。
    日期採民國年轉換；失敗時 fallback 爬蟲。
    www.twse.com.tw 可能被防火牆封鎖，故只重試 1 次以快速失敗。
    """
    if date_str is None:
        date_str = get_last_trading_day()

    try:
        params = {"response": "json", "date": date_str, "selectType": "ALL"}
        # max_retries=1：www.twse.com.tw 若逾時則立即 fallback，不浪費時間
        resp = _retry_request(ENDPOINTS["institutional_json"], params=params, max_retries=1)
        if not _is_json_response(resp):
            raise ValueError("回傳 HTML，非 JSON（可能維護中）")

        payload = resp.json()
        # TWSE 格式：{"stat": "OK", "data": [...], "fields": [...]}
        if payload.get("stat") != "OK" or "data" not in payload:
            raise ValueError(f"三大法人 API stat={payload.get('stat')}")

        fields = payload.get("fields", [])
        data   = payload["data"]
        df = pd.DataFrame(data, columns=fields if fields else None)

        df = _normalize_institutional_df(df)
        return FetchResult(df, "rwd_json")

    except Exception as exc:
        logger.warning("fetch_institutional_investors 主路徑失敗：%s，改用爬蟲", exc)
        return fetch_institutional_scrape(date_str)


def _normalize_institutional_df(df: pd.DataFrame) -> pd.DataFrame:
    """統一三大法人欄位名稱並轉換數值。"""
    col_map = {}
    for c in df.columns:
        cl = c.strip()
        if "證券代號" in cl or "代碼" in cl:
            col_map[c] = "code"
        elif "證券名稱" in cl or "名稱" in cl:
            col_map[c] = "name"
        elif "外資及陸資" in cl and "買進" in cl:
            col_map[c] = "foreign_buy"
        elif "外資及陸資" in cl and "賣出" in cl:
            col_map[c] = "foreign_sell"
        elif "外資及陸資" in cl and "淨買" in cl:
            col_map[c] = "foreign_net"
        elif "投信" in cl and "淨買" in cl:
            col_map[c] = "trust_net"
        elif "自營商" in cl and "淨買" in cl:
            col_map[c] = "dealer_net"
        elif "三大法人" in cl and "淨買" in cl:
            col_map[c] = "total_net"
    df = df.rename(columns=col_map)

    for col in ["foreign_buy", "foreign_sell", "foreign_net", "trust_net", "dealer_net", "total_net"]:
        if col in df.columns:
            df[col] = _numeric(df[col])

    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.strip()
        df = df[df["code"].str.match(r"^\d{4,6}$")]

    return df.reset_index(drop=True)


# ── Layer 3：BeautifulSoup Fallback ──────────────────────────────────────────

def fetch_sector_indices_scrape() -> FetchResult:
    """爬取 TWSE 類股指數歷史頁（MI_INDEX.html）作為 fetch_sector_indices 的備援。"""
    try:
        resp = _retry_request(ENDPOINTS["sector_index_html"])
        soup = BeautifulSoup(resp.text, "lxml")
        df   = _parse_twse_table(soup, table_index=0)
        if df.empty:
            return _empty("scraped", "爬蟲解析 MI_INDEX.html 失敗：table 為空")

        # 嘗試找類股名稱欄
        idx_col = next((c for c in df.columns if "指數" in c or "類股" in c), None)
        if idx_col:
            df = df[df[idx_col].isin(SECTOR_INDEX_MAP.keys())].copy()
            df["sector_zh"] = df[idx_col].map(lambda x: SECTOR_INDEX_MAP.get(x, (x, x))[1])
            df = df.rename(columns={idx_col: "指數"})

        for col in df.columns:
            if col not in ["指數", "sector_zh", "sector_en"]:
                df[col] = _numeric(df[col])

        return FetchResult(df.reset_index(drop=True), "scraped", ["類股指數資料來自 HTML 爬蟲"])

    except Exception as exc:
        logger.error("fetch_sector_indices_scrape 失敗：%s", exc)
        return _empty("empty", f"類股指數爬蟲失敗：{exc}")


def fetch_institutional_scrape(date_str: str | None = None) -> FetchResult:
    """爬取三大法人 HTML 表格作為 fetch_institutional_investors 的備援。"""
    if date_str is None:
        date_str = get_last_trading_day()

    try:
        params = {"date": date_str, "selectType": "ALL"}
        # 同樣只重試 1 次；若環境封鎖 www.twse.com.tw 則直接回傳空結果
        resp = _retry_request(ENDPOINTS["institutional_html"], params=params, max_retries=1)
        soup = BeautifulSoup(resp.text, "lxml")
        df   = _parse_twse_table(soup, table_index=0)
        if df.empty:
            return _empty("scraped", "爬蟲解析 T86 HTML 失敗：table 為空")

        df = _normalize_institutional_df(df)
        return FetchResult(df, "scraped", ["三大法人資料來自 HTML 爬蟲"])

    except Exception as exc:
        logger.error("fetch_institutional_scrape 失敗：%s", exc)
        return _empty("empty", f"三大法人爬蟲失敗：{exc}")


# ── Layer 4：yfinance ─────────────────────────────────────────────────────────

def fetch_price_history(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
) -> FetchResult:
    """
    下載多檔個股歷史 OHLCV（自動補 .TW 後綴）。
    NaN 比例 > THRESHOLDS['max_nan_ratio'] 的 ticker 會被丟棄並記錄警告。
    回傳 MultiIndex DataFrame (ticker, OHLCV)。
    """
    if not tickers:
        return _empty("empty", "tickers 清單為空")

    tw_tickers = [t if t.endswith(".TW") else f"{t}.TW" for t in tickers]
    warnings: list[str] = []

    try:
        raw = yf.download(
            tw_tickers,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            return _empty("yfinance", "yfinance 回傳空 DataFrame")

        # 單一 ticker 時 yfinance 不建 MultiIndex，手動轉換
        if not isinstance(raw.columns, pd.MultiIndex):
            raw.columns = pd.MultiIndex.from_product([[tw_tickers[0]], raw.columns])

        # 移除 NaN 過多的 ticker
        max_nan = THRESHOLDS["max_nan_ratio"]
        valid_tickers = []
        for tk in tw_tickers:
            if tk not in raw.columns.get_level_values(0):
                warnings.append(f"{tk}：yfinance 無資料")
                continue
            col_df = raw[tk]
            nan_ratio = col_df.isna().mean().mean()
            if nan_ratio > max_nan:
                warnings.append(f"{tk}：NaN 比例 {nan_ratio:.0%}，已略過")
            else:
                valid_tickers.append(tk)

        if not valid_tickers:
            return _empty("yfinance", "所有 ticker 資料不足", *warnings)

        df = raw[valid_tickers]
        return FetchResult(df, "yfinance", warnings)

    except Exception as exc:
        logger.error("fetch_price_history 失敗：%s", exc)
        return _empty("empty", f"yfinance 下載失敗：{exc}")


def fetch_taiex_history(period: str = "1y") -> FetchResult:
    """
    下載加權指數（^TWII）歷史資料。
    失敗時改下載期貨 FITX=F 作為備援。
    """
    for ticker, tag in [(TAIEX_TICKER, "^TWII"), (TAIEX_FALLBACK, "FITX=F")]:
        try:
            df = yf.download(
                ticker, period=period, interval="1d",
                auto_adjust=True, progress=False,
            )
            if not df.empty:
                # 移除 MultiIndex（單 ticker 有時會產生）
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                warn = [] if ticker == TAIEX_TICKER else [f"使用 {tag} 期貨替代 ^TWII"]
                return FetchResult(df, "yfinance", warn)
        except Exception as exc:
            logger.warning("fetch_taiex_history %s 失敗：%s", ticker, exc)

    return _empty("empty", "無法取得加權指數資料（^TWII 與 FITX=F 均失敗）")


def fetch_concept_group_prices(period: str = "1y") -> FetchResult:
    """一次下載所有概念股的歷史價格。"""
    all_codes = list({c for codes in CONCEPT_GROUPS.values() for c in codes})
    return fetch_price_history(all_codes, period=period)


# ── 類股歸屬對應 ──────────────────────────────────────────────────────────────

def map_stocks_to_sectors(stocks_df: pd.DataFrame) -> pd.DataFrame:
    """
    輸入個股 DataFrame（需含 'code' 欄位）。
    新增欄位：
        sector_zh    : 類股中文短標籤（依代碼區間）
        concept_group: 概念股分群名稱（可為 None）
    """
    df = stocks_df.copy()

    def _assign_sector(code: str) -> str:
        try:
            c = int(code)
        except ValueError:
            return "其他"
        for low, high, name in _SECTOR_CODE_RANGES:
            if low <= c <= high:
                return name
        return "其他"

    # 建立概念股反查字典
    concept_lookup: dict[str, str] = {}
    for group, codes in CONCEPT_GROUPS.items():
        for c in codes:
            concept_lookup[c] = group

    df["sector_zh"]     = df["code"].astype(str).map(_assign_sector)
    df["concept_group"] = df["code"].astype(str).map(concept_lookup)  # NaN if not in any group
    return df


# ── 主程式（冒煙測試） ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("=== 冒煙測試：data_fetcher ===\n")

    print("1) 最近交易日：", get_last_trading_day())

    print("\n2) 類股指數（前 5 筆）：")
    r = fetch_sector_indices()
    print(f"   來源：{r.source} | 筆數：{len(r.df)} | 警告：{r.warnings}")
    if r.ok:
        print(r.df[["sector_zh", "close_idx", "chg_pct"]].head())

    print("\n3) 個股成交資料（前 5 筆）：")
    s = fetch_all_stocks_today()
    print(f"   來源：{s.source} | 筆數：{len(s.df)} | 警告：{s.warnings}")
    if s.ok:
        s_mapped = map_stocks_to_sectors(s.df)
        print(s_mapped[["code", "name", "trade_value", "sector_zh", "concept_group"]].head())

    print("\n4) 大盤總成交值：")
    m = fetch_market_total_turnover()
    print(f"   來源：{m.source} | 資料：{m.df.to_dict('records')}")

    print("\n5) 加權指數歷史（最新 3 天）：")
    t = fetch_taiex_history(period="5d")
    print(f"   來源：{t.source} | 筆數：{len(t.df)}")
    if t.ok:
        print(t.df.tail(3))

    print("\n=== 測試完成 ===")
