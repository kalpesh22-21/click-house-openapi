/**
 * CSV-to-ClickHouse Ingestion UI
 * Single-page step wizard. No frameworks, no modules, no external deps.
 *
 * Steps:
 *   1 — Connection & CSV form  → POST /admin/analyze
 *   2 — Decision gate          (branch: create / append / wipe+create / abort)
 *   3 — Column type editor     (mode: create | wipe)
 *   4 — Result display
 */

/* ============================================================
   Column editor state — populated by renderStep3, read by ingest
   ============================================================ */
let columnState = [];

/* ============================================================
   App state — single source of truth
   ============================================================ */
const state = {
  currentStep: 1,

  // Form values captured from Step 1
  conn: {
    host: "",
    port: 8443,
    user: "",
    password: "",
    secure: true,
    database: "",
    table: "",
  },

  // Service bearer token for Authorization header
  apiKey: "",

  // The File object from the <input type="file"> picker.
  // Kept alive so we can re-send it on /admin/ingest.
  csvFile: null,

  // Full response from /admin/analyze
  analyzeResult: null,

  // Ingestion mode chosen in Step 2: "create" | "wipe" | "append"
  ingestMode: null,
};

/* ============================================================
   DOM references — resolved after DOMContentLoaded
   ============================================================ */
let dom = {};

/* ============================================================
   Utility helpers
   ============================================================ */

/**
 * Show or hide an element by toggling the "hidden" class.
 * @param {HTMLElement} el
 * @param {boolean} visible
 */
function setVisible(el, visible) {
  if (visible) {
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

/**
 * Clear all field-level validation messages inside a container.
 * @param {HTMLElement} container
 */
function clearFieldErrors(container) {
  container.querySelectorAll(".field-error").forEach((el) => el.remove());
  container.querySelectorAll(".error").forEach((el) => el.classList.remove("error"));
}

/**
 * Attach an inline error message below a field.
 * @param {HTMLElement} field
 * @param {string} message
 */
function showFieldError(field, message) {
  field.classList.add("error");
  const err = document.createElement("span");
  err.className = "field-error";
  err.setAttribute("role", "alert");
  err.textContent = message;
  field.insertAdjacentElement("afterend", err);
}

/**
 * Show a banner (error/info/success/warning) inside a container element.
 * Replaces any existing banner in that container.
 * @param {HTMLElement} container
 * @param {string} message
 * @param {"error"|"info"|"success"|"warning"} type
 */
function showBanner(container, message, type = "error") {
  // Remove existing banner
  const existing = container.querySelector(".banner");
  if (existing) existing.remove();

  const banner = document.createElement("div");
  banner.className = `banner banner-${type}`;
  banner.setAttribute("role", type === "error" ? "alert" : "status");
  banner.textContent = message;
  container.prepend(banner);
}

/**
 * Remove any existing banner from a container.
 * @param {HTMLElement} container
 */
function clearBanner(container) {
  const existing = container.querySelector(".banner");
  if (existing) existing.remove();
}

/**
 * Set a button into loading / idle state.
 * @param {HTMLButtonElement} btn
 * @param {boolean} loading
 * @param {string} [loadingText]
 */
function setButtonLoading(btn, loading, loadingText = "Loading…") {
  if (loading) {
    btn.disabled = true;
    btn._originalHTML = btn.innerHTML;
    btn.innerHTML = `<span class="spinner" aria-hidden="true"></span> ${loadingText}`;
  } else {
    btn.disabled = false;
    if (btn._originalHTML !== undefined) {
      btn.innerHTML = btn._originalHTML;
    }
  }
}

/**
 * POST a FormData payload and return the parsed JSON body.
 * Throws an Error with a user-readable message on non-2xx responses.
 * Sends Authorization: Bearer <apiKey> when an apiKey is provided.
 * Do NOT pass Content-Type — the browser must set the multipart boundary.
 * @param {string} url
 * @param {FormData} formData
 * @param {string} apiKey
 * @returns {Promise<object>}
 */
async function postFormData(url, formData, apiKey) {
  const headers = {};
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }

  const response = await fetch(url, {
    method: "POST",
    headers,
    body: formData,
  });

  if (response.status === 401 || response.status === 403) {
    throw new Error("Authentication failed — check your API key.");
  }

  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error(`Server returned ${response.status} with non-JSON body.`);
  }

  if (!response.ok) {
    const detail = body?.detail ?? body?.error ?? JSON.stringify(body);
    throw new Error(detail);
  }

  return body;
}

