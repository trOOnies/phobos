// "Show duplicates": the files an import skipped because the library already had
// them, each next to the library item it matched. Clicking one opens the viewer.

import { api, qs } from "./api.js";
import { formatDateTime, h, plural, toastError } from "./ui.js";

const PAGE = 200;
const dialog = document.getElementById("duplicates");
const title = dialog.querySelector("#dup-title");
const intro = dialog.querySelector("#dup-intro");
const list = dialog.querySelector("#dup-list");
const moreBtn = dialog.querySelector("#dup-more");

/** "/mnt/c/Users/me/x.jpg" -> "C:\Users\me\x.jpg"; other paths unchanged. */
export function displayPath(path) {
  const m = /^\/mnt\/([a-z])(?:\/(.*))?$/i.exec(path);
  return m ? `${m[1].toUpperCase()}:\\${(m[2] ?? "").replaceAll("/", "\\")}` : path;
}

// The viewer walks a grid-like object: this one is the list in the dialog.
class DuplicateList {
  items = [];
  done = true;
  rows = new Map(); // media id -> [<li>, ...] (one library item can match several files)
  tileFor() { return null; }
  loadMore() { return Promise.resolve(); }
  update(item) {
    const i = this.items.findIndex((x) => x.id === item.id);
    if (i >= 0) this.items[i] = { ...this.items[i], ...item };
  }
  remove(ids) { // trashed from the viewer: still the match, so keep it and flag it
    for (const id of ids) {
      for (const li of this.rows.get(id) ?? []) li.querySelector(".dup-trash").hidden = false;
      const item = this.items.find((x) => x.id === id);
      if (item) item.deleted_at ??= new Date().toISOString();
    }
  }
}

let state = null;

// Paths inside the imported folder read better relative to it ("Trip/IMG_1.jpg").
function shortSource(source) {
  const base = state.base;
  return base && source.startsWith(`${base}/`) ? source.slice(base.length + 1) : displayPath(source);
}

async function loadPage() {
  const { importId, list: dl } = state;
  moreBtn.disabled = true;
  try {
    const page = await api.get(`/api/imports/${importId}/duplicates${qs({ offset: state.offset, limit: PAGE })}`);
    if (state?.importId !== importId) return; // dialog reopened for another import
    state.total = page.total;
    state.offset += page.items.length;
    list.querySelector(".dup-empty")?.remove();
    list.append(...page.items.map(({ source, media }) => {
      const open = () => {
        const index = dl.items.findIndex((x) => x.id === media.id);
        if (index >= 0) dispatchEvent(new CustomEvent("phobos:view", { detail: { grid: dl, index } }));
      };
      const li = h("li", {},
        h("button.dup-row", { type: "button", onclick: open, title: "Open the library copy" },
          h("img", { src: media.thumb_url, alt: "", loading: "lazy", decoding: "async", width: 56, height: 56 }),
          h("span.dup-text", {},
            h("span.dup-source", { text: shortSource(source), title: displayPath(source) }),
            h("span.dup-match", {},
              h("span", { text: `Already in library: ${formatDateTime(media.taken_at)} · ${media.rel_path}` }),
              h("span.dup-trash", { text: "In trash", hidden: !media.deleted_at })))));
      if (!dl.rows.has(media.id)) {
        dl.rows.set(media.id, []);
        dl.items.push(media);
      }
      dl.rows.get(media.id).push(li);
      return li;
    }));
    if (!state.offset) list.append(h("li.dup-empty", { text: "The matching library items have since been deleted." }));
    moreBtn.hidden = state.offset >= state.total;
  } catch (err) {
    toastError(err);
  } finally {
    moreBtn.disabled = false;
  }
}

moreBtn.addEventListener("click", () => state && loadPage());

/** Open the list for one import-history entry ({id, kind, path, duplicates}). */
export async function showDuplicates(entry) {
  state = { importId: entry.id, offset: 0, total: 0, list: new DuplicateList(),
    base: entry.kind === "upload" ? null : entry.path.replace(/\/+$/, "") };
  title.textContent = `${plural(entry.duplicates, "duplicate")} skipped`;
  intro.textContent = `These files from ${entry.kind === "upload" ? entry.path : displayPath(entry.path)} `
    + "were not imported because identical copies (same content, any name) are already in your library.";
  list.replaceChildren();
  moreBtn.hidden = true;
  list.scrollTop = 0;
  dialog.showModal();
  await loadPage();
}
