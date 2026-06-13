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

  const strategyFamilyInfo = window.STRATEGY_FAMILIES || {
    trend_signal_control: {name: "趋势信号风控策略", desc: "以趋势状态为主轴生成目标仓位。"}
  };

  const strategyInfo = window.STRATEGY_INFO || {
    defensive: {name: "防守", desc: "买入更慢，卖出更快；适合不想承受大回撤。"},
    balanced: {name: "均衡", desc: "趋势、止损、仓位三者平衡。"},
    aggressive: {name: "进攻", desc: "盈利后加仓更快，但破位清仓不打折。"}
  };
  const strategyMarketStates = window.STRATEGY_MARKET_STATES || {
    bear: "熊市/大空头",
    below_200: "200日线下",
    sideways: "震荡",
    above_200: "200日线上",
    strong_bull: "强趋势"
  };
  const strategyKeys = Object.keys(strategyInfo);
  const strategyFamilyKeys = Object.keys(strategyFamilyInfo);
  const initialConfig = window.INITIAL_CONFIG || {};
  const familyParamState = normaliseAllFamilyParams(initialConfig.strategy_family_params || {}, initialConfig);
  const familyEditStyleState = {};
  let globalStyleState = strategyKeys.includes(initialConfig.strategy) ? initialConfig.strategy : defaultStyleKey();

  // 全局偏离度（全策略家族共享）
  const _rawDev = initialConfig.deviation && typeof initialConfig.deviation === "object" ? initialConfig.deviation : {};
  const globalDeviation = {};
  for (const sk of strategyKeys) {
    const d = _rawDev[sk] && typeof _rawDev[sk] === "object" ? _rawDev[sk] : {};
    globalDeviation[sk] = {amplitude: numberOr(d.amplitude, 0), threshold: numberOr(d.threshold, 0)};
  }

  const globalConfigState = {
    global_risk_multiplier: numberOr(initialConfig.global_risk_multiplier, 1.0), // 兼容旧配置；前端不再展示
    dca_base_buy_pct: numberOr(initialConfig.dca_base_buy_pct, 25),
    core_step_pct: numberOr(initialConfig.core_step_pct, 22), // 兼容旧配置；前端不再展示
    buy_step_limit_pct: numberOr(initialConfig.buy_step_limit_pct, 28),
    sell_step_limit_pct: numberOr(initialConfig.sell_step_limit_pct, 45),
    core_min_position_pct: numberOr(initialConfig.core_min_position_pct, 5),
    core_max_position_pct: numberOr(initialConfig.core_max_position_pct, 92),
    strict_min_position_pct: numberOr(initialConfig.strict_min_position_pct, 0),
    strict_max_position_pct: numberOr(initialConfig.strict_max_position_pct, 60),
  };

  function defaultStyleKey() {
    return strategyKeys.includes("balanced") ? "balanced" : (strategyKeys[0] || "balanced");
  }

  function activeFamilyKeyFromForm() {
    const direct = document.querySelector('[name="strategy_family"][form="config-form"]') || configForm?.querySelector('[name="strategy_family"]');
    let key = "";
    if (direct) {
      if (direct.type === "radio") {
        key = (document.querySelector('input[name="strategy_family"][form="config-form"]:checked') || configForm?.querySelector('input[name="strategy_family"]:checked'))?.value || "";
      } else {
        key = direct.value || "";
      }
    }
    key = key || initialConfig.strategy_family || strategyFamilyKeys[0] || "trend_signal_control";
    return strategyFamilyInfo[key] ? key : (strategyFamilyKeys[0] || key);
  }

  function fallbackStyleParamSchema() {
    // 没有声明策略参数的旧策略不再自动补出旧版节奏字段。
    // 执行层只保留全局“买入上限/卖出上限”。
    return [];
  }

  function styleParamSchema(familyKey) {
    const family = strategyFamilyInfo[familyKey] || {};
    return Array.isArray(family.style_param_schema) && family.style_param_schema.length
      ? family.style_param_schema
      : fallbackStyleParamSchema();
  }

  function iterStyleParamFields(familyKey) {
    const fields = [];
    for (const group of styleParamSchema(familyKey)) {
      if (!group || typeof group !== "object") continue;
      if (group.type === "core_base_table") {
        fields.push(Object.assign({name: "core_base_pct", type: "core_base_table"}, group));
        continue;
      }
      if (!Array.isArray(group.fields)) continue;
      for (const field of group.fields) {
        if (field && typeof field === "object" && field.name) fields.push(field);
      }
    }
    return fields;
  }

  function familyStylePreset(familyKey, styleKey) {
    const family = strategyFamilyInfo[familyKey] || {};
    const familyPresets = family.style_param_presets && typeof family.style_param_presets === "object" ? family.style_param_presets : {};
    const familyPreset = familyPresets[styleKey] && typeof familyPresets[styleKey] === "object" ? familyPresets[styleKey] : {};
    const globalPreset = strategyInfo[styleKey] || {};
    return Object.assign({}, globalPreset, familyPreset, {
      name: globalPreset.name || familyPreset.name || styleKey,
      desc: globalPreset.desc || familyPreset.desc || "",
      research_note: globalPreset.research_note || familyPreset.research_note || globalPreset.desc || ""
    });
  }

  function defaultCoreBasePct(styleKey, familyKey = activeFamilyKeyFromForm()) {
    const info = familyStylePreset(familyKey, styleKey);
    const coreBase = info.core_base || {};
    const out = {};
    for (const state of Object.keys(strategyMarketStates)) {
      out[state] = numberOr(coreBase[state], 0.5) * 100;
    }
    return out;
  }

  function fieldDefaultValue(field, preset, fallback = 0) {
    const name = field?.name || "";
    if (Object.prototype.hasOwnProperty.call(preset, name)) return preset[name];
    if (name === "buy_step_pct") return numberOr(preset.buy_step_limit_pct, numberOr(preset.buy_step, 0.28) * 100);
    if (name === "sell_step_pct") return numberOr(preset.sell_step_limit_pct, numberOr(preset.sell_step, 0.45) * 100);
    if (name === "risk_multiplier") return numberOr(preset.risk_multiplier, 1);
    return field?.default ?? fallback;
  }

  function clampNumber(value, min, max) {
    const n = numberOr(value, numberOr(min, 0));
    const low = numberOr(min, 0);
    const high = numberOr(max, 100);
    return Math.max(Math.min(n, Math.max(low, high)), Math.min(low, high));
  }

  function normaliseParamValue(value, field, fallback) {
    const type = field?.type || "number";
    if (type === "checkbox") return Boolean(value);
    if (type === "select" || type === "choice") {
      const raw = value === undefined || value === null || value === "" ? String(fallback ?? "") : String(value);
      const allowed = Array.isArray(field.options) ? field.options.map(item => String(Array.isArray(item) ? item[0] : item)) : [];
      return !allowed.length || allowed.includes(raw) ? raw : String(fallback ?? "");
    }
    return clampNumber(value === undefined || value === null || value === "" ? fallback : value, field?.min ?? 0, field?.max ?? 100);
  }

  function defaultStyleEntry(styleKey, selectedStyle, familyKey = activeFamilyKeyFromForm()) {
    const info = familyStylePreset(familyKey, styleKey);
    const selected = styleKey === selectedStyle;
    const entry = {
      enabled: selected,
      weight_pct: selected ? 100 : 0,
      core_base_pct: defaultCoreBasePct(styleKey, familyKey)
    };
    for (const field of iterStyleParamFields(familyKey)) {
      if (!field.name || field.name === "core_base_pct") continue;
      const fallback = fieldDefaultValue(field, info, 0);
      entry[field.name] = normaliseParamValue(fallback, field, fallback);
    }
    return entry;
  }

  function normaliseFamilyParams(rawFamilyParams, cfg = {}) {
    const familyKey = cfg.strategy_family || activeFamilyKeyFromForm();
    const raw = rawFamilyParams && typeof rawFamilyParams === "object" ? rawFamilyParams : {};
    // 参数风格是全局选择，不再跟随总体策略分别持久化。
    // 旧配置里 raw.strategy 会被忽略，只保留该总体策略下各风格的微调参数。
    const selectedStyle = strategyKeys.includes(cfg.strategy) ? cfg.strategy : defaultStyleKey();
    const rawMix = raw.strategy_mix && typeof raw.strategy_mix === "object"
      ? raw.strategy_mix
      : (cfg.strategy_mix && typeof cfg.strategy_mix === "object" ? cfg.strategy_mix : {});
    const mix = {};
    for (const styleKey of strategyKeys) {
      const defaults = defaultStyleEntry(styleKey, selectedStyle, familyKey);
      const preset = familyStylePreset(familyKey, styleKey);
      const entry = rawMix[styleKey] && typeof rawMix[styleKey] === "object" ? rawMix[styleKey] : {};
      const rawCore = entry.core_base_pct && typeof entry.core_base_pct === "object" ? entry.core_base_pct : {};
      const coreBasePct = {};
      for (const state of Object.keys(strategyMarketStates)) {
        coreBasePct[state] = numberOr(rawCore[state], defaults.core_base_pct[state]);
      }
      const selected = styleKey === selectedStyle;
      const item = {
        enabled: selected,
        weight_pct: selected ? 100 : 0,
        core_base_pct: coreBasePct
      };
      for (const field of iterStyleParamFields(familyKey)) {
        if (!field.name || field.name === "core_base_pct") continue;
        const fallback = defaults[field.name] ?? fieldDefaultValue(field, preset, 0);
        item[field.name] = normaliseParamValue(entry[field.name], field, fallback);
      }
      mix[styleKey] = item;
    }
    return {strategy_mix: mix};
  }


  function normaliseAllFamilyParams(rawAll, cfg = {}) {
    const out = {};
    const raw = rawAll && typeof rawAll === "object" ? rawAll : {};
    for (const familyKey of strategyFamilyKeys) {
      const familyCfg = Object.assign({}, cfg, {strategy_family: familyKey});
      out[familyKey] = normaliseFamilyParams(raw[familyKey], familyCfg);
    }
    return out;
  }

  function getFamilyParams(familyKey) {
    if (!familyParamState[familyKey]) {
      familyParamState[familyKey] = normaliseFamilyParams({}, Object.assign({}, initialConfig, {strategy_family: familyKey}));
    }
    return familyParamState[familyKey];
  }

  function setActiveStyleInput(styleKey) {
    const normalized = strategyKeys.includes(styleKey) ? styleKey : defaultStyleKey();
    globalStyleState = normalized;
    const fields = configForm ? $$('[name="strategy"]', configForm) : [];
    for (const field of fields) {
      if (field.type === "radio") field.checked = field.value === normalized;
      else field.value = normalized;
    }
  }

  function activeStyleKeyFromForm() {
    const checked = configForm?.querySelector?.('input[name="strategy"]:checked');
    const key = checked?.value || globalStyleState || initialConfig.strategy || defaultStyleKey();
    return strategyKeys.includes(key) ? key : defaultStyleKey();
  }

  function markStyleSelected(styleKey) {
    const normalized = strategyKeys.includes(styleKey) ? styleKey : defaultStyleKey();
    setActiveStyleInput(normalized);
    for (const familyKey of strategyFamilyKeys) {
      const params = getFamilyParams(familyKey);
      for (const key of strategyKeys) {
        if (!params.strategy_mix[key]) params.strategy_mix[key] = defaultStyleEntry(key, normalized, familyKey);
        params.strategy_mix[key].enabled = key === normalized;
        params.strategy_mix[key].weight_pct = key === normalized ? 100 : 0;
      }
    }
    return normalized;
  }

  function balancedOnlyMix(familyKey) {
    const params = getFamilyParams(familyKey);
    if (!params.strategy_mix.balanced) params.strategy_mix.balanced = defaultStyleEntry("balanced", "balanced", familyKey);
    const balanced = Object.assign({}, params.strategy_mix.balanced);
    // 均衡是唯一可保存基准；当前执行风格由 cfg.strategy 保存，不靠 mix.enabled 表达。
    balanced.enabled = true;
    balanced.weight_pct = 100;
    return {balanced};
  }

  function serialiseFamilyParams() {
    const out = {};
    for (const familyKey of strategyFamilyKeys) {
      out[familyKey] = {strategy_mix: balancedOnlyMix(familyKey)};
    }
    return out;
  }

  function syncActiveFamilyToForm() {
    const familyKey = activeFamilyKeyFromForm();
    markStyleSelected(activeStyleKeyFromForm());
    return {familyKey, params: getFamilyParams(familyKey)};
  }

  function strategyFamilyText(key) {
    const item = strategyFamilyInfo[key] || strategyFamilyInfo[strategyFamilyKeys[0]] || {name: "总体策略", desc: ""};
    const axes = Array.isArray(item.axes) && item.axes.length ? `；维度：${item.axes.join(" / ")}` : "";
    const status = item.status ? `（${item.status}）` : "";
    return `总体策略：${item.name || key}${status}<br>${item.desc || ""}${axes}`;
  }

  function strategyText(key) {
    const item = strategyInfo[key] || strategyInfo.balanced || {name: "策略", desc: ""};
    if (typeof item === "string") return item;
    return `${item.name || key}：${item.desc || item.research_note || ""}`;
  }

  function numberOr(value, fallback = 0) {
    if (value === undefined || value === null || value === "") return fallback;
    const num = Number(value);
    return Number.isFinite(num) ? num : fallback;
  }

  // 信号强度字段（不再包含旧版节奏字段；买卖只由“买入上限/卖出上限”控制）
  const AMPLITUDE_FIELDS = [];
  // 操作阈值字段（仓位边界 + 执行层控制 + 仓位上限）
  const THRESHOLD_FIELDS = [
    "core_min_position_pct", "core_max_position_pct",
    "strict_min_position_pct", "strict_max_position_pct",
    "dca_base_buy_pct", "buy_step_limit_pct", "sell_step_limit_pct",
    "bear_cap_pct", "below200_cap_pct", "risk_event_cap_pct",
    "high_valuation_cap_sideways_pct", "high_valuation_cap_trend_pct",
    "extreme_valuation_cap_sideways_pct", "extreme_valuation_cap_trend_pct",
  ];

  // 偏离度 0-100%：进攻向100%推，防守向0%推，天然保证0-100%范围
  // 进攻 X%: val + (100 - val) * X / 100
  // 防守 X%: val * (1 - X / 100)
  function applyDeviation(val, pct, isAggressive) {
    if (isAggressive) return val + (100 - val) * pct / 100;
    return val * (1 - pct / 100);
  }

  function getDeviations(styleKey) {
    const d = globalDeviation[styleKey] || {amplitude: 0, threshold: 0};
    return {amplitude: numberOr(d.amplitude, 0), threshold: numberOr(d.threshold, 0)};
  }

  function setGlobalDeviation(styleKey, amplitude, threshold) {
    if (!globalDeviation[styleKey]) globalDeviation[styleKey] = {amplitude: 0, threshold: 0};
    globalDeviation[styleKey].amplitude = amplitude;
    globalDeviation[styleKey].threshold = threshold;
  }

  function deviationGroupForField(fieldName) {
    const name = String(fieldName || "").toLowerCase();
    if (!name || name === "risk_multiplier" || name === "global_risk_multiplier") return null;
    if (AMPLITUDE_FIELDS.includes(fieldName)) return "amplitude";
    if (THRESHOLD_FIELDS.includes(fieldName)) return "threshold";
    if (!name.endsWith("_pct")) return null;
    if (
      name.includes("cap") || name.includes("limit") || name.includes("threshold") ||
      name.includes("above") || name.includes("below") || name.includes("stop") ||
      name.includes("floor") || name.includes("min_") || name.includes("max_") ||
      name.includes("dca_base_buy") || name.includes("core_step") || name.includes("core_base")
    ) return "threshold";
    return "amplitude";
  }

  function paramFieldLabel(familyKey, fieldName) {
    const field = iterStyleParamFields(familyKey).find(item => item.name === fieldName);
    return field?.label || ALL_FIELD_LABELS[fieldName] || fieldName;
  }

  // 偏离展示/执行的基准：始终只以【均衡基准】为源；配置保存时只保存 balanced + deviation。
  const GLOBAL_STYLE_DEFAULTS = {global_risk_multiplier: 1.0};
  const GLOBAL_EXECUTION_DEFAULTS = {dca_base_buy_pct: 25, buy_step_limit_pct: 28, sell_step_limit_pct: 45};
  const GLOBAL_POSITION_DEFAULTS = {core_min_position_pct: 5, core_max_position_pct: 92, strict_min_position_pct: 0, strict_max_position_pct: 60};

  function readGlobalField(name, fallback) {
    const el = document.querySelector(`#global-config-body [data-global-field="${name}"]`);
    if (el) return numberOr(el.value, fallback);
    return numberOr(globalConfigState[name], fallback);
  }

  function getGlobalStyleBase() {
    const risk = readGlobalField("global_risk_multiplier", GLOBAL_STYLE_DEFAULTS.global_risk_multiplier);
    return {
      global_risk_multiplier: risk,
      risk_multiplier: risk,
    };
  }

  function getGlobalExecutionBase() {
    const out = {};
    for (const [key, fallback] of Object.entries(GLOBAL_EXECUTION_DEFAULTS)) out[key] = readGlobalField(key, fallback);
    return out;
  }

  function getGlobalPositionBase() {
    const out = {};
    for (const [key, fallback] of Object.entries(GLOBAL_POSITION_DEFAULTS)) out[key] = readGlobalField(key, fallback);
    return out;
  }

  function roundedPct(value) {
    return Math.round(numberOr(value, 0) * 10) / 10;
  }

  function formatPreviewNumber(value) {
    const n = roundedPct(value);
    return Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, "");
  }

  function getBalancedCoreBasePct(familyKey) {
    const params = getFamilyParams(familyKey);
    const balanced = (params.strategy_mix || {}).balanced || defaultStyleEntry("balanced", "balanced", familyKey);
    const defaults = defaultCoreBasePct("balanced", familyKey);
    const out = {};
    const raw = balanced.core_base_pct && typeof balanced.core_base_pct === "object" ? balanced.core_base_pct : {};
    for (const state of Object.keys(strategyMarketStates)) out[state] = numberOr(raw[state], defaults[state]);
    return out;
  }

  function balancedFieldFallback(familyKey, fieldName) {
    const preset = familyStylePreset(familyKey, "balanced");
    const fieldSpec = iterStyleParamFields(familyKey).find(item => item.name === fieldName) || {name: fieldName, type: "number", min: 0, max: 100};
    return fieldDefaultValue(fieldSpec, preset, 0);
  }

  function getBalancedBaseValues(familyKey) {
    const params = getFamilyParams(familyKey);
    const balanced = (params.strategy_mix || {}).balanced || defaultStyleEntry("balanced", "balanced", familyKey);
    const out = Object.assign({}, getGlobalStyleBase(), getGlobalExecutionBase(), getGlobalPositionBase());

    // 策略 Python schema 声明的参数，统一从【均衡】读取。
    for (const field of iterStyleParamFields(familyKey)) {
      if (!field.name || field.name === "core_base_pct") continue;
      const fallback = balancedFieldFallback(familyKey, field.name);
      out[field.name] = normaliseParamValue(balanced[field.name], field, fallback);
    }
    return out;
  }

  function getDeviationBaseValue(familyKey, fieldName) {
    return getBalancedBaseValues(familyKey)[fieldName];
  }

  function getPreviewFields(familyKey, requestedFields) {
    const base = getBalancedBaseValues(familyKey);
    return (requestedFields || Object.keys(base)).filter(name => base[name] !== undefined && deviationGroupForField(name));
  }

  function computeDeviationValue(baseValue, devPct, styleKey) {
    const isAgg = styleKey === "aggressive";
    return roundedPct(applyDeviation(numberOr(baseValue, 0), devPct, isAgg));
  }

  function renderDeviationPreview(familyKey, styleKey, requestedFields) {
    const box = document.createElement("div");
    box.className = "multiplier-preview simple";
    box.dataset.previewFamily = familyKey;
    box.dataset.previewStyle = styleKey;

    const grid = document.createElement("div");
    grid.className = "multiplier-preview-grid";
    for (const fieldName of getPreviewFields(familyKey, requestedFields)) {
      const row = document.createElement("div");
      row.className = "preview-row";
      row.dataset.previewField = fieldName;
      appendTextEl(row, "span", "preview-name", paramFieldLabel(familyKey, fieldName));
      appendTextEl(row, "span", "preview-val", "");
      grid.appendChild(row);
    }
    box.appendChild(grid);
    refreshDeviationPreviewBox(box);
    return box;
  }

  function refreshDeviationPreviewBox(box) {
    if (!box) return;
    const familyKey = box.dataset.previewFamily || activeFamilyKeyFromForm();
    const styleKey = box.dataset.previewStyle || activeStyleKeyFromForm();
    const devs = getDeviations(styleKey);
    const base = getBalancedBaseValues(familyKey);
    for (const row of $$(".preview-row", box)) {
      const fieldName = row.dataset.previewField;
      if (!fieldName || base[fieldName] === undefined) continue;
      const dev = deviationGroupForField(fieldName) === "amplitude" ? devs.amplitude : devs.threshold;
      const bVal = roundedPct(base[fieldName]);
      const computed = computeDeviationValue(bVal, dev, styleKey);

      const val = row.querySelector(".preview-val");
      if (val) val.textContent = `${formatPreviewNumber(bVal)}→${formatPreviewNumber(computed)}`;
    }
  }

  function refreshDeviationPreviews(root = document) {
    for (const box of $$(".multiplier-preview", root)) refreshDeviationPreviewBox(box);
  }

  function applyDeviations(familyKey, styleKey, ampDev, thrDev) {
    // 只保存偏离度；有效值由展示层/后端运行时按均衡基准临时计算。
    setGlobalDeviation(styleKey, ampDev, thrDev);
    refreshDeviationPreviews(document);
  }

  function collectStrategyMix(data) {
    const familyKey = data?.strategy_family || activeFamilyKeyFromForm();
    return balancedOnlyMix(familyKey);
  }

  function strategySummaryText(data) {
    const styleKey = strategyKeys.includes(data?.strategy) ? data.strategy : activeStyleKeyFromForm();
    return `参数风格：${strategyText(styleKey)}<br>风格使用方式：只保存【均衡】基准；防守/进攻由偏离值实时计算，当前执行风格由左侧选择。`;
  }

  function renderSchemaField(field, values) {
    const wrap = document.createElement("label");
    wrap.className = "field strategy-schema-field";
    if (field.tip) wrap.dataset.tip = field.tip;

    const title = document.createElement("span");
    title.textContent = field.label || field.name;
    wrap.appendChild(title);

    if ((field.type || "select") === "select") {
      const select = document.createElement("select");
      select.name = field.name;
      const options = Array.isArray(field.options) ? field.options : [];
      for (const item of options) {
        const opt = document.createElement("option");
        opt.value = String(item[0]);
        opt.textContent = String(item[1]);
        select.appendChild(opt);
      }
      const oldValue = values[field.name];
      select.value = oldValue !== undefined ? String(oldValue) : String(field.default ?? "auto");
      wrap.appendChild(select);
    } else {
      const input = document.createElement("input");
      input.name = field.name;
      input.type = field.type || "text";
      if (field.step !== undefined) input.step = field.step;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      const oldValue = values[field.name];
      input.value = oldValue !== undefined ? String(oldValue) : String(field.default ?? "");
      wrap.appendChild(input);
    }
    return wrap;
  }

  function renderStrategySpecificInputs(familyKey) {
    const host = $("#strategy-specific-inputs");
    if (!host || !signalForm) return false;

    const schema = strategyFamilyInfo[familyKey]?.input_schema || [];
    if (!Array.isArray(schema) || !schema.length) {
      host.hidden = true;
      host.innerHTML = "";
      return false;
    }

    const values = formToObject(signalForm);
    host.hidden = false;
    host.innerHTML = "";

    for (const section of schema) {
      const card = document.createElement("section");
      card.className = `card signal-card ${section.tone || "neutral"}`;

      const head = document.createElement("div");
      head.className = "card-title";
      const h2 = document.createElement("h2");
      h2.textContent = section.title || "策略专属输入";
      const pill = document.createElement("span");
      pill.className = "pill neutral";
      pill.textContent = section.pill || "专属输入";
      head.appendChild(h2);
      head.appendChild(pill);
      card.appendChild(head);

      if (section.desc) {
        const desc = document.createElement("p");
        desc.className = "strategy-schema-desc";
        desc.textContent = section.desc;
        card.appendChild(desc);
      }

      const grid = document.createElement("div");
      grid.className = "strategy-schema-grid";
      for (const field of section.fields || []) {
        if (!field?.name) continue;
        grid.appendChild(renderSchemaField(field, values));
      }
      card.appendChild(grid);
      host.appendChild(card);
    }
    return true;
  }

  function applyStrategySignalProfile() {
    const familyKey = activeFamilyKeyFromForm();
    renderStrategySpecificInputs(familyKey);
    const signalPaneEl = document.getElementById("signal-pane");
    if (signalPaneEl) signalPaneEl.dataset.strategyFamily = familyKey;
  }




  const marketNames = {
    US: "美股/海外",
    CN: "A股/国内基金",
    auto: "自动"
  };

  const ADVANCED_CONFIG_KEYS = [
    "trade_step_limit_enabled",
    "dca_base_buy_pct", "buy_step_limit_pct", "sell_step_limit_pct",
    "core_min_position_pct", "core_max_position_pct",
    "strict_min_position_pct", "strict_max_position_pct",
  ];

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
    // 自动数据弹窗会为“没有可视控件”的字段创建隐藏 input。
    // 如果同名可视策略控件已经存在，隐藏 input 不能覆盖用户点击结果。
    const visibleNames = new Set(
      elements
        .filter(el => el.dataset?.autoHidden !== "1")
        .map(el => el.name)
    );

    for (const el of elements) {
      if (el.dataset?.autoHidden === "1" && visibleNames.has(el.name)) continue;
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
    const activeFamily = data.strategy_family || activeFamilyKeyFromForm();
    const selectedStyle = strategyKeys.includes(data.strategy) ? data.strategy : activeStyleKeyFromForm();
    data.strategy = markStyleSelected(selectedStyle);
    const riskFreeRaw = data.backtest_risk_free_rate_pct;
    const payload = {
      plan_amount: Number(data.plan_amount || 0),
      current_position_amount: Number(data.current_position_amount || 0),
      current_profit_pct: Number(data.current_profit_pct || 0),
      backtest_risk_free_rate_pct: riskFreeRaw === undefined || riskFreeRaw === "" ? 2 : Number(riskFreeRaw),
      strategy_family: activeFamily,
      strategy: data.strategy,
      strategy_mode: "single",
      strategy_mix: collectStrategyMix(data),
      strategy_family_params: serialiseFamilyParams(),
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
    // 只保存均衡基准；防守/进攻的有效值由 deviation 临时计算。
    const gStyle = getGlobalStyleBase();
    payload.global_risk_multiplier = gStyle.global_risk_multiplier;
    const gExec = getGlobalExecutionBase();
    payload.dca_base_buy_pct = gExec.dca_base_buy_pct;
    payload.buy_step_limit_pct = gExec.buy_step_limit_pct;
    payload.sell_step_limit_pct = gExec.sell_step_limit_pct;
    const gPos = getGlobalPositionBase();
    payload.core_min_position_pct = gPos.core_min_position_pct;
    payload.core_max_position_pct = gPos.core_max_position_pct;
    payload.strict_min_position_pct = gPos.strict_min_position_pct;
    payload.strict_max_position_pct = gPos.strict_max_position_pct;
    // 全局偏离度：后端从均衡基准 + 偏离度临时计算有效值。
    payload.deviation = {};
    for (const sk of strategyKeys) {
      const d = globalDeviation[sk] || {amplitude: 0, threshold: 0};
      payload.deviation[sk] = {amplitude: numberOr(d.amplitude, 0), threshold: numberOr(d.threshold, 0)};
    }
    return payload;
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

  function appendTextEl(parent, tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = text || "";
    parent.appendChild(el);
    return el;
  }

  // 全局配置字段显示名
  const ALL_FIELD_LABELS = {
    core_min_position_pct: "增强最低仓位%", core_max_position_pct: "增强最高仓位%",
    strict_min_position_pct: "交易最低仓位%", strict_max_position_pct: "交易最高仓位%",
    dca_base_buy_pct: "定投基准买入%", buy_step_limit_pct: "买入上限%", sell_step_limit_pct: "卖出上限%",
    bear_cap_pct: "熊市仓位上限%", below200_cap_pct: "200日线下上限%", risk_event_cap_pct: "风险事件上限%",
    high_valuation_cap_sideways_pct: "高估值震荡上限%", high_valuation_cap_trend_pct: "高估值趋势上限%",
    extreme_valuation_cap_sideways_pct: "极端估值震荡上限%", extreme_valuation_cap_trend_pct: "极端估值趋势上限%",
  };

  // 全局配置弹窗：仓位边界 + 执行层控制 + 偏离度
  function openGlobalConfigModal() {
    const modal = $("#global-config-modal");
    const title = $("#global-config-title");
    const subtitle = $("#global-config-subtitle");
    const body = $("#global-config-body");
    if (!modal || !body) return;
    body.innerHTML = "";
    if (title) title.textContent = "全局配置";
    if (subtitle) subtitle.textContent = "所有策略共享的仓位边界、执行层控制与偏离度。";

    const activeFamily = activeFamilyKeyFromForm();
    const activeStyle = activeStyleKeyFromForm();
    const params = getFamilyParams(activeFamily);

    // ① 均衡基准 — 仓位边界
    const boundBox = document.createElement("div");
    boundBox.className = "family-param-subgroup";
    const boundHead = document.createElement("div");
    boundHead.className = "family-param-group-title";
    appendTextEl(boundHead, "strong", "", "均衡基准 · 仓位边界");
    appendTextEl(boundHead, "em", "", "控制目标仓位的上下限。");
    boundBox.appendChild(boundHead);
    const boundGrid = document.createElement("div");
    boundGrid.className = "global-field-row";
    const boundFields = [
      {name: "core_min_position_pct", label: "增强最低仓位%", default: 5, tip: "定投增强策略的最低目标仓位。"},
      {name: "core_max_position_pct", label: "增强最高仓位%", default: 92, tip: "定投增强策略的最高目标仓位。"},
      {name: "strict_min_position_pct", label: "交易最低仓位%", default: 0, tip: "纯交易仓模式的最低目标仓位。"},
      {name: "strict_max_position_pct", label: "交易最高仓位%", default: 60, tip: "纯交易仓模式的最高目标仓位。"},
    ];
    for (const f of boundFields) {
      const label = document.createElement("label");
      label.className = "mini-field";
      if (f.tip) label.dataset.tip = f.tip;
      appendTextEl(label, "span", "", f.label);
      const input = document.createElement("input");
      input.type = "number"; input.step = "0.1"; input.min = "0"; input.max = "100";
      input.value = numberOr(globalConfigState[f.name], f.default);
      input.dataset.globalField = f.name;
      label.appendChild(input);
      boundGrid.appendChild(label);
    }
    boundBox.appendChild(boundGrid);
    body.appendChild(boundBox);

    // ② 均衡基准 — 执行层控制
    const execBox = document.createElement("div");
    execBox.className = "family-param-subgroup";
    const execHead = document.createElement("div");
    execHead.className = "family-param-group-title";
    appendTextEl(execHead, "strong", "", "均衡基准 · 执行层控制");
    appendTextEl(execHead, "em", "", "交易模式按策略本身执行；定投模式使用固定买入 + 策略偏移。");
    execBox.appendChild(execHead);
    const execGrid = document.createElement("div");
    execGrid.className = "global-field-row";
    const execFields = [
      {name: "dca_base_buy_pct", label: "定投基准买入%", default: 25, tip: "定投模式每次先给出的固定买入比例；再叠加当前策略的买卖偏移。"},
      {name: "buy_step_limit_pct", label: "买入上限%", default: 28, tip: "兼容趋势交易策略的单次买入参考。纯目标策略不会被它改写方向。"},
      {name: "sell_step_limit_pct", label: "卖出上限%", default: 45, tip: "基础单次卖出上限；严重破位时仍会按风险倍数放大。"},
    ];
    for (const f of execFields) {
      const label = document.createElement("label");
      label.className = "mini-field";
      if (f.tip) label.dataset.tip = f.tip;
      appendTextEl(label, "span", "", f.label);
      const input = document.createElement("input");
      input.type = "number"; input.step = "0.1"; input.min = "0"; input.max = "100";
      input.value = numberOr(globalConfigState[f.name], f.default);
      input.dataset.globalField = f.name;
      label.appendChild(input);
      execGrid.appendChild(label);
    }
    execBox.appendChild(execGrid);
    body.appendChild(execBox);

    // ③ 进攻/防守偏离度（信号强度 + 操作阈值）
    const renderDevGroup = (styleKey, label, tipSuffix) => {
      const devs = getDeviations(styleKey);
      const isAgg = styleKey === "aggressive";

      const box = document.createElement("div");
      box.className = "family-param-subgroup multiplier-section";
      const head = document.createElement("div");
      head.className = "family-param-group-title";
      appendTextEl(head, "strong", "", label);
      appendTextEl(head, "em", "", tipSuffix);
      box.appendChild(head);

      // 两个滑块：信号强度偏离 + 操作阈值偏离
      const g = document.createElement("div");
      g.className = "family-param-grid multiplier-grid";

      // 信号强度滑块
      const ampLbl = document.createElement("label");
      ampLbl.className = "mini-field multiplier-field";
      ampLbl.dataset.tip = isAgg
        ? "向右拖 = 策略自有的信号强度/因子权重更偏进攻。0%=与均衡基准一致。"
        : "向右拖 = 策略自有的信号强度/因子权重更偏防守。0%=与均衡基准一致。";
      appendTextEl(ampLbl, "span", "", "信号强度偏离");
      const ampInput = document.createElement("input");
      ampInput.type = "range"; ampInput.min = "0"; ampInput.max = "100"; ampInput.step = "1";
      ampInput.value = String(devs.amplitude);
      ampInput.className = "multiplier-slider";
      ampInput.dataset.globalAmpDev = styleKey;
      ampLbl.appendChild(ampInput);
      const ampVal = document.createElement("span");
      ampVal.className = "multiplier-value";
      ampVal.textContent = `${devs.amplitude}%`;
      ampLbl.appendChild(ampVal);
      g.appendChild(ampLbl);

      // 操作阈值滑块
      const thrLbl = document.createElement("label");
      thrLbl.className = "mini-field multiplier-field";
      thrLbl.dataset.tip = isAgg
        ? "向右拖 = 阈值向100%推（更宽松）。0%=与均衡基准一致。"
        : "向右拖 = 阈值向0%压（更严格）。0%=与均衡基准一致。";
      appendTextEl(thrLbl, "span", "", "操作阈值偏离");
      const thrInput = document.createElement("input");
      thrInput.type = "range"; thrInput.min = "0"; thrInput.max = "100"; thrInput.step = "1";
      thrInput.value = String(devs.threshold);
      thrInput.className = "multiplier-slider";
      thrInput.dataset.globalThrDev = styleKey;
      thrLbl.appendChild(thrInput);
      const thrVal = document.createElement("span");
      thrVal.className = "multiplier-value";
      thrVal.textContent = `${devs.threshold}%`;
      thrLbl.appendChild(thrVal);
      g.appendChild(thrLbl);

      box.appendChild(g);

      box.appendChild(renderDeviationPreview(activeFamilyKeyFromForm(), styleKey));
      return box;
    };

    body.appendChild(renderDevGroup("defensive", "防守偏离", "信号强度向0%方向压；阈值更严格。"));
    body.appendChild(renderDevGroup("aggressive", "进攻偏离", "信号强度向100%方向推；阈值更宽松。"));

    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
  }

  function closeGlobalConfigModal() {
    const modal = $("#global-config-modal");
    if (!modal) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    saveConfigDebounced();
  }

  function renderStrategyFamilyTabs() {
    const host = $("#strategy-family-tabs");
    if (!host) return;
    const activeFamily = activeFamilyKeyFromForm();
    const family = strategyFamilyInfo[activeFamily] || {};
    const style = strategyInfo[activeStyleKeyFromForm()] || {};
    host.innerHTML = "";

    const wrap = document.createElement("div");
    wrap.className = "top-strategy-select-card";

    const label = document.createElement("label");
    label.className = "top-strategy-select-label";
    appendTextEl(label, "span", "", "总体策略");

    const select = document.createElement("select");
    select.name = "strategy_family";
    select.setAttribute("form", "config-form");
    select.dataset.strategyFamilySelect = "true";
    for (const familyKey of strategyFamilyKeys) {
      const item = strategyFamilyInfo[familyKey] || {};
      const option = document.createElement("option");
      option.value = familyKey;
      option.selected = familyKey === activeFamily;
      option.textContent = item.name || familyKey;
      select.appendChild(option);
    }
    label.appendChild(select);

    const meta = document.createElement("div");
    meta.className = "top-strategy-select-meta";
    const metaLine = `${family.status || ""}${style.name ? ` · ${style.name}` : ""}`.replace(/^ · /, "");
    appendTextEl(meta, "strong", "", metaLine || "当前策略");
    appendTextEl(meta, "em", "", family.short_desc || family.desc || "");

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "strategy-card-settings-btn";
    btn.dataset.openFamilySettings = activeFamily;
    btn.setAttribute("aria-label", `编辑${family.name || activeFamily}参数`);
    btn.textContent = "参数";

    wrap.appendChild(label);
    wrap.appendChild(meta);
    wrap.appendChild(btn);
    host.appendChild(wrap);
    applyStrategySignalProfile();
  }

  function renderActiveStylePicker() {
    const host = $("#active-style-picker");
    if (!host) return;
    const familyKey = activeFamilyKeyFromForm();
    const family = strategyFamilyInfo[familyKey] || {};
    const activeStyle = activeStyleKeyFromForm();
    host.innerHTML = "";

    appendTextEl(host, "legend", "", "参数风格");

    const grid = document.createElement("div");
    grid.className = "active-style-grid";
    for (const styleKey of strategyKeys) {
      const item = strategyInfo[styleKey] || {};
      const label = document.createElement("label");
      label.className = `strategy-option ${styleKey} ${styleKey === activeStyle ? "is-active" : ""}`;
      label.dataset.tip = item.desc || "";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "strategy";
      input.value = styleKey;
      input.checked = styleKey === activeStyle;
      input.dataset.activeStyle = "true";
      label.appendChild(input);
      appendTextEl(label, "span", "", item.name || styleKey);
      appendTextEl(label, "em", "", item.desc || "");
      grid.appendChild(label);
    }
    host.appendChild(grid);

    // 全局配置按钮
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "inline-config-btn";
    btn.dataset.openGlobalConfig = "style";
    btn.setAttribute("aria-label", "编辑全局配置");
    btn.textContent = "全局配置";
    host.appendChild(btn);
  }

  function openFamilySettings(familyKey) {
    if (!strategyFamilyInfo[familyKey]) return;
    renderFamilySettingsPanel(familyKey);
    const panel = $("#strategy-family-settings");
    if (!panel) return;
    panel.hidden = false;
    panel.setAttribute("aria-hidden", "false");
  }

  function closeFamilySettings() {
    const panel = $("#strategy-family-settings");
    if (!panel) return;
    panel.hidden = true;
    panel.setAttribute("aria-hidden", "true");
  }

  function renderFamilySettingsPanel(familyKey) {
    const family = strategyFamilyInfo[familyKey] || {};
    const params = getFamilyParams(familyKey);
    const title = $("#family-settings-title");
    const subtitle = $("#family-settings-subtitle");
    const body = $("#family-settings-body");
    if (!body) return;
    if (title) title.textContent = `${family.name || familyKey} · 参数微调`;
    if (subtitle) subtitle.textContent = "编辑均衡基准参数；防守/进攻只由偏离值实时计算，不再保存单独配置。";
    body.innerHTML = "";

    const intro = document.createElement("section");
    intro.className = "family-editor-intro";
    appendTextEl(intro, "strong", "", family.name || familyKey);
    appendTextEl(intro, "p", "", family.desc || "");
    if (Array.isArray(family.axes) && family.axes.length) {
      appendTextEl(intro, "em", "", `维度：${family.axes.join(" / ")}`);
    }
    body.appendChild(intro);

    // 始终编辑均衡风格
    const styleKey = "balanced";
    const styleInfo = familyStylePreset(familyKey, styleKey);
    const entry = params.strategy_mix[styleKey] || defaultStyleEntry(styleKey, "balanced", familyKey);
    const section = document.createElement("section");
    section.className = "family-param-card is-active";
    section.dataset.familyKey = familyKey;
    section.dataset.styleKey = styleKey;

    const head = document.createElement("div");
    head.className = "family-param-head";
    const headText = document.createElement("div");
    appendTextEl(headText, "strong", "", "均衡基准参数");
    appendTextEl(headText, "em", "", styleInfo.research_note || styleInfo.desc || "");
    head.appendChild(headText);
    section.appendChild(head);

    // 非均衡时：显示偏离度；仍只编辑均衡基准。
    const activeStyle = activeStyleKeyFromForm();
    if (activeStyle !== "balanced") {
      const devs = getDeviations(activeStyle);
      const isAgg = activeStyle === "aggressive";
      const devDir = isAgg ? "进攻" : "防守";
      const tipAmp = isAgg
        ? "向右拖动 = 相关幅度类参数向100%方向推。0%=与均衡基准一致。"
        : "向右拖动 = 相关幅度类参数向0%方向压。0%=与均衡基准一致。";
      const tipThr = isAgg
        ? "向右拖动 = 相关阈值/仓位类参数向100%方向推。0%=与均衡基准一致。"
        : "向右拖动 = 相关阈值/仓位类参数向0%方向压。0%=与均衡基准一致。";

      const devBox = document.createElement("div");
      devBox.className = "family-param-subgroup multiplier-section";
      const devHead = document.createElement("div");
      devHead.className = "family-param-group-title";
      appendTextEl(devHead, "strong", "", `${devDir}偏离度`);
      appendTextEl(devHead, "em", "", "下方仍编辑均衡基准；这里实时展示当前风格由偏离计算出的有效值。");
      devBox.appendChild(devHead);

      const devGrid = document.createElement("div");
      devGrid.className = "family-param-grid multiplier-grid";
      const renderDevSlider = (field, title, value, tip) => {
        const label = document.createElement("label");
        label.className = "mini-field multiplier-field";
        label.dataset.tip = tip;
        appendTextEl(label, "span", "", title);
        const input = document.createElement("input");
        input.type = "range"; input.min = "0"; input.max = "100"; input.step = "1";
        input.value = String(value);
        input.className = "multiplier-slider";
        input.dataset.familyParam = familyKey;
        input.dataset.styleKey = activeStyle;
        input.dataset.field = field;
        label.appendChild(input);
        const val = document.createElement("span");
        val.className = "multiplier-value";
        val.textContent = `${value}%`;
        label.appendChild(val);
        devGrid.appendChild(label);
      };
      renderDevSlider("amplitude_deviation", "操作/权重幅度偏离", devs.amplitude, tipAmp);
      renderDevSlider("threshold_deviation", "阈值/仓位偏离", devs.threshold, tipThr);
      devBox.appendChild(devGrid);
      devBox.appendChild(renderDeviationPreview(familyKey, activeStyle));
      section.appendChild(devBox);
    }

    {
      const renderValueField = (grid, field) => {
        const name = field.name;
        const fallback = fieldDefaultValue(field, styleInfo, 0);
        const label = document.createElement("label");
        label.className = field.type === "checkbox" ? "mini-field mini-field-check" : "mini-field";
        label.dataset.tip = field.tip || field.desc || "";
        appendTextEl(label, "span", "", field.label || name);

        let input;
        if (field.type === "select" || field.type === "choice") {
          input = document.createElement("select");
          const options = Array.isArray(field.options) ? field.options : [];
          for (const item of options) {
            const value = Array.isArray(item) ? item[0] : item;
            const text = Array.isArray(item) ? (item[1] || item[0]) : item;
            const option = document.createElement("option");
            option.value = String(value);
            option.textContent = String(text);
            input.appendChild(option);
          }
          input.value = normaliseParamValue(entry[name], field, fallback);
        } else {
          input = document.createElement("input");
          input.type = field.type === "checkbox" ? "checkbox" : "number";
          if (input.type === "checkbox") {
            input.checked = Boolean(entry[name] ?? fallback);
          } else {
            input.step = String(field.step ?? 0.1);
            input.min = String(field.min ?? 0);
            input.max = String(field.max ?? 100);
            input.value = normaliseParamValue(entry[name], field, fallback);
          }
        }
        input.dataset.familyParam = familyKey;
        input.dataset.styleKey = styleKey;
        input.dataset.field = name;
        label.appendChild(input);
        grid.appendChild(label);
      };

      for (const group of styleParamSchema(familyKey)) {
        if (!group || typeof group !== "object") continue;

        if (group.type === "core_base_table") {
          // 仓位表 UI 已移除；策略仓位逻辑只保留在各自 Python 策略中。
          continue;
        }

        const fields = Array.isArray(group.fields) ? group.fields.filter(field => field && field.name) : [];
        if (!fields.length) continue;
        const groupBox = document.createElement("div");
        groupBox.className = "family-param-subgroup";
        if (group.title || group.desc) {
          const groupHead = document.createElement("div");
          groupHead.className = "family-param-group-title";
          if (group.title) appendTextEl(groupHead, "strong", "", group.title);
          if (group.desc) appendTextEl(groupHead, "em", "", group.desc);
          groupBox.appendChild(groupHead);
        }
        const grid = document.createElement("div");
        grid.className = "family-param-grid";
        for (const field of fields) renderValueField(grid, field);
        groupBox.appendChild(grid);
        section.appendChild(groupBox);
      }
    }

    body.appendChild(section);
  }

  function handleFamilySettingsInput(target) {
    if (!target) return false;

    const editFamily = target.dataset?.familyEditStyle;
    if (editFamily && target.checked) {
      familyEditStyleState[editFamily] = target.value || defaultStyleKey();
      renderFamilySettingsPanel(editFamily);
      return true;
    }

    const familyKey = target.dataset?.familyParam;
    if (!familyKey) return false;
    const styleKey = target.dataset.styleKey;
    const field = target.dataset.field;
    if (!styleKey || !field) return false;
    // 偏离度滑块：只改偏离值与当前展示，不写入防守/进攻单独参数。
    if (field === "amplitude_deviation" || field === "threshold_deviation") {
      const dev = clampNumber(target.value, 0, 100);
      const prev = getDeviations(styleKey);
      const ampDev = field === "amplitude_deviation" ? dev : prev.amplitude;
      const thrDev = field === "threshold_deviation" ? dev : prev.threshold;
      setGlobalDeviation(styleKey, ampDev, thrDev);
      const valEl = target.parentElement?.querySelector?.(".multiplier-value");
      if (valEl) valEl.textContent = `${dev}%`;
      refreshDeviationPreviews(target.closest(".multiplier-section") || document);
      updateStrategySummary();
      return true;
    }
    const params = getFamilyParams(familyKey);
    if (!params.strategy_mix[styleKey]) params.strategy_mix[styleKey] = defaultStyleEntry(styleKey, activeStyleKeyFromForm(), familyKey);
    const fieldSpec = iterStyleParamFields(familyKey).find(item => item.name === field) || {name: field, type: target.type === "checkbox" ? "checkbox" : "number", min: target.min, max: target.max};
    if (field === "core_base_pct") {
      const state = target.dataset.state;
      const num = clampNumber(target.value, 0, 100);
      if (!params.strategy_mix[styleKey].core_base_pct) params.strategy_mix[styleKey].core_base_pct = {};
      params.strategy_mix[styleKey].core_base_pct[state] = num;
    } else {
      const fallback = fieldDefaultValue(fieldSpec, familyStylePreset(familyKey, styleKey), 0);
      const raw = target.type === "checkbox" ? target.checked : target.value;
      params.strategy_mix[styleKey][field] = normaliseParamValue(raw, fieldSpec, fallback);
    }
    refreshDeviationPreviews(document);
    updateStrategySummary();
    return true;
  }

  function handleActiveStyleSelection(target) {
    if (!target?.matches?.('[name="strategy"]')) return false;
    markStyleSelected(target.value || defaultStyleKey());
    renderActiveStylePicker();
    renderStrategyFamilyTabs();
    const openPanel = $("#strategy-family-settings");
    const openCard = $(".family-param-card", openPanel || document);
    if (openPanel && !openPanel.hidden) {
      renderFamilySettingsPanel(openCard?.dataset.familyKey || activeFamilyKeyFromForm());
    }
    updateStrategySummary();
    return true;
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
    const fields = $$(
      `input[type="checkbox"][name="${CSS.escape(name)}"], input[type="radio"][name="${CSS.escape(name)}"]`,
      signalForm
    ).filter(el => el.dataset?.autoHidden !== "1");
    if (value === "__clear__") {
      fields.forEach(el => { el.checked = false; });
      return;
    }
    const normalized = value || "none";
    fields.forEach(el => {
      el.checked = String(el.value) === String(normalized);
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
    const safeName = CSS.escape(name);
    const safeValue = CSS.escape(value);
    const input = signalForm.querySelector(
      `input[type="checkbox"][name="${safeName}"][value="${safeValue}"], input[type="radio"][name="${safeName}"][value="${safeValue}"]`
    );
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
    setChoice("ma_position", "__clear__");

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

  function updateStrategyLabState(data = null) {
    if (!configForm) return;
    const payload = data || toConfigPayload();
    document.body.classList.add("strategy-single-mode");
    document.body.classList.remove("strategy-blend-mode");
    for (const key of strategyKeys) {
      const card = configForm.querySelector(`[data-strategy-key="${CSS.escape(key)}"]`);
      if (!card) continue;
      const selected = payload.strategy === key;
      card.classList.toggle("is-selected", selected);
      card.classList.toggle("is-active", selected);
      card.classList.toggle("is-disabled", !selected);
    }
  }


  function dataSourceText(value) {
    const key = String(value || "auto");
    return {
      auto: "自动多链路",
      danjuan_only: "只使用蛋卷",
      akshare: "AKShare",
      yfinance: "yfinance/Yahoo",
      stooq: "Stooq"
    }[key] || key;
  }

  function updateStrategySummary() {
    const summary = $("#strategy-summary");
    if (!summary || !configForm) return;
    const data = toConfigPayload();
    const modeText = data.position_mode === "core_satellite" ? "定投增强策略（固定买入 + 策略偏移）" : "纯交易仓";
    const assetText = data.symbol ? `${data.symbol_name || data.symbol} · ${data.symbol} · ${marketNames[data.market] || data.market} · ${data.asset_kind}` : "未选择标的";
    const dcaText = `定投基准买入 ${data.dca_base_buy_pct ?? 25}%`;
    summary.innerHTML = `${strategyFamilyText(data.strategy_family)}<br>${strategySummaryText(data)}<br>仓位模式：${modeText}<br>标的：${assetText}<br>数据容错：代理 ${data.proxy_mode || "system"} / 超时 ${data.request_timeout_sec || 12} 秒 / 重试 ${data.retry_count || 0} 次<br>回测无风险收益率：${data.backtest_risk_free_rate_pct ?? 2}%<br>${dcaText}<br>计划资金=100%上限，不按标的类型封顶。`;
    updateStrategyLabState(data);

    const selected = $("#selected-asset");
    if (selected) {
      selected.querySelector("strong").textContent = data.symbol ? `${data.symbol_name || data.symbol} · ${data.symbol}` : "未选择";
      selected.querySelector("em").textContent = `${data.market || "auto"} / ${data.asset_kind || "auto"} / ${dataSourceText(data.data_source)}`;
    }
  }

  async function calculateNow({quiet = true} = {}) {
    if (!signalForm) return;
    saveSignalState();
    saveScrollState();

    try {
      const payload = Object.assign({}, formToObject(signalForm), toConfigPayload());
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
      if (data.config && typeof data.config === "object") {
        for (const key of Object.keys(globalConfigState)) {
          if (data.config[key] !== undefined && data.config[key] !== null && data.config[key] !== "") {
            globalConfigState[key] = numberOr(data.config[key], globalConfigState[key]);
          }
        }
      }
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
      btn.querySelector("em").textContent = `${item.market || "auto"} / ${item.asset_kind || "auto"} / ${dataSourceText(item.source || item.data_source || "auto")}`;
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
    const currentDataSource = configForm.querySelector('[name="data_source"]')?.value || "auto";
    const mapping = {
      symbol: item.symbol || "",
      symbol_name: item.name || item.symbol || "",
      market: item.market || "auto",
      asset_kind: item.asset_kind || "auto",
      data_source: currentDataSource === "danjuan_only" ? "danjuan_only" : (item.source || item.data_source || "auto")
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
      const currentDataSource = configForm?.querySelector('[name="data_source"]')?.value || "auto";
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&data_source=${encodeURIComponent(currentDataSource)}`);
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
    if (ind.ma_position) setChoice("ma_position", ind.ma_position);
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
    if (ind.ma_position) markChoiceAuto("ma_position", ind.ma_position);
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
    if (window.__setTrendChartData) window.__setTrendChartData([], {});
    if (status) status.textContent = "已清空旧自动数据，正在拉取行情/指标…";

    try {
      const res = await postJSON("/api/fetch", data);
      if (window.__setTrendChartData) window.__setTrendChartData(res.chart_series || [], res.chart_meta || res);
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
      applyStrategySignalProfile();
      loadSignalState();
      applyStrategySignalProfile();

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
      configForm.addEventListener("change", (event) => {
        if (handleActiveStyleSelection(event.target)) {
          autoApplyInitialStopFromInputs();
          saveConfigDebounced();
          return;
        }
        if (handleFamilySettingsInput(event.target)) {
          autoApplyInitialStopFromInputs();
          saveConfigDebounced();
          return;
        }
        updateStrategySummary();
        autoApplyInitialStopFromInputs();
        saveConfigDebounced();
      });

      configForm.addEventListener("input", (event) => {
        if (handleFamilySettingsInput(event.target)) {
          autoApplyInitialStopFromInputs();
          saveConfigDebounced();
          return;
        }
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

    document.addEventListener("change", (event) => {
      const target = event.target;
      // 全局配置字段（仓位边界、执行控制）
      if (target?.dataset?.globalField) {
        globalConfigState[target.dataset.globalField] = numberOr(target.value, globalConfigState[target.dataset.globalField]);
        refreshDeviationPreviews(document);
        saveConfigDebounced();
        updateStrategySummary();
        return;
      }
      // 基础仓位表（全局弹窗内）
      if (target?.dataset?.globalCoreBase) {
        const state = target.dataset.globalCoreBase;
        const familyKey = activeFamilyKeyFromForm();
        const params = getFamilyParams(familyKey);
        if (!params.strategy_mix.balanced) params.strategy_mix.balanced = defaultStyleEntry("balanced", "balanced", familyKey);
        if (!params.strategy_mix.balanced.core_base_pct) params.strategy_mix.balanced.core_base_pct = {};
        params.strategy_mix.balanced.core_base_pct[state] = clampNumber(target.value, 0, 100);
        refreshDeviationPreviews(document);
        saveConfigDebounced();
        updateStrategySummary();
        return;
      }
      if (handleFamilySettingsInput(target)) {
        autoApplyInitialStopFromInputs();
        saveConfigDebounced();
        return;
      }
      if (!target?.matches?.('[name="strategy_family"][form="config-form"]')) return;
      syncActiveFamilyToForm();
      renderStrategyFamilyTabs();
      applyStrategySignalProfile();
      renderActiveStylePicker();
      updateStrategySummary();
      saveConfigDebounced();
    });

    document.addEventListener("input", (event) => {
      const t = event.target;
      if (t?.dataset?.globalCoreBase) {
        const familyKey = activeFamilyKeyFromForm();
        const params = getFamilyParams(familyKey);
        if (!params.strategy_mix.balanced) params.strategy_mix.balanced = defaultStyleEntry("balanced", "balanced", familyKey);
        if (!params.strategy_mix.balanced.core_base_pct) params.strategy_mix.balanced.core_base_pct = {};
        params.strategy_mix.balanced.core_base_pct[t.dataset.globalCoreBase] = clampNumber(t.value, 0, 100);
        refreshDeviationPreviews(document);
        saveConfigDebounced();
        return;
      }
      if (t?.dataset?.globalField) {
        globalConfigState[t.dataset.globalField] = numberOr(t.value, globalConfigState[t.dataset.globalField]);
        refreshDeviationPreviews(document);
        saveConfigDebounced();
        return;
      }
      // 偏离度滑块（全局弹窗内：操作幅度 + 操作阈值）
      if (t?.dataset?.globalAmpDev || t?.dataset?.globalThrDev) {
        const styleKey = t.dataset.globalAmpDev || t.dataset.globalThrDev;
        const isAmp = !!t.dataset.globalAmpDev;
        const dev = clampNumber(t.value, 0, 100);
        const cur = getDeviations(styleKey);
        if (isAmp) setGlobalDeviation(styleKey, dev, cur.threshold);
        else setGlobalDeviation(styleKey, cur.amplitude, dev);
        const parentLabel = t.closest(".multiplier-field");
        const valEl = parentLabel?.querySelector?.(".multiplier-value");
        if (valEl) valEl.textContent = `${dev}%`;
        refreshDeviationPreviews(t.closest(".multiplier-section") || document);
        saveConfigDebounced();
        return;
      }
      if (!handleFamilySettingsInput(t)) return;
      autoApplyInitialStopFromInputs();
      saveConfigDebounced();
    });

    document.addEventListener("click", (event) => {
      const openBtn = event.target?.closest?.("[data-open-family-settings]");
      if (openBtn) {
        event.preventDefault();
        event.stopPropagation();
        if (typeof event.stopImmediatePropagation === "function") event.stopImmediatePropagation();
        openFamilySettings(openBtn.dataset.openFamilySettings);
        return;
      }
      if (event.target?.closest?.("[data-close-family-settings]")) {
        event.preventDefault();
        closeFamilySettings();
      }
      // 全局配置弹窗
      const globalBtn = event.target?.closest?.("[data-open-global-config]");
      if (globalBtn) {
        event.preventDefault();
        openGlobalConfigModal();
        return;
      }
      if (event.target?.closest?.("[data-close-global-config]")) {
        event.preventDefault();
        closeGlobalConfigModal();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeFamilySettings();
        closeGlobalConfigModal();
      }
    });
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

  function updateBacktestTrendChartButton(result = lastBacktestResult) {
    const btn = $("#open-backtest-trend-chart-btn");
    if (!btn) return;
    const series = result?.backtest_chart_series || [];
    const enabled = Array.isArray(series) && series.length >= 2;
    btn.disabled = !enabled;
    btn.title = enabled ? "打开回测趋势图并显示买卖点" : "请先完成一次历史回测";
  }

  function openBacktestTrendChartFromResult() {
    const result = lastBacktestResult || {};
    const series = result.backtest_chart_series || [];
    if (!Array.isArray(series) || series.length < 2) {
      showToast("请先完成一次历史回测，且需要至少2条趋势数据", true);
      updateBacktestTrendChartButton(result);
      return;
    }
    const trades = result.backtest_trade_points || [];
    const meta = {
      ...(result.backtest_chart_meta || {}),
      ...(result.summary || {}),
    };
    if (window.__openBacktestTrendChartModal) {
      window.__openBacktestTrendChartModal(series, trades, meta);
    } else if (window.__setBacktestTrendChartData) {
      window.__setBacktestTrendChartData(series, trades, meta);
      showToast("趋势图模块已加载，但弹窗入口未初始化", true);
    } else {
      showToast("趋势图模块未加载，请刷新页面", true);
    }
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
    updateBacktestTrendChartButton(result);

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
    updateBacktestTrendChartButton(null);
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
        strategy_family: currentCfg.strategy_family || "trend_signal_control",
        strategy: currentCfg.strategy || "balanced",
        position_mode: currentCfg.position_mode || "core_satellite",
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
      for (const key of ADVANCED_CONFIG_KEYS) {
        payload[key] = currentCfg[key];
      }
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
    const backtestChartBtn = $("#open-backtest-trend-chart-btn");
    if (backtestChartBtn) {
      backtestChartBtn.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openBacktestTrendChartFromResult();
      });
      updateBacktestTrendChartButton(null);
    }
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
    renderStrategyFamilyTabs();
    syncActiveFamilyToForm();
    renderActiveStylePicker();
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
