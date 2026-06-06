(() => {
  const SIGNAL_KEY = "trendRisk.signalState.v3";
  const SCROLL_KEY = "trendRisk.scrollState.v3";
  const BACKTEST_KEY = "trendRisk.backtestState.v3";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const signalForm = $("#signal-form");
  const configForm = $("#config-form");
  const signalPane = $("#signal-pane");
  const resultPane = $(".result-pane");
  const popover = $("#hover-popover");
  let applyingAutoFill = false;
  let fetchInFlight = false;
  let lastBacktestResult = null;
  let lastCandidateQuery = "";
  const BACKTEST_METRIC_COLUMNS = ["指标", "数值", "备注"];
  const BACKTEST_METRIC_INLINE_COLUMNS = ["指标", "数值"];
  const VOLUME_SIGNAL_NAMES = ["volume_confirm", "pullback_volume_dry", "upper_shadow", "failed_close", "far_from_ma"];

  const strategyInfo = {
    defensive: "防守：买入更慢，卖出更快；适合不想承受大回撤。",
    balanced: "均衡：趋势、止损、仓位三者平衡。",
    aggressive: "进攻：盈利后加仓更快，但破位清仓不打折。"
  };

  const marketNames = {
    US: "美股/海外",
    CN: "A股/国内基金",
    auto: "自动"
  };

  function debounce(fn, wait = 220) {
    let timer = null;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), wait);
    };
  }

  function isChoiceCheckbox(el) {
    return el.type === "checkbox" && el.name && el.value && el.value !== "on";
  }

  function formToObject(form) {
    const data = {};
    const elements = Array.from(form.elements).filter(el => el.name && !el.disabled);

    for (const el of elements) {
      if (el.type === "checkbox") {
        if (isChoiceCheckbox(el)) {
          if (el.checked) data[el.name] = el.value;
        } else {
          data[el.name] = el.checked;
        }
      } else if (el.type === "radio") {
        if (el.checked) data[el.name] = el.value;
      } else {
        data[el.name] = el.value;
      }
    }
    return data;
  }

  function restoreForm(form, data) {
    if (!data) return;
    for (const [name, value] of Object.entries(data)) {
      const fields = Array.from(form.querySelectorAll(`[name="${CSS.escape(name)}"]`));
      for (const field of fields) {
        if (field.type === "checkbox") {
          if (isChoiceCheckbox(field)) {
            field.checked = field.value === value;
          } else {
            field.checked = Boolean(value);
          }
        } else if (field.type === "radio") {
          field.checked = field.value === value;
        } else {
          field.value = value;
        }
      }
    }
  }

  function saveSignalState() {
    if (!signalForm) return;
    localStorage.setItem(SIGNAL_KEY, JSON.stringify(formToObject(signalForm)));
  }

  function loadSignalState() {
    try {
      const raw = localStorage.getItem(SIGNAL_KEY);
      if (!raw || !signalForm) return;
      restoreForm(signalForm, JSON.parse(raw));
    } catch {}
  }

  function saveScrollState() {
    const state = {
      signalTop: signalPane ? signalPane.scrollTop : window.scrollY,
      resultTop: resultPane ? resultPane.scrollTop : 0
    };
    localStorage.setItem(SCROLL_KEY, JSON.stringify(state));
  }

  function restoreScrollState() {
    try {
      const raw = localStorage.getItem(SCROLL_KEY);
      if (!raw) return;
      const state = JSON.parse(raw);
      requestAnimationFrame(() => {
        if (signalPane) signalPane.scrollTop = state.signalTop || 0;
        if (resultPane) resultPane.scrollTop = state.resultTop || 0;
      });
    } catch {}
  }

  function toConfigPayload() {
    const data = formToObject(configForm);
    const riskFreeRaw = data.backtest_risk_free_rate_pct;
    return {
      plan_amount: Number(data.plan_amount || 0),
      current_position_amount: Number(data.current_position_amount || 0),
      current_profit_pct: Number(data.current_profit_pct || 0),
      risk_per_trade_pct: Number(data.risk_per_trade_pct || 1),
      backtest_risk_free_rate_pct: riskFreeRaw === undefined || riskFreeRaw === "" ? 2 : Number(riskFreeRaw),
      strategy: data.strategy || "balanced",
      position_mode: data.position_mode || "core_satellite",
      symbol: data.symbol || "",
      symbol_name: data.symbol_name || "",
      market: data.market || "auto",
      asset_kind: data.asset_kind || "auto",
      data_source: data.data_source || "auto",
      valuation_method: data.valuation_method || "auto",
      proxy_mode: data.proxy_mode || "system",
      proxy_url: data.proxy_url || "",
      request_timeout_sec: Number(data.request_timeout_sec || 12),
      retry_count: Number(data.retry_count || 2),
      danjuan_cookie: data.danjuan_cookie || ""
    };
  }

  function setBusy(button, busyText, restoreText = null) {
    if (!button) return () => {};
    const oldText = button.textContent;
    button.disabled = true;
    button.textContent = busyText;
    return () => {
      button.disabled = false;
      button.textContent = restoreText || oldText;
    };
  }

  function renderList(el, items, emptyText) {
    if (!el) return;
    el.innerHTML = "";
    const frag = document.createDocumentFragment();
    const list = items && items.length ? items : [emptyText];
    for (const item of list) {
      const li = document.createElement("li");
      li.textContent = item;
      frag.appendChild(li);
    }
    el.appendChild(frag);
  }

  function renderMetrics(metrics) {
    const host = $("#metrics");
    if (!host) return;
    host.innerHTML = "";

    const items = Array.isArray(metrics)
      ? metrics
      : Object.entries(metrics || {}).map(([label, value]) => ({label, value}));

    const frag = document.createDocumentFragment();
    for (const item of items) {
      const box = document.createElement("div");
      box.className = item.wide ? "metric wide" : "metric";
      box.innerHTML = `<span></span><strong></strong>`;
      box.querySelector("span").textContent = item.label;
      box.querySelector("strong").textContent = item.value;
      frag.appendChild(box);
    }
    host.appendChild(frag);

  }

  function renderDecision(payload) {
    const result = payload.result;
    const panel = $("#result-panel");
    if (!result || !panel) return;

    panel.className = `result-card ${result.action}`;

    const actionEl = $("#result-action");
    if (actionEl) {
      actionEl.textContent = result.action_text || result.action;
      actionEl.className = `result-action-text ${result.action_tone || "hold"}`;
    }

    $("#result-confidence").textContent = `${result.confidence}%`;
    $("#result-headline").textContent = result.headline;
    $("#result-headline").className = `result-headline-text ${result.action_tone || "hold"}`;
    $("#result-subline").textContent = result.subline;

    renderMetrics(result.metrics);
    renderList($("#warning-list"), result.warnings, "暂无额外风险/限制");
  }

  async function postJSON(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || "请求失败");
    }
    return data;
  }

  function showToast(text, isError = false) {
    const toast = $("#config-toast");
    if (!toast) return;
    toast.hidden = false;
    toast.textContent = text;
    toast.style.borderLeftColor = isError ? "var(--red)" : "var(--green)";
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.hidden = true, 2200);
  }

  function initHoverDetails() {
    if (!popover) return;

    document.addEventListener("pointerover", (event) => {
      const target = event.target.closest("[data-tip]");
      if (!target) return;
      popover.textContent = target.dataset.tip;
      popover.hidden = false;
    });

    document.addEventListener("pointermove", (event) => {
      if (popover.hidden) return;
      const pad = 14;
      const rect = popover.getBoundingClientRect();
      let left = event.clientX + 14;
      let top = event.clientY + 14;

      if (left + rect.width + pad > window.innerWidth) left = window.innerWidth - rect.width - pad;
      if (top + rect.height + pad > window.innerHeight) top = event.clientY - rect.height - 14;

      popover.style.left = `${Math.max(pad, left)}px`;
      popover.style.top = `${Math.max(pad, top)}px`;
    });

    document.addEventListener("pointerout", (event) => {
      if (event.target.closest("[data-tip]")) popover.hidden = true;
    });
  }

  function enforceToggleGroups(changed) {
    if (!changed || !changed.checked) return;

    if (changed.name === "volume_state") {
      VOLUME_SIGNAL_NAMES.forEach(name => setCheck(name, false));
      return;
    }

    if (VOLUME_SIGNAL_NAMES.includes(changed.name)) {
      setChoice("volume_state", "__clear__");
      return;
    }

    if (!isChoiceCheckbox(changed)) return;
    const groupName = changed.name;
    $$(`input[type="checkbox"][name="${CSS.escape(groupName)}"]`, signalForm)
      .forEach(el => {
        if (el !== changed && isChoiceCheckbox(el)) el.checked = false;
      });
  }

  function setChoice(name, value) {
    if (!signalForm) return;
    const fields = $$(`input[type="checkbox"][name="${CSS.escape(name)}"]`, signalForm);
    if (value === "__clear__") {
      fields.forEach(el => { el.checked = false; });
      return;
    }
    const normalized = value || "none";
    fields.forEach(el => {
      el.checked = el.value === normalized;
    });
  }

  function fieldLabel(input) {
    return input ? input.closest("label") : null;
  }

  function clearAutoFilledMarks(root = signalForm) {
    if (!root) return;
    $$(".is-auto-filled", root).forEach(el => el.classList.remove("is-auto-filled"));
  }

  function clearAutoMarkForInput(input) {
    if (!input || !signalForm) return;
    if (input.name === "volume_state") {
      VOLUME_SIGNAL_NAMES.forEach(name => {
        const signal = signalForm.querySelector(`input[type="checkbox"][name="${CSS.escape(name)}"]`);
        fieldLabel(signal)?.classList.remove("is-auto-filled");
      });
    }
    if (VOLUME_SIGNAL_NAMES.includes(input.name)) {
      const none = signalForm.querySelector('input[type="checkbox"][name="volume_state"][value="none"]');
      fieldLabel(none)?.classList.remove("is-auto-filled");
    }
    if (isChoiceCheckbox(input)) {
      $$(`input[type="checkbox"][name="${CSS.escape(input.name)}"]`, signalForm)
        .forEach(el => fieldLabel(el)?.classList.remove("is-auto-filled"));
    } else {
      fieldLabel(input)?.classList.remove("is-auto-filled");
    }
  }

  function markChoiceAuto(name, value) {
    if (!signalForm || !value) return;
    const input = signalForm.querySelector(`input[type="checkbox"][name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`);
    if (input && input.checked) fieldLabel(input)?.classList.add("is-auto-filled");
  }

  function markCheckAuto(name, active) {
    if (!signalForm || !active) return;
    const input = signalForm.querySelector(`input[type="checkbox"][name="${CSS.escape(name)}"]`);
    if (input && input.checked) fieldLabel(input)?.classList.add("is-auto-filled");
  }

  function setInput(name, value) {
    if (!signalForm) return;
    const el = signalForm.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!el) return;
    el.value = value === null || value === undefined ? "" : value;
  }

  function resetAutoFetchedFields() {
    if (!signalForm) return;

    // 每次重新拉取前先清空旧标的的自动数据，避免新数据缺失时沿用旧 PE/ROE/PB/止损/量价状态。
    clearAutoFilledMarks();
    ["pe_percentile", "pb_percentile", "roe_pct", "stop_loss_pct"].forEach(name => setInput(name, ""));

    setChoice("market_state", "sideways");
    setChoice("entry_state", "none");
    setChoice("exit_state", "none");
    setChoice("profit_state", "none");
    setChoice("volume_state", "none");

    VOLUME_SIGNAL_NAMES.forEach(name => setCheck(name, false));

    renderAutoData({
      source_used: "--",
      valuation_note: "--"
    });
    saveSignalState();
    calculateDebounced();
  }

  function setCheck(name, value) {
    if (!signalForm) return;
    const el = signalForm.querySelector(`input[type="checkbox"][name="${CSS.escape(name)}"]`);
    if (el && !isChoiceCheckbox(el)) el.checked = Boolean(value);
  }


  function autoApplyInitialStopFromInputs() {
    if (!signalForm || !configForm) return;
    const currentAmount = Number(configForm.querySelector('[name="current_position_amount"]')?.value || 0);
    const profitPct = Number(configForm.querySelector('[name="current_profit_pct"]')?.value || 0);
    const stopPct = Number(signalForm.querySelector('[name="stop_loss_pct"]')?.value || 0);
    const hitStopInput = signalForm.querySelector('input[type="checkbox"][name="exit_state"][value="hit_stop"]');
    const hitStopLabel = fieldLabel(hitStopInput);
    const shouldHitStop = currentAmount > 0 && stopPct > 0 && profitPct <= -stopPct;

    if (shouldHitStop) {
      setChoice("exit_state", "hit_stop");
      markChoiceAuto("exit_state", "hit_stop");
      saveSignalState();
      return;
    }

    // 只撤销“系统自动勾选”的初始止损；如果是用户手动勾选，不主动覆盖。
    if (hitStopInput?.checked && hitStopLabel?.classList.contains("is-auto-filled")) {
      setChoice("exit_state", "none");
      hitStopLabel.classList.remove("is-auto-filled");
      markChoiceAuto("exit_state", "none");
      saveSignalState();
    }
  }

  function updateStrategySummary() {
    const summary = $("#strategy-summary");
    if (!summary || !configForm) return;
    const data = toConfigPayload();
    const modeText = data.position_mode === "core_satellite" ? "长期底仓 + 交易仓（防守仓位动态计算）" : "纯交易仓";
    const assetText = data.symbol ? `${data.symbol_name || data.symbol} · ${data.symbol} · ${marketNames[data.market] || data.market} · ${data.asset_kind}` : "未选择标的";
    summary.innerHTML = `${strategyInfo[data.strategy] || strategyInfo.balanced}<br>仓位模式：${modeText}<br>标的：${assetText}<br>数据容错：代理 ${data.proxy_mode || "system"} / 超时 ${data.request_timeout_sec || 12} 秒 / 重试 ${data.retry_count || 0} 次<br>回测无风险收益率：${data.backtest_risk_free_rate_pct ?? 2}%<br>计划资金=100%上限，不按标的类型封顶。`;

    const selected = $("#selected-asset");
    if (selected) {
      selected.querySelector("strong").textContent = data.symbol ? `${data.symbol_name || data.symbol} · ${data.symbol}` : "未选择";
      selected.querySelector("em").textContent = `${data.market || "auto"} / ${data.asset_kind || "auto"} / ${data.data_source || "auto"}`;
    }
  }

  async function calculateNow({quiet = true} = {}) {
    if (!signalForm) return;
    saveSignalState();
    saveScrollState();

    try {
      const payload = formToObject(signalForm);
      const data = await postJSON("/api/decision", payload);
      renderDecision(data);
      restoreScrollState();
    } catch (err) {
      if (!quiet) alert(err.message || "计算失败");
    }
  }

  const calculateDebounced = debounce(() => calculateNow({quiet: true}), 160);

  async function saveConfigNow({quiet = true, recalc = true} = {}) {
    if (!configForm) return;

    try {
      const data = await postJSON("/api/config", toConfigPayload());
      $("#current-position-text").textContent = data.current_pos_text || "--";
      updateStrategySummary();
      if (!quiet) showToast(data.message || "配置已保存");
      if (recalc) calculateDebounced();
    } catch (err) {
      if (!quiet) showToast(err.message || "配置保存失败", true);
    }
  }

  const saveConfigDebounced = debounce(() => saveConfigNow({quiet: true, recalc: true}), 260);

  function renderCandidates(results, query = "") {
    const host = $("#asset-candidates");
    if (!host) return;
    host.innerHTML = "";
    lastCandidateQuery = query || lastCandidateQuery || "";
    const displayResults = Array.isArray(results) ? [...results].reverse() : [];
    if (!displayResults.length) {
      host.innerHTML = `<div class="candidate-empty">没有找到候选。可直接输入代码后搜索，或手动填写中间数据。</div>`;
      setSearchOpen(true);
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of displayResults) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "candidate-item";
      btn.innerHTML = `<strong></strong><span></span><em></em>`;
      btn.querySelector("strong").textContent = item.symbol;
      btn.querySelector("span").textContent = item.name || item.symbol;
      btn.querySelector("em").textContent = `${item.market || "auto"} / ${item.asset_kind || "auto"} / ${item.source || "auto"}`;
      btn.addEventListener("click", async () => {
        chooseAsset(item);
        clearSearchResults();
        await saveConfigNow({quiet: true, recalc: true});
        await fetchSelectedData();
      });
      frag.appendChild(btn);
    }
    host.appendChild(frag);
    setSearchOpen(true);
  }

  function chooseAsset(item) {
    if (!configForm) return;
    const mapping = {
      symbol: item.symbol || "",
      symbol_name: item.name || item.symbol || "",
      market: item.market || "auto",
      asset_kind: item.asset_kind || "auto",
      data_source: item.source || item.data_source || "auto"
    };
    for (const [name, value] of Object.entries(mapping)) {
      const el = configForm.querySelector(`[name="${CSS.escape(name)}"]`);
      if (el) el.value = value;
    }
    updateStrategySummary();
  }

  function clearSearchResults(clearInput = true) {
    const host = $("#asset-candidates");
    const input = $("#asset-search-input");
    if (host) host.innerHTML = "";
    if (input && clearInput) input.value = "";
    lastCandidateQuery = "";
    setSearchOpen(false);
  }

  function setSearchOpen(open) {
    const box = $(".asset-search-box");
    if (!box) return;
    box.classList.toggle("is-open", Boolean(open));
  }

  function toggleSearchBox() {
    const box = $(".asset-search-box");
    if (!box) return;
    setSearchOpen(!box.classList.contains("is-open"));
  }

  function openSettingsModal() {
    const modal = $("#settings-modal");
    if (!modal) return;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => modal.classList.add("is-open"));
    const editor = $("#index-map-editor");
    if (editor && !editor.value.trim()) loadIndexMap();
  }

  function closeSettingsModal() {
    const modal = $("#settings-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    window.setTimeout(() => {
      if (!modal.classList.contains("is-open")) modal.hidden = true;
    }, 180);
  }

  function renderConnectionTest(data) {
    const host = $("#connection-test-results");
    if (!host) return;
    const results = data.results || [];
    if (!results.length) {
      host.innerHTML = `<div class="test-empty">暂无测试结果</div>`;
      return;
    }
    host.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const item of results) {
      const card = document.createElement("details");
      card.className = `connection-test-item ${item.ok ? "ok" : "fail"}`;
      const status = item.ok ? "成功" : "失败";
      const statusCode = item.status_code == null ? "--" : item.status_code;
      const rows = item.rows == null ? "--" : item.rows;
      const parsed = item.parsed ? JSON.stringify(item.parsed, null, 2) : "--";
      const preview = item.response_preview || item.error || "--";
      card.open = !item.ok;
      card.innerHTML = `
        <summary><strong>${item.name}</strong><span>${status} / ${item.elapsed_ms || 0}ms</span></summary>
        <div class="test-line"><b>URL</b><code></code></div>
        <div class="test-grid">
          <span>状态码：${statusCode}</span>
          <span>行数/条数：${rows}</span>
        </div>
        <div class="test-block"><b>解析结果</b><pre></pre></div>
        <div class="test-block"><b>响应预览 / 错误</b><pre></pre></div>
      `;
      card.querySelector("code").textContent = item.url || "--";
      const pres = card.querySelectorAll("pre");
      pres[0].textContent = parsed;
      pres[1].textContent = preview;
      frag.appendChild(card);
    }
    host.appendChild(frag);
  }

  async function runConnectionTest() {
    const btn = $("#connection-test-btn");
    const restore = setBusy(btn, "测试中…");
    const host = $("#connection-test-results");
    if (host) host.innerHTML = `<div class="test-empty">正在测试接口连通性…</div>`;
    try {
      await saveConfigNow({quiet: true, recalc: false});
      const data = await postJSON("/api/connectivity-test", toConfigPayload());
      renderConnectionTest(data);
      showToast("接口连通性测试完成");
    } catch (err) {
      if (host) host.innerHTML = `<div class="test-empty error"></div>`;
      const el = host?.querySelector(".test-empty");
      if (el) el.textContent = err.message || "测试失败";
      showToast(err.message || "测试失败", true);
    } finally {
      restore();
    }
  }

  async function clearRuntimeCache() {
    const btn = $("#cache-clear-btn");
    const restore = setBusy(btn, "清除中…");
    try {
      const data = await postJSON("/api/cache/clear", {});
      showToast(data.message || "缓存已清除");
    } catch (err) {
      showToast(err.message || "清除缓存失败", true);
    } finally {
      restore();
    }
  }


  async function searchAsset() {
    const q = $("#asset-search-input")?.value?.trim();
    const host = $("#asset-candidates");
    if (!q) {
      // 只有搜索框为空时，点击【搜索】才作为“收起下拉栏”。
      clearSearchResults(false);
      return;
    }

    // 参数相同且已有候选时，不重复搜索；如果下拉栏被收起，则重新展开。
    // 注意：非空输入再次点击【搜索】不再关闭下拉栏，避免误操作。
    if (lastCandidateQuery === q && host?.children.length) {
      setSearchOpen(true);
      return;
    }

    if (host) {
      host.innerHTML = `<div class="candidate-empty">正在搜索“${escapeHTML(q)}”…</div>`;
    }
    // 非空搜索期间也保持下拉栏展开；否则按 Enter 时会先折叠，等结果回来再展开，交互很跳。
    setSearchOpen(true);
    const btn = $("#asset-search-btn");
    const restore = setBusy(btn, "搜索中…");
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.message || "搜索失败");
      renderCandidates(data.results || [], q);
      if (data.cache?.hit) showToast("搜索结果来自缓存");
    } catch (err) {
      showToast(err.message || "搜索失败", true);
    } finally {
      restore();
    }
  }

  function profitName(value) {
    return {
      none: "无明显盈利",
      profit_1r: "盈利≥1R",
      profit_2r: "盈利≥2R",
      profit_3r: "盈利≥3R"
    }[value || ""] || "--";
  }

  function volumeSignalText(data) {
    const parts = [];
    if (data.volume_confirm) parts.push("突破放量");
    if (data.pullback_volume_dry) parts.push("回踩缩量");
    if (data.upper_shadow) parts.push("放量长上影");
    if (data.failed_close) parts.push("收盘未站稳");
    if (data.far_from_ma) parts.push("远离均线");
    return parts.length ? parts.join(" / ") : "暂无明显量价信号";
  }

  function renderAutoData(data) {
    const host = $("#auto-data-list");
    if (!host) return;
    const items = [
      ["成功链路", data.source_used],
      ["代理模式", data.proxy_mode],
      ["日期", data.last_date],
      ["收盘", data.close],
      ["MA20", data.ma20],
      ["MA50", data.ma50],
      ["MA200", data.ma200],
      ["ATR14", data.atr14],
      ["20日量比", data.volume_ratio_20d],
      ["当前PE", data.current_pe],
      ["PE百分位", data.pe_percentile == null ? null : `${Number(data.pe_percentile).toFixed(2)}%`],
      ["当前PB", data.current_pb],
      ["PB百分位", data.pb_percentile == null ? null : `${Number(data.pb_percentile).toFixed(2)}%`],
      ["ROE", data.roe_pct == null ? null : `${Number(data.roe_pct).toFixed(2)}%`],
      ["估值来源", data.valuation_source],
      ["估值提示", data.valuation_note],
      ["当前涨跌幅", data.profit_pct == null ? null : `${Number(data.profit_pct).toFixed(2)}%`],
      ["盈利R倍数", data.profit_r == null ? null : `${Number(data.profit_r).toFixed(2)}R`],
      ["自动盈利阶段", profitName(data.profit_state)],
      ["自动量价", volumeSignalText(data)]
    ];
    host.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const [label, value] of items) {
      const box = document.createElement("div");
      box.className = "auto-data-item";
      box.innerHTML = `<span></span><strong></strong>`;
      box.querySelector("span").textContent = label;
      box.querySelector("strong").textContent = value === null || value === undefined || value === "" ? "--" : value;
      frag.appendChild(box);
    }
    host.appendChild(frag);

    if (Array.isArray(data.fetch_trace) && data.fetch_trace.length) {
      const trace = document.createElement("details");
      trace.className = "fetch-trace";
      trace.innerHTML = `<summary>数据源尝试记录</summary><ul></ul>`;
      const ul = trace.querySelector("ul");
      for (const item of data.fetch_trace) {
        const li = document.createElement("li");
        const ok = item.ok ? "成功" : "失败";
        const attempt = item.attempt == null ? "" : ` 第${item.attempt}次`;
        const extra = item.ok ? `${item.rows || 0}条 / ${item.elapsed_ms || 0}ms` : (item.error || "失败");
        li.textContent = `${item.source}${attempt}：${ok}（${extra}）`;
        ul.appendChild(li);
      }
      host.appendChild(trace);
    }
  }

  function applyFetchedIndicators(ind) {
    if (!ind) return;
    applyingAutoFill = true;
    clearAutoFilledMarks();

    setInput("stop_loss_pct", ind.stop_loss_pct);
    setInput("pe_percentile", ind.pe_percentile);
    setInput("pb_percentile", ind.pb_percentile);
    setInput("roe_pct", ind.roe_pct == null ? "" : Number(ind.roe_pct).toFixed(2));

    const marketState = ind.market_state || "sideways";
    const entryState = ind.entry_state || "none";
    const exitState = ind.exit_state || "none";
    const profitState = ind.profit_state || "none";

    setChoice("market_state", marketState);
    setChoice("entry_state", entryState);
    setChoice("exit_state", exitState);
    setChoice("profit_state", profitState);
    setCheck("volume_confirm", Boolean(ind.volume_confirm));
    setCheck("pullback_volume_dry", Boolean(ind.pullback_volume_dry));
    setCheck("upper_shadow", Boolean(ind.upper_shadow));
    setCheck("failed_close", Boolean(ind.failed_close));
    setCheck("far_from_ma", Boolean(ind.far_from_ma));

    const hasVolumeSignal = VOLUME_SIGNAL_NAMES.some(name => Boolean(ind[name]));
    setChoice("volume_state", hasVolumeSignal ? "__clear__" : "none");

    markChoiceAuto("market_state", marketState);
    markChoiceAuto("entry_state", entryState);
    markChoiceAuto("exit_state", exitState);
    markChoiceAuto("profit_state", profitState);
    if (!hasVolumeSignal) markChoiceAuto("volume_state", "none");
    markCheckAuto("volume_confirm", Boolean(ind.volume_confirm));
    markCheckAuto("pullback_volume_dry", Boolean(ind.pullback_volume_dry));
    markCheckAuto("upper_shadow", Boolean(ind.upper_shadow));
    markCheckAuto("failed_close", Boolean(ind.failed_close));
    markCheckAuto("far_from_ma", Boolean(ind.far_from_ma));

    applyingAutoFill = false;
    renderAutoData(ind);
    saveSignalState();
    calculateDebounced();
  }

  async function fetchSelectedData() {
    const data = toConfigPayload();
    const status = $("#fetch-status");
    const btn = $("#fetch-data-btn");

    if (fetchInFlight) return;
    if (!data.symbol) {
      showToast("请先搜索并选择标的", true);
      return;
    }

    fetchInFlight = true;
    const restore = setBusy(btn, "拉取中…", "拉取所选标的数据");

    resetAutoFetchedFields();
    if (status) status.textContent = "已清空旧自动数据，正在拉取行情/指标…";

    try {
      const res = await postJSON("/api/fetch", data);
      applyFetchedIndicators(res.indicators);
      if (status) status.textContent = res.message || "数据已更新";
      showToast(res.cache?.hit ? "数据来自缓存，已自动填入" : "数据已自动填入，可手动覆盖");
    } catch (err) {
      if (status) status.textContent = err.message || "自动获取失败，旧 PE/ROE/PB 已清空，可手动填写";
      showToast(err.message || "自动获取失败，旧自动数据已清空", true);
    } finally {
      fetchInFlight = false;
      restore();
    }
  }

  function resetSignalDefaults() {
    if (!signalForm) return;
    signalForm.reset();

    const defaults = {
      market_state: "sideways",
      entry_state: "none",
      exit_state: "none",
      profit_state: "none",
      volume_state: "none"
    };
    clearAutoFilledMarks();

    for (const [name, value] of Object.entries(defaults)) {
      setChoice(name, value);
    }

    const stop = signalForm.querySelector('input[name="stop_loss_pct"]');
    if (stop) stop.value = "6";
    renderAutoData({});
  }

  function initResetButton() {
    const btn = $("[data-reset-state]");
    if (!btn || !signalForm) return;
    btn.addEventListener("click", () => {
      resetSignalDefaults();
      localStorage.removeItem(SIGNAL_KEY);
      saveSignalState();
      calculateDebounced();
    });
  }

  function initAssetTools() {
    const input = $("#asset-search-input");
    const searchBtn = $("#asset-search-btn");
    const fetchBtn = $("#fetch-data-btn");
    const selected = $("#selected-asset");
    if (searchBtn) searchBtn.addEventListener("click", searchAsset);
    if (input) {
      input.addEventListener("input", () => {
        const q = input.value.trim();
        const host = $("#asset-candidates");

        if (!q) {
          // 只有输入框变为空时才收起；避免用户正在输入时下拉栏闪退。
          clearSearchResults(false);
          return;
        }

        if (q !== lastCandidateQuery) {
          // 输入内容变化时清掉旧候选，但保持下拉栏展开，提示用户继续搜索。
          if (host) {
            host.innerHTML = `<div class="candidate-empty">输入完成后点击搜索，或按 Enter 搜索。</div>`;
          }
          setSearchOpen(true);
          return;
        }

        // 当前输入与已搜索关键词一致时，保持已有结果展开。
        if (host?.children.length) setSearchOpen(true);
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
          searchAsset();
        }
      });
    }
    if (fetchBtn) fetchBtn.addEventListener("click", fetchSelectedData);
    if (selected) {
      selected.addEventListener("click", toggleSearchBox);
      selected.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleSearchBox();
        }
      });
    }
  }



  function setIndexMapStatus(text, isError = false) {
    const el = $("#index-map-status");
    if (!el) return;
    el.textContent = text;
    el.style.color = isError ? "var(--red)" : "var(--muted)";
  }

  async function loadIndexMap() {
    const editor = $("#index-map-editor");
    const btn = $("#index-map-load-btn");
    const restore = setBusy(btn, "载入中…");
    try {
      const res = await fetch("/api/index-map");
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.message || "载入失败");
      if (editor) editor.value = JSON.stringify(data.mapping || {}, null, 2);
      const c = data.counts || {};
      setIndexMapStatus(`已载入：基金映射 ${c.fund_index_map || 0} / 指数 ${c.index_codes || 0} / 关键词 ${c.keyword_rules || 0}`);
    } catch (err) {
      setIndexMapStatus(err.message || "载入失败", true);
      showToast(err.message || "映射表载入失败", true);
    } finally {
      restore();
    }
  }

  async function saveIndexMap() {
    const editor = $("#index-map-editor");
    const btn = $("#index-map-save-btn");
    if (!editor) return;
    let mapping;
    try {
      mapping = JSON.parse(editor.value || "{}");
    } catch (err) {
      setIndexMapStatus("JSON 格式错误，无法保存", true);
      showToast("映射表 JSON 格式错误", true);
      return;
    }
    const restore = setBusy(btn, "保存中…");
    try {
      const data = await postJSON("/api/index-map", {mapping});
      editor.value = JSON.stringify(data.mapping || mapping, null, 2);
      const c = data.counts || {};
      setIndexMapStatus(`已保存并热更新：基金映射 ${c.fund_index_map || 0} / 指数 ${c.index_codes || 0} / 关键词 ${c.keyword_rules || 0}`);
      showToast(data.message || "映射表已保存");
    } catch (err) {
      setIndexMapStatus(err.message || "保存失败", true);
      showToast(err.message || "映射表保存失败", true);
    } finally {
      restore();
    }
  }

  function initSettingsModal() {
    $$('[data-open-settings]').forEach(btn => btn.addEventListener('click', openSettingsModal));
    $$('[data-close-settings]').forEach(btn => btn.addEventListener('click', closeSettingsModal));
    const saveBtn = $('#settings-save-btn');
    const testBtn = $('#connection-test-btn');
    const cacheClearBtn = $('#cache-clear-btn');
    const mapLoadBtn = $('#index-map-load-btn');
    const mapSaveBtn = $('#index-map-save-btn');
    if (saveBtn) saveBtn.addEventListener('click', async () => {
      await saveConfigNow({quiet: false, recalc: true});
      closeSettingsModal();
    });
    if (testBtn) testBtn.addEventListener('click', runConnectionTest);
    if (cacheClearBtn) cacheClearBtn.addEventListener('click', clearRuntimeCache);
    if (mapLoadBtn) mapLoadBtn.addEventListener('click', loadIndexMap);
    if (mapSaveBtn) mapSaveBtn.addEventListener('click', saveIndexMap);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeSettingsModal();
    });
  }

  function initForms() {
    if (signalForm) {
      loadSignalState();

      signalForm.addEventListener("change", (event) => {
        if (!applyingAutoFill) clearAutoMarkForInput(event.target);
        enforceToggleGroups(event.target);
        if (event.target?.name === "stop_loss_pct") autoApplyInitialStopFromInputs();
        saveSignalState();
        calculateDebounced();
      });

      signalForm.addEventListener("input", (event) => {
        if (!applyingAutoFill) clearAutoMarkForInput(event.target);
        if (event.target?.name === "stop_loss_pct") autoApplyInitialStopFromInputs();
        saveSignalState();
        calculateDebounced();
      });

      signalForm.addEventListener("submit", (event) => {
        event.preventDefault();
        calculateNow({quiet: false});
      });
    }

    if (configForm) {
      configForm.addEventListener("change", () => {
        updateStrategySummary();
        autoApplyInitialStopFromInputs();
        saveConfigDebounced();
      });

      configForm.addEventListener("input", () => {
        updateStrategySummary();
        autoApplyInitialStopFromInputs();
        saveConfigDebounced();
      });

      configForm.addEventListener("submit", async (event) => {
        // 配置项已通过 input/change 自动保存；这里仅拦截回车提交，避免页面刷新。
        event.preventDefault();
        await saveConfigNow({quiet: false, recalc: true});
        restoreScrollState();
      });
    }
  }

  function persistScrollLive() {
    const save = () => {
      window.clearTimeout(persistScrollLive.timer);
      persistScrollLive.timer = window.setTimeout(saveScrollState, 120);
    };
    if (signalPane) signalPane.addEventListener("scroll", save, {passive: true});
    if (resultPane) resultPane.addEventListener("scroll", save, {passive: true});
    window.addEventListener("beforeunload", saveScrollState);
  }



  function setPanelVisible(el, visible) {
    if (!el) return;
    el.hidden = !visible;
    el.setAttribute("aria-hidden", visible ? "false" : "true");
  }

  function setBacktestLeftMode(enabled) {
    $$('[data-hide-in-backtest]').forEach(el => {
      el.hidden = Boolean(enabled);
      el.setAttribute("aria-hidden", enabled ? "true" : "false");
    });
  }

  function openBacktestPage() {
    const shell = $(".app-shell");
    const normalResult = $(".result-pane");
    const backtestConfig = $("#backtest-config-pane");
    const backtestResult = $("#backtest-result-pane");

    if (shell) shell.dataset.view = "backtest";
    setBacktestLeftMode(true);
    setPanelVisible(signalPane, false);
    setPanelVisible(normalResult, false);
    setPanelVisible(backtestConfig, true);
    setPanelVisible(backtestResult, true);

    $$('[data-open-backtest]').forEach(btn => btn.hidden = true);
    $$('[data-back-to-main]').forEach(btn => btn.hidden = false);
    if (backtestConfig) backtestConfig.scrollTop = 0;
    if (backtestResult) backtestResult.scrollTop = 0;
  }

  function closeBacktestPage() {
    const shell = $(".app-shell");
    const normalResult = $(".result-pane");
    const backtestConfig = $("#backtest-config-pane");
    const backtestResult = $("#backtest-result-pane");

    if (shell) shell.dataset.view = "main";
    setBacktestLeftMode(false);
    setPanelVisible(backtestConfig, false);
    setPanelVisible(backtestResult, false);
    setPanelVisible(signalPane, true);
    setPanelVisible(normalResult, true);

    $$('[data-open-backtest]').forEach(btn => btn.hidden = false);
    $$('[data-back-to-main]').forEach(btn => btn.hidden = true);
    restoreScrollState();
  }

  function fillBacktestFromCurrent() {
    const bt = $("#backtest-form");
    if (!bt || !configForm) return;
    const cfg = toConfigPayload();
    const mapping = {
      pe_percentile: signalForm?.querySelector('[name="pe_percentile"]')?.value || "",
      pb_percentile: signalForm?.querySelector('[name="pb_percentile"]')?.value || "",
      roe_pct: signalForm?.querySelector('[name="roe_pct"]')?.value || ""
    };
    for (const [name, value] of Object.entries(mapping)) {
      const fields = Array.from(bt.querySelectorAll(`[name="${CSS.escape(name)}"]`));
      for (const field of fields) {
        if (field.type === "radio") field.checked = field.value === String(value);
        else field.value = value;
      }
    }
    saveBacktestState();
    showToast("已填入当前估值假设");
  }

  function formToPlainObject(form) {
    const data = {};
    for (const el of Array.from(form.elements).filter(x => x.name && !x.disabled)) {
      if (el.type === "checkbox") data[el.name] = el.checked;
      else if (el.type === "radio") {
        if (el.checked) data[el.name] = el.value;
      } else {
        data[el.name] = el.value;
      }
    }
    return data;
  }

  function formToStorageObject(form) {
    const data = {};
    for (const el of Array.from(form.elements).filter(x => x.name)) {
      if (el.type === "checkbox") data[el.name] = el.checked;
      else if (el.type === "radio") {
        if (el.checked) data[el.name] = el.value;
      } else {
        data[el.name] = el.value;
      }
    }
    return data;
  }

  function saveBacktestState() {
    const form = $("#backtest-form");
    if (!form) return;
    try {
      localStorage.setItem(BACKTEST_KEY, JSON.stringify(formToStorageObject(form)));
    } catch {}
  }

  function loadBacktestState() {
    const form = $("#backtest-form");
    if (!form) return;
    try {
      const raw = localStorage.getItem(BACKTEST_KEY);
      if (!raw) return;
      restoreForm(form, JSON.parse(raw));
    } catch {}
  }

  function backtestMetricCategory(label) {
    const name = String(label || "");
    if (!name) return "other";
    if (name.includes("得分")) return "score";
    if (name.includes("卡玛") || name.includes("夏普") || name.includes("无风险")) return "risk_adjusted";
    if (name.includes("收益") || name.includes("年化收益") || name.includes("期末权益")) return "return";
    if (name.includes("回撤") || name.includes("波动")) return "risk";
    if (name.includes("交易") || name.includes("胜率") || name.includes("盈亏因子") || name.includes("仓位") || name.includes("换手")) return "trade";
    if (name.includes("估值") || name.includes("PE") || name.includes("PB") || name.includes("ROE")) return "valuation";
    return "other";
  }

  function backtestMetricTone(label, value) {
    const name = String(label || "");
    const text = String(value || "");
    if (!name.includes("得分")) return "";
    const score = Number((text.match(/-?\d+(?:\.\d+)?/) || [""])[0]);
    if (!Number.isFinite(score)) return " neutral";
    if (score >= 75) return " positive";
    if (score <= 40) return " negative";
    return " neutral";
  }

  function backtestTradeTone(row) {
    const text = `${row?.["方向"] || ""} ${row?.["操作建议"] || ""} ${row?.["命中规则"] || ""}`;
    if (text.includes("清仓")) return " clear";
    if (text.includes("卖出") || text.includes("减仓") || text.includes("止盈")) return " sell";
    if (text.includes("买入") || text.includes("加仓") || text.includes("补足")) return " buy";
    if (text.includes("等待") || text.includes("观望") || text.includes("不操作")) return " hold";
    return " neutral";
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[ch]));
  }

  function normalizeRowsForColumns(rows, forcedColumns = null) {
    if (!rows || !rows.length) return {rows: [], columns: []};
    if (forcedColumns && forcedColumns.length) return {rows, columns: forcedColumns};

    const columns = [];
    const seen = new Set();
    for (const row of rows) {
      for (const key of Object.keys(row || {})) {
        if (!seen.has(key)) {
          seen.add(key);
          columns.push(key);
        }
      }
    }
    return {rows, columns};
  }

  function renderTable(host, rows, emptyText, forcedColumns = null) {
    if (!host) return;
    if (!rows || !rows.length) {
      host.innerHTML = `<div class="test-empty">${escapeHTML(emptyText || "暂无数据")}</div>`;
      return;
    }
    const {rows: normalizedRows, columns} = normalizeRowsForColumns(rows, forcedColumns);
    const isMetricTable = columns.includes("指标") && columns.includes("数值");
    const isTradeTable = !isMetricTable && (columns.includes("方向") || columns.includes("操作建议") || columns.includes("成交金额"));
    const thead = `<thead><tr>${columns.map(c => `<th>${escapeHTML(c)}</th>`).join("")}</tr></thead>`;
    const body = normalizedRows.map(row => {
      const label = row?.["指标"] ?? "";
      const value = row?.["数值"] ?? "";
      let rowAttrs = "";
      if (isMetricTable) {
        const category = backtestMetricCategory(label);
        const tone = backtestMetricTone(label, value);
        rowAttrs = ` class="metric-row${tone}" data-metric-category="${escapeHTML(category)}"`;
      } else if (isTradeTable) {
        const tone = backtestTradeTone(row);
        rowAttrs = ` class="trade-row${tone}"`;
      }
      return `<tr${rowAttrs}>${columns.map(c => `<td data-col="${escapeHTML(c)}">${escapeHTML(row?.[c] ?? "")}</td>`).join("")}</tr>`;
    }).join("");
    const tableClass = isMetricTable ? "backtest-table metric-table" : (isTradeTable ? "backtest-table trade-table" : "backtest-table");
    host.innerHTML = `<table class="${tableClass}">${thead}<tbody>${body}</tbody></table>`;
  }

  function openTablePreview(kind) {
    const modal = $("#table-preview-modal");
    const title = $("#table-preview-title");
    const body = $("#table-preview-body");
    if (!modal || !title || !body) return;

    const result = lastBacktestResult || {};
    if (kind === "metrics") {
      title.textContent = "核心指标预览";
      renderTable(body, result.metrics || [], "暂无核心指标", BACKTEST_METRIC_COLUMNS);
    } else {
      title.textContent = "交易记录预览";
      renderTable(body, result.trades || [], "暂无交易记录");
    }

    modal.hidden = false;
    document.body.classList.add("preview-modal-open");
  }

  function closeTablePreview() {
    const modal = $("#table-preview-modal");
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("preview-modal-open");
  }

  function renderBacktestResult(result) {
    lastBacktestResult = result || {};
    const status = $("#backtest-status");
    const summary = $("#backtest-summary");
    if (status) {
      status.textContent = "完成";
      status.className = "pill buy";
    }
    const s = result.summary || {};
    if (summary) {
      const dcaLine = s.定投基准 ? `定投基准：${escapeHTML(s.定投基准)}<br>` : "";
      summary.innerHTML = `
        <b>${escapeHTML(s.标的 || "--")}</b> · ${escapeHTML(s.市场 || "--")} · ${escapeHTML(s.数据源 || "--")}<br>
        回测周期：${escapeHTML(s.回测周期 || "--")}；操作周期：${escapeHTML(s.操作周期 || "--")}；交易次数：${escapeHTML(s.交易次数 ?? "--")}<br>
        ${dcaLine}
        <span class="small">${escapeHTML(s.提示 || "")}</span>
      `;
    }
    renderTable($("#backtest-metrics"), result.metrics || [], "暂无核心指标", BACKTEST_METRIC_INLINE_COLUMNS);
    renderTable($("#backtest-trades"), result.trades || [], "暂无交易记录");

    const exportInfo = $("#backtest-export-info");
    if (exportInfo) {
      const exported = result.exported || {};
      const errors = result.fetch_errors || [];
      const files = Object.entries(exported).map(([k, v]) => `<li><b>${escapeHTML(k)}</b>：<code>${escapeHTML(v)}</code></li>`).join("");
      const errHtml = errors.length ? `<p class="small">备用链路失败记录：${escapeHTML(errors.join("；"))}</p>` : "";
      exportInfo.innerHTML = files ? `<ul>${files}</ul>${errHtml}` : `未导出文件。${errHtml}`;
    }
  }


  function updateBacktestValuationMode() {
    const mode = $("#backtest-valuation-mode")?.value || "none";
    $$(".backtest-fixed-valuation").forEach(el => {
      const enabled = mode === "fixed";
      el.classList.toggle("is-disabled", !enabled);
      el.querySelectorAll("input, select, textarea").forEach(input => {
        input.disabled = !enabled;
        if (!enabled) input.value = "";
      });
    });
  }

  async function runBacktest(event) {
    event?.preventDefault();
    const form = $("#backtest-form");
    if (!form) return;
    saveBacktestState();
    const btn = $("#backtest-run-btn");
    const status = $("#backtest-status");
    const restore = setBusy(btn, "回测中…");
    if (status) {
      status.textContent = "运行中";
      status.className = "pill wait";
    }
    lastBacktestResult = null;
    renderTable($("#backtest-metrics"), [], "正在计算核心指标…", BACKTEST_METRIC_INLINE_COLUMNS);
    renderTable($("#backtest-trades"), [], "正在生成交易记录…");
    try {
      await saveConfigNow({quiet: true, recalc: false});
      const currentCfg = toConfigPayload();
      if (!currentCfg.symbol) {
        throw new Error("请先在左侧搜索并选择一个当前标的");
      }
      const payload = {
        ...formToPlainObject(form),
        symbol: currentCfg.symbol,
        symbol_name: currentCfg.symbol_name,
        market: currentCfg.market || "auto",
        asset_kind: currentCfg.asset_kind || "auto",
        initial_cash: Number(currentCfg.plan_amount || 100000),
        strategy: currentCfg.strategy || "balanced",
        position_mode: currentCfg.position_mode || "core_satellite",
        risk_per_trade_pct: Number(currentCfg.risk_per_trade_pct || 1),
        risk_free_rate_pct: currentCfg.backtest_risk_free_rate_pct === undefined || currentCfg.backtest_risk_free_rate_pct === "" ? 2 : Number(currentCfg.backtest_risk_free_rate_pct),
        source: currentCfg.data_source || "auto",
        data_source: currentCfg.data_source || "auto",
        valuation_method: currentCfg.valuation_method || "auto",
        proxy_mode: currentCfg.proxy_mode || "system",
        proxy_url: currentCfg.proxy_url || "",
        request_timeout_sec: currentCfg.request_timeout_sec || 12,
        retry_count: currentCfg.retry_count || 2,
        danjuan_cookie: currentCfg.danjuan_cookie || ""
      };
      if (payload.valuation_mode === "fixed") {
        if (!payload.pe_percentile) payload.pe_percentile = signalForm?.querySelector('[name="pe_percentile"]')?.value || "";
        if (!payload.pb_percentile) payload.pb_percentile = signalForm?.querySelector('[name="pb_percentile"]')?.value || "";
        if (!payload.roe_pct) payload.roe_pct = signalForm?.querySelector('[name="roe_pct"]')?.value || "";
      } else {
        payload.pe_percentile = "";
        payload.pb_percentile = "";
        payload.roe_pct = "";
      }
      const data = await postJSON("/api/backtest", payload);
      renderBacktestResult(data.result || {});
      showToast(data.cache?.hit ? "历史回测结果来自缓存" : "历史回测完成");
    } catch (err) {
      if (status) {
        status.textContent = "失败";
        status.className = "pill danger";
      }
      const summary = $("#backtest-summary");
      if (summary) summary.textContent = err.message || "回测失败";
      showToast(err.message || "历史回测失败", true);
    } finally {
      restore();
    }
  }

  function initBacktestPage() {
    $$('[data-open-backtest]').forEach(btn => btn.addEventListener('click', openBacktestPage));
    $$('[data-back-to-main]').forEach(btn => btn.addEventListener('click', closeBacktestPage));
    $$('[data-preview-table]').forEach(btn => {
      btn.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        openTablePreview(btn.dataset.previewTable);
      });
    });
    $$('[data-close-preview]').forEach(btn => btn.addEventListener('click', closeTablePreview));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeTablePreview();
    });
    const form = $("#backtest-form");
    loadBacktestState();
    const valuationMode = $("#backtest-valuation-mode");
    if (valuationMode) {
      valuationMode.addEventListener("change", () => {
        updateBacktestValuationMode();
        saveBacktestState();
      });
      updateBacktestValuationMode();
    }
    if (form) {
      form.addEventListener("change", saveBacktestState);
      form.addEventListener("input", debounce(saveBacktestState, 180));
      form.addEventListener("submit", runBacktest);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initForms();
    initHoverDetails();
    initResetButton();
    initAssetTools();
    initSettingsModal();
    initBacktestPage();
    updateStrategySummary();
    persistScrollLive();
    restoreScrollState();
    calculateDebounced();
  });
})();
