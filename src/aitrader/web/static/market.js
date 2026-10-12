// Live Markets page: polls the dashboard's /api/market endpoints (which read Bybit's public
// API, cached ~1s on the server) every 1.5 seconds and draws a candlestick chart on a canvas.
(function () {
  "use strict";
  var root = document.getElementById("market");
  if (!root) return;
  var state = { symbol: root.dataset.symbol, tf: root.dataset.tf, candles: [], hover: null,
                last: null, lastOk: 0, busy: false, source: "" };
  var POLL_MS = 1500;
  var canvas = document.getElementById("chart");
  var ctx = canvas.getContext("2d");
  var tickersEl = document.getElementById("tickers");
  var statusEl = document.getElementById("live-status");
  var ohlcEl = document.getElementById("ohlc");
  var titleEl = document.getElementById("chart-title");
  var prices = {};

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

  function get(url) {
    return fetch(url, { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function poll() {
    if (state.busy || document.hidden) return;
    state.busy = true;
    var sym = state.symbol, tf = state.tf;
    Promise.all([
      get("/api/market/candles?symbol=" + encodeURIComponent(sym) + "&tf=" + encodeURIComponent(tf)),
      get("/api/market/tickers")
    ]).then(function (res) {
      if (sym === state.symbol && tf === state.tf) {
        state.candles = res[0].candles || [];
        state.source = res[0].source;
        state.note = res[0].note || "";
      }
      renderTickers(res[1].tickers || []);
      state.lastOk = Date.now();
      draw();
    }).catch(function () { /* status line shows the age of the last good update */ })
      .then(function () { state.busy = false; status(); });
  }

  function status() {
    if (!state.lastOk) { statusEl.innerHTML = '<span class="dot"></span> Connecting…'; return; }
    var age = (Date.now() - state.lastOk) / 1000;
    var live = state.source === "live" && age < 10;
    statusEl.className = "live-status " + (live ? "on" : "off");
    statusEl.innerHTML = '<span class="dot"></span> ' +
      (live ? "Live · Bybit" : state.source === "database" ? "Saved prices" : "Reconnecting") +
      " · updated " + (age < 1 ? "just now" : age.toFixed(0) + "s ago") +
      (state.note ? '<span class="muted"> · ' + escapeHtml(state.note) + "</span>" : "");
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
    var plotW = w - axisW, priceH = (h - axisH) * 0.78, volTop = priceH + 6, volH = h - axisH - volTop;
    var barW = w < 500 ? 6 : 8;
    var n = Math.max(10, Math.floor(plotW / barW));
    var bars = state.candles.slice(-n);
    return { w: w, h: h, axisW: axisW, axisH: axisH, plotW: plotW, priceH: priceH, volTop: volTop,
             volH: volH, bars: bars, step: plotW / Math.max(bars.length, 1) };
  }

  function niceStep(span, count) {
    var raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    return (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
  }

  function draw() {
    var L = layout(), bars = L.bars;
    ctx.clearRect(0, 0, L.w, L.h);
    var up = css("--good") || "#22c55e", down = css("--bad") || "#ef4444";
    var line = css("--line") || "#333", muted = css("--muted") || "#888", accent = css("--accent") || "#7aa2ff";
    ctx.font = "12px system-ui, sans-serif";
    if (!bars.length) {
      ctx.fillStyle = muted;
      ctx.fillText(state.lastOk ? "No candles for this pair yet." : "Loading…", 12, 24);
      return;
    }
    var hi = -Infinity, lo = Infinity, vmax = 0;
    bars.forEach(function (b) { hi = Math.max(hi, b[2]); lo = Math.min(lo, b[3]); vmax = Math.max(vmax, b[5] || 0); });
    var pad = (hi - lo) * 0.06 || hi * 0.001 || 1;
    hi += pad; lo -= pad;
    var y = function (v) { return 8 + (hi - v) / (hi - lo) * (L.priceH - 8); };

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
    for (var i = bars.length - 1; i >= 0; i -= every) {
      var tx = (i + 0.5) * L.step;
      if (tx < 24) break;
      ctx.fillText(timeLabel(bars[i][0]), tx, L.h - 6);
    }

    // volume and candles
    var bw = Math.max(1, L.step * 0.66);
    bars.forEach(function (b, i) {
      var x = (i + 0.5) * L.step, isUp = b[4] >= b[1], c = isUp ? up : down;
      if (vmax) {
        var vh = (b[5] || 0) / vmax * L.volH;
        ctx.globalAlpha = 0.35; ctx.fillStyle = c;
        ctx.fillRect(x - bw / 2, L.volTop + L.volH - vh, bw, vh);
        ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = c; ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, y(b[2])); ctx.lineTo(Math.round(x) + 0.5, y(b[3])); ctx.stroke();
      var top = y(Math.max(b[1], b[4])), bot = y(Math.min(b[1], b[4]));
      ctx.fillStyle = c; ctx.fillRect(x - bw / 2, top, bw, Math.max(1, bot - top));
    });

    // last price line and label
    var last = bars[bars.length - 1], ly = Math.round(y(last[4])) + 0.5;
    var lastUp = last[4] >= last[1];
    ctx.setLineDash([4, 4]); ctx.strokeStyle = lastUp ? up : down;
    ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(L.plotW, ly); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = lastUp ? up : down;
    ctx.fillRect(L.plotW + 1, ly - 10, L.axisW - 2, 20);
    ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.font = "600 12px system-ui, sans-serif";
    ctx.fillText(fmt(last[4]), L.plotW + 5, ly + 4);
    ctx.font = "12px system-ui, sans-serif";

    // crosshair
    var hb = state.hover !== null ? bars[Math.min(bars.length - 1, Math.max(0, state.hover))] : last;
    if (state.hover !== null) {
      var hx = Math.round((state.hover + 0.5) * L.step) + 0.5;
      ctx.strokeStyle = accent; ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, L.h - L.axisH); ctx.stroke(); ctx.globalAlpha = 1;
    }
    var chg = hb[1] ? (hb[4] / hb[1] - 1) * 100 : 0;
    ohlcEl.innerHTML = '<span class="muted">' + new Date(hb[0]).toISOString().slice(0, 16).replace("T", " ") +
      " UTC</span> O <b>" + fmt(hb[1]) + "</b> H <b>" + fmt(hb[2]) + "</b> L <b>" + fmt(hb[3]) +
      "</b> C <b>" + fmt(hb[4]) + '</b> <span class="' + (chg >= 0 ? "good" : "bad") + '">' +
      (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
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
    state.candles = []; state.hover = null;
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
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  root.classList.add("js");
  resize();
  poll();
  setInterval(poll, POLL_MS);
  setInterval(status, 1000);
})();
