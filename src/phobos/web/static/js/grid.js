// Month-grouped, infinitely scrolling thumbnail grid with multi-select.

import { api } from "./api.js";
import { formatDuration, formatMonth, h, icon, plural, toastError } from "./ui.js";

export class MediaGrid {
  /**
   * @param {object} opts
   * @param {object} opts.filter   query params for /api/media
   * @param {(grid: MediaGrid, index: number) => void} opts.onOpen
   * @param {(grid: MediaGrid) => void} opts.onSelectionChange
   * @param {Node} opts.empty      shown when there is nothing to list
   */
  constructor({ filter, onOpen, onSelectionChange, empty }) {
    this.filter = filter;
    this.onOpen = onOpen;
    this.onSelectionChange = onSelectionChange;
    this.emptyNode = empty;
    this.items = [];
    this.index = new Map(); // id -> item
    this.selected = new Set();
    this.anchor = null; // last clicked index, for shift-range selection
    this.cursor = null;
    this.done = false;
    this.loading = null;
    this.months = new Map(); // "YYYY-MM" -> {section, tiles, countEl}
    this.counts = new Map(); // month counts from /api/timeline

    this.el = h("div.grid-root");
    this.sentinel = h("div.sentinel");
    this.status = h("div.loading", { text: "Loading…" });
    this.el.append(this.sentinel, this.status);
    this.observer = new IntersectionObserver(
      (entries) => entries.some((e) => e.isIntersecting) && this.loadMore(),
      { rootMargin: "1600px 0px" },
    );
    this.el.addEventListener("click", (e) => this.#onClick(e));
  }

  /** (Re)load from the newest item, or from ``startCursor`` (timeline jumps). */
  async reset(startCursor = null) {
    this.items = [];
    this.index.clear();
    this.months.clear();
    this.cursor = startCursor;
    this.done = false;
    this.clearSelection();
    for (const node of [...this.el.children]) if (node !== this.sentinel && node !== this.status) node.remove();
    this.observer.observe(this.sentinel);
    await this.loadMore();
  }

  setCounts(timeline) {
    this.counts = new Map(timeline.map((t) => [t.month, t.count]));
    for (const [month, m] of this.months) m.countEl.textContent = plural(this.counts.get(month) ?? 0, "item");
  }

  loadMore() {
    if (this.done) return Promise.resolve();
    this.loading ??= (async () => {
      try {
        const page = await api.media(this.filter, this.cursor);
        this.cursor = page.next_cursor;
        this.done = !page.next_cursor;
        this.#append(page.items);
      } catch (err) {
        toastError(err);
        this.done = true;
      } finally {
        this.loading = null;
        this.status.hidden = this.done;
        if (this.done) this.observer.disconnect();
        if (this.done && !this.items.length && this.emptyNode) this.el.prepend(this.emptyNode);
      }
      // Keep filling if the sentinel is still on screen (tall windows, small tiles).
      if (!this.done && this.sentinel.getBoundingClientRect().top < innerHeight + 1600) await this.loadMore();
    })();
    return this.loading;
  }

  #section(month) {
    let m = this.months.get(month);
    if (m) return m;
    const countEl = h("span.count", { text: this.counts.has(month) ? plural(this.counts.get(month), "item") : "" });
    const selectBtn = h("button.btn.select-month", { type: "button", text: "Select", dataset: { month } });
    const tiles = h("div.tiles");
    const section = h("section.month", { dataset: { month } },
      h("div.month-head", {}, h("h2", { text: formatMonth(month) }), countEl, selectBtn), tiles);
    this.el.insertBefore(section, this.sentinel);
    m = { section, tiles, countEl };
    this.months.set(month, m);
    return m;
  }

