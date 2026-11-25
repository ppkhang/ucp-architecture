"use strict";
const STAGES = [["pending_payment", "Pending payment"], ["placed", "Placed"], ["processing", "Processing"],
  ["shipped", "Shipped"], ["delivered", "Delivered"]];
const $ = (s) => document.querySelector(s);
// everything from the shop goes through this before it reaches innerHTML
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const vnd = (n) => `${Number(n).toLocaleString("en-US")} VND`;
const time = (iso) => new Date(iso).toLocaleTimeString("en-GB");
let state = null;
let user = null; // who the shop's sign-in cookie belongs to
let myOrders = [];
const seen = new Map(); // order id -> status, to mark the waybill that just changed
const drawn = new Map();
const opened = new Set(); // orders whose details are open, kept across redraws

async function api(path, options) {
  const r = await fetch(path, options);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error?.message || `The shop answered ${r.status}`);
  return body;
}

function problem(message) {
  $("#problem").textContent = message || "";
  $("#problem").hidden = !message;
}

// redraw only what changed, and never under a field someone is typing in
function draw(selector, html) {
  const el = $(selector);
  if (drawn.get(selector) === html || (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT")) return;
  drawn.set(selector, html);
  el.innerHTML = html;
}

const payTag = (o) => ({
  succeeded: '<span class="tag ok">Paid</span>',
  failed: '<span class="tag bad">Payment failed</span>',
}[o.payment_status] || '<span class="tag wait">Not paid</span>');

function waybill(o, { owner, changed }) {
  const at = STAGES.findIndex(([key]) => key === o.status);
  const progress = o.status === "cancelled"
    ? (o.refund_ref ? '<p class="void">Refund started</p>' : "")
    : `<ol class="stages">${STAGES.map(([, name], i) =>
        `<li class="${i < at ? "done" : i === at ? "now" : ""}"${i === at ? ' aria-current="step"' : ""}>${name}</li>`).join("")}</ol>`;
  const pay = o.status === "pending_payment" && owner
    ? `<div class="actions"><a class="button primary" href="/pay/${esc(o.id)}">Pay ${esc(vnd(o.total))}</a></div>` : "";
  const next = at >= 0 ? STAGES[at + 1]?.[0] : undefined; // a cancelled order has no next stage
  const moves = [];
  if (o.status === "pending_payment") moves.push(`<a class="button" href="/pay/${esc(o.id)}">Open payment page</a>`);
  else if (next) moves.push(`<button type="button" data-act="status" data-id="${esc(o.id)}" data-status="${next}">Mark ${STAGES[at + 1][1].toLowerCase()}</button>`);
  if (["pending_payment", "placed", "processing"].includes(o.status)) {
    moves.push(`<button type="button" class="danger" data-act="status" data-id="${esc(o.id)}" data-status="cancelled">Cancel from the shop</button>`);
  }
  const stub = !owner && moves.length ? `<div class="stub test-only"><strong>Test controls</strong>${moves.join("")}</div>` : "";
  const first = o.lines[0];
  const summary = `${first.quantity} x ${first.product_name}${o.lines.length > 1 ? ` and ${o.lines.length - 1} more` : ""}`;
  const label = o.status === "cancelled" ? "Cancelled" : STAGES[at]?.[1] || o.status;
  const tone = o.status === "cancelled" ? "bad" : o.status === "pending_payment" ? "wait" : "now";
  return `<article class="waybill${changed ? " stamp-in" : ""}" aria-label="Order ${esc(o.id)}">
    <details data-order="${esc(o.id)}"${opened.has(o.id) ? " open" : ""}>
      <summary>
        <span class="what"><strong>${esc(summary)}</strong>
          <span class="muted">${esc((o.region || "VN"))}-${esc(o.id.replace("ord_", "").slice(0, 6).toUpperCase())}, ${esc(o.buyer)}</span></span>
        <span class="pill ${tone}">${esc(label)}</span>
        <span class="amount">${esc(vnd(o.total))}</span>
      </summary>
      <ul class="items">${o.lines.map((l) =>
        `<li><span>${esc(l.quantity)} x ${esc(l.product_name)} (${esc(l.name)})</span><span>${esc(vnd(l.amount))}</span></li>`).join("")}</ul>
      <dl class="fields">
        <div><dt>Shipping</dt><dd>${esc(o.shipping_method_id || "standard")}, ${esc(vnd(o.shipping_fee))}</dd></div>
        <div><dt>Payment</dt><dd>${payTag(o)}</dd></div>
        ${o.discount ? `<div><dt>Discount</dt><dd>${esc(vnd(o.discount))}</dd></div>` : ""}
        ${o.refund_ref ? `<div><dt>Refund</dt><dd>${esc(o.refund_ref)}</dd></div>` : ""}
        <div class="test-only"><dt>Order id</dt><dd>${esc(o.id)}</dd></div>
      </dl>
    </details>
    ${progress}${pay}${stub}
  </article>`;
}

function render() {
  const mode = state.config.mode;
  $("#mode-banner").hidden = mode === "always_succeed";
  $("#mode-banner").textContent = `Test mode is on: ${$(`#mode option[value="${mode}"]`)?.textContent || mode}.`;
  if (document.activeElement !== $("#mode")) $("#mode").value = mode;

  const changed = new Set(state.orders.filter((o) => seen.has(o.id) && seen.get(o.id) !== o.status).map((o) => o.id));
  state.orders.forEach((o) => seen.set(o.id, o.status));
  draw("#all-orders", state.orders.length
    ? state.orders.map((o) => waybill(o, { owner: false, changed: changed.has(o.id) })).join("")
    : '<p class="empty">No orders yet. An order shows up here when the agent confirms a checkout that the user agreed to.</p>');

  draw("#events", state.events.length ? state.events.slice().reverse().map((e) =>
    `<li><strong>${esc(e.type)}</strong> <span class="muted">${esc(time(e.created_at))}</span>
      <div class="muted test-only">${esc(JSON.stringify(e.data))}</div></li>`).join("") : '<li class="muted">None yet.</li>');

  draw("#refunds", state.refunds.length ? `<table><thead><tr><th>Order</th><th class="num">Amount</th><th>Status</th></tr></thead><tbody>${
    state.refunds.map((r) => `<tr><td>${esc(r.order_id)}</td><td class="num">${esc(vnd(r.amount))}</td><td>${esc(r.status)}
      ${r.status === "initiated" ? `<span class="test-only"><button type="button" data-act="refund" data-id="${esc(r.ref)}" data-ok="true">Complete</button>
      <button type="button" data-act="refund" data-id="${esc(r.ref)}" data-ok="false">Fail</button></span>` : ""}</td></tr>`).join("")
  }</tbody></table>` : '<p class="muted">No refunds. Cancelling a paid order starts one.</p>');

  draw("#stock", `<table><thead><tr><th>Variant</th><th>Price</th><th>Stock</th><th class="num">Held</th><th></th></tr></thead><tbody>${
    state.products.flatMap((p) => p.variants.map((v) => `<tr><td>${esc(p.name)}<div class="muted">${esc(v.id)}</div></td>
      <td><input type="number" min="0" id="price-${esc(v.id)}" aria-label="Price of ${esc(v.id)}" value="${esc(v.price)}"></td>
      <td><input type="number" min="0" id="stock-${esc(v.id)}" aria-label="Stock of ${esc(v.id)}" value="${esc(v.stock)}"></td>
      <td class="num">${esc(v.reserved)}</td><td><button type="button" data-act="variant" data-id="${esc(v.id)}">Save</button></td></tr>`)).join("")
  }</tbody></table>`);

  draw("#products", state.products.map((p) => `<article class="waybill">
    <div class="wb-head"><h3 class="route">${esc(p.name)}</h3><div class="muted test-only">${esc(p.id)}</div></div>
    <p class="items muted">${esc(p.description)}</p>
    <ul class="items">${p.variants.map((v) => {
      const left = v.stock - v.reserved;
      return `<li><span>${esc(v.name)} ${left <= 0 ? '<span class="tag bad">Sold out</span>' : left <= 3 ? `<span class="tag wait">${left} left</span>` : ""}
        <span class="muted test-only">${esc(v.id)}</span></span><strong>${esc(vnd(v.price))}</strong></li>`;
    }).join("")}</ul></article>`).join(""));

  $("#signin").hidden = !!user;
  $("#signed").hidden = !user;
  $("#who").textContent = user || "";
  draw("#my-orders", !user ? "" : myOrders.length
    ? myOrders.map((o) => waybill(o, { owner: true })).join("")
    : '<p class="empty">You have no orders in this shop yet.</p>');
}

const send = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function refresh() {
  state = await api("/_admin/state");
  ({ user, items: myOrders } = await api("/shop/session"));
  render();
}

async function act(button) {
  const { act: what, id } = button.dataset;
  const post = (path) => api(path, { method: "POST" });
  if (what === "status") await post(`/_admin/orders/${encodeURIComponent(id)}/status?status=${button.dataset.status}`);
  if (what === "refund") await post(`/_admin/refunds/${encodeURIComponent(id)}/settle?ok=${button.dataset.ok}`);
  if (what === "clock") await post(`/_admin/clock?advance_seconds=${button.dataset.seconds}`);
  if (what === "reset") {
    if (!confirm("Reset the shop? Every order, checkout and linked token is deleted.")) return;
    await post("/_admin/reset");
    seen.clear();
  }
  if (what === "variant") {
    const price = document.getElementById(`price-${id}`).value, stock = document.getElementById(`stock-${id}`).value;
    if (price === "" || stock === "") throw new Error("Price and stock both need a number.");
    await post(`/_admin/variants/${encodeURIComponent(id)}?price=${Number(price)}&stock=${Number(stock)}`);
  }
  if (what === "signout") await api("/shop/session", { method: "DELETE" });
  button.blur();
  await refresh();
}

function setView(view) {
  document.body.classList.toggle("testing", view === "testing");
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === view));
  localStorage.setItem("view", view);
}

