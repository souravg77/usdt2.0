// Live Markets page: polls the dashboard's /api/market endpoints (which read Bybit's public
// API, cached ~1s on the server) every 1.5 seconds and draws a candlestick chart on a canvas,
// with the indicators the strategies use (computed on the server, /api/market/studies) and the
// paper trader's entries, exits, stops and targets. The chart only shows: it never trades.
(function () {
  "use strict";
  var root = document.getElementById("market");
  if (!root) return;
  var state = { symbol: root.dataset.symbol, tf: root.dataset.tf, candles: [], hover: null,
                last: null, lastOk: 0, busy: false, busySince: 0, source: "", studies: null, polls: 0 };
  var POLL_MS = 1500, STUDY_EVERY = 2;  // indicators refresh every 2nd poll (3 seconds)
  var PANEL_H = 84;
  var COLORS = { ema20: "#f5c542", ema50: "#4cc3ff", ema200: "#c084fc", bb: "#94a3b8", vwap: "#f472b6",
                 tenkan: "#38bdf8", kijun: "#fb923c", psar: "#fb923c", rsi: "#c084fc", macd: "#4cc3ff",
                 signal: "#fb923c", adx: "#f5c542", k: "#2dd4bf", d: "#f472b6", sr: "#a3a3a3" };
  var PANELS = ["vol", "rsi", "macd", "adx", "stoch"];
  var on = {};
  var canvas = document.getElementById("chart");
  var ctx = canvas.getContext("2d");
  var tickersEl = document.getElementById("tickers");
  var statusEl = document.getElementById("live-status");
  var ohlcEl = document.getElementById("ohlc");
  var titleEl = document.getElementById("chart-title");
  var legendEl = document.getElementById("legend");
  var prices = {};

  // ---- indicator switches (remembered in this browser) ---------------------------------------

  var chips = document.querySelectorAll(".chip[data-study]");
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem("aitrader.studies") || "null"); } catch (e) { saved = null; }
  chips.forEach(function (b) {
    on[b.dataset.study] = saved && b.dataset.study in saved ? !!saved[b.dataset.study] : b.dataset.on === "1";
    b.setAttribute("aria-pressed", on[b.dataset.study] ? "true" : "false");
    b.addEventListener("click", function () {
      on[b.dataset.study] = !on[b.dataset.study];
      b.setAttribute("aria-pressed", on[b.dataset.study] ? "true" : "false");
      try { localStorage.setItem("aitrader.studies", JSON.stringify(on)); } catch (e) { /* private window */ }
      resize();
    });
  });
  var resetBtn = document.getElementById("studies-reset");
  if (resetBtn) resetBtn.addEventListener("click", function () {
    try { localStorage.removeItem("aitrader.studies"); } catch (e) { /* private window */ }
    chips.forEach(function (b) {
      on[b.dataset.study] = b.dataset.on === "1";
      b.setAttribute("aria-pressed", on[b.dataset.study] ? "true" : "false");
    });
    resize();
  });
  function panels() { return PANELS.filter(function (k) { return on[k]; }); }

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function fmt(x) {
    if (x === null || x === undefined || isNaN(x)) return "-";
    var d = x >= 1000 ? 2 : x >= 100 ? 2 : x >= 1 ? 4 : x >= 0.01 ? 5 : 8;
    return Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function fmtVol(x) {
    if (!x) return "-";
    if (x >= 1e9) return (x / 1e9).toFixed(2) + "B";
    if (x >= 1e6) return (x / 1e6).toFixed(2) + "M";
    if (x >= 1e3) return (x / 1e3).toFixed(1) + "K";
    return x.toFixed(0);
  }
  function timeLabel(ms) {
    var d = new Date(ms), p = function (n) { return (n < 10 ? "0" : "") + n; };
    if (state.tf === "1d") return p(d.getUTCDate()) + "/" + p(d.getUTCMonth() + 1);
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
  }

  // ---- data -----------------------------------------------------------------------------------

  // A request that never answers (the phone slept, the network switched) must not stop the updates:
  // each one gives up after FETCH_MS, and a poll stuck longer than STUCK_MS is let go.
  var FETCH_MS = 6000, STUCK_MS = 15000, RELOAD_AFTER_MS = 60000;

  function get(url) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer;
    var timeout = new Promise(function (_ok, fail) {
      timer = setTimeout(function () { if (ctl) ctl.abort(); fail(new Error("timed out")); }, FETCH_MS);
    });
    var req = fetch(url, { credentials: "same-origin", cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
    return Promise.race([req, timeout]).then(
      function (v) { clearTimeout(timer); return v; },
      function (e) { clearTimeout(timer); throw e; });
  }

  function poll() {
    if (document.hidden) return;
    if (state.busy && Date.now() - state.busySince < STUCK_MS) return;
    state.busy = true;
    state.busySince = Date.now();
    var sym = state.symbol, tf = state.tf, q = "?symbol=" + encodeURIComponent(sym) + "&tf=" + encodeURIComponent(tf);
    var wantStudies = !state.studies || state.polls % STUDY_EVERY === 0;
    state.polls += 1;
    var started = state.busySince;
    // the price cards and the chart update on their own: one failing doesn't hold up the other
    var cards = get("/api/market/tickers").then(function (res) {
      renderTickers(res.tickers || []);
      state.tickerSource = res.source;
      state.lastOk = Date.now();
    });
    var chart = Promise.all([
      get("/api/market/candles" + q),
      wantStudies ? get("/api/market/studies" + q).catch(function () { return null; }) : null
    ]).then(function (res) {
      if (sym === state.symbol && tf === state.tf) {
        state.candles = res[0].candles || [];
        state.source = res[0].source;
        state.note = res[0].note || "";
        if (res[1]) state.studies = res[1];
        state.lastOk = Date.now();
        draw();
      }
    });
    function done() {
      if (state.busySince === started) state.busy = false;  // a newer poll may have taken over
      status();
    }
    Promise.all([cards.catch(function () {}), chart.catch(function () {})]).then(done, done);
  }

  // last resort: nothing good for a minute while the page is open and online, so start the page
  // afresh (at most every 2 minutes, so a server that is down isn't reloaded over and over)
  function selfHeal() {
    if (document.hidden || !state.lastOk || navigator.onLine === false) return;
    if (Date.now() - state.lastOk < RELOAD_AFTER_MS) return;
    var last = 0;
    try { last = +sessionStorage.getItem("aitrader.marketReload") || 0; } catch (e) { last = 0; }
    if (Date.now() - last < 2 * RELOAD_AFTER_MS) return;
    try { sessionStorage.setItem("aitrader.marketReload", String(Date.now())); } catch (e) { /* private window */ }
    location.reload();
  }

  function status() {
    if (!state.lastOk) { statusEl.innerHTML = '<span class="dot"></span> Connecting…'; return; }
    var age = (Date.now() - state.lastOk) / 1000;
    var live = state.source === "live" && age < 10;
    statusEl.className = "live-status " + (live ? "on" : "off");
    statusEl.innerHTML = '<span class="dot"></span> ' +
      (live ? "Live · Bybit" : state.source === "database" ? "Saved prices" : "Reconnecting") +
      " · updated " + (age < 1 ? "just now" : age.toFixed(0) + "s ago") +
      (state.note ? '<span class="muted"> · ' + escapeHtml(state.note) + "</span>" : "") +
      (state.tickerSource === "database" && state.source === "live"
        ? '<span class="muted"> · the price cards show saved prices: Bybit\'s price list didn\'t answer</span>' : "");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderTickers(rows) {
    var html = "";
    rows.forEach(function (t) {
      var prev = prices[t.symbol];
      var flash = prev === undefined || t.price === prev ? "" : t.price > prev ? " up" : " down";
      prices[t.symbol] = t.price;
      var ch = t.change;
      var cls = ch === null || ch === undefined ? "" : ch >= 0 ? "good" : "bad";
      html += '<button type="button" class="tick' + (t.symbol === state.symbol ? " active" : "") + flash +
        '" data-symbol="' + escapeHtml(t.symbol) + '"><span class="tick-sym">' + escapeHtml(t.symbol) +
        '</span><span class="tick-px">' + fmt(t.price) + '</span><span class="tick-ch ' + cls + '">' +
        (ch === null || ch === undefined ? "" : (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%") +
        '</span><span class="tick-vol muted">Vol ' + fmtVol(t.volume) + "</span></button>";
    });
    tickersEl.innerHTML = html || '<p class="muted">No prices yet.</p>';
  }

  // ---- chart ----------------------------------------------------------------------------------

  function resize() {
    var dpr = window.devicePixelRatio || 1;
    var base = window.innerWidth <= 640 ? 340 : 440;
    canvas.style.height = (base + panels().length * PANEL_H) + "px";
    var w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function layout() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    var widest = 0;
    ctx.font = "600 12px system-ui, sans-serif";
    state.candles.slice(-300).forEach(function (b) { widest = Math.max(widest, b[2]); });
    var axisW = Math.max(56, Math.ceil(ctx.measureText(fmt(widest || 1)).width) + 14), axisH = 22;
    var pn = panels(), plotW = w - axisW, priceH = h - axisH - pn.length * PANEL_H;
    var barW = w < 500 ? 6 : 8;
    var n = Math.max(10, Math.floor(plotW / barW));
    var bars = state.candles.slice(-n);
    return { w: w, h: h, axisW: axisW, axisH: axisH, plotW: plotW, priceH: priceH, panels: pn,
             bars: bars, step: plotW / Math.max(bars.length, 1) };
  }

  function niceStep(span, count) {
    var raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    return (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
  }

  // the indicator values that line up with the visible bars (null where there is none)
  function aligned(bars) {
    var S = state.studies, out = { idx: [] };
    if (!S || !S.ts || S.symbol !== state.symbol || S.tf !== state.tf) return null;
    var pos = {};
    S.ts.forEach(function (t, i) { pos[t] = i; });
    bars.forEach(function (b) { out.idx.push(pos[b[0]] === undefined ? -1 : pos[b[0]]); });
    out.get = function (group, key, i) {
      var arr = (S[group] || {})[key], j = out.idx[i];
      return arr && j >= 0 ? arr[j] : null;
    };
    out.S = S;
    return out;
  }

  function polyline(n, val, y, step, color, width, dash) {
    ctx.strokeStyle = color; ctx.lineWidth = width || 1.5; ctx.setLineDash(dash || []);
    ctx.beginPath();
    var started = false;
    for (var i = 0; i < n; i++) {
      var v = val(i);
      if (v === null || v === undefined) { started = false; continue; }
      var x = (i + 0.5) * step, yy = y(v);
      if (started) ctx.lineTo(x, yy); else { ctx.moveTo(x, yy); started = true; }
    }
    ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
  }

  function barIndex(bars, ts) {
    if (!ts || !bars.length || ts < bars[0][0]) return -1;
    var lo = 0, hi = bars.length - 1;
    while (lo < hi) { var mid = (lo + hi + 1) >> 1; if (bars[mid][0] <= ts) lo = mid; else hi = mid - 1; }
    return lo;
  }

  function draw() {
    var L = layout(), bars = L.bars, n = bars.length;
    ctx.clearRect(0, 0, L.w, L.h);
    var up = css("--good") || "#22c55e", down = css("--bad") || "#ef4444";
    var line = css("--line") || "#333", muted = css("--muted") || "#888", accent = css("--accent") || "#7aa2ff";
    var fg = css("--fg") || "#ddd";
    ctx.font = "12px system-ui, sans-serif";
    if (!n) {
      ctx.fillStyle = muted;
      ctx.fillText(state.lastOk ? "No candles for this pair yet." : "Loading…", 12, 24);
      legendEl.innerHTML = "";
      return;
    }
    var A = aligned(bars), trades = A && on.trades ? (A.S.trades || { open: [], closed: [] }) : { open: [], closed: [] };
    var hi = -Infinity, lo = Infinity;
    bars.forEach(function (b) { hi = Math.max(hi, b[2]); lo = Math.min(lo, b[3]); });
    // overlays inside a sensible band around the candles widen the scale; far-away ones don't squash it
    var span0 = hi - lo || hi * 0.01, room = function (v) {
      if (v !== null && v !== undefined && v > lo - span0 * 0.6 && v < hi + span0 * 0.6) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
    };
    var overlayKeys = [];
    if (A) {
      if (on.ema) overlayKeys.push("ema20", "ema50", "ema200");
      if (on.bb) overlayKeys.push("bb_upper", "bb_lower");
      if (on.vwap) overlayKeys.push("vwap");
      if (on.st) overlayKeys.push("supertrend");
      if (on.ich) overlayKeys.push("tenkan", "kijun", "span_a", "span_b");
      if (on.psar) overlayKeys.push("psar");
      overlayKeys.forEach(function (k) { for (var i = 0; i < n; i++) room(A.get("overlays", k, i)); });
    }
    trades.open.forEach(function (t) { room(t.stop); room(t.target); room(t.entry); });
    var pad = (hi - lo) * 0.06 || hi * 0.001 || 1;
    hi += pad; lo -= pad;
    var top = 8, bottom = L.priceH - 4;
    var y = function (v) { return top + (hi - v) / (hi - lo) * (bottom - top); };

    // grid and price axis
    var stepV = niceStep(hi - lo, 5);
    ctx.strokeStyle = line; ctx.fillStyle = muted; ctx.lineWidth = 1; ctx.textAlign = "left";
    for (var v = Math.ceil(lo / stepV) * stepV; v <= hi; v += stepV) {
      var gy = Math.round(y(v)) + 0.5;
      ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(L.plotW, gy); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(fmt(v), L.plotW + 6, gy + 4);
    }
    // time axis
    var every = Math.max(1, Math.round(90 / L.step));
    ctx.textAlign = "center";
    for (var i = n - 1; i >= 0; i -= every) {
      var tx = (i + 0.5) * L.step;
      if (tx < 24) break;
      ctx.fillText(timeLabel(bars[i][0]), tx, L.h - 6);
    }

    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, L.plotW, L.priceH); ctx.clip();
    // Ichimoku cloud under everything: green where span A is above span B, red below
    if (A && on.ich) {
      for (var c = 0; c + 1 < n; c++) {
        var a0 = A.get("overlays", "span_a", c), b0 = A.get("overlays", "span_b", c);
        var a1 = A.get("overlays", "span_a", c + 1), b1 = A.get("overlays", "span_b", c + 1);
        if ([a0, b0, a1, b1].some(function (q) { return q === null; })) continue;
        var x0 = (c + 0.5) * L.step, x1 = (c + 1.5) * L.step;
        ctx.fillStyle = a0 >= b0 ? up : down; ctx.globalAlpha = 0.12;
        ctx.beginPath(); ctx.moveTo(x0, y(a0)); ctx.lineTo(x1, y(a1)); ctx.lineTo(x1, y(b1)); ctx.lineTo(x0, y(b0));
        ctx.closePath(); ctx.fill(); ctx.globalAlpha = 1;
      }
    }
    if (A && on.bb) {  // Bollinger band shaded
      ctx.fillStyle = COLORS.bb; ctx.globalAlpha = 0.08; ctx.beginPath();
      var first = true;
      for (var u = 0; u < n; u++) { var bu = A.get("overlays", "bb_upper", u); if (bu === null) continue;
        if (first) { ctx.moveTo((u + 0.5) * L.step, y(bu)); first = false; } else ctx.lineTo((u + 0.5) * L.step, y(bu)); }
      for (var l = n - 1; l >= 0; l--) { var bl = A.get("overlays", "bb_lower", l); if (bl !== null) ctx.lineTo((l + 0.5) * L.step, y(bl)); }
      if (!first) ctx.fill();
      ctx.globalAlpha = 1;
    }
    // support / resistance
    if (A && on.sr) {
      (A.S.levels || []).forEach(function (lv) {
        if (lv.price < lo || lv.price > hi) return;
        var ly0 = Math.round(y(lv.price)) + 0.5;
        ctx.strokeStyle = COLORS.sr; ctx.globalAlpha = 0.7; ctx.setLineDash([2, 4]);
        ctx.beginPath(); ctx.moveTo(0, ly0); ctx.lineTo(L.plotW, ly0); ctx.stroke(); ctx.setLineDash([]);
        ctx.globalAlpha = 1; ctx.fillStyle = muted; ctx.textAlign = "left";
        ctx.fillText(lv.touches + "×", 4, ly0 - 3);
      });
    }

    // candles (volume moved to its own panel)
    var bw = Math.max(1, L.step * 0.66);
    bars.forEach(function (b, i) {
      var x = (i + 0.5) * L.step, isUp = b[4] >= b[1], col = isUp ? up : down;
      ctx.strokeStyle = col; ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, y(b[2])); ctx.lineTo(Math.round(x) + 0.5, y(b[3])); ctx.stroke();
      var t0 = y(Math.max(b[1], b[4])), b0 = y(Math.min(b[1], b[4]));
      ctx.fillStyle = col; ctx.fillRect(x - bw / 2, t0, bw, Math.max(1, b0 - t0));
    });

    // overlay lines
    if (A) {
      var ov = function (k) { return function (i) { return A.get("overlays", k, i); }; };
      if (on.bb) { polyline(n, ov("bb_upper"), y, L.step, COLORS.bb, 1); polyline(n, ov("bb_lower"), y, L.step, COLORS.bb, 1);
                   polyline(n, ov("bb_mid"), y, L.step, COLORS.bb, 1, [4, 4]); }
      if (on.ema) { polyline(n, ov("ema20"), y, L.step, COLORS.ema20); polyline(n, ov("ema50"), y, L.step, COLORS.ema50);
                    polyline(n, ov("ema200"), y, L.step, COLORS.ema200, 2); }
      if (on.vwap) polyline(n, ov("vwap"), y, L.step, COLORS.vwap, 1.5, [6, 3]);
      if (on.ich) { polyline(n, ov("tenkan"), y, L.step, COLORS.tenkan, 1); polyline(n, ov("kijun"), y, L.step, COLORS.kijun, 1); }
      if (on.st) {  // green under price in an uptrend, red above it in a downtrend
        [1, -1].forEach(function (dir) {
          polyline(n, function (i) { return A.get("overlays", "supertrend_dir", i) === dir ? A.get("overlays", "supertrend", i) : null; },
                   y, L.step, dir === 1 ? up : down, 2);
        });
      }
      if (on.psar) {
        for (var p = 0; p < n; p++) {
          var sv = A.get("overlays", "psar", p);
          if (sv === null) continue;
          ctx.fillStyle = A.get("overlays", "psar_trend", p) === 1 ? up : down;
          ctx.beginPath(); ctx.arc((p + 0.5) * L.step, y(sv), 1.8, 0, Math.PI * 2); ctx.fill();
        }
      }
    }

    // the paper trader's trades: triangles at entries, dots at exits, lines for an open trade
    function tri(x, yy, pointUp, col) {
      var s = 6;
      ctx.fillStyle = col; ctx.beginPath();
      if (pointUp) { ctx.moveTo(x, yy); ctx.lineTo(x - s, yy + s * 1.6); ctx.lineTo(x + s, yy + s * 1.6); }
      else { ctx.moveTo(x, yy); ctx.lineTo(x - s, yy - s * 1.6); ctx.lineTo(x + s, yy - s * 1.6); }
      ctx.closePath(); ctx.fill();
    }
    function entryMark(t) {
      var i = barIndex(bars, t.entry_ts);
      if (i < 0) return;
      var x = (i + 0.5) * L.step, isShort = t.side === "sell";
      if (isShort) tri(x, y(bars[i][2]) - 4, false, down); else tri(x, y(bars[i][3]) + 4, true, up);
    }
    trades.closed.forEach(function (t) {
      entryMark(t);
      var i = barIndex(bars, t.exit_ts);
      if (i < 0 || t.exit === null || t.exit === undefined) return;
      ctx.fillStyle = (t.r || 0) >= 0 ? up : down; ctx.strokeStyle = fg;
      ctx.beginPath(); ctx.arc((i + 0.5) * L.step, y(t.exit), 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    });
    trades.open.forEach(function (t) {
      entryMark(t);
      var i = Math.max(0, barIndex(bars, t.entry_ts)), x0 = (i + 0.5) * L.step;
      [["entry", accent, "entry"], ["stop", down, "stop"], ["target", up, "target"]].forEach(function (k) {
        var val = t[k[0]];
        if (val === null || val === undefined || val < lo || val > hi) return;
        var yy = Math.round(y(val)) + 0.5;
        ctx.strokeStyle = k[1]; ctx.setLineDash(k[0] === "entry" ? [] : [5, 3]); ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(L.plotW, yy); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
        ctx.fillStyle = k[1]; ctx.textAlign = "right";
        ctx.fillText((t.side === "sell" ? "short " : "") + k[2] + " " + fmt(val), L.plotW - 4, yy - 3);
      });
    });
    ctx.restore();

    // last price line and label
    var last = bars[n - 1], ly = Math.round(y(last[4])) + 0.5;
    var lastUp = last[4] >= last[1];
    ctx.setLineDash([4, 4]); ctx.strokeStyle = lastUp ? up : down;
    ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(L.plotW, ly); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = lastUp ? up : down;
    ctx.fillRect(L.plotW + 1, ly - 10, L.axisW - 2, 20);
    ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.font = "600 12px system-ui, sans-serif";
    ctx.fillText(fmt(last[4]), L.plotW + 5, ly + 4);
    ctx.font = "12px system-ui, sans-serif";

    // panels below the price
    var hIdx = state.hover !== null ? Math.min(n - 1, Math.max(0, state.hover)) : n - 1;
    var legend = [];
    L.panels.forEach(function (key, k) {
      var pTop = L.priceH + k * PANEL_H + 6, pH = PANEL_H - 12;
      ctx.strokeStyle = line; ctx.globalAlpha = 0.8;
      ctx.beginPath(); ctx.moveTo(0, pTop - 3.5); ctx.lineTo(L.w, pTop - 3.5); ctx.stroke(); ctx.globalAlpha = 1;
      var fixed = { rsi: [0, 100, [30, 70]], adx: [0, 60, [25]], stoch: [0, 100, [20, 80]] }[key];
      var series = [], pmin = Infinity, pmax = -Infinity;
      var pv = function (name) { return function (i) { return A ? A.get("panels", name, i) : null; }; };
      if (key === "vol") series = [{ f: function (i) { return bars[i][5] || 0; }, bars: true }];
      if (key === "rsi") series = [{ f: pv("rsi"), c: COLORS.rsi, name: "RSI" }];
      if (key === "adx") series = [{ f: pv("adx"), c: COLORS.adx, name: "ADX" }];
      if (key === "stoch") series = [{ f: pv("stoch_k"), c: COLORS.k, name: "K" }, { f: pv("stoch_d"), c: COLORS.d, name: "D" }];
      if (key === "macd") series = [{ f: pv("macd_hist"), hist: true, name: "hist" }, { f: pv("macd"), c: COLORS.macd, name: "MACD" },
                                    { f: pv("macd_signal"), c: COLORS.signal, name: "signal" }];
      if (fixed) { pmin = fixed[0]; pmax = fixed[1]; } else {
        series.forEach(function (sr) { for (var i = 0; i < n; i++) { var q = sr.f(i); if (q !== null && q !== undefined) { pmin = Math.min(pmin, q); pmax = Math.max(pmax, q); } } });
        if (key === "vol") pmin = 0;
        if (key === "macd") { var m = Math.max(Math.abs(pmin), Math.abs(pmax)); pmin = -m; pmax = m; }
      }
      if (!isFinite(pmin) || !isFinite(pmax) || pmax === pmin) { pmin = 0; pmax = 1; }
      var py = function (val) { return pTop + (pmax - val) / (pmax - pmin) * pH; };
      if (fixed) fixed[2].forEach(function (g) {
        ctx.strokeStyle = muted; ctx.globalAlpha = 0.5; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(0, Math.round(py(g)) + 0.5); ctx.lineTo(L.plotW, Math.round(py(g)) + 0.5); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = muted; ctx.textAlign = "left";
        ctx.fillText(String(g), L.plotW + 6, py(g) + 4);
      });
      series.forEach(function (sr) {
        if (sr.bars || sr.hist) {
          var zero = sr.hist ? py(0) : pTop + pH;
          for (var i = 0; i < n; i++) {
            var q = sr.f(i);
            if (q === null || q === undefined) continue;
            ctx.fillStyle = sr.hist ? (q >= 0 ? up : down) : (bars[i][4] >= bars[i][1] ? up : down);
            ctx.globalAlpha = sr.hist ? 0.6 : 0.4;
            var yy = py(q); ctx.fillRect((i + 0.5) * L.step - bw / 2, Math.min(yy, zero), bw, Math.max(1, Math.abs(zero - yy)));
            ctx.globalAlpha = 1;
          }
        } else polyline(n, sr.f, py, L.step, sr.c, 1.5);
      });
      ctx.fillStyle = muted; ctx.textAlign = "left";
      ctx.fillText({ vol: "Volume", rsi: "RSI 14", macd: "MACD 12/26/9", adx: "ADX 14", stoch: "Stoch RSI" }[key], 4, pTop + 11);
      series.forEach(function (sr) {
        var q = sr.f(hIdx);
        if (q === null || q === undefined || sr.bars) return;
        legend.push('<span style="color:' + (sr.c || (q >= 0 ? up : down)) + '">' + (sr.name === "hist" ? "MACD hist" : sr.name) +
                    " <b>" + (key === "macd" ? Number(q.toPrecision(4)) : q.toFixed(1)) + "</b></span>");
      });
    });

    // crosshair across price and panels
    if (state.hover !== null) {
      var hx = Math.round((state.hover + 0.5) * L.step) + 0.5;
      ctx.strokeStyle = accent; ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, L.h - L.axisH); ctx.stroke(); ctx.globalAlpha = 1;
    }
    var hb = bars[hIdx];
    var chg = hb[1] ? (hb[4] / hb[1] - 1) * 100 : 0;
    ohlcEl.innerHTML = '<span class="muted">' + new Date(hb[0]).toISOString().slice(0, 16).replace("T", " ") +
      " UTC</span> O <b>" + fmt(hb[1]) + "</b> H <b>" + fmt(hb[2]) + "</b> L <b>" + fmt(hb[3]) +
      "</b> C <b>" + fmt(hb[4]) + '</b> <span class="' + (chg >= 0 ? "good" : "bad") + '">' +
      (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
    if (A) {
      var named = { ema20: "EMA20", ema50: "EMA50", ema200: "EMA200", vwap: "VWAP", supertrend: "Supertrend",
                    tenkan: "Tenkan", kijun: "Kijun", psar: "SAR", bb_upper: "BB up", bb_lower: "BB low" };
      var front = [];
      overlayKeys.forEach(function (k) {
        var q = A.get("overlays", k, hIdx);
        if (q === null || !named[k]) return;
        var col = COLORS[k] || (k === "supertrend" ? (A.get("overlays", "supertrend_dir", hIdx) === 1 ? up : down) :
                                k.indexOf("bb") === 0 ? COLORS.bb : COLORS.psar);
        front.push('<span style="color:' + col + '">' + named[k] + " <b>" + fmt(q) + "</b></span>");
      });
      legend = front.concat(legend);
    } else if (["ema", "bb", "vwap", "st", "ich", "psar", "sr"].some(function (k) { return on[k]; }) || L.panels.some(function (k) { return k !== "vol"; })) {
      legend = ['<span class="muted">Loading indicators…</span>'];
    }
    legendEl.innerHTML = legend.join("");
  }

  function pointer(ev) {
    var r = canvas.getBoundingClientRect();
    var p = ev.touches ? ev.touches[0] : ev;
    var L = layout();
    var x = p.clientX - r.left;
    state.hover = x > L.plotW || !L.bars.length ? null : Math.floor(x / L.step);
    draw();
  }

  // ---- controls -------------------------------------------------------------------------------

  function select(symbol, tf) {
    if (symbol) state.symbol = symbol;
    if (tf) state.tf = tf;
    state.candles = []; state.hover = null; state.studies = null; state.polls = 0;
    titleEl.textContent = state.symbol;
    document.querySelectorAll(".seg[data-tf]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tf === state.tf);
    });
    document.querySelectorAll(".tick").forEach(function (b) {
      b.classList.toggle("active", b.dataset.symbol === state.symbol);
    });
    history.replaceState(null, "", "/markets?symbol=" + encodeURIComponent(state.symbol) +
                         "&tf=" + encodeURIComponent(state.tf));
    draw();
    state.busy = false;
    poll();
  }

  tickersEl.addEventListener("click", function (ev) {
    var b = ev.target.closest(".tick");
    if (b) select(b.dataset.symbol, null);
  });
  document.querySelectorAll(".seg[data-tf]").forEach(function (b) {
    b.addEventListener("click", function () { select(null, b.dataset.tf); });
  });
  canvas.addEventListener("mousemove", pointer);
  canvas.addEventListener("touchstart", pointer, { passive: true });
  canvas.addEventListener("touchmove", pointer, { passive: true });
  canvas.addEventListener("mouseleave", function () { state.hover = null; draw(); });
  window.addEventListener("resize", resize);
  function wake() { state.busy = false; poll(); }  // back from sleep or another tab: poll at once
  document.addEventListener("visibilitychange", function () { if (!document.hidden) wake(); });
  window.addEventListener("pageshow", wake);
  window.addEventListener("online", wake);
  window.addEventListener("focus", wake);

  root.classList.add("js");
  resize();
  poll();
  setInterval(poll, POLL_MS);
  setInterval(status, 1000);
  setInterval(selfHeal, 5000);
})();
