# 台股 K 線與成交股數 App

這是一個 Python 標準庫即可執行的本地 Web App，用官方資料來源繪製台股 K 線、成交股數與可勾選均線。
期間最多可查 5 年。K 線圖支援滾輪縮放 X 軸、Shift + 滾輪縮放 Y 軸、拖曳左右平移，也可使用圖表上方的 X/Y 縮放與重設按鈕。

## 執行

```powershell
python server.py --host 127.0.0.1 --port 8000
```

開啟：

```text
http://127.0.0.1:8000
```

## 資料口徑

- 上市歷史日線：TWSE `STOCK_DAY`，欄位直接使用「成交股數」。
- 上櫃歷史日線：TPEx 個股日成交資訊 `afterTrading/tradingStock` 按月份查詢；此端點提供「成交張數」或舊欄位「成交仟股」，App 會轉成股數。最近一個 TPEx OpenAPI 盤後交易日可取得時，會改用 `TradingShares` 精準成交股數。
- 盤中估算：TWSE MIS `getStockInfo.jsp` 的整股累積成交量以「張」表示，轉為股數時乘以 1000；若勾選零股，會再加上 `getOddInfo.jsp` 的盤中零股累積股數。

盤中估算不是盤後正式成交股數，盤後正式資料可能包含盤後定價、鉅額交易等口徑差異。
