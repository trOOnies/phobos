// Import page: browser uploads (drag & drop, file/folder pickers) and server-side
// folder imports (paths like C:\Users\me\Pictures or /mnt/c/... ).

import { api, qs } from "./api.js";
import { displayPath, showDuplicates } from "./duplicates.js";
import { pickFolder } from "./folderpicker.js";
import { button, formatBytes, formatDateTime, h, icon, plural, toast, toastError } from "./ui.js";

const MEDIA_EXT = new Set([
  "jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif", "avif",
  "mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "mts", "m2ts", "wmv",
]);
const CONCURRENCY = 3;

const isMedia = (file) => MEDIA_EXT.has(file.name.split(".").pop().toLowerCase());

// Dropped folders: File -> "Trip/IMG_1.jpg". Browsers never reveal the full path on disk.
const sourcePaths = new WeakMap();
const sourceOf = (file) => sourcePaths.get(file) ?? file.name;

/** History label for a batch: the dropped folder names, or how many loose files. */
function describeBatch(files) {
  const folders = new Set();
  let loose = 0;
  for (const f of files) {
    const path = sourcePaths.get(f);
    if (path?.includes("/")) folders.add(path.split("/")[0]);
    else loose++;
  }
  const parts = [];
  if (folders.size) parts.push(`${folders.size === 1 ? "Folder" : "Folders"} ${[...folders].map((n) => `“${n}”`).join(", ")}`);
  if (loose) parts.push(`${plural(loose, "file")}`);
  return `${parts.join(" + ")} (from the browser)`;
}

// ------------------------------------------------------------------ uploader (survives navigation)

class Uploader {
  queue = [];
  active = 0;
  stats = null;
  session = null; // Promise of the import-history entry this batch reports into
  entry = null; // that entry once known ({id, path, ...})
  listeners = new Set();

  add(files) {
    const media = [...files].filter(isMedia);
    const ignored = files.length - media.length;
    if (ignored) toast(`Ignoring ${plural(ignored, "file")} that aren't photos or videos`);
    if (!media.length) return;
    if (!this.stats || (!this.active && !this.queue.length)) {
      this.stats = { total: 0, done: 0, imported: 0, duplicate: 0, skipped: 0, error: 0, bytes: 0, sent: 0, errors: [] };
      this.entry = null;
      // Uploads still work if the history entry can't be created.
      this.session = api.post("/api/uploads", { label: describeBatch(media) })
        .then((entry) => { this.entry = entry; return entry; }, () => null);
    }
    this.stats.total += media.length;
    this.stats.bytes += media.reduce((s, f) => s + f.size, 0);
    this.queue.push(...media);
    this.#pump();
    this.#emit();
  }

  #pump() {
    while (this.active < CONCURRENCY && this.queue.length) {
      const file = this.queue.shift();
      this.active++;
      let last = 0;
      const onProgress = (p) => {
        this.stats.sent += (p - last) * file.size;
        last = p;
        this.#emit();
      };
      this.session
        .then((entry) => api.upload(file, onProgress, { import_id: entry?.id, source: sourceOf(file) }))
        .then((r) => {
          this.stats[r.status] = (this.stats[r.status] ?? 0) + 1;
          if (r.status === "error") this.stats.errors.push(`${r.name}: ${r.message}`);
        })
        .catch((err) => {
          this.stats.error++;
          this.stats.errors.push(`${file.name}: ${err.message}`);
        })
        .finally(() => {
          this.stats.sent += (1 - last) * file.size;
          this.stats.done++;
          this.active--;
          this.#pump();
          this.#emit();
          if (!this.active && !this.queue.length) this.#finished();
        });
    }
  }

  async #finished() {
    const s = this.stats;
    const entry = await this.session;
    if (entry) {
      try { this.entry = await api.post(`/api/uploads/${entry.id}/finish`); } catch { /* history only */ }
      this.#emit();
    }
    dispatchEvent(new CustomEvent("phobos:imports-changed"));
    const parts = [`${plural(s.imported, "new item")}`];
    if (s.duplicate) parts.push(`${s.duplicate} already in library`);
    if (s.error) parts.push(`${s.error} failed`);
    let action = {};
    if (s.imported) action = { action: "View", onAction: () => { location.hash = "#/library"; } };
    else if (s.duplicate && this.entry) {
      const entry = this.entry;
      action = { action: "Show duplicates", onAction: () => showDuplicates(entry) };
    }
    toast(`Upload finished: ${parts.join(", ")}`, action);
    dispatchEvent(new CustomEvent("phobos:library-changed"));
  }

  #emit() {
    for (const fn of this.listeners) fn(this.stats);
  }
}

