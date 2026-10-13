// Replay backtest: loads candles, the strategies' trades and the indicator values for a past
// period from /api/replay, then plays the candles one at a time (1x = one candle per second). It
// draws the indicators the chosen strategies use (ChartStudies, shared with the Markets chart),
// shows each buy and short when its candle appears, reads the market at the current candle in
// plain words, and keeps a live win-rate table per strategy. Nothing drawn looks past the
// current candle.
(function () {
  "use strict";
  var root = document.getElementById("replay");
  if (!root) return;
  var form = document.getElementById("replay-form");
  var dlForm = document.getElementById("replay-download");
  var msgEl = document.getElementById("replay-msg");
  var canvas = document.getElementById("chart");
  var ctx = canvas.getContext("2d");
  var ohlcEl = document.getElementById("ohlc");
  var titleEl = document.getElementById("chart-title");
  var playBtn = document.getElementById("play");
  var stepBtn = document.getElementById("step");
  var restartBtn = document.getElementById("restart");
  var seek = document.getElementById("seek");
  var clockEl = document.getElementById("clock");
  var statsEl = document.getElementById("replay-stats");
  var boardEl = document.getElementById("board");
  var logEl = document.getElementById("log");
  var legendEl = document.getElementById("legend");
  var readEl = document.getElementById("read");
  var usedEl = document.getElementById("studies-used");
  var CS = window.ChartStudies, PANEL_H = CS.PANEL_H;
  var sw = CS.switches("aitrader.replay.studies", function () { resize(); });

  var S = { data: null, pos: 0, playing: false, speed: 1, timer: null, names: {}, hover: null, tf: "1h" };

  function css(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(x) {
    if (x === null || x === undefined || isNaN(x)) return "-";
    var d = x >= 100 ? 2 : x >= 1 ? 4 : x >= 0.01 ? 5 : 8;
    return Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  // times in the dashboard's zone (IST unless AITRADER_TZ says otherwise), set by the server
  var TZ_MS = (Number(document.documentElement.getAttribute("data-tz-offset")) || 0) * 60000;
  var TZ = document.documentElement.getAttribute("data-tz-label") || "UTC";
  function when(ms) { return new Date(ms + TZ_MS).toISOString().slice(0, 16).replace("T", " "); }
  function r(x) { return (x >= 0 ? "+" : "") + x.toFixed(2) + "R"; }

  // ---- loading ----------------------------------------------------------------------------------

  function params() {
    var p = new URLSearchParams(new FormData(form));
    return p;
  }

  function load(ev) {
    if (ev) ev.preventDefault();
    stop();
    var p = params();
    history.replaceState(null, "", "/replay?" + p.toString());
    msgEl.textContent = "Running the strategies over that period…";
    usedEl.textContent = "";
    dlForm.classList.add("hidden");
    setEnabled(false);
    fetch("/api/replay?" + p.toString(), { credentials: "same-origin" }).then(function (res) {
      if (res.status === 401) { location.href = "/login"; return null; }
      return res.json().then(function (j) { return { ok: res.ok, j: j }; });
    }).then(function (x) {
      if (!x) return;
      if (!x.ok) { msgEl.textContent = x.j.error || "Couldn't load that period."; return; }
      var d = x.j;
      S.tf = d.timeframe;
      if (!d.candles.length) {
        msgEl.textContent = "No saved " + d.timeframe + " prices for " + d.symbol + " in that period yet.";
        ["symbol", "tf", "month", "months"].forEach(function (k) { dlForm.elements[k].value = p.get(k); });
        dlForm.classList.remove("hidden");
        S.data = null; draw(); renderStats();
        return;
      }
      S.data = d; S.names = {};
      d.strategies.forEach(function (s) { S.names[s.id] = s.name; });
      msgEl.textContent = (d.top && d.strategies.length ? "The best " + d.strategies.length + " ranked strategies that trade " + d.timeframe :
        d.strategies.length + " strateg" + (d.strategies.length === 1 ? "y" : "ies") + " trade " + d.timeframe) +
        (d.top && d.ranked_on_tf ? " (ranked on their " + d.timeframe + " backtest results)" : "") +
        " · " + d.candles.length + " candles · " + d.trades.length + " trades in this period." +
        (d.fee_skipped ? " " + d.fee_skipped + " more signals were skipped because fees and slippage would cost over " +
          Math.round(d.max_cost_r * 100) + "% of the risk." : "") +
        (d.strategies.length ? "" : " No strategy uses this timeframe: try 1h or 4h.");
      titleEl.textContent = d.symbol + " · " + d.timeframe + " · from " + when(d.start).slice(0, 7);
      seek.max = String(d.candles.length);
      S.pos = Math.min(60, d.candles.length);
      setEnabled(true);
      showUsed(d);
      update();
    }).catch(function (e) { msgEl.textContent = "Couldn't reach the server."; if (window.console) console.error(e); });
  }

  // switch on the indicators the loaded strategies use, with how many use each on its switch
  // (for "All strategies" that would be nearly every indicator, so the switches stay as they are)
  var AUTO_MAX = 30;
  function showUsed(d) {
    var used = d.indicators || {}, keys = Object.keys(used);
    if (!keys.length) { usedEl.textContent = ""; return; }
    if (d.strategies.length <= AUTO_MAX) sw.set(keys, ["vol"]);
    else document.querySelectorAll(".chip[data-study] small").forEach(function (x) { x.remove(); });
    var names = [];
    document.querySelectorAll(".chip[data-study]").forEach(function (b) {
      var k = b.dataset.study;
      if (!(k in used)) return;
      var badge = document.createElement("small");
      badge.textContent = used[k];
      b.appendChild(document.createTextNode(" ")); b.appendChild(badge);
      names.push(b.textContent.replace(/\s+\d+$/, "").trim() + " (" + used[k] + ")");
    });
    usedEl.textContent = "Indicators these " + d.strategies.length + " strategies use (how many use each): " +
      names.join(", ") + ".";
    resize();
  }

  function setEnabled(on) {
    [playBtn, stepBtn, restartBtn, seek].forEach(function (b) { b.disabled = !on; });
  }

  // ---- playback ---------------------------------------------------------------------------------

  function tick() {
    if (!S.data) return;
    var stepN = S.speed >= 50 ? Math.round(S.speed / 10) : 1;
    S.pos = Math.min(S.data.candles.length, S.pos + stepN);
    update();
    if (S.pos >= S.data.candles.length) stop();
  }
  function interval() { return S.speed >= 50 ? 100 : 1000 / S.speed; }
  function play() {
    if (!S.data) return;
    if (S.pos >= S.data.candles.length) { S.pos = 1; update(); }
    S.playing = true; playBtn.textContent = "Pause";
    clearInterval(S.timer); S.timer = setInterval(tick, interval());
  }
  function stop() {
    S.playing = false; playBtn.textContent = "Play";
    clearInterval(S.timer); S.timer = null;
  }

  playBtn.addEventListener("click", function () { S.playing ? stop() : play(); });
  stepBtn.addEventListener("click", function () { stop(); tick(); });
  restartBtn.addEventListener("click", function () { stop(); S.pos = 1; update(); });
  seek.addEventListener("input", function () { S.pos = Math.max(1, parseInt(seek.value, 10) || 1); update(); });
  document.querySelectorAll("#speeds .seg").forEach(function (b) {
    b.addEventListener("click", function () {
      S.speed = parseInt(b.dataset.speed, 10) || 1;
      document.querySelectorAll("#speeds .seg").forEach(function (x) { x.classList.toggle("active", x === b); });
      if (S.playing) { clearInterval(S.timer); S.timer = setInterval(tick, interval()); }
    });
  });
  form.addEventListener("submit", load);

  // ---- what has happened up to the current candle ---------------------------------------------

  function now() { return S.data.candles[S.pos - 1][0]; }

  function seen() {
    var t = now(), closed = [], open = [];
    S.data.trades.forEach(function (tr) {
      if (tr.entry_ts > t) return;
      if (tr.exit_ts && tr.exit_ts <= t) closed.push(tr); else open.push(tr);
    });
    return { closed: closed, open: open };
  }

  function update() {
    if (S.data) {
      seek.value = String(S.pos);
      clockEl.textContent = when(now()) + " " + TZ + " · candle " + S.pos + " of " + S.data.candles.length;
    }
    draw(); renderStats();
  }

  function renderStats() {
    if (!S.data) { statsEl.innerHTML = ""; boardEl.innerHTML = ""; logEl.innerHTML = ""; return; }
    var s = seen(), wins = 0, total = 0;
    s.closed.forEach(function (t) { if (t.r > 0) wins++; total += t.r; });
    var n = s.closed.length, wr = n ? wins / n * 100 : 0;
    var tiles = [["Trades closed", n, ""], ["Win rate", n ? wr.toFixed(1) + "%" : "-", n ? (wr >= 50 ? "good" : "") : ""],
                 ["Total", n ? r(total) : "-", total > 0 ? "good" : total < 0 ? "bad" : ""],
                 ["Open now", s.open.length, ""]];
    statsEl.innerHTML = tiles.map(function (t) {
      return '<div class="stat"><span class="k">' + t[0] + '</span><span class="v ' + t[2] + '">' + t[1] + "</span></div>";
    }).join("");

    var by = {};
    s.closed.forEach(function (t) {
      var b = by[t.strategy] || (by[t.strategy] = { id: t.strategy, n: 0, w: 0, r: 0 });
      b.n++; b.r += t.r; if (t.r > 0) b.w++;
    });
    var rows = Object.keys(by).map(function (k) { return by[k]; });
    rows.sort(function (a, b) { return b.r - a.r || b.w / b.n - a.w / a.n; });
    boardEl.innerHTML = rows.length ? '<table><tr><th>#</th><th>Strategy</th><th>Trades</th><th>Win %</th>' +
      '<th>Avg / trade</th><th>Total</th></tr>' + rows.map(function (b, i) {
        var cls = b.r > 0 ? "good" : b.r < 0 ? "bad" : "";
        return "<tr><td class=muted>" + (i + 1) + "</td><td><b>" + esc(S.names[b.id] || b.id) + "</b></td><td>" + b.n +
          "</td><td>" + (b.w / b.n * 100).toFixed(0) + '%</td><td class="' + cls + '">' + r(b.r / b.n) +
          '</td><td class="' + cls + '"><b>' + r(b.r) + "</b></td></tr>";
      }).join("") + "</table>" : '<p class="muted">No closed trades yet. Press Play.</p>';

    var recent = s.closed.concat(s.open).sort(function (a, b) {
      return (b.exit_ts && b.exit_ts <= now() ? b.exit_ts : b.entry_ts) - (a.exit_ts && a.exit_ts <= now() ? a.exit_ts : a.entry_ts);
    }).slice(0, 40);
    logEl.innerHTML = recent.length ? '<ul class="trade-log">' + recent.map(function (t) {
      var done = t.exit_ts && t.exit_ts <= now();
      var short = t.side === "sell";
      return "<li><b>" + esc(S.names[t.strategy] || t.strategy) + '</b> <span class="muted small">' +
        (short ? "sold short " : "bought ") +
        when(t.entry_ts) + " at " + fmt(t.entry) + " · stop " + fmt(t.stop) +
        (t.target ? " · target " + fmt(t.target) : "") + "</span><br>" +
        (done ? (short ? "bought back " : "sold ") + when(t.exit_ts) + " at " + fmt(t.exit) + ' (' + esc(t.reason) + ') <b class="' +
          (t.r > 0 ? "good" : "bad") + '">' + r(t.r) + "</b>" : '<span class="warn">open</span>') + "</li>";
    }).join("") + "</ul>" : "";
  }

  // ---- chart ------------------------------------------------------------------------------------

  function resize() {
    var dpr = window.devicePixelRatio || 1;
    var base = window.innerWidth <= 640 ? 340 : 440;
    canvas.style.height = (base + CS.panels(sw.on).length * PANEL_H) + "px";
    canvas.width = Math.round(canvas.clientWidth * dpr);
    canvas.height = Math.round(canvas.clientHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function niceStep(span, count) {
    var raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), q = raw / mag;
    return (q < 1.5 ? 1 : q < 3 ? 2 : q < 7 ? 5 : 10) * mag;
  }

  // indicator value for visible candle ``i`` (the series cover every loaded candle)
  function getter(first) {
    var st = S.data && S.data.studies;
    if (!st || !st.overlays) return null;
    return function (group, key, i) {
      var arr = key === null ? st[group] : (st[group] || {})[key];
      var v = arr ? arr[first + i] : null;
      return v === undefined ? null : v;
    };
  }

  function draw() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);
    var up = css("--good") || "#22c55e", down = css("--bad") || "#ef4444", line = css("--line") || "#333";
    var muted = css("--muted") || "#888", accent = css("--accent") || "#7aa2ff";
    ctx.font = "12px system-ui, sans-serif";
    if (!S.data || !S.pos) {
      ctx.fillStyle = muted; ctx.fillText("Pick a period and press Load.", 12, 24);
      ohlcEl.innerHTML = ""; legendEl.innerHTML = ""; readEl.innerHTML = ""; return;
    }
    var on = sw.on, nPanels = CS.panels(on).length;
    var all = S.data.candles.slice(0, S.pos);
    var axisW = 74, axisH = 22, plotW = w - axisW, priceH = h - axisH - nPanels * PANEL_H;
    var barW = w < 500 ? 6 : 8, n = Math.max(20, Math.floor(plotW / barW));
    var first = Math.max(0, all.length - n), bars = all.slice(first), step = plotW / n;
    var get = getter(first);
    // levels, Fibonacci and the volume profile from the candles up to now only
    var levels = on.sr ? CS.levelsOf(all.slice(-300)) : null;
    var fib = on.fib ? CS.fibOf(all.slice(-100)) : null;
    var prof = on.vp ? CS.profileOf(all.slice(-100)) : null;
    var O = { ctx: ctx, on: on, get: get, n: bars.length, step: step, plotW: plotW, priceH: priceH, bars: bars,
              levels: levels, fib: fib, prof: prof, fmt: fmt };
    var hi = -Infinity, lo = Infinity;
    var room = function (v) { if (v !== null && v !== undefined && isFinite(v)) { hi = Math.max(hi, v); lo = Math.min(lo, v); } };
    bars.forEach(function (b) { room(b[2]); room(b[3]); });
    var blo = lo, bhi = hi, span = bhi - blo;
    // indicators widen the scale, but not so far that the candles get squashed
    CS.widen(O, function (v) { if (v !== null && v !== undefined && v > blo - span && v < bhi + span) room(v); });
    var s = seen();
    s.open.forEach(function (t) { room(t.target); room(t.stop); });
    var pad = (hi - lo) * 0.06 || hi * 0.001 || 1; hi += pad; lo -= pad;
    var y = function (v) { return 8 + (hi - v) / (hi - lo) * (priceH - 8); };
    O.y = y; O.lo = lo; O.hi = hi;
    var xOf = function (ts) {
      var i = Math.round((ts - bars[0][0]) / (bars.length > 1 ? bars[1][0] - bars[0][0] : 1));
      return (i + 0.5) * step;
    };

    var sv = niceStep(hi - lo, 5);
    ctx.strokeStyle = line; ctx.fillStyle = muted; ctx.lineWidth = 1; ctx.textAlign = "left";
    for (var v = Math.ceil(lo / sv) * sv; v <= hi; v += sv) {
      var gy = Math.round(y(v)) + 0.5;
      ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(plotW, gy); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(fmt(v), plotW + 6, gy + 4);
    }
    ctx.textAlign = "center";
    var every = Math.max(1, Math.round(110 / step));
    for (var i = bars.length - 1; i >= 0; i -= every) {
      var tx = (i + 0.5) * step;
      if (tx < 30) break;
      ctx.fillText(when(bars[i][0]).slice(5, S.tf === "1d" || S.tf === "4h" ? 10 : 16), tx, h - 6);
    }

    CS.drawUnder(O);  // Ichimoku cloud, Bollinger fill, levels, volume profile, Fibonacci lines
    var bw = Math.max(1, step * 0.66);
    bars.forEach(function (b, k) {
      var x = (k + 0.5) * step, c = b[4] >= b[1] ? up : down;
      ctx.strokeStyle = c; ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, y(b[2])); ctx.lineTo(Math.round(x) + 0.5, y(b[3])); ctx.stroke();
      var top = y(Math.max(b[1], b[4])), bot = y(Math.min(b[1], b[4]));
      ctx.fillStyle = c; ctx.fillRect(x - bw / 2, top, bw, Math.max(1, bot - top));
    });
    CS.drawOver(O);  // indicator lines, SAR dots, pattern letters, Fibonacci labels

    // trades: open ones with their stop and target, closed ones as entry-to-exit lines
    var t0 = bars[0][0], tEnd = now();
    s.open.forEach(function (t) {
      var x1 = Math.max(0, xOf(t.entry_ts)), x2 = plotW;
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = down; ctx.beginPath(); ctx.moveTo(x1, y(t.stop)); ctx.lineTo(x2, y(t.stop)); ctx.stroke();
      if (t.target) { ctx.strokeStyle = up; ctx.beginPath(); ctx.moveTo(x1, y(t.target)); ctx.lineTo(x2, y(t.target)); ctx.stroke(); }
      ctx.setLineDash([]);
    });
    s.closed.concat(s.open).forEach(function (t) {
      var done = t.exit_ts && t.exit_ts <= tEnd;
      if ((done ? t.exit_ts : tEnd) < t0) return;
      var xe = xOf(t.entry_ts), ye = y(t.entry);
      if (done) {
        var xx = xOf(t.exit_ts), yx = y(t.exit), col = t.r > 0 ? up : down;
        ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(xe, ye); ctx.lineTo(xx, yx); ctx.stroke();
        ctx.lineWidth = 1; ctx.fillStyle = col; ctx.beginPath(); ctx.arc(xx, yx, 4, 0, 6.3); ctx.fill();
      }
      if (xe >= 0) {  // entry marker: a triangle pointing up under a buy, down over a short
        var sh = t.side === "sell", d = sh ? -1 : 1;
        ctx.fillStyle = sh ? down : accent; ctx.beginPath();
        ctx.moveTo(xe, ye + 4 * d); ctx.lineTo(xe - 6, ye + 14 * d); ctx.lineTo(xe + 6, ye + 14 * d); ctx.closePath(); ctx.fill();
      }
    });

    var last = bars[bars.length - 1], ly = Math.round(y(last[4])) + 0.5, lc = last[4] >= last[1] ? up : down;
    ctx.setLineDash([4, 4]); ctx.strokeStyle = lc; ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(plotW, ly); ctx.stroke();
    ctx.setLineDash([]); ctx.fillStyle = lc; ctx.fillRect(plotW + 1, ly - 10, axisW - 2, 20);
    ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.font = "600 12px system-ui, sans-serif";
    ctx.fillText(fmt(last[4]), plotW + 5, ly + 4);
    ctx.font = "12px system-ui, sans-serif";

    var hIdx = S.hover !== null && bars[S.hover] ? S.hover : bars.length - 1, hb = bars[hIdx];
    O.top = priceH; O.w = w; O.bw = bw; O.hIdx = hIdx;
    var panelLegend = CS.drawPanels(O);  // volume, RSI, MACD, ADX, Stoch RSI, ATR, CCI, Williams %R
    if (S.hover !== null && bars[S.hover]) {
      var hx = Math.round((S.hover + 0.5) * step) + 0.5;
      ctx.strokeStyle = accent; ctx.globalAlpha = 0.6; ctx.beginPath(); ctx.moveTo(hx, 0); ctx.lineTo(hx, h - axisH); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    var chg = hb[1] ? (hb[4] / hb[1] - 1) * 100 : 0;
    ohlcEl.innerHTML = '<span class="muted">' + when(hb[0]) + " " + TZ + "</span> O <b>" + fmt(hb[1]) + "</b> H <b>" + fmt(hb[2]) +
      "</b> L <b>" + fmt(hb[3]) + "</b> C <b>" + fmt(hb[4]) + '</b> <span class="' + (chg >= 0 ? "good" : "bad") + '">' +
      (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
    legendEl.innerHTML = CS.overlayLegend(O, hIdx).concat(panelLegend).join("");
    var said = CS.read(get, bars, hIdx);
    readEl.innerHTML = said ? "<b>Market " + (S.hover !== null ? "at this candle" : "now") + ":</b> " + said : "";
    S.layout = { step: step, plotW: plotW, count: bars.length };
  }

  function pointer(ev) {
    if (!S.layout) return;
    var rect = canvas.getBoundingClientRect(), p = ev.touches ? ev.touches[0] : ev, x = p.clientX - rect.left;
    S.hover = x > S.layout.plotW ? null : Math.min(S.layout.count - 1, Math.floor(x / S.layout.step));
    draw();
  }
  canvas.addEventListener("mousemove", pointer);
  // a phone sends a mouseleave right after each tap: keep the tapped candle's values on screen
  var touchedAt = 0;
  canvas.addEventListener("touchstart", function (ev) { touchedAt = Date.now(); pointer(ev); }, { passive: true });
  canvas.addEventListener("touchmove", pointer, { passive: true });
  canvas.addEventListener("mouseleave", function () { if (Date.now() - touchedAt < 1500) return; S.hover = null; draw(); });
  window.addEventListener("resize", resize);
  resize();
  if (new URLSearchParams(location.search).get("month")) load();
})();
