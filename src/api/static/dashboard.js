"use strict";

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});
const integer = new Intl.NumberFormat("en-US");
const shortMoney = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateLabel = (value) =>
  new Date(`${value}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
const state = {
  days: 30,
  service: "",
  offset: 0,
  limit: 6,
  rows: [],
  summary: null,
};
let loadController;
let pageController;
const serviceColors = ["#81dabb", "#8badd9", "#b39ada", "#dcbb85", "#7abdc6"];

// API values only enter text nodes. Service names and detector labels are untrusted text.
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

async function request(path, signal) {
  const response = await fetch(path, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function showNotice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function serviceName(name) {
  const known = {
    "Amazon Elastic Compute Cloud - Compute": "EC2 Compute",
    "Amazon Simple Storage Service": "S3 Storage",
    "Amazon Relational Database Service": "RDS Database",
    AmazonCloudWatch: "CloudWatch",
    "Amazon CloudWatch": "CloudWatch",
    "Amazon Elastic Load Balancing": "Elastic Load Balancing",
    "Amazon Virtual Private Cloud": "VPC",
    "AWS Key Management Service": "Key Management Service",
  };
  return Object.hasOwn(known, name) ? known[name] : name;
}

function renderSummary(summary) {
  state.summary = summary;
  const formattedSpend = money.format(summary.total_spend);
  $("total-spend").textContent = formattedSpend;
  $("total-spend").style.setProperty("--spend-length", formattedSpend.length);
  $("spend-period").textContent = `Over the last ${state.days} days`;
  $("anomaly-total").textContent = integer.format(summary.anomalies);
  $("nav-count").textContent = integer.format(summary.anomalies);
  $("service-total").textContent = integer.format(summary.services_tracked);
  const statuses = summary.alerts_by_status;
  $("pending-total").textContent = integer.format(
    (statuses.pending || 0) + (statuses.partial || 0) + (statuses.failed || 0),
  );
  const daily = summary.daily_totals;
  $("daily-average").textContent = daily.length
    ? money.format(summary.total_spend / daily.length)
    : "—";
  $("data-through").textContent = daily.length
    ? `Latest recorded day: ${dateLabel(daily.at(-1).usage_date)}`
    : "No cost history in this period";
  renderServices(summary);
  renderChart(daily);
}

function renderServices(summary) {
  const list = $("service-list");
  list.replaceChildren();
  if (!summary.top_services.length) {
    list.append(
      element("p", "empty-state", "No service spend in this period."),
    );
    return;
  }
  summary.top_services.forEach((service, index) => {
    const share =
      summary.total_spend > 0 ? (service.total / summary.total_spend) * 100 : 0;
    const item = element("div", "service-item");
    const top = element("div", "service-topline");
    const name = element("span", "service-name", serviceName(service.service));
    name.title = service.service;
    top.append(
      name,
      element("span", "service-amount", money.format(service.total)),
    );
    const track = element("div", "service-track");
    const fill = element("span", "service-fill");
    fill.style.width = `${Math.max(0, Math.min(100, share))}%`;
    fill.style.backgroundColor = serviceColors[index % serviceColors.length];
    track.append(fill);
    const bar = element("div", "service-bar-line");
    bar.append(
      track,
      element("span", "service-percentage", `${share.toFixed(1)}%`),
    );
    item.append(top, bar);
    list.append(item);
  });
}

function svgNode(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, value]) =>
    node.setAttribute(name, value),
  );
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderChart(daily) {
  const container = $("spend-chart");
  container.replaceChildren();
  if (!daily.length) {
    const empty = element("div", "empty-state");
    empty.append(
      element("strong", "", "No spend recorded"),
      element("span", "", "There are no cost records in the selected period."),
    );
    container.append(empty);
    return;
  }
  const width = Math.max(300, container.clientWidth);
  const height = container.clientHeight;
  const left = 47,
    right = width - 14,
    top = 17,
    bottom = height - 29;
  const firstDate = Date.parse(daily[0].usage_date);
  const dateSpan = Date.parse(daily.at(-1).usage_date) - firstDate;
  const max = Math.max(1, ...daily.map((day) => day.total)) * 1.15;
  const min = Math.min(0, ...daily.map((day) => day.total));
  const x = (day) =>
    dateSpan
      ? left +
        ((Date.parse(day.usage_date) - firstDate) / dateSpan) * (right - left)
      : (left + right) / 2;
  const y = (total) => bottom - ((total - min) / (max - min)) * (bottom - top);
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "group",
    "aria-label":
      "Daily cloud spend in USD. Focus a data point to see its date and amount.",
  });
  svg.append(
    svgNode("title", {}, `Daily spend across ${daily.length} recorded days`),
  );
  const defs = svgNode("defs");
  const gradient = svgNode("linearGradient", {
    id: "chart-fill",
    x1: "0",
    y1: "0",
    x2: "0",
    y2: "1",
  });
  gradient.append(
    svgNode("stop", {
      offset: "0%",
      "stop-color": "#7be3be",
      "stop-opacity": ".16",
    }),
    svgNode("stop", {
      offset: "100%",
      "stop-color": "#7be3be",
      "stop-opacity": ".005",
    }),
  );
  defs.append(gradient);
  svg.append(defs);
  for (let i = 0; i <= 3; i++) {
    const total = min + ((max - min) * i) / 3;
    svg.append(
      svgNode("line", {
        x1: left,
        x2: right,
        y1: y(total),
        y2: y(total),
        class: "chart-grid",
      }),
    );
    svg.append(
      svgNode(
        "text",
        {
          x: left - 10,
          y: y(total) + 3,
          "text-anchor": "end",
          class: "chart-label",
        },
        shortMoney.format(total),
      ),
    );
  }
  const labelCount = Math.min(width < 450 ? 3 : 5, daily.length);
  for (let i = 0; i < labelCount; i++) {
    const day =
      daily[
        labelCount === 1
          ? 0
          : Math.round((i * (daily.length - 1)) / (labelCount - 1))
      ];
    svg.append(
      svgNode(
        "text",
        {
          x: x(day),
          y: height - 6,
          "text-anchor":
            i === 0 ? "start" : i === labelCount - 1 ? "end" : "middle",
          class: "chart-label",
        },
        dateLabel(day.usage_date),
      ),
    );
  }
  const points = daily.map((day) => `${x(day)},${y(day.total)}`).join(" L ");
  svg.append(
    svgNode("path", {
      d: `M ${x(daily[0])},${bottom} L ${points} L ${x(daily.at(-1))},${bottom} Z`,
      fill: "url(#chart-fill)",
    }),
  );
  svg.append(svgNode("path", { d: `M ${points}`, class: "chart-line" }));
  const indicator = svgNode("circle", {
    r: 4,
    class: "chart-point",
    visibility: daily.length === 1 ? "visible" : "hidden",
    cx: x(daily[0]),
    cy: y(daily[0].total),
  });
  const tooltip = svgNode("g", {
    class: "chart-tooltip",
    visibility: "hidden",
  });
  const tooltipWidth = Math.min(190, right - left);
  tooltip.append(
    svgNode("rect", { width: tooltipWidth, height: 30, rx: 5 }),
    svgNode("text", { x: 10, y: 19 }),
  );
  daily.forEach((day, index) => {
    const prior = daily[index - 1],
      next = daily[index + 1];
    const hitLeft = prior ? (x(prior) + x(day)) / 2 : left;
    const hitRight = next ? (x(next) + x(day)) / 2 : right;
    const hit = svgNode("rect", {
      x: hitLeft,
      y: top,
      width: Math.max(1, hitRight - hitLeft),
      height: bottom - top,
      class: "chart-hit",
      tabindex: "0",
      role: "img",
      "aria-label": `${day.usage_date}: ${money.format(day.total)}`,
    });
    const show = () => {
      indicator.setAttribute("cx", x(day));
      indicator.setAttribute("cy", y(day.total));
      indicator.setAttribute("visibility", "visible");
      tooltip.setAttribute("visibility", "visible");
      tooltip.setAttribute(
        "transform",
        `translate(${Math.min(right - tooltipWidth, Math.max(left, x(day) - tooltipWidth / 2))},${Math.max(0, y(day.total) - 42)})`,
      );
      tooltip.lastChild.textContent = `${dateLabel(day.usage_date)} · ${money.format(day.total)}`;
    };
    const hide = () => {
      indicator.setAttribute(
        "visibility",
        daily.length === 1 ? "visible" : "hidden",
      );
      tooltip.setAttribute("visibility", "hidden");
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", show);
    hit.addEventListener("blur", hide);
    svg.append(hit);
  });
  svg.append(indicator, tooltip);
  container.append(svg);
}

function deviation(record) {
  if (record.baseline_mean === null || record.baseline_mean === 0) return "—";
  const percent =
    ((record.amount - record.baseline_mean) / Math.abs(record.baseline_mean)) *
    100;
  return `${percent >= 0 ? "+" : ""}${percent.toFixed(1)}%`;
}

function renderAnomalies(page) {
  state.rows = page.items;
  $("anomaly-count").textContent = integer.format(page.total);
  const body = $("anomaly-rows");
  body.replaceChildren();
  if (!page.items.length) {
    const row = element("tr");
    const cell = element(
      "td",
      "table-empty",
      "No anomalies found for this period and service.",
    );
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
  page.items.forEach((record) => {
    const row = element("tr");
    const service = element("td");
    const serviceWrap = element("div", "table-service");
    const name = serviceName(record.service);
    serviceWrap.title = record.service;
    const monogram = element(
      "span",
      "service-monogram",
      name
        .replace(/^(AWS|Amazon)\s+/, "")
        .slice(0, 2)
        .toUpperCase(),
    );
    monogram.setAttribute("aria-hidden", "true");
    serviceWrap.append(monogram, element("span", "", name));
    service.append(serviceWrap);
    const amount = element("td", "numeric", money.format(record.amount));
    const baseline = element(
      "td",
      "numeric",
      record.baseline_mean === null ? "—" : money.format(record.baseline_mean),
    );
    const delta = element("td", "numeric");
    delta.append(
      element(
        "span",
        `deviation${record.amount < record.baseline_mean ? " drop" : ""}`,
        deviation(record),
      ),
    );
    const action = element("td");
    const button = element("button", "details-button");
    button.type = "button";
    button.setAttribute(
      "aria-label",
      `View anomaly for ${record.service} on ${record.usage_date}`,
    );
    button.append(icon("arrow"));
    button.addEventListener("click", () => showDetails(record));
    action.append(button);
    row.append(
      service,
      element("td", "", dateLabel(record.usage_date)),
      amount,
      baseline,
      delta,
      action,
    );
    body.append(row);
  });
  $("page-info").textContent = page.total
    ? `Showing ${state.offset + 1}–${Math.min(state.offset + state.limit, page.total)} of ${integer.format(page.total)} anomalies`
    : "0 anomalies";
  $("previous").disabled = state.offset === 0;
  $("next").disabled = state.offset + state.limit >= page.total;
}

function showDetails(record) {
  $("dialog-title").textContent = record.service;
  $("dialog-date").textContent = `Usage date · ${record.usage_date} (UTC)`;
  const values = $("dialog-values");
  values.replaceChildren();
  const details = [
    ["Actual spend", money.format(record.amount)],
    [
      "Expected daily spend",
      record.baseline_mean === null
        ? "Unavailable"
        : money.format(record.baseline_mean),
    ],
    ["Deviation from baseline", deviation(record)],
    [
      "Z-score",
      record.zscore === null ? "Unavailable" : `${record.zscore.toFixed(2)} σ`,
    ],
    ["Detector", record.detector || "Unavailable"],
  ];
  details.forEach(([label, value]) => {
    const row = element("div", "detail-row");
    row.append(element("dt", "", label), element("dd", "", value));
    values.append(row);
  });
  $("anomaly-dialog").showModal();
}

function renderConnection(ready) {
  $("provider").textContent =
    ready.cost_provider === "synthetic"
      ? "Synthetic demo data"
      : "AWS Cost Explorer";
  $("provider").classList.toggle(
    "synthetic",
    ready.cost_provider === "synthetic",
  );
  $("system-status").textContent = "Database connected";
  $("status-dot").className = "status-dot ready";
}

function populateServiceFilter(services) {
  const select = $("service-filter");
  select.replaceChildren(element("option", "", "All services"));
  select.firstChild.value = "";
  services.forEach((service) => {
    const option = element("option", "", serviceName(service));
    option.value = service;
    select.append(option);
  });
  if (services.includes(state.service)) select.value = state.service;
  else state.service = "";
}

async function loadAnomalies(signal) {
  const query = new URLSearchParams({
    days: state.days,
    anomalies_only: true,
    limit: state.limit,
    offset: state.offset,
  });
  if (state.service) query.set("service", state.service);
  const page = await request(`/costs/?${query}`, signal);
  renderAnomalies(page);
}

function setBusy(busy) {
  $("dashboard").setAttribute("aria-busy", String(busy));
  $("refresh").disabled = busy;
  $("date-range").disabled = busy;
  $("service-filter").disabled = busy;
  $("previous").disabled = true;
  $("next").disabled = true;
}

async function loadDashboard() {
  loadController?.abort();
  pageController?.abort();
  const controller = new AbortController();
  loadController = controller;
  const { signal } = controller;
  setBusy(true);
  showNotice("");
  let loaded = false;
  try {
    const [summaryResult, servicesResult, readyResult] =
      await Promise.allSettled([
        request(`/costs/summary?days=${state.days}&top=5`, signal),
        request("/costs/services", signal),
        request("/ready", signal),
      ]);
    if (signal.aborted) return;
    if (summaryResult.status === "rejected") throw summaryResult.reason;
    renderSummary(summaryResult.value);
    if (servicesResult.status === "fulfilled")
      populateServiceFilter(servicesResult.value);
    if (readyResult.status === "fulfilled") renderConnection(readyResult.value);
    else {
      $("system-status").textContent = "Connection check failed";
      $("status-dot").className = "status-dot error";
      $("provider").textContent = "Provider unavailable";
      $("provider").classList.remove("synthetic");
    }
    await loadAnomalies(signal);
    if (
      servicesResult.status === "rejected" ||
      readyResult.status === "rejected"
    ) {
      showNotice(
        "Spend data loaded, but some connection or service information is unavailable. Refresh to try again.",
      );
    }
    $("updated").textContent =
      `Last refreshed ${new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`;
    loaded = true;
  } catch (error) {
    if (error.name !== "AbortError") {
      showNotice(
        "Couldn't load the dashboard. Check that the API and database are running, then refresh. Any figures still visible are from the previous load.",
      );
      $("system-status").textContent = "Connection needs attention";
      $("status-dot").className = "status-dot error";
      $("updated").textContent = "Refresh failed · data may be out of date";
      if (!state.summary) {
        $("spend-chart").replaceChildren(
          element("p", "empty-state", "Spend history is unavailable."),
        );
        $("service-list").replaceChildren(
          element("p", "empty-state", "Service spend is unavailable."),
        );
      }
      if (!state.rows.length)
        $("anomaly-rows").firstElementChild.firstElementChild.textContent =
          "Anomaly data is unavailable. Refresh to try again.";
      $("page-info").textContent = "Data unavailable";
    }
  } finally {
    if (!signal.aborted) {
      $("dashboard").setAttribute("aria-busy", "false");
      $("refresh").disabled = false;
      $("date-range").disabled = false;
      $("service-filter").disabled = !loaded;
    }
  }
}

async function changePage(offset) {
  const previousOffset = state.offset;
  state.offset = offset;
  pageController?.abort();
  pageController = new AbortController();
  const { signal } = pageController;
  setBusy(true);
  showNotice("");
  let loaded = false;
  try {
    await loadAnomalies(signal);
    loaded = true;
  } catch (error) {
    if (error.name !== "AbortError") {
      state.offset = previousOffset;
      showNotice(
        "Couldn't load these anomalies. The table still shows the previous results. Refresh to try again.",
      );
    }
  } finally {
    if (!signal.aborted) {
      $("dashboard").setAttribute("aria-busy", "false");
      $("refresh").disabled = false;
      $("date-range").disabled = false;
      $("service-filter").disabled = !loaded;
    }
  }
}

$("refresh").addEventListener("click", () => {
  state.offset = 0;
  loadDashboard();
});
$("date-range").addEventListener("change", (event) => {
  state.days = Number(event.target.value);
  state.offset = 0;
  loadDashboard();
});
$("service-filter").addEventListener("change", (event) => {
  state.service = event.target.value;
  changePage(0);
});
$("previous").addEventListener("click", () =>
  changePage(Math.max(0, state.offset - state.limit)),
);
$("next").addEventListener("click", () =>
  changePage(state.offset + state.limit),
);
$("close-dialog").addEventListener("click", () => $("anomaly-dialog").close());
$("anomaly-dialog").addEventListener("click", (event) => {
  if (event.target === $("anomaly-dialog")) {
    const bounds = event.target.getBoundingClientRect();
    if (
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom
    )
      event.target.close();
  }
});
function updateNavigation() {
  const section = window.location.hash || "#overview";
  document.querySelectorAll("nav .nav-link").forEach((link) => {
    const active = link.getAttribute("href") === section;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
  const names = {
    "#overview": "Overview",
    "#spend": "Spend analytics",
    "#anomalies": "Anomalies",
  };
  document.querySelector(".breadcrumb strong").textContent =
    names[section] || "Overview";
}
window.addEventListener("hashchange", updateNavigation);
let resizeFrame;
new ResizeObserver(() => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    if (state.summary) renderChart(state.summary.daily_totals);
  });
}).observe($("spend-chart"));
updateNavigation();
loadDashboard();
