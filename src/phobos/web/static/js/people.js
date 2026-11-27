// People and pets: who is in a photo or video, and (in photos, optionally) where their
// face is - see faces.js. Names are whatever the user likes.

import { api } from "./api.js";
import { button, h, icon, plural, toastError } from "./ui.js";

let cache = null;

/** Everyone, sorted by name (cached until something changes). */
export function loadPeople() {
  cache ??= api.get("/api/people").catch((err) => { cache = null; throw err; });
  return cache;
}

export function invalidatePeople() {
  cache = null;
}

export const kindIcon = (kind) => (kind === "animal" ? "paw" : "person");
export const kindLabel = (kind) => (kind === "animal" ? "Animal" : "Person");

const sameName = (a, b) => a.trim().toLocaleLowerCase() === b.trim().toLocaleLowerCase();

export async function findPerson(name) {
  return (await loadPeople()).find((p) => sameName(p.name, name)) ?? null;
}

export async function createPerson(name, kind) {
  const person = await api.post("/api/people", { name, kind });
  invalidatePeople();
  return person;
}

function closed(dialog) {
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    const v = dialog.returnValue;
    dialog.returnValue = "";
    resolve(v);
  }, { once: true }));
}

function cover(person, size = "") {
  return person.cover_id
    ? h("img", { src: `/api/media/${person.cover_id}/thumb`, alt: "", loading: "lazy" })
    : h(`span.ph${size}`, {}, icon(kindIcon(person.kind)));
}

// ------------------------------------------------------------------ picker

/**
 * "Who's in these?" - pick someone, or add someone new.
 * @param {{count?: number, contains?: number[], title?: string}} opts  how many items; people
 *   already in them; a heading other than "Who's in this?"
 * @returns {Promise<object|null>} the person (created if new)
 */
export async function pickPerson({ count = 1, contains = [], title = null } = {}) {
  const d = document.getElementById("person-picker");
  const list = d.querySelector("#person-choices");
  const filter = d.querySelector("#person-filter");
  const nameInput = d.querySelector("#person-new-name");
  const people = await loadPeople();
  const already = new Set(contains);
  d.querySelector("#person-picker-title").textContent = title ?? (count === 1 ? "Who's in this?" : `Who's in these ${count.toLocaleString()}?`);
  nameInput.value = "";
  filter.value = "";
  filter.hidden = people.length < 8;
  d.querySelector('input[name="picker-kind"][value="person"]').checked = true;

  const rows = people.map((p) => h("button", { type: "submit", value: `p:${p.id}`, dataset: { name: p.name.toLocaleLowerCase() } },
    cover(p),
    h("span", {}, h("strong", { text: p.name }),
      h("small", { text: `${kindLabel(p.kind)} · ${plural(p.count, "item")}${already.has(p.id) ? " · already tagged" : ""}` })),
    already.has(p.id) ? icon("check") : null));
  list.replaceChildren(...rows);
  if (!people.length) list.append(h("p.muted", { text: "Nobody yet - add the first person or pet below." }));
  filter.oninput = () => {
    const q = filter.value.trim().toLocaleLowerCase();
    for (const r of rows) r.hidden = !r.dataset.name.includes(q);
  };

  const result = closed(d);
  d.showModal();
  (people.length >= 8 ? filter : people.length ? list.querySelector("button") : nameInput).focus();
  const value = await result;
  if (value.startsWith("p:")) return people.find((p) => p.id === Number(value.slice(2))) ?? null;
  const name = nameInput.value.trim();
  if (value !== "new" || !name) return null;
  // Typing a name that's taken picks that person instead of failing.
  const existing = people.find((p) => sameName(p.name, name));
  if (existing) return existing;
  const kind = d.querySelector('input[name="picker-kind"]:checked')?.value ?? "person";
  return createPerson(name, kind);
}

// ------------------------------------------------------------------ add / edit form

/** @returns {Promise<{name: string, kind: string} | null>} */
export async function personForm({ title = "Add someone", name = "", kind = "person", confirm = "Save" } = {}) {
  const d = document.getElementById("person-form");
  d.querySelector("#person-form-title").textContent = title;
  d.querySelector("#person-form-ok").textContent = confirm;
  const input = d.querySelector("#person-form-name");
  input.value = name;
  d.querySelector(`input[name="form-kind"][value="${kind}"]`).checked = true;
  const result = closed(d);
  d.showModal();
  input.focus();
  input.select();
  if ((await result) !== "ok" || !input.value.trim()) return null;
  return { name: input.value.trim(), kind: d.querySelector('input[name="form-kind"]:checked').value };
}

// ------------------------------------------------------------------ people page (#/people)

export async function renderPeople(view) {
  invalidatePeople();
  const people = await loadPeople();
  const add = button("Add someone", {
    cls: "btn primary", icon: "plus",
    onclick: async () => {
      const form = await personForm({ confirm: "Add" });
      if (!form) return;
      try {
        const p = await createPerson(form.name, form.kind);
        location.hash = `#/people/${p.id}`;
      } catch (err) { toastError(err); }
    },
  });
  const card = (p) => h("a.album-card.person-card", { href: `#/people/${p.id}` },
    h("div.cover", {}, cover(p)),
    h("h3", {}, h("span", { text: p.name })),
    h("p", {}, icon(kindIcon(p.kind)), h("span", { text: plural(p.count, "item") })));
  const humans = people.filter((p) => p.kind !== "animal");
  const animals = people.filter((p) => p.kind === "animal");
  const group = (title, list) => (list.length
    ? h("section.people-group", {}, h("h2", { text: `${title} · ${list.length.toLocaleString()}` }), h("div.albums.people", {}, list.map(card)))
    : null);
  const meta = `${humans.length.toLocaleString()} ${humans.length === 1 ? "person" : "people"} · ${plural(animals.length, "animal")}`;
  // "You" is always there, so the hint shows until someone else is added or you're tagged.
  const started = people.some((p) => !p.is_self || p.count);
  view.replaceChildren(h("div.page", {},
    h("div.page-head", {}, h("h1", { text: "People & pets" }), h("span.meta", { text: meta }), h("div.actions", {}, add)),
    h("div", {}, group("People", humans), group("Animals", animals)),
    started ? null : h("div.empty", {}, icon("people"),
      h("h2", { text: "Who's in your photos?" }),
      h("p", { text: "Open a photo and press P to add the people and pets in it – type “You” for yourself – or select several and choose “People”." }),
      add.cloneNode(true)),
  ));
  // cloneNode drops listeners: wire the empty-state copy too.
  view.querySelector(".empty .btn")?.addEventListener("click", () => add.click());
}
