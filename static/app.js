/* 合同智能审核系统 前端逻辑（纯本地，无外部依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
let currentTaskId = null;
let currentResult = null;
let pollTimer = null;
let sysStatus = { llm_available: null };
let reviewBusy = false;
let pollInFlight = false;
let pollErrors = 0;

/* ================= 导航 ================= */
document.querySelectorAll(".nav-item[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "history") loadHistory();
  });
});

document.querySelectorAll(".subtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".subtab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".subpanel").forEach((p) => p.classList.add("hidden"));
    btn.classList.add("active");
    $("sub-" + btn.dataset.sub).classList.remove("hidden");
  });
});

/* ================= 系统状态 ================= */
async function refreshStatus() {
  try {
    const r = await fetch("/api/system/status");
    if (r.status === 401) { window.location.replace('/login'); return; }
    const s = await r.json();
    sysStatus = s;
    renderModelOptions(s.review_models || [], s.llm_model);
    setDot("dotLlm", s.llm_available ? "on" : "warn");
    $("txtLlm").textContent = s.llm_available ? "大模型" : "大模型未连接（可规则审查）";
    setDot("dotKb", s.kb_loaded ? "on" : "off");
    $("txtKb").textContent = s.kb_loaded ? `知识库 ${s.kb_total} 条` : "知识库未加载";
  } catch (e) {
    sysStatus = { llm_available: false };
    setDot("dotLlm", "off"); setDot("dotKb", "off");
  }
}
function setDot(id, state) {
  const el = $(id);
  el.classList.remove("dot-on", "dot-off", "dot-warn");
  el.classList.add("dot-" + state);
}
refreshStatus();
setInterval(refreshStatus, 30000);

function renderModelOptions(models, defaultModel) {
  const select = $("modelSelect");
  if (!select || !models.length) return;
  const previous = select.value;
  select.innerHTML = models.map((model) =>
    `<option value="${escapeHtml(model.value)}" ${model.installed ? "" : "disabled"}>${escapeHtml(model.label)}${model.installed ? "" : "（本机未安装）"}</option>`
  ).join("");
  const preferred = models.find((model) => model.value === previous && model.installed) ||
    models.find((model) => model.value === defaultModel && model.installed) ||
    models.find((model) => model.installed);
  if (preferred) select.value = preferred.value;
  select.disabled = !preferred;
  updateModelHint();
}

function updateModelHint() {
  const selected = (sysStatus.review_models || []).find((model) => model.value === $("modelSelect").value);
  $("modelHint").textContent = selected
    ? `本机已安装，可用上下文 ${Math.round(selected.num_ctx / 1024)}K；大模型越大，单份审核通常越慢`
    : "未检测到允许使用的本地模型";
}
$("modelSelect").addEventListener("change", updateModelHint);

/* ================= 上传与审核 ================= */
const dropzone = $("dropzone"), fileInput = $("fileInput");
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault(); dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; onFilePicked(); }
});
fileInput.addEventListener("change", onFilePicked);

function onFilePicked() {
  const f = fileInput.files[0];
  if (!f) return;
  $("dzTitle").textContent = f.name;
  $("btnStart").disabled = reviewBusy;
}

$("btnStart").addEventListener("click", async () => {
  if (reviewBusy) return;
  const file = fileInput.files[0];
  if (!file) return;
  const mode = document.querySelector('input[name="mode"]:checked').value;
  // 大模型未连接时，选了"完整审核"必须先明确告知并确认
  if (mode === "full" && sysStatus.llm_available === false) {
    const ok = confirm(
      "当前未连接到大模型服务，本次将自动降级为【规则引擎 + 知识库检索】审查：\n" +
      "· 无法进行法律/商业风险的智能分析\n" +
      "· 规则引擎检查和引用依据仍然有效\n\n" +
      "是否仍要继续？也可先检查 Ollama 服务后重试。");
    if (!ok) return;
  }
  const fd = new FormData();
  fd.append("file", file);
  fd.append("review_mode", mode);
  fd.append("party", document.querySelector('input[name="party"]:checked').value);
  fd.append("pay_side", document.querySelector('input[name="pay_side"]:checked').value);
  fd.append("review_model", $("modelSelect").value);
  $("btnStart").disabled = true;
  reviewBusy = true;
  stopPolling();
  $("btnStart").textContent = "上传中…";
  try {
    const r = await fetch("/api/review/upload", { method: "POST", body: fd });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || "上传失败");
    }
    const data = await r.json();
    currentTaskId = data.task_id;
    $("progressFile").textContent = file.name;
    $("progressPanel").classList.remove("hidden");
    $("resultWrap").classList.add("hidden");
    renderSteps([{ step: "排队等待", status: "running" }]);
    pollTimer = setInterval(pollStatus, 1500);
    $("btnCancel").classList.remove("hidden");
  } catch (e) {
    alert(e.message);
    reviewBusy = false;
  } finally {
    $("btnStart").disabled = reviewBusy;
    $("btnStart").textContent = reviewBusy ? "审核中…" : "开始审核";
  }
});

