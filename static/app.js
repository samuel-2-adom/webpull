/* ============================================================
   WebPull — Frontend JavaScript
   Wizard logic, job polling, live log, table preview,
   AI format assist via Groq.
   ============================================================ */

"use strict";

// ============================================================
// STATE
// ============================================================

const state = {
  url        : "",
  jobId      : null,       // detection job
  scrapeJobId: null,       // scrape job
  detection  : null,       // site detection result
  authMethod : "none",
  pollTimer  : null,
  currentStep: 0,
};


// ============================================================
// DOM REFS
// ============================================================

const $ = id => document.getElementById(id);

const hero    = $("hero");
const wizard  = $("wizard");
const urlInput = $("urlInput");
const startBtn = $("startBtn");


// ============================================================
// METRICS
// ============================================================

async function loadMetrics() {
  try {
    const res  = await fetch("/api/metrics");
    const data = await res.json();
    const el   = document.getElementById("metricsCounter");
    if (el && (data.total_scrapes > 0)) {
      el.textContent = `⬇ ${data.total_scrapes.toLocaleString()} scrapes run  ·  ${data.unique_urls.toLocaleString()} unique URLs`;
      el.style.display = "block";
    }
  } catch (e) {
    // metrics unavailable — silent fail
  }
}


// ============================================================
// INIT
// ============================================================

document.addEventListener("DOMContentLoaded", () => {
  // Load metrics counter
  loadMetrics();

  // URL input — start on Enter
  urlInput.addEventListener("keydown", e => {
    if (e.key === "Enter") startScrape();
  });

  startBtn.addEventListener("click", startScrape);

  // Auth method selection
  document.querySelectorAll(".option-card").forEach(card => {
    card.addEventListener("click", () => {
      document.querySelectorAll(".option-card")
        .forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
      card.querySelector("input").checked = true;
      state.authMethod = card.dataset.auth;
      toggleCredentialsForm(state.authMethod);
    });
  });

  // Extraction checkboxes
  document.querySelectorAll('input[name="extract"]').forEach(cb => {
    cb.addEventListener("change", toggleConditionalForms);
  });

  // Pages input — show next selector when > 1
  $("pages").addEventListener("input", () => {
    const pages = parseInt($("pages").value) || 1;
    $("nextSelectorRow").hidden = pages <= 1;
  });

  // Step buttons
  $("step1Next").addEventListener("click", () => goToStep(2));
  $("step2Next").addEventListener("click", validateAndGoStep3);
  $("step3Next").addEventListener("click", validateAndGoStep4);
  $("step4Next").addEventListener("click", startScrapeJob);

  // Add initial field row for structured extraction
  addField();
});


// ============================================================
// START — URL submitted, show wizard, run detection
// ============================================================

function startScrape() {
  const raw = urlInput.value.trim();

  if (!raw) {
    urlInput.focus();
    return;
  }

  let url = raw;
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    url = "https://" + url;
  }

  state.url = url;

  // Hide hero, show wizard
  hero.hidden   = true;
  wizard.hidden = false;

  goToStep(1);
  runDetection(url);
}


// ============================================================
// STEP NAVIGATION
// ============================================================

function goToStep(n) {
  state.currentStep = n;

  // Hide all steps
  document.querySelectorAll(".step").forEach(s => {
    s.hidden = true;
  });

  // Show target step
  const target = $(`step-${n}`);
  if (target) target.hidden = false;

  // Update progress dots
  document.querySelectorAll(".progress-step").forEach(dot => {
    const s = parseInt(dot.dataset.step);
    dot.classList.remove("active", "completed");
    if (s === n) dot.classList.add("active");
    if (s < n)  dot.classList.add("completed");
  });

  // Update progress lines
  document.querySelectorAll(".progress-line").forEach((line, i) => {
    line.classList.toggle("completed", i + 1 < n);
  });

  // Scroll to top
  window.scrollTo({ top: 0, behavior: "smooth" });
}


// ============================================================
// STEP 1: DETECTION
// ============================================================

