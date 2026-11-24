// App shell: hash router, library/album/trash views, selection actions.
//
// Routes: #/library  #/favorites  #/albums  #/albums/<id>  #/trash  #/import  #/help
// Grid routes accept ?q=<text>&tag=<name>; the library also ?kind=image|video.
// The old #/photos and #/videos routes still work (they open the library).

import { api } from "./api.js";
import { exitApp } from "./exit.js";
import { MediaGrid } from "./grid.js";
import { filesFromDrop, renderImport, uploader } from "./importer.js";
import {
  button, confirmDialog, debounce, formatMonth, formatShortMonth, h, icon, plural, promptDialog,
  toast, toastError,
} from "./ui.js";
import { Viewer } from "./viewer.js";

const view = document.getElementById("view");
const searchInput = document.getElementById("search");
const selectionBar = document.getElementById("selectionbar");
const selectionCount = document.getElementById("selection-count");
const selectionActions = document.getElementById("selection-actions");

let current = { grid: null, ctx: null, disposers: [] };
// Aborted on Exit, which removes every global listener below at once.
const app = new AbortController();
const listen = (target, type, fn) => target.addEventListener(type, fn, { signal: app.signal });

// Items that are in an album are left out of the library unless this is on.
const SHOW_ALBUM_ITEMS_KEY = "phobos.showAlbumItems";
let showAlbumItems = false;
try { showAlbumItems = localStorage.getItem(SHOW_ALBUM_ITEMS_KEY) === "1"; } catch { /* storage unavailable */ }
// qs() drops boolean false, so the "not in any album" filter is sent as a string.
const albumScope = () => (showAlbumItems ? {} : { in_album: "false" });

// ------------------------------------------------------------------ routing