/* ============================================================
   Stepper UI
   ============================================================ */

/**
 * Update the visual stepper to reflect the current step.
 * Steps before current are marked "done", current is "active".
 * @param {number} step  1–4
 */
function updateStepper(step) {
  const items = dom.stepper.querySelectorAll(".step-item");
  const connectors = dom.stepper.querySelectorAll(".step-connector");

  items.forEach((item, i) => {
    const n = i + 1; // steps are 1-indexed
    item.classList.remove("active", "done");
    if (n < step) item.classList.add("done");
    else if (n === step) item.classList.add("active");
  });

  connectors.forEach((conn, i) => {
    const n = i + 1; // connector i sits between step i+1 and i+2
    conn.classList.remove("done");
    if (n < step) conn.classList.add("done");
  });
}

/**
 * Show only the panel for the given step; hide all others.
 * @param {number} step
 */
function showStep(step) {
  state.currentStep = step;
  updateStepper(step);

  // Hide all step panels
  dom.step1Panel.classList.add("hidden");
  dom.step2Panel.classList.add("hidden");
  dom.step3Panel.classList.add("hidden");
  dom.step4Panel.classList.add("hidden");

  // Show the target panel
  const panels = [null, dom.step1Panel, dom.step2Panel, dom.step3Panel, dom.step4Panel];
  panels[step].classList.remove("hidden");

  // Scroll card into view for smaller screens
  panels[step].scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ============================================================
   Step 1 — Connection & CSV form
   ============================================================ */

function initStep1() {
  // Restore any saved connection values
  dom.host.value = state.conn.host;
  dom.port.value = state.conn.port;
  dom.user.value = state.conn.user;
  dom.password.value = state.conn.password;
  dom.secureCheck.checked = state.conn.secure;
  dom.database.value = state.conn.database;
  dom.table.value = state.conn.table;
  dom.apiKey.value = state.apiKey;
  // Do NOT restore the file picker — browser security prevents it.

  clearBanner(dom.step1Panel);
  clearFieldErrors(dom.step1Panel);

  dom.analyzeBtn.addEventListener("click", handleAnalyze);
}

/**
 * Validate Step 1 form fields. Returns true if all required fields are filled.
 */
function validateStep1() {
  clearFieldErrors(dom.step1Panel);
  let valid = true;

  const required = [
    { el: dom.host, label: "Host" },
    { el: dom.port, label: "Port" },
    { el: dom.user, label: "User" },
    { el: dom.database, label: "Database" },
    { el: dom.table, label: "Table" },
    { el: dom.apiKey, label: "API Key" },
  ];

  for (const { el, label } of required) {
    if (!el.value.trim()) {
      showFieldError(el, `${label} is required.`);
      valid = false;
    }
  }

  if (!dom.csvFile.files[0]) {
    showFieldError(dom.csvFile, "Please select a CSV file.");
    valid = false;
  }

  return valid;
}

/**
 * Handle the "Analyze" button click.
 * Captures form state, sends POST /admin/analyze, then advances to Step 2.
 */
async function handleAnalyze() {
  if (!validateStep1()) return;

  // Snapshot connection state
  state.conn = {
    host: dom.host.value.trim(),
    port: parseInt(dom.port.value, 10),
    user: dom.user.value.trim(),
    password: dom.password.value,
    secure: dom.secureCheck.checked,
    database: dom.database.value.trim(),
    table: dom.table.value.trim(),
  };
  state.apiKey = dom.apiKey.value;

  // Keep a reference to the File object for later re-use on /admin/ingest
  state.csvFile = dom.csvFile.files[0];

  clearBanner(dom.step1Panel);
  setButtonLoading(dom.analyzeBtn, true, "Analyzing…");

  const fd = buildConnectionFormData();
  fd.append("file", state.csvFile);

  try {
    const result = await postFormData("/admin/analyze", fd, state.apiKey);
    state.analyzeResult = result;
    renderStep2(result);
    showStep(2);
  } catch (err) {
    showBanner(dom.step1Panel, `Analysis failed: ${err.message}`, "error");
  } finally {
    setButtonLoading(dom.analyzeBtn, false);
  }
}

/* ============================================================
   Step 2 — Decision gate
   ============================================================ */

/**
 * Build and display the Step 2 decision panel based on analyze response.
 * @param {object} result  — the /admin/analyze response body
 */
function renderStep2(result) {
  // Clear previous content in the dynamic area
  dom.step2Content.innerHTML = "";
  clearBanner(dom.step2Panel);

  const { table_exists, schema_matches, schema_diff, columns } = result;

  // ------------------------------------------------------------------
  // Branch A: Table does not exist → create
  // ------------------------------------------------------------------
  if (!table_exists) {
    state.ingestMode = "create";

    dom.step2Content.innerHTML = `
      <div class="banner banner-info" role="status">
        Table <strong>${escHtml(state.conn.database)}.${escHtml(state.conn.table)}</strong>
        does not exist — it will be <strong>created</strong>.
      </div>
      <p class="text-muted" style="font-size:0.875rem;">
        Review the inferred column types in the next step before ingesting.
      </p>
    `;

    const btn = makeButton("Continue to Column Editor", "btn-primary");
    btn.addEventListener("click", () => {
      renderStep3((columns || []).filter((c) => c.in_csv), "create");
      showStep(3);
    });
    dom.step2Content.appendChild(makeBtnGroup([btn]));

  // ------------------------------------------------------------------
  // Branch B: Table exists, schema matches → append
  // ------------------------------------------------------------------
  } else if (table_exists && schema_matches) {
    state.ingestMode = "append";

    dom.step2Content.innerHTML = `
      <div class="banner banner-success" role="status">
        Table <strong>${escHtml(state.conn.database)}.${escHtml(state.conn.table)}</strong>
        exists and the schema <strong>matches</strong>.
      </div>
    `;

    // Timestamp column picker.
    // The option VALUE is the sanitized column name (what the table actually
    // uses); the label shows the original CSV header so it's recognizable.
    const tsCols = (columns || []).filter((c) => c.in_csv);
    const tsGroup = document.createElement("div");
    tsGroup.className = "form-group mt-2";
    tsGroup.innerHTML = `
      <label for="ts-col-select">
        Timestamp column <span class="required">*</span>
      </label>
      <select id="ts-col-select" aria-required="true">
        <option value="">— Select a column —</option>
        ${tsCols
          .map((c) => {
            const label = c.original_name && c.original_name !== c.name
              ? `${escHtml(c.original_name)} → ${escHtml(c.name)}`
              : escHtml(c.name);
            return `<option value="${escHtml(c.name)}">${label}</option>`;
          })
          .join("")}
      </select>
      <small>Only rows with a value newer than the table's current maximum for this column will be inserted.</small>
    `;
    dom.step2Content.appendChild(tsGroup);

    const tsSelect = tsGroup.querySelector("#ts-col-select");

    // Pre-select a DateTime-looking column if one exists.
    const dtCol = tsCols.find((c) => {
      const t = c.existing_type || c.inferred_type || "";
      return t.includes("DateTime") || t.includes("Date");
    });
    if (dtCol) tsSelect.value = dtCol.name;

    const ingestBtn = makeButton("Append new data only", "btn-success");
    ingestBtn.addEventListener("click", async () => {
      const tsCol = tsSelect.value;
      if (!tsCol) {
        showBanner(dom.step2Panel, "Please select a timestamp column before ingesting.", "error");
        return;
      }
      clearBanner(dom.step2Panel);
      await handleIngest({ mode: "append", timestamp_column: tsCol }, ingestBtn);
    });

    dom.step2Content.appendChild(makeBtnGroup([ingestBtn]));

  // ------------------------------------------------------------------
  // Branch C: Table exists, schema DOES NOT match → wipe or abort
  // ------------------------------------------------------------------
  } else if (table_exists && !schema_matches) {
    dom.step2Content.innerHTML = `
      <div class="banner banner-warning" role="alert">
        Schema does <strong>NOT</strong> match for
        <strong>${escHtml(state.conn.database)}.${escHtml(state.conn.table)}</strong>.
        Review the differences below.
      </div>
    `;

    // Render schema diff
    dom.step2Content.appendChild(renderSchemaDiff(schema_diff));

    // Buttons
    const wipeBtn = makeButton("Wipe table &amp; re-create", "btn-danger");
    wipeBtn.innerHTML = "Wipe table &amp; re-create";
    wipeBtn.addEventListener("click", () => {
      state.ingestMode = "wipe";
      renderStep3((columns || []).filter((c) => c.in_csv), "wipe");
      showStep(3);
    });

    const abortBtn = makeButton("Abort", "btn-secondary");
    abortBtn.addEventListener("click", resetToStep1);

    dom.step2Content.appendChild(makeBtnGroup([wipeBtn, abortBtn]));
  }
}

/**
 * Build a DOM subtree representing the schema_diff object.
 * @param {object} diff
 * @returns {HTMLElement}
 */
function renderSchemaDiff(diff) {
  const wrap = document.createElement("div");

  if (!diff) {
    wrap.innerHTML = `<p class="text-muted">No diff information available.</p>`;
    return wrap;
  }

  const { missing_in_csv = [], extra_in_csv = [], type_mismatches = [] } = diff;

  if (missing_in_csv.length === 0 && extra_in_csv.length === 0 && type_mismatches.length === 0) {
    wrap.innerHTML = `<p class="text-muted">No structural differences reported.</p>`;
    return wrap;
  }

  // Helper to build a small list section
  const buildSection = (title, tagClass, items, columns, renderRow) => {
    if (!items.length) return "";
    return `
      <div class="diff-section">
        <h3>
          <span class="diff-tag ${tagClass}">${escHtml(title)}</span>
        </h3>
        <table class="data-table" role="table" aria-label="${escHtml(title)}">
          <thead>
            <tr>${columns.map((c) => `<th scope="col">${escHtml(c)}</th>`).join("")}</tr>
          </thead>
          <tbody>
            ${items.map(renderRow).join("")}
          </tbody>
        </table>
      </div>
    `;
  };

  wrap.innerHTML =
    buildSection(
      "Columns in table but missing from CSV",
      "diff-tag-missing",
      missing_in_csv,
      ["Column name"],
      (col) => `<tr><td class="mono">${escHtml(col)}</td></tr>`
    ) +
    buildSection(
      "Columns in CSV but not in table",
      "diff-tag-extra",
      extra_in_csv,
      ["Column name"],
      (col) => `<tr><td class="mono">${escHtml(col)}</td></tr>`
    ) +
    buildSection(
      "Type mismatches",
      "diff-tag-mismatch",
      type_mismatches,
      ["Column", "CSV type", "Table type"],
      (m) => `
        <tr>
          <td class="mono">${escHtml(m.name)}</td>
          <td class="mono">${escHtml(m.csv_type)}</td>
          <td class="mono">${escHtml(m.table_type)}</td>
        </tr>`
    );

  return wrap;
}

/* ============================================================
   Step 3 — Column type editor (create & wipe only)
   ============================================================ */

/**
 * Build and display the column type editor table and ORDER BY selector.
 * Populates the module-level columnState array from the analyze response.
 * @param {Array<{name:string, original_name:string, inferred_type:string, description:string}>} inferredColumns
 * @param {"create"|"wipe"} mode
 */
function renderStep3(inferredColumns, mode) {
  dom.step3Content.innerHTML = "";
  clearBanner(dom.step3Panel);

  // Seed columnState from the backend's analyze response.
  // Backend contract: each entry has name (sanitized), original_name (raw header),
  // inferred_type, and description (existing column comment or "").
  columnState = inferredColumns.map((col) => ({
    name: col.name,
    originalName: col.original_name || col.name,
    type: col.inferred_type || col.suggested_type || "",
    orderBy: false,
    description: col.description || "",
  }));

  // Heading annotation
  const modeLabel = mode === "wipe" ? "Wipe &amp; Re-create" : "Create";
  const headingNote = document.createElement("p");
  headingNote.className = "text-muted mb-2";
  headingNote.style.fontSize = "0.875rem";
  headingNote.innerHTML = `Mode: <strong>${modeLabel}</strong>. Edit ClickHouse types as needed before ingesting.`;
  dom.step3Content.appendChild(headingNote);

  // ---- Column type table ----
  const tableWrap = document.createElement("div");
  tableWrap.style.overflowX = "auto";

  const table = document.createElement("table");
  table.className = "data-table col-editor-table";
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Column type editor");
  table.innerHTML = `
    <thead>
      <tr>
        <th scope="col">Column name</th>
        <th scope="col">ClickHouse type <small>(editable)</small></th>
        <th scope="col">Description</th>
      </tr>
    </thead>
    <tbody id="col-editor-tbody"></tbody>
  `;
  tableWrap.appendChild(table);
  dom.step3Content.appendChild(tableWrap);

  const tbody = table.querySelector("#col-editor-tbody");

  columnState.forEach((col, idx) => {
    const tr = document.createElement("tr");
    const typeInputId = `col-type-${sanitizeId(col.name)}-${idx}`;
    const showHint = col.originalName !== col.name;
    tr.innerHTML = `
      <td>
        <input
          type="text"
          class="name-input"
          data-idx="${idx}"
          value="${escHtml(col.name)}"
          aria-label="Column name for index ${idx}"
          spellcheck="false"
          autocomplete="off"
        />${showHint ? `<div class="orig-hint">from: ${escHtml(col.originalName)}</div>` : ""}
      </td>
      <td>
        <input
          type="text"
          id="${typeInputId}"
          class="type-input"
          data-idx="${idx}"
          value="${escHtml(col.type)}"
          aria-label="ClickHouse type for column ${escHtml(col.name)}"
          spellcheck="false"
          autocomplete="off"
        />
      </td>
      <td>
        <input
          type="text"
          class="desc-input"
          data-idx="${idx}"
          placeholder="optional"
          value="${escHtml(col.description)}"
          aria-label="Description for column ${escHtml(col.name)}"
          autocomplete="off"
        />
      </td>
    `;
    tbody.appendChild(tr);
  });

  // Wire input listeners to keep columnState in sync with edits
  tbody.addEventListener("input", (e) => {
    const idx = parseInt(e.target.dataset.idx, 10);
    if (isNaN(idx)) return;
    if (e.target.classList.contains("name-input")) {
      columnState[idx].name = e.target.value;
    } else if (e.target.classList.contains("type-input")) {
      columnState[idx].type = e.target.value;
    } else if (e.target.classList.contains("desc-input")) {
      columnState[idx].description = e.target.value;
    }
  });

  // ---- ORDER BY selector ----
  const orderBySection = document.createElement("div");
  orderBySection.className = "form-group mt-3";
  orderBySection.innerHTML = `
    <label>ORDER BY columns</label>
    <small>
      Select one or more columns to define the primary sort key.
      If none are selected, <span class="mono">ORDER BY tuple()</span> is used.
    </small>
    <div class="orderby-grid" id="orderby-grid" role="group" aria-label="ORDER BY column selection"></div>
  `;
  dom.step3Content.appendChild(orderBySection);

  const orderByGrid = orderBySection.querySelector("#orderby-grid");
  columnState.forEach((col, idx) => {
    const checkId = `ob-${sanitizeId(col.name)}-${idx}`;
    // Default-check if the type looks like a date/time column
    const isDateTime = col.type.includes("DateTime") || col.type.includes("Date");

    const label = document.createElement("label");
    label.className = "orderby-item";
    label.htmlFor = checkId;
    label.innerHTML = `
      <input type="checkbox" id="${checkId}" data-idx="${idx}"${isDateTime ? " checked" : ""} />
      <span>${escHtml(col.name)}</span>
    `;
    orderByGrid.appendChild(label);

    // Sync initial checked state into columnState
    if (isDateTime) columnState[idx].orderBy = true;
  });

  // Wire ORDER BY checkboxes to columnState
  orderByGrid.addEventListener("change", (e) => {
    if (e.target.type !== "checkbox") return;
    const idx = parseInt(e.target.dataset.idx, 10);
    if (!isNaN(idx)) columnState[idx].orderBy = e.target.checked;
  });

  // ---- Ingest button ----
  const ingestBtn = makeButton("Ingest", "btn-primary");
  ingestBtn.addEventListener("click", async () => {
    // Build columns payload from current columnState
    const columns = columnState.map((c) => ({
      name: c.name.trim(),
      type: c.type,
      description: (c.description || "").trim(),
    }));

    // Client-side guard: reject empty or invalid column names
    const namePattern = /^[A-Za-z_][A-Za-z0-9_]*$/;
    for (const col of columns) {
      if (!col.name) {
        showBanner(dom.step3Panel, "Column names must not be empty.", "error");
        return;
      }
      if (!namePattern.test(col.name)) {
        showBanner(
          dom.step3Panel,
          `Invalid column name "${col.name}". Names must start with a letter or underscore and contain only letters, digits, and underscores.`,
          "error"
        );
        return;
      }
    }

    // Validate types are non-empty
    if (columns.some((c) => !c.type)) {
      showBanner(dom.step3Panel, "All column types must be non-empty.", "error");
      return;
    }

    const orderBy = columnState.filter((c) => c.orderBy).map((c) => c.name.trim());

    clearBanner(dom.step3Panel);
    await handleIngest(
      {
        mode,
        columns: JSON.stringify(columns),
        order_by: JSON.stringify(orderBy),
      },
      ingestBtn
    );
  });

  dom.step3Content.appendChild(makeBtnGroup([ingestBtn]));
}

/* ============================================================
   Ingest (shared handler for Steps 2 & 3)
   ============================================================ */

/**
 * POST /admin/ingest with connection fields + extras.
 * On success renders Step 4. On error shows a banner in the current step panel.
 * @param {object} extras   Additional form fields to append (mode, columns, order_by, etc.)
 * @param {HTMLButtonElement} triggerBtn  The button that fired the action (for loading state)
 */
async function handleIngest(extras, triggerBtn) {
  const currentPanel =
    state.currentStep === 2 ? dom.step2Panel : dom.step3Panel;

  setButtonLoading(triggerBtn, true, "Ingesting…");

  const fd = buildConnectionFormData();
  fd.append("file", state.csvFile);

  for (const [key, val] of Object.entries(extras)) {
    if (val !== undefined && val !== null) {
      fd.append(key, val);
    }
  }

  try {
    const result = await postFormData("/admin/ingest", fd, state.apiKey);
    renderStep4(result);
    showStep(4);
  } catch (err) {
    showBanner(currentPanel, `Ingest failed: ${err.message}`, "error");
  } finally {
    setButtonLoading(triggerBtn, false);
  }
}

/* ============================================================
   Step 4 — Result display
   ============================================================ */

/**
 * Render the result panel from /admin/ingest response.
 * @param {object} result
 */
function renderStep4(result) {
  dom.step4Content.innerHTML = "";

  const { mode, table, rows_in_csv, rows_inserted, rows_skipped, watermark } = result;

  // Success banner
  const banner = document.createElement("div");
  banner.className = "banner banner-success";
  banner.setAttribute("role", "status");
  banner.innerHTML = `Ingestion complete. Mode: <strong>${escHtml(mode)}</strong> into <strong class="mono">${escHtml(table)}</strong>.`;
  dom.step4Content.appendChild(banner);

  // Stats grid
  const grid = document.createElement("div");
  grid.className = "result-grid";
  grid.innerHTML = `
    <div class="result-stat">
      <div class="stat-label">Rows in CSV</div>
      <div class="stat-value">${escHtml(String(rows_in_csv ?? "—"))}</div>
    </div>
    <div class="result-stat">
      <div class="stat-label">Rows inserted</div>
      <div class="stat-value text-success">${escHtml(String(rows_inserted ?? "—"))}</div>
    </div>
    <div class="result-stat">
      <div class="stat-label">Rows skipped</div>
      <div class="stat-value">${escHtml(String(rows_skipped ?? "—"))}</div>
    </div>
    <div class="result-stat">
      <div class="stat-label">Mode</div>
      <div class="stat-value" style="font-size:1rem;">${escHtml(mode ?? "—")}</div>
    </div>
  `;
  dom.step4Content.appendChild(grid);

  // Watermark (only shown if present)
  if (watermark) {
    const wm = document.createElement("div");
    wm.className = "watermark-row";
    wm.innerHTML = `<strong>Watermark:</strong> <span class="mono">${escHtml(String(watermark))}</span>`;
    dom.step4Content.appendChild(wm);
  }

  // Start over button
  const startOverBtn = makeButton("Start over", "btn-secondary");
  startOverBtn.addEventListener("click", resetToStep1);
  dom.step4Content.appendChild(makeBtnGroup([startOverBtn]));
}

/* ============================================================
   Reset
   ============================================================ */

/**
 * Reset all state and return to Step 1.
 * Clears the file picker (the only input that cannot be pre-filled by JS).
 */
function resetToStep1() {
  state.analyzeResult = null;
  state.ingestMode = null;
  state.csvFile = null;

  // Clear the file picker
  dom.csvFile.value = "";

  // Clear banners and errors on Step 1
  clearBanner(dom.step1Panel);
  clearFieldErrors(dom.step1Panel);

  showStep(1);
}

/* ============================================================
   FormData builder
   ============================================================ */

/**
 * Build a FormData with the connection fields from app state.
 * @returns {FormData}
 */
function buildConnectionFormData() {
  const fd = new FormData();
  fd.append("host", state.conn.host);
  fd.append("port", String(state.conn.port));
  fd.append("user", state.conn.user);
  fd.append("password", state.conn.password);
  fd.append("secure", state.conn.secure ? "true" : "false");
  fd.append("database", state.conn.database);
  fd.append("table", state.conn.table);
  return fd;
}

/* ============================================================
   DOM factories
   ============================================================ */

/**
 * Create a <button> element.
 * @param {string} labelHtml  Inner HTML for the button label.
 * @param {string} cssClass   CSS class string (e.g. "btn-primary").
 * @returns {HTMLButtonElement}
 */
function makeButton(labelHtml, cssClass) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `btn ${cssClass}`;
  btn.innerHTML = labelHtml;
  return btn;
}

