// Pages that refresh themselves (data-reload-ms on <html>) wait while someone is typing in a box,
// such as the kill switch's KILL or a form being filled in, so the reload never wipes it.
(function () {
  "use strict";
  var ms = Number(document.documentElement.getAttribute("data-reload-ms")) || 0;
  if (!ms) return;
  function typing() {
    var boxes = document.querySelectorAll("input:not([type=hidden]),textarea,select");
    for (var i = 0; i < boxes.length; i++) {
      var e = boxes[i];
      if (e === document.activeElement) return true;
      if (e.tagName === "SELECT") {  // a select has no defaultValue: compare with its selected option
        var opt = e.querySelector("option[selected]");
        if (opt && e.value !== opt.value) return true;
        continue;
      }
      if (e.type !== "checkbox" && e.type !== "radio" && e.value && e.value !== e.defaultValue) return true;
    }
    return false;
  }
  function again() {
    if (typing()) { setTimeout(again, 5000); return; }
    location.reload();
  }
  setTimeout(again, ms);
})();
