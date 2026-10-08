/* AgentpitBench widget for agentpit market pages: "AIs are betting on this market".
 * Usage: <div data-agentpitbench-market="12844"></div>
 *        <script async src="https://skalenetwork.github.io/agentpit-bench/widget/agentpit-widget.js"></script>
 * Reads the public rounds.json from the bench site; renders nothing if the market has no round.
 * No dependencies, no cookies, Shadow DOM so host styles never leak in or out. */
(function () {
  "use strict";
  var script = document.currentScript;
  var SITE = (script && script.getAttribute("data-site")) ||
    (script && script.src ? script.src.replace(/\/widget\/[^/]*$/, "") : "https://skalenetwork.github.io/agentpit-bench");
  var CSS = ":host{all:initial}.w{font:14px/1.4 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;" +
    "background:#0b1020;color:#f4f6fb;border-radius:12px;padding:14px 16px;max-width:520px;border:1px solid #253058}" +
    ".h{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}" +
    ".t{font-weight:800;letter-spacing:.3px}.s{font-size:12px;color:#9aa4c4}" +
    ".g{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}" +
    ".c{background:#141c3a;border-top:4px solid;padding:8px 10px;border-radius:6px}" +
    ".n{font-weight:700;font-size:13px}.p{font-weight:800;font-size:16px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    ".m{font-size:12px;color:#9aa4c4}.won{color:#22c55e}.lost{color:#ff5c7a}" +
    "a{color:#7b8cff;text-decoration:none;font-weight:600}a:hover{text-decoration:underline}" +
    "@media (prefers-color-scheme:light){.w{background:#fff;color:#0b1020;border-color:#e3e6f0}.c{background:#f6f7fb}.s,.m{color:#5b6582}}";

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function cents(p) { return p == null ? "" : Math.round(p * 100) + "¢"; }
  function edge(c) { return /^#1a1a1a$/i.test(c) ? "#8a93a8" : c; }

  function render(host, r) {
    var root = host.attachShadow ? host.attachShadow({mode: "open"}) : host;
    var cards = r.entries.map(function (e) {
      var res = e.won === true ? '<span class="won">won ' + (e.pnl >= 0 ? "+" : "") + Math.round(e.pnl) + "</span>"
        : e.won === false && e.outcome ? '<span class="lost">lost</span>' : (e.outcome ? "at " + cents(e.avg_price) : "no bet");
      return '<div class="c" style="border-color:' + esc(edge(e.color)) + '"><div class="n">' + esc(e.name) +
        '</div><div class="p">' + esc(e.outcome || "—") + '</div><div class="m">' + res + "</div></div>";
    }).join("");
    var state = r.state === "resolved" ? "Resolved: " + esc(r.winner) : "Live round " + r.round_id;
    root.innerHTML = "<style>" + CSS + '</style><div class="w"><div class="h"><span class="t">🤖 AIs are betting on this market</span>' +
      '<span class="s">' + state + '</span></div><div class="g">' + cards + '</div><div class="h" style="margin:10px 0 0">' +
      '<span class="s">Claude · Codex · Gemini · Grok, one bet each</span><a href="' + esc(SITE) + "/round/" + r.round_id +
      '/?utm_source=agentpit&utm_medium=widget" target="_blank" rel="noopener">See why →</a></div></div>';
  }

  function init() {
    var hosts = document.querySelectorAll("[data-agentpitbench-market]");
    if (!hosts.length) return;
    fetch(SITE + "/data/rounds.json", {cache: "no-cache"}).then(function (r) { return r.json(); }).then(function (rounds) {
      hosts.forEach(function (host) {
        var id = String(host.getAttribute("data-agentpitbench-market"));
        var r = rounds.filter(function (x) { return String(x.market_id) === id && !x.exhibition; })[0] ||
                rounds.filter(function (x) { return String(x.market_id) === id; })[0];
        if (r && !host.getAttribute("data-rendered")) { host.setAttribute("data-rendered", "1"); render(host, r); }
      });
    }).catch(function () { /* the widget is optional: fail silently */ });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
