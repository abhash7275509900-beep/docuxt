/**
 * DocEngine Complete Frontend Application
 * Covers Phases 0 through 8 of the DocEngine API.
 * All dynamic values rendered into HTML are strictly escaped via esc() to prevent XSS.
 */
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const state = {
  token: localStorage.getItem("docengine_token") || null,
  user: null,
  authMode: "login",
  files: [],
  checkedFileIds: new Set(),
  activeFileId: null,
  activeFileInfo: null,
  currentPage: 1,
  totalPages: 1,
  pageWidthPt: 612,
  pageHeightPt: 792,
  selectedCoords: { page: 1, x: 72, y: 650, width: 150, height: 24 },
  redactRegions: [],
  queuedFormFields: [],
  detectedPii: [],
  activeDocId: null,
  activeShareToken: null,
  previewBlobUrl: null,
};

function toast(msg, type = "info") {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token && !options.noAuth) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const resp = await fetch(path, Object.assign({}, options, { headers }));
  const ct = resp.headers.get("Content-Type") || "";
  const data = ct.includes("application/json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    if (resp.status === 401 && !options.noAuth && state.token) {
      resetSessionAndUI();
    }
    const errMsg = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
    throw new Error(errMsg);
  }
  return data;
}

async function runJob(label, submitPromise) {
  try {
    const { job_id } = await submitPromise;
    addJobPill(job_id, label, "running");
    const start = Date.now();
    while (Date.now() - start < 180000) {
      const job = await api(`/jobs/${encodeURIComponent(job_id)}`);
      if (job.status === "done") {
        updateJobPill(job_id, label, "done");
        toast(`${label} completed`, "success");
        await loadFiles();
        return job.result;
      }
      if (job.status === "failed") {
        updateJobPill(job_id, label, "failed");
        throw new Error(job.error || "Job failed");
      }
      await new Promise((r) => setTimeout(r, 350));
    }
    throw new Error("Job timed out");
  } catch (err) {
    toast(`${label}: ${err.message}`, "error");
    throw err;
  }
}

function addJobPill(jobId, label, status) {
  const list = document.getElementById("jobs-list");
  if (list.querySelector(".muted")) list.innerHTML = "";
  const el = document.createElement("div");
  el.className = "job-pill";
  el.id = `job-${jobId}`;
  el.innerHTML = `<span>${esc(label)}</span><span class="badge">${esc(status)}</span>`;
  list.prepend(el);
}

function updateJobPill(jobId, label, status) {
  const el = document.getElementById(`job-${jobId}`);
  if (el) {
    el.innerHTML = `<span>${esc(label)}</span><span class="badge">${esc(status)}</span>`;
  }
}

// ---------- Auth ----------

async function checkSession() {
  if (!state.token) {
    showAuthScreen();
    return;
  }
  try {
    const me = await api("/auth/me");
    state.user = me;
    document.getElementById("auth-user-email").textContent = me.email;
    document.getElementById("auth-user-pill").classList.remove("hidden");
    document.getElementById("auth-screen").classList.add("hidden");
    document.getElementById("app-workspace").classList.remove("hidden");
    await loadFiles();
    await loadDocuments();
  } catch (_) {
    resetSessionAndUI();
  }
}

function showAuthScreen() {
  document.getElementById("auth-user-pill").classList.add("hidden");
  document.getElementById("app-workspace").classList.add("hidden");
  document.getElementById("auth-screen").classList.remove("hidden");
}

document.getElementById("tab-auth-login").addEventListener("click", () => {
  state.authMode = "login";
  document.getElementById("tab-auth-login").classList.add("active");
  document.getElementById("tab-auth-signup").classList.remove("active");
  document.getElementById("btn-auth-submit").textContent = "Login";
});

document.getElementById("tab-auth-signup").addEventListener("click", () => {
  state.authMode = "signup";
  document.getElementById("tab-auth-signup").classList.add("active");
  document.getElementById("tab-auth-login").classList.remove("active");
  document.getElementById("btn-auth-submit").textContent = "Create Account";
});

document.getElementById("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = document.getElementById("auth-email").value.trim();
  const password = document.getElementById("auth-password").value;
  const endpoint = state.authMode === "signup" ? "/auth/signup" : "/auth/login";
  try {
    const res = await api(endpoint, { method: "POST", json: { email, password }, noAuth: true });
    state.token = res.token;
    localStorage.setItem("docengine_token", res.token);
    toast(`Welcome, ${email}!`, "success");
    await checkSession();
  } catch (err) {
    toast(err.message, "error");
  }
});

function clearViewerUI() {
  if (state.previewBlobUrl) {
    try { URL.revokeObjectURL(state.previewBlobUrl); } catch (_) {}
    state.previewBlobUrl = null;
  }
  const img = document.getElementById("pdf-page-img");
  if (img) img.removeAttribute("src");
  document.getElementById("pdf-canvas-wrapper")?.classList.add("hidden");
  document.getElementById("text-preview-box")?.classList.add("hidden");
  document.getElementById("selection-rect")?.classList.add("hidden");
  document.getElementById("viewer-empty")?.classList.remove("hidden");
  const lbl = document.getElementById("active-file-label");
  if (lbl) lbl.textContent = "No file selected";
  document.getElementById("active-file-badge")?.classList.add("hidden");
  const dlBtn = document.getElementById("btn-download-active");
  if (dlBtn) dlBtn.disabled = true;
  const ind = document.getElementById("page-indicator");
  if (ind) ind.textContent = "Page 0 / 0";
  const prevBtn = document.getElementById("btn-prev-page");
  const nextBtn = document.getElementById("btn-next-page");
  if (prevBtn) prevBtn.disabled = true;
  if (nextBtn) nextBtn.disabled = true;
}

function resetSessionAndUI() {
  state.token = null;
  state.user = null;
  state.files = [];
  state.checkedFileIds = new Set();
  state.activeFileId = null;
  state.activeFileInfo = null;
  state.currentPage = 1;
  state.totalPages = 1;
  state.pageWidthPt = 612;
  state.pageHeightPt = 792;
  state.redactRegions = [];
  state.queuedFormFields = [];
  state.detectedPii = [];
  state.activeDocId = null;
  state.activeShareToken = null;
  localStorage.removeItem("docengine_token");
  clearViewerUI();
  const fc = document.getElementById("file-count");
  if (fc) fc.textContent = "0";
  for (const id of ["file-list", "jobs-list", "annotations-list", "shares-list", "comments-list", "documents-list"]) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = "";
  }
  showAuthScreen();
}

document.getElementById("btn-logout").addEventListener("click", () => {
  resetSessionAndUI();
});

// ---------- File Management ----------