export const uploader = new Uploader();

/** Collect files from a drop, descending into dropped folders. */
export async function filesFromDrop(dataTransfer) {
  const entries = [...dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      sourcePaths.set(file, entry.fullPath.replace(/^\//, ""));
      out.push(file);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      // readEntries returns results in batches until it yields an empty array.
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const child of batch) await walk(child);
      }
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

// ------------------------------------------------------------------ page

export function renderImport(container, { onDispose }) {
  const page = h("div.page");
  container.replaceChildren(page);
  page.append(h("div.page-head", {}, h("h1", { text: "Import" })));

  const infoCard = h("section.card", {}, h("h2", { text: "Library" }), h("p", { text: "Loading…" }));
  const historyList = h("ul.history", {}, h("li.history-empty", { text: "Loading…" }));
  const historyCard = h("section.card.history-card", {},
    h("h2", { text: "Import history" }),
    h("p", { text: "Every folder import, browser upload and rescan, with the folder it came from. Kept across restarts." }),
    historyList);
  const grid = h("div.import-grid", {}, uploadCard(), folderCard(), infoCard, historyCard);
  page.append(grid);

  const jobsBox = grid.querySelector("#jobs");
  let timer = null;
  let jobs = [];
  let knownActive = new Set();
  const loadHistory = async () => {
    try {
      renderHistory(historyList, await api.get(`/api/imports${qs({ limit: 100 })}`), jobs);
    } catch (err) {
      toastError(err);
    }
  };
  const poll = async () => {
    try {
      jobs = await api.get("/api/jobs");
      renderJobs(jobsBox, jobs.filter((j) => !j.finished_at));
      const active = new Set(jobs.filter((j) => !j.finished_at).map((j) => j.id));
      let finished = false;
      for (const id of knownActive) {
        const j = jobs.find((x) => x.id === id);
        if (j && j.finished_at) {
          finished = true;
          toast(`${j.kind === "rescan" ? "Rescan" : "Import"} ${j.status}: ${plural(j.imported, "new item")}, ${j.duplicates} duplicates, ${j.failed} errors`,
            j.duplicates ? { action: "Show duplicates", onAction: () => showDuplicates({ ...j, id: j.import_id }) } : {});
        }
      }
      if (finished) {
        loadInfo(infoCard);
        loadHistory();
      }
      knownActive = active;
      timer = setTimeout(poll, active.size ? 1000 : 5000);
    } catch (err) {
      toastError(err);
    }
  };
  const onImportsChanged = () => loadHistory();
  addEventListener("phobos:imports-changed", onImportsChanged);
  page.addEventListener("job-started", () => { clearTimeout(timer); poll(); });
  poll().then(loadHistory);
  loadInfo(infoCard);
  onDispose(() => {
    clearTimeout(timer);
    removeEventListener("phobos:imports-changed", onImportsChanged);
  });
}

const KIND_LABEL = { import: "Folder import", upload: "Browser upload", rescan: "Library rescan" };

function countsText(r) {
  return `${plural(r.imported, "new item")} · ${plural(r.duplicates, "duplicate")}`
    + `${r.skipped ? ` · ${r.skipped} skipped` : ""}${r.failed ? ` · ${r.failed} failed` : ""}`;
}

function renderHistory(list, rows, jobs) {
  const finished = rows.filter((r) => r.finished_at); // running ones show live above
  if (!finished.length) {
    list.replaceChildren(h("li.history-empty", { text: "Nothing imported yet." }));
    return;
  }
  list.replaceChildren(...finished.map((r) => {
    const job = jobs.find((j) => j.import_id === r.id); // errors are only kept in memory
    const where = r.kind === "upload" ? r.path : displayPath(r.path) + (r.recursive ? "" : " (top folder only)");
    return h("li", {},
      h("div.history-head", {},
        h("strong", { text: KIND_LABEL[r.kind] ?? r.kind }),
        r.status === "done" ? null : h("span", { class: `history-status ${r.status}`, text: r.status }),
        h("time", { datetime: r.started_at, text: formatDateTime(r.started_at) })),
      h("div.path", { text: where, title: r.path }),
      h("div.history-foot", {},
        h("span.stats", { text: countsText(r) }),
        r.duplicates ? button("Show duplicates", { cls: "btn small", onclick: () => showDuplicates(r) }) : null),
      job?.errors.length ? h("details", {}, h("summary", { text: `Problems (${job.errors.length})` }),
        h("ul", {}, job.errors.map((e) => h("li", { text: `${e.file}: ${e.error}` })))) : null);
  }));
}

function uploadCard() {
  const fileInput = h("input", { type: "file", multiple: true, accept: "image/*,video/*,.heic,.heif", hidden: true });
  fileInput.addEventListener("change", () => { uploader.add([...fileInput.files]); fileInput.value = ""; });
  const zone = h("div.dropzone", {},
    icon("import"),
    h("strong", { text: "Drag photos, videos or whole folders here" }),
    h("span", { text: "or" }),
    h("div.row", {},
      button("Choose files", { cls: "btn primary", onclick: () => fileInput.click() }),
      button("Choose folder", { icon: "folder", onclick: (e) => chooseAndImport(e.currentTarget) })),
    fileInput);
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", () => zone.classList.remove("over")); // the window handler imports

  const bar = h("div");
  const summary = h("div.upload-summary");
  const progress = h("div.progress", { hidden: true }, bar);
  const errors = h("details", { hidden: true }, h("summary", { text: "Errors" }), h("ul"));
  const dupBtn = button("Show duplicates", { cls: "btn small", hidden: true,
    onclick: () => uploader.entry && showDuplicates({ ...uploader.entry, duplicates: uploader.stats.duplicate }) });
  const render = (s) => {
    if (!s) return;
    dupBtn.hidden = !(s.duplicate && uploader.entry);
    progress.hidden = false;
    bar.style.width = `${s.bytes ? Math.min(100, (s.sent / s.bytes) * 100) : 100}%`;
    summary.textContent = `${s.done}/${s.total} files (${formatBytes(s.sent)} of ${formatBytes(s.bytes)}) · `
      + `${s.imported} new · ${s.duplicate} duplicates${s.skipped ? ` · ${s.skipped} skipped` : ""}${s.error ? ` · ${s.error} failed` : ""}`;
    errors.hidden = !s.errors.length;
    errors.querySelector("ul").replaceChildren(...s.errors.slice(-100).map((e) => h("li", { text: e })));
  };
  uploader.listeners.add(render);
  render(uploader.stats);

  return h("section.card", {},
    h("h2", { text: "Upload from this browser" }),
    h("p", { text: "Files are copied into the library; your originals are not touched. Duplicates are detected and skipped." }),
    zone, progress, h("div.upload-foot", {}, summary, dupBtn), errors);
}

async function startImport(origin, path, recursive) {
  try {
    const job = await api.post("/api/imports", { path, recursive });
    toast(`Import started: ${job.path}`);
    origin.dispatchEvent(new CustomEvent("job-started", { bubbles: true }));
  } catch (err) {
    toastError(err);
  }
}

async function chooseAndImport(origin) {
  const picked = await pickFolder();
  if (picked) await startImport(origin, picked.path, picked.recursive);
}

function folderCard() {
  const input = h("input", { type: "text", name: "path", required: true, spellcheck: false,
    placeholder: "C:\\Users\\you\\Pictures  or  /mnt/c/Users/you/Pictures" });
  const recursive = h("input", { type: "checkbox", checked: true });
  const form = h("form", {},
    h("label.field", {}, h("span", { text: "Folder path (Windows or Linux)" }),
      h("div.row.nowrap", {}, input, button("Browse…", { icon: "folder", onclick: async () => {
        const picked = await pickFolder(input.value.trim());
        if (!picked) return;
        input.value = picked.path;
        recursive.checked = picked.recursive;
        startImport(form, picked.path, picked.recursive);
      } }))),
    h("div.row", {},
      h("label.row", {}, recursive, h("span", { text: "Include subfolders" })),
      h("button.btn.primary", { type: "submit" }, icon("import"), h("span", { text: "Start import" }))));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    startImport(form, input.value, recursive.checked);
  });
  return h("section.card", {},
    h("h2", { text: "Import a folder on this computer" }),
    h("p", { text: "Best for large collections: the server reads the folder directly. Paste a path from Windows Explorer's address bar." }),
    form, h("div", { id: "jobs" }));
}

