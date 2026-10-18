// 语录管理（内嵌在 admin 页面里）
// 用 bridge 调后端 API；缩略图走插件扩展路由（同一 dashboard 源，带 cookie）。
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const API_PREFIX = "/api/v1/plugins/extensions";

const RARITIES = [6, 5, 4, 3, 2, 1];
const stars = (n) => "★".repeat(n);
const SORTABLE = ["display", "rarity", "submitter", "time", "size"];

const $ = (id) => document.getElementById(id);

const q = {
  groupId: "",
  rarity: 0,
  query: "",
  page: 1,
  pageSize: 20,
  pages: 1,
  total: 0,
  sort: "name",
  order: "asc",
  items: [],
  weights: {},
};

// ---------------------------------------------------------------- 工具
function qMsg(text, ok = true) {
  const el = $("q_msg");
  el.classList.remove("ok", "err");
  el.textContent = text || "";
  if (text) el.classList.add(ok ? "ok" : "err");
}

function option(value, label, selected = false) {
  const opt = document.createElement("option");
  opt.value = String(value);
  opt.textContent = label;
  if (selected) opt.selected = true;
  return opt;
}

function rateOf(rarity) {
  const total = RARITIES.reduce((sum, r) => sum + (Number(q.weights[`weight_${r}`]) || 0), 0);
  const w = Number(q.weights[`weight_${rarity}`]) || 0;
  if (!total || !w) return null;
  return `${((w / total) * 100).toFixed(w / total < 0.1 ? 1 : 0)}%`;
}

function fillRaritySelects() {
  const filter = $("q_rarity");
  const bulk = $("q_bulk_rarity");
  filter.replaceChildren(option(0, "全部星级", true));
  bulk.replaceChildren();
  for (const r of RARITIES) {
    const rate = rateOf(r);
    const suffix = rate ? `（${rate}）` : "";
    filter.appendChild(option(r, `${stars(r)}${suffix}`));
    bulk.appendChild(option(r, `${stars(r)}${suffix}`, r === 4));
  }
  const parts = RARITIES.map((r) => `${stars(r)} ${rateOf(r) || "不抽"}`);
  $("q_rate_hint").textContent = `当前出率（按权重）：${parts.join("　")}`;
}