/**
 * Wrap buttons in a .btn-group div.
 * @param {HTMLButtonElement[]} buttons
 * @returns {HTMLDivElement}
 */
function makeBtnGroup(buttons) {
  const group = document.createElement("div");
  group.className = "btn-group";
  buttons.forEach((b) => group.appendChild(b));
  return group;
}

/* ============================================================
   Security helpers
   ============================================================ */

/**
 * Escape a string for safe insertion as text content in HTML.
 * Prevents XSS when displaying server-returned strings.
 * @param {string} str
 * @returns {string}
 */
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Sanitize a string for use as an HTML id attribute value.
 * Replaces non-alphanumeric characters with underscores.
 * @param {string} str
 * @returns {string}
 */
function sanitizeId(str) {
  return String(str).replace(/[^a-zA-Z0-9_-]/g, "_");
}

/* ============================================================
   Bootstrap
   ============================================================ */

document.addEventListener("DOMContentLoaded", () => {
  // Resolve all DOM references once
  dom = {
    stepper: document.getElementById("stepper"),

    step1Panel: document.getElementById("step1"),
    step2Panel: document.getElementById("step2"),
    step3Panel: document.getElementById("step3"),
    step4Panel: document.getElementById("step4"),

    // Step 1 form fields
    host: document.getElementById("host"),
    port: document.getElementById("port"),
    user: document.getElementById("user"),
    password: document.getElementById("password"),
    secureCheck: document.getElementById("secure"),
    database: document.getElementById("database"),
    table: document.getElementById("table"),
    apiKey: document.getElementById("api-key"),
    csvFile: document.getElementById("csv-file"),
    analyzeBtn: document.getElementById("analyze-btn"),

    // Step 2 & 3 dynamic content areas
    step2Content: document.getElementById("step2-content"),
    step3Content: document.getElementById("step3-content"),
    step4Content: document.getElementById("step4-content"),
  };

  // Wire up Step 1
  initStep1();

  // Show Step 1 to start
  showStep(1);
});