async function pollStatus() {
  if (!currentTaskId || pollInFlight) return;
  const taskId = currentTaskId;
  pollInFlight = true;
  try {
    const r = await fetch(`/api/review/status/${taskId}`);
    if (!r.ok) throw new Error(`读取任务失败（${r.status}）`);
    const s = await r.json();
    pollErrors = 0;
    if (taskId !== currentTaskId) return;
    if (s.status === "running" && s.result === null) {
      // P2-8：优先渲染后端同步来的真实步骤；未就绪前用占位兜底
      renderSteps((s.steps && s.steps.length ? s.steps : placeholderSteps()));
      return;
    }
    if (s.status === "completed") {
      stopPolling();
      finishReview();
      renderResult(s.result);
    } else if (["failed", "cancelled", "interrupted"].includes(s.status)) {
      stopPolling();
      finishReview();
      renderSteps([{ step: "审核失败：" + (s.error || "未知错误"), status: "error" }]);
    }
  } catch (e) {
    if (++pollErrors >= 5) {
      stopPolling(); finishReview();
      renderSteps([{step:"连接中断。任务可能仍在后台执行，请稍后从历史文档查看结果。",status:"error"}]);
    }
  } finally { pollInFlight = false; }
}

function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
function finishReview() {
  reviewBusy = false;
  $("btnStart").disabled = !fileInput.files.length;
  $("btnStart").textContent = "开始审核";
  $("btnCancel").classList.add("hidden");
}
$("btnCancel").addEventListener("click", async () => {
  if (!currentTaskId) return;
  const r = await fetch(`/api/review/cancel/${currentTaskId}`, {method:"POST"});
  const data = await r.json();
  alert(data.message || data.detail || "请求已提交");
});

function placeholderSteps() {
  return [
    { step: "合同解析", status: "done" },
    { step: "场景识别与规则引擎", status: "running" },
    { step: "知识库检索与大模型分析", status: "running" },
  ];
}

function renderSteps(steps) {
  const icons = { done: "●", running: "◐", skip: "○", error: "×" };
  $("stepList").innerHTML = steps.map((s) => `
    <li><span class="step-icon step-${s.status}">${icons[s.status] || "•"}</span>
    <span>${escapeHtml(s.step)}${s.detail ? " — " + escapeHtml(s.detail) : ""}</span></li>`).join("");
}

// 键与后端风险项 level 字段的实际取值一致（RiskLevel 枚举中文值）。
// 此前键为 high/medium/low/info，而后端返回的是"高风险/中风险/低风险/提示"，
// 导致 LEVEL[r.level] 恒为 undefined、所有卡片回退成"提示"，与顶部统计矛盾。
const LEVEL = {
  "高风险": { text: "高", cls: "badge-high" },
  "中风险": { text: "中", cls: "badge-medium" },
  "低风险": { text: "低", cls: "badge-low" },
  "提示": { text: "提示", cls: "badge-info" },
};

