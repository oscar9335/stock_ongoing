const state = {
  payload: null,
  rows: [],
  hoverIndex: null,
  viewStart: 0,
  viewEnd: 0,
  yZoom: 1,
  isDragging: false,
  dragStartX: 0,
  dragStartViewStart: 0,
  dragStartViewEnd: 0,
  pointerId: null,
};

const colors = {
  up: "#cf3f46",
  down: "#16805b",
  grid: "#e3e7ee",
  axis: "#687386",
  ma5: "#2f6fed",
  ma10: "#c88400",
  ma20: "#7057d2",
  ma60: "#4b5563",
  vma5: "#0f9f8f",
  vma20: "#b45309",
  realtime: "#111827",
};

const els = {
  symbolInput: document.getElementById("symbolInput"),
  marketSelect: document.getElementById("marketSelect"),
  monthsSelect: document.getElementById("monthsSelect"),
  realtimeToggle: document.getElementById("realtimeToggle"),
  oddLotToggle: document.getElementById("oddLotToggle"),
  loadButton: document.getElementById("loadButton"),
  suggestions: document.getElementById("suggestions"),
  marketStatus: document.getElementById("marketStatus"),
  summaryGrid: document.getElementById("summaryGrid"),
  chartTitle: document.getElementById("chartTitle"),
  chartMeta: document.getElementById("chartMeta"),
  legend: document.getElementById("legend"),
  canvas: document.getElementById("chartCanvas"),
  tooltip: document.getElementById("tooltip"),
  notes: document.getElementById("notes"),
  dataBody: document.getElementById("dataBody"),
  downloadButton: document.getElementById("downloadButton"),
  xZoomInButton: document.getElementById("xZoomInButton"),
  xZoomOutButton: document.getElementById("xZoomOutButton"),
  yZoomInButton: document.getElementById("yZoomInButton"),
  yZoomOutButton: document.getElementById("yZoomOutButton"),
  resetViewButton: document.getElementById("resetViewButton"),
};

const ctx = els.canvas.getContext("2d");

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("zh-TW", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatPrice(value) {
  if (value === null || value === undefined) return "-";
  return Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 2 });
}

