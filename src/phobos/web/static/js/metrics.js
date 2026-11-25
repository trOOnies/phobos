// Metrics tab: library overview (tiles, items per year, file types, cameras, date
// sources) and the cleanup tool (black borders to crop, blank/broken shots, empty,
// small and low-resolution files, short videos, the largest files).

import { api, qs } from "./api.js";
import {
  button, confirmDialog, formatBytes, formatDuration, formatLongDate, h, icon, plural, toast, toastError,
} from "./ui.js";

const SMALL_KEY = "phobos.cleanup.smallKb";
const SMALL_OPTIONS = [20, 50, 100, 500];
const LIST_LIMIT = 200;
const DATE_SOURCES = {
  exif: "Camera (EXIF)", video: "Video file", filename: "File name",
  file: "Modified time", manual: "Set by you",
};

export async function renderMetrics(view, { onDispose }) {
  let alive = true;
  onDispose(() => { alive = false; });

  const overview = h("div.metrics-overview", {}, h("p.loading", { text: "Loading…" }));
  const cleanup = new Cleanup(() => alive, () => loadOverview());
  view.replaceChildren(h("div.page.metrics", {},
    h("div.page-head", {}, h("h1", { text: "Metrics" }), h("span.meta", { text: "Your library at a glance" })),
    overview,
    cleanup.el));

  async function loadOverview() {
    try {
      const m = await api.get("/api/metrics");
      if (alive) overview.replaceChildren(...overviewNodes(m));
    } catch (err) { toastError(err); }
  }
  await Promise.all([loadOverview(), cleanup.start()]);
}

// ------------------------------------------------------------------ overview

function overviewNodes(m) {
  const t = m.totals;
  const items = t.photos + t.videos;
  const bytes = t.photo_bytes + t.video_bytes;
  const pct = (n) => (items ? `${Math.round((n / items) * 100)}%` : "–");
  const tiles = [
    tile("Photos", t.photos.toLocaleString(), formatBytes(t.photo_bytes)),
    tile("Videos", t.videos.toLocaleString(), formatBytes(t.video_bytes)),
    tile("Library size", formatBytes(bytes), m.disk ? `${formatBytes(m.disk.free)} free on this drive` : ""),
    tile("In albums", pct(t.in_albums), `${plural(t.in_albums, "item")} in ${plural(t.albums, "album")}`),
    tile("Favorites", t.favorites.toLocaleString(), pct(t.favorites) + " of the library"),
    tile("Tagged", t.tagged.toLocaleString(), plural(t.tags, "different tag")),
    tile("With a place", t.with_place.toLocaleString(), `${pct(t.with_place)} · ${t.with_location.toLocaleString()} with GPS`),
    tile("People & pets", t.people.toLocaleString(), `in ${plural(t.with_people, "item")}`, "#/people"),
    tile("Trash", t.trash.toLocaleString(), t.trash ? `${formatBytes(t.trash_bytes)} to free up` : "Empty", "#/trash"),
  ];
  const span = t.oldest ? `${formatLongDate(t.oldest)} – ${formatLongDate(t.newest)}` : "";
  return [
    h("div.tiles-row", {}, tiles),
    h("div.metrics-grid", {},
      h("section.card.wide", {},
        h("h2", { text: "Items per year" }),
        span ? h("p.chart-sub", { text: `Photos and videos by the year they were taken · ${span}` }) : null,
        yearChart(m.by_year)),
      h("section.card", {}, h("h2", { text: "File types" }),
        hbars(m.by_type.map((r) => ({ label: typeLabel(r.mime), value: r.count, note: formatBytes(r.bytes) })))),
      h("section.card", {}, h("h2", { text: "Cameras" }),
        m.cameras.length
          ? hbars(m.cameras.map((r) => ({ label: r.camera, value: r.count })))
          : h("p.muted", { text: "No camera information in your files." })),
      h("section.card", {}, h("h2", { text: "Top countries" }),
        m.countries.length
          ? hbars(m.countries.map((r) => ({ label: r.country, value: r.count })))
          : h("p.muted", { text: "No places yet. Photos with GPS get one automatically; you can also set it in the viewer." })),
      h("section.card", {}, h("h2", { text: "Where the dates come from" }),
        hbars(m.date_sources.map((r) => ({ label: DATE_SOURCES[r.source] ?? r.source, value: r.count }))),
        h("p.card-note", { text: "Dates from the file name or modified time are guesses. Fix them in the viewer's details." }))),
  ];
}