async function runDetection(url) {
  $("detectingUrl").textContent = url;
  $("detectingAnim").hidden  = false;
  $("detectionResult").hidden = true;

  try {
    const res  = await fetch("/api/detect", {
      method : "POST",
      headers: { "Content-Type": "application/json" },
      body   : JSON.stringify({ url }),
    });

    const data = await res.json();
    state.jobId = data.job_id;

    // Poll for detection result
    pollDetection(data.job_id);

  } catch (err) {
    showDetectionError("Could not connect to WebPull server.");
  }
}


function pollDetection(jobId) {
  state.pollTimer = setInterval(async () => {
    try {
      const res  = await fetch(`/api/jobs/${jobId}`);
      const job  = await res.json();

      if (job.detection) {
        clearInterval(state.pollTimer);
        showDetectionResult(job.detection);
      }

      if (job.state === "failed") {
        clearInterval(state.pollTimer);
        showDetectionError(job.error || "Detection failed.");
      }

    } catch (err) {
      clearInterval(state.pollTimer);
      showDetectionError("Lost connection to server.");
    }
  }, 1200);
}


function showDetectionResult(detection) {
  state.detection = detection;

  $("detectingAnim").hidden   = false;
  $("detectingAnim").hidden   = true;
  $("detectionResult").hidden = false;

  // Badge
  const badge = $("siteBadge");
  badge.textContent = `${siteIcon(detection.type)}  ${detection.label}`;
  badge.className = `site-badge ${badgeColor(detection.type)}`;

  // Notes
  $("detectionNotes").textContent = detection.notes;

  // Signals
  const sigContainer = $("detectionSignals");
  sigContainer.innerHTML = "";
  (detection.signals || []).forEach(sig => {
    const tag = document.createElement("span");
    tag.className   = "signal-tag";
    tag.textContent = sig;
    sigContainer.appendChild(tag);
  });

  // Warnings for tricky site types
  const warnTypes = {
    cloudflare : "Cloudflare bot protection is active. WebPull cannot bypass this. The scrape may return a challenge page instead of real content.",
    shopify    : "This is a Shopify store. Consider using the Shopify API directly for more reliable data access.",
    js_rendered: "This site renders content with JavaScript. WebPull may only see the empty HTML shell. Check DevTools → Network → Fetch/XHR for a direct API endpoint.",
    rest_api   : "This site loads data from a REST API. If you can find the API endpoint in DevTools → Network, use that URL directly for cleaner results.",
  };

  const warn = warnTypes[detection.type];
  if (warn) {
    $("detectionWarn").hidden  = false;
    $("detectionWarnText").textContent = " " + warn;
  } else {
    $("detectionWarn").hidden = true;
  }

  // Pre-select auth method based on detection
  const authMap = {
    nextauth : "nextauth",
    wordpress: "wordpress",
    django   : "html_form",
    laravel  : "html_form",
  };

  const suggestedAuth = authMap[detection.type];
  if (suggestedAuth) {
    const card = document.querySelector(
      `.option-card[data-auth="${suggestedAuth}"]`
    );
    if (card) {
      document.querySelectorAll(".option-card")
        .forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
      card.querySelector("input").checked = true;
      state.authMethod = suggestedAuth;
    }
  }
}


function showDetectionError(msg) {
  $("detectingAnim").hidden   = true;
  $("detectionResult").hidden = false;
  $("siteBadge").textContent  = "⚠  Detection failed";
  $("siteBadge").className    = "site-badge badge-yellow";
  $("detectionNotes").textContent = msg;
  $("detectionSignals").innerHTML = "";
}


function siteIcon(type) {
  const icons = {
    html       : "📄",
    nextauth   : "⚡",
    wordpress  : "🔵",
    django     : "🐍",
    laravel    : "🔴",
    shopify    : "🛍",
    rest_api   : "🔌",
    js_rendered: "⚙️",
    cloudflare : "🛡",
    unknown    : "❓",
  };
  return icons[type] || "🌐";
}


