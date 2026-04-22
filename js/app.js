// ── Config ──
const SUPABASE_URL = "https://ercbzutulfrerwmkndhy.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_tlU-oHKcVblfTHjc_YF7sw_hW4lubGo";

// ── State ──
let dashData = null;
let activeRange = "all";
let currentFilter = "all";
let currentAEs = [];                  // empty = all AEs
let currentTransition = "all";
let sortCol = 5;
let sortAsc = true;
let aeSortCol = 7;
let aeSortAsc = true;

// ── Load data ──
// Two snapshot types live in the same table: the daily MTD pull and the
// every-6h recent (today+yesterday) pull. Fetch the latest of each and
// overlay recent's today/yesterday on top of the MTD base.
async function loadData() {
  try {
    const resp = await fetch(
      `${SUPABASE_URL}/rest/v1/dashboard_snapshots?select=data,generated_at&order=generated_at.desc&limit=10`,
      { headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${SUPABASE_ANON_KEY}` } }
    );
    if (!resp.ok) throw new Error("Supabase fetch failed: " + resp.status);
    const rows = await resp.json();
    if (!rows.length) throw new Error("No snapshots found");

    let fullRow = null, recentRow = null;
    for (const row of rows) {
      const type = row.data.range_type || "mtd";  // legacy untagged = mtd
      if ((type === "mtd" || type === "custom") && !fullRow) fullRow = row;
      else if (type === "recent" && !recentRow) recentRow = row;
      if (fullRow && recentRow) break;
    }
    if (!fullRow) throw new Error("No MTD snapshot found");

    const full = fullRow.data;
    const recent = recentRow?.data;
    const useRecent = recent && recent.generated_at > full.generated_at;

    if (useRecent) {
      const today = ptDateKey(new Date());
      const yesterday = ptDateKey(new Date(Date.now() - 86400000));
      const merged = { ...full.by_date };
      delete merged[today];
      delete merged[yesterday];
      if (recent.by_date[today]) merged[today] = recent.by_date[today];
      if (recent.by_date[yesterday]) merged[yesterday] = recent.by_date[yesterday];
      const mergedAll = Object.values(merged).flat();
      dashData = { ...full, by_date: merged, all: mergedAll };
    } else {
      dashData = full;
    }

    buildDateButtons();
    render();

    const mtdPulled = new Date(full.generated_at).toLocaleString();
    const recentPulled = useRecent ? new Date(recent.generated_at).toLocaleString() : null;
    document.getElementById("meta").textContent = recentPulled
      ? `MTD pulled: ${mtdPulled} | Today/Yesterday pulled: ${recentPulled} | Range: ${full.start_date} → ${full.end_date}`
      : `Data pulled: ${mtdPulled} | Range: ${full.start_date} → ${full.end_date}`;
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

// Compute MTD/WTD/last-7-days start keys in PT
function rangeBoundaries() {
  const today = ptDateKey(new Date());
  const [ty, tm, td] = today.split("-").map(Number);
  const mtdStart = `${ty}-${String(tm).padStart(2, "0")}-01`;
  const todayUTC = new Date(Date.UTC(ty, tm - 1, td));
  const daysFromMonday = (todayUTC.getUTCDay() + 6) % 7;  // Mon=0 … Sun=6
  const wtdStart = new Date(Date.UTC(ty, tm - 1, td - daysFromMonday)).toISOString().slice(0, 10);
  const sevenDaysAgo = new Date(Date.UTC(ty, tm - 1, td - 7)).toISOString().slice(0, 10);
  return { today, mtdStart, wtdStart, sevenDaysAgo };
}

function countSince(startKey) {
  return Object.entries(dashData.by_date)
    .filter(([d]) => d >= startKey)
    .reduce((s, [, leads]) => s + leads.length, 0);
}

function buildDateButtons() {
  const container = document.getElementById("dateRange");
  const { today, mtdStart, wtdStart, sevenDaysAgo } = rangeBoundaries();
  const yesterday = ptDateKey(new Date(Date.now() - 86400000));
  const dates = Object.keys(dashData.by_date).sort();

  let html = `<button data-range="all" class="active" onclick="setRange('all')">All <span class="count-badge">${dashData.all.length}</span></button>`;
  html += `<button data-range="mtd" onclick="setRange('mtd')">MTD <span class="count-badge">${countSince(mtdStart)}</span></button>`;
  html += `<button data-range="wtd" onclick="setRange('wtd')">WTD <span class="count-badge">${countSince(wtdStart)}</span></button>`;

  // Individual date tabs limited to the last 7 days (older data still reachable via MTD/All)
  for (const date of dates) {
    if (date === today || date === yesterday) continue;
    if (date < sevenDaysAgo) continue;
    const count = dashData.by_date[date].length;
    const d = new Date(date + "T12:00:00Z");
    const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    html += `<button data-range="${date}" onclick="setRange('${date}')">${label} <span class="count-badge">${count}</span></button>`;
  }

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
  const s = d.toLocaleString("en-US", {
    timeZone: "America/Los_Angeles",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return `${s} PT`;
}

// ── Actions ──
function setRange(key) {
  activeRange = key;
  currentFilter = "all";
  currentAEs = [];
  currentTransition = "all";
  document.querySelectorAll(".date-range button").forEach(b => b.classList.remove("active"));
  document.querySelector(`.date-range button[data-range="${key}"]`).classList.add("active");
  render();
}

function filterBy(bucket) { currentFilter = bucket; render(); }
function filterByTransition(t) { currentTransition = t; render(); }

// ── AE multi-select ──
function toggleAEPanel(e) {
  if (e) e.stopPropagation();
  const panel = document.getElementById("aeMultiPanel");
  const btn = document.getElementById("aeMultiBtn");
  const open = !panel.hidden;
  panel.hidden = open;
  btn.classList.toggle("open", !open);
}

function closeAEPanel() {
  const panel = document.getElementById("aeMultiPanel");
  const btn = document.getElementById("aeMultiBtn");
  if (panel && !panel.hidden) {
    panel.hidden = true;
    btn.classList.remove("open");
  }
}

function toggleAE(ae) {
  const i = currentAEs.indexOf(ae);
  if (i >= 0) currentAEs.splice(i, 1);
  else currentAEs.push(ae);
  render();
}

function clearAEs() {
  currentAEs = [];
  render();
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".multi-select")) closeAEPanel();
});

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
  const { mtdStart, wtdStart } = rangeBoundaries();

  let rawData;
  if (activeRange === "all") {
    rawData = dashData.all;
  } else if (activeRange === "mtd" || activeRange === "wtd") {
    const startKey = activeRange === "mtd" ? mtdStart : wtdStart;
    rawData = [];
    for (const [date, leads] of Object.entries(dashData.by_date)) {
      if (date >= startKey) rawData.push(...leads);
    }
  } else {
    rawData = dashData.by_date[activeRange] || [];
  }

  const processed = rawData.map(r => ({ ...r }));

  // Range info
  const rangeInfo = activeRange === "all"
    ? `${dashData.start_date} to ${dashData.end_date} | ${processed.length} leads`
    : activeRange === "mtd" ? `MTD (${mtdStart} →) | ${processed.length} leads`
    : activeRange === "wtd" ? `WTD (${wtdStart} →) | ${processed.length} leads`
    : `${activeRange} | ${processed.length} leads`;
  document.getElementById("rangeInfo").textContent = rangeInfo;

  if (!processed.length) {
    document.getElementById("withinCount").textContent = "--";
    document.getElementById("precallCount").textContent = "--";
    document.getElementById("afterCount").textContent = "--";
    document.getElementById("neverCount").textContent = "--";
    document.getElementById("withinPct").textContent = "";
    document.getElementById("precallPct").textContent = "";
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

  // Populate AE multi-select panel
  const aes = [...new Set(processed.map(r => r.ae))].sort();
  // Prune stale selections (AEs no longer in this range)
  currentAEs = currentAEs.filter(ae => aes.includes(ae));
  const panel = document.getElementById("aeMultiPanel");
  const btn = document.getElementById("aeMultiBtn");
  panel.innerHTML =
    `<label><input type="checkbox" ${currentAEs.length === 0 ? "checked" : ""} onclick="event.stopPropagation(); clearAEs();"> All AEs</label>` +
    `<div class="divider"></div>` +
    aes.map(ae => {
      const checked = currentAEs.includes(ae) ? "checked" : "";
      const safe = ae.replace(/'/g, "\\'");
      return `<label><input type="checkbox" ${checked} onclick="event.stopPropagation(); toggleAE('${safe}');"> ${ae}</label>`;
    }).join("");
  btn.textContent = currentAEs.length === 0 ? "All AEs"
    : currentAEs.length === 1 ? currentAEs[0]
    : `${currentAEs.length} AEs`;

  // Apply filters
  const transProcessed = currentTransition === "all" ? processed : processed.filter(r => (r.transition || "Active Scenario") === currentTransition);
  const aeProcessed = currentAEs.length === 0 ? transProcessed : transProcessed.filter(r => currentAEs.includes(r.ae));
  const filtered = currentFilter === "all" ? aeProcessed
    : currentFilter === "precall" ? aeProcessed.filter(r => r.preCall)
    : aeProcessed.filter(r => r.bucket === currentFilter);

  const within = aeProcessed.filter(r => r.bucket === "within").length;
  const after  = aeProcessed.filter(r => r.bucket === "after").length;
  const never  = aeProcessed.filter(r => r.bucket === "never").length;
  const pending = aeProcessed.filter(r => r.bucket === "pending").length;
  const preCall = aeProcessed.filter(r => r.preCall).length;
  const eligible = within + after + never;

  document.getElementById("withinCount").textContent = within;
  document.getElementById("precallCount").textContent = preCall;
  document.getElementById("afterCount").textContent = after;
  document.getElementById("neverCount").textContent = never;
  document.getElementById("withinPct").textContent = eligible ? Math.round(within/eligible*100) + "% of eligible" : "";
  document.getElementById("precallPct").textContent = within ? Math.round(preCall/within*100) + "% of within" : "";
  document.getElementById("afterPct").textContent = eligible ? Math.round(after/eligible*100) + "% of eligible" : "";
  document.getElementById("neverPct").textContent = eligible
    ? Math.round(never/eligible*100) + "% of eligible" + (pending ? "  |  " + pending + " pending" : "")
    : "";


  // ── AE Summary Table ──
  const aeSource = currentTransition === "all" ? processed : processed.filter(r => (r.transition || "Active Scenario") === currentTransition);
  const aeMap = {};
  aeSource.forEach(r => {
    if (!aeMap[r.ae]) aeMap[r.ae] = { ae: r.ae, total: 0, within: 0, preCall: 0, after: 0, never: 0, pending: 0 };
    const a = aeMap[r.ae];
    a.total++;
    if (r.bucket === "within") a.within++;
    else if (r.bucket === "after") a.after++;
    else if (r.bucket === "never") a.never++;
    else if (r.bucket === "pending") a.pending++;
    if (r.preCall) a.preCall++;
  });

  const aeRows = Object.values(aeMap).map(a => {
    const elig = a.within + a.after + a.never;
    const callRate = elig ? Math.round((a.within + a.after) / elig * 100) : null;
    return { ...a, eligible: elig, callRate };
  });

  aeRows.sort((a, b) => {
    let va, vb;
    switch (aeSortCol) {
      case 0: va = a.ae; vb = b.ae; break;
      case 1: va = a.preCall; vb = b.preCall; break;
      case 2: va = a.within; vb = b.within; break;
      case 3: va = a.after; vb = b.after; break;
      case 4: va = a.never; vb = b.never; break;
      case 5: va = a.pending; vb = b.pending; break;
      case 6: va = a.total; vb = b.total; break;
      case 7: va = a.callRate ?? -1; vb = b.callRate ?? -1; break;
    }
    return typeof va === "string"
      ? (aeSortAsc ? va.localeCompare(vb) : vb.localeCompare(va))
      : (aeSortAsc ? va - vb : vb - va);
  });

  document.getElementById("aeTableBody").innerHTML = aeRows.map(a => {
    const rateClass = a.callRate === null ? "" : a.callRate >= 50 ? "call-rate-good" : a.callRate >= 25 ? "call-rate-mid" : "call-rate-bad";
    return `<tr>
      <td>${a.ae}</td>
      <td>${a.preCall}</td>
      <td>${a.within}</td>
      <td>${a.after}</td>
      <td>${a.never}</td>
      <td>${a.pending}</td>
      <td>${a.total}</td>
      <td class="${rateClass}">${a.callRate !== null ? a.callRate + "%" : "--"}</td>
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
    const preBadge = r.preCall
      ? `<span class="badge-precall" title="Connected call within 30 min before status change">Pre-call</span>`
      : "";
    return `<tr>
      <td><strong>${r.contact}</strong></td>
      <td>${r.ae}</td>
      <td><span class="badge-trans">${trans}</span></td>
      <td>${fmt(r.changedAt)}</td>
      <td>${fmt(r.callAt)}</td>
      <td>${r.minsToCall !== null ? r.minsToCall + " min" : "--"}</td>
      <td><span class="badge ${r.bucket}">${bl}</span>${preBadge}</td>
      <td style="text-align:center">${link}</td>
    </tr>`;
  }).join("");
}

// Boot
loadData();
