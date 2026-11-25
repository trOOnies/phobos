// Small DOM and formatting helpers shared by every view.

/** h("div.cls", {attrs}, ...children) - tiny element builder. */
export function h(spec, attrs = {}, ...children) {
  const [tag, ...classes] = spec.split(".");
  const el = document.createElement(tag || "div");
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "text") el.textContent = v;
    else if (k === "value") el.value = v; // <textarea> has no value attribute
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

export function button(label, { icon: name, cls = "btn", onclick, title, ...rest } = {}) {
  return h(`button.${cls.split(" ").join(".")}`, { type: "button", onclick, title, ...rest },
    name ? icon(name) : null, h("span", { text: label }));
}

// ------------------------------------------------------------------ formatting

const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const longDateFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "long", year: "numeric" });
const monthFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const shortMonthFmt = new Intl.DateTimeFormat(undefined, { month: "short" });

// Timestamps from the API are naive local wall-clock times ("2024-07-15T14:30:12"),
// which Date parses as local time - exactly what we want.
export const parseDate = (s) => new Date(s);
export const formatDateTime = (s) => dateFmt.format(parseDate(s));
export const formatLongDate = (s) => longDateFmt.format(parseDate(s));
export const formatMonth = (ym) => monthFmt.format(new Date(`${ym}-01T00:00:00`));
export const formatShortMonth = (ym) => shortMonthFmt.format(new Date(`${ym}-01T00:00:00`));

export function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function formatDuration(sec) {
  if (sec === null || sec === undefined) return "";
  const s = Math.round(sec);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

export const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ------------------------------------------------------------------ toasts

export function toast(message, { error = false, action, onAction, timeout = 5000 } = {}) {
  const box = document.getElementById("toasts");
  // A modal <dialog> (like the viewer) covers - and makes inert - everything outside it,
  // so toasts go inside the topmost one while it's open.
  const host = [...document.querySelectorAll("dialog:modal")].at(-1) ?? document.body;
  if (box.parentElement !== host) {
    host.append(box);
    if (host !== document.body) host.addEventListener("close", () => document.body.append(box), { once: true });
  }
  const el = h("div.toast", { class: error ? "toast error" : "toast" }, h("span", {}, message));
  if (action) {
    el.append(h("button", { type: "button", text: action, onclick: () => { onAction?.(); el.remove(); } }));
  }
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => el.remove(), error ? Math.max(timeout, 8000) : timeout);
}

/**
 * Confirm an "add to album" with a link to the album.
 * @param {{id: number, name: string, created?: boolean}} album
 * @param {{added: number, count?: number, onOpen?: () => void}} opts
 *   added: how many were newly added; count: how many were chosen (omit for a single open item)
 */
export function albumToast(album, { added, count, onOpen }) {
  const link = h("a", { href: `#/albums/${album.id}`, text: album.name, onclick: (e) => {
    e.currentTarget.closest(".toast")?.remove();
    onOpen?.();
  } });
  const many = count !== undefined;
  let msg;
  if (album.created) msg = many ? ["Created the album ", link, ` with ${plural(added, "item")}`] : ["Created the album ", link, " and added this to it"];
  else if (!added) msg = [many && count > 1 ? "Already all in " : "Already in ", link];
  else msg = many ? [`Added ${plural(added, "item")} to `, link] : ["Added to ", link];
  toast(msg);
}

export const toastError =(err) => toast(err?.message || String(err), { error: true });

// ------------------------------------------------------------------ dialogs

/** Resolve with the returnValue of a <dialog>, resetting it for next time. */
function closed(dialog) {
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      const v = dialog.returnValue;
      dialog.returnValue = "";
      resolve(v);
    }, { once: true });
  });
}

export async function confirmDialog({ title, message = "", confirm = "OK", danger = false }) {
  const d = document.getElementById("ask");
  d.querySelector("#ask-title").textContent = title;
  d.querySelector("#ask-message").textContent = message;
  d.querySelector("#ask-message").hidden = !message;
  d.querySelector("#ask-field").hidden = true;
  d.querySelector("#ask-input").required = false;
  const ok = d.querySelector("#ask-ok");
  ok.textContent = confirm;
  ok.className = `btn ${danger ? "danger" : "primary"}`;
  const result = closed(d);
  d.showModal();
  d.querySelector("#ask-cancel").focus(); // safe action first for destructive prompts
  return (await result) === "ok";
}

export async function promptDialog({ title, label, value = "", confirm = "Save", message = "" }) {
  const d = document.getElementById("ask");
  d.querySelector("#ask-title").textContent = title;
  d.querySelector("#ask-message").textContent = message;
  d.querySelector("#ask-message").hidden = !message;
  d.querySelector("#ask-field").hidden = false;
  d.querySelector("#ask-label").textContent = label;
  const input = d.querySelector("#ask-input");
  input.value = value;
  input.required = true;
  const ok = d.querySelector("#ask-ok");
  ok.textContent = confirm;
  ok.className = "btn primary";
  const result = closed(d);
  d.showModal();
  input.focus();
  input.select();
  return (await result) === "ok" ? input.value.trim() : null;
}