  #tile(item) {
    const label = `${item.kind === "video" ? "Video" : "Photo"} ${item.original_name}, ${item.taken_at.replace("T", " ")}`;
    return h("button.tile", { type: "button", dataset: { id: item.id }, "aria-label": label, "aria-pressed": "false" },
      h("img", { src: item.thumb_url, alt: "", loading: "lazy", decoding: "async" }),
      h("span.check", {}, icon("check")),
      item.favorite ? h("span.fav", {}, icon("heart")) : null,
      item.kind === "video" ? h("span.badge", {}, icon("play"), formatDuration(item.duration)) : null);
  }

  #append(items) {
    for (const item of items) {
      if (this.index.has(item.id)) continue;
      this.index.set(item.id, item);
      this.items.push(item);
      this.#section(item.taken_at.slice(0, 7)).tiles.append(this.#tile(item));
    }
  }

  tileFor(id) {
    return this.el.querySelector(`.tile[data-id="${id}"]`);
  }

  /** Re-render one tile after an edit (favorite toggled, date changed...). */
  update(item) {
    const old = this.index.get(item.id);
    if (!old) return;
    Object.assign(old, item);
    const tile = this.tileFor(item.id);
    if (!tile) return;
    const fresh = this.#tile(old);
    fresh.setAttribute("aria-pressed", String(this.selected.has(item.id)));
    tile.replaceWith(fresh);
  }

  /** Drop items that no longer belong to this view (trashed, unfavorited...). */
  remove(ids) {
    const gone = new Set(ids);
    this.items = this.items.filter((i) => !gone.has(i.id));
    for (const id of gone) {
      this.index.delete(id);
      this.selected.delete(id);
      const tile = this.tileFor(id);
      const section = tile?.closest(".month");
      tile?.remove();
      if (section && !section.querySelector(".tile")) {
        this.months.delete(section.dataset.month);
        section.remove();
      }
    }
    if (!this.items.length && this.done && this.emptyNode) this.el.prepend(this.emptyNode);
    this.#selectionChanged();
  }

  // ------------------------------------------------------------------ selection

  /** Put ``item`` where ``oldId`` was (an edit that replaced the original). */
  swap(oldId, item) {
    const idx = this.items.findIndex((i) => i.id === oldId);
    if (idx < 0) return false;
    this.items[idx] = item;
    this.index.delete(oldId);
    this.index.set(item.id, item);
    this.selected.delete(oldId);
    this.tileFor(oldId)?.replaceWith(this.#tile(item));
    return true;
  }

  /** Show ``item`` right after ``refId`` (an edit saved as a copy). */
  insertAfter(refId, item) {
    const idx = this.items.findIndex((i) => i.id === refId);
    if (idx < 0 || this.index.has(item.id)) return false;
    this.items.splice(idx + 1, 0, item);
    this.index.set(item.id, item);
    this.tileFor(refId)?.after(this.#tile(item));
    return true;
  }

  get selecting() {
    return this.selected.size > 0;
  }

  clearSelection() {
    for (const id of this.selected) this.tileFor(id)?.setAttribute("aria-pressed", "false");
    this.selected.clear();
    this.#selectionChanged();
  }

  #setSelected(id, on) {
    if (on) this.selected.add(id);
    else this.selected.delete(id);
    this.tileFor(id)?.setAttribute("aria-pressed", String(on));
  }

  #selectionChanged() {
    this.el.classList.toggle("selecting", this.selecting);
    this.onSelectionChange?.(this);
  }

  #onClick(e) {
    const monthBtn = e.target.closest(".select-month");
    if (monthBtn) {
      const ids = this.items.filter((i) => i.taken_at.startsWith(monthBtn.dataset.month)).map((i) => i.id);
      const allOn = ids.every((id) => this.selected.has(id));
      for (const id of ids) this.#setSelected(id, !allOn);
      this.#selectionChanged();
      return;
    }
    const tile = e.target.closest(".tile");
    if (!tile) return;
    const id = Number(tile.dataset.id);
    const idx = this.items.findIndex((i) => i.id === id);
    const toggle = this.selecting || e.ctrlKey || e.metaKey || e.target.closest(".check");
    if (e.shiftKey && this.anchor !== null) {
      const [a, b] = [Math.min(this.anchor, idx), Math.max(this.anchor, idx)];
      for (let i = a; i <= b; i++) this.#setSelected(this.items[i].id, true);
      this.#selectionChanged();
    } else if (toggle) {
      this.#setSelected(id, !this.selected.has(id));
      this.anchor = idx;
      this.#selectionChanged();
    } else {
      this.anchor = idx;
      this.onOpen?.(this, idx);
    }
  }

  destroy() {
    this.observer.disconnect();
  }
}