function humanSize(bytes) {
  if (!bytes) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function selectedNames() {
  return [...document.querySelectorAll("#q_rows input.row-check:checked")].map(
    (el) => el.dataset.name,
  );
}

function updateSelected() {
  $("q_selected").textContent = `已选 ${selectedNames().length} 张`;
}

function updateSortMarks() {
  document.querySelectorAll("th[data-sort] .sort-mark").forEach((el) => {
    const th = el.closest("th");
    el.textContent = th.dataset.sort === q.sort ? (q.order === "asc" ? "▲" : "▼") : "";
  });
}

// ---------------------------------------------------------------- 渲染
function renderRows() {
  const tbody = $("q_rows");
  tbody.replaceChildren();
  if (!q.items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "empty";
    td.textContent = q.groupId ? "没有符合条件的语录" : "填写群号后点「查询」";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const item of q.items) {
    const tr = document.createElement("tr");

    const tdCheck = document.createElement("td");
    tdCheck.className = "col-check";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "row-check";
    check.dataset.name = item.name;
    check.addEventListener("change", updateSelected);
    tdCheck.appendChild(check);

    const tdThumb = document.createElement("td");
    tdThumb.className = "col-thumb";
    const img = document.createElement("img");
    img.loading = "lazy";
    // 后端返回的是 "/quote_collocter/quotes/image?...", 只加扩展路由前缀
    img.src = `${API_PREFIX}${item.image_url}`;
    img.alt = item.display;
    img.title = "点击查看大图";
    img.addEventListener("click", () => window.open(img.src, "_blank"));
    img.addEventListener("error", () => {
      tdThumb.textContent = "×";
      tdThumb.classList.add("thumb-error");
      tdThumb.title = "图片加载失败";
    });
    tdThumb.appendChild(img);

    const tdTitle = document.createElement("td");
    tdTitle.className = "col-title";
    tdTitle.textContent = item.display;
    tdTitle.title = item.name;

    const tdRarity = document.createElement("td");
    tdRarity.className = "col-rarity";
    const select = document.createElement("select");
    select.className = "rarity-select";
    for (const r of RARITIES) select.appendChild(option(r, stars(r), r === item.rarity));
    select.addEventListener("change", () => changeRarity([item.name], Number(select.value)));
    tdRarity.appendChild(select);

    const tdUser = document.createElement("td");
    tdUser.textContent = item.submitter || "-";

    const tdTime = document.createElement("td");
    tdTime.textContent = item.time || "-";

    const tdSize = document.createElement("td");
    tdSize.className = "col-size";
    tdSize.textContent = humanSize(item.size);

    const tdActions = document.createElement("td");
    tdActions.className = "col-actions";
    const rename = document.createElement("button");
    rename.textContent = "改名";
    rename.className = "link";
    rename.addEventListener("click", () => renameQuote(item));
    const del = document.createElement("button");
    del.textContent = "删除";
    del.className = "link danger";
    del.addEventListener("click", () => deleteQuotes([item.name], item.display));
    tdActions.append(rename, del);

    tr.append(tdCheck, tdThumb, tdTitle, tdRarity, tdUser, tdTime, tdSize, tdActions);
    tbody.appendChild(tr);
  }
}

function renderPager() {
  $("q_page_info").textContent = `第 ${q.page} / ${q.pages} 页`;
  $("q_total").textContent = `共 ${q.total} 张`;
  $("q_prev").disabled = q.page <= 1;
  $("q_next").disabled = q.page >= q.pages;
  $("q_select_all").checked = false;
  updateSelected();
  updateSortMarks();
}

function applyRows(rows) {
  q.page = rows.page;
  q.pages = rows.pages;
  q.total = rows.total;
  q.sort = rows.sort || q.sort;
  q.order = rows.order || q.order;
  q.items = rows.items;
  renderRows();
  renderPager();
}

function queryPayload(extra = {}) {
  return {
    group_id: q.groupId,
    filter_rarity: q.rarity,
    filter_query: q.query,
    page: q.page,
    page_size: q.pageSize,
    sort: q.sort,
    order: q.order,
    ...extra,
  };
}

// ---------------------------------------------------------------- 数据
async function loadSettings() {
  try {
    const settings = await bridge.apiGet("settings");
    q.weights = settings?.rarity_weights || {};
    fillRaritySelects();
  } catch (e) {
    qMsg(`读取权重失败：${e.message}`, false);
  }
}

async function loadQuotes(page = 1) {
  const groupId = $("q_group").value.trim();
  qMsg("");
  if (!groupId) {
    qMsg("请先填写群号", false);
    return;
  }
  q.groupId = groupId;
  q.rarity = Number($("q_rarity").value);
  q.query = $("q_query").value.trim();
  q.pageSize = Number($("q_page_size").value);
  try {
    const rows = await bridge.apiGet("quotes", {
      group_id: q.groupId,
      rarity: q.rarity,
      query: q.query,
      page,
      page_size: q.pageSize,
      sort: q.sort,
      order: q.order,
    });
    applyRows(rows);
    qMsg(`已载入 ${rows.total} 张`);
  } catch (e) {
    qMsg(`查询失败：${e.message}`, false);
  }
}

async function changeRarity(names, rarity) {
  if (!names.length) return;
  try {
    const res = await bridge.apiPost("quotes/rarity", queryPayload({ names, rarity }));
    applyRows(res.rows);
    const parts = [`已改 ${res.changed} 张`];
    if (res.skipped) parts.push(`已是该档 ${res.skipped} 张`);
    if (res.failed?.length) parts.push(`失败 ${res.failed.length} 张`);
    qMsg(parts.join("，"), !res.failed?.length);
  } catch (e) {
    qMsg(`修改失败：${e.message}`, false);
  }
}

async function applyFiltered(rarity) {
  if (!q.groupId) {
    qMsg("请先查询一个群", false);
    return;
  }
  const scope = `${q.rarity ? stars(q.rarity) : "全部星级"}${q.query ? `、含「${q.query}」` : ""}`;
  if (!window.confirm(`把「${scope}」的全部 ${q.total} 张改成 ${stars(rarity)}？`)) return;
  try {
    const res = await bridge.apiPost("quotes/rarity", queryPayload({ rarity }));
    applyRows(res.rows);
    const parts = [`已改 ${res.changed} 张`];
    if (res.skipped) parts.push(`已是该档 ${res.skipped} 张`);
    if (res.failed?.length) parts.push(`失败 ${res.failed.length} 张`);
    qMsg(parts.join("，"), !res.failed?.length);
  } catch (e) {
    qMsg(`批量修改失败：${e.message}`, false);
  }
}

async function renameQuote(item) {
  const dot = item.display.lastIndexOf(".");
  const current = dot > 0 ? item.display.slice(0, dot) : item.display;
  const next = window.prompt(`把 ${stars(item.rarity)} 的标题改成：`, current);
  if (next === null) return;
  const title = next.trim();
  if (!title || title === current) return;
  try {
    const res = await bridge.apiPost("quotes/rename", queryPayload({ name: item.name, new_title: title }));
    applyRows(res.rows);
    qMsg(res.message || "已改名");
  } catch (e) {
    qMsg(`改名失败：${e.message}`, false);
  }
}

async function deleteQuotes(names, label = "") {
  if (!names.length) return;
  const what = label ? `「${label}」` : `${names.length} 张语录`;
  if (!window.confirm(`确定删除${what}？会同时删除投稿记录，不可恢复。`)) return;
  try {
    const res = await bridge.apiPost("quotes/delete", { group_id: q.groupId, names });
    await loadQuotes(q.page);
    qMsg(
      `已删除 ${res.deleted} 张${res.failed?.length ? `，失败 ${res.failed.length} 张` : ""}`,
      !res.failed?.length,
    );
  } catch (e) {
    qMsg(`删除失败：${e.message}`, false);
  }
}

async function resetStats() {
  if (!q.groupId) {
    qMsg("请先查询一个群", false);
    return;
  }
  if (!window.confirm("清零本群累计出货计数？（不影响保底计数与图库）")) return;
  try {
    await bridge.apiPost("quotes/stats/reset", { group_id: q.groupId });
    qMsg("已清零本群出货计数");
  } catch (e) {
    qMsg(`清零失败：${e.message}`, false);
  }
}

// ---------------------------------------------------------------- 事件
function bindSorting() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (!SORTABLE.includes(key)) return;
      if (q.sort === key) q.order = q.order === "asc" ? "desc" : "asc";
      else {
        q.sort = key;
        q.order = key === "rarity" || key === "size" || key === "time" ? "desc" : "asc";
      }
      if (q.groupId) loadQuotes(1);
      else updateSortMarks();
    });
  });
}

