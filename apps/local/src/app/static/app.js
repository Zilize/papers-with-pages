"use strict";

const $ = (id) => document.getElementById(id);
const labels = {
  project_page: "Project",
  repository: "Code",
  dataset: "Dataset",
  model: "Model",
  demo: "Demo",
  video: "Video",
  paper: "Paper",
  other: "Link",
};
let papers = [],
  selectedKind = "project_page",
  dateFrom = "", dateThrough = "", selectedCategory = "",
  lastRun = null,
  lastRevision = "",
  busy = false, stopping = false, githubConfigured = false;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
function resourceLink(resource) {
  const href = safeUrl(resource.url);
  if (!href) return null;
  const link = node(
    "a",
    `resource-link ${resource.kind}`,
    `${labels[resource.kind] || "Link"} ↗`,
  );
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.title = resource.url;
  return link;
}
function uniqueResources(resources) {
  const seen = new Set();
  return resources.filter((resource) => {
    const href = safeUrl(resource.url);
    if (!href) return false;
    const url = new URL(href);
    let identity = href;
    if (["arxiv.org", "www.arxiv.org", "export.arxiv.org"].includes(url.hostname)) {
      const match = url.pathname.match(
        /^\/(?:abs|pdf|html)\/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7}))(?:v\d+)?(?:\.pdf)?\/?$/i,
      );
      if (match) identity = `arxiv:${match[1].toLowerCase()}`;
    }
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

function localImage(path, title) {
  const img = node("img");
  img.src = path;
  img.alt = title;
  img.loading = "lazy";
  return img;
}
function authors(paper) {
  return paper.authors.map((a) => a.name).join(", ");
}
function displayDate(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleDateString("en", {
        month: "short",
        day: "numeric",
        year: "numeric",
        timeZone: "UTC",
      });
}
function placeholder(paper) {
  const art = node("div", "text-cover");
  art.append(
    node(
      "span",
      "",
      paper.primary_category || paper.categories[0] || "RESEARCH",
    ),
    node("strong", "", paper.title),
    node("span", "", "↗"),
  );
  return art;
}
const cardCache = new Map();
const pageSize = 18;
let detailRequest = 0, visibleLimit = pageSize, filterKey = "", hasMore = false;
const pageObserver = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    }, { rootMargin: "0px 0px 240px 0px" })
  : null;

function loadMore() {
  if (!hasMore) return;
  visibleLimit += pageSize;
  renderGallery();
}
$("load-more").addEventListener("click", loadMore);

