# screener.py
# 台股隔日沖選股邏輯 — 純計算模組（無 I/O）

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================
# 技術指標計算
# ============================================================

def compute_ma(closes: List[Optional[float]], window: int) -> Optional[float]:
    """計算最近 window 日的簡單移動平均。資料不足回傳 None。"""
    valid = [c for c in closes if c is not None]
    if len(valid) < window:
        return None
    arr = np.array(valid[-window:], dtype=float)
    return round(float(np.mean(arr)), 2)


def compute_bollinger(
    closes: List[Optional[float]],
    window: int = 20,
    num_std: float = 2.0
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    計算布林通道（Bollinger Bands）。

    Returns:
        (upper, middle, lower) — 資料不足時回傳 (None, None, None)
    """
    valid = [c for c in closes if c is not None]
    if len(valid) < window:
        return None, None, None
    arr = np.array(valid[-window:], dtype=float)
    mid   = float(np.mean(arr))
    std   = float(np.std(arr, ddof=0))
    upper = round(mid + num_std * std, 2)
    lower = round(mid - num_std * std, 2)
    return upper, round(mid, 2), lower


def compute_volume_ratio(
    today_vol: Optional[float],
    history: List[Dict],
    lookback: int = 5
) -> Optional[float]:
    """
    計算今日成交量 vs 過去 N 日均量的比值。

    Args:
        today_vol: 今日成交股數
        history: 歷史資料（已排序，不含今日）
        lookback: 比較天數（預設 5）

    Returns:
        比值（float），資料不足或量為 0 時回傳 None
    """
    if today_vol is None or today_vol <= 0:
        return None

    vols = [r["volume"] for r in history if r.get("volume") is not None]
    if len(vols) < lookback:
        return None

    avg = np.mean(vols[-lookback:])
    if avg <= 0:
        return None

    return round(today_vol / avg, 2)


def _twse_tick_size(price: float) -> float:
    """TWSE 股票報價最小跳動單位（依價格區間）。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _twse_limit_up_price(prev_close: float) -> float:
    """計算 TWSE 漲停板價格（前日收盤 × 1.1，依 tick 向下取整）。"""
    import math as _math
    tick = _twse_tick_size(prev_close)
    return _math.floor(prev_close * 1.1 / tick) * tick


def detect_limit_up(
    gain_pct: float,
    close: Optional[float] = None,
    next_limit_up: Optional[float] = None,
    last_ask_vol: Optional[float] = None,
    near_limit_pct: float = 9.5,
    prev_close: Optional[float] = None,
) -> str:
    """
    判斷漲停狀態。

    Returns:
        'LOCKED'     — 確定鎖漲停
        'NEAR_LIMIT' — 接近漲停（漲幅 >= near_limit_pct，但未確認鎖死）
        'NORMAL'     — 普通上漲
    """
    if gain_pct < near_limit_pct:
        return "NORMAL"

    # ── TPEX：有次日漲停價與最後揭示賣量，可精確判定 ──────────
    if next_limit_up is not None and close is not None:
        today_limit = round(next_limit_up / 1.1, 2)
        if abs(close - today_limit) < 0.1 and last_ask_vol is not None and last_ask_vol > 0:
            return "LOCKED"

    # ── TWSE：用前日收盤推算漲停板，比對收盤價 ────────────────
    # prev_close 由 data_fetcher 計算（= close - change），精確可靠
    if prev_close is not None and prev_close > 0 and close is not None:
        limit_price = _twse_limit_up_price(prev_close)
        tick = _twse_tick_size(prev_close)
        if abs(close - limit_price) < tick * 0.5:
            return "LOCKED"

    return "NEAR_LIMIT"


def check_ma_alignment(
    history_closes: List[Optional[float]],
    today_close: float,
    ma_short: int = 5,
    ma_mid: int = 10,
    ma_long: int = 20
) -> Dict:
    """
    檢查均線多頭排列：收盤 > MA短 > MA中 > MA長。

    Returns:
        dict 含 ma_short_val, ma_mid_val, ma_long_val, aligned (bool or None)
    """
    # 把今日收盤加入計算
    all_closes = history_closes + [today_close]

    ms = compute_ma(all_closes, ma_short)
    mm = compute_ma(all_closes, ma_mid)
    ml = compute_ma(all_closes, ma_long)

    aligned: Optional[bool] = None
    if ms is not None and mm is not None and ml is not None:
        aligned = bool(today_close > ms > mm > ml)

    return {
        "ma_short_val": ms,
        "ma_mid_val":   mm,
        "ma_long_val":  ml,
        "ma_aligned":   aligned,
    }


def check_bollinger(
    history_closes: List[Optional[float]],
    today_close: float,
    today_vol: Optional[float],
    history: List[Dict],
    window: int = 20,
    num_std: float = 2.0,
    vol_lookback: int = 5
) -> Dict:
    """
    檢查布林通道突破（收盤 >= 上軌）並確認量能放大。

    Returns:
        dict 含 bb_upper, bb_mid, bb_lower, bb_broke, bb_vol_confirmed
    """
    all_closes = history_closes + [today_close]
    upper, mid, lower = compute_bollinger(all_closes, window, num_std)

    broke: Optional[bool] = None
    if upper is not None:
        broke = bool(today_close >= upper)

    # 量能確認：今日量 > 過去 5 日均量
    vol_ratio = compute_volume_ratio(today_vol, history, vol_lookback)
    vol_confirmed: Optional[bool] = None
    if vol_ratio is not None:
        vol_confirmed = bool(vol_ratio >= 1.0)

    return {
        "bb_upper":         upper,
        "bb_mid":           mid,
        "bb_lower":         lower,
        "bb_broke":         broke,
        "bb_vol_confirmed": vol_confirmed,
    }


# ============================================================
# 主選股邏輯
# ============================================================

def build_result(
    stock: Dict,
    history: List[Dict],
    capital: Optional[int],
    institution_net: Optional[float],
    day_trade_vol: Optional[float],
    cfg: Dict,
    trade_date_iso: Optional[str] = None,
) -> Optional[Dict]:
    """
    對單支股票套用所有篩選條件，回傳結果 dict 或 None（不符合條件）。

    Args:
        stock: 當日行情 dict（來自 data_fetcher）
        history: 歷史 OHLCV list（已排序升冪，可能含今日）
        capital: 實收資本額 (NTD)，None 表示無資料
        institution_net: 三大法人淨買超股數，None 表示無資料
        day_trade_vol: 當沖量，None 表示無資料
        cfg: 設定參數 dict
        trade_date_iso: 交易日 ISO 字串 (YYYY-MM-DD)，用來排除歷史中的今日資料

    Returns:
        結果 dict 或 None
    """
    code     = stock["code"]
    name     = stock["name"]
    exchange = stock["exchange"]
    close    = stock["close"]
    gain_pct = stock["gain_pct"]
    volume   = stock["volume"]

    flags: List[str] = []

    # ---- Filter 1: 漲幅 ----
    if gain_pct < cfg["min_gain_pct"]:
        return None

    # ---- Filter 2: 股本 ----
    capital_ok: Optional[bool] = None
    capital_b: Optional[float] = None  # 億元
    if capital is not None:
        capital_b = round(capital / 1e8, 1)
        if capital < cfg["capital_min_ntd"] or capital > cfg["capital_max_ntd"]:
            return None
        capital_ok = True
    else:
        flags.append("CAPITAL_DATA_MISSING")

    # ---- 漲停狀態 ----
    limit_status = detect_limit_up(
        gain_pct=gain_pct,
        close=close,
        next_limit_up=stock.get("next_limit_up"),
        last_ask_vol=stock.get("last_ask_vol"),
        near_limit_pct=cfg["near_limit_up_pct"],
        prev_close=stock.get("prev_close"),   # TWSE 漲停板計算用
    )

    # ---- 歷史資料整理 ----
    # 排除今日資料（月歷史 API 可能含今日，避免污染均量計算）
    if trade_date_iso:
        past_history = [r for r in history if r.get("date", "") < trade_date_iso]
    else:
        # 無明確交易日時，排除最後一筆（若與今日收盤相同）
        if history and history[-1].get("close") == close:
            past_history = history[:-1]
        else:
            past_history = history

    history_closes = [r["close"] for r in past_history]
    has_history    = len(history_closes) >= cfg["min_history_days"]

    if not has_history:
        flags.append("INSUFFICIENT_HISTORY")

    # ---- Filter 3: 量比（使用去除今日後的歷史均量）----
    vol_ratio = compute_volume_ratio(volume, past_history, lookback=5)
    if has_history:
        if vol_ratio is None or vol_ratio < cfg["volume_multiplier"]:
            return None
    # 若無歷史資料，不強制過濾量比，但加入旗標
    if vol_ratio is None:
        flags.append("VOLUME_RATIO_UNAVAILABLE")

    # ---- 技術指標 ----
    ma_result   = check_ma_alignment(
        history_closes, close,
        cfg["ma_short"], cfg["ma_mid"], cfg["ma_long"]
    )
    boll_result = check_bollinger(
        history_closes, close, volume, past_history,
        cfg["boll_window"], cfg["boll_std_mult"]
    )

    # ---- Filter 4: 均線多頭排列 ----
    if has_history and ma_result["ma_aligned"] is False:
        return None

    # ---- Filter 5: 布林通道突破 ----
    if has_history and boll_result["bb_broke"] is False:
        return None

    # ---- 當沖率 ----
    day_trade_ratio: Optional[float] = None
    day_trade_ok:    Optional[bool]  = None
    if day_trade_vol is not None and volume is not None and volume > 0:
        day_trade_ratio = round(day_trade_vol / volume, 3)
        day_trade_ok    = bool(day_trade_ratio < cfg["max_day_trade_ratio"])
        if not day_trade_ok:
            # 當沖率過高：不直接排除，但加重要警告
            flags.append("HIGH_DAY_TRADE_RATIO")
    else:
        flags.append("DAY_TRADE_DATA_UNAVAILABLE")

    # ---- 三大法人 ----
    inst_net_k: Optional[int] = None  # 張
    if institution_net is not None:
        inst_net_k = int(institution_net / 1000)

    # ---- 評分（僅供排序）----
    score = gain_pct  # 基礎分：漲幅
    # 漲停鎖死時若開啟 bt_skip_locked，鎖死股票終究會被過濾掉，不應給加分
    if limit_status == "LOCKED" and not cfg.get("bt_skip_locked", True):
        score += 8.0
    elif limit_status == "NEAR_LIMIT":
        score += 4.0
    if vol_ratio is not None:
        score += min(vol_ratio, 5.0)
    if ma_result["ma_aligned"] is True:
        score += 3.0
    if boll_result["bb_broke"] is True:
        score += 4.0
    if boll_result["bb_vol_confirmed"] is True:
        score += 2.0
    if "HIGH_DAY_TRADE_RATIO" in flags:
        score -= 5.0

    return {
        # 基本資訊
        "代號":           code,
        "名稱":           name,
        "市場":           exchange,
        "收盤價":         close,
        "漲幅(%)":        gain_pct,
        "漲停狀態":       limit_status,
        # 量能
        "成交量(張)":     int(volume / 1000) if volume else None,
        "量比(5日)":      vol_ratio,
        # 股本
        "股本(億)":       capital_b,
        "股本符合":       capital_ok,
        # 均線
        f"MA{cfg['ma_short']}":  ma_result["ma_short_val"],
        f"MA{cfg['ma_mid']}":    ma_result["ma_mid_val"],
        f"MA{cfg['ma_long']}":   ma_result["ma_long_val"],
        "均線多頭排列":   ma_result["ma_aligned"],
        # 布林通道
        "BB上軌":         boll_result["bb_upper"],
        "BB突破":         boll_result["bb_broke"],
        "BB量能確認":     boll_result["bb_vol_confirmed"],
        # 當沖
        "當沖率(%)":      round(day_trade_ratio * 100, 1) if day_trade_ratio is not None else None,
        "當沖率正常":     day_trade_ok,
        # 法人
        "三大法人淨買(張)": inst_net_k,
        # 其他
        "資料旗標":       "|".join(flags) if flags else "",
        "評分":          round(score, 1),
    }


def screen_stocks(
    all_stocks: List[Dict],
    history_fn,          # callable: (code, exchange) -> List[Dict]
    capital_map: Dict[str, int],
    institution_map: Dict[str, float],
    day_trade_map: Dict[str, float],
    cfg: Dict,
    trade_date_iso: Optional[str] = None,
) -> List[Dict]:
    """
    主選股函式。

    Args:
        all_stocks:      當日全市場行情（來自 data_fetcher）
        history_fn:      取歷史資料的函式，簽名 (code, exchange) -> history_list
        capital_map:     股本資料 dict
        institution_map: 三大法人淨買超 dict
        day_trade_map:   當沖量 dict
        cfg:             設定參數 dict
        trade_date_iso:  交易日 ISO 字串 (YYYY-MM-DD)，用來排除歷史中的今日資料

    Returns:
        通過篩選的股票 list，依評分降冪排序
    """
    # Step 1: 寬鬆預篩（只看漲幅，降低歷史 API 呼叫量）
    pre_filtered = [
        s for s in all_stocks
        if s.get("gain_pct", 0) >= cfg["pre_filter_gain_pct"]
        and s.get("close", 0) > 0
    ]
    logger.info("預篩選（漲幅≥%.1f%%）：%d / %d 支",
                cfg["pre_filter_gain_pct"], len(pre_filtered), len(all_stocks))

    candidates = []
    for i, stock in enumerate(pre_filtered):
        code     = stock["code"]
        exchange = stock["exchange"]

        logger.debug("[%d/%d] 處理 %s %s", i + 1, len(pre_filtered), code, stock["name"])

        # 取歷史資料
        history = history_fn(code, exchange)

        # 套用篩選
        result = build_result(
            stock=stock,
            history=history,
            capital=capital_map.get(code),
            institution_net=institution_map.get(code),
            day_trade_vol=day_trade_map.get(code),
            cfg=cfg,
            trade_date_iso=trade_date_iso,
        )
        if result is not None:
            candidates.append(result)

    # 依評分降冪排序
    candidates.sort(key=lambda x: x["評分"], reverse=True)
    logger.info("最終篩選結果：%d 支候選標的", len(candidates))
    return candidates