function badgeColor(type) {
  const good    = ["html", "nextauth", "wordpress", "django", "laravel"];
  const warn    = ["js_rendered", "rest_api", "shopify", "unknown"];
  const danger  = ["cloudflare"];

  if (good.includes(type))   return "badge-green";
  if (danger.includes(type)) return "badge-red";
  return "badge-yellow";
}


// ============================================================
// STEP 2: AUTH
// ============================================================

function toggleCredentialsForm(method) {
  const form = $("credentialsForm");
  const loginUrlRow = $("loginUrlRow");

  if (method === "none") {
    form.hidden = true;
    return;
  }

  form.hidden = false;

  // Show login URL field only for HTML form (user may need different URL)
  loginUrlRow.hidden = method !== "html_form";
}


function validateAndGoStep3() {
  const method = state.authMethod;

  if (method !== "none") {
    const username = $("username").value.trim();
    const password = $("password").value.trim();

    if (!username || !password) {
      alert("Please enter your username and password.");
      return;
    }
  }

  goToStep(3);
}


// ============================================================
// STEP 3: EXTRACTION
// ============================================================

function toggleConditionalForms() {
  const selectorChecked  = $("selectorCheck").checked;
  const structuredChecked = $("structuredCheck").checked;

  $("selectorForm").hidden  = !selectorChecked;
  $("structuredForm").hidden = !structuredChecked;
}


function addField() {
  const list = $("fieldsList");
  const row  = document.createElement("div");
  row.className = "field-row";

  row.innerHTML = `
    <input
      type="text"
      class="form-input form-mono field-name"
      placeholder="field name (e.g. price)"
      style="flex:1"
    />
    <input
      type="text"
      class="form-input form-mono field-selector"
      placeholder="CSS selector (e.g. .price)"
      style="flex:1.5"
    />
    <button class="btn-remove-field" onclick="removeField(this)">×</button>
  `;

  list.appendChild(row);
}


function removeField(btn) {
  btn.closest(".field-row").remove();
}


function getStructuredFields() {
  const fields = {};
  document.querySelectorAll(".field-row").forEach(row => {
    const name = row.querySelector(".field-name").value.trim();
    const sel  = row.querySelector(".field-selector").value.trim();
    if (name && sel) fields[name] = sel;
  });
  return fields;
}


function validateAndGoStep4() {
  const modes = getExtractModes();

  if (modes.length === 0) {
    alert("Please select at least one thing to extract.");
    return;
  }

  if (modes.includes("selector") && !$("cssSelector").value.trim()) {
    alert("Please enter a CSS selector.");
    return;
  }

  if (modes.includes("structured")) {
    if (!$("containerSelector").value.trim()) {
      alert("Please enter a container CSS selector for structured extraction.");
      return;
    }
    const fields = getStructuredFields();
    if (Object.keys(fields).length === 0) {
      alert("Please add at least one field for structured extraction.");
      return;
    }
  }

  goToStep(4);
}


function getExtractModes() {
  return Array.from(
    document.querySelectorAll('input[name="extract"]:checked')
  ).map(cb => cb.value);
}


// ============================================================
// STEP 4 → 5: START SCRAPE JOB
// ============================================================

async function startScrapeJob() {
  const method = state.authMethod;

  const payload = {
    url           : state.url,
    auth_method   : method,
    username      : $("username").value.trim()     || null,
    password      : $("password").value.trim()     || null,
    username_field: $("usernameField").value.trim() || "email",
    password_field: "password",
    login_url     : $("loginUrl").value.trim()     || null,
    extract_modes : getExtractModes(),
    selector      : $("cssSelector").value.trim()  || null,
    container     : $("containerSelector").value.trim() || null,
    fields        : getStructuredFields(),
    pages         : parseInt($("pages").value) || 1,
    next_selector : $("nextSelector").value.trim() || null,
    delay         : parseFloat($("delay").value)   || 0.5,
    timeout       : parseInt($("timeout").value)   || 20,
  };

  goToStep(5);
  $("step5Title").textContent = "Running...";
  $("step5Desc").textContent  = "WebPull is fetching your data.";
  $("resultsSection").hidden  = true;
  $("errorSection").hidden    = true;
  $("logFeed").innerHTML      = logLine("info", "Starting job...");

  try {
    const res  = await fetch("/api/scrape", {
      method : "POST",
      headers: { "Content-Type": "application/json" },
      body   : JSON.stringify(payload),
    });

    const data = await res.json();
    state.scrapeJobId = data.job_id;

    pollScrapeJob(data.job_id);

  } catch (err) {
    appendLog("error", "Could not connect to WebPull server.");
    showError("Connection failed. Is the server running?");
  }
}


