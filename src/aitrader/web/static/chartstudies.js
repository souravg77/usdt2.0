// Indicator drawing shared by the Markets chart (market.js) and the Replay chart (replay.js):
// the overlays on the price (EMA, Bollinger, VWAP, Supertrend, Ichimoku, SAR, Keltner, support/
// resistance, Fibonacci, volume profile, candle patterns), the panels below it (volume, RSI, MACD,
// ADX, Stoch RSI, ATR, CCI, Williams %R, Force index, OBV, MFI, CMF, ROC; overlays also SMA and Donchian), the legend values, and a plain-words read of the market
// at one candle. The values come from the server (web/studies.py, the strategies' own formulas).
// Drawing only: nothing here trades.
(function () {
  "use strict";
  var COLORS = { ema20: "#f5c542", ema50: "#4cc3ff", ema200: "#c084fc", bb: "#94a3b8", vwap: "#f472b6",
                 tenkan: "#38bdf8", kijun: "#fb923c", psar: "#fb923c", rsi: "#c084fc", macd: "#4cc3ff",
                 signal: "#fb923c", adx: "#f5c542", k: "#2dd4bf", d: "#f472b6", sr: "#a3a3a3",
                 kc: "#a78bfa", fib: "#eab308", vp: "#64748b", atr: "#f97316", cci: "#22d3ee", willr: "#e879f9",
                 sma20: "#fde68a", sma50: "#93c5fd", sma200: "#f9a8d4", dc: "#60a5fa", fi: "#34d399", obv: "#a3e635",
                 mfi: "#fbbf24", cmf: "#f87171", roc: "#93c5fd" };
  var PANELS = ["vol", "rsi", "macd", "adx", "stoch", "atr", "cci", "willr", "fi", "obv", "mfi", "cmf", "roc"];
  var OVERLAYS = ["ema", "sma", "dc", "bb", "vwap", "st", "ich", "psar", "sr", "kc", "fib", "vp", "pat"];
  var PATTERN_NAMES = { E: "engulfing", H: "hammer", D: "doji", I: "inside bar", N: "NR7 (narrowest range of 7)" };
  var PANEL_H = 84;

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function theme() {
    return { up: css("--good") || "#22c55e", down: css("--bad") || "#ef4444", line: css("--line") || "#333",
             muted: css("--muted") || "#888", accent: css("--accent") || "#7aa2ff", fg: css("--fg") || "#ddd",
             card: css("--card") || "#fff" };
  }

  // ---- switches (the .chip[data-study] buttons), remembered per page in this browser ----------

  function switches(storeKey, onChange) {
    var on = {}, chips = document.querySelectorAll(".chip[data-study]"), saved = null;
    try { saved = JSON.parse(localStorage.getItem(storeKey) || "null"); } catch (e) { saved = null; }
    function mark(b) { b.setAttribute("aria-pressed", on[b.dataset.study] ? "true" : "false"); }
    function save() { try { localStorage.setItem(storeKey, JSON.stringify(on)); } catch (e) { /* private window */ } }
    chips.forEach(function (b) {
      on[b.dataset.study] = saved && b.dataset.study in saved ? !!saved[b.dataset.study] : b.dataset.on === "1";
      mark(b);
      b.addEventListener("click", function () { on[b.dataset.study] = !on[b.dataset.study]; mark(b); save(); onChange(); });
    });
    return {
      on: on,
      // turn on exactly ``keys`` (plus what is always on), forgetting this browser's own choice
      set: function (keys, always) {
        try { localStorage.removeItem(storeKey); } catch (e) { /* private window */ }
        chips.forEach(function (b) {
          var k = b.dataset.study;
          on[k] = keys.indexOf(k) >= 0 || (always || []).indexOf(k) >= 0;
          mark(b);
          var badge = b.querySelector("small");
          if (badge) badge.remove();
        });
      },
      reset: function () {
        try { localStorage.removeItem(storeKey); } catch (e) { /* private window */ }
        chips.forEach(function (b) { on[b.dataset.study] = b.dataset.on === "1"; mark(b); });
      }
    };
  }

  function panels(on) { return PANELS.filter(function (k) { return on[k]; }); }
  function anyOn(on) {
    return OVERLAYS.some(function (k) { return on[k]; }) || PANELS.some(function (k) { return k !== "vol" && on[k]; });
  }

  function overlayKeys(on) {
    var k = [];
    if (on.ema) k.push("ema20", "ema50", "ema200");
    if (on.sma) k.push("sma20", "sma50", "sma200");
    if (on.dc) k.push("dc_upper", "dc_lower");
    if (on.bb) k.push("bb_upper", "bb_lower");
    if (on.vwap) k.push("vwap");
    if (on.st) k.push("supertrend");
    if (on.ich) k.push("tenkan", "kijun", "span_a", "span_b");
    if (on.psar) k.push("psar");
    if (on.kc) k.push("kc_upper", "kc_mid", "kc_lower");
    return k;
  }

  function polyline(ctx, n, val, y, step, color, width, dash) {
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

  // ---- Fibonacci and volume profile of a window of candles (as web/studies.py and the strategies) --

  var FIB_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786];

  function fibOf(bars) {
    if (bars.length < 2) return null;
    var hiI = 0, loI = 0;
    bars.forEach(function (b, i) { if (b[2] > bars[hiI][2]) hiI = i; if (b[3] < bars[loI][3]) loI = i; });
    var hi = bars[hiI][2], lo = bars[loI][3], span = hi - lo, up = hiI > loI;
    if (!(span > 0)) return null;
    return { leg: up ? "up" : "down", high: hi, low: lo, levels: FIB_RATIOS.map(function (r) {
      return { ratio: r, price: up ? hi - r * span : lo + r * span };
    }) };
  }

  function profileOf(bars, nBins) {
    nBins = nBins || 24;
    if (bars.length < 2) return null;
    var lo = Infinity, hi = -Infinity, total = 0;
    bars.forEach(function (b) { lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); total += b[5] || 0; });
    if (!(total > 0) || !(hi > lo)) return null;
    var width = (hi - lo) / nBins, vol = [];
    for (var i = 0; i < nBins; i++) vol.push(0);
    bars.forEach(function (b) {
      var tp = (b[2] + b[3] + b[4]) / 3, k = Math.min(nBins - 1, Math.max(0, Math.floor((tp - lo) / width)));
      vol[k] += b[5] || 0;
    });
    var poc = 0;
    vol.forEach(function (v, i) { if (v > vol[poc]) poc = i; });
    var a = poc, z = poc, inside = vol[poc];
    while (inside < 0.7 * total && (a > 0 || z < nBins - 1)) {
      var below = a > 0 ? vol[a - 1] : -1, above = z < nBins - 1 ? vol[z + 1] : -1;
      if (above >= below) { z++; inside += above; } else { a--; inside += below; }
    }
    return { bins: vol.map(function (v, i) { return { low: lo + i * width, high: lo + (i + 1) * width, volume: v }; }),
             poc: lo + (poc + 0.5) * width, vah: lo + (z + 1) * width, val: lo + a * width };
  }

  // support/resistance as indicators/core.py support_resistance: swing highs and lows (a swing
  // is known only ``right`` candles later, so nothing here looks ahead of the last candle given),
  // sorted and clustered within ``tol`` of price; levels touched at least twice, most touched first
  function levelsOf(bars, left, right, tol, keep) {
    left = left || 3; right = right || 3; tol = tol || 0.003; keep = keep || 8;
    var win = left + right + 1, pts = [];
    for (var j = win - 1; j < bars.length; j++) {
      var c = bars[j - right], hiMax = -Infinity, loMin = Infinity;
      for (var k = j - win + 1; k <= j; k++) { hiMax = Math.max(hiMax, bars[k][2]); loMin = Math.min(loMin, bars[k][3]); }
      if (c[2] === hiMax) pts.push(c[2]);
      if (c[3] === loMin) pts.push(c[3]);
    }
    pts.sort(function (a, b) { return a - b; });
    var groups = [];
    pts.forEach(function (p) {
      var g = groups[groups.length - 1];
      if (g) {
        var mean = g.reduce(function (a, b) { return a + b; }, 0) / g.length;
        if (Math.abs(p - mean) / p <= tol) { g.push(p); return; }
      }
      groups.push([p]);
    });
    return groups.filter(function (g) { return g.length >= 2; }).map(function (g) {
      return { price: g.reduce(function (a, b) { return a + b; }, 0) / g.length, touches: g.length };
    }).sort(function (a, b) { return b.touches - a.touches; }).slice(0, keep);
  }

  function fibLevels(f) {
    return [{ ratio: 0, price: f.leg === "up" ? f.high : f.low }].concat(f.levels,
            [{ ratio: 1, price: f.leg === "up" ? f.low : f.high }]);
  }

  // ---- drawing --------------------------------------------------------------------------------
  // ``o``: { ctx, on, get(group, key, i) -> value or null, n, step, y, lo, hi, plotW, priceH,
  //          bars, levels, fib, prof }

  function widen(o, room) {
    if (!o.get) return;
    overlayKeys(o.on).forEach(function (k) { for (var i = 0; i < o.n; i++) room(o.get("overlays", k, i)); });
    if (o.fib) { room(o.fib.high); room(o.fib.low); }
  }

  // under the candles: the Ichimoku cloud, the Bollinger fill, levels, the volume profile, Fibonacci lines
  function drawUnder(o) {
    var ctx = o.ctx, T = theme(), on = o.on, n = o.n, y = o.y, step = o.step, lo = o.lo, hi = o.hi;
    if (o.get && on.ich) {
      for (var c = 0; c + 1 < n; c++) {
        var a0 = o.get("overlays", "span_a", c), b0 = o.get("overlays", "span_b", c);
        var a1 = o.get("overlays", "span_a", c + 1), b1 = o.get("overlays", "span_b", c + 1);
        if ([a0, b0, a1, b1].some(function (q) { return q === null || q === undefined; })) continue;
        var x0 = (c + 0.5) * step, x1 = (c + 1.5) * step;
        ctx.fillStyle = a0 >= b0 ? T.up : T.down; ctx.globalAlpha = 0.12;
        ctx.beginPath(); ctx.moveTo(x0, y(a0)); ctx.lineTo(x1, y(a1)); ctx.lineTo(x1, y(b1)); ctx.lineTo(x0, y(b0));
        ctx.closePath(); ctx.fill(); ctx.globalAlpha = 1;
      }
    }
    if (o.get && on.bb) {
      ctx.fillStyle = COLORS.bb; ctx.globalAlpha = 0.08; ctx.beginPath();
      var first = true;
      for (var u = 0; u < n; u++) {
        var bu = o.get("overlays", "bb_upper", u);
        if (bu === null || bu === undefined) continue;
        if (first) { ctx.moveTo((u + 0.5) * step, y(bu)); first = false; } else ctx.lineTo((u + 0.5) * step, y(bu));
      }
      for (var l = n - 1; l >= 0; l--) {
        var bl = o.get("overlays", "bb_lower", l);
        if (bl !== null && bl !== undefined) ctx.lineTo((l + 0.5) * step, y(bl));
      }
      if (!first) ctx.fill();
      ctx.globalAlpha = 1;
    }
    if (on.sr && o.levels) {
      o.levels.forEach(function (lv) {
        if (lv.price < lo || lv.price > hi) return;
        var ly0 = Math.round(y(lv.price)) + 0.5;
        ctx.strokeStyle = COLORS.sr; ctx.globalAlpha = 0.7; ctx.setLineDash([2, 4]);
        ctx.beginPath(); ctx.moveTo(0, ly0); ctx.lineTo(o.plotW, ly0); ctx.stroke(); ctx.setLineDash([]);
        ctx.globalAlpha = 1; ctx.fillStyle = T.muted; ctx.textAlign = "left";
        ctx.fillText(lv.touches + "×", 4, ly0 - 3);
      });
    }
    var prof = on.vp ? o.prof : null;
    if (prof) {
      var vmax = 0;
      prof.bins.forEach(function (bn) { vmax = Math.max(vmax, bn.volume); });
      prof.bins.forEach(function (bn) {
        if (!vmax || bn.high < lo || bn.low > hi) return;
        var y0 = y(bn.high), y1 = y(bn.low), wd = bn.volume / vmax * o.plotW * 0.25;
        var poc = prof.poc >= bn.low && prof.poc <= bn.high;
        ctx.fillStyle = poc ? COLORS.fib : COLORS.vp; ctx.globalAlpha = poc ? 0.35 : 0.25;
        ctx.fillRect(o.plotW - wd, y0 + 0.5, wd, Math.max(1, y1 - y0 - 1));
      });
      ctx.globalAlpha = 1;
      [["vah", "VAH"], ["poc", "POC"], ["val", "VAL"]].forEach(function (k) {
        var pv0 = prof[k[0]];
        if (pv0 === null || pv0 === undefined || pv0 < lo || pv0 > hi) return;
        var vy = Math.round(y(pv0)) + 0.5;
        ctx.strokeStyle = k[0] === "poc" ? COLORS.fib : T.muted; ctx.globalAlpha = 0.7; ctx.setLineDash(k[0] === "poc" ? [] : [3, 3]);
        ctx.beginPath(); ctx.moveTo(o.plotW * 0.75, vy); ctx.lineTo(o.plotW, vy); ctx.stroke(); ctx.setLineDash([]);
        ctx.globalAlpha = 1; ctx.fillStyle = T.muted; ctx.textAlign = "right";
        ctx.fillText(k[1], o.plotW * 0.75 - 4, vy + 4);
      });
    }
    var fib = on.fib ? o.fib : null;
    if (fib) {
      fibLevels(fib).forEach(function (lv) {
        if (lv.price < lo || lv.price > hi) return;
        var fy = Math.round(y(lv.price)) + 0.5;
        ctx.strokeStyle = COLORS.fib; ctx.globalAlpha = lv.ratio === 0 || lv.ratio === 1 ? 0.4 : 0.75;
        ctx.setLineDash([6, 4]); ctx.beginPath(); ctx.moveTo(0, fy); ctx.lineTo(o.plotW, fy); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      });
    }
  }

  // over the candles: indicator lines, SAR dots, pattern letters, Fibonacci labels
  function drawOver(o) {
    var ctx = o.ctx, T = theme(), on = o.on, n = o.n, y = o.y, step = o.step, lo = o.lo, hi = o.hi;
    if (o.get) {
      var ov = function (k) { return function (i) { return o.get("overlays", k, i); }; };
      if (on.bb) {
        polyline(ctx, n, ov("bb_upper"), y, step, COLORS.bb, 1); polyline(ctx, n, ov("bb_lower"), y, step, COLORS.bb, 1);
        polyline(ctx, n, ov("bb_mid"), y, step, COLORS.bb, 1, [4, 4]);
      }
      if (on.ema) {
        polyline(ctx, n, ov("ema20"), y, step, COLORS.ema20); polyline(ctx, n, ov("ema50"), y, step, COLORS.ema50);
        polyline(ctx, n, ov("ema200"), y, step, COLORS.ema200, 2);
      }
      if (on.sma) {
        polyline(ctx, n, ov("sma20"), y, step, COLORS.sma20, 1.5, [2, 2]); polyline(ctx, n, ov("sma50"), y, step, COLORS.sma50, 1.5, [2, 2]);
        polyline(ctx, n, ov("sma200"), y, step, COLORS.sma200, 2, [2, 2]);
      }
      if (on.dc) { polyline(ctx, n, ov("dc_upper"), y, step, COLORS.dc, 1); polyline(ctx, n, ov("dc_lower"), y, step, COLORS.dc, 1); }
      if (on.vwap) polyline(ctx, n, ov("vwap"), y, step, COLORS.vwap, 1.5, [6, 3]);
      if (on.ich) { polyline(ctx, n, ov("tenkan"), y, step, COLORS.tenkan, 1); polyline(ctx, n, ov("kijun"), y, step, COLORS.kijun, 1); }
      if (on.st) {  // green under price in an uptrend, red above it in a downtrend
        [1, -1].forEach(function (dir) {
          polyline(ctx, n, function (i) { return o.get("overlays", "supertrend_dir", i) === dir ? o.get("overlays", "supertrend", i) : null; },
                   y, step, dir === 1 ? T.up : T.down, 2);
        });
      }
      if (on.kc) {
        polyline(ctx, n, ov("kc_upper"), y, step, COLORS.kc, 1); polyline(ctx, n, ov("kc_lower"), y, step, COLORS.kc, 1);
        polyline(ctx, n, ov("kc_mid"), y, step, COLORS.kc, 1, [4, 4]);
      }
      if (on.pat && o.bars) {  // the pattern's letter above the candle that completes it
        ctx.font = "600 10px system-ui, sans-serif"; ctx.textAlign = "center"; ctx.fillStyle = T.fg;
        for (var q = 0; q < n; q++) {
          var pm = o.get("patterns", null, q);
          if (pm) ctx.fillText(pm, (q + 0.5) * step, y(o.bars[q][2]) - 4);
        }
        ctx.font = "12px system-ui, sans-serif";
      }
      if (on.psar) {
        for (var p = 0; p < n; p++) {
          var sv = o.get("overlays", "psar", p);
          if (sv === null || sv === undefined) continue;
          ctx.fillStyle = o.get("overlays", "psar_trend", p) === 1 ? T.up : T.down;
          ctx.beginPath(); ctx.arc((p + 0.5) * step, y(sv), 1.8, 0, Math.PI * 2); ctx.fill();
        }
      }
    }
    var fib = on.fib ? o.fib : null;
    if (fib) {
      ctx.textAlign = "left";
      fibLevels(fib).forEach(function (lv) {
        if (lv.price < lo || lv.price > hi) return;
        var fy = Math.round(y(lv.price)) + 0.5;
        var ft = (lv.ratio * 100).toFixed(1).replace(".0", "") + "% " + o.fmt(lv.price);
        ctx.fillStyle = T.card; ctx.globalAlpha = 0.85;
        ctx.fillRect(2, fy - 15, ctx.measureText(ft).width + 4, 13); ctx.globalAlpha = 1;
        ctx.fillStyle = COLORS.fib; ctx.fillText(ft, 4, fy - 4);
      });
    }
  }

  // the panels below the price; returns the legend entries for candle ``hIdx``
  // ``o`` adds: top (y of the first panel), w (full width), bw (candle body width), hIdx
  function drawPanels(o) {
    var ctx = o.ctx, T = theme(), n = o.n, legend = [], list = panels(o.on);
    list.forEach(function (key, k) {
      var pTop = o.top + k * PANEL_H + 6, pH = PANEL_H - 12;
      ctx.strokeStyle = T.line; ctx.globalAlpha = 0.8;
      ctx.beginPath(); ctx.moveTo(0, pTop - 3.5); ctx.lineTo(o.w, pTop - 3.5); ctx.stroke(); ctx.globalAlpha = 1;
      var fixed = { rsi: [0, 100, [30, 70]], adx: [0, 60, [25]], stoch: [0, 100, [20, 80]], willr: [-100, 0, [-80, -20]],
                    mfi: [0, 100, [20, 80]] }[key];
      var guides = fixed ? fixed[2] : key === "cci" ? [-100, 100] : ["fi", "cmf", "roc"].indexOf(key) >= 0 ? [0] : [];
      var series = [], pmin = Infinity, pmax = -Infinity;
      var pv = function (name) { return function (i) { return o.get ? o.get("panels", name, i) : null; }; };
      if (key === "vol") series = [{ f: function (i) { return o.bars[i][5] || 0; }, bars: true }];
      if (key === "rsi") series = [{ f: pv("rsi"), c: COLORS.rsi, name: "RSI" }];
      if (key === "adx") series = [{ f: pv("adx"), c: COLORS.adx, name: "ADX" }];
      if (key === "atr") series = [{ f: pv("atr"), c: COLORS.atr, name: "ATR" }];
      if (key === "cci") series = [{ f: pv("cci"), c: COLORS.cci, name: "CCI" }];
      if (key === "willr") series = [{ f: pv("willr"), c: COLORS.willr, name: "%R" }];
      if (key === "fi") series = [{ f: pv("fi"), c: COLORS.fi, name: "Force" }];
      if (key === "obv") series = [{ f: pv("obv"), c: COLORS.obv, name: "OBV" }];
      if (key === "mfi") series = [{ f: pv("mfi"), c: COLORS.mfi, name: "MFI" }];
      if (key === "cmf") series = [{ f: pv("cmf"), c: COLORS.cmf, name: "CMF" }];
      if (key === "roc") series = [{ f: pv("roc"), c: COLORS.roc, name: "ROC %" }];
      if (key === "stoch") series = [{ f: pv("stoch_k"), c: COLORS.k, name: "K" }, { f: pv("stoch_d"), c: COLORS.d, name: "D" }];
      if (key === "macd") series = [{ f: pv("macd_hist"), hist: true, name: "hist" }, { f: pv("macd"), c: COLORS.macd, name: "MACD" },
                                    { f: pv("macd_signal"), c: COLORS.signal, name: "signal" }];
      if (fixed) {
        pmin = fixed[0]; pmax = fixed[1];
        if (key === "adx") for (var ai = 0; ai < n; ai++) {  // a strong trend can run past 60
          var av = series[0].f(ai);
          if (av !== null && av !== undefined && av > pmax) pmax = Math.min(100, Math.ceil(av / 10) * 10);
        }
      } else {
        series.forEach(function (sr) {
          for (var i = 0; i < n; i++) { var q = sr.f(i); if (q !== null && q !== undefined) { pmin = Math.min(pmin, q); pmax = Math.max(pmax, q); } }
        });
        if (key === "vol") pmin = 0;
        if (key === "macd") { var m = Math.max(Math.abs(pmin), Math.abs(pmax)); pmin = -m; pmax = m; }
        if (key === "cci") { pmin = Math.min(pmin, -150); pmax = Math.max(pmax, 150); }
        if (["fi", "cmf", "roc"].indexOf(key) >= 0) { var z = Math.max(Math.abs(pmin), Math.abs(pmax)); pmin = -z; pmax = z; }
      }
      if (!isFinite(pmin) || !isFinite(pmax) || pmax === pmin) { pmin = 0; pmax = 1; }
      var py = function (val) { return pTop + (pmax - val) / (pmax - pmin) * pH; };
      guides.forEach(function (g) {
        ctx.strokeStyle = T.muted; ctx.globalAlpha = 0.5; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(0, Math.round(py(g)) + 0.5); ctx.lineTo(o.plotW, Math.round(py(g)) + 0.5); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = T.muted; ctx.textAlign = "left";
        ctx.fillText(String(g), o.plotW + 6, py(g) + 4);
      });
      series.forEach(function (sr) {
        if (sr.bars || sr.hist) {
          var zero = sr.hist ? py(0) : pTop + pH;
          for (var i = 0; i < n; i++) {
            var q = sr.f(i);
            if (q === null || q === undefined) continue;
            ctx.fillStyle = sr.hist ? (q >= 0 ? T.up : T.down) : (o.bars[i][4] >= o.bars[i][1] ? T.up : T.down);
            ctx.globalAlpha = sr.hist ? 0.6 : 0.4;
            var yy = py(q);
            ctx.fillRect((i + 0.5) * o.step - o.bw / 2, Math.min(yy, zero), o.bw, Math.max(1, Math.abs(zero - yy)));
            ctx.globalAlpha = 1;
          }
        } else polyline(ctx, n, sr.f, py, o.step, sr.c, 1.5);
      });
      ctx.fillStyle = T.muted; ctx.textAlign = "left";
      ctx.fillText({ vol: "Volume", rsi: "RSI 14", macd: "MACD 12/26/9", adx: "ADX 14", stoch: "Stoch RSI",
                     atr: "ATR 14", cci: "CCI 20", willr: "Williams %R 14", fi: "Force index 13", obv: "OBV",
                     mfi: "MFI 14", cmf: "CMF 20", roc: "ROC 10 (%)" }[key], 4, pTop + 11);
      series.forEach(function (sr) {
        var q = sr.f(o.hIdx);
        if (q === null || q === undefined || sr.bars) return;
        legend.push('<span style="color:' + (sr.c || (q >= 0 ? T.up : T.down)) + '">' + (sr.name === "hist" ? "MACD hist" : sr.name) +
                    " <b>" + (key === "macd" || key === "fi" || key === "obv" ? Number(q.toPrecision(4)) : key === "atr" ? o.fmt(q) :
                     key === "cmf" ? q.toFixed(3) : q.toFixed(1)) + "</b></span>");
      });
    });
    return legend;
  }

  // legend entries for the price overlays at candle ``i``
  function overlayLegend(o, i) {
    var T = theme(), out = [];
    if (!o.get) return out;
    var named = { ema20: "EMA20", ema50: "EMA50", ema200: "EMA200", vwap: "VWAP", supertrend: "Supertrend",
                  tenkan: "Tenkan", kijun: "Kijun", psar: "SAR", bb_upper: "BB up", bb_lower: "BB low",
                  kc_upper: "KC up", kc_lower: "KC low", sma20: "SMA20", sma50: "SMA50", sma200: "SMA200",
                  dc_upper: "DC up", dc_lower: "DC low" };
    overlayKeys(o.on).forEach(function (k) {
      var q = o.get("overlays", k, i);
      if (q === null || q === undefined || !named[k]) return;
      var col = COLORS[k] || (k === "supertrend" ? (o.get("overlays", "supertrend_dir", i) === 1 ? T.up : T.down) :
                              k.indexOf("bb") === 0 ? COLORS.bb : k.indexOf("kc") === 0 ? COLORS.kc :
                              k.indexOf("dc") === 0 ? COLORS.dc : COLORS.psar);
      out.push('<span style="color:' + col + '">' + named[k] + " <b>" + o.fmt(q) + "</b></span>");
    });
    var hp = o.on.pat ? o.get("patterns", null, i) : null;
    if (hp) out.push("<span>" + hp.split("").map(function (c) { return PATTERN_NAMES[c]; }).join(", ") + "</span>");
    return out;
  }

  // the market at candle ``i`` in plain words, from the indicator values (whatever chips are on)
  function read(get, bars, i) {
    if (!get || !bars[i]) return "";
    var close = bars[i][4], out = [];
    var e20 = get("overlays", "ema20", i), e50 = get("overlays", "ema50", i), e200 = get("overlays", "ema200", i);
    if (e200 !== null && e200 !== undefined && e20 !== null && e50 !== null) {
      var upT = close > e200 && e20 > e50, downT = close < e200 && e20 < e50;
      out.push(upT ? '<b class="good">uptrend</b> (price above EMA 200, EMA 20 above 50)' :
               downT ? '<b class="bad">downtrend</b> (price below EMA 200, EMA 20 below 50)' :
               "<b>mixed trend</b> (the EMAs disagree)");
    }
    var adx = get("panels", "adx", i);
    if (adx !== null && adx !== undefined) out.push("ADX " + adx.toFixed(0) + (adx >= 25 ? " strong trend" : adx >= 20 ? " weak trend" : " no trend (range)"));
    var rsi = get("panels", "rsi", i);
    if (rsi !== null && rsi !== undefined) out.push("RSI " + rsi.toFixed(0) + (rsi >= 70 ? " overbought" : rsi <= 30 ? " oversold" : rsi >= 50 ? " buyers ahead" : " sellers ahead"));
    var hist = get("panels", "macd_hist", i);
    if (hist !== null && hist !== undefined) out.push("MACD " + (hist >= 0 ? "above" : "below") + " its signal");
    var from = Math.max(0, i - 19), sum = 0, cnt = 0;
    for (var j = from; j <= i; j++) { sum += bars[j][5] || 0; cnt++; }
    var avg = cnt ? sum / cnt : 0;
    if (avg > 0 && bars[i][5]) out.push("volume " + (bars[i][5] / avg).toFixed(1) + "× its 20-candle average");
    var atr = get("panels", "atr", i);
    if (atr !== null && atr !== undefined && close) out.push("ATR " + (atr / close * 100).toFixed(2) + "% of price");
    var st = get("overlays", "supertrend_dir", i);
    if (st === 1 || st === -1) out.push("Supertrend " + (st === 1 ? "up" : "down"));
    return out.join(" · ");
  }

  window.ChartStudies = { COLORS: COLORS, PANELS: PANELS, PANEL_H: PANEL_H, switches: switches, panels: panels,
                          anyOn: anyOn, overlayKeys: overlayKeys, widen: widen, drawUnder: drawUnder, drawOver: drawOver,
                          drawPanels: drawPanels, overlayLegend: overlayLegend, read: read, fibOf: fibOf, levelsOf: levelsOf,
                          profileOf: profileOf, polyline: polyline };
})();