function renderJobs(box, jobs) {
  box.replaceChildren(...jobs.map((j) => {
    const pct = j.total ? (j.processed / j.total) * 100 : 0;
    const running = !j.finished_at;
    return h("div.job", {},
      h("div.row", {},
        h("strong", { text: `${j.kind === "rescan" ? "Rescan" : "Import"} · ${j.status}` }),
        running ? button("Cancel", {
          onclick: async () => { try { await api.post(`/api/jobs/${j.id}/cancel`); } catch (err) { toastError(err); } },
        }) : null),
      h("div.path", { text: displayPath(j.path) }),
      running ? h("div.progress", {}, h("div", { style: `width:${pct}%` })) : null,
      h("div.stats", { text: `${j.processed}/${j.total || "?"} processed · ${j.imported} new · ${j.duplicates} duplicates`
        + `${j.skipped ? ` · ${j.skipped} skipped` : ""}${j.failed ? ` · ${j.failed} failed` : ""}${j.missing ? ` · ${j.missing} missing on disk` : ""}` }),
      j.message ? h("div.stats", { text: j.message }) : null,
      j.duplicates && j.import_id ? button("Show duplicates so far", { cls: "btn small",
        onclick: () => showDuplicates({ ...j, id: j.import_id }) }) : null,
      j.errors.length ? h("details", {}, h("summary", { text: `Problems (${j.errors.length})` }),
        h("ul", {}, j.errors.map((e) => h("li", { text: `${e.file}: ${e.error}` })))) : null);
  }));
}

