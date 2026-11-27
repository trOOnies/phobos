// Face boxes over the photo in the viewer: shows where each tagged person or pet is,
// and lets the user drag a box around a face to tag it.
//
// Boxes are stored as fractions (0-1) of the upright picture, so they fit the photo at
// any size. The layer is laid over the part of the stage the <img> actually covers
// (it's letterboxed by object-fit: contain).

import { h, icon } from "./ui.js";
import { kindIcon } from "./people.js";

// Smaller than this (as a share of the picture's width or height) is a stray click.
const MIN_SIZE = 0.02;

export class FaceLayer {
  /**
   * @param {HTMLImageElement} img  the photo, already in the stage
   * @param {HTMLElement} stage     its container (the layer is positioned inside it)
   * @param {{onOpen?: (person) => void}} opts  a face's name was clicked
   */
  constructor(img, stage, { onOpen } = {}) {
    this.img = img;
    this.stage = stage;
    this.onOpen = onOpen;
    this.el = h("div.faces", { "aria-hidden": "true" });
    this.hint = h("div.face-hint", { hidden: true, role: "status" });
    this.drawing = null; // {resolve, cleanup} while the user is drawing a box
    stage.append(this.el, this.hint);
    this.resize = new ResizeObserver(() => this.layout());
    this.resize.observe(stage);
    img.addEventListener("load", () => this.layout());
    this.layout();
  }

  /** Match the layer to the picture's on-screen rectangle. */
  layout() {
    const { naturalWidth: nw, naturalHeight: nh } = this.img;
    if (!nw || !nh) { this.el.hidden = true; return; }
    const box = this.img.getBoundingClientRect();
    const stage = this.stage.getBoundingClientRect();
    const scale = Math.min(box.width / nw, box.height / nh);
    const w = nw * scale, h_ = nh * scale;
    Object.assign(this.el.style, {
      left: `${box.left - stage.left + (box.width - w) / 2}px`,
      top: `${box.top - stage.top + (box.height - h_) / 2}px`,
      width: `${w}px`,
      height: `${h_}px`,
    });
    this.el.hidden = false;
  }

  /** Draw a box for everyone whose face is marked. */
  show(people) {
    const boxes = people.filter((p) => p.face).map((p) => {
      const { x, y, w, h: fh } = p.face;
      // Names go under the box, or above it when the box reaches the bottom.
      const label = h("button.face-name", { type: "button", tabIndex: -1, onclick: () => this.onOpen?.(p) },
        icon(kindIcon(p.kind)), h("span", { text: p.name }));
      return h("div.face", {
        dataset: { person: p.id, below: String(y + fh < 0.9) },
        style: `left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${fh * 100}%`,
      }, label);
    });
    this.el.replaceChildren(...boxes);
  }

  /** Light up one person's box (while their name is hovered in the details). */
  highlight(personId) {
    for (const b of this.el.querySelectorAll(".face")) {
      b.classList.toggle("hot", personId !== null && b.dataset.person === String(personId));
    }
  }

  /**
   * Let the user drag a box around a face.
   * @param {string} message  what to tell them while drawing
   * @returns {Promise<{x: number, y: number, w: number, h: number} | null>}  null if cancelled.
   *   The drawn box stays on the photo (while the user says whose face it is) until the
   *   next show().
   */
  draw(message) {
    this.cancel();
    this.layout();
    this.stage.classList.add("drawing-face");
    this.hint.replaceChildren(h("span", { text: message }), h("small", { text: "Esc to cancel" }));
    this.hint.hidden = false;

    return new Promise((resolve) => {
      let start = null;
      let rect = null;
      const ghost = h("div.face.new");
      const point = (e) => {
        const r = this.el.getBoundingClientRect();
        return {
          x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
        };
      };
      const down = (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        this.el.setPointerCapture(e.pointerId);
        start = point(e);
        rect = null;
        this.el.append(ghost);
      };
      const move = (e) => {
        if (!start) return;
        const p = point(e);
        rect = {
          x: Math.min(start.x, p.x), y: Math.min(start.y, p.y),
          w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y),
        };
        Object.assign(ghost.style, {
          left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.w * 100}%`, height: `${rect.h * 100}%`,
        });
      };
      const up = () => {
        if (!start) return;
        start = null;
        // Resolve after the click that follows this pointerup: a dialog opened before it
        // (the "whose face?" picker) would take that click as one outside it and close.
        const box = rect;
        if (box && box.w >= MIN_SIZE && box.h >= MIN_SIZE) setTimeout(() => finish(box), 0);
        else ghost.remove(); // a click, not a box: let them try again
      };
      const cleanup = (keep) => {
        this.el.removeEventListener("pointerdown", down);
        this.el.removeEventListener("pointermove", move);
        this.el.removeEventListener("pointerup", up);
        this.el.removeEventListener("pointercancel", up);
        if (!keep) ghost.remove();
        this.stage.classList.remove("drawing-face");
        this.hint.hidden = true;
        this.drawing = null;
      };
      const finish = (value) => { cleanup(value !== null); resolve(value); };
      this.el.addEventListener("pointerdown", down);
      this.el.addEventListener("pointermove", move);
      this.el.addEventListener("pointerup", up);
      this.el.addEventListener("pointercancel", up);
      this.drawing = { finish };
    });
  }

  /** Stop drawing (the draw() promise resolves to null). */
  cancel() {
    this.drawing?.finish(null);
  }

  destroy() {
    this.cancel();
    this.resize.disconnect();
    this.el.remove();
    this.hint.remove();
  }
}