function priceLabel(row, long = false) {
  if (row.isRealtime) return long ? "現價" : "現";
  return long ? "收盤" : "收";
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function resetView() {
  const lastIndex = Math.max(0, state.rows.length - 1);
  state.viewStart = 0;
  state.viewEnd = lastIndex;
  state.yZoom = 1;
  state.hoverIndex = null;
}

function clampView() {
  const count = state.rows.length;
  if (!count) {
    resetView();
    return;
  }
  const minVisible = Math.min(12, count);
  let visible = state.viewEnd - state.viewStart + 1;
  visible = clamp(visible, minVisible, count);
  if (visible >= count) {
    state.viewStart = 0;
    state.viewEnd = count - 1;
    return;
  }
  const center = (state.viewStart + state.viewEnd) / 2;
  state.viewStart = center - (visible - 1) / 2;
  state.viewEnd = state.viewStart + visible - 1;
  if (state.viewStart < 0) {
    state.viewStart = 0;
    state.viewEnd = visible - 1;
  }
  if (state.viewEnd > count - 1) {
    state.viewEnd = count - 1;
    state.viewStart = state.viewEnd - visible + 1;
  }
}

function zoomX(scale, anchorRatio = 0.5) {
  if (!state.rows.length) return;
  const visible = state.viewEnd - state.viewStart + 1;
  const targetVisible = clamp(visible * scale, Math.min(12, state.rows.length), state.rows.length);
  const anchor = state.viewStart + (visible - 1) * anchorRatio;
  state.viewStart = anchor - (anchor - state.viewStart) * (targetVisible / visible);
  state.viewEnd = state.viewStart + targetVisible - 1;
  clampView();
  state.hoverIndex = null;
  els.tooltip.hidden = true;
  drawChart();
}

function zoomY(scale) {
  state.yZoom = clamp(state.yZoom * scale, 0.35, 12);
  drawChart();
}

function movingAverage(rows, field, period) {
  const result = Array(rows.length).fill(null);
  let sum = 0;
  const queue = [];
  rows.forEach((row, index) => {
    const value = Number(row[field]);
    queue.push(value);
    sum += value;
    if (queue.length > period) sum -= queue.shift();
    if (queue.length === period) result[index] = sum / period;
  });
  return result;
}

function selectedPeriods(selector) {
  return [...document.querySelectorAll(selector)]
    .filter((input) => input.checked)
    .map((input) => Number(input.dataset.period));
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = els.canvas.getBoundingClientRect();
  els.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  els.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawLine(points, xForIndex, yForValue, stroke, startIndex, endIndex) {
  ctx.save();
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1.8;
  ctx.beginPath();
  let started = false;
  for (let index = startIndex; index <= endIndex; index += 1) {
    const value = points[index];
    if (value === null || value === undefined) {
      started = false;
      continue;
    }
    const x = xForIndex(index);
    const y = yForValue(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();
  ctx.restore();
}

function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min || 0];
  const span = max - min;
  const rawStep = span / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const residual = rawStep / magnitude;
  const step = (residual >= 5 ? 5 : residual >= 2 ? 2 : 1) * magnitude;
  const ticks = [];
  let current = Math.ceil(min / step) * step;
  while (current <= max + step * 0.5) {
    ticks.push(current);
    current += step;
  }
  return ticks;
}

function drawChart() {
  resizeCanvas();
  const rows = state.rows;
  const rect = els.canvas.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0, 0, width, height);

  if (!rows.length) {
    ctx.fillStyle = "#687386";
    ctx.font = "15px Segoe UI, Noto Sans TC, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("請查詢股票資料", width / 2, height / 2);
    return;
  }

  clampView();
  const startIndex = Math.max(0, Math.floor(state.viewStart));
  const endIndex = Math.min(rows.length - 1, Math.ceil(state.viewEnd));
  const visibleRows = rows.slice(startIndex, endIndex + 1);
  const visibleCount = Math.max(1, state.viewEnd - state.viewStart + 1);
  els.chartMeta.textContent = `成交量單位：股；資料筆數 ${rows.length}；顯示 ${rows[startIndex].date} ~ ${rows[endIndex].date}`;
  const margin = { left: 58, right: 72, top: 24, bottom: 32 };
  const gap = 22;
  const plotW = width - margin.left - margin.right;
  const priceH = Math.max(220, Math.floor((height - margin.top - margin.bottom - gap) * 0.66));
  const priceTop = margin.top;
  const priceBottom = priceTop + priceH;
  const volTop = priceBottom + gap;
  const volBottom = height - margin.bottom;
  const volH = volBottom - volTop;
  const candleW = Math.max(2, Math.min(12, plotW / visibleCount * 0.62));
  const step = plotW / visibleCount;
  const xForIndex = (index) => margin.left + (index - state.viewStart) * step + step / 2;

  const maPeriods = selectedPeriods(".ma-toggle");
  const vmaPeriods = selectedPeriods(".vma-toggle");
  const closeMas = new Map(maPeriods.map((period) => [period, movingAverage(rows, "close", period)]));
  const volumeMas = new Map(vmaPeriods.map((period) => [period, movingAverage(rows, "volumeShares", period)]));

  const priceValues = visibleRows.flatMap((row) => [row.high, row.low]);
  for (const values of closeMas.values()) {
    for (let index = startIndex; index <= endIndex; index += 1) {
      if (values[index] !== null) priceValues.push(values[index]);
    }
  }
  let minPrice = Math.min(...priceValues);
  let maxPrice = Math.max(...priceValues);
  const padding = (maxPrice - minPrice || maxPrice * 0.02 || 1) * 0.08;
  minPrice -= padding;
  maxPrice += padding;
  const priceCenter = (minPrice + maxPrice) / 2;
  const priceRange = Math.max((maxPrice - minPrice) / state.yZoom, 1);
  minPrice = priceCenter - priceRange / 2;
  maxPrice = priceCenter + priceRange / 2;
  const yPrice = (value) => priceBottom - ((value - minPrice) / (maxPrice - minPrice)) * priceH;

  const maxVolume = Math.max(...visibleRows.map((row) => Number(row.volumeShares) || 0), 1);
  const yVolume = (value) => volBottom - (value / maxVolume) * volH;

  ctx.save();
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.font = "12px Segoe UI, Noto Sans TC, sans-serif";
  ctx.fillStyle = colors.axis;
  ctx.textBaseline = "middle";

  for (const tick of niceTicks(minPrice, maxPrice, 5)) {
    const y = yPrice(tick);
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
    ctx.textAlign = "left";
    ctx.fillText(formatPrice(tick), width - margin.right + 8, y);
  }

  for (const tick of [0, maxVolume / 2, maxVolume]) {
    const y = yVolume(tick);
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(formatNumber(tick), margin.left - 8, y);
  }

  const dateEvery = Math.max(1, Math.ceil(visibleRows.length / 6));
  ctx.textBaseline = "top";
  for (let index = startIndex; index <= endIndex; index += 1) {
    const row = rows[index];
    if ((index - startIndex) % dateEvery !== 0 && index !== endIndex) continue;
    const x = xForIndex(index);
    ctx.textAlign = "center";
    ctx.fillText(row.date.slice(5), x, volBottom + 10);
  }
  ctx.restore();

  for (let index = startIndex; index <= endIndex; index += 1) {
    const row = rows[index];
    const x = xForIndex(index);
    const isUp = row.close >= row.open;
    const color = row.isRealtime ? colors.realtime : isUp ? colors.up : colors.down;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = row.isRealtime ? 2 : 1.2;
    ctx.beginPath();
    ctx.moveTo(x, yPrice(row.high));
    ctx.lineTo(x, yPrice(row.low));
    ctx.stroke();
    const top = yPrice(Math.max(row.open, row.close));
    const bottom = yPrice(Math.min(row.open, row.close));
    const bodyH = Math.max(1.5, bottom - top);
    if (isUp) {
      ctx.fillRect(x - candleW / 2, top, candleW, bodyH);
    } else {
      ctx.strokeRect(x - candleW / 2, top, candleW, bodyH);
    }

    const volTopY = yVolume(row.volumeShares);
    ctx.globalAlpha = 0.78;
    ctx.fillRect(x - candleW / 2, volTopY, candleW, Math.max(1, volBottom - volTopY));
    ctx.globalAlpha = 1;
  }

  closeMas.forEach((values, period) => drawLine(values, xForIndex, yPrice, colors[`ma${period}`] || colors.axis, startIndex, endIndex));
  volumeMas.forEach((values, period) => drawLine(values, xForIndex, yVolume, colors[`vma${period}`] || colors.axis, startIndex, endIndex));

  if (state.hoverIndex !== null && state.hoverIndex >= startIndex && state.hoverIndex <= endIndex && rows[state.hoverIndex]) {
    const row = rows[state.hoverIndex];
    const x = xForIndex(state.hoverIndex);
    ctx.save();
    ctx.strokeStyle = "rgba(17, 24, 39, 0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, priceTop);
    ctx.lineTo(x, volBottom);
    ctx.stroke();
    ctx.restore();
    showTooltip(row, x, yPrice(row.close), width, height);
  }
}

function showTooltip(row, x, y, width, height) {
  const cls = row.close >= row.open ? "up" : "down";
  els.tooltip.innerHTML = `
    <strong>${row.date}${row.isRealtime ? " 盤中" : ""}</strong><br>
    開 ${formatPrice(row.open)} 高 ${formatPrice(row.high)} 低 ${formatPrice(row.low)}<br>
    ${priceLabel(row)} <span class="${cls}">${formatPrice(row.close)}</span><br>
    成交股數 ${formatNumber(row.volumeShares)}<br>
    來源 ${row.source}
  `;
  els.tooltip.hidden = false;
  const tipRect = els.tooltip.getBoundingClientRect();
  const left = Math.min(width - tipRect.width - 12, Math.max(10, x + 12));
  const top = Math.min(height - tipRect.height - 12, Math.max(10, y - tipRect.height / 2));
  els.tooltip.style.left = `${left}px`;
  els.tooltip.style.top = `${top}px`;
}

function updateLegend() {
  const parts = [
    ["上漲", colors.up],
    ["下跌", colors.down],
    ...selectedPeriods(".ma-toggle").map((period) => [`MA${period}`, colors[`ma${period}`] || colors.axis]),
    ...selectedPeriods(".vma-toggle").map((period) => [`量均${period}`, colors[`vma${period}`] || colors.axis]),
    ["盤中估算", colors.realtime],
  ];
  els.legend.innerHTML = parts
    .map(([name, color]) => `<span><i style="background:${color}"></i>${name}</span>`)
    .join("");
}

function updateSummary(payload) {
  const rows = payload.rows;
  const last = rows[rows.length - 1];
  const previous = rows[rows.length - 2];
  const change = previous ? last.close - previous.close : 0;
  const changeClass = change >= 0 ? "up" : "down";
  const source = last.source || "-";
  const changeText = previous
    ? `${last.isRealtime ? "較前收 " : ""}${change >= 0 ? "+" : ""}${formatPrice(change)}`
    : "-";
  els.summaryGrid.innerHTML = `
    <div><span>${priceLabel(last, true)}</span><strong class="${changeClass}">${formatPrice(last.close)} (${changeText})</strong></div>
    <div><span>成交股數</span><strong>${formatNumber(last.volumeShares)}</strong></div>
    <div><span>資料日期</span><strong>${last.date}${last.isRealtime ? " 盤中" : ""}</strong></div>
    <div><span>資料來源</span><strong>${source}</strong></div>
  `;
}

function updateTable(rows) {
  const recent = rows.slice(-80).reverse();
  els.dataBody.innerHTML = recent
    .map((row) => {
      const cls = row.close >= row.open ? "up" : "down";
      return `
        <tr>
          <td>${row.date}${row.isRealtime ? " *" : ""}</td>
          <td>${formatPrice(row.open)}</td>
          <td>${formatPrice(row.high)}</td>
          <td>${formatPrice(row.low)}</td>
          <td class="${cls}">${priceLabel(row)} ${formatPrice(row.close)}</td>
          <td>${formatNumber(row.volumeShares)}</td>
          <td>${row.source}</td>
        </tr>
      `;
    })
    .join("");
}

function updateNotes(payload) {
  const notes = [
    payload.volumePolicy,
    ...(payload.notes || []),
    ...(payload.warnings || []),
  ].filter(Boolean);
  if (!notes.length) {
    els.notes.classList.remove("visible");
    els.notes.innerHTML = "";
    return;
  }
  els.notes.classList.add("visible");
  els.notes.innerHTML = `<ul>${notes.map((note) => `<li>${note}</li>`).join("")}</ul>`;
}

function render(payload) {
  state.payload = payload;
  state.rows = payload.rows || [];
  resetView();
  const marketName = payload.market === "twse" ? "上市" : "上櫃";
  els.marketStatus.textContent = marketName;
  els.chartTitle.textContent = `${payload.symbol} ${payload.name || ""}`.trim();
  els.chartMeta.textContent = `成交量單位：股；資料筆數 ${state.rows.length}`;
  updateLegend();
  updateSummary(payload);
  updateTable(state.rows);
  updateNotes(payload);
  els.tooltip.hidden = true;
  drawChart();
}

async function loadHistory() {
  const symbol = els.symbolInput.value.trim();
  if (!symbol) return;
  const params = new URLSearchParams({
    symbol,
    market: els.marketSelect.value,
    months: els.monthsSelect.value,
    realtime: els.realtimeToggle.checked ? "1" : "0",
    oddlot: els.oddLotToggle.checked ? "1" : "0",
  });
  els.loadButton.disabled = true;
  els.loadButton.textContent = "查詢中";
  els.marketStatus.textContent = "讀取中";
  try {
    const response = await fetch(`/api/history?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "查詢失敗");
    render(data);
  } catch (error) {
    els.marketStatus.textContent = "錯誤";
    els.notes.classList.add("visible");
    els.notes.innerHTML = `<ul><li>${error.message}</li></ul>`;
  } finally {
    els.loadButton.disabled = false;
    els.loadButton.textContent = "查詢";
  }
}

let searchTimer = null;
async function updateSuggestions() {
  const q = els.symbolInput.value.trim();
  if (q.length < 1) return;
  const params = new URLSearchParams({ q, market: els.marketSelect.value });
  try {
    const response = await fetch(`/api/search?${params}`);
    if (!response.ok) return;
    const data = await response.json();
    els.suggestions.innerHTML = (data.items || [])
      .map((item) => `<option value="${item.symbol}">${item.label}</option>`)
      .join("");
  } catch {
    // Suggestions are optional.
  }
}

function downloadCsv() {
  if (!state.rows.length) return;
  const lines = [
    ["date", "open", "high", "low", "priceType", "price", "volumeShares", "source"],
    ...state.rows.map((row) => [
      row.date,
      row.open,
      row.high,
      row.low,
      priceLabel(row, true),
      row.close,
      row.volumeShares,
      row.source,
    ]),
  ];
  const csv = lines.map((line) => line.map((cell) => `"${String(cell ?? "").replaceAll('"', '""')}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const symbol = state.payload?.symbol || "stock";
  a.href = url;
  a.download = `${symbol}_daily_volume_shares.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

els.loadButton.addEventListener("click", loadHistory);
els.symbolInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadHistory();
});
els.symbolInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(updateSuggestions, 220);
});
els.marketSelect.addEventListener("change", updateSuggestions);
els.downloadButton.addEventListener("click", downloadCsv);
document.querySelectorAll(".ma-toggle,.vma-toggle").forEach((input) => {
  input.addEventListener("change", () => {
    updateLegend();
    drawChart();
  });
});

function updateHoverFromPoint(clientX) {
  if (!state.rows.length) return;
  const rect = els.canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const marginLeft = 58;
  const marginRight = 72;
  const plotW = rect.width - marginLeft - marginRight;
  if (x < marginLeft || x > rect.width - marginRight) {
    state.hoverIndex = null;
    els.tooltip.hidden = true;
    drawChart();
    return;
  }
  const visible = state.viewEnd - state.viewStart + 1;
  const step = plotW / Math.max(1, visible);
  const index = Math.round(state.viewStart + (x - marginLeft - step / 2) / step);
  state.hoverIndex = clamp(index, Math.floor(state.viewStart), Math.ceil(state.viewEnd));
  drawChart();
}

els.canvas.addEventListener("pointerdown", (event) => {
  if (!state.rows.length) return;
  state.isDragging = true;
  state.pointerId = event.pointerId;
  state.dragStartX = event.clientX;
  state.dragStartViewStart = state.viewStart;
  state.dragStartViewEnd = state.viewEnd;
  els.canvas.classList.add("dragging");
  els.canvas.setPointerCapture(event.pointerId);
});

els.canvas.addEventListener("pointermove", (event) => {
  if (!state.rows.length) return;
  if (state.isDragging && state.pointerId === event.pointerId) {
    const rect = els.canvas.getBoundingClientRect();
    const plotW = rect.width - 58 - 72;
    const visible = state.dragStartViewEnd - state.dragStartViewStart + 1;
    const deltaIndex = -((event.clientX - state.dragStartX) / Math.max(1, plotW)) * visible;
    state.viewStart = state.dragStartViewStart + deltaIndex;
    state.viewEnd = state.dragStartViewEnd + deltaIndex;
    clampView();
    state.hoverIndex = null;
    els.tooltip.hidden = true;
    drawChart();
    return;
  }
  updateHoverFromPoint(event.clientX);
});

function endDrag(event) {
  if (state.pointerId !== null && event.pointerId !== state.pointerId) return;
  state.isDragging = false;
  state.pointerId = null;
  els.canvas.classList.remove("dragging");
}

els.canvas.addEventListener("pointerup", endDrag);
els.canvas.addEventListener("pointercancel", endDrag);

els.canvas.addEventListener("wheel", (event) => {
  if (!state.rows.length) return;
  event.preventDefault();
  if (event.shiftKey) {
    zoomY(event.deltaY < 0 ? 1.18 : 1 / 1.18);
    return;
  }
  const rect = els.canvas.getBoundingClientRect();
  const anchorRatio = clamp((event.clientX - rect.left - 58) / Math.max(1, rect.width - 58 - 72), 0, 1);
  zoomX(event.deltaY < 0 ? 0.78 : 1 / 0.78, anchorRatio);
}, { passive: false });

els.canvas.addEventListener("mouseleave", () => {
  if (state.isDragging) return;
  state.hoverIndex = null;
  els.tooltip.hidden = true;
  drawChart();
});

els.xZoomInButton.addEventListener("click", () => zoomX(0.72));
els.xZoomOutButton.addEventListener("click", () => zoomX(1 / 0.72));
els.yZoomInButton.addEventListener("click", () => zoomY(1.2));
els.yZoomOutButton.addEventListener("click", () => zoomY(1 / 1.2));
els.resetViewButton.addEventListener("click", () => {
  resetView();
  els.tooltip.hidden = true;
  drawChart();
});

window.addEventListener("resize", drawChart);

updateLegend();
drawChart();
loadHistory();