function showPage() {
  const id = ["control", "store", "orders"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "control";
  document.querySelectorAll(".page").forEach((s) => { s.hidden = s.id !== id; });
  document.querySelectorAll(".mast nav a").forEach((a) =>
    a.hash === `#${id}` ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
}

document.addEventListener("click", (event) => {
  const view = event.target.closest("[data-view]");
  if (view) return setView(view.dataset.view);
  const button = event.target.closest("[data-act]");
  if (!button) return;
  button.disabled = true;
  act(button).then(() => problem(""), (e) => problem(e.message)).finally(() => { button.disabled = false; });
});

$("#mode").addEventListener("change", (event) =>
  send("/_admin/config", { mode: event.target.value }).then(refresh).then(() => problem(""), (e) => problem(e.message)));

$("#signin").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    await send("/shop/session", data);
    event.target.reset();
    problem("");
    await refresh();
  } catch (e) {
    problem(e.message);
  }
});

window.addEventListener("hashchange", showPage);
document.addEventListener("toggle", (event) => {
  const id = event.target.dataset?.order;
  if (id) event.target.open ? opened.add(id) : opened.delete(id);
}, true);
setView((new URLSearchParams(location.search).get("view") || localStorage.getItem("view")) === "testing" ? "testing" : "demo"); // ?view=testing links straight in
showPage();

let lost = false;
(async function poll() {
  try {
    await refresh();
    if (lost) problem("");
    lost = false;
  } catch (e) {
    lost = true;
    problem(`Cannot reach the shop: ${e.message}. Retrying.`);
  }
  setTimeout(poll, 2000);
})();