// ============================================================
// POLLING — live log updates
// ============================================================

let lastLogCount = 0;
let jobFinished  = false;

function pollScrapeJob(jobId) {
  lastLogCount = 0;
  jobFinished  = false;

  state.pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      const job = await res.json();

      // Append new log entries
      const entries = job.log || [];
      for (let i = lastLogCount; i < entries.length; i++) {
        appendLog(entries[i].level, entries[i].message);
      }
      lastLogCount = entries.length;

      if (job.state === "done" && !jobFinished) {
        jobFinished = true;
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        showResults(jobId);
      }

      if (job.state === "failed" && !jobFinished) {
        jobFinished = true;
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        showError(job.error || "Scrape failed. Check the log above.");
      }

    } catch (err) {
      // Network hiccup — keep trying
    }
  }, 1500);
}


// ============================================================
// LOG HELPERS
// ============================================================

function logLine(level, message) {
  const prefix = {
    info : "  ",
    ok   : "✓ ",
    warn : "⚠ ",
    error: "✗ ",
  };
  return `<div class="log-entry log-${level}">${prefix[level] || ""}${escHtml(message)}</div>`;
}


function appendLog(level, message) {
  const feed = $("logFeed");
  feed.insertAdjacentHTML("beforeend", logLine(level, message));
  feed.scrollTop = feed.scrollHeight;
}


function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}


// ============================================================
// SHOW RESULTS
// ============================================================

async function showResults(jobId) {
  $("step5Title").textContent = "Done!";
  $("step5Desc").textContent  = "Your data is ready to download.";

  // Set download links
  $("dlJson").href = `/api/jobs/${jobId}/download/json`;
  $("dlCsv").href  = `/api/jobs/${jobId}/download/csv`;
  $("dlTxt").href  = `/api/jobs/${jobId}/download/txt`;

  // Show results section FIRST so insertBefore works correctly
  $("resultsSection").hidden  = false;
  $("errorSection").hidden    = true;

  // Then fetch and render preview (async — AI call happens here)
  try {
    const res  = await fetch(`/api/jobs/${jobId}/download/json`);
    const data = await res.json();
    await renderPreview(data, jobId);
  } catch (err) {
    $("resultsSummary").textContent = "Scrape complete. Download your files below.";
  }

  // Add AI summary button after preview
  addSummaryButton(jobId);
}


function addSummaryButton(jobId) {
  // Remove existing button if any
  const existing = document.getElementById("summarySection");
  if (existing) existing.remove();

  const section = document.createElement("div");
  section.id    = "summarySection";
  section.style.cssText = "margin-bottom:20px";

  section.innerHTML = `
    <button class="btn btn-ghost" id="summaryBtn" onclick="loadSummary('${jobId}')">
      ✦ Analyse with AI
    </button>
    <div id="summaryResult" style="display:none;margin-top:12px;padding:14px 16px;
         background:var(--blue-bg);border:1px solid var(--blue-border);
         border-radius:var(--radius);font-size:0.875rem;line-height:1.7;
         color:var(--text)"></div>
  `;

  const dlButtons = document.querySelector(".download-buttons");
  $("resultsSection").insertBefore(section, dlButtons);
}


