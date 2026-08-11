/**
 * Hookdeck dashboard tab.
 *
 * A plain IIFE against window.__HERMES_PLUGIN_SDK__ — no build step, no bundled
 * React, which is why this file is committed rather than generated.
 *
 * The two panels answer different questions and are deliberately not merged:
 * Hookdeck's view is what is still owed to this gateway; the local ledger is
 * what this gateway did with each delivery. A run that fails after the 202 is
 * only visible in the second, because from Hookdeck's side it was delivered.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  // Fail quietly rather than throwing into the host's bundle loader if this
  // ships against a dashboard whose SDK surface predates what it uses.
  if (!SDK || !PLUGINS || !PLUGINS.register) return;

  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const h = React.createElement;

  const API = "/api/plugins/hookdeck";

  function Row(props) {
    return h(
      "div",
      { className: "flex items-center justify-between gap-3 border-b py-2 text-sm last:border-0" },
      props.children
    );
  }

  function HookdeckPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(function () {
      setBusy(true);
      SDK.fetchJSON(API + "/overview")
        .then(function (d) { setData(d); setError(null); })
        .catch(function (e) { setError(String((e && e.message) || e)); })
        .finally(function () { setBusy(false); });
    }, []);

    useEffect(function () { load(); }, [load]);

    function act(path) {
      setBusy(true);
      SDK.fetchJSON(API + path, { method: "POST" })
        .then(load)
        .catch(function (e) { setError(String((e && e.message) || e)); setBusy(false); });
    }

    if (error) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Hookdeck")),
        h(CardContent, null,
          h("p", { className: "text-sm text-destructive" }, error),
          h(Button, { onClick: load, className: "mt-3" }, "Retry")));
    }
    if (!data) {
      return h(Card, null, h(CardContent, { className: "py-6 text-sm text-muted-foreground" }, "Loading…"));
    }

    const hd = data.hookdeck || {};
    const local = data.local || {};

    if (!hd.configured) {
      return h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Hookdeck")),
        h(CardContent, null,
          h("p", { className: "text-sm text-muted-foreground" },
            "HOOKDECK_API_KEY is not set, so the queue cannot be read. The adapter " +
            "still runs — this tab is read-only observability, not a dependency.")));
    }

    const failed = hd.failed || [];
    const conns = hd.connections || [];
    const depth = hd.depth || {};
    const others = hd.other_connection_count || 0;
    const counts = local.counts || {};
    const stranded = local.stranded || [];

    return h("div", { className: "flex flex-col gap-6" },

      // ── What Hookdeck still owes us ────────────────────────────────
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center justify-between" },
            h(CardTitle, { className: "text-lg" }, "Queue"),
            h(Button, { variant: "outline", onClick: load, disabled: busy }, busy ? "…" : "Refresh"))),
        h(CardContent, { className: "flex flex-col gap-2" },
          h("p", { className: "text-sm text-muted-foreground" },
            "Events Hookdeck is still holding for this gateway, over the last 24h."),
          h("div", { className: "flex flex-wrap gap-6" },
            h("div", null,
              h("div", { className: "text-2xl font-semibold" }, String(depth.max_depth != null ? depth.max_depth : "—")),
              h("div", { className: "text-xs text-muted-foreground" }, "peak queued")),
            h("div", null,
              h("div", { className: "text-2xl font-semibold" }, String((hd.issues || []).length)),
              h("div", { className: "text-xs text-muted-foreground" }, "open issues"))))),

      // ── Failures Hookdeck knows about, with a way to act ───────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-lg" }, "Failed deliveries")),
        h(CardContent, null,
          failed.length === 0
            ? h("p", { className: "text-sm text-muted-foreground" }, "None.")
            : failed.map(function (e) {
                return h(Row, { key: e.id },
                  h("div", { className: "flex min-w-0 items-center gap-2" },
                    h("code", { className: "truncate text-xs" }, e.id),
                    h(Badge, { variant: "outline" }, String(e.response_status || e.error_code || "?")),
                    h("span", { className: "text-xs text-muted-foreground" },
                      "attempt " + String(e.attempts))),
                  h(Button, {
                    variant: "outline",
                    disabled: busy,
                    onClick: function () { act("/events/" + e.id + "/retry"); },
                  }, "Retry"));
              }))),

      // ── What this gateway actually did ─────────────────────────────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-lg" }, "Agent runs")),
        h(CardContent, { className: "flex flex-col gap-2" },
          h("p", { className: "text-sm text-muted-foreground" },
            "From the local ledger. A run that failed after the 202 is only " +
            "visible here — Hookdeck recorded that delivery as successful."),
          !local.exists
            ? h("p", { className: "text-sm" }, "No deliveries recorded yet.")
            : h("div", { className: "flex flex-wrap gap-2" },
                Object.keys(counts).map(function (k) {
                  return h(Badge, { key: k, variant: k === "succeeded" ? "outline" : "destructive" },
                    k + ": " + String(counts[k]));
                })),
          stranded.length
            ? h("p", { className: "text-sm text-destructive" },
                stranded.length + " run(s) still marked running after an hour — a crash " +
                "lost the completion signal. Restarting the gateway retries them.")
            : null,
          (local.failures || []).map(function (f) {
            return h(Row, { key: f.event_id },
              h("div", { className: "flex min-w-0 items-center gap-2" },
                h("code", { className: "truncate text-xs" }, f.event_id),
                h(Badge, { variant: "outline" }, f.route || "—"),
                h("span", { className: "text-xs text-muted-foreground" },
                  f.status + " · " + String(f.agent_attempts) + " run(s)")),
              h(Button, {
                variant: "outline",
                disabled: busy,
                onClick: function () { act("/events/" + f.event_id + "/retry"); },
              }, "Retry"));
          }))),

      // ── Pause is the safe-restart control, so it belongs here ──────
      h(Card, null,
        h(CardHeader, null, h(CardTitle, { className: "text-lg" }, "Connections")),
        h(CardContent, null,
          h("p", { className: "mb-2 text-sm text-muted-foreground" },
            "Pause holds events in Hookdeck instead of dropping them — do it " +
            "before stopping the gateway, and resume after."),
          conns.length === 0
            ? h("p", { className: "text-sm" }, "No connection matches a configured route yet.")
            : null,
          others
            ? h("p", { className: "mb-2 text-xs text-muted-foreground" },
                others + " other connection(s) in this project are not shown — they " +
                "belong to something else, and a Pause button next to them is a misclick away from an outage.")
            : null,
          conns.map(function (c) {
            return h(Row, { key: c.id },
              h("div", { className: "flex min-w-0 items-center gap-2" },
                h("span", { className: "truncate" }, c.name || c.id),
                c.paused ? h(Badge, { variant: "destructive" }, "paused") : null),
              h(Button, {
                variant: "outline",
                disabled: busy,
                onClick: function () {
                  act("/connections/" + c.id + (c.paused ? "/resume" : "/pause"));
                },
              }, c.paused ? "Resume" : "Pause"));
          })))
    );
  }

  // The name must match manifest.json's `name`; that is how the host pairs the
  // registered component with the tab it discovered.
  PLUGINS.register("hookdeck", HookdeckPage);
})();
