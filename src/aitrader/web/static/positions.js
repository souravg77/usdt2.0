// Trading page: the Open positions "Now USDT", "Price now" and "Stop loss" columns follow the live
// price. Every 1.5 seconds it asks /api/positions (Bybit's public prices, cached ~1s on the server,
// the same feed as the Markets page) and rewrites each cell and the total. Display only: it never trades.
(function () {
  "use strict";
  var cells = document.querySelectorAll("td[data-pos]");
  if (!cells.length) return;
  var label = document.getElementById("pos-live");
  var POLL_MS = 1500, FETCH_MS = 6000, busy = false, lastOk = 0, source = "";

  function show(td, x, bold) {
    if (x === null || x === undefined) return;
    var text = (x >= 0 ? "+" : "") + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    td.className = x >= 0 ? "good" : "bad";
    td.innerHTML = bold ? "<b>" + text + "</b>" : text;
  }

  function fmt(x) { return Number(x.toPrecision(6)).toString(); }

  function showPrice(k, px) {
    var td = document.querySelector('td[data-px="' + k + '"]');
    if (td) td.textContent = fmt(px);
    var st = document.querySelector('td[data-stop="' + k + '"]');
    if (!st) return;
    var stop = parseFloat(st.getAttribute("data-level"));
    var gap = st.getAttribute("data-short") ? stop - px : px - stop;
    var pct = 100 * gap / px;
    st.innerHTML = fmt(stop) + ' <span class="small ' + (pct > 0 ? "muted" : "bad") + '">' + pct.toFixed(2) + "% away</span>";
  }

  function poll() {
    if (document.hidden || busy) return;
    busy = true;
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, FETCH_MS);
    fetch("/api/positions", { credentials: "same-origin", cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (res) {
        cells.forEach(function (td) {
          var k = td.getAttribute("data-pos");
          if (k === "total") show(td, res.total, true);
          else if (res.rows && k in res.rows) show(td, res.rows[k], false);
        });
        Object.keys(res.prices || {}).forEach(function (k) { showPrice(k, res.prices[k]); });
        source = res.source || "";
        lastOk = Date.now();
      })
      .catch(function () { /* keep the last numbers; the label says how old they are */ })
      .then(function () { clearTimeout(timer); busy = false; status(); });
  }

  function status() {
    if (!label || !lastOk) return;
    var age = Math.round((Date.now() - lastOk) / 1000);
    label.textContent = (source === "live" ? "live Bybit price" : "saved price (Bybit didn't answer)") +
      ", before exit fees · " + (age < 2 ? "just now" : age + "s ago");
  }

  poll();
  setInterval(poll, POLL_MS);
  setInterval(status, 1000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { busy = false; poll(); } });
})();