function parseRoute() {
  const [path, query = ""] = location.hash.replace(/^#/, "").split("?");
  const [name = "library", id] = path.split("/").filter(Boolean);
  return { name, id, params: new URLSearchParams(query) };
}

/** #/photos and #/videos were separate tabs; send old links and bookmarks to the library. */
function redirectLegacyRoute(route) {
  if (route.name !== "photos" && route.name !== "videos") return route;
  const params = new URLSearchParams(route.params);
  if (route.name === "videos") params.set("kind", "video");
  const q = params.toString();
  history.replaceState(null, "", `#/library${q ? `?${q}` : ""}`);
  return parseRoute();
}

function navigateWithParams(updates) {
  const r = parseRoute();
  const grids = ["library", "favorites", "trash", "albums"];
  let path = grids.includes(r.name) ? `/${r.name}${r.id ? `/${r.id}` : ""}` : "/library";
  if (r.name === "albums" && !r.id) path = "/library"; // searching from the album list searches the library
  const params = new URLSearchParams(r.params);
  for (const [k, v] of Object.entries(updates)) v ? params.set(k, v) : params.delete(k);
  const q = params.toString();
  history.replaceState(null, "", `#${path}${q ? `?${q}` : ""}`);
  render();
}

function dispose() {
  current.grid?.destroy();
  for (const fn of current.disposers) fn();
  current = { grid: null, ctx: null, disposers: [] };
  updateSelectionBar(null);
}

async function render() {
  dispose();
  const route = redirectLegacyRoute(parseRoute());
  searchInput.value = route.params.get("q") ?? "";
  for (const a of document.querySelectorAll("[data-nav]")) {
    const active = a.dataset.nav === route.name;
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  const q = route.params.get("q") || undefined;
  const tag = route.params.get("tag") || undefined;
  const searching = q || tag;
  try {
    switch (route.name) {
      case "favorites":
        return await libraryView({ title: "Favorites", filter: { favorite: true, q, tag }, ctx: "favorites",
          empty: emptyState("heart", searching ? "No matches." : "No favorites yet. Press F in the viewer or ♥ in the selection bar.") });
      case "trash":
        return await trashView({ q, tag });
      case "albums":
        return route.id ? await albumView(Number(route.id), { q, tag }) : await albumsView();
      case "import":
        return renderImport(view, { onDispose: (fn) => current.disposers.push(fn) });
      case "help":
        return await helpView();
      default: {
        const kind = ["image", "video"].includes(route.params.get("kind")) ? route.params.get("kind") : undefined;
        const nothing = kind === "video" ? emptyState("video", "No videos yet.")
          : kind === "image" ? emptyState("photos", "No photos yet.") : welcome();
        return await libraryView({ title: tag ? `Tag: ${tag}` : "Library", filter: { q, tag, kind, ...albumScope() }, ctx: "library",
          albumToggle: true, kindFilter: true, empty: searching ? emptyState("search", "No matches.") : nothing });
      }
    }
  } catch (err) {
    toastError(err);
  }
}

function emptyState(iconName, text) {
  return h("div.empty", {}, icon(iconName), h("p", { text }));
}

function welcome() {
  return h("div.empty", {}, icon("photos"),
    h("h2", { text: "Your library is empty" }),
    h("p", { text: "Drop photos and videos anywhere on this page, or import a folder." }),
    h("a.btn.primary", { href: "#/import" }, icon("import"), h("span", { text: "Import" })));
}

// ------------------------------------------------------------------ grid views

function makeGrid(filter, empty, ctx) {
  const grid = new MediaGrid({
    filter, empty,
    onOpen: (g, idx) => viewer.open(g, idx),
    onSelectionChange: (g) => updateSelectionBar(g),
  });
  current.grid = grid;
  current.ctx = ctx;
  return grid;
}

const searchMeta = (filter) => (filter.q ? `matching “${filter.q}”` : "");

async function libraryView({ title, filter, ctx, empty, head = [], extra = null, albumToggle = false, kindFilter = false }) {
  // Wrapped so the message can change once we know whether albums hold everything.
  const emptyBox = h("div", {}, empty);
  const grid = makeGrid(filter, emptyBox, ctx);
  const meta = h("span.meta");
  const banner = h("div.notice", { hidden: true });
  const rail = h("nav.rail", { "aria-label": "Jump to date" });
  const clear = filter.q || filter.tag ? button("Clear search", { onclick: () => navigateWithParams({ q: "", tag: "" }) }) : null;
  const toggle = albumToggle ? albumItemsToggle() : null;
  const page = h("div.page.library", {},
    h("div", {},
      h("div.page-head", {}, ...head, h("h1", { text: title }), meta,
        h("div.actions", {}, kindFilter ? kindSwitch(filter.kind) : null, toggle, clear)),
      extra, banner, grid.el),
    rail);
  view.replaceChildren(page);

  const hiding = albumToggle && !showAlbumItems;
  const [timeline, , inAlbums] = await Promise.all([
    api.timeline(filter), grid.reset(),
    hiding ? api.timeline({ ...filter, in_album: true }) : null,
  ]);
  const total = timeline.reduce((s, t) => s + t.count, 0);
  const hidden = inAlbums?.reduce((s, t) => s + t.count, 0) ?? 0;
  meta.textContent = [plural(total, "item"), searchMeta(filter)].filter(Boolean).join(" ")
    + (hidden ? ` · ${hidden.toLocaleString()} more in albums` : "");
  if (!total && hidden) {
    emptyBox.replaceChildren(emptyState("album", `Everything here is in an album. Turn on “Show items in albums” to see ${hidden === 1 ? "it" : "them"} here too.`));
  }
  grid.setCounts(timeline);
  buildRail(rail, timeline, async (month) => {
    banner.replaceChildren(h("span", { text: `Showing ${formatMonth(month)} and earlier.` }),
      button("Back to newest", { onclick: async () => { banner.hidden = true; await grid.reset(); scrollTo({ top: 0 }); } }));
    banner.hidden = false;
    await grid.reset(cursorBefore(month));
    scrollTo({ top: 0 });
  });
  return grid;
}

/** All / Photos / Videos. The choice lives in the URL (?kind=), so it survives reloads. */
function kindSwitch(kind) {
  const options = [["", "All"], ["image", "Photos"], ["video", "Videos"]];
  return h("fieldset.segmented", {},
    h("legend.visually-hidden", { text: "Show" }),
    options.map(([value, label]) => h("label", {},
      h("input", {
        type: "radio", name: "kind", value, checked: (kind ?? "") === value,
        onchange: () => navigateWithParams({ kind: value }),
      }),
      h("span", { text: label }))));
}

function albumItemsToggle() {
  const input = h("input", {
    type: "checkbox", role: "switch", checked: showAlbumItems,
    onchange: () => {
      showAlbumItems = input.checked;
      try { localStorage.setItem(SHOW_ALBUM_ITEMS_KEY, showAlbumItems ? "1" : "0"); } catch { /* ignore */ }
      render();
    },
  });
  return h("label.switch", { title: "Photos and videos that are in an album are hidden here unless this is on" },
    input, h("span", { text: "Show items in albums" }));
}

/** Cursor that starts listing at the end of ``month`` (YYYY-MM). */
function cursorBefore(month) {
  let [y, m] = month.split("-").map(Number);
  m += 1;
  if (m > 12) { m = 1; y += 1; }
  return `${y}-${String(m).padStart(2, "0")}-01T00:00:00|0`;
}

function buildRail(rail, timeline, onJump) {
  if (timeline.length < 2) { rail.replaceChildren(); return; }
  const years = new Map();
  for (const t of timeline) {
    const y = t.month.slice(0, 4);
    if (!years.has(y)) years.set(y, []);
    years.get(y).push(t);
  }
  const nodes = [];
  for (const [year, months] of years) {
    const monthBtns = months.map((t) => h("button.mon", {
      type: "button", text: formatShortMonth(t.month), title: `${formatMonth(t.month)} · ${plural(t.count, "item")}`,
      hidden: true, onclick: (e) => { markCurrent(e.currentTarget); onJump(t.month); },
    }));
    const yearBtn = h("button.year", {
      type: "button", text: year, "aria-expanded": "false",
      title: `${year} · ${plural(months.reduce((s, t) => s + t.count, 0), "item")}`,
      onclick: (e) => {
        const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
        e.currentTarget.setAttribute("aria-expanded", String(open));
        for (const b of monthBtns) b.hidden = !open;
        if (open) { markCurrent(e.currentTarget); onJump(months[0].month); }
      },
    });
    nodes.push(yearBtn, ...monthBtns);
  }
  const markCurrent = (btn) => {
    for (const b of rail.querySelectorAll("[aria-current]")) b.removeAttribute("aria-current");
    btn.setAttribute("aria-current", "true");
  };
  rail.replaceChildren(...nodes);
}

async function trashView(search) {
  const filter = { trash: true, ...search };
  const empty = button("Empty trash", {
    cls: "btn danger", icon: "trash",
    onclick: async () => {
      if (!(await confirmDialog({ title: "Empty trash?", message: "Everything in the trash will be permanently deleted from disk. This can't be undone.", confirm: "Delete forever", danger: true }))) return;
      try {
        const r = await api.del("/api/trash");
        toast(`Deleted ${plural(r.affected, "item")}`);
        render();
      } catch (err) { toastError(err); }
    },
  });
  await libraryView({
    title: "Trash", filter, ctx: "trash", empty: emptyState("trash", "Trash is empty."),
    extra: h("div.notice", {}, h("span", { text: "Items stay here until you delete them. Their files keep using disk space until then." }), empty),
  });
}

async function albumView(albumId, search) {
  const album = await api.get(`/api/albums/${albumId}`);
  const back = h("a.icon-btn", { href: "#/albums", "aria-label": "All albums", title: "All albums" }, icon("left"));
  const rename = button("Edit", {
    icon: "edit",
    onclick: async () => {
      const name = await promptDialog({ title: "Rename album", label: "Name", value: album.name });
      if (!name) return;
      try { await api.patch(`/api/albums/${albumId}`, { name }); render(); } catch (err) { toastError(err); }
    },
  });
  const describe = button("Description", {
    onclick: async () => {
      const description = await promptDialog({ title: "Album description", label: "Description", value: album.description });
      if (description === null) return;
      try { await api.patch(`/api/albums/${albumId}`, { description }); render(); } catch (err) { toastError(err); }
    },
  });
  const del = button("Delete album", {
    icon: "trash",
    onclick: async () => {
      if (!(await confirmDialog({ title: `Delete “${album.name}”?`, message: "The photos and videos stay in your library.", confirm: "Delete album", danger: true }))) return;
      try { await api.del(`/api/albums/${albumId}`); location.hash = "#/albums"; } catch (err) { toastError(err); }
    },
  });
  await libraryView({
    title: album.name, filter: { album: albumId, ...search }, ctx: "album",
    head: [back],
    empty: emptyState("album", "This album is empty. Select photos in the library and choose “Add to album”."),
    extra: h("div", {},
      album.description ? h("p.album-desc", { text: album.description }) : null,
      h("div.row", { style: "margin-bottom:14px" }, rename, describe, del)),
  });
  current.album = album;
}

async function albumsView() {
  const albums = await api.get("/api/albums");
  const create = button("New album", {
    cls: "btn primary", icon: "plus",
    onclick: async () => {
      const name = await promptDialog({ title: "New album", label: "Name", confirm: "Create" });
      if (!name) return;
      try {
        const a = await api.post("/api/albums", { name });
        location.hash = `#/albums/${a.id}`;
      } catch (err) { toastError(err); }
    },
  });
  const cards = albums.map((a) => h("a.album-card", { href: `#/albums/${a.id}` },
    h("div.cover", {}, a.cover_id ? h("img", { src: `/api/media/${a.cover_id}/thumb`, alt: "", loading: "lazy" }) : icon("album")),
    h("h3", { text: a.name }),
    h("p", { text: plural(a.count, "item") + (a.first_taken_at ? ` · ${yearRange(a)}` : "") })));
  view.replaceChildren(h("div.page", {},
    h("div.page-head", {}, h("h1", { text: "Albums" }), h("span.meta", { text: plural(albums.length, "album") }), h("div.actions", {}, create)),
    albums.length ? h("div.albums", {}, cards) : emptyState("album", "No albums yet.")));
}

function yearRange(a) {
  const y1 = a.first_taken_at.slice(0, 4), y2 = a.last_taken_at.slice(0, 4);
  return y1 === y2 ? y1 : `${y1}–${y2}`;
}

// ------------------------------------------------------------------ help

async function helpView() {
  const page = document.getElementById("help-template").content.cloneNode(true);
  page.querySelector('[data-help="url"]').textContent = location.origin;
  const lib = page.querySelector('[data-help="library"]');
  view.replaceChildren(page);
  try {
    const info = await api.get("/api/info");
    lib.textContent = info.library_dir_windows || info.library_dir;
    if (info.library_dir_windows) lib.title = info.library_dir;
  } catch (err) {
    lib.textContent = "~/Phobos (default)";
    toastError(err);
  }
}

// ------------------------------------------------------------------ selection actions

function updateSelectionBar(grid) {
  const n = grid?.selected.size ?? 0;
  selectionBar.hidden = n === 0;
  if (!n) return;
  selectionCount.textContent = `${n} selected`;
  const ids = () => [...grid.selected];
  const items = () => ids().map((id) => grid.index.get(id)).filter(Boolean);
  const run = async (fn) => { try { await fn(); } catch (err) { toastError(err); } };
  const actions = [];

  if (current.ctx === "trash") {
    actions.push(
      button("Restore", { icon: "restore", onclick: () => run(async () => {
        const sel = ids();
        await api.action(sel, "restore");
        grid.remove(sel);
        toast(`Restored ${plural(sel.length, "item")}`);
      }) }),
      button("Delete forever", { cls: "btn danger", icon: "trash", onclick: () => run(async () => {
        const sel = ids();
        if (!(await confirmDialog({ title: `Permanently delete ${plural(sel.length, "item")}?`, message: "The files are removed from disk. This can't be undone.", confirm: "Delete forever", danger: true }))) return;
        await api.action(sel, "purge");
        grid.remove(sel);
      }) }),
    );
  } else {
    const allFav = items().every((i) => i.favorite);
    actions.push(
      button(allFav ? "Unfavorite" : "Favorite", { icon: "heart", onclick: () => run(async () => {
        const sel = ids();
        await api.action(sel, allFav ? "unfavorite" : "favorite");
        if (allFav && current.ctx === "favorites") grid.remove(sel);
        else for (const it of items()) grid.update({ ...it, favorite: !allFav });
        toast(`${allFav ? "Removed from" : "Added to"} favorites: ${plural(sel.length, "item")}`);
        grid.clearSelection();
      }) }),
      button("Add to album", { icon: "album", onclick: () => run(async () => {
        const album = await pickAlbum();
        if (!album) return;
        const sel = ids();
        const r = await api.action(sel, "add_to_album", { album_id: album.id });
        const msg = r.affected ? `Added ${plural(r.affected, "item")} to “${album.name}”` : `Already in “${album.name}”`;
        toast(msg, { action: "Open", onAction: () => { location.hash = `#/albums/${album.id}`; } });
        // The library only lists items outside albums (unless the toggle is on).
        if (current.ctx === "library" && !showAlbumItems) grid.remove(sel);
        else grid.clearSelection();
      }) }),
      button("Tag", { icon: "tag", onclick: () => run(async () => {
        const tag = await promptDialog({ title: "Add a tag", label: "Tag", confirm: "Add" });
        if (!tag) return;
        await api.action(ids(), "add_tag", { tag });
        toast(`Tagged ${plural(grid.selected.size, "item")} “${tag}”`);
        grid.clearSelection();
      }) }),
    );
    if (current.ctx === "album" && current.album) {
      const album = current.album;
      if (n === 1) {
        actions.push(button("Use as cover", { icon: "image", onclick: () => run(async () => {
          await api.patch(`/api/albums/${album.id}`, { cover_media_id: ids()[0] });
          toast("Album cover updated");
          grid.clearSelection();
        }) }));
      }
      actions.push(button("Remove from album", { icon: "close", onclick: () => run(async () => {
        const sel = ids();
        await api.action(sel, "remove_from_album", { album_id: album.id });
        grid.remove(sel);
      }) }));
    }
    actions.push(button("Trash", { icon: "trash", onclick: () => run(async () => {
      const sel = ids();
      await api.action(sel, "trash");
      grid.remove(sel);
      toast(`Moved ${plural(sel.length, "item")} to trash`, { action: "Undo", onAction: () => restore(sel) });
    }) }));
  }
  selectionActions.replaceChildren(...actions);
}

async function restore(ids) {
  try {
    await api.action(ids, "restore");
    render();
  } catch (err) { toastError(err); }
}

listen(document.querySelector('[data-sel="clear"]'), "click", () => current.grid?.clearSelection());

// ------------------------------------------------------------------ album picker

/** @param {{contains?: number[]}} opts  albums the item is already in (marked, not hidden) */
async function pickAlbum({ contains = [] } = {}) {
  const d = document.getElementById("album-picker");
  const list = d.querySelector("#album-choices");
  const nameInput = d.querySelector("#album-new-name");
  const albums = await api.get("/api/albums");
  const already = new Set(contains);
  nameInput.value = "";
  list.replaceChildren(...albums.map((a) => h("button", { type: "submit", value: `a:${a.id}` },
    a.cover_id ? h("img", { src: `/api/media/${a.cover_id}/thumb`, alt: "" }) : h("span.ph"),
    h("span", {}, h("strong", { text: a.name }),
      h("small", { text: plural(a.count, "item") + (already.has(a.id) ? " · already in here" : "") })),
    already.has(a.id) ? icon("check") : null)));
  if (!albums.length) list.append(h("p", { text: "No albums yet - create one below." }));
  const result = new Promise((resolve) => d.addEventListener("close", () => {
    const v = d.returnValue;
    d.returnValue = "";
    resolve(v);
  }, { once: true }));
  d.showModal();
  (albums.length ? list.querySelector("button") : nameInput).focus();
  const value = await result;
  if (value.startsWith("a:")) return albums.find((a) => a.id === Number(value.slice(2)));
  const name = nameInput.value.trim();
  if (value !== "new" || !name) return null;
  // Typing the name of an existing album adds to it instead of creating a twin.
  return albums.find((a) => a.name.toLowerCase() === name.toLowerCase()) ?? api.post("/api/albums", { name });
}

// ------------------------------------------------------------------ viewer

const viewer = new Viewer({
  pickAlbum,
  onChange: (item, grid) => {
    if (!grid) return;
    if (grid === current.grid && current.ctx === "favorites" && !item.favorite) return; // keep it while viewing; gone on next load
    grid.update(item);
  },
  onRemove: (ids, grid) => grid?.remove(ids),
  onRestore: (ids) => restore(ids),
});

// ------------------------------------------------------------------ global wiring

const ZOOM_KEY = "phobos.zoom";
const zoom = document.getElementById("zoom");
const applyZoom = (v) => document.documentElement.style.setProperty("--tile", `${v}px`);
try { zoom.value = localStorage.getItem(ZOOM_KEY) ?? zoom.value; } catch { /* storage unavailable */ }
applyZoom(zoom.value);
listen(zoom, "input", () => {
  applyZoom(zoom.value);
  try { localStorage.setItem(ZOOM_KEY, zoom.value); } catch { /* ignore */ }
});

const onSearch = debounce(() => navigateWithParams({ q: searchInput.value.trim() }), 300);
listen(searchInput, "input", onSearch);
listen(document.getElementById("search-form"), "submit", (e) => {
  e.preventDefault();
  navigateWithParams({ q: searchInput.value.trim() });
});

listen(document, "keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;
  const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
  if (e.key === "Escape" && current.grid?.selecting) current.grid.clearSelection();
  else if (e.key === "/" && !typing) { e.preventDefault(); searchInput.focus(); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "a" && !typing && current.grid) {
    e.preventDefault();
    for (const it of current.grid.items) current.grid.selected.add(it.id);
    for (const t of current.grid.el.querySelectorAll(".tile")) t.setAttribute("aria-pressed", "true");
    current.grid.el.classList.add("selecting");
    updateSelectionBar(current.grid);
  }
});

// Drop files anywhere to import them.
const overlay = document.getElementById("drop-overlay");
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer?.types.includes("Files");
listen(window, "dragenter", (e) => { if (hasFiles(e)) { dragDepth++; overlay.hidden = false; } });
listen(window, "dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; overlay.hidden = true; } });
listen(window, "dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
listen(window, "drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  overlay.hidden = true;
  const files = await filesFromDrop(e.dataTransfer);
  uploader.add(files);
  if (parseRoute().name !== "import") location.hash = "#/import";
});

listen(window, "phobos:library-changed", () => {
  if (["library", "photos", "videos", "favorites"].includes(parseRoute().name) && !current.grid?.selecting) render();
});
listen(window, "hashchange", render);
// Other modules (e.g. the duplicates list) open items in the viewer with a grid-like list.
listen(window, "phobos:view", (e) => viewer.open(e.detail.grid, e.detail.index));

listen(document.getElementById("exit-btn"), "click", () => exitApp(() => {
  dispose(); // stops the current view's timers (e.g. import progress polling)
  app.abort();
}));
render();