function renderGallery() {
  const search = $("search").value.trim().toLowerCase();
  const nextFilter = JSON.stringify([selectedKind, search, dateFrom, dateThrough, selectedCategory]);
  if (nextFilter !== filterKey) {
    visibleLimit = pageSize;
    if (filterKey) window.scrollTo({ top: 0, behavior: "instant" });
    filterKey = nextFilter;
  }
  const matching = papers.filter(
    (p) =>
      (selectedKind === "all" ||
        p.resources.some((r) => r.kind === selectedKind)) &&
      (!dateFrom || (p.paper.published_at && p.paper.published_at.slice(0, 10) >= dateFrom)) &&
      (!dateThrough || (p.paper.published_at && p.paper.published_at.slice(0, 10) <= dateThrough)) &&
      (!selectedCategory || p.paper.categories.includes(selectedCategory) ||
        p.paper.primary_category === selectedCategory) &&
      `${p.paper.title} ${authors(p.paper)} ${p.paper.abstract} ${p.paper.categories.join(" ")}`
        .toLowerCase()
        .includes(search),
  );
  const filterCount = Number(selectedKind !== "all") + Number(Boolean(dateFrom || dateThrough)) + Number(Boolean(selectedCategory));
  $("filter-badge").textContent = filterCount;
  $("filter-badge").hidden = filterCount === 0;
  $("gallery-filter-open").classList.toggle("has-filters", filterCount > 0);
  $("filter-results").textContent = `${matching.length} papers`;
  const visible = matching.slice(0, visibleLimit);
  hasMore = matching.length > visible.length;
  pageObserver?.unobserve($("gallery-pagination"));
  $("gallery-pagination").hidden = !hasMore;
  const wanted = new Set(visible.map((item) => item.key));
  for (const child of [...$("gallery").children]) {
    if (!wanted.has(child.dataset.key)) {
      child.querySelectorAll("video").forEach((video) => video.pause());
      child.remove();
      cardCache.delete(child.dataset.key);
    }
  }
  for (const [index, item] of visible.entries()) {
    const signature = JSON.stringify(item);
    const cached = cardCache.get(item.key);
    if (cached?.signature === signature) continue;
    const p = item.paper,
      card = node("article", "card"),
      cover = carousel(item, () => openDetail(item.key));
    const body = node("div", "card-body"),
      meta = node("div", "card-meta");
    meta.append(
      node(
        "span",
        "category",
        p.primary_category || p.categories[0] || "Research",
      ),
      node("time", "", displayDate(p.published_at)),
    );
    const title = node("button", "title-button", p.title);
    title.addEventListener("click", () => openDetail(item.key));
    const links = node("div", "resource-links"),
      seen = new Set();
    for (const resource of item.resources) {
      if (seen.has(resource.kind)) continue;
      const link = resourceLink(resource);
      if (link) {
        links.append(link);
        seen.add(resource.kind);
      }
    }
    const byline = node("p", "list-authors", authors(p));
    const abstract = node("p", "list-abstract", p.abstract || "No abstract available.");
    body.append(meta, title, byline, abstract, links);
    if (item.stale)
      body.append(node("span", "stale", "Showing previous resource results"));
    else if (item.problems?.length)
      body.append(node("span", "stale", item.problems[0].message));
    else if (item.warnings?.length)
      body.append(node("span", "stale", "Collected with resource warnings"));
    card.append(cover, body);
    card.dataset.key = item.key;
    if (cached) cached.card.replaceWith(card);
    else $("gallery").insertBefore(card, $("gallery").children[index] || null);
    cardCache.set(item.key, { signature, card });
  }
  $("empty").hidden = papers.length !== 0;
  $("no-matches").hidden = papers.length === 0 || visible.length !== 0;
  $("collection-count").textContent =
    `${visible.length} of ${matching.length} papers with resources`;
  if (hasMore) pageObserver?.observe($("gallery-pagination"));
}
async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(
      typeof body.detail === "string"
        ? body.detail
        : `The local app returned an error (HTTP ${response.status}). Please try again.`,
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}
async function loadGallery() {
  const data = await api("/api/gallery");
  papers = data.items;
  const categories = new Set(papers.flatMap((item) => [
    ...item.paper.categories, item.paper.primary_category,
  ]).filter(Boolean));
  if (selectedCategory) categories.add(selectedCategory);
  $("filter-category").replaceChildren(new Option("All categories", ""),
    ...[...categories].sort().map((category) => new Option(category, category)));
  $("filter-category").value = selectedCategory;
  renderGallery();
  if (data.saved > papers.length)
    $("collection-count").textContent += ` · ${data.saved} saved in total`;
}
async function openDetail(key) {
  const request = ++detailRequest;
  showError("detail-error", "");
  $("detail-dialog").setAttribute("aria-busy", "true");
  try {
    const item = await api(`/api/papers/${encodeURIComponent(key)}`),
      p = item.paper,
      result = item.result;
    if (request !== detailRequest) return;
    $("detail-title").textContent = p.title;
    $("detail-category").textContent =
      `${p.primary_category || p.categories[0] || "Research"} · ${displayDate(p.published_at)}`;
    $("detail-authors").textContent = authors(p);
    $("detail-abstract").textContent = p.abstract || "No abstract available.";
    $("detail-resources").replaceChildren();
    const findings = [...(item.problems || []), ...(item.warnings || [])];
    $("detail-quality").hidden = !findings.length;
    $("detail-quality").open = false;
    $("detail-quality-title").textContent = `${findings.length} resource notices`;
    $("detail-quality-items").replaceChildren(...findings.map(finding => {
      const row = node("li", "", `${finding.step}: ${finding.message} `);
      const url = safeUrl(finding.url);
      if (url) {
        const link = node("a", "", finding.url);
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        row.append(link);
      }
      return row;
    }));
    const all = [
      { url: `https://arxiv.org/abs/${p.arxiv_id}`, kind: "paper" },
      ...(result?.resources || []),
    ];
    for (const resource of uniqueResources(all)) {
      const link = resourceLink(resource);
      if (link) $("detail-resources").append(link);
    }
    if (!$("detail-dialog").open) $("detail-dialog").showModal();
    $("detail-dialog").scrollTop = 0;
  } catch (error) {
    if (request === detailRequest)
      showError($("detail-dialog").open ? "detail-error" : "page-error", error.message);
  } finally {
    if (request === detailRequest) $("detail-dialog").removeAttribute("aria-busy");
  }
}
function showError(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}
function showCollection() {
  $("collect-dialog").showModal();
  api("/api/settings/github").then(showGithubState).catch(error => {
    $("github-status").textContent = error.message;
  });
  poll();
}
function showGithubState(state) {
  githubConfigured = Boolean(state.configured);
  if (!githubConfigured) $("collect-advanced").open = true;
  $("github-status").textContent = state.configured
    ? (state.persisted ? "Token saved securely on this computer (not yet verified with GitHub)."
      : "Token configured (not yet verified with GitHub).")
    : "A GitHub token is required before collection can start.";
  if (state.storage_error) $("github-status").textContent += ` ${state.storage_error}.`;
  renderRun(lastRun);
}
async function updateGithubToken(clear) {
  const token = clear ? "" : $("github-token").value.trim();
  if (!clear && !token) {
    $("github-status").textContent = "Enter a token first.";
    return;
  }
  $("github-save").disabled = $("github-clear").disabled = true;
  try {
    showGithubState(await api("/api/settings/github", {
      method: "PUT",
      headers: {"Content-Type": "application/json", "X-PWP-App": "1"},
      body: JSON.stringify({token}),
    }));
  } catch (error) {
    $("github-status").textContent = error.message;
  } finally {
    $("github-token").value = "";
    $("github-save").disabled = $("github-clear").disabled = false;
  }
}
$("github-save").addEventListener("click", () => updateGithubToken(false));
$("github-clear").addEventListener("click", () => updateGithubToken(true));
$("github-token").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); updateGithubToken(false); }
});
$("collect-dialog").addEventListener("close", () => { $("github-token").value = ""; });
function renderRun(run) {
  lastRun = run;
  const running = run?.status === "running";
  $("running-dot").hidden = !running;
  $("collect-open").title = running
    ? `Collecting: ${run.phase === "harvesting" ? `${run.harvested} records received` : `${run.completed} / ${run.total} processed`}. View progress.`
    : "Collect papers or view the last collection";
  $("collect-fields").disabled = running;
  $("collect-fields").hidden = running;
  $("collect-advanced").hidden = running;
  $("collect-description").hidden = running;
  $("run-range").hidden = !running;
  $("run-range").textContent = running ? `${run.request.start} — ${run.request.end} · ${run.request.archive || "All archives"}` : "";
  $("stop-button").hidden = !running;
  $("stop-button").disabled = stopping;
  $("stop-button").textContent = stopping ? "Stopping…" : "Stop collection";
  $("start-button").hidden = running;
  $("start-button").disabled = running || !githubConfigured;
  $("start-button").textContent = running
    ? "Collecting…"
    : run && ["failed", "interrupted"].includes(run.status)
      ? "Try again ↗"
      : "Start collecting ↗";
  $("close-note").textContent = running
    ? "Closing this window keeps collection running."
    : "Saved on this computer.";
  $("run-status").hidden = !run;
  $("worker-status").hidden = !running || !run?.workers?.length;
  $("worker-list").replaceChildren(...(running ? run.workers || [] : []).map(work =>
    node("li", "", `${work.title} · ${work.stage} · ${Math.round(work.stage_elapsed_s)}s in stage / ${Math.round(work.elapsed_s)}s total`)
  ));
  if (!run) return;
  const phase = run.phase;
  $("phase-label").textContent =
    {
      harvesting: "Gathering metadata",
      resolving: "Finding resources",
      completed: "Collection complete",
      failed: "Collection could not finish",
      interrupted: "Collection stopped",
    }[phase] || phase;
  $("phase-count").textContent =
    phase === "harvesting"
      ? `${run.harvested} received`
      : `${run.completed} / ${run.total}`;
  if (phase === "harvesting") $("progress").removeAttribute("value");
  else {
    $("progress").max = Math.max(run.total, 1);
    $("progress").value =
      run.total === 0 && run.status === "completed" ? 1 : run.completed;
  }
  $("status-text").textContent =
    run.error ||
    (phase === "harvesting"
      ? "Checking arXiv for records updated in your date range. The total is not known yet."
      : `${run.with_resources} with resources · ${run.reused} reused · ${run.with_warnings || 0} with warnings · ${run.invalid} invalid · ${run.deleted} deleted`);
  if (running && run.concurrency) {
    $("status-text").textContent += ` · ${run.concurrency.active} active / ${run.concurrency.target} workers`;
  }
  $("current-paper").textContent =
    run.current ||
    (run.status === "completed" && run.total === 0
      ? "No active papers were found in this range."
      : "");
  $("failures").hidden = !run.failures.length;
  $("failures-title").textContent =
    `${run.failures.length} papers need another attempt`;
  $("failure-list").replaceChildren(
    ...run.failures.map((f) => node("li", "", `${f.id}: ${f.message}`)),
  );
}
async function poll() {
  if (busy) return;
  busy = true;
  try {
    const run = await api("/api/collection");
    if (!lastRun && run) {
      $("start").value = run.request.start;
      $("end").value = run.request.end;
      $("archive").value = run.request.archive;
      $("refresh").checked = run.request.refresh;
    }
    renderRun(run);
    const revision = JSON.stringify([
      run?.id,
      run?.harvested,
      run?.completed,
      run?.status,
    ]);
    if (revision !== lastRevision) {
      await loadGallery();
      lastRevision = revision;
    }
    showError("page-error", "");
  } catch (error) {
    showError(
      "page-error",
      error.status ? error.message : "Cannot reach the local app. Check that it is running; this page will reconnect automatically.",
    );
  } finally {
    busy = false;
  }
}
$("stop-button").addEventListener("click", async () => {
  if (stopping) return;
  stopping = true;
  renderRun(lastRun);
  showError("form-error", "");
  try {
    renderRun(await api("/api/collection/stop", {
      method: "POST", headers: { "X-PWP-App": "1" },
    }));
    await loadGallery();
  } catch (error) {
    showError("form-error", error.message);
  } finally {
    stopping = false;
    renderRun(lastRun);
  }
});
$("collect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!githubConfigured) {
    showError("form-error", "Configure a GitHub token before starting collection.");
    $("collect-advanced").open = true;
    $("github-token").focus();
    return;
  }
  showError("form-error", "");
  if ($("start").value > $("end").value) {
    showError("form-error", "Start date must not be after end date.");
    return;
  }
  $("start-button").disabled = true;
  try {
    const run = await api("/api/collection", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PWP-App": "1" },
      body: JSON.stringify({
        start: $("start").value,
        end: $("end").value,
        archive: $("archive").value,
        refresh: $("refresh").checked,
      }),
    });
    renderRun(run);
  } catch (error) {
    showError("form-error", error.message);
    $("start-button").disabled = !githubConfigured;
  }
});
$("collect-open").addEventListener("click", showCollection);
$("empty-collect").addEventListener("click", showCollection);
$("search").addEventListener("input", renderGallery);
document
  .querySelectorAll("[data-close]")
  .forEach((button) =>
    button.addEventListener("click", () => $(button.dataset.close).close()),
  );
