// Safe DOM helpers. Dynamic text is ALWAYS inserted as text nodes (never innerHTML), so API/LLM
// output cannot inject markup or scripts.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function badge(text, tone = "neutral", title) {
  return h("span", { class: `badge badge-${tone}`, title }, text);
}

export function announce(message) {
  const el = document.getElementById("announcer");
  if (!el) return;
  el.textContent = "";
  // re-set on the next frame so repeated identical messages are still announced
  requestAnimationFrame(() => { el.textContent = message; });
}

export function loadingBlock(label = "Loading…") {
  return h("div", { class: "loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, label));
}

export function errorBlock(message, onRetry) {
  return h("div", { class: "alert alert-danger", role: "alert" },
    h("p", {}, message),
    onRetry ? h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: onRetry }, "Try again") : null);
}

export function emptyBlock(title, text) {
  return h("div", { class: "empty-state" }, h("h3", {}, title), text ? h("p", {}, text) : null);
}

export function kv(label, value) {
  return h("div", { class: "kv" }, h("dt", {}, label), h("dd", {}, value));
}
