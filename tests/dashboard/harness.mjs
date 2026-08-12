/**
 * A fake Hermes plugin SDK, so the dashboard bundle can be tested as shipped.
 *
 * `dist/index.js` is committed rather than generated — no package.json, no
 * bundler, no build step — and this keeps that property. It takes React, its
 * hooks, its components and its fetch from `window.__HERMES_PLUGIN_SDK__`, so
 * everything it touches is injectable: run it under `node:vm` with the SDK
 * faked and you exercise the real file, byte for byte, with no dependencies.
 *
 * `createElement` returns plain objects rather than rendering, because every
 * question worth asking here is about the tree and the requests — which
 * endpoint a button posts to, whether it is disabled — and none of them need a
 * DOM to answer.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
export const BUNDLE = join(HERE, "..", "..", "hookdeck", "dashboard", "dist", "index.js");

/** A rendered node: `{type, props, children}` with `type` a string. */
function createElement(type, props, ...children) {
  const flat = [];
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) flat.push(child);
  }
  return {
    type: typeof type === "function" ? type.name || "fn" : String(type),
    props: props || {},
    children: flat,
  };
}

/**
 * Hooks for a single component rendered repeatedly.
 *
 * State lives in a slot array keyed by call order — the same contract React
 * relies on — so a setter can trigger a re-render and the next pass reads the
 * updated value from the same slot.
 */
function makeHooks(rerender) {
  let slots = [];
  let cursor = 0;
  const effects = [];

  return {
    beginRender() {
      cursor = 0;
      effects.length = 0;
    },
    runEffects() {
      // Copied first: an effect may setState, which re-renders and refills
      // the list while we are walking it.
      for (const effect of [...effects]) effect();
    },
    hooks: {
      useState(initial) {
        const slot = cursor++;
        if (slots.length <= slot) slots[slot] = initial;
        const set = (next) => {
          slots[slot] = typeof next === "function" ? next(slots[slot]) : next;
          rerender();
        };
        return [slots[slot], set];
      },
      // Dependencies are ignored deliberately: this harness re-renders from
      // scratch, so a stale-closure bug is not something it can observe, and
      // pretending otherwise would be a test that lies about its own reach.
      useCallback: (fn) => fn,
      useEffect(fn) {
        const slot = cursor++;
        if (slots[slot] === undefined) {
          slots[slot] = "ran";
          effects.push(fn);
        }
      },
    },
    reset() {
      slots = [];
      cursor = 0;
    },
  };
}

/**
 * Load the bundle with a faked SDK and render its page component.
 *
 * `responses` maps a request path to either a value to resolve or an `Error`
 * to reject with — `{"/overview": {...}}`.
 */
export function mount({ responses = {}, sdk = {}, plugins = {} } = {}) {
  const requests = [];
  const registered = new Map();

  const fetchJSON = (path, options) => {
    requests.push({ path, method: (options && options.method) || "GET" });
    const answer = responses[path];
    if (answer instanceof Error) return Promise.reject(answer);
    if (typeof answer === "function") return answer();
    return Promise.resolve(answer === undefined ? {} : answer);
  };

  let tree = null;
  let component = null;
  const hookStore = makeHooks(() => render());

  function render() {
    if (!component) return null;
    hookStore.beginRender();
    tree = component({});
    return tree;
  }

  const window = {
    __HERMES_PLUGIN_SDK__: {
      React: { createElement },
      // Plain strings: `createElement` stringifies a non-function type, so
      // the node's `type` comes out as "Button" and stays greppable.
      components: Object.fromEntries(
        ["Card", "CardHeader", "CardTitle", "CardContent", "Badge", "Button"].map(
          (name) => [name, name],
        ),
      ),
      hooks: hookStore.hooks,
      fetchJSON,
      ...sdk,
    },
    __HERMES_PLUGINS__: {
      register: (name, fn) => registered.set(name, fn),
      ...plugins,
    },
  };

  vm.runInNewContext(readFileSync(BUNDLE, "utf8"), { window, console });

  component = registered.get("hookdeck") || null;
  render();

  return {
    requests,
    registered,
    get tree() {
      return tree;
    },
    /** Render, then flush effects and any renders they cause. */
    async settle() {
      hookStore.runEffects();
      for (let i = 0; i < 20; i++) await Promise.resolve();
      render();
      hookStore.runEffects();
      for (let i = 0; i < 20; i++) await Promise.resolve();
      return render();
    },
  };
}

/**
 * Run the bundle against a window supplied verbatim.
 *
 * `mount` always builds a complete SDK, so it cannot express "this host does
 * not have one" — which is exactly the case the bundle's opening guard exists
 * for. Returns what the bundle registered, or throws if it threw.
 */
export function mountRaw(window) {
  const registered = new Map();
  if (window && window.__HERMES_PLUGINS__ && !window.__HERMES_PLUGINS__.register) {
    // left absent on purpose by the caller
  } else if (window && window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register = (name, fn) => registered.set(name, fn);
  }
  vm.runInNewContext(readFileSync(BUNDLE, "utf8"), { window, console });
  return registered;
}

/** Every node in the tree, depth first. */
export function walk(node, out = []) {
  if (!node || typeof node !== "object") return out;
  out.push(node);
  for (const child of node.children || []) walk(child, out);
  return out;
}

/** Nodes whose type matches, e.g. `nodesOfType(tree, "Button")`. */
export function nodesOfType(tree, type) {
  return walk(tree).filter((n) => n.type === type);
}

/** Visible text of a node and everything under it. */
export function textOf(node) {
  return walk(node)
    .flatMap((n) => n.children.filter((c) => typeof c === "string"))
    .join(" ");
}

/** All text in the tree, for "does it mention X" assertions. */
export function allText(tree) {
  return textOf(tree);
}

/** Buttons whose own label matches `label` exactly. */
export function buttonsLabelled(tree, label) {
  return nodesOfType(tree, "Button").filter((b) => textOf(b).trim() === label);
}
