# config.py
# 台股隔日沖選股系統 — 預設參數設定
# 所有數值皆可透過 settings.json 或 CLI 參數覆蓋

# ============================================================
# 篩選閾值 (Screening Thresholds)
# ============================================================

# 最低日漲幅 (%) — 主篩選條件
MIN_GAIN_PCT = 7.0

# 預篩選閾值 (%) — 在取歷史資料前的寬鬆篩選，減少 API 呼叫量
PRE_FILTER_GAIN_PCT = 5.0

# 漲停判定閾值 (%) — 超過此值視為接近漲停
NEAR_LIMIT_UP_PCT = 9.5

# 股本範圍 (NTD) — 10億 ~ 50億
CAPITAL_MIN_NTD = 1_000_000_000   # 10億
CAPITAL_MAX_NTD = 5_000_000_000   # 50億

# 量比 — 今日成交量 vs 過去 5 日均量
VOLUME_MULTIPLIER = 2.0

# 均線週期
MA_SHORT = 5
MA_MID   = 10
MA_LONG  = 20

# 布林通道
BOLL_WINDOW  = 20
BOLL_STD_MULT = 2.0

# 當沖率上限 (0~1)
MAX_DAY_TRADE_RATIO = 0.60

# 預設是否包含上櫃 (TPEX)
INCLUDE_TPEX = False

# ============================================================
# API URLs
# ============================================================

# TWSE (上市)
TWSE_DAY_ALL_URL      = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
TWSE_STOCK_DAY_URL    = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_INSTITUTION_URL  = "https://www.twse.com.tw/fund/T86"
TWSE_DAY_TRADE_URL    = "https://www.twse.com.tw/exchangeReport/TWTB4U"
TWSE_COMPANY_L_URL    = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

# TPEX (上櫃)
TPEX_DAY_ALL_URL      = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
TPEX_STOCK_DAY_URL    = "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
TPEX_COMPANY_O_URL    = "https://openapi.twse.com.tw/v1/opendata/t187ap03_O"

# ============================================================
# 已知隔日沖券商分點 (Known 隔日沖 Broker Branches)
# 供未來串接分點資料時使用
# ============================================================
DAYTRADE_BROKER_BRANCHES = [
    "凱基台北",
    "富邦建國",
    "摩根大通",
    "美林",
    "凱基",
]

# ============================================================
# 快取設定 (Cache Settings)
# ============================================================
CACHE_DIR = "cache"

# 當月資料快取有效時間 (秒) — 1 小時
CACHE_CURRENT_MONTH_TTL = 3600

# 歷史月份快取有效時間 (秒) — 永久 (365天)
CACHE_PAST_MONTH_TTL = 86400 * 365

# 公司基本資料快取有效時間 (秒) — 30 天
CACHE_COMPANY_TTL = 86400 * 30

# ============================================================
# 其他設定
# ============================================================

# API 請求間隔 (秒)
API_DELAY = 1.1

# 計算均線/布林通道所需最少歷史天數
MIN_HISTORY_DAYS = 22

# HTTP 請求 Headers
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