async function main() {
  await bridge.ready();
  bindSorting();
  await loadSettings();

  $("q_load").addEventListener("click", () => loadQuotes(1));
  ["q_group", "q_query"].forEach((id) => {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadQuotes(1);
    });
  });
  $("q_rarity").addEventListener("change", () => loadQuotes(1));
  $("q_page_size").addEventListener("change", () => loadQuotes(1));

  $("q_select_all").addEventListener("change", (e) => {
    document.querySelectorAll("#q_rows input.row-check").forEach((el) => {
      el.checked = e.target.checked;
    });
    updateSelected();
  });

  $("q_apply_selected").addEventListener("click", () => {
    const names = selectedNames();
    if (!names.length) {
      qMsg("先勾选要修改的行", false);
      return;
    }
    changeRarity(names, Number($("q_bulk_rarity").value));
  });
  $("q_apply_filtered").addEventListener("click", () =>
    applyFiltered(Number($("q_bulk_rarity").value)),
  );
  $("q_delete_select").addEventListener("click", () => {
    const names = selectedNames();
    if (!names.length) {
      qMsg("先勾选要删除的行", false);
      return;
    }
    deleteQuotes(names);
  });
  $("q_reset_stats").addEventListener("click", resetStats);

  $("q_prev").addEventListener("click", () => {
    if (q.page > 1) loadQuotes(q.page - 1);
  });
  $("q_next").addEventListener("click", () => {
    if (q.page < q.pages) loadQuotes(q.page + 1);
  });
}

main();
