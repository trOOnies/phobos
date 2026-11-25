// Places: the place filter menu, the "Where was this taken?" dialog and formatting.
// Place names come from GPS (looked up offline on the server) or are typed in.

import { api } from "./api.js";
import { h, icon } from "./ui.js";

let cache = null;

/** Countries with their cities and counts (cached until something changes). */
export function loadPlaces() {
  cache ??= api.get("/api/places").catch((err) => { cache = null; throw err; });
  return cache;
}

export function invalidatePlaces() {
  cache = null;
}

/** "Buenos Aires, Buenos Aires F.D., Argentina" (skips missing parts). */
export const formatPlace = (p) => [p.city, p.region, p.country].filter(Boolean).join(", ");

/** "Buenos Aires, Argentina" - what a filter shows. */
export const shortPlace = (p) => [p.city, p.country].filter(Boolean).join(", ");

/** Link to everything from a place (the library, filtered). */
export function placeHref({ country, city }) {
  const params = new URLSearchParams();
  if (country) params.set("country", country);
  if (city) params.set("city", city);
  return `#/library?${params}`;
}

const same = (a, b) => (a ?? "").toLocaleLowerCase() === (b ?? "").toLocaleLowerCase();

/**
 * Menu to show one country or city. Hidden while the library has no places.
 * @param {{country?: string, city?: string}} current
 * @param {(place: {country: string, city: string}) => void} onChange
 */
export function placeFilter(current, onChange) {
  const select = h("select", { "aria-label": "Place" }, h("option", { value: "", text: "Anywhere" }));
  const wrap = h("label.place-filter", { hidden: true, title: "Show photos and videos from one place" }, icon("pin"), select);
  const value = (country, city) => JSON.stringify([country ?? "", city ?? ""]);
  loadPlaces().then((countries) => {
    const active = current.country || current.city;
    if (!countries.length && !active) return;
    let selected = "";
    for (const c of countries) {
      const v = value(c.country, "");
      select.append(h("option", { value: v, text: `${c.country} (${c.count.toLocaleString()})` }));
      if (same(c.country, current.country) && !current.city) selected = v;
      for (const ci of c.cities) {
        const cv = value(c.country, ci.city);
        // Indented under its country; the region tells same-named towns apart.
        select.append(h("option", { value: cv, text: ` ${ci.city} (${ci.count.toLocaleString()})`, title: ci.region || "" }));
        if (same(ci.city, current.city) && (!current.country || same(c.country, current.country))) selected = cv;
      }
    }
    if (active && !selected) {
      // A place nothing is from any more (e.g. everything there was trashed): keep it visible.
      selected = value(current.country, current.city);
      select.append(h("option", { value: selected, text: `${shortPlace(current)} (0)` }));
    }
    select.value = selected;
    wrap.hidden = false;
  }).catch(() => { /* the filter is optional */ });
  select.addEventListener("change", () => {
    const [country, city] = select.value ? JSON.parse(select.value) : ["", ""];
    onChange({ country, city });
  });
  return wrap;
}

// ------------------------------------------------------------------ place dialog

const dialog = document.getElementById("place-dialog");
const fields = {
  city: dialog.querySelector("#place-city"),
  region: dialog.querySelector("#place-region"),
  country: dialog.querySelector("#place-country"),
};
const error = dialog.querySelector("#place-error");

// A place needs a city or a country; keep the dialog open until it has one.
dialog.querySelector("form").addEventListener("submit", (e) => {
  if (e.submitter?.value !== "ok") return;
  if (!fields.city.value.trim() && !fields.country.value.trim()) {
    e.preventDefault();
    error.hidden = false;
    fields.city.focus();
  }
});

// A different city: its old region no longer applies. A known one fills in its
// region (and its country when none is typed yet).
fields.city.addEventListener("change", async () => {
  const city = fields.city.value.trim();
  const match = (await loadPlaces().catch(() => []))
    .flatMap((c) => c.cities.map((ci) => ({ ...ci, country: c.country })))
    .find((ci) => same(ci.city, city) && (!fields.country.value.trim() || same(ci.country, fields.country.value.trim())));
  fields.region.value = match?.region ?? "";
  if (match && !fields.country.value.trim()) fields.country.value = match.country;
});

/**
 * Ask where photos were taken.
 * @returns {Promise<{place: {city: string, region: string, country: string}} | {reset: true} | null>}
 */
export async function editPlace({ title = "Where was this taken?", message = "", current = {}, canReset = false } = {}) {
  dialog.querySelector("#place-title").textContent = title;
  const msg = dialog.querySelector("#place-message");
  msg.textContent = message;
  msg.hidden = !message;
  for (const [k, input] of Object.entries(fields)) input.value = current[k] ?? "";
  error.hidden = true;
  dialog.querySelector("#place-reset").hidden = !canReset;

  const countries = await loadPlaces().catch(() => []);
  const cityNames = [...new Set(countries.flatMap((c) => c.cities.map((ci) => ci.city)))].sort();
  dialog.querySelector("#place-cities").replaceChildren(...cityNames.map((value) => h("option", { value })));
  dialog.querySelector("#place-countries").replaceChildren(...countries.map((c) => h("option", { value: c.country })));

  const result = new Promise((resolve) => dialog.addEventListener("close", () => {
    const v = dialog.returnValue;
    dialog.returnValue = "";
    resolve(v);
  }, { once: true }));
  dialog.showModal();
  fields.city.focus();
  const value = await result;
  if (value === "reset") return { reset: true };
  if (value !== "ok") return null;
  return { place: Object.fromEntries(Object.entries(fields).map(([k, input]) => [k, input.value.trim()])) };
}