async function loadFiles() {
  const data = await api("/files");
  state.files = data.files || [];
  document.getElementById("file-count").textContent = state.files.length;
  if (state.activeFileId && !state.files.some((f) => f.id === state.activeFileId)) {
    state.activeFileId = null;
    state.activeFileInfo = null;
  }
  renderFileList();
  populateCompareSelect();
  if (!state.activeFileId && state.files.length > 0) {
    await selectFile(state.files[0].id);
  } else if (state.files.length === 0) {
    clearViewerUI();
  }
}

function renderFileList() {
  const list = document.getElementById("file-list");
  const q = document.getElementById("file-search-input").value.trim().toLowerCase();
  const fmtFilter = document.getElementById("file-format-filter").value;
  list.innerHTML = "";

  const filtered = state.files.filter((f) => {
    const nameMatch =
      !q ||
      (f.original_name || "").toLowerCase().includes(q) ||
      (f.id || "").toLowerCase().includes(q);
    const fmtMatch = !fmtFilter || f.format === fmtFilter || (f.id || "").endsWith(`.${fmtFilter}`);
    return nameMatch && fmtMatch;
  });

  if (filtered.length === 0) {
    list.innerHTML = `<div class="muted small">No matching files.</div>`;
    return;
  }

  for (const f of filtered) {
    const row = document.createElement("div");
    row.className = `file-item ${f.id === state.activeFileId ? "active" : ""}`;
    const checkedAttr = state.checkedFileIds.has(f.id) ? "checked" : "";
    const kb = ((f.size || 0) / 1024).toFixed(1);
    const displayName = f.original_name || f.id;
    row.innerHTML = `
      <input type="checkbox" class="file-check" data-id="${esc(f.id)}" ${esc(checkedAttr)} />
      <div class="file-item-meta">
        <div class="file-item-name" title="${esc(displayName)}">${esc(displayName)}</div>
        <div class="file-item-sub">${esc(f.format || "file")} · ${esc(kb)} KB · ${esc(f.kind)}</div>
      </div>
    `;
    row.querySelector(".file-check").addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (ev.target.checked) state.checkedFileIds.add(f.id);
      else state.checkedFileIds.delete(f.id);
    });
    row.addEventListener("click", () => selectFile(f.id));
    list.appendChild(row);
  }
}

document.getElementById("file-search-input").addEventListener("input", renderFileList);
document.getElementById("file-format-filter").addEventListener("change", renderFileList);
document.getElementById("btn-refresh-files").addEventListener("click", loadFiles);
document.getElementById("btn-clear-jobs").addEventListener("click", () => {
  document.getElementById("jobs-list").innerHTML = `<div class="muted small">Cleared.</div>`;
});

document.getElementById("file-upload-input").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api("/files", { method: "POST", body: fd });
      toast(`Uploaded ${file.name} (${res.format})`, "success");
      state.activeFileId = res.file_id;
    } catch (err) {
      toast(`Upload failed (${file.name}): ${err.message}`, "error");
    }
  }
  e.target.value = "";
  await loadFiles();
  if (state.activeFileId) await selectFile(state.activeFileId);
});

function populateCompareSelect() {
  const sel = document.getElementById("compare-file-b");
  sel.innerHTML = "";
  for (const f of state.files.filter((x) => (x.id || "").endsWith(".pdf"))) {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = `${f.original_name || f.id} (${f.id.slice(0, 8)})`;
    sel.appendChild(opt);
  }
}

async function selectFile(fileId) {
  state.activeFileId = fileId;
  state.currentPage = 1;
  renderFileList();
  try {
    const info = await api(`/files/${encodeURIComponent(fileId)}/info`);
    state.activeFileInfo = info;
    const meta = info.meta || {};
    document.getElementById("active-file-label").textContent = meta.original_name || fileId;
    const badge = document.getElementById("active-file-badge");
    badge.textContent = `${meta.format || "file"} · ${((info.size || 0) / 1024).toFixed(1)} KB`;
    badge.classList.remove("hidden");
    document.getElementById("btn-download-active").disabled = false;

    const isPdf = fileId.toLowerCase().endsWith(".pdf") || meta.format === "pdf";
    if (isPdf) {
      state.totalPages = info.pages || 1;
      state.pageWidthPt = info.page_width || 612;
      state.pageHeightPt = info.page_height || 792;
      await renderPdfPage();
      loadAnnotations();
      loadShares();
      loadComments();
    } else {
      state.totalPages = 1;
      document.getElementById("page-indicator").textContent = "Non-PDF File";
      document.getElementById("btn-prev-page").disabled = true;
      document.getElementById("btn-next-page").disabled = true;
      document.getElementById("pdf-canvas-wrapper").classList.add("hidden");
      document.getElementById("viewer-empty").classList.add("hidden");
      const textBox = document.getElementById("text-preview-box");
      if (/\.(txt|md|csv)$/i.test(fileId)) {
        const resp = await fetch(`/files/${encodeURIComponent(fileId)}?inline=1`, {
          headers: { Authorization: `Bearer ${state.token}` },
        });
        textBox.textContent = await resp.text();
        textBox.classList.remove("hidden");
      } else {
        textBox.textContent = `Selected file: ${meta.original_name || fileId}\nFormat: ${meta.format}\nSize: ${info.size} bytes\n\nUse Tab 3 (Office Convert) to convert this file to PDF or another format.`;
        textBox.classList.remove("hidden");
      }
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

async function renderPdfPage() {
  document.getElementById("viewer-empty").classList.add("hidden");
  document.getElementById("text-preview-box").classList.add("hidden");
  const wrapper = document.getElementById("pdf-canvas-wrapper");
  const img = document.getElementById("pdf-page-img");
  const pageSizes = state.activeFileInfo?.page_sizes;
  if (Array.isArray(pageSizes) && pageSizes[state.currentPage - 1]) {
    state.pageWidthPt = pageSizes[state.currentPage - 1].width || state.pageWidthPt;
    state.pageHeightPt = pageSizes[state.currentPage - 1].height || state.pageHeightPt;
  }
  document.getElementById("page-indicator").textContent = `Page ${state.currentPage} / ${state.totalPages}`;
  document.getElementById("btn-prev-page").disabled = state.currentPage <= 1;
  document.getElementById("btn-next-page").disabled = state.currentPage >= state.totalPages;
  document.getElementById("ann-page").value = state.currentPage;

  try {
    const resp = await fetch(
      `/files/${encodeURIComponent(state.activeFileId)}/preview?page=${state.currentPage}&dpi=120`,
      { headers: { Authorization: `Bearer ${state.token}` } }
    );
    if (!resp.ok) throw new Error("Preview unavailable (file may be password-locked)");
    const blob = await resp.blob();
    if (state.previewBlobUrl) {
      try { URL.revokeObjectURL(state.previewBlobUrl); } catch (_) {}
    }
    state.previewBlobUrl = URL.createObjectURL(blob);
    img.src = state.previewBlobUrl;
    wrapper.classList.remove("hidden");
  } catch (err) {
    wrapper.classList.add("hidden");
    const textBox = document.getElementById("text-preview-box");
    textBox.textContent = `PDF Preview Note: ${err.message}`;
    textBox.classList.remove("hidden");
  }
}

document.getElementById("btn-prev-page").addEventListener("click", () => {
  if (state.currentPage > 1) {
    state.currentPage--;
    renderPdfPage();
  }
});

document.getElementById("btn-next-page").addEventListener("click", () => {
  if (state.currentPage < state.totalPages) {
    state.currentPage++;
    renderPdfPage();
  }
});

document.getElementById("btn-download-active").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const resp = await fetch(`/files/${encodeURIComponent(state.activeFileId)}`, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = state.activeFileInfo?.meta?.original_name || state.activeFileId;
  a.click();
});

// ---------- Interactive Canvas Coordinate Selection ----------

const canvasWrapper = document.getElementById("pdf-canvas-wrapper");
const selRect = document.getElementById("selection-rect");
let dragStart = null;

canvasWrapper.addEventListener("mousedown", (e) => {
  const rect = canvasWrapper.getBoundingClientRect();
  dragStart = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  selRect.style.left = `${dragStart.x}px`;
  selRect.style.top = `${dragStart.y}px`;
  selRect.style.width = `0px`;
  selRect.style.height = `0px`;
  selRect.classList.remove("hidden");
});

window.addEventListener("mousemove", (e) => {
  if (!dragStart) return;
  const rect = canvasWrapper.getBoundingClientRect();
  const curX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
  const curY = Math.max(0, Math.min(rect.height, e.clientY - rect.top));
  const left = Math.min(dragStart.x, curX);
  const top = Math.min(dragStart.y, curY);
  const width = Math.max(8, Math.abs(curX - dragStart.x));
  const height = Math.max(8, Math.abs(curY - dragStart.y));

  selRect.style.left = `${left}px`;
  selRect.style.top = `${top}px`;
  selRect.style.width = `${width}px`;
  selRect.style.height = `${height}px`;

  const scaleX = state.pageWidthPt / rect.width;
  const scaleY = state.pageHeightPt / rect.height;
  const xPt = Math.round(left * scaleX);
  const wPt = Math.round(width * scaleX);
  const hPt = Math.round(height * scaleY);
  const yPt = Math.round(state.pageHeightPt - top * scaleY - hPt);

  state.selectedCoords = { page: state.currentPage, x: xPt, y: yPt, width: wPt, height: hPt };
  document.getElementById("selected-coords-pill").textContent =
    `p:${state.currentPage} x:${xPt}, y:${yPt}, w:${wPt}, h:${hPt}`;
  document.getElementById("ann-page").value = state.currentPage;
  document.getElementById("ann-x").value = xPt;
  document.getElementById("ann-y").value = yPt;
  document.getElementById("ann-w").value = wPt;
  document.getElementById("ann-h").value = hPt;
});

window.addEventListener("mouseup", () => {
  dragStart = null;
});

// ---------- Phase Tabs Navigation ----------

document.querySelectorAll(".phase-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".phase-tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tool-pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});

// ---------- Phase 1: Core PDF Operations ----------

function parsePageList(str) {
  return str
    .split(",")
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => !isNaN(n));
}

