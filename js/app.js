// ── Config ──
const SUPABASE_URL = "https://ercbzutulfrerwmkndhy.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_tlU-oHKcVblfTHjc_YF7sw_hW4lubGo";

// ── State ──
let dashData = null;
let activeRange = "all";
let currentFilter = "all";
let currentAE = "all";
let currentTransition = "all";
let sortCol = 5;
let sortAsc = true;
let aeSortCol = 6;
let aeSortAsc = true;

// ── Load data ──
async function loadData() {
  try {
    const resp = await fetch(
      `${SUPABASE_URL}/rest/v1/dashboard_snapshots?select=data,generated_at&order=generated_at.desc&limit=1`,
      { headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${SUPABASE_ANON_KEY}` } }
    );
    if (!resp.ok) throw new Error("Supabase fetch failed: " + resp.status);
    const rows = await resp.json();
    if (!rows.length) throw new Error("No snapshots found");
    dashData = rows[0].data;
    buildDateButtons();
    render();
    document.getElementById("meta").textContent =
      `Data pulled: ${new Date(dashData.generated_at).toLocaleString()} | Range: ${dashData.start_date} to ${dashData.end_date}`;
  } catch (e) {
    document.getElementById("tableBody").innerHTML =
      `<tr><td colspan="8" class="loading">Could not load data: ${e.message}</td></tr>`;
  }
}

// ── Build date range buttons dynamically ──
// PT-based date keys match the backend's business-day bucketing.
function ptDateKey(date) {
  return date.toLocaleDateString("en-CA", { timeZone: "America/Los_Angeles" });
}

function buildDateButtons() {
  const container = document.getElementById("dateRange");
  const today = ptDateKey(new Date());
  const yesterday = ptDateKey(new Date(Date.now() - 86400000));
  const dates = Object.keys(dashData.by_date).sort();

  let html = `<button data-range="all" class="active" onclick="setRange('all')">All <span class="count-badge">${dashData.all.length}</span></button>`;

  // Older dates (excluding today/yesterday — those are pinned at the end)
  for (const date of dates) {
    if (date === today || date === yesterday) continue;
    const count = dashData.by_date[date].length;
    const d = new Date(date + "T12:00:00Z");
    const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    html += `<button data-range="${date}" onclick="setRange('${date}')">${label} <span class="count-badge">${count}</span></button>`;
  }

  // Always show Yesterday then Today at the end (count = 0 if no data)
  const yesterdayCount = (dashData.by_date[yesterday] || []).length;
  html += `<button data-range="${yesterday}" onclick="setRange('${yesterday}')">Yesterday <span class="count-badge">${yesterdayCount}</span></button>`;

  const todayCount = (dashData.by_date[today] || []).length;
  html += `<button data-range="${today}" onclick="setRange('${today}')">Today <span class="count-badge">${todayCount}</span></button>`;

  container.innerHTML = html;
}

// ── Formatting ──
function fmt(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  const mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getUTCMonth()];
  return `${mon} ${d.getUTCDate()}, ${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}:${String(d.getUTCSeconds()).padStart(2,'0')} UTC`;
}

// ── Actions ──
function setRange(key) {
  activeRange = key;
  currentFilter = "all";
  currentAE = "all";
  currentTransition = "all";
  document.querySelectorAll(".date-range button").forEach(b => b.classList.remove("active"));
  document.querySelector(`.date-range button[data-range="${key}"]`).classList.add("active");
  render();
}

function filterBy(bucket) { currentFilter = bucket; render(); }
function filterByAE(ae) { currentAE = ae; render(); }
function filterByTransition(t) { currentTransition = t; render(); }

function sortAETable(col) {
  if (aeSortCol === col) aeSortAsc = !aeSortAsc;
  else { aeSortCol = col; aeSortAsc = true; }
  document.querySelectorAll(".ae-table thead th .ae-arrow").forEach(a => a.textContent = "");
  document.querySelectorAll(".ae-table thead th")[col].querySelector(".ae-arrow").textContent = aeSortAsc ? "\u25B2" : "\u25BC";
  render();
}

function sortTable(col) {
  if (sortCol === col) sortAsc = !sortAsc;
  else { sortCol = col; sortAsc = true; }
  document.querySelectorAll("thead th .arrow").forEach(a => a.textContent = "");
  document.querySelectorAll("thead th")[col].querySelector(".arrow").textContent = sortAsc ? "\u25B2" : "\u25BC";
  render();
}

// ── Main render ──
function render() {
  if (!dashData) return;

  const now = new Date(dashData.generated_at);
  const rawData = activeRange === "all"
    ? dashData.all
    : (dashData.by_date[activeRange] || []);

  const processed = rawData.map(r => ({ ...r }));

  // Range info
  const rangeInfo = activeRange === "all"
    ? `${dashData.start_date} to ${dashData.end_date} | ${processed.length} leads`
    : `${activeRange} | ${processed.length} leads`;
  document.getElementById("rangeInfo").textContent = rangeInfo;

  if (!processed.length) {
    document.getElementById("withinCount").textContent = "--";
    document.getElementById("afterCount").textContent = "--";
    document.getElementById("neverCount").textContent = "--";
    document.getElementById("withinPct").textContent = "";
    document.getElementById("afterPct").textContent = "";
    document.getElementById("neverPct").textContent = "";
    document.getElementById("tableBody").innerHTML =
      '<tr><td colspan="8" class="loading">No data for this range.</td></tr>';
    return;
  }

  // Populate Transition dropdown
  const transSel = document.getElementById("transFilter");
  const transitions = [...new Set(processed.map(r => r.transition || "Active Scenario"))].sort();
  transSel.innerHTML = '<option value="all">All</option>' +
    transitions.map(t => `<option value="${t}"${t === currentTransition ? ' selected' : ''}>${t}</option>`).join("");

  // Populate AE dropdown
  const sel = document.getElementById("aeFilter");
  const aes = [...new Set(processed.map(r => r.ae))].sort();
  sel.innerHTML = '<option value="all">All AEs</option>' +
    aes.map(ae => `<option value="${ae}"${ae === currentAE ? ' selected' : ''}>${ae}</option>`).join("");

  // Apply filters
  const transProcessed = currentTransition === "all" ? processed : processed.filter(r => (r.transition || "Active Scenario") === currentTransition);
  const aeProcessed = currentAE === "all" ? transProcessed : transProcessed.filter(r => r.ae === currentAE);
  const filtered = currentFilter === "all" ? aeProcessed : aeProcessed.filter(r => r.bucket === currentFilter);

  const within = aeProcessed.filter(r => r.bucket === "within").length;
  const after  = aeProcessed.filter(r => r.bucket === "after").length;
  const never  = aeProcessed.filter(r => r.bucket === "never").length;
  const pending = aeProcessed.filter(r => r.bucket === "pending").length;
  const eligible = within + after + never;

  document.getElementById("withinCount").textContent = within;
  document.getElementById("afterCount").textContent = after;
  document.getElementById("neverCount").textContent = never;
  document.getElementById("withinPct").textContent = eligible ? Math.round(within/eligible*100) + "% of eligible" : "";
  document.getElementById("afterPct").textContent = eligible ? Math.round(after/eligible*100) + "% of eligible" : "";
  document.getElementById("neverPct").textContent = eligible
    ? Math.round(never/eligible*100) + "% of eligible" + (pending ? "  |  " + pending + " pending" : "")
    : "";


  // ── AE Summary Table ──
  const aeSource = currentTransition === "all" ? processed : processed.filter(r => (r.transition || "Active Scenario") === currentTransition);
  const aeMap = {};
  aeSource.forEach(r => {
    if (!aeMap[r.ae]) aeMap[r.ae] = { ae: r.ae, total: 0, within: 0, after: 0, never: 0, pending: 0, callMins: [] };
    const a = aeMap[r.ae];
    a.total++;
    if (r.bucket === "within") { a.within++; a.callMins.push(r.minsToCall); }
    else if (r.bucket === "after") { a.after++; a.callMins.push(r.minsToCall); }
    else if (r.bucket === "never") a.never++;
    else if (r.bucket === "pending") a.pending++;
  });

  const aeRows = Object.values(aeMap).map(a => {
    const elig = a.within + a.after + a.never;
    const callRate = elig ? Math.round((a.within + a.after) / elig * 100) : null;
    const avgMins = a.callMins.length
      ? Math.round(a.callMins.reduce((s,v) => s+v, 0) / a.callMins.length * 10) / 10
      : null;
    return { ...a, eligible: elig, callRate, avgMins };
  });

  aeRows.sort((a, b) => {
    let va, vb;
    switch (aeSortCol) {
      case 0: va = a.ae; vb = b.ae; break;
      case 1: va = a.total; vb = b.total; break;
      case 2: va = a.within; vb = b.within; break;
      case 3: va = a.after; vb = b.after; break;
      case 4: va = a.never; vb = b.never; break;
      case 5: va = a.pending; vb = b.pending; break;
      case 6: va = a.callRate ?? -1; vb = b.callRate ?? -1; break;
      case 7: va = a.avgMins ?? 99999; vb = b.avgMins ?? 99999; break;
    }
    return typeof va === "string"
      ? (aeSortAsc ? va.localeCompare(vb) : vb.localeCompare(va))
      : (aeSortAsc ? va - vb : vb - va);
  });

  document.getElementById("aeTableBody").innerHTML = aeRows.map(a => {
    const rateClass = a.callRate === null ? "" : a.callRate >= 50 ? "call-rate-good" : a.callRate >= 25 ? "call-rate-mid" : "call-rate-bad";
    return `<tr>
      <td>${a.ae}</td>
      <td>${a.total}</td>
      <td>${a.within}</td>
      <td>${a.after}</td>
      <td>${a.never}</td>
      <td>${a.pending}</td>
      <td class="${rateClass}">${a.callRate !== null ? a.callRate + "%" : "--"}</td>
      <td>${a.avgMins !== null ? a.avgMins + " min" : "--"}</td>
    </tr>`;
  }).join("");

  // Filter button highlight
  document.querySelectorAll("#bucketFilters button").forEach(b => b.classList.remove("active"));
  const ab = document.querySelector(`#bucketFilters button[data-bucket="${currentFilter}"]`);
  if (ab) ab.classList.add("active");

  // Card highlight
  document.querySelectorAll(".card").forEach(c => c.classList.remove("active"));
  if (currentFilter !== "all") {
    const c = document.querySelector(`.card[data-filter="${currentFilter}"]`);
    if (c) c.classList.add("active");
  }

  // Sort + render table
  const sorted = [...filtered].sort((a, b) => {
    let va, vb;
    switch (sortCol) {
      case 0: va = a.contact; vb = b.contact; break;
      case 1: va = a.ae; vb = b.ae; break;
      case 2: va = a.transition || "Active Scenario"; vb = b.transition || "Active Scenario"; break;
      case 3: va = a.changedAt; vb = b.changedAt; break;
      case 4: va = a.callAt || ""; vb = b.callAt || ""; break;
      case 5: va = a.minsToCall ?? 99999; vb = b.minsToCall ?? 99999; break;
      case 6: va = a.bucket; vb = b.bucket; break;
    }
    return typeof va === "string"
      ? (sortAsc ? va.localeCompare(vb) : vb.localeCompare(va))
      : (sortAsc ? va - vb : vb - va);
  });

  document.getElementById("tableBody").innerHTML = sorted.map(r => {
    const bl = r.bucket === "within" ? "Within 2 hrs"
      : r.bucket === "after" ? "After 2 hrs"
      : r.bucket === "never" ? "Never called"
      : "Pending";
    const trans = r.transition || "Active Scenario";
    const link = r.leadId
      ? `<a href="https://app.close.com/lead/${r.leadId}/" target="_blank" style="color:#6c5ce7;font-weight:700;text-decoration:none;font-size:15px;" title="Open in Close">&#8599;</a>`
      : `<span style="color:#b2bec3">&#8212;</span>`;
    return `<tr>
      <td><strong>${r.contact}</strong></td>
      <td>${r.ae}</td>
      <td><span class="badge-trans">${trans}</span></td>
      <td>${fmt(r.changedAt)}</td>
      <td>${fmt(r.callAt)}</td>
      <td>${r.minsToCall !== null ? r.minsToCall + " min" : "--"}</td>
      <td><span class="badge ${r.bucket}">${bl}</span></td>
      <td style="text-align:center">${link}</td>
    </tr>`;
  }).join("");
}

// Boot
loadData();