async function loadInfo(card) {
  try {
    const info = await api.get("/api/info");
    const s = info.stats;
    const copy = (text) => button("Copy", {
      onclick: async () => {
        try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed", { error: true }); }
      },
    });
    card.replaceChildren(
      h("h2", { text: "Library" }),
      ...info.warnings.map((w) => h("div.notice.warn", { text: w })),
      h("dl.kv", {},
        h("dt", { text: "Items" }), h("dd", { text: `${plural(s.photos, "photo")}, ${plural(s.videos, "video")} · ${formatBytes(s.bytes)}` }),
        h("dt", { text: "Albums" }), h("dd", { text: String(s.albums) }),
        h("dt", { text: "In trash" }), h("dd", { text: String(s.trash) }),
        h("dt", { text: "Originals" }), h("dd", {}, h("code.path", { text: info.library_dir })),
        info.library_dir_windows ? h("dt", { text: "From Windows" }) : null,
        info.library_dir_windows ? h("dd", {}, h("code.path", { text: info.library_dir_windows }), " ", copy(info.library_dir_windows)) : null,
        h("dt", { text: "Database" }), h("dd", {}, h("code.path", { text: info.data_dir }))),
      h("p", { style: "margin-top:12px", text: "Added files to the library folder yourself (e.g. from Explorer)? Rescan to index them." }),
      button("Rescan library", {
        icon: "restore",
        onclick: async () => {
          try {
            await api.post("/api/rescan");
            toast("Rescan started");
            card.dispatchEvent(new CustomEvent("job-started", { bubbles: true }));
          } catch (err) {
            toastError(err);
          }
        },
      }),
      h("p", { style: "margin-top:12px; font-size:.85rem",
        text: `Phobos ${info.version} · video thumbnails ${info.video_thumbnails ? "on" : "off (install ffmpeg)"}` }),
    );
  } catch (err) {
    toastError(err);
  }
}