async function loadSummary(jobId) {
  const btn    = $("summaryBtn");
  const result = $("summaryResult");

  // Show loading state immediately so user knows something is happening
  btn.textContent      = "✦ Analysing...";
  btn.disabled         = true;
  result.style.display = "block";
  result.innerHTML     = `
    <div style="display:flex;align-items:center;gap:10px;color:var(--text-muted)">
      <div style="width:14px;height:14px;border-radius:50%;
                  border:2px solid var(--border);
                  border-top-color:var(--blue);
                  animation:spin 0.8s linear infinite;
                  flex-shrink:0"></div>
      <span>Analysing your data...</span>
    </div>`;

  try {
    // 12 second timeout — Groq is usually 1-3s but give it room
    const controller = new AbortController();
    const timer      = setTimeout(() => controller.abort(), 12000);

    const res = await fetch("/api/summarise", {
      method : "POST",
      headers: { "Content-Type": "application/json" },
      body   : JSON.stringify({ job_id: jobId }),
      signal : controller.signal,
    });

    clearTimeout(timer);
    const data = await res.json();

    if (data.success && data.summary) {
      result.innerHTML  = `<strong>✦ AI Summary</strong><br/><br/>${escHtml(data.summary)}`;
      btn.style.display = "none";
    } else {
      result.innerHTML = `<span style="color:var(--text-muted)">
        AI unavailable right now — try again in a moment.
      </span>`;
      btn.textContent = "✦ Try again";
      btn.disabled    = false;
    }

  } catch (err) {
    // Timeout or network error
    result.innerHTML = `<span style="color:var(--text-muted)">
      ${err.name === "AbortError"
        ? "AI took too long — try again."
        : "Could not reach AI — try again."}
    </span>`;
    btn.textContent = "✦ Try again";
    btn.disabled    = false;
  }
}


// ============================================================
// TABLE PREVIEW + AI FORMAT ASSIST
// ============================================================