document.getElementById("btn-op-split").addEventListener("click", async () => {
  if (!state.activeFileId) return toast("Select a PDF first", "error");
  const raw = document.getElementById("split-ranges").value.trim();
  let ranges = null;
  if (raw) {
    ranges = raw.split(",").map((part) => {
      const [s, e] = part.trim().split("-").map((x) => parseInt(x.trim(), 10));
      return [s, e || s];
    });
  }
  const outIds = await runJob("Split PDF", api("/pdf/split", {
    method: "POST",
    json: { file_id: state.activeFileId, ranges },
  }));
  if (Array.isArray(outIds) && outIds.length > 0) await selectFile(outIds[0]);
});

document.getElementById("btn-op-reorder").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const order = parsePageList(document.getElementById("reorder-pages").value);
  const outId = await runJob("Reorder Pages", api("/pdf/reorder", {
    method: "POST",
    json: { file_id: state.activeFileId, order },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-rotate").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const rawPages = document.getElementById("rotate-pages").value.trim();
  const pages = rawPages ? parsePageList(rawPages) : null;
  const degrees = parseInt(document.getElementById("rotate-degrees").value, 10);
  const outId = await runJob("Rotate PDF", api("/pdf/rotate", {
    method: "POST",
    json: { file_id: state.activeFileId, pages, degrees },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-delete-pages").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const pages = parsePageList(document.getElementById("del-ext-pages").value);
  const outId = await runJob("Delete Pages", api("/pdf/delete-pages", {
    method: "POST",
    json: { file_id: state.activeFileId, pages },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-extract-pages").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const pages = parsePageList(document.getElementById("del-ext-pages").value);
  const outId = await runJob("Extract Pages", api("/pdf/extract-pages", {
    method: "POST",
    json: { file_id: state.activeFileId, pages },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-compress").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const quality = document.getElementById("compress-quality").value;
  const res = await runJob(`Compress (${quality})`, api("/pdf/compress", {
    method: "POST",
    json: { file_id: state.activeFileId, quality },
  }));
  if (res && res.file_id) {
    const banner = document.getElementById("compress-result");
    banner.textContent = `Before: ${res.size_before} B → After: ${res.size_after} B (Saved ${(res.ratio * 100).toFixed(1)}%)`;
    banner.classList.remove("hidden");
    await selectFile(res.file_id);
  }
});

document.getElementById("btn-op-merge").addEventListener("click", async () => {
  const fileIds = Array.from(state.checkedFileIds);
  if (fileIds.length < 2) return toast("Check at least 2 PDF files in the sidebar to merge", "error");
  const outId = await runJob("Merge PDFs", api("/pdf/merge", {
    method: "POST",
    json: { file_ids: fileIds },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-images-to-pdf").addEventListener("click", async () => {
  const fileIds = Array.from(state.checkedFileIds);
  if (fileIds.length < 1) return toast("Check at least 1 image file in the sidebar", "error");
  const outId = await runJob("Images to PDF", api("/convert/images-to-pdf", {
    method: "POST",
    json: { file_ids: fileIds },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-pdf-to-images").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const fmt = document.getElementById("pdf2img-fmt").value;
  const dpi = parseInt(document.getElementById("pdf2img-dpi").value, 10);
  await runJob("PDF to Images", api("/convert/pdf-to-images", {
    method: "POST",
    json: { file_id: state.activeFileId, fmt, dpi },
  }));
});

// ---------- Phase 2: Annotations ----------

async function loadAnnotations() {
  if (!state.activeFileId) return;
  try {
    const res = await api(`/annotations/${encodeURIComponent(state.activeFileId)}`);
    const list = document.getElementById("ann-list");
    list.innerHTML = "";
    const anns = res.annotations || [];
    if (anns.length === 0) {
      list.innerHTML = `<div class="muted small">No annotations stored on this file.</div>`;
      return;
    }
    for (const a of anns) {
      const row = document.createElement("div");
      row.className = "list-row";
      const snippet = a.data?.text ? ` — "${esc(a.data.text)}"` : "";
      row.innerHTML = `
        <span><strong>${esc(a.type)}</strong> (p.${esc(a.page)})${snippet}</span>
        <button class="btn btn-danger btn-xs">Delete</button>
      `;
      row.querySelector("button").addEventListener("click", async () => {
        await api(`/annotations/${encodeURIComponent(a.id)}`, { method: "DELETE" });
        toast("Annotation deleted", "success");
        loadAnnotations();
      });
      list.appendChild(row);
    }
  } catch (_) {}
}

document.getElementById("btn-ann-refresh").addEventListener("click", loadAnnotations);

document.getElementById("btn-ann-add").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const type = document.getElementById("ann-type").value;
  const page = parseInt(document.getElementById("ann-page").value, 10);
  const x = parseFloat(document.getElementById("ann-x").value);
  const y = parseFloat(document.getElementById("ann-y").value);
  const width = parseFloat(document.getElementById("ann-w").value);
  const height = parseFloat(document.getElementById("ann-h").value);
  const text = document.getElementById("ann-text").value;
  const color = document.getElementById("ann-color").value;
  const background = document.getElementById("ann-bg").value;
  const font_size = parseInt(document.getElementById("ann-fontsize").value, 10);

  const data = { x, y, width, height, text, color, background, font_size };
  if (type === "freehand") {
    data.points = [[x, y], [x + width / 2, y + height], [x + width, y]];
    data.stroke_width = 2;
  }
  try {
    await api("/annotations", {
      method: "POST",
      json: { file_id: state.activeFileId, type, page, data },
    });
    toast(`Added ${type} annotation`, "success");
    loadAnnotations();
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-ann-export").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const outId = await runJob("Export Flattened Annotations", api(`/annotations/${encodeURIComponent(state.activeFileId)}/export`, {
    method: "POST",
  }));
  if (outId) await selectFile(outId);
});

// ---------- Phase 3: Office Convert & Extract ----------

document.getElementById("btn-op-office-convert").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const target_format = document.getElementById("office-target-format").value;
  const outId = await runJob(`Convert to ${target_format.toUpperCase()}`, api("/convert/office", {
    method: "POST",
    json: { file_id: state.activeFileId, target_format },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-extract-text").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const format = document.getElementById("extract-text-fmt").value;
  const outId = await runJob(`Extract ${format}`, api("/extract/text", {
    method: "POST",
    json: { file_id: state.activeFileId, format },
  }));
  if (outId) {
    const resp = await fetch(`/files/${encodeURIComponent(outId)}`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const text = await resp.text();
    const box = document.getElementById("extract-text-preview");
    box.textContent = text;
    box.classList.remove("hidden");
  }
});

// ---------- Phase 4: OCR ----------

document.getElementById("btn-op-ocr").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const lang = document.getElementById("ocr-lang").value.trim() || "eng";
  const dpi = parseInt(document.getElementById("ocr-dpi").value, 10) || 300;
  const force = document.getElementById("ocr-force").checked;
  const res = await runJob("OCR Document", api("/ocr", {
    method: "POST",
    json: { file_id: state.activeFileId, lang, dpi, force },
  }));
  if (res) {
    const box = document.getElementById("ocr-result-box");
    box.classList.remove("hidden");
    const sd = res.script_detection || {};
    const pagesOcredStr = (res.pages_ocred || []).join(", ") || "None (already searchable)";
    const scriptName = sd.script || "N/A";
    const scriptConf = sd.script_confidence ?? "N/A";
    document.getElementById("ocr-summary-line").innerHTML =
      `<strong>Pages OCR'd:</strong> ${esc(pagesOcredStr)} · ` +
      `<strong>Script:</strong> ${esc(scriptName)} (conf: ${esc(scriptConf)})`;

    const wordsList = document.getElementById("ocr-words-list");
    wordsList.innerHTML = "";
    const wb = res.word_boxes || {};
    for (const [pg, words] of Object.entries(wb)) {
      for (const w of words) {
        const row = document.createElement("div");
        row.className = "list-row";
        row.innerHTML = `<span>p.${esc(pg)} <strong>"${esc(w.text)}"</strong> (${esc(w.x)}, ${esc(w.y)}) conf:${esc(w.confidence)}%</span><button class="btn btn-outline btn-xs">Edit Word</button>`;
        row.querySelector("button").addEventListener("click", () => {
          document.getElementById("ann-type").value = "text_edit";
          document.getElementById("ann-page").value = pg;
          document.getElementById("ann-x").value = w.x;
          document.getElementById("ann-y").value = w.y;
          document.getElementById("ann-w").value = w.width;
          document.getElementById("ann-h").value = w.height;
          document.getElementById("ann-text").value = w.text;
          document.querySelector('[data-tab="tab-annotate"]').click();
          toast("Word bounding box loaded into Text Edit annotation!", "info");
        });
        wordsList.appendChild(row);
      }
    }
    if (res.file_id) await selectFile(res.file_id);
  }
});

// ---------- Phase 5: Security, Redaction, Watermark, Forms ----------

document.getElementById("btn-sec-inspect").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const password = document.getElementById("sec-inspect-pw").value;
  try {
    const res = await api(`/pdf/${encodeURIComponent(state.activeFileId)}/security`, {
      method: "POST",
      json: { password },
    });
    const box = document.getElementById("sec-status-box");
    box.textContent = JSON.stringify(res, null, 2);
    box.classList.remove("hidden");
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-op-encrypt").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const user_password = document.getElementById("enc-user-pw").value;
  const owner_password = document.getElementById("enc-owner-pw").value || null;
  const allow_printing = document.getElementById("enc-allow-print").checked;
  const allow_copying = document.getElementById("enc-allow-copy").checked;
  const allow_modification = document.getElementById("enc-allow-mod").checked;
  const outId = await runJob("Encrypt PDF", api("/pdf/encrypt", {
    method: "POST",
    json: { file_id: state.activeFileId, user_password, owner_password, allow_printing, allow_copying, allow_modification },
  }));
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-decrypt").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const password = document.getElementById("dec-pw").value;
  const outId = await runJob("Decrypt PDF", api("/pdf/decrypt", {
    method: "POST",
    json: { file_id: state.activeFileId, password },
  }));
  if (outId) await selectFile(outId);
});

function renderRedactList() {
  const list = document.getElementById("redact-regions-list");
  list.innerHTML = "";
  state.redactRegions.forEach((r, idx) => {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<span>p.${esc(r.page)} [x:${esc(r.x)}, y:${esc(r.y)}, w:${esc(r.width)}, h:${esc(r.height)}]</span><button class="btn btn-ghost btn-xs">&times;</button>`;
    row.querySelector("button").addEventListener("click", () => {
      state.redactRegions.splice(idx, 1);
      renderRedactList();
    });
    list.appendChild(row);
  });
}

document.getElementById("btn-redact-add-canvas").addEventListener("click", () => {
  state.redactRegions.push(Object.assign({}, state.selectedCoords));
  renderRedactList();
});

document.getElementById("btn-redact-clear").addEventListener("click", () => {
  state.redactRegions = [];
  renderRedactList();
});

document.getElementById("btn-op-redact").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const regions = state.redactRegions.length ? state.redactRegions : [state.selectedCoords];
  const res = await runJob("True Redaction", api("/pdf/redact", {
    method: "POST",
    json: { file_id: state.activeFileId, regions },
  }));
  if (res && res.file_id) {
    state.redactRegions = [];
    renderRedactList();
    await selectFile(res.file_id);
  }
});

document.getElementById("btn-op-watermark").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const text = document.getElementById("wm-text").value;
  const opacity = parseFloat(document.getElementById("wm-opacity").value);
  const font_size = parseInt(document.getElementById("wm-size").value, 10);
  const color = document.getElementById("wm-color").value;
  const diagonal = document.getElementById("wm-diagonal").checked;
  const rawPages = document.getElementById("wm-pages").value.trim();
  const pages = rawPages ? parsePageList(rawPages) : null;
  const outId = await runJob("Watermark PDF", api("/pdf/watermark", {
    method: "POST",
    json: { file_id: state.activeFileId, text, opacity, font_size, color, diagonal, pages },
  }));
  if (outId) await selectFile(outId);
});

function renderQueuedFormFields() {
  const list = document.getElementById("form-queued-fields");
  list.innerHTML = "";
  state.queuedFormFields.forEach((f, idx) => {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<span><strong>${esc(f.name)}</strong> (${esc(f.type)}) p.${esc(f.page)} [${esc(f.x)},${esc(f.y)}]</span><button class="btn btn-ghost btn-xs">&times;</button>`;
    row.querySelector("button").addEventListener("click", () => {
      state.queuedFormFields.splice(idx, 1);
      renderQueuedFormFields();
    });
    list.appendChild(row);
  });
}

document.getElementById("btn-form-add-field").addEventListener("click", () => {
  const name = document.getElementById("form-field-name").value.trim();
  if (!name) return toast("Enter a field name", "error");
  const type = document.getElementById("form-field-type").value;
  state.queuedFormFields.push(Object.assign({ name, type }, state.selectedCoords));
  document.getElementById("form-field-name").value = "";
  renderQueuedFormFields();
});

document.getElementById("btn-op-form-create").addEventListener("click", async () => {
  if (!state.activeFileId || state.queuedFormFields.length === 0) return toast("Queue at least one field first", "error");
  const outId = await runJob("Create AcroForm", api("/forms/create", {
    method: "POST",
    json: { file_id: state.activeFileId, fields: state.queuedFormFields },
  }));
  state.queuedFormFields = [];
  renderQueuedFormFields();
  if (outId) await selectFile(outId);
});

document.getElementById("btn-op-form-read").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  try {
    const res = await api(`/forms/${encodeURIComponent(state.activeFileId)}/data`);
    const container = document.getElementById("form-live-fields");
    container.innerHTML = "";
    const fields = res.fields || {};
    const entries = Object.entries(fields);
    if (entries.length === 0) {
      container.innerHTML = `<div class="muted small">No AcroForm fields found in this PDF.</div>`;
      return;
    }
    for (const [k, v] of entries) {
      const row = document.createElement("div");
      row.className = "form-row";
      row.innerHTML = `<label class="small" style="min-width:110px;">${esc(k)}</label><input class="input input-sm form-fill-input" data-name="${esc(k)}" value="${esc(v ?? "")}" />`;
      container.appendChild(row);
    }
    const fillBtn = document.createElement("button");
    fillBtn.className = "btn btn-success btn-sm";
    fillBtn.textContent = "Fill & Save Form Values";
    fillBtn.addEventListener("click", async () => {
      const data = {};
      container.querySelectorAll(".form-fill-input").forEach((inp) => {
        data[inp.dataset.name] = inp.value;
      });
      const outId = await runJob("Fill Form", api("/forms/fill", {
        method: "POST",
        json: { file_id: state.activeFileId, data },
      }));
      if (outId) await selectFile(outId);
    });
    container.appendChild(fillBtn);
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- Phase 6: Documents, Collaboration, Shares, Comments ----------

async function loadDocuments() {
  try {
    const res = await api("/documents");
    const list = document.getElementById("docs-list");
    list.innerHTML = "";
    const allDocs = [
      ...(res.owned || []).map((d) => Object.assign({}, d, { role: "owner" })),
      ...(res.shared_with_me || []).map((d) => Object.assign({}, d, { role: d.your_permission })),
    ];
    if (allDocs.length === 0) {
      list.innerHTML = `<div class="muted small">No documents yet.</div>`;
      return;
    }
    for (const d of allDocs) {
      const row = document.createElement("div");
      row.className = "list-row";
      const docTitle = d.title || d.id.slice(0, 8);
      row.innerHTML = `
        <span><strong>${esc(docTitle)}</strong> (v${esc(d.current_version)}) · <span class="badge">${esc(d.role)}</span></span>
        <button class="btn btn-outline btn-xs">Open</button>
      `;
      row.querySelector("button").addEventListener("click", () => openDocument(d.id));
      list.appendChild(row);
    }
  } catch (_) {}
}

document.getElementById("btn-docs-refresh").addEventListener("click", loadDocuments);

document.getElementById("btn-doc-create").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const title = document.getElementById("doc-create-title").value.trim() || "Untitled Document";
  try {
    const res = await api("/documents", {
      method: "POST",
      json: { file_id: state.activeFileId, title },
    });
    toast("Document created at v1", "success");
    await loadDocuments();
    await openDocument(res.document_id);
  } catch (err) {
    toast(err.message, "error");
  }
});

async function openDocument(docId) {
  state.activeDocId = docId;
  try {
    const info = await api(`/documents/${encodeURIComponent(docId)}`);
    const versionsRes = await api(`/documents/${encodeURIComponent(docId)}/versions`);
    const collabsRes = await api(`/documents/${encodeURIComponent(docId)}/collaborators`);

    const panel = document.getElementById("active-doc-panel");
    panel.classList.remove("hidden");
    const lockStatus = info.document.locked_by ? " (Locked)" : " (Unlocked)";
    document.getElementById("active-doc-title").textContent =
      `${info.document.title || docId.slice(0, 8)} — v${info.document.current_version}${lockStatus}`;

    const vList = document.getElementById("doc-versions-list");
    vList.innerHTML = "";
    for (const v of versionsRes.versions || []) {
      const row = document.createElement("div");
      row.className = "list-row";
      const vLabel = v.label || "version";
      const shortFid = (v.file_id || "").slice(0, 8);
      row.innerHTML = `
        <span><strong>v${esc(v.version_number)}</strong> — ${esc(vLabel)} (${esc(shortFid)})</span>
        <div class="form-row">
          <button class="btn btn-ghost btn-xs btn-view-v">View</button>
          <button class="btn btn-outline btn-xs btn-rev-v">Revert to v${esc(v.version_number)}</button>
        </div>
      `;
      row.querySelector(".btn-view-v").addEventListener("click", () => selectFile(v.file_id));
      row.querySelector(".btn-rev-v").addEventListener("click", async () => {
        await api(`/documents/${encodeURIComponent(docId)}/revert`, {
          method: "POST",
          json: { version_number: v.version_number },
        });
        toast(`Reverted (appended new version pointing to v${v.version_number})`, "success");
        openDocument(docId);
        loadDocuments();
      });
      vList.appendChild(row);
    }

    const cList = document.getElementById("doc-collabs-list");
    cList.innerHTML = "";
    for (const c of collabsRes.collaborators || []) {
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<span>${esc(c.email)} (<span class="badge">${esc(c.permission)}</span>)</span><button class="btn btn-danger btn-xs">Remove</button>`;
      row.querySelector("button").addEventListener("click", async () => {
        await api(`/documents/${encodeURIComponent(docId)}/collaborators/${encodeURIComponent(c.user_id)}`, { method: "DELETE" });
        openDocument(docId);
      });
      cList.appendChild(row);
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

document.getElementById("btn-doc-add-version").addEventListener("click", async () => {
  if (!state.activeDocId || !state.activeFileId) return;
  const label = document.getElementById("doc-new-version-label").value.trim() || "updated version";
  try {
    await api(`/documents/${encodeURIComponent(state.activeDocId)}/versions`, {
      method: "POST",
      json: { file_id: state.activeFileId, label },
    });
    toast("Added new version", "success");
    openDocument(state.activeDocId);
    loadDocuments();
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-doc-lock").addEventListener("click", async () => {
  if (!state.activeDocId) return;
  try {
    await api(`/documents/${encodeURIComponent(state.activeDocId)}/lock`, { method: "POST", json: {} });
    toast("Document edit lock acquired", "success");
    openDocument(state.activeDocId);
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-doc-unlock").addEventListener("click", async () => {
  if (!state.activeDocId) return;
  try {
    await api(`/documents/${encodeURIComponent(state.activeDocId)}/unlock`, { method: "POST", json: {} });
    toast("Document edit lock released", "success");
    openDocument(state.activeDocId);
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-collab-add").addEventListener("click", async () => {
  if (!state.activeDocId) return;
  const email = document.getElementById("collab-email").value.trim();
  const permission = document.getElementById("collab-perm").value;
  try {
    await api(`/documents/${encodeURIComponent(state.activeDocId)}/collaborators`, {
      method: "POST",
      json: { email, permission },
    });
    toast(`Added ${email} (${permission})`, "success");
    openDocument(state.activeDocId);
  } catch (err) {
    toast(err.message, "error");
  }
});

async function loadShares() {
  if (!state.activeFileId) return;
  try {
    const res = await api(`/files/${encodeURIComponent(state.activeFileId)}/shares`);
    const list = document.getElementById("shares-list");
    list.innerHTML = "";
    for (const s of res.shares || []) {
      const row = document.createElement("div");
      row.className = "list-row";
      const shortTok = (s.token || "").slice(0, 12);
      const revText = s.revoked ? "(Revoked)" : "";
      row.innerHTML = `
        <span><code>${esc(shortTok)}...</code> · <span class="badge">${esc(s.permission)}</span> ${esc(revText)}</span>
        <div class="form-row">
          <button class="btn btn-outline btn-xs btn-open-portal">Open in Portal</button>
        </div>
      `;
      const actionBox = row.querySelector(".form-row");
      if (!s.revoked) {
        const revBtn = document.createElement("button");
        revBtn.className = "btn btn-danger btn-xs btn-revoke";
        revBtn.textContent = "Revoke";
        revBtn.addEventListener("click", async () => {
          await api(`/share/${encodeURIComponent(s.token)}`, { method: "DELETE" });
          loadShares();
        });
        actionBox.appendChild(revBtn);
      }
      row.querySelector(".btn-open-portal").addEventListener("click", () => {
        document.getElementById("public-share-token-input").value = s.token;
        document.getElementById("share-portal-modal").classList.remove("hidden");
        loadPublicShareToken(s.token);
      });
      list.appendChild(row);
    }
  } catch (_) {}
}

document.getElementById("btn-share-create").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const permission = document.getElementById("share-perm").value;
  const expRaw = document.getElementById("share-expiry").value.trim();
  const expires_in_hours = expRaw ? parseFloat(expRaw) : null;
  try {
    const res = await api("/share", {
      method: "POST",
      json: { file_id: state.activeFileId, permission, expires_in_hours },
    });
    toast(`Share link created: ${res.token}`, "success");
    loadShares();
  } catch (err) {
    toast(err.message, "error");
  }
});

async function loadComments() {
  if (!state.activeFileId) return;
  try {
    const res = await api(`/comments/${encodeURIComponent(state.activeFileId)}`);
    const list = document.getElementById("comments-list");
    list.innerHTML = "";
    for (const c of res.comments || []) {
      const row = document.createElement("div");
      row.className = "list-row";
      const statusMark = c.resolved ? " (Resolved)" : "";
      row.innerHTML = `
        <span><strong>${esc(c.author_label)}</strong>: ${esc(c.text)}${esc(statusMark)}</span>
        <div class="form-row"></div>
      `;
      const actionRow = row.querySelector(".form-row");
      if (!c.resolved) {
        const resBtn = document.createElement("button");
        resBtn.className = "btn btn-outline btn-xs";
        resBtn.textContent = "Resolve";
        resBtn.addEventListener("click", async () => {
          try {
            await api(`/comments/${encodeURIComponent(c.id)}/resolve`, { method: "POST", json: {} });
            loadComments();
          } catch (err) {
            toast(err.message, "error");
          }
        });
        actionRow.appendChild(resBtn);
      }
      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger btn-xs";
      delBtn.textContent = "Del";
      delBtn.addEventListener("click", async () => {
        try {
          await api(`/comments/${encodeURIComponent(c.id)}`, { method: "DELETE" });
          loadComments();
        } catch (err) {
          toast(err.message, "error");
        }
      });
      actionRow.appendChild(delBtn);
      list.appendChild(row);
    }
  } catch (_) {}
}

document.getElementById("btn-comment-add").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const text = document.getElementById("comment-text").value.trim();
  if (!text) return;
  const mentionsRaw = document.getElementById("comment-mentions").value.trim();
  const mentions = mentionsRaw ? mentionsRaw.split(",").map((s) => s.trim()).filter(Boolean) : [];
  try {
    await api("/comments", {
      method: "POST",
      json: { file_id: state.activeFileId, text, page: state.currentPage, mentions },
    });
    document.getElementById("comment-text").value = "";
    loadComments();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- Public Share Portal Modal ----------

document.getElementById("btn-public-share-portal").addEventListener("click", () => {
  document.getElementById("share-portal-modal").classList.remove("hidden");
});

document.getElementById("btn-close-share-portal").addEventListener("click", () => {
  document.getElementById("share-portal-modal").classList.add("hidden");
});

async function loadPublicShareToken(token) {
  state.activeShareToken = token;
  try {
    const info = await api(`/share/${encodeURIComponent(token)}`, { noAuth: true });
    document.getElementById("public-share-details").classList.remove("hidden");
    document.getElementById("pub-share-file-id").textContent = info.file_id;
    document.getElementById("pub-share-name").textContent = info.original_name || info.meta?.original_name || info.file_id;
    document.getElementById("pub-share-perm").textContent = info.permission;
    document.getElementById("pub-share-download-link").href = `/share/${encodeURIComponent(token)}/download`;

    const cRes = await api(`/share/${encodeURIComponent(token)}/comments`, { noAuth: true });
    const cList = document.getElementById("pub-share-comments-list");
    cList.innerHTML = "";
    for (const c of cRes.comments || []) {
      const div = document.createElement("div");
      div.className = "list-row";
      div.innerHTML = `<span><strong>${esc(c.author_label)}</strong>: ${esc(c.text)}</span>`;
      cList.appendChild(div);
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

document.getElementById("btn-load-public-share").addEventListener("click", () => {
  const token = document.getElementById("public-share-token-input").value.trim();
  if (token) loadPublicShareToken(token);
});

document.getElementById("btn-pub-post-comment").addEventListener("click", async () => {
  if (!state.activeShareToken) return;
  const author_name = document.getElementById("pub-comment-author").value.trim() || "Guest Reviewer";
  const text = document.getElementById("pub-comment-text").value.trim();
  const page = parseInt(document.getElementById("pub-comment-page").value, 10) || 1;
  const mentionsRaw = document.getElementById("pub-comment-mentions").value.trim();
  const mentions = mentionsRaw ? mentionsRaw.split(",").map((s) => s.trim()).filter(Boolean) : [];
  try {
    await api(`/share/${encodeURIComponent(state.activeShareToken)}/comments`, {
      method: "POST",
      json: { author_name, text, page, mentions },
      noAuth: true,
    });
    document.getElementById("pub-comment-text").value = "";
    toast("Public comment posted", "success");
    await loadPublicShareToken(state.activeShareToken);
    if (state.activeFileId) loadComments();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- Phase 7: Offline AI & PII ----------

document.getElementById("btn-ai-summarize").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const sentences = parseInt(document.getElementById("ai-sum-sentences").value, 10) || 3;
  const sumFileId = await runJob("AI Summarize", api("/ai/summarize", {
    method: "POST",
    json: { file_id: state.activeFileId, sentences },
  }));
  if (sumFileId) {
    const resp = await fetch(`/files/${encodeURIComponent(sumFileId)}?inline=1`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const text = await resp.text();
    const box = document.getElementById("ai-summary-output");
    box.textContent = text;
    box.classList.remove("hidden");
  }
});

document.getElementById("btn-ai-ask").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const question = document.getElementById("ai-question").value.trim();
  const top_k = parseInt(document.getElementById("ai-topk").value, 10) || 3;
  const res = await runJob("AI Document Q&A", api("/ai/ask", {
    method: "POST",
    json: { file_id: state.activeFileId, question, top_k },
  }));
  const list = document.getElementById("ai-matches-list");
  list.innerHTML = "";
  const matches = res?.matches || [];
  if (matches.length === 0) {
    list.innerHTML = `<div class="muted small">No matching sentences found.</div>`;
    return;
  }
  for (const m of matches) {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<span><strong>[p.${esc(m.page)} · score ${esc(m.score)}]</strong> ${esc(m.text)}</span>`;
    list.appendChild(row);
  }
});

document.getElementById("btn-ai-pii").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const findings = await runJob("Detect PII", api("/ai/detect-pii", {
    method: "POST",
    json: { file_id: state.activeFileId },
  }));
  state.detectedPii = findings || [];
  const list = document.getElementById("ai-pii-list");
  list.innerHTML = "";
  const redactBtn = document.getElementById("btn-ai-redact-all-pii");
  if (state.detectedPii.length === 0) {
    list.innerHTML = `<div class="muted small">No PII detected in this document.</div>`;
    redactBtn.classList.add("hidden");
    return;
  }
  redactBtn.classList.remove("hidden");
  for (const f of state.detectedPii) {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<span><span class="badge">${esc(f.type)}</span> p.${esc(f.page)}: <code>${esc(f.text)}</code> [${esc(f.x)}, ${esc(f.y)}]</span>`;
    list.appendChild(row);
  }
});

document.getElementById("btn-ai-redact-all-pii").addEventListener("click", async () => {
  if (!state.activeFileId || state.detectedPii.length === 0) return;
  const regions = state.detectedPii.map((f) => ({
    page: f.page,
    x: f.x,
    y: f.y,
    width: f.width,
    height: f.height,
  }));
  const res = await runJob("Redact All PII", api("/pdf/redact", {
    method: "POST",
    json: { file_id: state.activeFileId, regions },
  }));
  if (res && res.file_id) await selectFile(res.file_id);
});

// ---------- Phase 8: Metadata, Structure, Tables, Compare/Analyze & Batch ----------

async function loadMetadata() {
  if (!state.activeFileId) return;
  try {
    const m = await api(`/pdf/${encodeURIComponent(state.activeFileId)}/metadata`);
    document.getElementById("meta-title").value = m.title || "";
    document.getElementById("meta-author").value = m.author || "";
    document.getElementById("meta-subject").value = m.subject || "";
    document.getElementById("meta-keywords").value = m.keywords || "";
    document.getElementById("meta-creator").value = m.creator || "";
    document.getElementById("meta-custom").value = JSON.stringify(m.custom || {});
  } catch (err) {
    toast(err.message, "error");
  }
}

document.getElementById("btn-meta-load").addEventListener("click", loadMetadata);

document.getElementById("btn-meta-save").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const valOrNull = (id) => {
    const v = document.getElementById(id).value.trim();
    return v === "" ? null : v;
  };
  const fields = {
    title: valOrNull("meta-title"),
    author: valOrNull("meta-author"),
    subject: valOrNull("meta-subject"),
    keywords: valOrNull("meta-keywords"),
    creator: valOrNull("meta-creator"),
  };
  let custom = {};
  try {
    const rawC = document.getElementById("meta-custom").value.trim();
    if (rawC) custom = JSON.parse(rawC);
  } catch (_) {
    return toast("Custom metadata must be valid JSON", "error");
  }
  const outId = await runJob("Save Metadata", api("/pdf/metadata", {
    method: "POST",
    json: { file_id: state.activeFileId, fields, custom },
  }));
  if (outId) await selectFile(outId);
});

async function loadStructure() {
  if (!state.activeFileId) return;
  try {
    const bmRes = await api(`/pdf/${encodeURIComponent(state.activeFileId)}/bookmarks`);
    const bmList = document.getElementById("bm-list");
    bmList.innerHTML = "";
    for (const b of bmRes.bookmarks || []) {
      const div = document.createElement("div");
      div.className = "list-row";
      div.style.paddingLeft = `${8 + b.depth * 18}px`;
      div.textContent = `${"↳ ".repeat(b.depth)}${b.title}`;
      bmList.appendChild(div);
    }

    const lkRes = await api(`/pdf/${encodeURIComponent(state.activeFileId)}/hyperlinks`);
    const lkList = document.getElementById("links-list");
    lkList.innerHTML = "";
    for (const l of lkRes.hyperlinks || []) {
      const div = document.createElement("div");
      div.className = "list-row";
      div.textContent = `p.${l.page} ${l.url ? `URL: ${l.url}` : "Internal Page Dest"} [${l.rect.join(", ")}]`;
      lkList.appendChild(div);
    }
  } catch (_) {}
}

document.getElementById("btn-struct-load").addEventListener("click", loadStructure);

document.getElementById("btn-bm-add").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const title = document.getElementById("bm-title").value.trim();
  const page = parseInt(document.getElementById("bm-page").value, 10) || 1;
  const parent_title = document.getElementById("bm-parent").value.trim() || null;
  const outId = await runJob("Add Bookmark", api("/pdf/bookmarks", {
    method: "POST",
    json: { file_id: state.activeFileId, title, page, parent_title },
  }));
  if (outId) {
    await selectFile(outId);
    loadStructure();
  }
});

document.getElementById("btn-link-add").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const url = document.getElementById("link-url").value.trim() || null;
  const tpRaw = document.getElementById("link-target-page").value.trim();
  const target_page = tpRaw ? parseInt(tpRaw, 10) : null;
  const c = state.selectedCoords;
  const outId = await runJob("Add Hyperlink", api("/pdf/hyperlinks", {
    method: "POST",
    json: {
      file_id: state.activeFileId,
      page: c.page,
      x: c.x,
      y: c.y,
      width: c.width,
      height: c.height,
      url,
      target_page,
    },
  }));
  if (outId) {
    await selectFile(outId);
    loadStructure();
  }
});

document.getElementById("btn-op-tables").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const format = document.getElementById("table-fmt").value;
  const res = await runJob(`Extract Tables (${format})`, api("/pdf/extract-tables", {
    method: "POST",
    json: { file_id: state.activeFileId, format },
  }));
  if (res) {
    const out = document.getElementById("analysis-output");
    out.textContent = `Extracted ${res.tables_found} table(s) into file ${res.file_id}`;
    out.classList.remove("hidden");
  }
});

document.getElementById("btn-op-analyze").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  try {
    const res = await api(`/pdf/${encodeURIComponent(state.activeFileId)}/analyze`);
    const out = document.getElementById("analysis-output");
    out.textContent = `Pages: ${res.page_count} | Words: ${res.word_count} | Sentences: ${res.sentence_count} | Flesch Ease: ${res.flesch_reading_ease ?? "n/a"} (${res.reading_level})`;
    out.classList.remove("hidden");
  } catch (err) {
    toast(err.message, "error");
  }
});

document.getElementById("btn-op-duplicates").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const res = await runJob("Find Duplicate Pages", api("/pdf/find-duplicate-pages", {
    method: "POST",
    json: { file_id: state.activeFileId },
  }));
  if (res) {
    const out = document.getElementById("analysis-output");
    out.textContent = `Total Pages: ${res.total_pages} | Duplicate Groups: ${JSON.stringify(res.duplicate_groups)}`;
    out.classList.remove("hidden");
  }
});

document.getElementById("btn-op-compare").addEventListener("click", async () => {
  if (!state.activeFileId) return;
  const file_id_b = document.getElementById("compare-file-b").value;
  if (!file_id_b) return;
  const res = await runJob("Compare PDFs", api("/pdf/compare", {
    method: "POST",
    json: { file_id_a: state.activeFileId, file_id_b },
  }));
  if (res) {
    const box = document.getElementById("compare-diff-output");
    box.textContent =
      `Similarity: ${(res.similarity * 100).toFixed(1)}% | Words: ${res.words_a} vs ${res.words_b} | +${res.lines_added} / -${res.lines_removed} lines\n\n` +
      (res.diff || []).join("\n");
    box.classList.remove("hidden");
  }
});

document.getElementById("btn-op-batch").addEventListener("click", async () => {
  const file_ids = Array.from(state.checkedFileIds);
  if (file_ids.length === 0) return toast("Check 1 or more files in the sidebar first", "error");
  const operation = document.getElementById("batch-op").value;
  let params = {};
  try {
    params = JSON.parse(document.getElementById("batch-params").value || "{}");
  } catch (_) {
    return toast("Batch params must be valid JSON", "error");
  }
  try {
    const res = await api("/batch", {
      method: "POST",
      json: { operation, file_ids, params },
    });
    const list = document.getElementById("batch-results");
    list.innerHTML = "";
    for (const [fid, jid] of Object.entries(res.job_ids || {})) {
      const result = await runJob(`Batch ${operation} (${fid.slice(0, 6)})`, Promise.resolve({ job_id: jid }));
      const row = document.createElement("div");
      row.className = "list-row";
      row.textContent = `${fid.slice(0, 8)} → ${typeof result === "string" ? result : JSON.stringify(result)}`;
      list.appendChild(row);
    }
  } catch (err) {
    toast(err.message, "error");
  }
});

// Boot
checkSession();
