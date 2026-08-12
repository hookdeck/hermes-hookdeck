/**
 * Tests for the shipped dashboard bundle.
 *
 * Run with `node --test tests/dashboard/`. No dependencies: the bundle takes
 * everything it uses from the injected SDK, so a fake one is enough — see
 * harness.mjs for why that beats adding a JS toolchain to test one file.
 *
 * The emphasis is the two buttons that mutate a live Hookdeck project. A
 * mislabelled one is invisible on screen and only shows up in traffic.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { mount, mountRaw, nodesOfType, allText, buttonsLabelled } from "./harness.mjs";

const OVERVIEW = "/api/plugins/hookdeck/overview";

/** An overview payload with the fields the page reads, overridable per test. */
function overview({ hookdeck = {}, local = {} } = {}) {
  return {
    hookdeck: {
      configured: true,
      depth: { max_depth: 0 },
      failed: [],
      issues: [],
      connections: [],
      other_connection_count: 0,
      ...hookdeck,
    },
    local: { exists: true, counts: {}, failures: [], stranded: [], ...local },
  };
}

async function page(payload, extra = {}) {
  const app = mount({ responses: { [OVERVIEW]: payload, ...extra } });
  await app.settle();
  return app;
}

// ----------------------------------------------------------------------
// The pause/resume toggle
// ----------------------------------------------------------------------
//
// The label and the endpoint are both derived from `c.paused`. Invert that
// expression and the UI still reads correctly while doing the opposite — a
// button labelled "Pause" that resumes production traffic.

test("a paused connection offers Resume, and resuming is what it posts", async () => {
  const app = await page(
    overview({ hookdeck: { connections: [{ id: "web_1", name: "stripe", paused: true }] } }),
  );

  const [button] = buttonsLabelled(app.tree, "Resume");
  assert.ok(button, "a paused connection should offer Resume");
  assert.equal(buttonsLabelled(app.tree, "Pause").length, 0);

  button.props.onClick();
  assert.deepEqual(app.requests.at(-1), {
    path: "/api/plugins/hookdeck/connections/web_1/resume",
    method: "POST",
  });
});

test("an active connection offers Pause, and pausing is what it posts", async () => {
  const app = await page(
    overview({ hookdeck: { connections: [{ id: "web_1", name: "stripe", paused: false }] } }),
  );

  const [button] = buttonsLabelled(app.tree, "Pause");
  assert.ok(button, "an active connection should offer Pause");
  assert.equal(buttonsLabelled(app.tree, "Resume").length, 0);

  button.props.onClick();
  assert.deepEqual(app.requests.at(-1), {
    path: "/api/plugins/hookdeck/connections/web_1/pause",
    method: "POST",
  });
});

test("each connection acts on its own id", async () => {
  // One shared handler closing over the wrong row would pause a connection
  // the operator did not click.
  const app = await page(
    overview({
      hookdeck: {
        connections: [
          { id: "web_1", name: "a", paused: false },
          { id: "web_2", name: "b", paused: false },
        ],
      },
    }),
  );

  const buttons = buttonsLabelled(app.tree, "Pause");
  assert.equal(buttons.length, 2);
  buttons[1].props.onClick();
  assert.match(app.requests.at(-1).path, /web_2\/pause$/);
});

// ----------------------------------------------------------------------
// Retry is a redelivery
// ----------------------------------------------------------------------

test("retrying a failed delivery posts to that event's retry endpoint", async () => {
  const app = await page(
    overview({ hookdeck: { failed: [{ id: "evt_1", response_status: 500, attempts: 2 }] } }),
  );

  const [button] = buttonsLabelled(app.tree, "Retry");
  button.props.onClick();
  assert.deepEqual(app.requests.at(-1), {
    path: "/api/plugins/hookdeck/events/evt_1/retry",
    method: "POST",
  });
});

test("retrying a failed agent run posts for that run's event", async () => {
  const app = await page(
    overview({
      local: { failures: [{ event_id: "evt_9", route: "stripe", status: "failed", agent_attempts: 2 }] },
    }),
  );

  const [button] = buttonsLabelled(app.tree, "Retry");
  button.props.onClick();
  assert.match(app.requests.at(-1).path, /events\/evt_9\/retry$/);
});

// ----------------------------------------------------------------------
// `busy` — the guard against a double redelivery
// ----------------------------------------------------------------------

test("every action is disabled while a request is in flight", async () => {
  // This plugin exists so one event runs the agent once. A double-click that
  // fires two retries is that guarantee failing in the UI instead of the
  // adapter.
  const app = await page(
    overview({
      hookdeck: {
        failed: [{ id: "evt_1", response_status: 500, attempts: 1 }],
        connections: [{ id: "web_1", name: "a", paused: false }],
      },
      local: { failures: [{ event_id: "evt_9", route: "r", status: "failed", agent_attempts: 1 }] },
    }),
    // Never resolves, so `busy` stays true and the tree can be inspected
    // mid-flight.
    { "/api/plugins/hookdeck/events/evt_1/retry": () => new Promise(() => {}) },
  );

  buttonsLabelled(app.tree, "Retry")[0].props.onClick();
  const actionable = nodesOfType(app.tree, "Button").filter(
    (b) => b.props.onClick && "disabled" in b.props,
  );
  assert.ok(actionable.length >= 3, "retry, retry and pause all carry a disabled flag");
  for (const button of actionable) {
    assert.equal(button.props.disabled, true);
  }
});

