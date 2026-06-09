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
    trend_signal_control: {name: "趋势信号风控策略", desc: "以趋势状态为主轴生成目标仓位。"},
    five_dimension_timing: {name: "五维择时策略", desc: "估值、资金、技术、情绪、基本面五维投票。"}
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

  function defaultCoreBasePct(styleKey) {
    const info = strategyInfo[styleKey] || {};
    const coreBase = info.core_base || {};
    const out = {};
    for (const state of Object.keys(strategyMarketStates)) {
      out[state] = numberOr(coreBase[state], 0.5) * 100;
    }
    return out;
  }

  function defaultStyleEntry(styleKey, selectedStyle) {
    const info = strategyInfo[styleKey] || {};
    const selected = styleKey === selectedStyle;
    return {
      enabled: selected,
      weight_pct: selected ? 100 : 0,
      buy_step_pct: numberOr(info.buy_step, 0.28) * 100,
      sell_step_pct: numberOr(info.sell_step, 0.45) * 100,
      risk_multiplier: numberOr(info.risk_multiplier, 1),
      core_base_pct: defaultCoreBasePct(styleKey)
    };
  }

  function normaliseFamilyParams(rawFamilyParams, cfg = {}) {
    const raw = rawFamilyParams && typeof rawFamilyParams === "object" ? rawFamilyParams : {};
    // 参数风格是全局选择，不再跟随总体策略分别持久化。
    // 旧配置里 raw.strategy 会被忽略，只保留该总体策略下各风格的微调参数。
    const selectedStyle = strategyKeys.includes(cfg.strategy) ? cfg.strategy : defaultStyleKey();
    const rawMix = raw.strategy_mix && typeof raw.strategy_mix === "object"
      ? raw.strategy_mix
      : (cfg.strategy_mix && typeof cfg.strategy_mix === "object" ? cfg.strategy_mix : {});
    const mix = {};
    for (const styleKey of strategyKeys) {
      const defaults = defaultStyleEntry(styleKey, selectedStyle);
      const entry = rawMix[styleKey] && typeof rawMix[styleKey] === "object" ? rawMix[styleKey] : {};
      const rawCore = entry.core_base_pct && typeof entry.core_base_pct === "object" ? entry.core_base_pct : {};
      const coreBasePct = {};
      for (const state of Object.keys(strategyMarketStates)) {
        coreBasePct[state] = numberOr(rawCore[state], defaults.core_base_pct[state]);
      }
      const selected = styleKey === selectedStyle;
      mix[styleKey] = {
        enabled: selected,
        weight_pct: selected ? 100 : 0,
        buy_step_pct: numberOr(entry.buy_step_pct, defaults.buy_step_pct),
        sell_step_pct: numberOr(entry.sell_step_pct, defaults.sell_step_pct),
        risk_multiplier: numberOr(entry.risk_multiplier, defaults.risk_multiplier),
        core_base_pct: coreBasePct
      };
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
        if (!params.strategy_mix[key]) params.strategy_mix[key] = defaultStyleEntry(key, normalized);
        params.strategy_mix[key].enabled = key === normalized;
        params.strategy_mix[key].weight_pct = key === normalized ? 100 : 0;
      }
    }
    return normalized;
  }

  function serialiseFamilyParams() {
    const out = {};
    for (const familyKey of strategyFamilyKeys) {
      const params = getFamilyParams(familyKey);
      out[familyKey] = {strategy_mix: params.strategy_mix || {}};
    }
    return out;
  }

  function syncActiveFamilyToForm() {
    const familyKey = activeFamilyKeyFromForm();
    markStyleSelected(activeStyleKeyFromForm());
    return {familyKey, params: getFamilyParams(familyKey)};
  }

  function strategyFamilyText(key) {
    const item = strategyFamilyInfo[key] || strategyFamilyInfo.trend_signal_control || {name: "总体策略", desc: ""};
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

  function collectStrategyMix(data) {
    const familyKey = data?.strategy_family || activeFamilyKeyFromForm();
    const params = getFamilyParams(familyKey);
    const selectedStyle = strategyKeys.includes(data?.strategy) ? data.strategy : activeStyleKeyFromForm();
    for (const key of strategyKeys) {
      if (!params.strategy_mix[key]) params.strategy_mix[key] = defaultStyleEntry(key, selectedStyle);
      params.strategy_mix[key].enabled = key === selectedStyle;
      params.strategy_mix[key].weight_pct = key === selectedStyle ? 100 : 0;
    }
    return params.strategy_mix;
  }

  function strategySummaryText(data) {
    const styleKey = strategyKeys.includes(data?.strategy) ? data.strategy : activeStyleKeyFromForm();
    return `参数风格：${strategyText(styleKey)}<br>风格使用方式：左侧选择当前执行风格，且不再跟随总体策略分别记忆；顶部【参数】只负责编辑各风格微调。`;
  }

  const signalUiProfiles = {
    trend_signal_control: {
      sections: {
        "market-context": {title: "② 大趋势环境", pill: "先判断能不能做"},
        "entry-setup": {title: "③ 入场信号", pill: "只保留三类"},
        "volume-confirm": {title: "④ 量价辅助确认", pill: "只加减权重"},
        "exit-setup": {title: "⑤ 减仓 / 清仓信号", pill: "风险优先"},
      },
      options: {
        "market_state:sideways": {text: "震荡/无趋势", tip: "价格反复穿越均线，假突破较多，仓位要打折。", impact: "wait"},
        "market_state:bear": {text: "200日线下方且向下", tip: "价格在200日线下方，且200日线向下；原则上不新增买入。", impact: "sell-strong"},
        "market_state:below_200": {text: "未站上200日线", tip: "价格仍在200日线下方，但已开始反弹；只能小仓验证。", impact: "wait"},
        "market_state:above_200": {text: "站上200日线", tip: "价格在200日线上方，50日线走平或向上，可以开始做趋势仓。", impact: "buy"},
        "market_state:strong_bull": {text: "50日线 > 200日线强多头", tip: "价格 > 50日线 > 200日线，且均线向上，是最适合加仓的环境。", impact: "buy-strong"},
        "market_risk": {text: "大盘/板块同步走弱", tip: "大盘、同类ETF或板块同步跌破关键均线时，单个标的信号要降权。", impact: "sell"},
        "entry_state:none": {text: "暂无买点", tip: "没有站回、突破、回踩不破或持续新高等明确买点。", impact: "wait"},
        "entry_state:reversal_50": {text: "站回50日线反转试仓", tip: "下跌后站回50日线，但未完全确认，只能试仓。", impact: "buy"},
        "entry_state:breakout": {text: "平台/前高突破", tip: "多头环境中突破平台/前高，最好用收盘价确认，而不是盘中刺破。", impact: "buy"},
        "entry_state:pullback_hold": {text: "回踩20/50日线不破", tip: "多头趋势中回踩20/50日线不破，通常比追高更稳。", impact: "buy-strong"},
        "entry_state:continuation_high": {text: "强趋势持续创新高", tip: "强多头中持续创新高，适合已有盈利后加仓，不适合亏损加仓。", impact: "buy-strong"},
        "volume_state:none": {text: "暂无明显量价信号", tip: "没有明显放量突破、回踩缩量、冲高回落、收盘未站稳或远离均线。", impact: "wait"},
        "volume_confirm": {text: "突破时放量", tip: "突破时放量是加分项，但不能替代趋势和止损。", impact: "buy"},
        "pullback_volume_dry": {text: "回踩缩量", tip: "回踩缩量说明抛压较小，是辅助确认。", impact: "buy"},
        "upper_shadow": {text: "放量长上影 / 冲高回落", tip: "长上影/冲高回落代表上方抛压，买入仓位自动降低。", impact: "sell"},
        "failed_close": {text: "收盘未站稳关键位", tip: "突破后没有收在关键位上方，不视为有效突破。", impact: "sell"},
        "far_from_ma": {text: "远离均线 / 涨速过快", tip: "价格明显远离20日/50日均线时，追高风险收益比变差。", impact: "sell"},
        "exit_state:none": {text: "暂无破位", tip: "没有跌破20/50/200日线、突破失败或触发初始止损。", impact: "wait"},
        "exit_state:below_20": {text: "跌破20日线", tip: "短线趋势弱化，适合先减一部分或收紧止损。", impact: "sell"},
        "exit_state:failed_breakout": {text: "突破失败", tip: "突破失败是常见亏损来源，优先降风险。", impact: "sell"},
        "exit_state:below_50": {text: "跌破50日线", tip: "50日线失守代表中期趋势破坏，至少大减仓。", impact: "sell-strong"},
        "exit_state:below_200": {text: "跌破200日线", tip: "200日线失守代表大趋势破坏，交易仓应退出。", impact: "sell-strong"},
        "exit_state:hit_stop": {text: "触发初始止损", tip: "初始止损触发后不要犹豫，不补亏损仓。", impact: "sell-strong"},
      }
    },
    five_dimension_timing: {
      sections: {
        "market-context": {title: "② 五维市场底色", pill: "先看维度方向"},
        "entry-setup": {title: "③ 技术维度", pill: "一票输入"},
        "volume-confirm": {title: "④ 资金 / 情绪维度", pill: "一票输入"},
        "exit-setup": {title: "⑤ 风控负票", pill: "约束仓位"},
      },
      options: {
        "market_state:sideways": {text: "技术中性：震荡/无趋势", tip: "技术维度不给明显正负票，五维策略会更多依赖估值、资金、情绪和基本面。", impact: "wait"},
        "market_state:bear": {text: "技术负票：长期下行", tip: "长期趋势处于下行，技术维度给负票，并压低可用仓位上限。", impact: "sell-strong"},
        "market_state:below_200": {text: "技术偏弱：低于长期线", tip: "价格未站上长期趋势线，技术维度偏谨慎。", impact: "sell"},
        "market_state:above_200": {text: "技术正票：站上长期趋势", tip: "长期趋势恢复，技术维度给正票，但仍需其他维度交叉验证。", impact: "buy"},
        "market_state:strong_bull": {text: "技术强正票：多周期共振", tip: "中长期趋势共振，技术维度明显偏多。", impact: "buy-strong"},
        "market_risk": {text: "资金负票：市场同步走弱", tip: "大盘/板块同步走弱，五维策略把它视为资金/风险维度负票。", impact: "sell"},
        "entry_state:none": {text: "技术维度无优势", tip: "没有反转、突破、回踩确认或趋势延续，技术维度不加分。", impact: "wait"},
        "entry_state:reversal_50": {text: "技术修复：站回中期线", tip: "中期趋势修复，技术维度小幅加分。", impact: "buy"},
        "entry_state:breakout": {text: "技术正票：结构突破", tip: "突破平台/前高，技术维度给正票。", impact: "buy"},
        "entry_state:pullback_hold": {text: "技术正票：回踩确认", tip: "回踩均线不破，说明趋势结构仍有效。", impact: "buy-strong"},
        "entry_state:continuation_high": {text: "技术强正票：趋势延续", tip: "趋势持续创新高，技术维度强加分，但情绪过热会抵消。", impact: "buy-strong"},
        "volume_state:none": {text: "资金/情绪中性", tip: "缺少明显资金或情绪信号，按0票处理。", impact: "wait"},
        "volume_confirm": {text: "资金正票：放量确认", tip: "上涨或突破伴随放量，资金维度给正票。", impact: "buy"},
        "pullback_volume_dry": {text: "资金正票：缩量回踩", tip: "回踩时缩量，说明抛压有限，资金维度加分。", impact: "buy"},
        "upper_shadow": {text: "情绪负票：冲高回落", tip: "冲高回落说明追涨情绪不稳，情绪维度给负票。", impact: "sell"},
        "failed_close": {text: "确认负票：关键位未站稳", tip: "突破或反弹未能收稳，确认维度扣分。", impact: "sell"},
        "far_from_ma": {text: "情绪负票：短期过热", tip: "价格远离均线，代表追高风险，情绪维度给负票。", impact: "sell"},
        "exit_state:none": {text: "无重大风控负票", tip: "没有触发明显风控负票。", impact: "wait"},
        "exit_state:below_20": {text: "短期风控负票", tip: "短期趋势弱化，但不必直接清仓。", impact: "sell"},
        "exit_state:failed_breakout": {text: "结构失败负票", tip: "突破失败会削弱技术和情绪维度。", impact: "sell"},
        "exit_state:below_50": {text: "中期风控负票", tip: "中期趋势失守，五维策略会明显压低目标仓位。", impact: "sell-strong"},
        "exit_state:below_200": {text: "长期风控负票", tip: "长期趋势破坏，目标仓位上限会被压低。", impact: "sell-strong"},
        "exit_state:hit_stop": {text: "硬风控负票", tip: "触发止损，优先服从风控，而不是继续投票。", impact: "sell-strong"},
      }
    },
    mini_factor_timing: {
      sections: {
        "market-context": {title: "② 趋势 / 动量因子", pill: "因子输入"},
        "entry-setup": {title: "③ 结构动量因子", pill: "因子输入"},
        "volume-confirm": {title: "④ 量能 / 过热因子", pill: "因子输入"},
        "exit-setup": {title: "⑤ 风险因子", pill: "限制上限"},
      },
      options: {
        "market_state:sideways": {text: "动量中性：噪音区", tip: "趋势因子没有明显方向，目标仓位更多由估值、回撤、波动、质量决定。", impact: "wait"},
        "market_state:bear": {text: "长期趋势因子为负", tip: "长期趋势和动量偏弱，因子策略会降低目标仓位上限。", impact: "sell-strong"},
        "market_state:below_200": {text: "价格低于长期均线", tip: "长期趋势因子偏弱，但不等于直接清仓。", impact: "sell"},
        "market_state:above_200": {text: "长期趋势因子为正", tip: "价格站上长期均线，趋势因子加分。", impact: "buy"},
        "market_state:strong_bull": {text: "多周期动量强", tip: "MA50 > MA200 或多周期动量较强，趋势因子明显加分。", impact: "buy-strong"},
        "market_risk": {text: "系统风险因子为负", tip: "市场同步走弱，作为风险因子扣分。", impact: "sell"},
        "entry_state:none": {text: "无结构动量优势", tip: "没有结构突破、趋势修复或回踩确认，结构动量不加分。", impact: "wait"},
        "entry_state:reversal_50": {text: "修复动量", tip: "价格重新站回中期均线，结构动量小幅加分。", impact: "buy"},
        "entry_state:breakout": {text: "突破动量", tip: "结构突破，动量因子加分。", impact: "buy"},
        "entry_state:pullback_hold": {text: "回踩强度保持", tip: "回踩不破说明趋势韧性较好，动量因子加分。", impact: "buy-strong"},
        "entry_state:continuation_high": {text: "持续新高动量", tip: "持续新高代表动量较强，但若远离均线会被过热因子抵消。", impact: "buy-strong"},
        "volume_state:none": {text: "量能因子中性", tip: "没有明显量能确认或风险，按中性处理。", impact: "wait"},
        "volume_confirm": {text: "量能确认因子为正", tip: "放量确认，量能因子加分。", impact: "buy"},
        "pullback_volume_dry": {text: "缩量回踩因子为正", tip: "缩量回踩说明抛压有限，量能因子加分。", impact: "buy"},
        "upper_shadow": {text: "冲高回落因子为负", tip: "冲高回落说明短期抛压或情绪不稳，扣分。", impact: "sell"},
        "failed_close": {text: "确认失败因子为负", tip: "关键位未确认，结构因子扣分。", impact: "sell"},
        "far_from_ma": {text: "过热因子为负", tip: "价格远离均线，过热风险增加，扣分。", impact: "sell"},
        "exit_state:none": {text: "风险因子未触发", tip: "没有明显风险因子触发。", impact: "wait"},
        "exit_state:below_20": {text: "短期风险因子", tip: "短期趋势变弱，降低目标仓位或加仓速度。", impact: "sell"},
        "exit_state:failed_breakout": {text: "突破失败风险因子", tip: "突破失败会削弱结构动量。", impact: "sell"},
        "exit_state:below_50": {text: "中期风险因子", tip: "中期趋势破坏，压低目标仓位上限。", impact: "sell-strong"},
        "exit_state:below_200": {text: "长期风险因子", tip: "长期趋势破坏，因子策略不允许高仓位。", impact: "sell-strong"},
        "exit_state:hit_stop": {text: "止损风险因子", tip: "触发止损后优先控制风险。", impact: "sell-strong"},
      }
    }
  };

  function setSectionTitle(sectionName, title, pill) {
    const section = document.querySelector(`[data-section="${sectionName}"]`);
    if (!section) return;
    const h2 = section.querySelector(".card-title h2");
    const badge = section.querySelector(".card-title .pill");
    if (h2) h2.textContent = title;
    if (badge) badge.textContent = pill;
  }

  function inputSelectorFromSignalKey(key) {
    const [name, value] = String(key || "").split(":");
    if (!name) return "";
    if (value === undefined) {
      return `input[type="checkbox"][name="${CSS.escape(name)}"]`;
    }
    return `input[type="checkbox"][name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`;
  }

  function relabelSignalOption(key, item) {
    if (!signalForm || !key || !item) return;
    const input = signalForm.querySelector(inputSelectorFromSignalKey(key));
    const label = input?.closest?.("label");
    if (!input || !label) return;
    const checked = input.checked;
    label.dataset.signalKey = key;
    if (item.impact) label.dataset.impact = item.impact;
    if (item.tip !== undefined) label.dataset.tip = item.tip;
    while (label.firstChild) label.removeChild(label.firstChild);
    label.appendChild(input);
    input.checked = checked;
    label.appendChild(document.createTextNode(` ${item.text || ""}`));
  }

  const sharedSignalSections = ["market-context", "entry-setup", "volume-confirm", "exit-setup"];

  function setSharedSignalSectionsVisible(visible) {
    for (const sectionName of sharedSignalSections) {
      const section = document.querySelector(`[data-section="${sectionName}"]`);
      if (!section) continue;
      section.hidden = !visible;
      section.setAttribute("aria-hidden", visible ? "false" : "true");
    }
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
    const useCustomSchema = familyKey !== "trend_signal_control" && Array.isArray(schema) && schema.length > 0;
    setSharedSignalSectionsVisible(!useCustomSchema);

    if (!useCustomSchema) {
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
    const custom = renderStrategySpecificInputs(familyKey);
    if (!custom) {
      const profile = signalUiProfiles[familyKey] || signalUiProfiles.trend_signal_control;
      const fallback = signalUiProfiles.trend_signal_control;
      const sections = Object.assign({}, fallback.sections || {}, profile.sections || {});
      for (const [sectionName, section] of Object.entries(sections)) {
        setSectionTitle(sectionName, section.title, section.pill);
      }
      const options = Object.assign({}, fallback.options || {}, profile.options || {});
      for (const [key, item] of Object.entries(options)) {
        relabelSignalOption(key, item);
      }
    }

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
    "buy_step_defensive_pct", "buy_step_balanced_pct", "buy_step_aggressive_pct",
    "sell_step_defensive_pct", "sell_step_balanced_pct", "sell_step_aggressive_pct",
    "core_step_defensive_pct", "core_step_balanced_pct", "core_step_aggressive_pct",
    "core_min_position_pct", "core_max_position_pct",
    "strict_min_position_pct", "strict_max_position_pct"
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
    const activeFamily = data.strategy_family || activeFamilyKeyFromForm();
    const selectedStyle = strategyKeys.includes(data.strategy) ? data.strategy : activeStyleKeyFromForm();
    data.strategy = markStyleSelected(selectedStyle);
    const riskFreeRaw = data.backtest_risk_free_rate_pct;
    const payload = {
      plan_amount: Number(data.plan_amount || 0),
      current_position_amount: Number(data.current_position_amount || 0),
      current_profit_pct: Number(data.current_profit_pct || 0),
      risk_per_trade_pct: Number(data.risk_per_trade_pct || 1),
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
    for (const key of ADVANCED_CONFIG_KEYS) {
      if (key === "trade_step_limit_enabled") {
        payload[key] = Boolean(data[key]);
      } else {
        const raw = data[key];
        payload[key] = raw === undefined || raw === "" ? undefined : Number(raw);
      }
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
    if (title) title.textContent = `${family.name || familyKey} · 参数设置`;
    if (subtitle) subtitle.textContent = "左侧选择全局执行参数风格；这里仅编辑该总体策略下各风格的具体参数。";
    body.innerHTML = "";

    const intro = document.createElement("section");
    intro.className = "family-editor-intro";
    appendTextEl(intro, "strong", "", family.name || familyKey);
    appendTextEl(intro, "p", "", family.desc || "");
    if (Array.isArray(family.axes) && family.axes.length) {
      appendTextEl(intro, "em", "", `维度：${family.axes.join(" / ")}`);
    }
    body.appendChild(intro);

    const picker = document.createElement("fieldset");
    picker.className = "family-style-picker";
    appendTextEl(picker, "legend", "", "编辑哪个参数风格");
    const pickerGrid = document.createElement("div");
    pickerGrid.className = "family-style-grid";
    const editStyle = strategyKeys.includes(familyEditStyleState[familyKey])
      ? familyEditStyleState[familyKey]
      : activeStyleKeyFromForm();
    familyEditStyleState[familyKey] = editStyle;
    for (const styleKey of strategyKeys) {
      const item = strategyInfo[styleKey] || {};
      const label = document.createElement("label");
      label.className = `family-style-option ${styleKey === editStyle ? "is-active" : ""}`;
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `family_edit_style_${familyKey}`;
      input.value = styleKey;
      input.checked = styleKey === editStyle;
      input.dataset.familyEditStyle = familyKey;
      label.appendChild(input);
      const text = document.createElement("span");
      appendTextEl(text, "strong", "", item.name || styleKey);
      appendTextEl(text, "em", "", styleKey === activeStyleKeyFromForm() ? "当前执行 · " + (item.desc || "") : item.desc || "");
      label.appendChild(text);
      pickerGrid.appendChild(label);
    }
    picker.appendChild(pickerGrid);
    body.appendChild(picker);

    const list = document.createElement("div");
    list.className = "family-param-list";

    // 这里只编辑一个风格的参数；当前执行风格由左侧【参数风格】选择。
    const styleKey = editStyle;
    const styleInfo = strategyInfo[styleKey] || {};
    const entry = params.strategy_mix[styleKey] || defaultStyleEntry(styleKey, activeStyleKeyFromForm());
    const section = document.createElement("section");
    section.className = "family-param-card is-active";
    section.dataset.familyKey = familyKey;
    section.dataset.styleKey = styleKey;

    const head = document.createElement("div");
    head.className = "family-param-head";
    const headText = document.createElement("div");
    appendTextEl(headText, "strong", "", `${styleInfo.name || styleKey}微调`);
    appendTextEl(headText, "em", "", styleInfo.research_note || styleInfo.desc || "");
    head.appendChild(headText);
    appendTextEl(head, "span", "family-param-badge", styleKey === activeStyleKeyFromForm() ? "当前执行" : "备用配置");
    section.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "family-param-grid";
    const fields = [
      ["buy_step_pct", "买入节奏%", "买入/加仓时的单次执行速度。越高越快接近目标仓位。", 0, 100, 0.1],
      ["sell_step_pct", "卖出节奏%", "减仓/止盈时的单次执行速度。越高卖出越快。", 0, 100, 0.1],
      ["risk_multiplier", "风险倍率", "风险预算倍率。1=默认，低于1更保守，高于1更激进。", 0.1, 5, 0.05],
    ];
    for (const [field, labelText, tip, min, max, step] of fields) {
      const label = document.createElement("label");
      label.className = "mini-field";
      label.dataset.tip = tip;
      appendTextEl(label, "span", "", labelText);
      const input = document.createElement("input");
      input.type = "number";
      input.step = String(step);
      input.min = String(min);
      input.max = String(max);
      input.value = entry[field] ?? defaultStyleEntry(styleKey, activeStyleKeyFromForm())[field];
      input.dataset.familyParam = familyKey;
      input.dataset.styleKey = styleKey;
      input.dataset.field = field;
      label.appendChild(input);
      grid.appendChild(label);
    }
    section.appendChild(grid);

    const details = document.createElement("details");
    details.className = "family-core-tune";
    appendTextEl(details, "summary", "", "目标仓位表");
    const coreGrid = document.createElement("div");
    coreGrid.className = "family-core-grid";
    for (const [state, stateName] of Object.entries(strategyMarketStates)) {
      const label = document.createElement("label");
      label.className = "mini-field";
      label.dataset.tip = `${stateName}状态下，该参数风格的基础目标仓位。`;
      appendTextEl(label, "span", "", `${stateName}%`);
      const input = document.createElement("input");
      input.type = "number";
      input.step = "0.1";
      input.min = "0";
      input.max = "100";
      input.value = entry.core_base_pct?.[state] ?? defaultCoreBasePct(styleKey)[state];
      input.dataset.familyParam = familyKey;
      input.dataset.styleKey = styleKey;
      input.dataset.field = "core_base_pct";
      input.dataset.state = state;
      label.appendChild(input);
      coreGrid.appendChild(label);
    }
    details.appendChild(coreGrid);
    section.appendChild(details);
    list.appendChild(section);
    body.appendChild(list);
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
    const params = getFamilyParams(familyKey);
    if (!params.strategy_mix[styleKey]) params.strategy_mix[styleKey] = defaultStyleEntry(styleKey, activeStyleKeyFromForm());
    const num = numberOr(target.value, 0);
    if (field === "core_base_pct") {
      const state = target.dataset.state;
      if (!params.strategy_mix[styleKey].core_base_pct) params.strategy_mix[styleKey].core_base_pct = {};
      params.strategy_mix[styleKey].core_base_pct[state] = num;
    } else {
      params.strategy_mix[styleKey][field] = num;
    }
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


  function updateStrategySummary() {
    const summary = $("#strategy-summary");
    if (!summary || !configForm) return;
    const data = toConfigPayload();
    const modeText = data.position_mode === "core_satellite" ? "定投增强策略（目标仓位动态计算）" : "纯交易仓";
    const assetText = data.symbol ? `${data.symbol_name || data.symbol} · ${data.symbol} · ${marketNames[data.market] || data.market} · ${data.asset_kind}` : "未选择标的";
    const stepText = data.trade_step_limit_enabled === false
      ? "关闭（检查日直接调到目标仓位）"
      : `开启（长期均衡补仓上限 ${data.core_step_balanced_pct ?? 22}%）`;
    summary.innerHTML = `${strategyFamilyText(data.strategy_family)}<br>${strategySummaryText(data)}<br>仓位模式：${modeText}<br>标的：${assetText}<br>数据容错：代理 ${data.proxy_mode || "system"} / 超时 ${data.request_timeout_sec || 12} 秒 / 重试 ${data.retry_count || 0} 次<br>回测无风险收益率：${data.backtest_risk_free_rate_pct ?? 2}%<br>单次操作上限：${stepText}<br>计划资金=100%上限，不按标的类型封顶。`;
    updateStrategyLabState(data);

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
      if (!handleFamilySettingsInput(event.target)) return;
      autoApplyInitialStopFromInputs();
      saveConfigDebounced();
    });

    document.addEventListener("click", (event) => {
      const openBtn = event.target?.closest?.("[data-open-family-settings]");
      if (openBtn) {
        event.preventDefault();
        event.stopPropagation();
        openFamilySettings(openBtn.dataset.openFamilySettings);
        return;
      }
      if (event.target?.closest?.("[data-close-family-settings]")) {
        event.preventDefault();
        closeFamilySettings();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeFamilySettings();
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
        strategy_family: currentCfg.strategy_family || "trend_signal_control",
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