function tile(label, value, sub, href) {
  return h(href ? "a.stat" : "div.stat", { href },
    h("span.stat-label", { text: label }), h("strong.stat-value", { text: value }),
    sub ? h("span.stat-sub", { text: sub }) : null);
}

function typeLabel(mime) {
  const sub = mime.split("/")[1] ?? mime;
  const names = { jpeg: "JPEG", png: "PNG", heic: "HEIC", heif: "HEIF", webp: "WebP", gif: "GIF", tiff: "TIFF",
    avif: "AVIF", bmp: "BMP", mp4: "MP4 video", quicktime: "MOV video", webm: "WebM video",
    "x-matroska": "MKV video", "x-msvideo": "AVI video", "3gpp": "3GP video", "mp2t": "MTS video" };
  return names[sub] ?? sub.toUpperCase();
}

/** Single-series horizontal bars; the value sits at the bar's end in text ink. */
function hbars(rows) {
  if (!rows.length) return h("p.muted", { text: "Nothing yet." });
  const max = Math.max(...rows.map((r) => r.value));
  return h("ul.hbars", {}, rows.map((r) => h("li", {},
    h("span.hbar-label", { text: r.label, title: r.label }),
    h("span.hbar-track", {}, h("span.hbar-fill", { style: `width:${Math.max(1, (r.value / max) * 100)}%` })),
    h("span.hbar-value", { text: r.value.toLocaleString() + (r.note ? ` · ${r.note}` : "") }))));
}

function niceMax(v) {
  if (v <= 4) return 4;
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * mag >= v) return m * mag;
  return 10 * mag;
}

/** Items per year: one series (columns), hover/focus tooltip, table view. */
function yearChart(rows) {
  if (!rows.length) return h("p.muted", { text: "Nothing imported yet." });
  const totals = rows.map((r) => r.photos + r.videos);
  const top = niceMax(Math.max(...totals));
  const peak = totals.indexOf(Math.max(...totals));
  const every = Math.ceil(rows.length / 14); // thin out year labels on long timelines
  const tip = h("div.chart-tip", { hidden: true });
  const plot = h("div.chart-plot", {},
    [0, 0.5, 1].map((f) => h("div.gridline", { style: `bottom:${f * 100}%` },
      h("span", { text: (top * f).toLocaleString() }))));
  const cols = h("div.chart-cols");
  const labels = h("div.chart-labels");
  rows.forEach((r, i) => {
    const total = totals[i];
    const text = `${r.year}: ${plural(total, "item")} (${plural(r.photos, "photo")}, ${plural(r.videos, "video")}) · ${formatBytes(r.bytes)}`;
    const col = h("div.chart-col", { tabindex: 0, "aria-label": text },
      h("span.chart-bar", { style: `height:${(total / top) * 100}%` },
        i === peak ? h("span.chart-peak", { text: total.toLocaleString() }) : null));
    const show = () => {
      tip.replaceChildren(h("strong", { text: r.year }),
        h("span", { text: `${plural(r.photos, "photo")} · ${plural(r.videos, "video")}` }),
        h("span", { text: formatBytes(r.bytes) }));
      tip.hidden = false;
      const x = col.offsetLeft + col.offsetWidth / 2;
      tip.style.left = `${x}px`;
      col.classList.add("on");
    };
    const hide = () => { tip.hidden = true; col.classList.remove("on"); };
    col.addEventListener("pointerenter", show);
    col.addEventListener("pointerleave", hide);
    col.addEventListener("focus", show);
    col.addEventListener("blur", hide);
    cols.append(col);
    labels.append(h("span", { text: i % every === 0 || i === rows.length - 1 ? r.year : "" }));
  });
  plot.append(cols, tip);
  const table = h("details.chart-table", {}, h("summary", { text: "Show as a table" }),
    h("table", {},
      h("thead", {}, h("tr", {}, ["Year", "Photos", "Videos", "Size"].map((c) => h("th", { scope: "col", text: c })))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        h("th", { scope: "row", text: r.year }), h("td", { text: r.photos.toLocaleString() }),
        h("td", { text: r.videos.toLocaleString() }), h("td", { text: formatBytes(r.bytes) }))))));
  return h("div.chart", {}, plot, labels, table);
}