document.querySelectorAll(".filter").forEach((button) =>
  button.addEventListener("click", () => {
    selectedKind = button.dataset.kind;
    document.querySelectorAll(".filter").forEach((b) => {
      b.classList.toggle("active", b === button);
      b.setAttribute("aria-pressed", String(b === button));
    });
    renderGallery();
  }),
);
const today = new Date(),
  yesterday = new Date();
yesterday.setDate(today.getDate() - 1);
const iso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
for (const id of ["start", "end"]) {
  $(id).max = iso(today);
  $(id).value = iso(yesterday);
}
poll();
setInterval(poll, 1800);

$("detail-dialog").addEventListener("close", () => {
  detailRequest++;
  $("detail-dialog").removeAttribute("aria-busy");
});
document.querySelectorAll("dialog").forEach((dialog) => {
  const outside = (event) => {
    const bounds = dialog.getBoundingClientRect();
    return event.clientX < bounds.left || event.clientX > bounds.right ||
      event.clientY < bounds.top || event.clientY > bounds.bottom;
  };
  let startedOutside = false;
  dialog.addEventListener("pointerdown", (event) => {
    startedOutside = event.target === dialog && outside(event);
  });
  dialog.addEventListener("click", (event) => {
    if (startedOutside && event.target === dialog && outside(event)) dialog.close();
    startedOutside = false;
  });
  dialog.addEventListener("pointercancel", () => { startedOutside = false; });
});
document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    $("gallery").classList.toggle("list-view", button.dataset.view === "list");
    document.querySelectorAll("[data-view]").forEach((item) =>
      item.setAttribute("aria-pressed", String(item === button)));
  });
});
function clearGalleryFilters() {
  dateFrom = dateThrough = selectedCategory = "";
  for (const id of ["filter-from", "filter-through", "filter-category"]) $(id).value = "";
  showError("filter-error", "");
  document.querySelector('[data-kind="all"]').click();
}
$("clear-gallery-filters").addEventListener("click", clearGalleryFilters);
for (const id of ["filter-from", "filter-through"]) {
  $(id).addEventListener("change", () => {
    const from = $("filter-from").value, through = $("filter-through").value;
    if (from && through && from > through) {
      showError("filter-error", "From must be on or before Through. Previous dates are still applied.");
      return;
    }
    showError("filter-error", "");
    dateFrom = from;
    dateThrough = through;
    renderGallery();
  });
}
$("filter-category").addEventListener("change", () => {
  selectedCategory = $("filter-category").value;
  renderGallery();
});
$("reset-filters").addEventListener("click", () => {
  $("search").value = "";
  clearGalleryFilters();
  $("search").focus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !document.querySelector("dialog[open]") &&
      !event.ctrlKey && !event.metaKey && !event.altKey &&
      !event.target.closest("input, textarea, select, [contenteditable]")) {
    event.preventDefault();
    $("search").focus();
  }
});

// A top-layer popover needs viewport coordinates, including the centered page margin.
function positionGalleryFilters() {
  const panel = $("gallery-filters");
  const anchor = $("gallery-filter-open").getBoundingClientRect();
  const viewportWidth = document.documentElement.clientWidth;
  const viewportHeight = window.innerHeight;
  const width = Math.min(420, viewportWidth - 32);
  const top = Math.max(16, Math.min(anchor.bottom + 12, viewportHeight - 80));
  panel.style.left = `${Math.max(16, Math.min(anchor.left, viewportWidth - width - 16))}px`;
  panel.style.top = `${top}px`;
  panel.style.maxHeight = `${Math.max(48, viewportHeight - top - 16)}px`;
}
$("gallery-filters").addEventListener("beforetoggle", (event) => {
  if (event.newState === "open") positionGalleryFilters();
});
let filterPositionFrame = 0;
function scheduleFilterPosition() {
  if (!$("gallery-filters").matches(":popover-open") || filterPositionFrame) return;
  filterPositionFrame = requestAnimationFrame(() => {
    filterPositionFrame = 0;
    positionGalleryFilters();
  });
}
window.addEventListener("resize", scheduleFilterPosition);
window.addEventListener("scroll", scheduleFilterPosition, { passive: true });
