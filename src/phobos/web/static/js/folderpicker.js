// Phobos' own folder picker. The server lists folders on this computer (Windows
// drives under /mnt included) and imports the chosen one directly, so there is no
// browser "upload all files from this folder?" prompt and nothing goes over HTTP.

import { api, qs } from "./api.js";
import { h, icon, plural } from "./ui.js";

const dialog = document.getElementById("folder-picker");
const rootsBox = dialog.querySelector("#fp-roots");
const list = dialog.querySelector("#fp-list");
const pathInput = dialog.querySelector("#fp-path");
const upBtn = dialog.querySelector("#fp-up");
const status = dialog.querySelector("#fp-status");
const okBtn = dialog.querySelector("#fp-ok");
const recursive = dialog.querySelector("#fp-recursive");

let current = null;

// Windows drives read best as "C:\...", Linux folders as plain Linux paths.
const shownPath = (data) => (data.path.startsWith("/mnt/") && data.windows_path) || data.path;
 // last successful listing
let requestId = 0;

async function open(path) {
  const id = ++requestId;
  status.className = "fp-status";
  status.textContent = "Loading…";
  try {
    const data = await api.get(`/api/folders${qs({ path })}`);
    if (id !== requestId) return; // a newer navigation won
    current = data;
    render(data);
  } catch (err) {
    if (id !== requestId) return;
    status.className = "fp-status warn";
    status.textContent = err.message;
    if (current) pathInput.value = shownPath(current);
  }
}

function render(data) {
  pathInput.value = shownPath(data);
  pathInput.scrollLeft = pathInput.scrollWidth; // keep the deepest folder in view
  upBtn.disabled = !data.parent;

  rootsBox.replaceChildren(...data.roots.map((r) => {
    const here = data.path === r.path;
    return h("button.btn", { type: "button", text: r.label, title: r.path, "aria-current": here ? "true" : null,
      onclick: () => open(r.path) });
  }));

  list.replaceChildren(...(data.dirs.length
    ? data.dirs.map((d) => h("li", {}, h("button", { type: "button", title: d.name, onclick: () => open(d.path) },
        icon("folder"), h("span", { text: d.name }))))
    : [h("li.fp-empty", { text: "No subfolders" })]));
  list.scrollTop = 0;

  const bits = [data.media_files ? `${plural(data.media_files, "photo/video file")} directly in this folder` : "No photos or videos directly in this folder"];
  if (data.dirs.length) bits.push(`${plural(data.dirs.length, "subfolder")}${data.truncated ? " (list truncated)" : ""}`);
  if (data.inside_library) {
    status.className = "fp-status warn";
    status.textContent = "This folder is inside your library already - use “Rescan library” instead.";
  } else {
    status.className = "fp-status";
    status.textContent = bits.join(" · ");
  }
  okBtn.disabled = data.inside_library || !data.parent; // never the whole filesystem (/)
}

upBtn.addEventListener("click", () => current?.parent && open(current.parent));
pathInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault(); // navigate, don't submit the dialog
    if (pathInput.value.trim()) open(pathInput.value.trim());
  }
});
list.addEventListener("keydown", (e) => {
  if (e.key === "Backspace" && current?.parent) { e.preventDefault(); open(current.parent); }
});

/** Show the picker; resolves to {path, recursive} or null when cancelled. */
export async function pickFolder(startPath) {
  current = null;
  okBtn.disabled = true;
  list.replaceChildren();
  const result = new Promise((resolve) => dialog.addEventListener("close", () => {
    const v = dialog.returnValue;
    dialog.returnValue = "";
    resolve(v);
  }, { once: true }));
  dialog.showModal();
  await open(startPath || undefined);
  if (!current) await open(undefined); // bad start path: fall back to the default place
  list.querySelector("button")?.focus();
  return (await result) === "ok" && current ? { path: current.path, recursive: recursive.checked } : null;
}