// ------------------------------------------------------------------ cleanup

class Cleanup {
  constructor(isAlive, onChanged) {
    this.isAlive = isAlive;
    this.onChanged = onChanged; // something was trashed or cropped: refresh the overview
    this.summary = null;
    this.category = null;
    this.selected = new Set();
    this.list = null;
    this.analysing = false;
    this.paused = false;
    try { this.smallKb = Number(localStorage.getItem(SMALL_KEY)) || 50; } catch { this.smallKb = 50; }

    this.progress = h("div.analysis", { hidden: true });
    this.chips = h("div.cleanup-chips", { role: "group", "aria-label": "What to look for" });
    this.panel = h("div.cleanup-panel");
    const small = h("select", { "aria-label": "Very small means under" },
      SMALL_OPTIONS.map((kb) => h("option", { value: kb, text: `${kb} KB`, selected: kb === this.smallKb })));
    small.addEventListener("change", () => {
      this.smallKb = Number(small.value);
      try { localStorage.setItem(SMALL_KEY, String(this.smallKb)); } catch { /* ignore */ }
      this.refresh(true);
    });
    this.el = h("section.card.cleanup", { "aria-labelledby": "cleanup-title" },
      h("div.cleanup-head", {},
        h("div", {},
          h("h2", { id: "cleanup-title", text: "Clean up" }),
          h("p", { text: "Things you might not want to keep. Nothing is deleted from here: whatever you remove goes to the Trash first." })),
        h("label.cleanup-option", {}, h("span", { text: "“Very small” means under" }), small)),
      this.progress, this.chips, this.panel);
  }

  async start() {
    await this.refresh();
    if (this.summary?.analysis.pending) this.analyse();
  }

  async refresh(reloadList = false) {
    try {
      this.summary = await api.get(`/api/cleanup${qs({ small_kb: this.smallKb })}`);
    } catch (err) { toastError(err); return; }
    if (!this.isAlive()) return;
    const cats = this.summary.categories;
    // First visit: open the first category with something in it; afterwards keep the user's pick.
    if (!cats.some((c) => c.id === this.category)) this.category = (cats.find((c) => c.count) ?? cats[0]).id;
    this.#drawChips();
    this.#drawProgress();
    if (reloadList || !this.list || this.list.category !== this.category) await this.#loadList();
  }

  // ---------------------------------------------------------------- analysis

  async analyse() {
    if (this.analysing) return;
    this.analysing = true;
    this.paused = false;
    this.#drawProgress();
    let rounds = 0;
    try {
      while (this.isAlive() && !this.paused) {
        const r = await api.post("/api/analysis/run");
        this.summary.analysis.pending = r.remaining;
        this.#drawProgress();
        if (!r.remaining) break;
        if (++rounds % 4 === 0) await this.refresh(); // counts fill in as we go
      }
    } catch (err) {
      toastError(err);
    } finally {
      this.analysing = false;
    }
    if (this.isAlive()) await this.refresh(true);
  }

  #drawProgress() {
    const a = this.summary?.analysis;
    if (!a || (!a.pending && !this.analysing)) { this.progress.hidden = true; return; }
    const done = a.total - a.pending;
    const pct = a.total ? Math.round((done / a.total) * 100) : 100;
    const toggle = this.analysing
      ? button("Pause", { cls: "btn small", onclick: () => { this.paused = true; } })
      : button("Continue", { cls: "btn small", onclick: () => this.analyse() });
    this.progress.replaceChildren(
      h("div.row", {},
        h("span", { text: this.analysing
          ? `Looking at your photos for black borders, blank and broken shots… ${done.toLocaleString()} of ${a.total.toLocaleString()}`
          : `${plural(a.pending, "photo")} not checked yet for black borders, blank and broken shots.` }),
        toggle),
      h("div.progress", { role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Checking photos" },
        h("div", { style: `width:${pct}%` })));
    this.progress.hidden = false;
  }

