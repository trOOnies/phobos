// Full-screen viewer (<dialog> opened with showModal()).
// Keys: ←/→ navigate, F favorite, I info, P people & pets, T tag a face, A add to album,
// E edit (crop), Del trash, Esc close.

import { api } from "./api.js";
import { CropEditor } from "./editor.js";
import { FaceLayer } from "./faces.js";
import { createPerson, findPerson, invalidatePeople, kindIcon, loadPeople, pickPerson } from "./people.js";
import { editPlace, formatPlace, invalidatePlaces, placeHref } from "./places.js";
import {
  albumToast, formatBytes, formatDateTime, formatDuration, formatLongDate, h, icon, toast, toastError,
} from "./ui.js";

const INFO_KEY = "phobos.viewer.info";

export class Viewer {
  constructor({ pickAlbum, onChange, onRemove, onRestore }) {
    this.onRestore = onRestore; // (ids) => void, "Undo" after trashing
    this.dialog = document.getElementById("viewer");
    this.stage = document.getElementById("v-stage");
    this.info = document.getElementById("v-info");
    this.pickAlbum = pickAlbum;
    this.onChange = onChange; // (item, grid) => void, after an edit
    this.onRemove = onRemove; // (ids, grid) => void, after trash/restore
    this.grid = null;
    this.idx = 0;
    this.detail = null; // full record of the current item (tags, albums)
    this.btn = Object.fromEntries([...this.dialog.querySelectorAll("[data-v]")].map((b) => [b.dataset.v, b]));
    // Name suggestions for the people & pets field.
    this.names = h("datalist", { id: "people-names" });
    this.dialog.append(this.names);
    this.focusPeople = false; // put the cursor back in the people field after a re-render
    this.faces = null; // FaceLayer over the current photo

    try { this.info.hidden = localStorage.getItem(INFO_KEY) !== "1"; } catch { /* storage unavailable */ }
    this.btn.info.setAttribute("aria-pressed", String(!this.info.hidden));

    this.dialog.addEventListener("click", (e) => {
      const action = e.target.closest("[data-v]")?.dataset.v;
      if (action && action !== "download") this.#act(action);
    });
    this.editor = new CropEditor(this.dialog);
    this.dialog.addEventListener("keydown", (e) => this.#onKey(e));
    // Esc while cropping (or drawing a face box) stops that, not the viewer.
    this.dialog.addEventListener("cancel", (e) => {
      if (this.editor.open) { e.preventDefault(); this.editor.cancel(); }
      else if (this.faces?.drawing) { e.preventDefault(); this.faces.cancel(); }
    });
    this.dialog.addEventListener("close", () => this.#onClose());
  }

  get item() {
    return this.grid?.items[this.idx];
  }

  open(grid, idx) {
    this.grid = grid;
    this.idx = idx;
    if (!this.dialog.open) this.dialog.showModal();
    this.#render();
  }

  #onClose() {
    if (this.editor.open) this.editor.cancel();
    this.faces?.destroy();
    this.faces = null;
    this.stage.replaceChildren(); // stops video playback
    const tile = this.item && this.grid.tileFor(this.item.id);
    if (tile) {
      tile.scrollIntoView({ block: "nearest" });
      tile.focus({ preventScroll: true });
    }
  }

  #onKey(e) {
    if (this.editor.open || this.faces?.drawing) return;
    if (e.target.closest("input, textarea")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const map = { ArrowLeft: "prev", ArrowRight: "next", f: "favorite", i: "info", p: "people", t: "face", a: "album", e: "edit", Delete: "trash" };
    const action = map[e.key] ?? map[e.key.toLowerCase()];
    if (!action) return;
    // Let the video element handle arrow keys (seeking) when it has focus.
    if (e.target.tagName === "VIDEO" && e.key.startsWith("Arrow")) return;
    e.preventDefault();
    this.#act(action);
  }

  async #act(action) {
    const item = this.item;
    try {
      switch (action) {
        case "close": return this.dialog.close();
        case "prev": return this.#go(-1);
        case "next": return this.#go(1);
        case "info": {
          this.info.hidden = !this.info.hidden;
          this.btn.info.setAttribute("aria-pressed", String(!this.info.hidden));
          try { localStorage.setItem(INFO_KEY, this.info.hidden ? "0" : "1"); } catch { /* ignore */ }
          if (!this.info.hidden) this.#renderInfo();
          this.#renderFaces();
          return;
        }
        case "face": return this.#tagFace();
        case "people": {
          this.focusPeople = true; // kept until the details (with the people) have loaded
          if (this.info.hidden) await this.#act("info");
          this.info.querySelector(".people-input")?.focus();
          if (this.detail) this.focusPeople = false;
          return;
        }
        case "favorite": {
          const updated = await api.patch(`/api/media/${item.id}`, { favorite: !item.favorite });
          this.#updated(updated);
          return;
        }
        case "album": {
          const album = await this.pickAlbum({ contains: (this.detail?.albums ?? []).map((x) => x.id) });
          if (!album) return;
          const r = await api.action([item.id], "add_to_album", { album_id: album.id });
          albumToast(album, { added: r.affected, onOpen: () => this.dialog.close() });
          this.#loadDetail();
          return;
        }
        case "edit": {
          if (!editable(item)) return;
          const result = await this.editor.edit(item);
          if (result) this.#edited(item, result);
          return;
        }
        case "trash": {
          if (item.deleted_at) return;
          await api.action([item.id], "trash");
          toast("Moved to trash", {
            action: "Undo",
            onAction: () => this.onRestore([item.id]),
          });
          const wasLast = this.idx >= this.grid.items.length - 1;
          this.onRemove([item.id], this.grid);
          if (!this.grid.items.length) return this.dialog.close();
          if (wasLast) this.idx = this.grid.items.length - 1;
          this.#render();
          return;
        }
      }
    } catch (err) {
      toastError(err);
    }
  }

