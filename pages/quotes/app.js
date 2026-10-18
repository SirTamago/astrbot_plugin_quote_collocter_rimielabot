// 群友语录 · 管理页
// 通过 AstrBot 插件页 bridge 调用后端 API；图片直接用 extensions 路径加载。
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const API_BASE = "/api/v1/plugins/extensions/quote_collocter";

const RARITIES = [6, 5, 4, 3, 2, 1];
const stars = (n) => "★".repeat(n);

const $ = (id) => document.getElementById(id);

const state = {
  groupId: "",
  rarity: 0,
  query: "",
  page: 1,
  pageSize: 20,
  pages: 1,
  total: 0,
  items: [],
  weights: {},
};

// ---------------------------------------------------------------- 工具
function setMsg(text, ok = true) {
  const el = $("msg");
  el.classList.remove("ok", "err");
  if (!text) {
    el.textContent = "";
    return;
  }
  el.classList.add(ok ? "ok" : "err");
  el.textContent = text;
}

function option(value, label, selected = false) {
  const opt = document.createElement("option");
  opt.value = String(value);
  opt.textContent = label;
  if (selected) opt.selected = true;
  return opt;
}

function fillRaritySelects() {
  const filter = $("rarityFilter");
  const bulk = $("bulkRarity");
  filter.replaceChildren(option(0, "全部星级", true));
  bulk.replaceChildren();
  for (const r of RARITIES) {
    const rate = rateOf(r);
    const suffix = rate === null ? "" : `（${rate}）`;
    filter.appendChild(option(r, `${stars(r)}${suffix}`));
    bulk.appendChild(option(r, `${stars(r)}${suffix}`, r === 4));
  }
}

function rateOf(rarity) {
  const total = RARITIES.reduce((sum, r) => sum + (Number(state.weights[`weight_${r}`]) || 0), 0);
  const w = Number(state.weights[`weight_${rarity}`]) || 0;
  if (!total || !w) return null;
  return `${((w / total) * 100).toFixed(w / total < 0.1 ? 1 : 0)}%`;
}

function renderRateHint() {
  const parts = RARITIES.map((r) => {
    const rate = rateOf(r);
    return `${stars(r)} ${rate === null ? "不抽" : rate}`;
  });
  $("rateHint").textContent = `当前出率（按权重）：${parts.join("　")}；改权重请到「群友语录 · 管理面板」页。`;
}