  // ---------------------------------------------------------------- categories

  #drawChips() {
    this.chips.replaceChildren(...this.summary.categories.map((c) => h("button.chip-btn", {
      type: "button", "aria-pressed": String(c.id === this.category), title: c.description,
      onclick: () => { this.category = c.id; this.#drawChips(); this.#loadList(); },
    }, h("span", { text: c.label }), h("span.count", { text: c.count.toLocaleString() }))));
  }

  async #loadList() {
    const cat = this.summary.categories.find((c) => c.id === this.category);
    let page;
    try {
      page = await api.get(`/api/cleanup/${cat.id}${qs({ small_kb: this.smallKb, limit: LIST_LIMIT })}`);
    } catch (err) { toastError(err); return; }
    if (!this.isAlive() || this.category !== cat.id) return;
    this.selected.clear();
    this.list = new CleanupList(cat.id, page.items, (ids) => this.#removed(ids));
    this.#drawPanel(cat, page.total);
  }

  #drawPanel(cat, total) {
    const list = this.list;
    const croppable = cat.id === "croppable";
    const selectAll = h("input", { type: "checkbox", "aria-label": "Select all" });
    const trashSel = button("Move to trash", { cls: "btn small", icon: "trash", disabled: true, onclick: () => this.#trash([...this.selected]) });
    const cropSel = croppable ? button("Crop", { cls: "btn small primary", icon: "crop", disabled: true, onclick: () => this.#crop([...this.selected]) }) : null;
    const sync = () => {
      const n = this.selected.size;
      trashSel.disabled = !n;
      trashSel.querySelector("span").textContent = n ? `Move ${n} to trash` : "Move to trash";
      if (cropSel) {
        cropSel.disabled = !n;
        cropSel.querySelector("span").textContent = n ? `Crop ${n}` : "Crop";
      }
      selectAll.checked = n > 0 && n === list.items.length;
      selectAll.indeterminate = n > 0 && n < list.items.length;
    };
    selectAll.addEventListener("change", () => {
      for (const it of list.items) (selectAll.checked ? this.selected.add(it.id) : this.selected.delete(it.id));
      for (const box of list.el.querySelectorAll("input[type=checkbox]")) box.checked = selectAll.checked;
      sync();
    });
    list.onToggle = (id, on) => { (on ? this.selected.add(id) : this.selected.delete(id)); sync(); };
    list.sync = sync;
    list.render({ onCrop: croppable ? (id) => this.#crop([id]) : null, onTrash: (id) => this.#trash([id]) });

    const empty = !list.items.length;
    this.panel.replaceChildren(
      h("p.cleanup-desc", {}, h("strong", { text: cat.label }), " – ", cat.description),
      empty
        ? h("div.empty.small", {}, icon("check"), h("p", { text: emptyText(cat.id, this.summary.analysis.pending) }))
        : h("div", {},
          h("div.cleanup-toolbar", {},
            h("label.row", {}, selectAll, h("span", { text: total > list.items.length ? `Showing ${list.items.length} of ${total.toLocaleString()}` : plural(total, "item") })),
            h("div.row", {}, cropSel, trashSel)),
          list.el));
  }

  // ---------------------------------------------------------------- actions

  async #trash(ids) {
    if (!ids.length) return;
    try {
      await api.action(ids, "trash");
      this.list.remove(ids);
      toast(`Moved ${plural(ids.length, "item")} to the trash`, {
        action: "Undo",
        onAction: async () => {
          try { await api.action(ids, "restore"); await this.refresh(true); this.onChanged(); } catch (err) { toastError(err); }
        },
      });
    } catch (err) { toastError(err); }
  }

  async #crop(ids) {
    const items = ids.map((id) => this.list.items.find((i) => i.id === id)).filter((i) => i?.analysis?.crop);
    if (!items.length) return;
    if (items.length > 1 && !(await confirmDialog({
      title: `Crop ${plural(items.length, "photo")}?`,
      message: "The black borders are cut off. The originals go to the Trash, so you can restore them if a crop isn't right.",
      confirm: "Crop",
    }))) return;
    const done = [];
    for (const item of items) {
      try {
        const { left, top, right, bottom } = item.analysis.crop;
        await api.post(`/api/media/${item.id}/edit`, { crop: { left, top, right, bottom }, mode: "replace" });
        done.push(item.id);
      } catch (err) {
        toastError(`${item.original_name}: ${err.message}`);
      }
    }
    if (done.length) {
      this.list.remove(done);
      toast(`Cropped ${plural(done.length, "photo")}. The originals are in the Trash.`, {
        action: "Trash", onAction: () => { location.hash = "#/trash"; }, timeout: 10000,
      });
    }
  }

  async #removed(ids) {
    for (const id of ids) this.selected.delete(id);
    this.list.sync?.();
    this.onChanged();
    await this.refresh(!this.list.items.length);
  }
}

function emptyText(category, pending) {
  if (pending && ["croppable", "blank", "broken"].includes(category)) {
    return "Nothing found so far. Some photos haven't been checked yet.";
  }
  return "Nothing here. All good!";
}

function reason(category, item, a) {
  switch (category) {
    case "croppable": {
      const { left, top, right, bottom } = a.crop;
      const cut = Math.round((1 - (right - left) * (bottom - top)) * 100);
      return `Black border: ${cut}% of the picture`;
    }
    case "blank": return a.mean < 60 ? "Almost completely dark" : a.mean > 200 ? "Almost completely white" : "Almost a single colour";
    case "broken": return a.error ? `Can't be opened: ${a.error}` : "Can't be opened";
    case "empty": return "0 bytes";
    case "lowres": return `${item.width} × ${item.height} (${((item.width * item.height) / 1e6).toFixed(2)} MP)`;
    case "short": return `${(item.duration ?? 0).toFixed(1)} seconds long`;
    default: return formatBytes(item.size);
  }
}

/** The rows of one category; also stands in for a grid when opened in the viewer. */
class CleanupList {
  constructor(category, rows, onRemoved) {
    this.category = category;
    this.items = rows.map((r) => ({ ...r.media, analysis: r.analysis }));
    this.onRemoved = onRemoved;
    this.done = true;
    this.el = h("ul.cleanup-list");
    this.rows = new Map();
  }

  render({ onCrop, onTrash }) {
    this.el.replaceChildren(...this.items.map((item) => {
      const open = () => {
        const index = this.items.findIndex((x) => x.id === item.id);
        if (index >= 0) dispatchEvent(new CustomEvent("phobos:view", { detail: { grid: this, index } }));
      };
      const check = h("input", { type: "checkbox", "aria-label": `Select ${item.original_name}`,
        onchange: (e) => this.onToggle?.(item.id, e.currentTarget.checked) });
      const meta = [formatLongDate(item.taken_at), formatBytes(item.size)];
      if (item.kind === "video" && item.duration) meta.push(formatDuration(item.duration));
      const li = h("li.cleanup-row", {},
        check,
        h("button.cleanup-thumb", { type: "button", "aria-label": `Open ${item.original_name}`, onclick: open },
          h("img", { src: item.thumb_url, alt: "", loading: "lazy" }),
          item.kind === "video" ? h("span.badge", {}, icon("play")) : null),
        h("div.cleanup-info", {},
          h("strong", { text: item.original_name }),
          h("span.cleanup-reason", { text: reason(this.category, item, item.analysis) }),
          h("span.muted", { text: meta.join(" · ") })),
        h("div.row.nowrap", {},
          onCrop ? button("Crop", { cls: "btn small", icon: "crop", onclick: () => onCrop(item.id), title: "Cut off the black border (the original goes to the Trash)" }) : null,
          button("Trash", { cls: "btn small", icon: "trash", onclick: () => onTrash(item.id), title: "Move to the trash" })));
      this.rows.set(item.id, li);
      return li;
    }));
  }

  // grid interface used by the viewer
  tileFor() { return null; }
  loadMore() { return Promise.resolve(); }
  update(item) {
    const i = this.items.findIndex((x) => x.id === item.id);
    if (i >= 0) this.items[i] = { ...this.items[i], ...item };
  }
  remove(ids) {
    const gone = new Set(ids);
    this.items = this.items.filter((i) => !gone.has(i.id));
    for (const id of gone) { this.rows.get(id)?.remove(); this.rows.delete(id); }
    this.onRemoved(ids);
  }
}
