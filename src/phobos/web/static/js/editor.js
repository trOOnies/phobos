// Photo editor (crop), shown over the viewer. The crop box is kept as fractions of
// the upright picture, which is what the server's /edit endpoint takes.
//
// Drag the box to move it, drag a corner or edge to resize. With the box focused,
// arrow keys move it and Shift+arrow keys resize it. Esc cancels.

import { api } from "./api.js";
import { button, h, toast, toastError } from "./ui.js";

const MIN = 0.02; // smallest box side, as a fraction of the picture
const STEP = 0.01; // keyboard nudge
const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const ASPECTS = [["free", "Free", null], ["square", "Square", 1], ["4:3", "4:3", 4 / 3], ["3:4", "3:4", 3 / 4],
  ["16:9", "16:9", 16 / 9], ["9:16", "9:16", 9 / 16]];
const FULL = { l: 0, t: 0, r: 1, b: 1 };

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class CropEditor {
  /** @param {HTMLDialogElement} dialog  the viewer, which hosts the editor */
  constructor(dialog) {
    this.dialog = dialog;
    this.root = null;
  }

  get open() { return !!this.root; }

  /** Resolve with the /edit response ({media, replaced}) or null when cancelled. */
  edit(item) {
    this.item = item;
    this.rect = { ...FULL };
    this.aspect = null; // width / height in pixels, or null for free
    return new Promise((resolve) => {
      this.resolve = resolve;
      this.#build();
    });
  }

  cancel() { this.#finish(null); }

  #finish(result) {
    this.root?.remove();
    this.root = null;
    // Focus would fall back to <body> (outside the viewer) and its shortcuts would stop working.
    const edit = this.dialog.querySelector('[data-v="edit"]');
    (edit && !edit.hidden ? edit : this.dialog.querySelector('[data-v="close"]'))?.focus();
    this.resolve?.(result);
    this.resolve = null;
  }

  // ---------------------------------------------------------------- DOM

  #build() {
    const item = this.item;
    this.img = h("img", { src: item.preview_url, alt: item.description || item.original_name, draggable: false });
    this.box = h("div.crop-box", {
      tabindex: 0, role: "group",
      "aria-label": "Crop area. Arrow keys move it, Shift and arrow keys resize it.",
    }, h("div.crop-grid"), HANDLES.map((dir) => h("span.crop-handle", { dataset: { dir } })));
    this.hole = h("div.crop-hole");
    // The shade is clipped to the picture; the box (and its handles) is not.
    this.wrap = h("div.crop-wrap", {}, this.img, h("div.crop-shade", {}, this.hole), this.box);
    this.readout = h("span.crop-size", { "aria-live": "polite" });

    this.aspectBtns = ASPECTS.map(([id, label, ratio]) => h("button.btn.small", {
      type: "button", text: label, "aria-pressed": String(id === "free"),
      onclick: (e) => this.#setAspect(ratio, e.currentTarget),
    }));
    const trim = button("Trim black borders", { cls: "btn small", icon: "crop", onclick: () => this.#trim() });
    const reset = button("Reset", { cls: "btn small", icon: "restore", onclick: () => { this.rect = { ...FULL }; this.#setAspect(null, this.aspectBtns[0]); } });

    this.saveBtns = [
      button("Cancel", { onclick: () => this.cancel() }),
      button("Save as copy", { onclick: () => this.#save("copy"), title: "Keep the original and add the cropped photo next to it" }),
      button("Save", { cls: "btn primary", icon: "check", onclick: () => this.#save("replace"), title: "Keep the cropped photo; the original goes to the Trash" }),
    ];

    this.root = h("div.editor", { role: "region", "aria-label": "Crop photo" },
      h("div.editor-bar", {},
        h("strong", { text: "Crop" }),
        h("div.editor-tools", { role: "group", "aria-label": "Shape" }, this.aspectBtns),
        h("div.editor-tools", {}, trim, reset)),
      h("div.editor-stage", {}, this.wrap),
      h("div.editor-bar.bottom", {}, this.readout, h("div.editor-tools", {}, this.saveBtns)));
    this.dialog.append(this.root);

    this.box.addEventListener("pointerdown", (e) => this.#onPointerDown(e));
    this.box.addEventListener("keydown", (e) => this.#onKey(e));
    this.img.addEventListener("load", () => this.#draw(), { once: true });
    if (this.img.complete) this.#draw();
    this.box.focus({ preventScroll: true });
  }

  /** Crop-box aspect in fraction units for a pixel aspect (fractions stretch with the picture). */
  #fracRatio(pixelRatio) {
    const { naturalWidth: w, naturalHeight: h } = this.img;
    return w && h ? pixelRatio * (h / w) : pixelRatio;
  }

  #draw() {
    const { l, t, r, b } = this.rect;
    const pos = { left: `${l * 100}%`, top: `${t * 100}%`, width: `${(r - l) * 100}%`, height: `${(b - t) * 100}%` };
    Object.assign(this.box.style, pos);
    Object.assign(this.hole.style, pos);
    const w = Math.round((r - l) * (this.item.width || this.img.naturalWidth));
    const hh = Math.round((b - t) * (this.item.height || this.img.naturalHeight));
    this.readout.textContent = `${w.toLocaleString()} × ${hh.toLocaleString()} px`;
  }

  // ---------------------------------------------------------------- shape

  #setAspect(ratio, btn) {
    for (const b of this.aspectBtns) b.setAttribute("aria-pressed", String(b === btn));
    this.aspect = ratio;
    if (ratio) {
      // Largest box of that shape, centred on the picture.
      const fr = this.#fracRatio(ratio);
      let w = 1, hh = w / fr;
      if (hh > 1) { hh = 1; w = fr; }
      this.rect = { l: (1 - w) / 2, t: (1 - hh) / 2, r: (1 + w) / 2, b: (1 + hh) / 2 };
    }
    this.#draw();
  }

  async #trim() {
    try {
      const a = await api.get(`/api/media/${this.item.id}/analysis`);
      if (!this.open) return;
      if (!a.crop) { toast("No black border found around this photo."); return; }
      this.#setAspect(null, this.aspectBtns[0]);
      this.rect = { l: a.crop.left, t: a.crop.top, r: a.crop.right, b: a.crop.bottom };
      this.#draw();
    } catch (err) { toastError(err); }
  }

  // ---------------------------------------------------------------- dragging

  #onPointerDown(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const dir = e.target.closest(".crop-handle")?.dataset.dir ?? "move";
    const bounds = this.wrap.getBoundingClientRect();
    const start = { ...this.rect };
    const x0 = e.clientX, y0 = e.clientY;
    this.box.setPointerCapture(e.pointerId);
    this.box.focus({ preventScroll: true });
    const move = (ev) => {
      const dx = (ev.clientX - x0) / bounds.width, dy = (ev.clientY - y0) / bounds.height;
      this.rect = dir === "move" ? moved(start, dx, dy) : resized(start, dir, dx, dy, this.aspect && this.#fracRatio(this.aspect));
      this.#draw();
    };
    const up = () => {
      this.box.removeEventListener("pointermove", move);
      this.box.removeEventListener("pointerup", up);
      this.box.removeEventListener("pointercancel", up);
    };
    this.box.addEventListener("pointermove", move);
    this.box.addEventListener("pointerup", up);
    this.box.addEventListener("pointercancel", up);
  }

  #onKey(e) {
    const d = { ArrowLeft: [-STEP, 0], ArrowRight: [STEP, 0], ArrowUp: [0, -STEP], ArrowDown: [0, STEP] }[e.key];
    if (!d) return;
    e.preventDefault();
    e.stopPropagation();
    this.rect = e.shiftKey ? resized(this.rect, "se", d[0], d[1], this.aspect && this.#fracRatio(this.aspect)) : moved(this.rect, d[0], d[1]);
    this.#draw();
  }

  // ---------------------------------------------------------------- saving

  async #save(mode) {
    const { l, t, r, b } = this.rect;
    for (const btn of this.saveBtns) btn.disabled = true;
    try {
      const result = await api.post(`/api/media/${this.item.id}/edit`, {
        crop: { left: l, top: t, right: r, bottom: b }, mode,
      });
      this.#finish(result);
    } catch (err) {
      toastError(err);
      for (const btn of this.saveBtns) btn.disabled = false;
    }
  }
}

function moved(rect, dx, dy) {
  const w = rect.r - rect.l, hh = rect.b - rect.t;
  const l = clamp(rect.l + dx, 0, 1 - w), t = clamp(rect.t + dy, 0, 1 - hh);
  return { l, t, r: l + w, b: t + hh };
}

/** Drag edge(s) named by ``dir`` (n/e/s/w, or corners); ``ratio`` keeps the shape (fraction units). */
function resized(rect, dir, dx, dy, ratio) {
  if (!ratio) {
    let { l, t, r, b } = rect;
    if (dir.includes("w")) l = clamp(l + dx, 0, r - MIN);
    if (dir.includes("e")) r = clamp(r + dx, l + MIN, 1);
    if (dir.includes("n")) t = clamp(t + dy, 0, b - MIN);
    if (dir.includes("s")) b = clamp(b + dy, t + MIN, 1);
    return { l, t, r, b };
  }
  // Keep the opposite side (or the centre line, for edge handles) fixed.
  const horiz = dir.includes("e") || dir.includes("w"), vert = dir.includes("n") || dir.includes("s");
  const ax = dir.includes("w") ? rect.r : dir.includes("e") ? rect.l : (rect.l + rect.r) / 2;
  const ay = dir.includes("n") ? rect.b : dir.includes("s") ? rect.t : (rect.t + rect.b) / 2;
  let w = rect.r - rect.l + (dir.includes("w") ? -dx : dir.includes("e") ? dx : 0);
  let hh = rect.b - rect.t + (dir.includes("n") ? -dy : dir.includes("s") ? dy : 0);
  if (horiz && vert) w = Math.max(w, hh * ratio);
  else if (vert) w = hh * ratio;
  const room = (a, side) => (side === "start" ? 1 - a : side === "end" ? a : 2 * Math.min(a, 1 - a));
  const maxW = room(ax, dir.includes("e") ? "start" : dir.includes("w") ? "end" : "mid");
  const maxH = room(ay, dir.includes("s") ? "start" : dir.includes("n") ? "end" : "mid");
  w = clamp(w, Math.max(MIN, MIN * ratio), Math.min(maxW, maxH * ratio));
  hh = w / ratio;
  const l = dir.includes("w") ? ax - w : dir.includes("e") ? ax : ax - w / 2;
  const t = dir.includes("n") ? ay - hh : dir.includes("s") ? ay : ay - hh / 2;
  return { l, t, r: l + w, b: t + hh };
}
