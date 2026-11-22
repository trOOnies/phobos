// Thin wrapper over the JSON API. Every state-changing request carries the
// X-Phobos header the server requires (cross-site requests can't add it).

const HEADERS = { "X-Phobos": "1" };

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
  }
}

async function request(method, url, body) {
  const opts = { method, headers: { ...HEADERS } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 204) return null;
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    let detail = data?.detail ?? data;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new ApiError(res.status, detail || res.statusText);
  }
  return data;
}

export function qs(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body ?? {}),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),

  media: (filter, cursor, limit = 200) => request("GET", `/api/media${qs({ ...filter, cursor, limit })}`),
  timeline: (filter) => request("GET", `/api/timeline${qs(filter)}`),
  action: (ids, action, extra = {}) => request("POST", "/api/media/actions", { ids, action, ...extra }),

  /** Upload one file with progress (fetch() can't report upload progress). */
  upload(file, onProgress, fields = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-Phobos", "1");
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response);
        else reject(new ApiError(xhr.status, xhr.response?.detail ?? xhr.statusText));
      };
      xhr.onerror = () => reject(new ApiError(0, "network error"));
      const form = new FormData();
      form.append("file", file, file.name);
      if (file.lastModified) form.append("last_modified", String(file.lastModified));
      for (const [k, v] of Object.entries(fields)) if (v != null) form.append(k, String(v));
      xhr.send(form);
    });
  },
};
