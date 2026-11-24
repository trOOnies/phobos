// Exit button: confirm, stop the server, then show a farewell page with no app controls left.

import { api } from "./api.js";
import { uploader } from "./importer.js";
import { h, plural, toastError } from "./ui.js";

// The Phobos moon, inlined: once the server is gone, /static/favicon.svg can't be fetched.
const MOON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" class="farewell-moon" aria-hidden="true">
<defs><radialGradient id="farewell-g" cx="38%" cy="34%" r="70%"><stop offset="0" stop-color="#e9a0c6"/>
<stop offset=".55" stop-color="#9b1b66"/><stop offset="1" stop-color="#4a0b30"/></radialGradient></defs>
<ellipse cx="16" cy="16.5" rx="13" ry="11.5" transform="rotate(-18 16 16.5)" fill="url(#farewell-g)"/>
<circle cx="11" cy="13" r="2.2" fill="#4a0b30" opacity=".45"/><circle cx="19.5" cy="19" r="3.2" fill="#4a0b30" opacity=".35"/>
<circle cx="20" cy="11" r="1.3" fill="#4a0b30" opacity=".4"/></svg>`;

/** Ask for confirmation; resolve true once the server has agreed to stop. */
async function confirmExit() {
  const d = document.getElementById("exit-dialog");
  const warn = d.querySelector("#exit-warn");
  const busy = [];
  try {
    const running = (await api.get("/api/jobs")).filter((j) => !j.finished_at);
    if (running.length) busy.push(`${plural(running.length, "import")} still running`);
  } catch { /* the server may already be unhappy; still let the user leave */ }
  const uploading = uploader.active + uploader.queue.length;
  if (uploading) busy.push(`${plural(uploading, "upload")} not finished`);
  warn.textContent = busy.length ? `Heads up: ${busy.join(" and ")}. Exiting now stops them; you can import the rest next time.` : "";
  warn.hidden = !busy.length;

  const result = new Promise((resolve) => d.addEventListener("close", () => {
    const v = d.returnValue;
    d.returnValue = "";
    resolve(v);
  }, { once: true }));
  d.showModal();
  d.querySelector("#exit-stay").focus();
  return (await result) === "exit";
}

/**
 * Exit flow for the top-bar door button.
 * @param {() => void} teardown  stops the app's views, timers and global listeners
 */
export async function exitApp(teardown) {
  if (!(await confirmExit())) return;
  let info = null;
  try { info = await api.get("/api/info"); } catch { /* path is optional on the farewell page */ }
  try {
    await api.post("/api/shutdown");
  } catch (err) {
    // 404: the page is newer than the running server (started before an update, without --reload).
    if (err.status === 404) toastError("This Phobos server is running an older version that can't exit from here. Stop it with Ctrl+C in its terminal and start it again.");
    else toastError(err);
    return;
  }
  uploader.queue.length = 0;
  teardown();
  showFarewell(info);
}

function showFarewell(info) {
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  const moon = h("div.farewell-orbit");
  moon.innerHTML = MOON_SVG; // static markup above, no user data
  const page = h("main.farewell", { "aria-labelledby": "farewell-title" },
    h("div.stars", { "aria-hidden": "true" }),
    moon,
    h("h1", { id: "farewell-title", text: "See you soon!" }),
    h("p.lead", { text: "Phobos has left orbit. The server is stopped." }),
    info?.library_dir
      ? h("p", {}, "Your photos and videos are resting safely in ",
        h("code.path", { text: info.library_dir_windows || info.library_dir }), ".")
      : h("p", { text: "Your photos and videos are resting safely on disk." }),
    h("p.hint", { text: "You can close this tab now. To come back, run phobos-web (or make serve) and reopen this page." }));
  document.title = "Phobos · See you soon";
  document.body.replaceChildren(page);
  document.body.classList.add("closed");
  // Nothing is left to interact with; also stop dropped files from opening in the tab.
  for (const type of ["dragover", "drop"]) addEventListener(type, (e) => e.preventDefault());
}