  async #go(delta) {
    const next = this.idx + delta;
    if (next < 0) return;
    if (next >= this.grid.items.length) {
      if (this.grid.done) return;
      await this.grid.loadMore();
      if (next >= this.grid.items.length) return;
    }
    this.idx = next;
    this.#render();
  }

  /** After a crop: show the new photo where the old one was (or right after it, for a copy). */
  #edited(old, { media, replaced }) {
    if (replaced) {
      toast("Cropped. The original is in the Trash.", {
        action: "Undo",
        onAction: async () => {
          try {
            await api.action([media.id], "trash");
            if (this.dialog.open) this.dialog.close();
            this.onRestore([old.id]); // brings the original back and reloads the view
          } catch (err) { toastError(err); }
        },
      });
      if (this.grid.swap?.(old.id, media)) return this.#render();
      this.onRemove([old.id], this.grid); // a list that can't show the new item: drop the old one
      if (!this.grid.items.length) return this.dialog.close();
      this.idx = Math.min(this.idx, this.grid.items.length - 1);
      return this.#render();
    }
    toast("Saved a cropped copy next to the original.");
    if (this.grid.insertAfter?.(old.id, media)) {
      this.idx += 1;
      this.#render();
    }
  }

  #updated(item) {
    this.detail = item;
    this.onChange(item, this.grid);
    this.#renderBar();
    if (!this.info.hidden) this.#renderInfo();
    this.#renderFaces();
  }

  /** Face boxes on the photo: shown while the details panel is open, or while drawing. */
  #renderFaces() {
    if (!this.faces) return;
    this.faces.show(this.detail?.people ?? []);
    this.stage.classList.toggle("show-faces", !this.info.hidden);
    this.faces.layout(); // the panel opening or closing resizes the photo
  }

  /**
   * Drag a box around a face, then say whose it is (or pass who it is, to mark or move
   * their face). Tags them in the photo if they weren't yet.
   */
  async #tagFace(person = null) {
    const item = this.item;
    if (!this.faces || !canTagFaces(item)) return;
    if (this.info.hidden) await this.#act("info"); // show the other boxes and the names
    const face = await this.faces.draw(person ? `Drag a box around ${person.name}'s face` : "Drag a box around a face");
    if (!face || this.item?.id !== item.id) return;
    const who = person ?? await pickPerson({
      title: "Whose face is this?", contains: (this.detail?.people ?? []).map((p) => p.id),
    });
    if (!who) return this.#renderFaces(); // drops the drawn box
    try {
      const updated = await api.put(`/api/media/${item.id}/people/${who.id}`, { face });
      invalidatePeople();
      this.#updated(updated);
    } catch (err) { toastError(err); this.#renderFaces(); }
  }

  #render() {
    const item = this.item;
    if (!item) return;
    this.detail = null;
    this.#renderBar();
    this.#renderMedia(item);
    if (!this.info.hidden) this.#renderInfo();
    this.#loadDetail();
    // Near the end of what's loaded? Fetch the next page in the background.
    if (this.grid.items.length - this.idx < 10) this.grid.loadMore();
    this.#prefetch();
  }

  async #loadDetail() {
    const id = this.item.id;
    try {
      const detail = await api.get(`/api/media/${id}`);
      if (this.item?.id !== id) return;
      this.detail = detail;
      if (!this.info.hidden) this.#renderInfo();
      this.#renderFaces();
    } catch (err) {
      toastError(err);
    }
  }

  #prefetch() {
    for (const d of [1, -1, 2]) {
      const it = this.grid.items[this.idx + d];
      if (it?.kind === "image") new Image().src = it.preview_url;
    }
  }

  #renderBar() {
    const item = this.item;
    document.getElementById("v-date").textContent = formatLongDate(item.taken_at);
    document.getElementById("v-name").textContent = item.original_name;
    this.btn.favorite.setAttribute("aria-pressed", String(!!item.favorite));
    this.btn.download.href = item.download_url;
    this.btn.trash.hidden = !!item.deleted_at;
    this.btn.album.hidden = !!item.deleted_at;
    this.btn.people.hidden = !!item.deleted_at;
    this.btn.face.hidden = !canTagFaces(item);
    this.btn.edit.hidden = !editable(item);
    this.btn.prev.disabled = this.idx === 0;
    this.btn.next.disabled = this.grid.done && this.idx >= this.grid.items.length - 1;
  }

  #renderMedia(item) {
    this.faces?.destroy();
    this.faces = null;
    if (item.kind === "image") {
      const img = h("img", { src: item.preview_url, alt: item.description || item.original_name });
      this.stage.replaceChildren(img);
      this.faces = new FaceLayer(img, this.stage, {
        onOpen: (p) => { this.dialog.close(); location.hash = `#/people/${p.id}`; },
      });
      this.#renderFaces();
      return;
    }
    const video = h("video", {
      src: item.original_url, controls: true, autoplay: true, playsInline: true, preload: "metadata",
      poster: item.thumb_url,
    });
    video.addEventListener("error", () => {
      this.stage.replaceChildren(h("div.unplayable", {},
        icon("video"),
        h("p", { text: "This browser can't play this video format (e.g. HEVC, AVI, MKV codecs)." }),
        h("a.btn.primary", { href: item.download_url, download: "" }, icon("download"), h("span", { text: "Download original" }))));
    });
    this.stage.replaceChildren(video);
  }

  #renderInfo() {
    const item = this.detail ?? this.item;
    const save = async (body) => {
      try {
        this.#updated(await api.patch(`/api/media/${item.id}`, body));
      } catch (err) {
        toastError(err);
      }
    };

    const desc = h("textarea", { rows: 3, placeholder: "Add a description", value: item.description ?? "", maxLength: 5000 });
    desc.addEventListener("change", () => save({ description: desc.value }));

    const when = h("input", { type: "datetime-local", step: 1, value: item.taken_at });
    when.addEventListener("change", () => when.value && save({ taken_at: when.value }));

    const tags = this.detail?.tags ?? [];
    const tagInput = h("input", { placeholder: "Add tag, press Enter", maxLength: 64, "aria-label": "Add tag" });
    tagInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && tagInput.value.trim()) {
        e.preventDefault();
        save({ tags: [...tags, tagInput.value.trim()] });
      }
    });
    const tagChips = tags.map((t) =>
      h("span.chip", {},
        h("a", { href: `#/library?tag=${encodeURIComponent(t)}`, text: t, onclick: () => this.dialog.close() }),
        h("button", { type: "button", "aria-label": `Remove tag ${t}`, onclick: () => save({ tags: tags.filter((x) => x !== t) }) }, icon("close"))));

    const albums = (this.detail?.albums ?? []).map((a) =>
      h("a.chip", { href: `#/albums/${a.id}`, onclick: () => this.dialog.close() }, icon("album"), h("span", { text: a.name })));

    const rows = [
      ["Taken", `${formatDateTime(item.taken_at)}${item.taken_at_source === "exif" || item.taken_at_source === "video" ? "" : ` (from ${item.taken_at_source})`}`],
      ["File", item.original_name],
      ["Size", `${item.width && item.height ? `${item.width} × ${item.height} · ` : ""}${formatBytes(item.size)}`],
      item.duration ? ["Length", formatDuration(item.duration)] : null,
      item.camera_model || item.camera_make ? ["Camera", [item.camera_make, item.camera_model].filter(Boolean).join(" ")] : null,
      ["Stored as", item.rel_path],
    ].filter(Boolean);

    const location = this.#placeSection(item, save);
    const people = this.#peopleSection(item);

    // The panel is rebuilt when details load: keep a name that's being typed.
    const typing = document.activeElement?.matches?.(".people-input") ? document.activeElement.value : null;
    this.info.replaceChildren(...[
      h("h2", { text: "Details" }),
      h("section", {}, h("span.label", { text: "Description" }), desc),
      h("dl.kv", {}, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
      location,
      people,
      h("section", {}, h("span.label", { text: "Date & time" }), when),
      h("section", {}, h("span.label", { text: "Tags" }), h("div.chips", {}, tagChips), tagInput),
      h("section", {}, h("span.label", { text: "Albums" }),
        albums.length ? h("div.chips", {}, albums) : h("span", { text: "Not in any album", style: "color:#9a929c" })),
    ].filter(Boolean));
    const peopleInput = this.info.querySelector(".people-input");
    if (typing !== null && peopleInput) {
      peopleInput.value = typing;
      peopleInput.focus();
    } else if (this.focusPeople) {
      peopleInput?.focus();
      if (this.detail) this.focusPeople = false;
    }
  }

  /** Where it was taken: the place name (links to everything from there) and the GPS spot. */
  #placeSection(item, save) {
    const hasGps = item.latitude !== null && item.latitude !== undefined;
    const name = formatPlace(item);
    const change = h("button.link-btn", {
      type: "button", text: name ? "Change" : "Add place",
      onclick: async () => {
        const answer = await editPlace({ current: item, canReset: item.place_source === "manual" });
        if (!answer) return;
        await save({ place: answer.reset ? null : answer.place });
        invalidatePlaces();
      },
    });
    let map = null;
    if (hasGps) {
      const { latitude: lat, longitude: lon } = item;
      map = h("a.coords", { href: `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`, target: "_blank", rel: "noopener noreferrer", title: "Open the map (OpenStreetMap, online)" },
        `${lat.toFixed(5)}, ${lon.toFixed(5)} ↗`);
    }
    const note = item.place_source === "manual" ? "typed in" : hasGps && !name ? "no town nearby" : "";
    return h("section.place", {},
      h("span.label", { text: "Place" }),
      name
        ? h("a.place-name", { href: placeHref(item), title: "Everything from here", onclick: () => this.dialog.close() }, icon("pin"), h("span", { text: name }))
        : h("span.muted-dark", { text: hasGps ? "Unknown place" : "No location saved" }),
      h("div.place-meta", {}, map, note ? h("small", { text: note }) : null, change));
  }

  /** Who is in it: chips for each person or pet, and a field to add one by name. */
  #peopleSection(item) {
    const people = this.detail?.people ?? [];
    const update = async (fn) => {
      try {
        await fn();
        invalidatePeople();
        await this.#loadDetail();
      } catch (err) { toastError(err); }
    };
    const add = (person) => update(async () => {
      await api.action([item.id], "add_person", { person_id: person.id });
      this.focusPeople = true;
    });
    const faces = canTagFaces(item);
    const chips = people.map((p) => {
      const faceLabel = p.face ? `Move ${p.name}'s face box` : `Mark where ${p.name} is`;
      return h("span.chip", {
        // Hovering a name lights up their box on the photo.
        onpointerenter: () => this.faces?.highlight(p.id),
        onpointerleave: () => this.faces?.highlight(null),
      },
        h("a", { href: `#/people/${p.id}`, onclick: () => this.dialog.close(), title: `Everything with ${p.name}` },
          icon(kindIcon(p.kind)), h("span", { text: p.name })),
        faces ? h(`button${p.face ? ".has-face" : ""}`, { type: "button", "aria-label": faceLabel, title: faceLabel,
          onclick: () => this.#tagFace(p) }, icon("face")) : null,
        h("button", { type: "button", "aria-label": `${p.name} isn't in this`, title: `${p.name} isn't in this`,
          onclick: () => update(() => api.action([item.id], "remove_person", { person_id: p.id })) }, icon("close")));
    });
    const tagFace = faces
      ? h("button.link-btn.tag-face", { type: "button", onclick: () => this.#tagFace() }, icon("face"), h("span", { text: "Tag a face (T)" }))
      : null;

    const input = h("input.people-input", { placeholder: "Add a name, press Enter", maxLength: 100, list: "people-names", "aria-label": "Add a person or pet", autocomplete: "off" });
    // Someone new: ask whether it's a person or an animal before saving the name.
    const ask = h("div.new-kind", { hidden: true });
    const askKind = (name) => {
      const choose = (kind) => update(async () => {
        const person = await createPerson(name, kind);
        await api.action([item.id], "add_person", { person_id: person.id });
        this.focusPeople = true;
      });
      ask.replaceChildren(
        h("span", { text: `“${name}” is new. Is it a…` }),
        h("div.row", {},
          h("button.btn.small", { type: "button", onclick: () => choose("person") }, icon("person"), h("span", { text: "Person" })),
          h("button.btn.small", { type: "button", onclick: () => choose("animal") }, icon("paw"), h("span", { text: "Animal" })),
          h("button.link-btn", { type: "button", text: "Cancel", onclick: () => { ask.hidden = true; input.focus(); } })));
      ask.hidden = false;
      ask.querySelector("button").focus();
    };
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" || !input.value.trim()) return;
      e.preventDefault();
      const name = input.value.trim();
      try {
        const known = await findPerson(name);
        if (known) {
          if (people.some((p) => p.id === known.id)) { input.value = ""; return toast(`${known.name} is already here`); }
          return add(known);
        }
        askKind(name);
      } catch (err) { toastError(err); }
    });
    loadPeople().then((all) => {
      const here = new Set(people.map((p) => p.id));
      this.names.replaceChildren(...all.filter((p) => !here.has(p.id)).map((p) => h("option", { value: p.name })));
    }).catch(() => { /* suggestions are optional */ });

    return h("section.people-field", {},
      h("span.label", { text: "People & pets" }),
      chips.length ? h("div.chips", {}, chips) : null,
      item.deleted_at ? null : input,
      ask,
      tagFace);
  }
}

/** Photos whose faces can be marked (not videos or trashed items). */
function canTagFaces(item) {
  return item.kind === "image" && !item.deleted_at;
}

/** Photos that can be cropped (not videos, GIFs or trashed items). */
function editable(item) {
  return item.kind === "image" && item.mime !== "image/gif" && !item.deleted_at;
}