async function renderPreview(data, jobId) {
  // Remove any existing preview first
  const old = document.querySelector(".table-preview-section");
  if (old) old.remove();

  if (!data || data.length === 0) {
    $("resultsSummary").textContent = "Scrape complete — no data returned.";
    return;
  }

  const pageCount = data.length;

  $("resultsSummary").textContent =
    `✓ ${pageCount} page${pageCount !== 1 ? "s" : ""} scraped successfully.`;

  // Try AI format assist — await result before rendering so we only render once
  let formatted = null;

  try {
    formatted = await aiFormatAssist(data);
  } catch (e) {
    // AI failed or timed out — fall back to raw preview
    formatted = null;
  }

  const rows    = formatted ? formatted.rows    : flattenForPreview(data);
  const columns = formatted ? formatted.columns : (rows[0] ? Object.keys(rows[0]) : []);
  const aiUsed  = !!formatted;

  if (rows.length === 0 || columns.length === 0) {
    $("resultsSummary").textContent += " Preview not available for this data type.";
    return;
  }

  // Build the preview section HTML
  const previewSection = document.createElement("div");
  previewSection.className = "table-preview-section";

  const label = document.createElement("div");
  label.className = "table-preview-label";
  const shownRows = Math.min(rows.length, rows.length <= 20 ? rows.length : rows.length <= 50 ? 20 : 25);
  label.innerHTML = `Data Preview (${shownRows} of ${rows.length} rows)`;

  if (aiUsed && formatted.data_type) {
    const aiBadge = document.createElement("span");
    aiBadge.className = "ai-badge";
    aiBadge.innerHTML = `✦ AI: ${formatted.data_type}`;
    label.appendChild(aiBadge);
  }

  const wrap  = document.createElement("div");
  wrap.className = "table-wrap";

  const table = document.createElement("table");
  table.className = "preview-table";

  // Header
  const thead = document.createElement("thead");
  const hr    = document.createElement("tr");
  columns.forEach(col => {
    const th = document.createElement("th");
    th.textContent = col;
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  table.appendChild(thead);

  // Body (max 10 rows)
  const tbody    = document.createElement("tbody");
  // Smart row count based on dataset size
  const maxRows    = rows.length <= 20 ? rows.length
                   : rows.length <= 50 ? 20
                   : 25;
  const previewRows = rows.slice(0, maxRows);

  previewRows.forEach(row => {
    const tr = document.createElement("tr");
    columns.forEach(col => {
      const td = document.createElement("td");
      const val = row[col];
      td.textContent = val !== null && val !== undefined ? String(val) : "—";
      td.title = td.textContent;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  wrap.appendChild(table);
  previewSection.appendChild(label);
  previewSection.appendChild(wrap);

  // Insert before download buttons
  const dlButtons = document.querySelector(".download-buttons");
  $("resultsSection").insertBefore(previewSection, dlButtons);
}


// ============================================================
// AI FORMAT ASSIST — Groq
// Sends a sample of raw data to the backend which calls Groq.
// Returns suggested columns and formatted rows.
// ============================================================

async function aiFormatAssist(data) {
  // Take a small sample to keep tokens low
  const sample = data.slice(0, 3);

  // 8 second timeout — if Groq is slow we fall back to raw preview
  const controller = new AbortController();
  const timer      = setTimeout(() => controller.abort(), 8000);

  try {
    const res = await fetch("/api/format-assist", {
      method : "POST",
      headers: { "Content-Type": "application/json" },
      body   : JSON.stringify({ sample }),
      signal : controller.signal,
    });

    clearTimeout(timer);

    if (!res.ok) return null;

    const result = await res.json();

    if (!result.success) return null;

    // Basic sanity check — AI must return at least columns and rows
    if (!result.columns || !result.rows || result.columns.length === 0) {
      return null;
    }

    return {
      data_type: result.data_type,
      columns  : result.columns,
      rows     : result.rows,
    };

  } catch (e) {
    clearTimeout(timer);
    return null;  // timeout or network error — fall back to raw
  }
}


// ============================================================
// FLATTEN RAW DATA FOR PREVIEW (fallback without AI)
// ============================================================

function flattenForPreview(data) {
  const rows = [];

  for (const page of data) {
    // Structured data is already row-based
    if (page.structured && page.structured.length > 0) {
      return page.structured;
    }

    // Links
    if (page.links && page.links.length > 0) {
      return page.links.map(l => ({
        text: l.text,
        url : l.url,
      }));
    }

    // Images
    if (page.images && page.images.length > 0) {
      return page.images.map(i => ({
        alt: i.alt,
        url: i.url,
      }));
    }

    // Selector results
    if (page.selector_results && page.selector_results.length > 0) {
      return page.selector_results.map(r => ({ result: r }));
    }

    // Headings
    if (page.headings && page.headings.length > 0) {
      return page.headings.map(h => ({
        level  : `H${h.level}`,
        heading: h.text,
      }));
    }

    // Fallback: title + first 200 chars of text
    const row = {};
    if (page.title) row.title = page.title;
    if (page.text)  row.text  = page.text.slice(0, 200);
    if (page._url)  row.url   = page._url;
    if (Object.keys(row).length > 0) rows.push(row);
  }

  return rows;
}


// ============================================================
// ERROR STATE
// ============================================================

function showError(message) {
  $("step5Title").textContent = "Something went wrong";
  $("step5Desc").textContent  = "";
  $("errorText").textContent  = " " + message;
  $("errorSection").hidden    = false;
  $("resultsSection").hidden  = true;
}


// ============================================================
// RESET
// ============================================================

function resetToHome() {
  clearInterval(state.pollTimer);

  state.url         = "";
  state.jobId       = null;
  state.scrapeJobId = null;
  state.detection   = null;
  state.authMethod  = "none";
  state.currentStep = 0;

  lastLogCount = 0;
  jobFinished  = false;

  // Clear summary section
  const sum = document.getElementById("summarySection");
  if (sum) sum.remove();

  urlInput.value  = "";
  hero.hidden     = false;
  wizard.hidden   = true;

  window.scrollTo({ top: 0, behavior: "smooth" });
}