function humanSize(bytes) {
  if (!bytes) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function selectedNames() {
  return [...document.querySelectorAll("#rows input.row-check:checked")].map((el) => el.dataset.name);
}

function updateSelectedCount() {
  const names = selectedNames();
  $("selectedCount").textContent = `已选 ${names.length} 张`;
}

// ---------------------------------------------------------------- 渲染
function renderRows() {
  const tbody = $("rows");
  tbody.replaceChildren();
  if (!state.items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "empty";
    td.textContent = state.groupId ? "没有符合条件的语录" : "填写群号后点「查询」";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const item of state.items) {
    const tr = document.createElement("tr");

    const tdCheck = document.createElement("td");
    tdCheck.className = "col-check";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "row-check";
    check.dataset.name = item.name;
    check.addEventListener("change", updateSelectedCount);
    tdCheck.appendChild(check);

    const tdThumb = document.createElement("td");
    tdThumb.className = "col-thumb";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = `${API_BASE}${item.image_url}`;
    img.alt = item.display;
    img.addEventListener("click", () => window.open(img.src, "_blank"));
    tdThumb.appendChild(img);

    const tdTitle = document.createElement("td");
    tdTitle.className = "col-title";
    tdTitle.textContent = item.display;
    tdTitle.title = item.name;

    const tdRarity = document.createElement("td");
    tdRarity.className = "col-rarity";
    const select = document.createElement("select");
    select.className = "rarity-select";
    for (const r of RARITIES) {
      const opt = option(r, stars(r), r === item.rarity);
      select.appendChild(opt);
    }
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
    const del = document.createElement("button");
    del.textContent = "删除";
    del.className = "link danger";
    del.addEventListener("click", () => deleteQuotes([item.name], item.display));
    tdActions.appendChild(del);

    tr.append(tdCheck, tdThumb, tdTitle, tdRarity, tdUser, tdTime, tdSize, tdActions);
    tbody.appendChild(tr);
  }
}

function renderPager() {
  $("pageInfo").textContent = `第 ${state.page} / ${state.pages} 页`;
  $("totalInfo").textContent = `共 ${state.total} 张`;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= state.pages;
  $("selectAll").checked = false;
  updateSelectedCount();
}

function applyRows(rows) {
  state.page = rows.page;
  state.pages = rows.pages;
  state.total = rows.total;
  state.items = rows.items;
  renderRows();
  renderPager();
}

// ---------------------------------------------------------------- 数据
async function loadSettings() {
  try {
    const settings = await bridge.apiGet("settings");
    state.weights = settings?.rarity_weights || {};
    fillRaritySelects();
    renderRateHint();
  } catch (e) {
    setMsg(`读取权重失败：${e.message}`, false);
  }
}

async function loadQuotes(page = 1) {
  const groupId = $("groupId").value.trim();
  setMsg("");
  if (!groupId) {
    setMsg("请先填写群号", false);
    return;
  }
  state.groupId = groupId;
  state.rarity = Number($("rarityFilter").value);
  state.query = $("query").value.trim();
  state.pageSize = Number($("pageSize").value);
  try {
    const rows = await bridge.apiGet("quotes", {
      group_id: state.groupId,
      rarity: state.rarity,
      query: state.query,
      page,
      page_size: state.pageSize,
    });
    applyRows(rows);
    setMsg(`已载入 ${rows.total} 张`, true);
  } catch (e) {
    setMsg(`查询失败：${e.message}`, false);
  }
}

async function changeRarity(names, rarity) {
  if (!names.length) return;
  try {
    const res = await bridge.apiPost("quotes/rarity", {
      group_id: state.groupId,
      names,
      rarity,
    });
    applyRows(res.rows);
    const parts = [`已改 ${res.changed} 张`];
    if (res.skipped) parts.push(`已是该档 ${res.skipped} 张`);
    if (res.failed?.length) parts.push(`失败 ${res.failed.length} 张`);
    setMsg(parts.join("，"), !res.failed?.length);
  } catch (e) {
    setMsg(`修改失败：${e.message}`, false);
  }
}

async function applyFiltered(rarity) {
  if (!state.groupId) {
    setMsg("请先查询一个群", false);
    return;
  }
  const scope = `${state.rarity ? stars(state.rarity) : "全部星级"}${state.query ? `、含「${state.query}」` : ""}`;
  if (!window.confirm(`把「${scope}」的全部 ${state.total} 张改成 ${stars(rarity)}？`)) return;
  try {
    const res = await bridge.apiPost("quotes/rarity", {
      group_id: state.groupId,
      rarity,
      filter_rarity: state.rarity,
      filter_query: state.query,
      page: state.page,
      page_size: state.pageSize,
    });
    applyRows(res.rows);
    const parts = [`已改 ${res.changed} 张`];
    if (res.skipped) parts.push(`已是该档 ${res.skipped} 张`);
    if (res.failed?.length) parts.push(`失败 ${res.failed.length} 张`);
    setMsg(parts.join("，"), !res.failed?.length);
  } catch (e) {
    setMsg(`批量修改失败：${e.message}`, false);
  }
}

async function deleteQuotes(names, label = "") {
  if (!names.length) return;
  const what = label ? `「${label}」` : `${names.length} 张语录`;
  if (!window.confirm(`确定删除${what}？会同时删除投稿记录，不可恢复。`)) return;
  try {
    const res = await bridge.apiPost("quotes/delete", {
      group_id: state.groupId,
      names,
    });
    await loadQuotes(state.page);
    setMsg(`已删除 ${res.deleted} 张${res.failed?.length ? `，失败 ${res.failed.length} 张` : ""}`, !res.failed?.length);
  } catch (e) {
    setMsg(`删除失败：${e.message}`, false);
  }
}

// ---------------------------------------------------------------- 事件
async function main() {
  await bridge.ready();
  await loadSettings();

  $("load").addEventListener("click", () => loadQuotes(1));
  $("groupId").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadQuotes(1);
  });
  $("query").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadQuotes(1);
  });
  $("rarityFilter").addEventListener("change", () => loadQuotes(1));
  $("pageSize").addEventListener("change", () => loadQuotes(1));

  $("selectAll").addEventListener("change", (e) => {
    document.querySelectorAll("#rows input.row-check").forEach((el) => {
      el.checked = e.target.checked;
    });
    updateSelectedCount();
  });

  $("applySelected").addEventListener("click", () => {
    const names = selectedNames();
    if (!names.length) {
      setMsg("先勾选要修改的行", false);
      return;
    }
    changeRarity(names, Number($("bulkRarity").value));
  });

  $("applyFiltered").addEventListener("click", () => applyFiltered(Number($("bulkRarity").value)));

  $("deleteSelected").addEventListener("click", () => {
    const names = selectedNames();
    if (!names.length) {
      setMsg("先勾选要删除的行", false);
      return;
    }
    deleteQuotes(names);
  });

  $("prev").addEventListener("click", () => {
    if (state.page > 1) loadQuotes(state.page - 1);
  });
  $("next").addEventListener("click", () => {
    if (state.page < state.pages) loadQuotes(state.page + 1);
  });
}

main();
