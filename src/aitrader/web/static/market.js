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
  var CS = window.ChartStudies, PANEL_H = CS.PANEL_H;
  var canvas = document.getElementById("chart");
  var ctx = canvas.getContext("2d");
  var tickersEl = document.getElementById("tickers");
  var statusEl = document.getElementById("live-status");
  var ohlcEl = document.getElementById("ohlc");
  var titleEl = document.getElementById("chart-title");
  var legendEl = document.getElementById("legend");
  var prices = {};

  // ---- indicator switches (remembered in this browser) ---------------------------------------

  var sw = CS.switches("aitrader.studies", function () { resize(); }), on = sw.on;
  var resetBtn = document.getElementById("studies-reset");
  if (resetBtn) resetBtn.addEventListener("click", function () { sw.reset(); resize(); });
  function panels() { return CS.panels(on); }

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
  // times in the dashboard's zone (IST unless AITRADER_TZ says otherwise), set by the server
  var TZ_MS = (Number(document.documentElement.getAttribute("data-tz-offset")) || 0) * 60000;
  var TZ = document.documentElement.getAttribute("data-tz-label") || "UTC";
  function timeLabel(ms) {
    var d = new Date(ms + TZ_MS), p = function (n) { return (n < 10 ? "0" : "") + n; };
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
    // LIVE: Bybit answered in the last 10 s; DELAYED: under a minute ago; STALE: saved prices or
    // older; DISCONNECTED: nothing for 3 minutes
    var label = age > 180 ? "DISCONNECTED" : state.source !== "live" || age >= 60 ? "STALE"
      : age >= 10 ? "DELAYED" : "LIVE";
    var live = label === "LIVE";
    statusEl.className = "live-status " + (live ? "on" : "off") + (label === "DISCONNECTED" ? " down" : "");
    statusEl.innerHTML = '<span class="dot"></span> <b>' + label + "</b> · " +
      (live ? "Bybit" : state.source === "database" ? "saved prices" : label === "DELAYED" ? "Bybit" : "reconnecting") +
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
        (t.gainer ? ' <span class="hot">top gainer</span>' : "") +
        (t.held ? ' <span class="hot">open position</span>' : "") +
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
      var arr = key === null ? S[group] : (S[group] || {})[key], j = out.idx[i];
      return arr && j >= 0 ? arr[j] : null;
    };
    out.S = S;
    return out;
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
    var fib = A && on.fib ? A.S.fib : null, prof = A && on.vp ? A.S.profile : null;
    var O = { ctx: ctx, on: on, get: A ? A.get : null, n: n, step: L.step, plotW: L.plotW, bars: bars, fmt: fmt,
              levels: A ? A.S.levels : null, fib: fib, prof: prof };
    CS.widen(O, room);
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
    O.y = y; O.lo = lo; O.hi = hi;
    CS.drawUnder(O);  // Ichimoku cloud, Bollinger fill, levels, volume profile, Fibonacci lines

    // candles (volume moved to its own panel)
    var bw = Math.max(1, L.step * 0.66);
    bars.forEach(function (b, i) {
      var x = (i + 0.5) * L.step, isUp = b[4] >= b[1], col = isUp ? up : down;
      ctx.strokeStyle = col; ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, y(b[2])); ctx.lineTo(Math.round(x) + 0.5, y(b[3])); ctx.stroke();
      var t0 = y(Math.max(b[1], b[4])), b0 = y(Math.min(b[1], b[4]));
      ctx.fillStyle = col; ctx.fillRect(x - bw / 2, t0, bw, Math.max(1, b0 - t0));
    });

    CS.drawOver(O);  // indicator lines, SAR dots, pattern letters, Fibonacci labels

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
    O.top = L.priceH; O.w = L.w; O.bw = bw; O.hIdx = hIdx;
    var legend = CS.drawPanels(O);

    // crosshair across price and panels
    if (state.hover !== null) {
      var hx = Math.round((state.hover + 0.5) * L.step) + 0.5;
      ctx.strokeStyle = accent; ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, L.h - L.axisH); ctx.stroke(); ctx.globalAlpha = 1;
    }
    var hb = bars[hIdx];
    var chg = hb[1] ? (hb[4] / hb[1] - 1) * 100 : 0;
    ohlcEl.innerHTML = '<span class="muted">' + new Date(hb[0] + TZ_MS).toISOString().slice(0, 16).replace("T", " ") +
      " " + TZ + "</span> O <b>" + fmt(hb[1]) + "</b> H <b>" + fmt(hb[2]) + "</b> L <b>" + fmt(hb[3]) +
      "</b> C <b>" + fmt(hb[4]) + '</b> <span class="' + (chg >= 0 ? "good" : "bad") + '">' +
      (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
    if (A) legend = CS.overlayLegend(O, hIdx).concat(legend);
    else if (CS.anyOn(on)) legend = ['<span class="muted">Loading indicators…</span>'];
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
  // a phone sends a mouseleave right after each tap: keep the tapped candle's values on screen
  var touchedAt = 0;
  canvas.addEventListener("touchstart", function (ev) { touchedAt = Date.now(); pointer(ev); }, { passive: true });
  canvas.addEventListener("touchmove", pointer, { passive: true });
  canvas.addEventListener("mouseleave", function () { if (Date.now() - touchedAt < 1500) return; state.hover = null; draw(); });
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
