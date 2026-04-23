# -*- coding: utf-8 -*-
"""
全域設定：API 端點、類股對照、概念股名單、分析閾值
"""

# ── TWSE OpenAPI 基礎路徑 ──────────────────────────────────────────────────────
TWSE_OPENAPI_BASE = "https://openapi.twse.com.tw/v1"

ENDPOINTS = {
    # 所有類股指數收盤價（無成交量）
    "sector_index":   f"{TWSE_OPENAPI_BASE}/exchangeReport/MI_INDEX",
    # 全市場個股當日成交資料（含成交量、成交值）
    "stock_day_all":  f"{TWSE_OPENAPI_BASE}/exchangeReport/STOCK_DAY_ALL",
    # 大盤當日總成交資訊
    "market_total":   f"{TWSE_OPENAPI_BASE}/exchangeReport/FMTQIK",
    # 成交量前 20 名（異常交叉確認用）
    "top20_volume":   f"{TWSE_OPENAPI_BASE}/exchangeReport/MI_INDEX20",
    # 三大法人買賣超（JSON，需附日期參數 YYYYMMDD）
    "institutional_json":  "https://www.twse.com.tw/rwd/zh/fund/T86",
    # 三大法人 HTML fallback（BeautifulSoup 爬取）
    "institutional_html":  "https://www.twse.com.tw/rwd/zh/fund/T86",
    # 類股指數歷史 HTML fallback
    "sector_index_html":   "https://www.twse.com.tw/zh/trading/historical/MI_INDEX.html",
}

# ── 29 大類股指數名稱對照 ──────────────────────────────────────────────────────
# key   = MI_INDEX 回傳的「指數」欄位值
# value = (英文短標籤, 中文短標籤)
SECTOR_INDEX_MAP: dict[str, tuple[str, str]] = {
    "水泥類指數":           ("Cement",         "水泥"),
    "食品類指數":           ("Food",           "食品"),
    "塑膠類指數":           ("Plastics",       "塑膠"),
    "紡織纖維類指數":       ("Textiles",       "紡纖"),
    "電機機械類指數":       ("Elec Mach",      "電機"),
    "電器電纜類指數":       ("Elec Cable",     "電纜"),
    "化學類指數":           ("Chemicals",      "化學"),
    "生技醫療類指數":       ("Biotech",        "生技"),
    "玻璃陶瓷類指數":       ("Glass/Ceramic",  "玻陶"),
    "造紙類指數":           ("Paper",          "造紙"),
    "鋼鐵類指數":           ("Steel",          "鋼鐵"),
    "橡膠類指數":           ("Rubber",         "橡膠"),
    "汽車類指數":           ("Auto",           "汽車"),
    "電子工業類指數":       ("Electronics",    "電子"),
    "半導體類指數":         ("Semicon",        "半導"),
    "電腦及週邊設備類指數": ("Computers",      "電腦"),
    "光電類指數":           ("Optoelec",       "光電"),
    "通信網路類指數":       ("Telecom/Net",    "通網"),
    "電子零組件類指數":     ("Elec Parts",     "零件"),
    "電子通路類指數":       ("Elec Distrib",   "通路"),
    "資訊服務類指數":       ("IT Services",    "資訊"),
    "其他電子類指數":       ("Other Elec",     "其他電"),
    "建材營造類指數":       ("Construction",   "建材"),
    "航運類指數":           ("Shipping",       "航運"),
    "觀光餐旅類指數":       ("Tourism",        "觀光"),
    "金融保險類指數":       ("Finance",        "金融"),
    "貿易百貨類指數":       ("Trade/Retail",   "貿百"),
    "油電燃氣類指數":       ("Energy/Gas",     "油電"),
    "其他類指數":           ("Others",         "其他"),
    # 新增類股（2020 年後）
    "綠能環保類指數":       ("Green Energy",   "綠能"),
    "數位雲端類指數":       ("Digital/Cloud",  "數雲"),
    "運動休閒類指數":       ("Sports/Leisure", "運休"),
    "居家生活類指數":       ("Home Living",    "居家"),
}

# ── 概念股自訂分群（不含 .TW 後綴） ──────────────────────────────────────────
CONCEPT_GROUPS: dict[str, list[str]] = {
    # 1491、3491 已下市，改用仍在交易的光通訊族群
    "光通訊":     ["4904", "6285", "3308", "3234", "5371"],
    "低軌衛星":   ["3813", "6579", "4977", "6269"],
    "記憶體":     ["3008", "2408", "2303"],
    "半導體設備": ["3450", "6533", "5285"],
}