function renderResult(res) {
  currentResult = res;
  $("progressPanel").classList.add("hidden");
  $("resultWrap").classList.remove("hidden");
  $("resTitle").textContent = res.contract_title || res.filename || "审核结果";
  const modeText = ({complete:"所有批次已完成（需人工复核）",partial:"部分分析未完成",rules_only:"规则审查"})[res.completeness] || "历史报告（未记录覆盖范围）";
  let partyText = "未区分立场";
  const pc = res.party_context || {};
  if (res.party || pc.our_side) {
    const side = pc.our_side || res.party;
    const roles = (pc.our_roles || []).join("/");
    const pay = pc.we_pay === true ? "我方付款" : pc.we_pay === false ? "我方收款" : "未指定付款方向";
    partyText = `我方为${side}方${roles ? "（" + roles + "）" : ""} · ${pay}`;
  }
  $("resMeta").textContent =
    `合同类型：${res.contract_type} · 条款数：${res.clause_count} · 审核模型：${res.selected_model || res.provenance?.usage?.model || "未记录"} · 审核模式：${modeText} · ${partyText} · ${res.review_time}`;

  const exportState = res.export_control || {};
  const polishedAllowed = exportState.polished_report_allowed === true ||
    (!res.export_control && (res.completeness === "complete" || res.completeness === "rules_only"));
  const statusNote = $("resultStatusNote");
  statusNote.classList.toggle("incomplete", !polishedAllowed);
  statusNote.textContent = polishedAllowed
    ? "自动审核已完成。风险结论和法律适用仍需专业人员复核后使用。"
    : (exportState.reason || "审核未完整完成。PDF/CSV 已禁用，请先查看缺失批次和错误原因；原始 JSON 仍可导出排查。");
  document.querySelectorAll("[data-polished-export]").forEach((button) => {
    button.disabled = !polishedAllowed;
    button.title = polishedAllowed ? "" : "审核不完整，不能导出成完整报告";
  });

  const dn = $("degradedNote");
  const notices = [res.degraded_reason, ...(res.review_errors || []), ...(res.review_warnings || []),
    ...(res.document_warnings || [])].filter(Boolean);
  const activeKbDomains = new Set(sysStatus.kb_review_domains || ["legal_kb"]);
  const inactiveEvidenceDomains = [...new Set((res.evidence || [])
    .map((item) => item.kb_type).filter((domain) => domain && !activeKbDomains.has(domain)))];
  if (inactiveEvidenceDomains.length) {
    notices.unshift(`这是旧审核结果，保留了当前已停用知识域的历史快照：${inactiveEvidenceDomains.join("、")}。新审核不会使用这些条目。`);
  }
  const cov = res.coverage;
  if (cov) notices.unshift(`可读条款/表格块共${cov.total_clauses}个；法律模型覆盖${cov.legal_checked.length}个，商业模型覆盖${cov.commercial_checked.length}个。${cov.unreviewed_by_llm.length ? "尚有模型未完整检查的条款。" : ""}`);
  if (notices.length) { dn.textContent = notices.join("\n"); dn.classList.remove("hidden"); }
  else dn.classList.add("hidden");

  const s = res.summary;
  $("statRow").innerHTML = `
    ${statCard("high", "高风险", s.high)}${statCard("medium", "中风险", s.medium)}
    ${statCard("low", "低风险", s.low)}${statCard("info", "提示", s.info)}`;

  renderDocumentChecklist(res.document_checklist);

  const groups = [
    ["rule_risks", "规则引擎检查"],
    ["legal_risks", "法律与合规线索（需人工复核）"],
    ["commercial_risks", "商业风险"],
  ];
  const evs = res.evidence || [];
  let html = "";
  for (const [key, label] of groups) {
    const risks = res[key] || [];
    html += `<div class="risk-group"><div class="risk-group-title">${label}（${risks.length} 项）</div>`;
    if (!risks.length) {
      // 规则审查模式下法律/商业分析未执行，必须与"未发现风险"区分开
      const notRun = (key !== "rule_risks" && res.review_mode === "rules_only");
      const note = notRun
        ? (res.degraded_reason ? "未执行（大模型未连接，本次已降级）" : "未执行（本次为规则审查模式）")
        : (res.completeness === "partial" ? "分析未完整完成，不能据此判断无风险" : "本次检查未检出风险");
      html += `<table class="risk-table"><tr><td style="text-align:center;${notRun ? "color:var(--medium)" : "color:var(--ink-soft)"}">${note}</td></tr></table></div>`;
      continue;
    }
    html += `<table class="risk-table"><tr><th style="width:52px">等级</th><th style="width:26%">风险事项</th><th>说明 / 建议</th></tr>`;
    for (const r of risks) {
      const lv = LEVEL[r.level] || LEVEL["提示"];
      html += `<tr>
        <td><span class="badge ${lv.cls}">${lv.text}</span></td>
        <td><b>${escapeHtml(r.title)}</b>${r.clause_id ? `<br><span class="clause-ref">${escapeHtml(r.clause_id)}</span>` : ""}</td>
        <td>${escapeHtml(r.description || "")}
          ${r.original_text ? `<details><summary>查看合同原文</summary><blockquote>${escapeHtml(r.original_text)}</blockquote></details>` : ""}
          ${r.suggestion ? `<br><span class="suggestion">建议：${escapeHtml(r.suggestion)}</span>` : ""}
          ${r.legal_basis ? `<p class="risk-basis">参考依据：${escapeHtml(r.legal_basis)}</p>` : ""}
          <p class="review-state">${riskReviewText(r)}</p>
          ${(r.validation_issues || []).map(x => `<p class="review-state">${escapeHtml(x)}</p>`).join("")}
          ${(r.evidence || []).filter(x => typeof x === "string").map(x => `<a class="evidence-link" href="${evidenceHref(x, evs)}">${escapeHtml(x)}</a>`).join(" ")}
          ${(r.related_evidence || []).filter(x => typeof x === "string").map(x => `<a class="evidence-link" href="${evidenceHref(x, evs)}">相关材料：${escapeHtml(x)}</a>`).join(" ")}
        </td>
      </tr>`;
    }
    html += `</table></div>`;
  }
  $("risksBody").innerHTML = html;

  $("evidenceBody").innerHTML = evs.length
    ? evs.map((ev) => {
        const url = ev.source_url || "";
        const srcType = ev.source_type_label || { legal_kb: "法规", industry_kb: "行业规范", company_kb: "公司政策", template_kb: "范本" }[ev.kb_type] || "";
        const sourceBadge = srcType + (ev.kb_type && !activeKbDomains.has(ev.kb_type) ? "（历史停用）" : "");
return `<details class="evidence-item" id="ev-${encodeURIComponent(ev.evidence_id || "")}">
          <summary class="ev-title">${sourceBadge ? `<span class="ev-type">${escapeHtml(sourceBadge)}</span>` : ""}${escapeHtml(ev.citation || ev.title || "")}</summary>
          <div class="ev-content">${escapeHtml(ev.excerpt || ev.content || "")}</div>
          <div class="ev-src">${safeUrl(url) ? `溯源：<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a>` : ""}${ev.validity === "date_unverified" ? " · 适用日期待核实" : ""}</div>
        </details>`;
      }).join("")
    : '<div class="evidence-item">本次审核未产生证据引用</div>';
}