test("a failed action leaves a way back rather than a dead panel", async () => {
  // A failed action swaps the whole page for the error card, so the recovery
  // that matters is that card's Retry — it reloads the overview and restores
  // the page. (`busy` is not the guard here: the error card's button has no
  // `disabled` binding, so it stays clickable regardless.)
  const overviewPayload = overview({
    hookdeck: { connections: [{ id: "web_1", name: "a", paused: false }] },
  });
  const app = await page(overviewPayload, {
    "/api/plugins/hookdeck/connections/web_1/pause": () => Promise.reject(new Error("nope")),
  });

  buttonsLabelled(app.tree, "Pause")[0].props.onClick();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.match(allText(app.tree), /nope/, "the failure is shown, not swallowed");

  const [back] = buttonsLabelled(app.tree, "Retry");
  assert.ok(back, "a failed action must leave something to click");
  assert.equal(back.props.disabled, undefined, "the way back is never disabled");

  back.props.onClick();
  await app.settle();
  assert.match(allText(app.tree), /Connections/, "the page comes back");
});

// ----------------------------------------------------------------------
// Loading against a host that does not have what it needs
// ----------------------------------------------------------------------

test("an SDK older than the bundle is a quiet no-op, not a throw", async () => {
  // Upgrading the plugin before Hermes is the normal order, so the bundle
  // will meet hosts that lack what it uses. Throwing here goes into the
  // host's bundle loader and takes the whole dashboard down with it.
  const hosts = [
    ["no SDK at all", {}],
    ["an SDK but no plugin registry", { __HERMES_PLUGIN_SDK__: { React: {} } }],
    ["a registry with no register()", { __HERMES_PLUGIN_SDK__: { React: {} }, __HERMES_PLUGINS__: {} }],
  ];
  for (const [what, window] of hosts) {
    let registered;
    assert.doesNotThrow(() => {
      registered = mountRaw(window);
    }, `should load quietly against ${what}`);
    assert.equal(registered.size, 0, `should register nothing against ${what}`);
  }
});

test("the tab registers under the name its manifest declares", async () => {
  const app = mount({ responses: { [OVERVIEW]: overview() } });
  assert.ok(app.registered.has("hookdeck"), "the host pairs the component to the tab by this name");
});

// ----------------------------------------------------------------------
// States an operator will actually see
// ----------------------------------------------------------------------

test("a missing API key explains itself and asks for nothing else", async () => {
  const app = await page({ hookdeck: { configured: false }, local: {} });
  const text = allText(app.tree);
  assert.match(text, /HOOKDECK_EG_API_KEY/);
  assert.equal(nodesOfType(app.tree, "Button").length, 0, "nothing to click without a key");
});

test("an empty queue says so rather than rendering a bare panel", async () => {
  const app = await page(overview());
  assert.match(allText(app.tree), /None\./);
  assert.match(allText(app.tree), /No connection matches a configured route yet\./);
});

test("hidden connections are counted without justifying themselves", async () => {
  const app = await page(overview({ hookdeck: { other_connection_count: 26 } }));
  const text = allText(app.tree);
  assert.match(text, /26 other connection\(s\)/);
  assert.match(text, /Only connections matching a configured route appear here\./);
});

test("stranded runs are surfaced with what to do about them", async () => {
  const app = await page(overview({ local: { stranded: [{ event_id: "evt_1" }] } }));
  assert.match(allText(app.tree), /still marked running after an hour/);
  assert.match(allText(app.tree), /Restarting the gateway retries them/);
});

// ----------------------------------------------------------------------
// Error rendering
// ----------------------------------------------------------------------

test("a rejection is shown whether or not it is an Error", async () => {
  // `String((e && e.message) || e)` is written for both shapes; a plain
  // string rejection is the one less likely to have been tried.
  for (const [thrown, expected] of [
    [new Error("boom"), /boom/],
    ["plain string failure", /plain string failure/],
  ]) {
    const app = mount({
      responses: { [OVERVIEW]: () => Promise.reject(thrown) },
    });
    await app.settle();
    assert.match(allText(app.tree), expected);
    // And a way back, rather than a dead panel.
    assert.ok(buttonsLabelled(app.tree, "Retry").length >= 1);
  }
});

test("the error panel's Retry reloads the overview", async () => {
  const app = mount({ responses: { [OVERVIEW]: () => Promise.reject(new Error("down")) } });
  await app.settle();
  const before = app.requests.length;
  buttonsLabelled(app.tree, "Retry")[0].props.onClick();
  assert.equal(app.requests.at(-1).path, OVERVIEW);
  assert.ok(app.requests.length > before);
});