# ── yfinance 大盤 ticker ──────────────────────────────────────────────────────
TAIEX_TICKER    = "^TWII"
TAIEX_FALLBACK  = "FITX=F"    # 期貨備援（TWII 延遲時）

# ── 分析閾值 ──────────────────────────────────────────────────────────────────
THRESHOLDS = {
    "anomaly_volume_surge_pct":   20.0,   # 成交比重較 5 日均值增加 >20% → 資金流入警示
    "anomaly_high_severity_pct":  50.0,   # >50% → 高嚴重度
    "anomaly_med_severity_pct":   30.0,   # 30-50% → 中嚴重度
    "ma_short":                   50,     # 短期均線天數（階段判斷）
    "ma_long":                    200,    # 長期均線天數
    "momentum_short_days":        5,
    "momentum_long_days":         20,
    "top_sectors_for_leaders":    5,      # 領頭羊表：取前 N 名類股
    "leaders_per_sector":         3,      # 每類股顯示前 K 名股票
    "min_trade_value_ntd":        1e8,    # 過濾低成交額股票（NT$1 億）
    "institutional_net_buy_ntd":  5e8,    # 三大法人淨買超閾值（NT$5 億）
    "concept_rs_threshold":       5.0,    # 概念股 RS 超越大盤 >5% → 警示
    "stage_consolidation_band":   0.03,   # 整理：股價距 MA50 ±3%
    "stage_slope_flat":           0.001,  # 均線斜率 <0.1% 視為平坦
    "stage_breakout_window":      5,      # Breakout：N 日內穿越 MA50
    "min_history_days":           200,    # 資料不足 200 天 → "資料不足"
    "max_nan_ratio":              0.80,   # yfinance：NaN 超過 80% 則丟棄
}

# ── 快取 TTL（秒） ────────────────────────────────────────────────────────────
TTL_INTRADAY   = 3600      # 1 小時：即時行情、成交量
TTL_HISTORICAL = 86400     # 24 小時：均線、RS、階段

# ── HTTP 請求設定 ─────────────────────────────────────────────────────────────
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.twse.com.tw/",
}
REQUEST_TIMEOUT = 15    # 每次請求 timeout（秒）
MAX_RETRIES     = 3
RETRY_BACKOFF   = 2.0   # 指數退避基底：2s, 4s, 8s

# ── 個股代碼區間 → 類股對應（底層 fallback，不含概念股） ─────────────────────
# 格式：(代碼下限, 代碼上限, 類股中文短標籤)
# 依「最精確 → 最廣泛」排列，首次符合即採用
_SECTOR_CODE_RANGES: list[tuple[int, int, str]] = [
    (1100, 1199, "水泥"),
    (1200, 1299, "食品"),
    (1300, 1499, "塑膠"),
    (1500, 1699, "紡纖"),
    (1700, 1799, "電機"),
    (1800, 1999, "電纜"),
    (2000, 2099, "鋼鐵"),
    (2100, 2199, "汽車"),
    (2200, 2299, "航運"),
    (2300, 2399, "半導"),
    (2400, 2499, "光電"),
    (2500, 2599, "建材"),
    (2600, 2699, "航運"),
    (2700, 2799, "觀光"),
    (2800, 2899, "金融"),
    (2900, 2999, "貿百"),
    (3000, 3099, "零件"),
    (3100, 3199, "電腦"),
    (3300, 3399, "通網"),
    (3400, 3499, "零件"),
    (3500, 3699, "電子"),
    (4400, 4499, "貿百"),
    (4900, 4999, "通網"),
    (5000, 5099, "建材"),
    (5800, 5999, "建材"),
    (6000, 6099, "電子"),
    (6200, 6499, "其他電"),
    (6500, 6999, "零件"),
    (8000, 8999, "其他電"),
    (9100, 9199, "航運"),
    (9900, 9999, "其他"),
]

# ── 階段標籤中文對照 ──────────────────────────────────────────────────────────
STAGE_LABELS_ZH = {
    "Breakout":          "噴發",
    "Accumulation":      "築底",
    "Consolidation":     "震盪",
    "Decline":           "衰退",
    "Insufficient Data": "資料不足",
}

# 階段對應顏色（Plotly 用）
STAGE_COLORS = {
    "Breakout":          "#00CC44",   # 綠
    "Accumulation":      "#3399FF",   # 藍
    "Consolidation":     "#FFAA00",   # 橘
    "Decline":           "#FF3333",   # 紅
    "Insufficient Data": "#888888",   # 灰
}