function evidenceHref(reference, evidence) {
  const value = String(reference || "").trim();
  const match = value.match(/^\[?(\d+)\]?$/);
  const legacy = match ? evidence[Number(match[1]) - 1] : null;
  const targetId = legacy?.evidence_id || value;
  return `#ev-${encodeURIComponent(targetId)}`;
}

function riskReviewText(r) {
  if (r.source === "rule") return "规则检查信号 · 仍需结合合同事实复核";
  if (r.evidence_status === "claim_aligned") return "需人工复核 · 法规主题与结论已机械核对，未确认法律适用";
  if (r.evidence_status === "topic_related") return "需人工复核 · 引用仅属相关材料，不能直接支撑结论";
  if (r.evidence_status === "not_applicable") return "商业风险判断 · 需人工复核";
  return "待核查线索 · 尚无直接法规支撑";
}

function renderDocumentChecklist(checklist) {
  const host = $("documentChecklist");
  if (!checklist || !Array.isArray(checklist.items)) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="document-checklist">
    <h3>全文要素检查</h3>
    <div class="checklist-items">${checklist.items.map((item) =>
      `<span class="check-item ${item.status === "present" ? "" : "missing"}">${escapeHtml(item.label)}：${item.status === "present" ? "已检出 " + escapeHtml((item.clause_ids || []).join("、")) : "全文未检出，待人工确认"}</span>`
    ).join("")}</div>
    <p class="checklist-note">${escapeHtml(checklist.notice || "")}</p>
  </div>`;
}

function statCard(cls, label, num) {
  return `<div class="stat-card c-${cls}"><div class="num">${num}</div><div class="lbl">${label}</div></div>`;
}

function exportReport(format) {
  if (!currentTaskId) { alert("请先完成一次审核"); return; }
  if (format !== "json" && currentResult && currentResult.export_control &&
      !currentResult.export_control.polished_report_allowed) {
    alert(currentResult.export_control.reason || "审核不完整，不能导出成完整报告");
    return;
  }
  window.open(`/api/review/export/${currentTaskId}?format=${format}`, "_blank");
}

/* ================= 法律检索 ================= */
async function loadDomains() {
  try {
    const r = await fetch("/api/kb/domains");
    const list = await r.json();
    $("domainSelect").innerHTML = `<option value="">全部知识域</option>` +
      list.map((d) => `<option value="${d.key}" ${d.enabled ? "" : "disabled"}>${d.label}（${d.count}${d.enabled ? "" : "，待补充"}）</option>`).join("");
  } catch (e) { /* 忽略 */ }
}
loadDomains();
doSearch();

$("btnSearch").addEventListener("click", doSearch);
$("searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });

async function doSearch() {
  const q = $("searchInput").value.trim();
  $("btnSearch").disabled = true;
  $("searchResults").innerHTML = '<div class="panel"><p style="color:var(--ink-soft)">检索中…</p></div>';
  try {
    const domain = $("domainSelect").value;
    const r = await fetch(`/api/kb/search?q=${encodeURIComponent(q)}${q ? "&top_k=12" : "&top_k=1000"}${domain ? "&domain=" + domain : ""}`);
    if (!r.ok) throw new Error("检索失败");
    const data = await r.json();
    $("searchMeta").classList.remove("hidden");
    $("searchMeta").innerHTML =
      `共 ${data.total} 条结果 · ${data.mode === "browse" ? "当前启用的全部法规" : `检索方式：<span class="mode-tag">${["vector","hybrid"].includes(data.mode) ? "向量与关键词" : "本地关键词"}</span>`}` +
      (data.contract_type && data.contract_type !== "general" ? ` · 识别场景：<span class="mode-tag">${data.contract_type}</span>` : "");
    $("searchResults").innerHTML = data.results.length
      ? data.results.map((x) => `
        <div class="panel evidence-item" style="border-left:3px solid var(--navy)">
          <div class="ev-title">${escapeHtml(x.title)}</div>
          <div class="ev-content">${escapeHtml(x.content)}</div>
          <div class="ev-src">
            ${safeUrl(x.source_url) ? `溯源：<a href="${escapeHtml(x.source_url)}" target="_blank" rel="noopener">${escapeHtml(x.source_url)}</a>` : ""}
            ${x.score == null ? "" : `<span class="ev-score">检索排序分 ${Number(x.score).toFixed(3)}（不代表法律判断正确率）</span>`}
          </div>
        </div>`).join("")
      : '<div class="panel"><p style="color:var(--ink-soft)">未检索到相关条文，请调整关键词</p></div>';
  } catch (e) {
    $("searchResults").innerHTML = `<div class="panel"><p style="color:var(--high)">${escapeHtml(e.message)}</p></div>`;
  } finally {
    $("btnSearch").disabled = false;
  }
}

/* ================= 历史文档 ================= */
async function loadHistory() {
  try {
    const r = await fetch("/api/review/history");
    const data = await r.json();
    const body = $("historyBody");
    if (!data.history.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">暂无审核记录</td></tr>';
      return;
    }
    body.innerHTML = data.history.map((h) => {
      const s = h.summary || {};
      const lvCls = { "高风险": "lv-high", "中风险": "lv-medium", "低风险": "lv-low" }[h.risk_level_text] || "lv-info";
      return `<tr>
        <td>${escapeHtml(h.filename || "")}</td>
        <td>${escapeHtml(h.contract_title || "—")}</td>
        <td>${escapeHtml(h.contract_type || "—")}</td>
        <td><span class="lv-text ${lvCls}">${escapeHtml(h.risk_level_text || "—")}</span>
            <small style="color:var(--ink-soft)">（高${s.high || 0} 中${s.medium || 0} 低${s.low || 0}）</small></td>
        <td>${escapeHtml(h.review_time || "")}</td>
        <td>
          <button class="link-btn" onclick="viewHistory('${h.task_id}')">查看</button>
          <button class="link-btn" ${h.export_control && !h.export_control.polished_report_allowed ? "disabled title=\"审核不完整，PDF不可导出\"" : ""} onclick="exportHistory('${h.task_id}','pdf')">PDF</button>
        </td>
      </tr>`;
    }).join("");
  } catch (e) {
    $("historyBody").innerHTML = '<tr><td colspan="6" class="empty">加载失败</td></tr>';
  }
}

function viewHistory(taskId) {
  stopPolling(); finishReview();
  document.querySelector('.nav-item[data-tab="review"]').click();
  currentTaskId = taskId;
  fetch(`/api/review/result/${taskId}`)
    .then((r) => { if (!r.ok) throw new Error("历史结果不可用"); return r.json(); })
    .then((res) => {
      $("progressPanel").classList.add("hidden");
      renderResult(res);
      window.scrollTo({ top: 0, behavior: "smooth" });
    })
    .catch(() => alert("加载历史结果失败"));
}

function exportHistory(taskId, format) {
  window.open(`/api/review/export/${taskId}?format=${format}`, "_blank");
}

/* ================= 工具 ================= */
function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function safeUrl(s) { try { return ["https:","http:"].includes(new URL(s).protocol); } catch { return false; } }
$("risksBody").addEventListener("click", (event) => {
  if (event.target.classList.contains("evidence-link")) {
    event.preventDefault();
    document.querySelector('.subtab[data-sub="evidence"]').click();
    const target = document.getElementById(event.target.getAttribute("href").slice(1));
    document.querySelectorAll("#evidenceBody details.evidence-item").forEach((item) => {
      item.open = item === target;
      item.classList.toggle("evidence-selected", item === target);
    });
    if (target) {
      history.replaceState(null, "", event.target.getAttribute("href"));
      requestAnimationFrame(() => target.scrollIntoView({behavior:"smooth", block:"center"}));
    }
  }
});
