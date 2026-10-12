/* AgentpitBench embed loader (docs/embed-spec.md §4): sizes AgentpitBench iframes to their content.
 * Optional: <script async src="https://agentpitbench.org/embed.js"></script> next to the iframe.
 * Accepts height messages only from this site's origin and only for an iframe on this page. No cookies, no tracking. */
(function () {
  "use strict";
  if (window.__agentpitbenchEmbed) return;
  window.__agentpitbenchEmbed = 1;
  var s = document.currentScript, origin;
  try { origin = new URL(s && s.src ? s.src : "https://agentpitbench.org/").origin; } catch (e) { return; }
  window.addEventListener("message", function (e) {
    var d = e.data;
    if (e.origin !== origin || !d || d.type !== "agentpitbench:height") return;
    var h = Math.round(+d.height), f = document.getElementsByTagName("iframe");
    if (!(h > 0 && h < 5000)) return;
    for (var i = 0; i < f.length; i++) {
      if (f[i].contentWindow === e.source && String(f[i].src).indexOf(origin + "/") === 0) {
        f[i].style.height = h + "px";
        f[i].setAttribute("height", String(h));
        return;
      }
    }
  });
})();
