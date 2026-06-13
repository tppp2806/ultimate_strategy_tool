from __future__ import annotations

import copy
import csv
from collections import OrderedDict
import hashlib
import json
import math
import os
import re
import time
import contextlib
import html as html_lib
from io import StringIO
from urllib.parse import quote_plus
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

from strategies.strategy_engine import (
    ADVANCED_PARAM_DEFAULTS,
    ADVANCED_PARAM_KEYS,
    DEFAULT_STRATEGY_FAMILY,
    STRATEGY_FAMILIES,
    STRATEGY_MARKET_STATES,
    STRATEGY_PRESETS,
    _ADVANCED_PCT_KEYS,
    core_asset_floor_bounds,
    core_asset_profile,
    core_target_weight,
    get_strategy,
    lower_floor,
    normalise_strategy_family_config,
    normalise_strategy_lab_config,
    full_strategy_summary,
    strategy_family_summary,
    strategy_mix_summary,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
INDEX_MAP_PATH = os.path.join(DATA_DIR, "index_map.json")

# 历史回测落盘缓存：把历史行情/净值、历史估值，以及常用技术衍生字段保存为 CSV。
# 以后扩大回测区间时，优先读取本地缓存，只补网络上缺失的日期段。
HISTORY_CACHE_DIR = os.path.join(DATA_DIR, "history_cache")
PRICE_HISTORY_CACHE_DIR = os.path.join(HISTORY_CACHE_DIR, "prices")
VALUATION_HISTORY_CACHE_DIR = os.path.join(HISTORY_CACHE_DIR, "valuations")
# 前端搜索 / 自动拉取结果的持久化缓存。
# 搜索缓存用于“先展示本地，再增量联网刷新”；拉取缓存按自然日刷新，避免同一天重复抓行情。
SEARCH_CACHE_PATH = os.path.join(DATA_DIR, "search_cache.json")
FETCH_RESULT_CACHE_DIR = os.path.join(DATA_DIR, "fetch_cache")

app = Flask(__name__)
app.secret_key = "local-trend-risk-position-tool"

# 进程内轻量缓存：同一轮本地使用中，参数完全相同的搜索、拉取、回测、连通性测试不重复执行。
# 不落盘，不保存原始参数；只用参数摘要作为 key，避免把 Cookie/代理等敏感配置写进缓存索引。
RUNTIME_CACHE_VERSION = 25
RUNTIME_CACHE_MAX_ITEMS = 160
RUNTIME_CACHE_TTL_SEC: Dict[str, int] = {
    "search": 6 * 60 * 60,
    "fetch": 15 * 60,
    "backtest": 12 * 60 * 60,
    "connectivity": 5 * 60,
}
_RUNTIME_CACHE: OrderedDict[str, Dict[str, Any]] = OrderedDict()


def _json_cache_copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _stable_cache_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def runtime_cache_key(scope: str, payload: Dict[str, Any]) -> str:
    raw = _stable_cache_json({"v": RUNTIME_CACHE_VERSION, "scope": scope, "payload": payload})
    return f"{scope}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def runtime_cache_get(scope: str, payload: Dict[str, Any]) -> Tuple[Optional[Any], int]:
    key = runtime_cache_key(scope, payload)
    item = _RUNTIME_CACHE.get(key)
    if not item:
        return None, 0
    age = int(time.time() - float(item.get("time", 0)))
    ttl = int(RUNTIME_CACHE_TTL_SEC.get(scope, 10 * 60))
    if age > ttl:
        _RUNTIME_CACHE.pop(key, None)
        return None, 0
    # LRU：命中即刷新为最近使用，避免高频参数被淘汰。
    _RUNTIME_CACHE.move_to_end(key)
    return _json_cache_copy(item.get("value")), age


def runtime_cache_set(scope: str, payload: Dict[str, Any], value: Any) -> None:
    key = runtime_cache_key(scope, payload)
    _RUNTIME_CACHE[key] = {"time": time.time(), "value": _json_cache_copy(value)}
    _RUNTIME_CACHE.move_to_end(key)
    # OrderedDict 按插入/最近使用顺序保存，popitem(last=False) 为 O(1) 淘汰最老项。
    while len(_RUNTIME_CACHE) > RUNTIME_CACHE_MAX_ITEMS:
        _RUNTIME_CACHE.popitem(last=False)


def runtime_cache_clear(scope: Optional[str] = None) -> None:
    if not scope:
        _RUNTIME_CACHE.clear()
        return
    prefix = f"{scope}:"
    for key in list(_RUNTIME_CACHE.keys()):
        if key.startswith(prefix):
            _RUNTIME_CACHE.pop(key, None)


def add_cache_meta(payload: Dict[str, Any], hit: bool, age_sec: int = 0) -> Dict[str, Any]:
    out = _json_cache_copy(payload)
    out["cache"] = {"hit": bool(hit), "age_sec": int(age_sec)}
    return out


def _ensure_cache_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FETCH_RESULT_CACHE_DIR, exist_ok=True)


def _read_json_file(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return _json_cache_copy(default)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _json_cache_copy(default)


def _write_json_file_atomic(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _search_cache_query_key(query: str) -> str:
    return _normalize_query(str(query or ""))[:80]


def _search_cache_item_key(item: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    return (
        str(item.get("symbol") or "").strip().upper(),
        str(item.get("market") or "").strip().upper(),
        str(item.get("asset_kind") or "").strip().lower(),
        str(item.get("source") or "").strip().lower(),
        str(item.get("name") or "").strip(),
    )


def _load_search_cache() -> Dict[str, Any]:
    data = _read_json_file(SEARCH_CACHE_PATH, {"version": 1, "updated_at": "", "queries": {}, "items": []})
    if not isinstance(data, dict):
        data = {"version": 1, "updated_at": "", "queries": {}, "items": []}
    if not isinstance(data.get("queries"), dict):
        data["queries"] = {}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def _save_search_cache(data: Dict[str, Any]) -> None:
    data["version"] = 1
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 控制文件大小：保留最近/常见的最多 2000 个候选。
    items = data.get("items") if isinstance(data.get("items"), list) else []
    data["items"] = items[-2000:]
    _write_json_file_atomic(SEARCH_CACHE_PATH, data)


def _merge_search_cache_results(query: str, results: List[Dict[str, Any]], source_mode: str = "") -> None:
    if not results:
        return
    qkey = _search_cache_query_key(query)
    cache = _load_search_cache()
    old_items = [dict(x) for x in cache.get("items", []) if isinstance(x, dict)]
    by_key: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for item in old_items:
        key = _search_cache_item_key(item)
        if key[0]:
            by_key[key] = item
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query_symbols: List[str] = []
    for raw in results:
        item = dict(raw or {})
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        item["symbol"] = symbol
        item["name"] = str(item.get("name") or symbol).strip()
        item["market"] = str(item.get("market") or ("CN" if re.fullmatch(r"\d{6}", symbol) else "US")).strip().upper()
        item["asset_kind"] = resolve_asset_kind(symbol, item.get("market", ""), item.get("asset_kind", "auto"), item.get("name", ""))
        item["source"] = str(item.get("source") or source_mode or "auto").strip()
        item["cached_at"] = now
        key = _search_cache_item_key(item)
        by_key[key] = {**by_key.get(key, {}), **item}
        query_symbols.append(symbol)
    cache["items"] = list(by_key.values())
    q = cache["queries"].get(qkey, {}) if isinstance(cache.get("queries"), dict) else {}
    cache["queries"][qkey] = {
        "query": query,
        "source_mode": source_mode,
        "symbols": sorted(set(list(q.get("symbols", [])) + query_symbols)),
        "updated_at": now,
    }
    _save_search_cache(cache)


def _search_from_persistent_cache(query: str, source: str = "auto") -> List[Dict[str, str]]:
    q = str(query or "").strip()
    if not q:
        return []
    qnorm = _normalize_query(q)
    source_l = str(source or "auto").strip().lower()
    cache = _load_search_cache()
    items = [dict(x) for x in cache.get("items", []) if isinstance(x, dict)]
    matched: List[Dict[str, str]] = []
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or symbol).strip()
        if not symbol:
            continue
        if is_danjuan_only_source(source_l) and not is_danjuan_nav_like(symbol, item.get("market", "CN"), item.get("asset_kind", "auto"), name):
            continue
        haystack = _normalize_query(f"{symbol} {name} {item.get('market','')} {item.get('asset_kind','')}")
        if qnorm and qnorm not in haystack:
            continue
        matched.append({
            "symbol": symbol,
            "name": name,
            "market": str(item.get("market") or ("CN" if re.fullmatch(r"\d{6}", symbol) else "US")).strip().upper(),
            "asset_kind": str(item.get("asset_kind") or "auto").strip(),
            "source": str(item.get("source") or "local_cache").strip(),
        })
    return dedupe_symbols(matched, q)[:12]


def _fetch_cache_file(symbol: str, market: str, asset_kind: str, source: str, cfg: Dict[str, Any], symbol_name: str = "") -> str:
    key_payload = {
        "v": 2,
        "symbol": str(symbol or "").strip().upper(),
        "market": str(market or "auto").strip().upper(),
        "asset_kind": str(asset_kind or "auto").strip().lower(),
        "source": str(source or "auto").strip().lower(),
        "symbol_name": str(symbol_name or "").strip(),
        "valuation_method": str(cfg.get("valuation_method") or "system_calc"),
    }
    digest = hashlib.sha256(_stable_cache_json(key_payload).encode("utf-8")).hexdigest()[:28]
    safe_symbol = _cache_safe_part(key_payload["symbol"] or "unknown")
    return os.path.join(FETCH_RESULT_CACHE_DIR, f"{safe_symbol}__{digest}.json")


def _load_daily_fetch_cache(path: str) -> Optional[Dict[str, Any]]:
    data = _read_json_file(path, None)
    if not isinstance(data, dict):
        return None
    today = date.today().isoformat()
    if str(data.get("cache_date") or "") != today:
        return None
    records = data.get("records")
    if not isinstance(records, list) or not records:
        return None
    return data


def _save_daily_fetch_cache(path: str, records: List[Dict[str, Any]], fundamentals: Dict[str, Any], trace: List[Dict[str, Any]], source_used: str, symbol: str, market: str, asset_kind: str, symbol_name: str) -> None:
    payload = {
        "version": 2,
        "cache_date": date.today().isoformat(),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "symbol_name": symbol_name,
        "market": market,
        "asset_kind": asset_kind,
        "source_used": source_used,
        "records": records,
        "fundamentals": fundamentals or {},
        "trace": trace or [],
    }
    _write_json_file_atomic(path, payload)


def _clear_search_fetch_persistent_cache() -> int:
    count = 0
    for path in [SEARCH_CACHE_PATH]:
        try:
            if os.path.exists(path):
                os.remove(path)
                count += 1
        except Exception:
            pass
    try:
        if os.path.isdir(FETCH_RESULT_CACHE_DIR):
            for name in os.listdir(FETCH_RESULT_CACHE_DIR):
                if name.endswith(".json"):
                    try:
                        os.remove(os.path.join(FETCH_RESULT_CACHE_DIR, name))
                        count += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return count


DEFAULT_CONFIG: Dict[str, Any] = {
    "plan_amount": 10000.0,
    "current_position_amount": 0.0,
    "current_profit_pct": 0.0,
    "strategy_family": DEFAULT_STRATEGY_FAMILY,
    "strategy": "balanced",
    "strategy_mode": "single",  # 固定单风格；总体策略由 strategy_family 切换
    "strategy_mix": {},
    "strategy_family_params": {},
    "position_mode": "core_satellite",  # core_satellite / strict_trade
    "risk_per_trade_pct": 1.0,
    "backtest_risk_free_rate_pct": 2.0,
    "dca_base_buy_pct": 25.0,
    **ADVANCED_PARAM_DEFAULTS,
    "symbol": "",
    "symbol_name": "",
    "market": "auto",
    "asset_kind": "auto",
    "data_source": "auto",
    "proxy_mode": "system",  # system / custom / none
    "proxy_url": "",
    "request_timeout_sec": 12.0,
    "retry_count": 2,
    "danjuan_cookie": "",
    "valuation_method": "system_calc",  # system_calc / danjuan
}

DEFAULT_INDEX_MAPPING: Dict[str, Any] = {
    "version": 1,
    "comment": "指数/基金估值映射表。基金/ETF 自身通常没有 PE 百分位，需映射到跟踪指数。可直接编辑本文件，保存后在设置页点击『重载映射表』或重启应用。",
    "local_symbols": [
        {"symbol": "NVDA", "name": "NVIDIA Corporation / 英伟达", "market": "US", "asset_kind": "stock", "source": "yfinance"},
        {"symbol": "AAPL", "name": "Apple Inc. / 苹果", "market": "US", "asset_kind": "stock", "source": "yfinance"},
        {"symbol": "MSFT", "name": "Microsoft Corporation / 微软", "market": "US", "asset_kind": "stock", "source": "yfinance"},
        {"symbol": "TSLA", "name": "Tesla, Inc. / 特斯拉", "market": "US", "asset_kind": "stock", "source": "yfinance"},
        {"symbol": "AMD", "name": "Advanced Micro Devices / AMD", "market": "US", "asset_kind": "stock", "source": "yfinance"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust / 纳斯达克100ETF", "market": "US", "asset_kind": "etf", "source": "yfinance"},
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF / 标普500ETF", "market": "US", "asset_kind": "etf", "source": "yfinance"},
        {"symbol": "000300", "name": "沪深300指数", "market": "CN", "asset_kind": "index", "source": "akshare"},
        {"symbol": "510300", "name": "沪深300ETF", "market": "CN", "asset_kind": "etf", "source": "akshare"},
        {"symbol": "513100", "name": "纳指ETF / 纳斯达克100ETF", "market": "CN", "asset_kind": "etf", "source": "akshare"},
        {"symbol": "270042", "name": "广发纳斯达克100ETF联接(QDII)A", "market": "CN", "asset_kind": "fund", "source": "akshare"},
        {"symbol": "017641", "name": "摩根标普500指数(QDII)人民币A", "market": "CN", "asset_kind": "fund", "source": "akshare"}
    ],
    "aliases": {
        "英伟达": ["NVDA"],
        "nvidia": ["NVDA"],
        "nvda": ["NVDA"],
        "苹果": ["AAPL"],
        "微软": ["MSFT"],
        "特斯拉": ["TSLA"],
        "纳斯达克100": ["QQQ", "513100", "270042"],
        "纳指100": ["QQQ", "513100", "270042"],
        "标普500": ["SPY", "017641"],
        "s&p500": ["SPY", "017641"],
        "sp500": ["SPY", "017641"],
        "摩根标普500": ["017641"],
        "沪深300": ["000300", "510300"]
    },
    "index_codes": {
        "000300": "SH000300",
        "000016": "SH000016",
        "000905": "SH000905",
        "000852": "SH000852",
        "000688": "SH000688",
        "399006": "SZ399006",
        "399001": "SZ399001",
        "399005": "SZ399005",
        "000922": "SH000922",
        "NDX": "NDX",
        "SP500": "SP500"
    },
    "fund_index_map": {
        "510300": "SH000300",
        "159919": "SH000300",
        "110020": "SH000300",
        "510500": "SH000905",
        "159922": "SH000905",
        "160119": "SH000905",
        "512100": "SH000852",
        "159845": "SH000852",
        "588000": "SH000688",
        "588080": "SH000688",
        "159915": "SZ399006",
        "110026": "SZ399006",
        "510050": "SH000016",
        "513100": "NDX",
        "159941": "NDX",
        "270042": "NDX",
        "513500": "SP500",
        "161125": "SP500",
        "050025": "SP500",
        "017641": "SP500"
    },
    "index_names": {
        "SH000300": "沪深300",
        "SH000016": "上证50",
        "SH000905": "中证500",
        "SH000852": "中证1000",
        "SH000688": "科创50",
        "SZ399006": "创业板指",
        "SZ399001": "深证成指",
        "SZ399005": "中小100",
        "SH000922": "中证红利",
        "NDX": "纳斯达克100",
        "SP500": "标普500"
    },
    "keyword_rules": [
        {"keywords": ["纳指", "纳斯达克", "NASDAQ", "NDX"], "index_code": "NDX"},
        {"keywords": ["标普", "S&P", "SP500", "S&P500"], "index_code": "SP500"},
        {"keywords": ["沪深300", "CSI300"], "index_code": "SH000300"},
        {"keywords": ["上证50"], "index_code": "SH000016"},
        {"keywords": ["中证500"], "index_code": "SH000905"},
        {"keywords": ["中证1000"], "index_code": "SH000852"},
        {"keywords": ["科创50", "科创板50"], "index_code": "SH000688"},
        {"keywords": ["创业板", "创业板指"], "index_code": "SZ399006"},
        {"keywords": ["中证红利"], "index_code": "SH000922"}
    ]
}


def _safe_json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _normalize_index_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """校验外置映射表。顶层字段缺失时补默认；字段存在时以用户文件为准。"""
    if not isinstance(mapping, dict):
        mapping = {}
    normalized = _safe_json_copy(mapping)
    for key, default_value in DEFAULT_INDEX_MAPPING.items():
        if key not in normalized:
            normalized[key] = _safe_json_copy(default_value)

    def norm_map(src: Any, upper_value: bool = True, digits_key: bool = False) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if not isinstance(src, dict):
            return out
        for k, v in src.items():
            key = re.sub(r"\D", "", str(k)) if digits_key else str(k).strip().upper()
            val = str(v).strip().upper() if upper_value else str(v).strip()
            if key and val:
                out[key] = val
        return out

    normalized["index_codes"] = norm_map(normalized.get("index_codes"), upper_value=True, digits_key=False)
    normalized["fund_index_map"] = norm_map(normalized.get("fund_index_map"), upper_value=True, digits_key=True)
    normalized["index_names"] = norm_map(normalized.get("index_names"), upper_value=False, digits_key=False)

    local_symbols: List[Dict[str, str]] = []
    for item in normalized.get("local_symbols") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        local_symbols.append({
            "symbol": symbol.upper() if re.search(r"[A-Za-z]", symbol) else symbol,
            "name": str(item.get("name") or symbol).strip(),
            "market": str(item.get("market") or "auto").strip(),
            "asset_kind": str(item.get("asset_kind") or "auto").strip(),
            "source": str(item.get("source") or "auto").strip(),
        })
    normalized["local_symbols"] = local_symbols

    aliases: Dict[str, List[str]] = {}
    for k, values in (normalized.get("aliases") or {}).items():
        if not isinstance(values, list):
            values = [values]
        aliases[re.sub(r"\s+", "", str(k or "").strip().lower())] = [str(v).strip().upper() for v in values if str(v).strip()]
    normalized["aliases"] = aliases

    rules: List[Dict[str, Any]] = []
    for rule in normalized.get("keyword_rules") or []:
        if not isinstance(rule, dict):
            continue
        keywords = rule.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        index_code = str(rule.get("index_code") or "").strip().upper()
        if keywords and index_code:
            rules.append({"keywords": [str(k) for k in keywords if str(k)], "index_code": index_code})
    normalized["keyword_rules"] = rules
    return normalized


def load_index_mapping() -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(INDEX_MAP_PATH):
        mapping = _normalize_index_mapping(DEFAULT_INDEX_MAPPING)
        save_index_mapping(mapping, apply=False)
        return mapping
    try:
        with open(INDEX_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = DEFAULT_INDEX_MAPPING
    mapping = _normalize_index_mapping(data)
    # 回写一次：让旧文件自动补齐新增字段，但不覆盖用户已存在的映射内容。
    save_index_mapping(mapping, apply=False)
    return mapping


def save_index_mapping(mapping: Dict[str, Any], apply: bool = True) -> Dict[str, Any]:
    normalized = _normalize_index_mapping(mapping)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INDEX_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    if apply:
        apply_index_mapping(normalized)
    return normalized


def apply_index_mapping(mapping: Optional[Dict[str, Any]] = None) -> None:
    global INDEX_MAPPING, INDEX_CODE_MAP, FUND_INDEX_MAP, AK_INDEX_NAME_MAP, INDEX_KEYWORD_RULES, LOCAL_SYMBOLS, ALIASES
    INDEX_MAPPING = _normalize_index_mapping(mapping or load_index_mapping())
    INDEX_CODE_MAP = INDEX_MAPPING.get("index_codes", {})
    FUND_INDEX_MAP = INDEX_MAPPING.get("fund_index_map", {})
    AK_INDEX_NAME_MAP = INDEX_MAPPING.get("index_names", {})
    INDEX_KEYWORD_RULES = INDEX_MAPPING.get("keyword_rules", [])
    LOCAL_SYMBOLS = INDEX_MAPPING.get("local_symbols", [])
    ALIASES = INDEX_MAPPING.get("aliases", {})


INDEX_MAPPING: Dict[str, Any] = {}
INDEX_CODE_MAP: Dict[str, str] = {}
FUND_INDEX_MAP: Dict[str, str] = {}
AK_INDEX_NAME_MAP: Dict[str, str] = {}
INDEX_KEYWORD_RULES: List[Dict[str, Any]] = []
LOCAL_SYMBOLS: List[Dict[str, str]] = []
ALIASES: Dict[str, List[str]] = {}
apply_index_mapping()



@dataclass
class Decision:
    action: str = "观望"
    target_position: float = 0.0
    confidence: int = 50
    reason: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    matched_rule: str = "未触发明确信号"
    risk_score: int = 0
    trend_score: int = 0
    risk_cap: float = 0.0
    buy_step_limit: float = 0.0
    sell_step_limit: float = 0.0
    stop_distance: float = 0.0
    valuation_adjustment: float = 0.0
    quality_adjustment: float = 0.0
    core_floor: float = 0.0
    signal_quality: int = 50
    expected_reward_r: float = 0.0
    trade_frequency: str = "不操作"
    opportunity_grade: str = "中性"


def clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


def pct(x: float) -> str:
    return f"{round(x * 100):.0f}%"


def pct2(x: float) -> str:
    return f"{x * 100:.2f}%"




def valuation_method_text(value: Any) -> str:
    value = str(value or "system_calc")
    if value == "danjuan":
        return "蛋卷（雪球）"
    return "乐咕乐股"

def money(x: float) -> str:
    return f"{x:,.2f}"


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def get_bool(form: Dict[str, Any], key: str) -> bool:
    return form.get(key) == "on" or form.get(key) is True


def ensure_config() -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        cfg = DEFAULT_CONFIG.copy()
        save_config(cfg)
        return cfg

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    merged = DEFAULT_CONFIG.copy()
    merged.update(data)

    if merged.get("strategy") not in STRATEGY_PRESETS:
        old = str(merged.get("risk_sensitivity", "balanced"))
        merged["strategy"] = old if old in STRATEGY_PRESETS else "balanced"
    apply_active_family_params(merged)
    if merged.get("position_mode") not in {"core_satellite", "strict_trade"}:
        merged["position_mode"] = "core_satellite"

    merged["plan_amount"] = max(as_float(merged.get("plan_amount"), DEFAULT_CONFIG["plan_amount"]), 0.0)
    merged["current_position_amount"] = max(as_float(merged.get("current_position_amount"), 0.0), 0.0)
    # 兼容旧版：不再用"持仓成本/买入价"，改为直接填写当前涨跌幅/持仓盈亏率。
    merged.pop("cost_basis_price", None)
    merged.pop("core_floor_pct", None)
    merged.pop("asset_type", None)
    merged["current_profit_pct"] = clamp(as_float(merged.get("current_profit_pct"), 0.0), -99.99, 9999.0)
    merged["risk_per_trade_pct"] = clamp(as_float(merged.get("risk_per_trade_pct"), 1.0), 0.1, 100.0)
    merged["backtest_risk_free_rate_pct"] = clamp(as_float(merged.get("backtest_risk_free_rate_pct"), 2.0), -20.0, 30.0)

    for key in ["symbol", "symbol_name", "market", "asset_kind", "data_source", "proxy_mode", "proxy_url", "danjuan_cookie", "valuation_method"]:
        merged[key] = str(merged.get(key, DEFAULT_CONFIG.get(key, "")) or DEFAULT_CONFIG.get(key, ""))
    if merged.get("proxy_mode") not in {"system", "custom", "none"}:
        merged["proxy_mode"] = "system"
    if merged.get("valuation_method") not in {"system_calc", "danjuan"}:
        # 兼容旧配置：旧版的 auto 统一迁移到"乐咕乐股/系统自算"。
        merged["valuation_method"] = "system_calc"
    merged["request_timeout_sec"] = clamp(as_float(merged.get("request_timeout_sec"), 12.0), 3.0, 60.0)
    merged["retry_count"] = int(clamp(as_float(merged.get("retry_count"), 2.0), 0.0, 5.0))

    # 全局仓位边界：从顶层读取，缺失时回退到 ADVANCED_PARAM_DEFAULTS。
    for key, default in [
        ("core_min_position_pct", 5.0), ("core_max_position_pct", 92.0),
        ("strict_min_position_pct", 0.0), ("strict_max_position_pct", 60.0),
    ]:
        if key not in merged or merged[key] is None or merged[key] == "":
            merged[key] = default

    # 全局执行层控制：旧版节奏字段已移除；只保留定投基准与兼容旧配置字段。
    for key, default in [
        ("global_risk_multiplier", 1.0),
        ("dca_base_buy_pct", 25.0),
    ]:
        if key not in merged or merged[key] is None or merged[key] == "":
            merged[key] = default

    apply_active_family_params(merged)

    save_config(merged)
    return merged


def _balanced_only_strategy_mix(mix: Any) -> Dict[str, Any]:
    """保存层只保留 balanced 作为唯一基准；非均衡有效值运行时由 deviation 计算。"""
    if not isinstance(mix, dict):
        return {}
    balanced = mix.get("balanced") if isinstance(mix.get("balanced"), dict) else None
    if not balanced:
        return {}
    item = copy.deepcopy(balanced)
    item["enabled"] = True
    item["weight_pct"] = 100.0
    return {"balanced": item}


def _compact_config_for_save(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """避免把 defensive/aggressive 的运行时默认值写入 config.json。"""
    out = copy.deepcopy(cfg)
    out["strategy_mode"] = "single"
    out["strategy_mix"] = _balanced_only_strategy_mix(out.get("strategy_mix"))

    family_params = out.get("strategy_family_params") if isinstance(out.get("strategy_family_params"), dict) else {}
    compact_family_params: Dict[str, Dict[str, Any]] = {}
    for family_key, value in family_params.items():
        if family_key not in STRATEGY_FAMILIES or not isinstance(value, dict):
            continue
        compact_family_params[family_key] = {"strategy_mix": _balanced_only_strategy_mix(value.get("strategy_mix"))}
    active_family = str(out.get("strategy_family") or DEFAULT_STRATEGY_FAMILY)
    if active_family in STRATEGY_FAMILIES and out.get("strategy_mix"):
        compact_family_params[active_family] = {"strategy_mix": out["strategy_mix"]}
    out["strategy_family_params"] = compact_family_params
    return out


def save_config(cfg: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_compact_config_for_save(cfg), f, ensure_ascii=False, indent=2)


def apply_active_family_params(cfg: Dict[str, Any]) -> None:
    """把当前总体策略对应的微调参数载入 cfg。

    参数风格 strategy 是全局选择：切换总体策略时不再分别记忆
    防守/均衡/进攻。strategy_family_params 只保存每个总体策略下
    各参数风格的微调值（strategy_mix），不保存当前执行风格。
    """
    normalise_strategy_family_config(cfg)
    if cfg.get("strategy") not in STRATEGY_PRESETS:
        cfg["strategy"] = "balanced"

    family_key = str(cfg.get("strategy_family") or DEFAULT_STRATEGY_FAMILY)
    all_params = cfg.get("strategy_family_params") if isinstance(cfg.get("strategy_family_params"), dict) else {}
    active = all_params.get(family_key) if isinstance(all_params.get(family_key), dict) else {}

    if isinstance(active.get("strategy_mix"), dict):
        cfg["strategy_mix"] = active.get("strategy_mix") or {}

    normalise_strategy_lab_config(cfg)

    _flatten_active_style_params(cfg)

    cleaned_params: Dict[str, Dict[str, Any]] = {}
    for key, value in all_params.items():
        if key not in STRATEGY_FAMILIES or not isinstance(value, dict):
            continue
        mix = value.get("strategy_mix") if isinstance(value.get("strategy_mix"), dict) else {}
        cleaned_params[key] = {"strategy_mix": mix}

    cleaned_params[family_key] = {"strategy_mix": cfg.get("strategy_mix", {})}
    cfg["strategy_family_params"] = cleaned_params


def _flatten_active_style_params(cfg: Dict[str, Any]) -> None:
    """把当前激活风格的运行参数展平到 cfg 顶层。

    关键规则：偏离度只是一层运行时/展示层计算。
    配置文件只保存 balanced 这个基准；防守/进攻有效值始终由
    【均衡基准参数 + 当前风格 deviation】临时得到。
    """
    strategy_key = str(cfg.get("strategy", "balanced"))
    mix = cfg.get("strategy_mix") if isinstance(cfg.get("strategy_mix"), dict) else {}
    balanced = mix.get("balanced") if isinstance(mix.get("balanced"), dict) else {}

    deviation = cfg.get("deviation") if isinstance(cfg.get("deviation"), dict) else {}
    style_dev = deviation.get(strategy_key) if isinstance(deviation.get(strategy_key), dict) else {}
    amp_dev = clamp(as_float(style_dev.get("amplitude"), 0.0), 0.0, 100.0)
    thr_dev = clamp(as_float(style_dev.get("threshold"), 0.0), 0.0, 100.0)
    is_agg = strategy_key == "aggressive"

    def _deviate(base: float, dev_pct: float) -> float:
        """进攻向100%推，防守向0%压；均衡或0偏离时原样返回。"""
        base = clamp(float(base), 0.0, 100.0)
        if strategy_key == "balanced" or dev_pct <= 0:
            return round(base, 1)
        if is_agg:
            return round(base + (100.0 - base) * dev_pct / 100.0, 1)
        return round(base * (1.0 - dev_pct / 100.0), 1)

    def _base_pct(key: str, default: float) -> float:
        # 优先使用全局/左侧保存的均衡基准；缺失时回退到均衡 strategy_mix，再回退默认值。
        if key in cfg and cfg.get(key) not in (None, ""):
            return as_float(cfg.get(key), default)
        return as_float(balanced.get(key), default)

    cfg["global_risk_multiplier"] = clamp(_base_pct("global_risk_multiplier", as_float(balanced.get("risk_multiplier"), 1.0)), 0.1, 5.0)

    cfg["trade_step_limit_enabled"] = advanced_bool(balanced, "trade_step_limit_enabled", True)

    # 执行层控制 + 仓位边界：统一从均衡基准出发。
    for key, default in [
        ("dca_base_buy_pct", 25.0),
        ("core_step_pct", 22.0),  # 兼容旧配置；前端不再展示。
        ("buy_step_limit_pct", 28.0),
        ("sell_step_limit_pct", 45.0),
    ]:
        cfg[key] = clamp(_deviate(_base_pct(key, default), thr_dev), 0.0, 100.0)

    # 兼容旧执行函数：内部 buy_step/sell_step 直接等同于“买入上限/卖出上限”，不再单独维护“节奏”。
    cfg["buy_step_pct"] = cfg["buy_step_limit_pct"]
    cfg["sell_step_pct"] = cfg["sell_step_limit_pct"]

    for key, default in [
        ("core_min_position_pct", 5.0), ("core_max_position_pct", 92.0),
        ("strict_min_position_pct", 0.0), ("strict_max_position_pct", 60.0),
    ]:
        cfg[key] = clamp(_deviate(_base_pct(key, default), thr_dev), 0.0, 100.0)
    cfg["core_min_position_pct"] = min(float(cfg["core_min_position_pct"]), float(cfg["core_max_position_pct"]))
    cfg["strict_min_position_pct"] = min(float(cfg["strict_min_position_pct"]), float(cfg["strict_max_position_pct"]))

def advanced_pct(cfg: Dict[str, Any], key: str, default: float, min_value: float = 0.0, max_value: float = 100.0) -> float:
    """读取设置页百分数字段，并转换为 0~1。"""
    return clamp(as_float(cfg.get(key), default), min_value, max_value) / 100.0


def advanced_bool(cfg: Dict[str, Any], key: str, default: bool = True) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.lower() not in {"0", "false", "off", "no", ""}
    return bool(value)




def current_position_market_amount(cfg: Dict[str, Any]) -> float:
    """用户填写的“当前持仓”按账户里的当前持仓市值理解。"""
    return max(as_float(cfg.get("current_position_amount"), 0.0), 0.0)


def current_profit_factor(cfg: Dict[str, Any]) -> float:
    """持仓市值 / 已投入本金。

    例：当前持仓市值 50，持仓盈亏 +100%，说明已投入本金是 25。
    这个值只用于反推“已占用的外部计划本金”和“累计盈亏”。
    """
    profit_pct = clamp(as_float(cfg.get("current_profit_pct"), 0.0), -99.99, 9999.0)
    return max(1.0 + profit_pct / 100.0, 0.0001)


def current_position_cost_amount(cfg: Dict[str, Any]) -> float:
    """当前持仓占用的外部计划本金。

    current_position_amount 是当前市值；结合持仓盈亏率反推本金占用：
    已投入本金 = 当前持仓市值 / (1 + 持仓盈亏率)。
    """
    return current_position_market_amount(cfg) / current_profit_factor(cfg)


def current_position_pnl_amount(cfg: Dict[str, Any]) -> float:
    """当前累计盈亏 = 当前持仓市值 - 已投入外部本金。"""
    return current_position_market_amount(cfg) - current_position_cost_amount(cfg)


def current_account_equity_amount(cfg: Dict[str, Any]) -> float:
    """当前实际本金/总资产口径：计划金额 + 当前累计盈亏。

    计划金额表示用户最多愿意投入的外部本金上限；盈利后实际可管理资产应随盈利增加，
    亏损后实际可管理资产应随亏损减少。
    """
    plan = max(as_float(cfg.get("plan_amount"), 0.0), 0.0)
    equity = plan + current_position_pnl_amount(cfg)
    return max(equity, 0.0)


def remaining_plan_cash_amount(cfg: Dict[str, Any]) -> float:
    """剩余可投入外部本金，不含已浮盈部分。"""
    plan = max(as_float(cfg.get("plan_amount"), 0.0), 0.0)
    return max(plan - current_position_cost_amount(cfg), 0.0)


def current_position(cfg: Dict[str, Any]) -> float:
    """当前仓位按“当前持仓市值 / 当前实际总资产”计算。

    当前实际总资产 = 计划金额 + 当前累计盈亏。
    这与历史回测中的 equity = 现金 + 持仓市值保持同一口径。
    """
    equity = current_account_equity_amount(cfg)
    return clamp(current_position_market_amount(cfg) / equity, 0.0, 2.0) if equity > 0 else 0.0



def target_action_from_delta(cur: float, target: float, buy_label: str = "加仓", sell_label: str = "减仓") -> str:
    """根据目标仓位差额给动作命名；这里只命名，不决定是否成交。"""
    if target > cur + 0.01:
        return "买入" if cur <= 0.005 else buy_label
    if target < cur - 0.01:
        return "清仓" if target <= 0.005 else sell_label
    return "持有" if cur > 0.005 else "观望"

def parse_optional_pct(form: Dict[str, Any], key: str) -> Optional[float]:
    raw = form.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_signals(form: Dict[str, Any]) -> Dict[str, Any]:
    pe_percentile = parse_optional_pct(form, "pe_percentile")
    roe_pct = parse_optional_pct(form, "roe_pct")
    pb_percentile = parse_optional_pct(form, "pb_percentile")

    signals = {
        "market_state": str(form.get("market_state", "sideways")),
        "entry_state": str(form.get("entry_state", "none")),
        "exit_state": str(form.get("exit_state", "none")),
        "profit_state": str(form.get("profit_state", "none")),
        "volume_confirm": get_bool(form, "volume_confirm"),
        "pullback_volume_dry": get_bool(form, "pullback_volume_dry"),
        "upper_shadow": get_bool(form, "upper_shadow"),
        "failed_close": get_bool(form, "failed_close"),
        "far_from_ma": get_bool(form, "far_from_ma"),
        "market_risk": get_bool(form, "market_risk"),
        "stop_loss_pct": clamp(as_float(form.get("stop_loss_pct"), 0.0), 0.0, 80.0) / 100,
        "pe_percentile": clamp(pe_percentile, 0.0, 100.0) if pe_percentile is not None else None,
        "pb_percentile": clamp(pb_percentile, 0.0, 100.0) if pb_percentile is not None else None,
        "roe_pct": roe_pct,
    }

    # 给"小因子择时策略"保留自动行情因子。手动表单没有这些字段时为 None；
    # 自动拉取/回测时 compute_indicators 会填充，策略文件可直接读取。
    optional_numeric_keys = (
        "close", "ma20", "ma50", "ma200", "atr14", "atr_pct",
        "volume_ratio_20d", "return_20d", "return_60d", "return_120d",
        "volatility_20d", "volatility_60d", "drawdown_252d",
        "ma50_slope_20d", "ma200_slope_20d", "distance_ma20_pct", "distance_ma50_pct", "distance_ma200_pct",
        "macd_dif", "macd_dea", "macd_bar", "macd_dif_pct", "macd_dea_pct", "macd_bar_pct",
        "rsi6", "rsi14",
        "boll_mid", "boll_upper", "boll_lower", "boll_width_pct", "boll_percent_b",
    )
    for key in optional_numeric_keys:
        raw = form.get(key)
        signals[key] = as_float(raw, None) if raw not in (None, "") else None

    # 总体策略专属输入：不再使用"自动"可视选项。
    # 未选择时按 0 = 无影响信号处理；自动行情因子仍通过 hidden 字段进入策略。
    for key in (
        "five_valuation_vote", "five_fund_vote", "five_tech_vote",
        "five_sentiment_vote", "five_fundamental_vote", "five_risk_vote",
        "mini_trend_bias", "mini_structure_bias", "mini_volume_bias", "mini_risk_bias",
    ):
        raw = str(form.get(key, "0") or "0").strip()
        signals[key] = raw if raw in {"-1", "0", "1"} else "0"

    # 策略 INPUT_SCHEMA 中的其他字段（如 ma_position）透传到 signals
    for key, value in form.items():
        if key not in signals:
            signals[key] = value

    return signals


def trend_score(signals: Dict[str, Any]) -> int:
    market = signals["market_state"]
    table = {
        "bear": -60,
        "below_200": -35,
        "sideways": 0,
        "above_200": 45,
        "strong_bull": 75,
    }
    score = table.get(market, 0)
    if signals["market_risk"]:
        score -= 20
    return int(clamp(score, -100, 100))


def valuation_penalty_bonus(signals: Dict[str, Any], action: str) -> Tuple[float, List[str]]:
    """PE/PB 百分位用连续曲线，不做硬分段。返回对目标仓位的加减值。"""
    notes: List[str] = []
    adj = 0.0
    pe = signals.get("pe_percentile")
    pb = signals.get("pb_percentile")

    # PE 是主估值刹车：低估最多 +8%，高估最多 -18%。
    if pe is not None:
        if pe < 50:
            adj += (50 - pe) / 50 * 0.08
        else:
            adj -= (pe - 50) / 50 * 0.18
        if pe >= 90:
            notes.append(f"历史PE百分位 {pe:.1f}%：极高估，新增仓位明显降权。")
        elif pe >= 80:
            notes.append(f"历史PE百分位 {pe:.1f}%：高估，买入仓位降权。")
        elif pe <= 30:
            notes.append(f"历史PE百分位 {pe:.1f}%：估值偏低，允许小幅放宽仓位。")
        else:
            notes.append(f"历史PE百分位 {pe:.1f}%：估值处于可接受区间。")

    # PB 作为可选辅助，权重比 PE 低。
    if pb is not None:
        if pb < 50:
            adj += (50 - pb) / 50 * 0.03
        else:
            adj -= (pb - 50) / 50 * 0.06
        notes.append(f"历史PB百分位 {pb:.1f}%：作为辅助估值修正。")

    if action not in {"试仓", "买入", "加仓", "重仓"}:
        # 卖出/观望时只提示，不主动抬高目标；高估仍可略微强化减仓。
        adj = min(adj, 0.0)
    return clamp(adj, -0.24, 0.10), notes


def quality_bonus(signals: Dict[str, Any], action: str) -> Tuple[float, List[str]]:
    """ROE 用连续曲线：质量修正，不是买卖信号。"""
    notes: List[str] = []
    roe = signals.get("roe_pct")
    if roe is None:
        return 0.0, notes

    # 12% 为中轴，25% 附近加分接近上限；8%以下扣分。
    if roe >= 12:
        adj = min((roe - 12) / 13 * 0.07, 0.07)
    else:
        adj = -min((12 - roe) / 12 * 0.07, 0.07)

    # 极高估时，ROE不允许无限抵消估值风险。
    pe = signals.get("pe_percentile")
    if pe is not None and pe >= 90 and adj > 0:
        adj *= 0.35
        notes.append("PE极高估时，ROE优秀只保留少量质量加分，不抵消高估风险。")

    if roe >= 18:
        notes.append(f"ROE {roe:.1f}%：盈利质量较强，目标仓位小幅上调。")
    elif roe < 8:
        notes.append(f"ROE {roe:.1f}%：盈利质量偏弱，目标仓位下调。")
    else:
        notes.append(f"ROE {roe:.1f}%：质量修正接近中性。")

    if action not in {"试仓", "买入", "加仓", "重仓"}:
        adj = min(adj, 0.0)
    return clamp(adj, -0.08, 0.07), notes


def risk_score(signals: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[int, List[str]]:
    score = 0
    notes: List[str] = []
    market = signals["market_state"]
    exit_state = signals["exit_state"]

    if market == "bear":
        score += 38
        notes.append("价格位于200日线下方且200日线向下，大趋势不利。")
    elif market == "below_200":
        score += 28
        notes.append("价格仍在200日线下方，反转确认不足。")
    elif market == "sideways":
        score += 12
        notes.append("震荡环境容易假突破，仓位需要打折。")

    if exit_state == "below_20":
        score += 18
        notes.append("跌破20日线，短线趋势开始弱化。")
    elif exit_state == "failed_breakout":
        score += 30
        notes.append("突破后收盘跌回突破位，属于假突破/失败突破风险。")
    elif exit_state == "below_50":
        score += 48
        notes.append("跌破50日线，中期趋势明显受损。")
    elif exit_state in {"below_200", "hit_stop"}:
        score += 90
        notes.append("跌破200日线或初始止损，交易理由已经失效。")

    if signals["upper_shadow"]:
        score += 14
        notes.append("放量长上影/冲高回落，说明上方抛压明显。")
    if signals["failed_close"]:
        score += 14
        notes.append("收盘没有站稳关键位，突破确认不足。")
    if signals["far_from_ma"]:
        score += 12
        notes.append("价格远离均线，追高的风险收益比下降。")
    if signals["market_risk"]:
        score += 18
        notes.append("大盘/同类资产同步走弱，单个标的信号需要降权。")

    pe = signals.get("pe_percentile")
    if pe is not None:
        if pe >= 95:
            score += 24
            notes.append("历史PE百分位≥95%，估值风险非常高。")
        elif pe >= 90:
            score += 18
            notes.append("历史PE百分位≥90%，估值风险高。")
        elif pe >= 80:
            score += 10
            notes.append("历史PE百分位≥80%，买入需要更克制。")

    roe = signals.get("roe_pct")
    if roe is not None and roe < 8:
        score += 10
        notes.append("ROE低于8%，质量偏弱。")

    if cfg.get("strategy") == "defensive":
        score = int(score * 1.12)
    elif cfg.get("strategy") == "aggressive" and score < 90:
        score = int(score * 0.90)

    return int(clamp(score, 0, 100)), notes


def risk_position_cap(cfg: Dict[str, Any], signals: Dict[str, Any], strategy: Dict[str, Any]) -> Tuple[float, List[str]]:
    warnings: List[str] = []
    stop = float(signals["stop_loss_pct"])
    risk_budget = clamp(as_float(strategy.get("risk_per_trade_pct"), cfg.get("risk_per_trade_pct", 1.0)), 0.1, 100.0) / 100

    if stop <= 0:
        warnings.append("未填写止损距离：禁止新增交易仓；已有仓位只根据破位/止盈信号处理。")
        return 0.0, warnings

    # 单笔风险预算已经是趋势信号策略自己的显式参数。
    # 不再叠加隐藏的 risk_multiplier，避免用户看不到的倍率继续影响结果。
    cap = risk_budget / stop
    cap = clamp(cap, 0.0, 0.9999)

    if cap < 0.08:
        warnings.append("止损距离过宽或单笔风险预算过低，系统自动压低买入仓位。")
    elif cap >= 0.9999:
        warnings.append("按你的风险预算/止损距离计算，风险仓位上限已接近计划资金100%。")
    return cap, warnings


def buy_step_sensitivity(signals: Dict[str, Any]) -> Tuple[float, List[str]]:
    """对"本次新增仓位上限"做连续修正。

    v29 测试发现：当风险仓位上限成为主限制时，PE/ROE 如果只修正 raw target，
    最终加仓幅度可能完全相同。这里把估值/质量也作用到本次买入上限，
    让"高估限制加仓"真正体现在最终建议里。
    """
    notes: List[str] = []
    mult = 1.0

    pe = signals.get("pe_percentile")
    if pe is not None:
        pe_v = clamp(as_float(pe), 0.0, 100.0)
        if pe_v <= 60:
            pe_mult = 1.0
        elif pe_v <= 80:
            pe_mult = 1.0 - (pe_v - 60.0) / 20.0 * 0.28
        elif pe_v <= 95:
            pe_mult = 0.72 - (pe_v - 80.0) / 15.0 * 0.40
        else:
            pe_mult = 0.32 - (pe_v - 95.0) / 5.0 * 0.14
        pe_mult = clamp(pe_mult, 0.18, 1.0)
        mult *= pe_mult
        if pe_mult < 0.98:
            notes.append(f"PE百分位 {pe_v:.1f}% 偏高，本次新增仓位上限乘以 {pe_mult:.2f}。")

    roe = signals.get("roe_pct")
    if roe is not None:
        roe_v = as_float(roe)
        if roe_v < 8:
            roe_mult = 0.82
        elif roe_v < 12:
            roe_mult = 0.92
        elif roe_v <= 18:
            roe_mult = 1.0
        else:
            roe_mult = min(1.10, 1.0 + (roe_v - 18.0) / 35.0)
        mult *= roe_mult
        if roe_mult < 0.99:
            notes.append(f"ROE {roe_v:.1f}% 偏弱，本次新增仓位上限乘以 {roe_mult:.2f}。")

    # 不让质量优秀把买入上限放大到超过原始风险预算/策略上限；这里只负责降低风险。
    return clamp(mult, 0.12, 1.0), notes


def core_allocation_step_limit(cfg: Dict[str, Any], signals: Dict[str, Any], strategy: Dict[str, Any]) -> Tuple[float, List[str]]:
    """基础配置仓补足的单次买入上限。

    基础配置仓不是一笔带止损的交易，所以不应被 risk_per_trade / stop_loss_pct
    过度压低；否则核心宽基会长期低仓，实际跑输无脑定投。
    """
    notes: List[str] = []
    profile = core_asset_profile(cfg)
    strategy_key = str(cfg.get("strategy", "balanced"))
    if not advanced_bool(cfg, "trade_step_limit_enabled", True):
        notes.append("单次操作上限已关闭：本次允许直接调到目标仓位。")
        return 1.0, notes

    if profile:
        default_key = "core_step_pct"
        default_pct = ADVANCED_PARAM_DEFAULTS.get("core_step_pct", 22.0)
    else:
        default_key = "buy_step_limit_pct"
        default_pct = {"defensive": 5.0, "balanced": 8.0, "aggressive": 11.0}.get(strategy_key, 8.0)

    step = advanced_pct(cfg, default_key, float(default_pct))
    market = str(signals.get("market_state", "sideways"))
    if market == "strong_bull":
        step *= 1.15
    elif market == "above_200":
        step *= 1.00
    elif market == "sideways":
        step *= (0.88 if profile else 0.75)
    elif market == "below_200":
        step *= (0.50 if profile else 0.45)
    else:
        step *= 0.25

    pe = signals.get("pe_percentile")
    if pe is not None:
        pe_v = clamp(as_float(pe), 0.0, 100.0)
        if pe_v >= 95:
            step *= 0.45 if profile else 0.30
        elif pe_v >= 90:
            step *= 0.65 if profile else 0.40
        elif pe_v >= 80:
            step *= 0.85 if profile else 0.62
        elif pe_v <= 30:
            step *= 1.10

    if signals.get("market_risk"):
        step *= 0.55
    if signals.get("far_from_ma"):
        step *= 0.78 if profile else 0.70
    if signals.get("allocation_soft_pullback"):
        step *= 0.70 if profile else 0.55

    if signals.get("allocation_fill"):
        label = "定投增强仓" if profile else "基础配置"
        notes.append(f"{label}补足按配置仓节奏执行，本次最多新增 {pct2(step)}，不按单笔交易止损预算过度压低。")
    return clamp(step, 0.01, 1.0), notes



def signal_quality_score(signals: Dict[str, Any], action: str) -> Tuple[int, List[str]]:
    """信号质量分：近似"胜率/确定性"，不是历史胜率。

    核心思想：趋势与主信号权重大，量价/估值/ROE只做修正。
    这样避免把一堆同源指标重复加分，导致系统过度乐观。
    """
    notes: List[str] = []
    market = signals.get("market_state", "sideways")
    entry = signals.get("entry_state", "none")
    exit_state = signals.get("exit_state", "none")
    profit = signals.get("profit_state", "none")

    market_score = {
        "bear": 8,
        "below_200": 28,
        "sideways": 45,
        "above_200": 68,
        "strong_bull": 82,
    }.get(market, 45)

    entry_score = {
        "none": 0,
        "reversal_50": 38,
        "breakout": 68,
        "pullback_hold": 78,
        "continuation_high": 72,
    }.get(entry, 0)

    exit_score = {
        "none": 35,
        "below_20": 62,
        "failed_breakout": 74,
        "below_50": 84,
        "below_200": 94,
        "hit_stop": 96,
    }.get(exit_state, 35)

    volume_adj = 0
    if signals.get("volume_confirm"):
        volume_adj += 5
    if signals.get("pullback_volume_dry"):
        volume_adj += 5
    if signals.get("upper_shadow"):
        volume_adj -= 8
    if signals.get("failed_close"):
        volume_adj -= 8
    if signals.get("far_from_ma"):
        volume_adj -= 7
    if signals.get("market_risk"):
        volume_adj -= 10

    valuation_adj = 0
    pe = signals.get("pe_percentile")
    if pe is not None:
        pe_v = clamp(as_float(pe), 0.0, 100.0)
        if pe_v >= 95:
            valuation_adj -= 14
        elif pe_v >= 90:
            valuation_adj -= 10
        elif pe_v >= 80:
            valuation_adj -= 6
        elif pe_v <= 30:
            valuation_adj += 4

    roe = signals.get("roe_pct")
    if roe is not None:
        roe_v = as_float(roe)
        if roe_v < 8:
            valuation_adj -= 6
        elif roe_v >= 18:
            valuation_adj += 4

    buy_actions = {"试仓", "买入", "加仓", "重仓"}
    sell_actions = {"减仓", "大减仓", "止盈", "清仓"}
    if action in buy_actions:
        if signals.get("allocation_fill"):
            # 基础配置仓补足不是短线买点，不按"没有入场信号"重罚；
            # 但它也不能被当成高质量交易机会，只给中等信号质量。
            entry_score = 52
            score = market_score * 0.62 + entry_score * 0.26 + 50 * 0.12 + volume_adj * 0.5 + valuation_adj
            notes.append("本次属于基础配置仓补足，不等同于交易买点，仓位应逐步提高而不是一次追满。")
        else:
            score = market_score * 0.50 + entry_score * 0.38 + 50 * 0.12 + volume_adj + valuation_adj
            if entry == "none":
                score -= 25
                notes.append("没有明确买点，信号质量不支持主动加仓。")
        if market in {"bear", "below_200"} and entry != "reversal_50":
            score -= 12
        if score >= 75:
            notes.append("信号质量较高：趋势、买点和确认项相对一致。")
        elif score < 55:
            notes.append("信号质量一般：即使触发买点，也应降低新增仓位。")
    elif action in sell_actions:
        score = exit_score * 0.70 + max(0, -volume_adj) * 1.2 + (100 if profit in {"profit_2r", "profit_3r"} else 50) * 0.12
        if pe is not None and as_float(pe) >= 85:
            score += 4
        if signals.get("market_risk"):
            score += 6
        if score >= 78:
            notes.append("风险信号质量较高：退出/减仓理由比较明确。")
    else:
        score = 52 + (market_score - 50) * 0.18 + volume_adj * 0.4 + valuation_adj * 0.2
        if entry == "none" and exit_state == "none":
            notes.append("当前缺少明确操作触发点，适合降低交易频率。")

    return int(clamp(round(score), 1, 99)), notes


def expected_reward_r(signals: Dict[str, Any], action: str) -> Tuple[float, str, List[str]]:
    """预期赔率R：没有输入目标价时，只能做规则估算。

    它不是预测涨幅，而是根据入场类型、趋势、估值、量价风险估算
    "这笔新增交易是否值得做"。低于1R时不应主动开新仓。
    """
    notes: List[str] = []
    buy_actions = {"试仓", "买入", "加仓", "重仓"}
    if action not in buy_actions:
        return 0.0, "--", notes

    if signals.get("allocation_fill"):
        # 基础配置仓补足不是赔率型交易，不要求突破/回踩买点；
        # 但为了避免在明显无赔率的位置追高，只给温和的合格赔率。
        market = signals.get("market_state", "sideways")
        base = {"sideways": 1.15, "above_200": 1.45, "strong_bull": 1.65}.get(market, 1.10)
        pe = signals.get("pe_percentile")
        if pe is not None:
            pe_v = clamp(as_float(pe), 0.0, 100.0)
            if pe_v >= 95:
                base -= 0.45
            elif pe_v >= 90:
                base -= 0.30
            elif pe_v >= 80:
                base -= 0.15
            elif pe_v <= 30:
                base += 0.10
        r = clamp(base, 0.0, 2.0)
        grade = "配置赔率合格" if r >= 1.2 else "配置赔率偏低"
        if r < 1.0:
            notes.append("基础配置仓补足的估算赔率不足，系统会暂停新增。")
        return r, grade, notes

    entry = signals.get("entry_state", "none")
    market = signals.get("market_state", "sideways")
    base = {
        "none": 0.0,
        "reversal_50": 1.05,
        "breakout": 1.65,
        "pullback_hold": 2.00,
        "continuation_high": 1.45,
    }.get(entry, 0.0)

    if market == "strong_bull":
        base += 0.20
    elif market == "above_200":
        base += 0.10
    elif market == "sideways":
        base -= 0.25
    elif market == "below_200":
        base -= 0.35
    elif market == "bear":
        base -= 0.60

    if signals.get("volume_confirm"):
        base += 0.15
    if signals.get("pullback_volume_dry"):
        base += 0.20
    if signals.get("upper_shadow"):
        base -= 0.30
    if signals.get("failed_close"):
        base -= 0.35
    if signals.get("far_from_ma"):
        base -= 0.35
    if signals.get("market_risk"):
        base -= 0.25

    pe = signals.get("pe_percentile")
    if pe is not None:
        pe_v = clamp(as_float(pe), 0.0, 100.0)
        if pe_v >= 95:
            base -= 0.45
        elif pe_v >= 90:
            base -= 0.35
        elif pe_v >= 80:
            base -= 0.22
        elif pe_v <= 30:
            base += 0.12

    roe = signals.get("roe_pct")
    if roe is not None:
        roe_v = as_float(roe)
        if roe_v < 8:
            base -= 0.18
        elif roe_v >= 18:
            base += 0.08

    r = clamp(base, 0.0, 3.0)
    if r >= 1.8:
        grade = "赔率较好"
    elif r >= 1.3:
        grade = "赔率一般"
    elif r >= 1.0:
        grade = "赔率偏低"
    else:
        grade = "赔率不足"
        notes.append("预期赔率低于1R，不适合主动新增交易仓。")
    return r, grade, notes


def trade_frequency_profile(signals: Dict[str, Any], action: str) -> Tuple[str, float, List[str]]:
    """操作频率控制：避免系统把普通波动当成频繁交易机会。"""
    notes: List[str] = []
    entry = signals.get("entry_state", "none")
    market = signals.get("market_state", "sideways")
    exit_state = signals.get("exit_state", "none")
    buy_actions = {"试仓", "买入", "加仓", "重仓"}
    sell_actions = {"减仓", "大减仓", "止盈", "清仓"}

    if action in sell_actions:
        if exit_state in {"hit_stop", "below_200", "below_50"}:
            return "风险退出，不看频率", 1.0, notes
        return "防守性调整", 0.95, notes

    if action not in buy_actions:
        return "不操作 / 等待信号", 1.0, notes

    if signals.get("allocation_fill"):
        notes.append("基础配置仓补足按低频再平衡处理，避免频繁小额追买。")
        return "低频基础配置补足", 0.78, notes

    if entry == "pullback_hold":
        return "低频优先买点", 1.0, notes
    if entry == "breakout":
        if signals.get("volume_confirm"):
            return "中低频确认买点", 0.95, notes
        notes.append("突破未获得放量确认，按更高噪音的买点处理。")
        return "中频突破试错", 0.85, notes
    if entry == "continuation_high":
        if signals.get("far_from_ma") or signals.get("pe_percentile", 0) and as_float(signals.get("pe_percentile")) >= 85:
            notes.append("趋势延续买点容易变成追高，降低交易频率权重。")
            return "偏高频追涨，需克制", 0.72, notes
        return "趋势延续买点", 0.88, notes
    if entry == "reversal_50":
        if market == "below_200":
            return "反转试仓，高噪音", 0.70, ["仍在200日线下方，反转试仓属于高噪音机会。"]
        return "反转试仓", 0.80, notes

    return "不操作 / 等待信号", 1.0, notes


def dynamic_sell_step_limit(cfg: Dict[str, Any], strategy: Dict[str, Any], signals: Dict[str, Any], action: str) -> Tuple[float, List[str]]:
    """卖出上限动态化：轻微风险少卖，严重风险允许快速退出。"""
    if not advanced_bool(cfg, "trade_step_limit_enabled", True):
        return 1.0, ["单次操作上限已关闭：卖出也允许直接调到目标仓位。"]
    base = float(strategy["sell_step"])
    exit_state = signals.get("exit_state", "none")
    profit = signals.get("profit_state", "none")
    notes: List[str] = []

    if action == "清仓" or exit_state in {"hit_stop", "below_200"}:
        mult = 2.6
    elif exit_state == "below_50":
        mult = 1.45
    elif exit_state == "failed_breakout":
        mult = 1.05
    elif exit_state == "below_20":
        mult = 0.72
    elif action == "止盈" and profit == "profit_3r":
        mult = 1.05
    elif action == "止盈":
        mult = 0.82
    else:
        mult = 1.0

    pe = signals.get("pe_percentile")
    if pe is not None and as_float(pe) >= 90 and action in {"减仓", "大减仓", "止盈", "清仓"}:
        mult += 0.18
    roe = signals.get("roe_pct")
    if roe is not None and as_float(roe) < 8 and action in {"减仓", "大减仓", "止盈", "清仓"}:
        mult += 0.12
    if signals.get("upper_shadow") or signals.get("failed_close"):
        mult += 0.08
    if signals.get("market_risk"):
        mult += 0.15

    limit = clamp(base * mult, 0.06, 0.9999)
    if abs(limit - base) > 0.01:
        notes.append(f"卖出上限按风险级别动态调整为 {pct2(limit)}。")
    return limit, notes


def buy_opportunity_multiplier(signal_quality: int, expected_r: float, frequency_mult: float) -> Tuple[float, List[str], bool]:
    """把信号质量、预期赔率和交易频率合并成新增仓位修正。"""
    notes: List[str] = []
    reject = False

    if signal_quality < 45:
        quality_mult = 0.45
        notes.append(f"信号质量仅 {signal_quality}/100，新增仓位大幅降权。")
    elif signal_quality < 60:
        quality_mult = 0.72
        notes.append(f"信号质量 {signal_quality}/100，只适合小幅试仓。")
    elif signal_quality < 75:
        quality_mult = 0.90
    else:
        quality_mult = 1.0

    if expected_r <= 0:
        odds_mult = 1.0
    elif expected_r < 1.0:
        odds_mult = 0.0
        reject = True
        notes.append(f"预期赔率约 {expected_r:.2f}R，低于1R，不新增交易仓。")
    elif expected_r < 1.3:
        odds_mult = 0.55
        notes.append(f"预期赔率约 {expected_r:.2f}R，赔率偏低，新增仓位降权。")
    elif expected_r < 1.8:
        odds_mult = 0.82
    else:
        odds_mult = 1.0

    mult = clamp(quality_mult * odds_mult * frequency_mult, 0.0, 1.0)
    return mult, notes, reject



def active_strategy_family_key(cfg: Dict[str, Any]) -> str:
    """当前总体策略 key。用于把"总体策略方法论"和"参数风格"彻底分开。

    注意：这里不能调用未导入的 normalise_strategy_family_key；旧版因此异常回退到默认
    trend_signal_control，导致 UI 选了简易均线，后端仍执行趋势策略。
    """
    key = str((cfg or {}).get("strategy_family") or DEFAULT_STRATEGY_FAMILY)
    return key if key in STRATEGY_FAMILIES else DEFAULT_STRATEGY_FAMILY


def is_signal_driven_family(cfg: Dict[str, Any]) -> bool:
    """策略是否声明了信号驱动模式（纯交易仓时使用信号硬规则）。"""
    key = active_strategy_family_key(cfg)
    return bool(STRATEGY_FAMILIES.get(key, {}).get("signal_driven", False))


def strategy_rule_label(signals: Dict[str, Any], fallback: str = "目标仓位模型") -> str:
    label = str(signals.get("strategy_match_label") or "").strip()
    return label or fallback


def strategy_confidence_hint(signals: Dict[str, Any], fallback: int) -> int:
    raw = signals.get("strategy_confidence")
    try:
        return int(clamp(float(raw), 1, 99))
    except Exception:
        return int(fallback)

def raw_target_by_signal(cfg: Dict[str, Any], signals: Dict[str, Any], cur: float) -> Tuple[str, float, str, List[str], int]:
    market = signals["market_state"]
    entry = signals["entry_state"]
    exit_state = signals["exit_state"]
    profit = signals["profit_state"]
    floor = lower_floor(cfg, signals)
    reason: List[str] = []
    confidence = 50
    action = "观望"
    matched = "未触发明确信号"
    target = cur
    is_core_mode = cfg.get("position_mode") == "core_satellite"
    family_key = active_strategy_family_key(cfg)
    signal_driven = is_signal_driven_family(cfg)
    # 定投增强策略永远使用总体策略目标模型；非信号驱动类总体策略在纯交易仓模式下也必须使用自己的目标模型。
    # strategy_isolation_v3_model_branch
    # 非信号驱动总体策略，例如 simple_ma / mini_factor_timing，
    # 在纯交易仓下也必须使用自己的 target_weight()，不能回落到趋势信号硬规则。
    use_target_model = is_core_mode or (not signal_driven)
    if use_target_model:
        signals["core_target_model"] = True
        signals["strategy_model_driven"] = not signal_driven

    # 定投增强策略 = 固定定投底盘 + 当前总体策略在【交易模式】下给出的偏移。
    # 这样定投模式不会把一套“核心仓目标表”混进具体策略，策略判断仍保持纯净。
    if is_core_mode:
        strict_cfg = copy.deepcopy(cfg)
        strict_cfg["position_mode"] = "strict_trade"
        strict_signals = copy.deepcopy(signals)
        strategy_action, strategy_target, strategy_matched, strategy_reasons, strategy_confidence = raw_target_by_signal(strict_cfg, strict_signals, cur)
        strategy_delta = strategy_target - cur
        dca_delta = advanced_pct(cfg, "dca_base_buy_pct", 25.0)
        low, high = core_asset_floor_bounds("core", cfg)
        target = clamp(cur + dca_delta + strategy_delta, low, high)
        action = target_action_from_delta(cur, target, buy_label="买入", sell_label="减仓")
        # 操作说明只展示当前策略理由，不展示“固定买入 + 策略偏移”的计算过程。
        matched = f"定投增强：{strategy_matched}"
        confidence = strategy_confidence
        signals["core_target_model"] = True
        signals["strategy_model_driven"] = True
        signals["pure_strategy_target"] = True
        signals["dca_composed_target"] = True
        reason.extend(strategy_reasons)
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    # 先生成"总体策略目标仓位"，再由执行层决定是否交易。
    core_target = floor
    core_notes: List[str] = []
    if use_target_model:
        signals["core_target_model"] = True
        core_target, core_notes = core_target_weight(cfg, signals)

    # 非信号驱动策略（如【简易均线策略】）必须在这里直接返回自己的目标仓位。
    # 这类策略只允许被自身参数/风格上限影响，不能继续落入下面的
    # 初始止损、2R/3R止盈、跌破均线、突破失败等趋势交易通用规则。
    if use_target_model:
        target = core_target
        action = target_action_from_delta(cur, target, buy_label="买入", sell_label="减仓")
        base_rule = strategy_rule_label(signals, "目标仓位模型")
        confidence = strategy_confidence_hint(signals, 68 if action in {"买入", "加仓"} else 62)
        signals["pure_strategy_target"] = True
        signals["strategy_rule_isolated"] = True
        if action in {"买入", "加仓"}:
            matched = f"{base_rule}：策略买入"
        elif action in {"减仓", "清仓"}:
            matched = f"{base_rule}：策略卖出"
        else:
            matched = f"{base_rule}：策略维持"
        reason.extend(core_notes)
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    # 1. 硬退出优先。定投增强策略不再把 200日线/50日线 当作一键清零，
    # 而是切换到更低的目标仓位；纯交易仓仍沿用原来的防守逻辑。
    if exit_state == "hit_stop":
        if use_target_model:
            action = target_action_from_delta(cur, core_target, sell_label="减仓")
            target = core_target
            matched = "初始止损软风控（降低交易仓）"
            confidence = 72
            reason.append("定投增强策略下，初始止损只作为交易仓降温信号；核心配置仓是否大幅下降由50/200日线和熊市状态确认。")
            reason.extend(core_notes)
            return action, clamp(target, 0.0, 0.9999), matched, reason, confidence
        action = "清仓"
        target = floor
        matched = "跌破初始止损"
        confidence = 92
        reason.append("初始止损被触发，交易失败，先退出交易仓。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if exit_state == "below_200" and signal_driven:
        if is_core_mode:
            target = min(cur, core_target)
            action = target_action_from_delta(cur, target, sell_label="减仓")
            matched = "跌破200日线（核心仓防守目标）"
            confidence = 82
            reason.append("定投增强策略下，跌破200日线代表进入防守目标仓位；风险信号确认前不反向补仓，只在当前仓位高于防守目标时减仓。")
            reason.extend(core_notes)
            return action, clamp(target, 0.0, 0.9999), matched, reason, confidence
        action = "清仓"
        target = floor
        matched = "跌破200日线"
        confidence = 88
        reason.append("200日线是大趋势过滤线，跌破后只保留动态防守仓位，严重时可以接近空仓。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if exit_state == "below_50" and signal_driven:
        if is_core_mode:
            target = min(cur, core_target)
            action = target_action_from_delta(cur, target, sell_label="减仓")
            matched = "跌破50日线（降低交易仓）"
            confidence = 76
            reason.append("50日线失守说明中期趋势降温，定投增强策略降低交易仓；若当前仓位低于目标，则先持有观察，不在风险信号当天补仓。")
            reason.extend(core_notes)
            return action, clamp(target, 0.0, 0.9999), matched, reason, confidence
        action = "大减仓"
        target = max(floor, min(cur * 0.45, 0.30))
        matched = "跌破50日线"
        confidence = 82
        reason.append("50日线失守，中期趋势破坏，先把仓位降到防守状态。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if exit_state == "failed_breakout" and signal_driven:
        if is_core_mode:
            target = min(cur, core_target)
            action = target_action_from_delta(cur, target, sell_label="减仓")
            matched = "突破失败（交易仓降速）"
            confidence = 70
            reason.append("突破失败只削减交易仓，不把定投增强仓当成短线突破仓处理；若当前仓位已经低于目标，则不反向补仓。")
            reason.extend(core_notes)
            return action, clamp(target, 0.0, 0.9999), matched, reason, confidence
        action = "减仓"
        target = max(floor, cur * 0.65)
        matched = "突破失败"
        confidence = 76
        reason.append("突破后跌回突破位，说明这次入场信号失效，需要降低风险。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    # 2. 止盈：定投增强策略只止盈交易仓，不把核心配置仓全部卖掉。
    if profit == "profit_3r" and cur > 0:
        action = "止盈"
        target = max(core_target if is_core_mode else floor, cur * 0.50)
        matched = "盈利达到3R"
        confidence = 74
        reason.append("盈利达到3R，先兑现交易仓，剩余仓位用移动止损跟随趋势。")
        if use_target_model:
            reason.extend(core_notes)
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if profit == "profit_2r" and cur > 0 and (signals["upper_shadow"] or signals["far_from_ma"] or signals["failed_close"]):
        action = "止盈"
        target = max(core_target if is_core_mode else floor, cur * 0.67)
        matched = "盈利达到2R + 风险形态"
        confidence = 72
        reason.append("盈利达到2R且出现冲高/远离均线/未站稳，先兑现交易仓。")
        if use_target_model:
            reason.extend(core_notes)
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if exit_state == "below_20" and signal_driven:
        if is_core_mode:
            action = target_action_from_delta(cur, core_target, buy_label="加仓", sell_label="减仓")
            # 20日线只是短线信号；如果模型目标低于当前，不主动卖出，只停止追买。
            if action in {"减仓", "清仓"}:
                action = "持有" if cur > 0 else "观望"
                target = cur
                matched = "跌破20日线（暂停追买）"
                reason.append("20日线失守只代表短线降温，暂不为了短线波动卖出定投增强仓。")
            else:
                target = core_target
                matched = "跌破20日线（按目标仓位降速补足）"
                if action in {"买入", "加仓"}:
                    signals["allocation_fill"] = True
                    signals["allocation_soft_pullback"] = True
                reason.append("价格跌破20日线但中长期趋势未确认破坏，定投增强仓仍可按降速目标补足。")
            confidence = 62
            reason.extend(core_notes)
            return action, clamp(target, 0.0, 0.9999), matched, reason, confidence
        matched = "跌破20日线"
        confidence = 66
        if cur > floor:
            action = "减仓"
            target = max(floor, cur * 0.75)
            reason.append("20日线失守属于短线弱化，先减一部分，等待50日线确认。")
        else:
            action = "持有" if cur > 0 else "观望"
            target = cur
            matched = "跌破20日线（当前仓位已低于防守目标）"
            reason.append("20日线失守，但当前仓位已经低于系统防守仓位，本次不追加卖出，也不因防守仓位差额买入。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    # 3. 纯交易仓：趋势信号风控策略保留原趋势交易逻辑。
    if market == "bear":
        action = "观望" if cur <= floor else "减仓"
        target = min(cur, floor)
        matched = "大趋势空头"
        confidence = 78
        reason.append("价格在200日线下方且200日线向下，不做新增买入。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if market == "below_200":
        if entry == "reversal_50":
            action = "试仓"
            target = 0.20
            matched = "站回50日线但未站上200日线"
            confidence = 58
            reason.append("反转初期只能小仓验证，不能重仓抄底。")
        else:
            action = "观望"
            target = min(cur, max(floor, 0.20))
            matched = "反转确认不足"
            confidence = 62
            reason.append("仍在200日线下方，除小仓反转验证外不主动买入。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if market == "sideways":
        if entry in {"breakout", "pullback_hold"}:
            action = "试仓"
            target = 0.25
            matched = "震荡环境中的突破/回踩"
            confidence = 55
            reason.append("震荡市假突破多，只能小仓试错，等待站上趋势后再加。")
        else:
            action = "观望"
            target = cur
            matched = "震荡无优势入场"
            confidence = 56
            reason.append("震荡环境没有清晰优势点，不追涨也不猜底。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if market == "above_200":
        if entry == "breakout":
            action = "买入"
            target = 0.45
            matched = "200日线上方平台突破"
            confidence = 68
            reason.append("大趋势转多，平台突破可以正常买入，但仍受止损距离限制。")
        elif entry == "pullback_hold":
            action = "加仓"
            target = 0.65
            matched = "200日线上方回踩不破"
            confidence = 70
            reason.append("回踩20/50日线不破，说明趋势仍有效，可把仓位提高一档。")
        elif entry == "reversal_50":
            action = "试仓"
            target = 0.25
            matched = "刚转强但买点不足"
            confidence = 58
            reason.append("虽然站上200日线，但信号还不是强入场，只适合试仓。")
        else:
            action = "观望"
            target = cur
            matched = "趋势可交易但暂无买点"
            confidence = 58
            reason.append("趋势环境可以交易，但没有突破或回踩确认，等待更好的风险收益比。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    if market == "strong_bull":
        if entry == "continuation_high":
            action = "重仓"
            target = 0.90
            matched = "强多头持续创新高"
            confidence = 78
            reason.append("价格在50日线和200日线上方，且趋势持续创新高，可以重仓但不追到失控。")
        elif entry == "pullback_hold":
            action = "加仓"
            target = 0.75
            matched = "强多头回踩不破"
            confidence = 76
            reason.append("强趋势中回踩不破，比直接追高更稳，允许加仓。")
        elif entry == "breakout":
            action = "买入"
            target = 0.65
            matched = "强多头突破"
            confidence = 72
            reason.append("强趋势中的突破有效性更高，但仍要看止损距离决定买多少。")
        else:
            action = "观望"
            target = cur
            matched = "强趋势但无新买点"
            confidence = 60
            reason.append("趋势很强不等于任何位置都能买，等待回踩或突破后的合理点。")
        return action, clamp(target, 0.0, 0.9999), matched, reason, confidence

    return action, clamp(target, 0.0, 0.9999), matched, reason, confidence


def compute_decision(cfg: Dict[str, Any], form: Dict[str, Any]) -> Decision:
    cur = current_position(cfg)
    strategy = get_strategy(cfg)
    signals = parse_signals(form)

    # 手动修改"当前涨跌幅 / 持仓盈亏 %"后，即使没有重新拉取数据，
    # 后端也要按同一规则自动触发初始止损，避免 UI 状态和计算结果脱节。
    profit_pct = as_float(cfg.get("current_profit_pct"), 0.0)
    stop_pct = signals.get("stop_loss_pct", 0.0) * 100
    if cur > 0 and stop_pct > 0 and profit_pct <= -stop_pct:
        if core_asset_profile(cfg):
            # 【定投增强策略】不能套用短线"初始止损=硬退出"。
            # 否则会出现刚补到 50%~60%，小幅回撤后又砍到 18% 的反复打脸，
            # 解除单次操作上限后这种来回会更剧烈。这里把它降级为软风控：
            # - 若已经跌破 200 日线，保留原 below_200 防守；
            # - 若只是短线回撤，把风险状态提高到 below_50，降低交易仓但不硬砍核心仓。
            prev_exit = str(signals.get("exit_state", "none"))
            if prev_exit in {"none", "below_20", "failed_breakout"}:
                signals["exit_state"] = "below_50"
            signals["stop_triggered_auto"] = True
            signals["core_stop_softened"] = True
        else:
            signals["exit_state"] = "hit_stop"

    action, raw_target, matched, reasons, confidence = raw_target_by_signal(cfg, signals, cur)
    pure_strategy_target = bool(signals.get("pure_strategy_target"))

    risk, risk_notes = risk_score(signals, cfg)
    t_score = trend_score(signals)
    if pure_strategy_target:
        # 简易均线 / 小因子 / 定投增强合成目标等纯目标策略，不使用趋势交易的止损距离与单笔风险预算。
        r_cap, cap_warnings = 1.0, []
    else:
        r_cap, cap_warnings = risk_position_cap(cfg, signals, strategy)

    signal_quality, quality_notes2 = signal_quality_score(signals, action)
    expected_r, opportunity_grade, odds_notes = expected_reward_r(signals, action)
    trade_frequency, frequency_mult, frequency_notes = trade_frequency_profile(signals, action)
    warnings: List[str] = [] if pure_strategy_target else (risk_notes + cap_warnings)

    # 信号质量/赔率/频率是趋势交易系统层面的过滤；纯目标策略已经在自己的 Python 文件中完成判断。
    if not pure_strategy_target:
        warnings.extend(quality_notes2)
        warnings.extend(odds_notes)
        warnings.extend(frequency_notes)

    # 量价只做确认，不主导。
    # 目标仓位模型类策略（定投增强、五维、小因子）已经在各自策略文件中处理量价/情绪/风险，
    # 这里不再二次叠加，避免不同总体策略最后又被同一套趋势确认规则拉回同质化。
    if not signals.get("core_target_model"):
        if signals["volume_confirm"] and action in {"买入", "加仓", "重仓"}:
            raw_target += 0.03
            reasons.append("突破时放量，作为确认项小幅加分。")
        if signals["pullback_volume_dry"] and action in {"试仓", "买入", "加仓"}:
            raw_target += 0.02
            reasons.append("回踩缩量，说明抛压较小，作为辅助确认。")
        if signals["upper_shadow"] and action in {"买入", "加仓", "重仓"}:
            raw_target -= 0.06
            warnings.append("出现长上影/冲高回落，新买入仓位被压低。")
        if signals["failed_close"] and action in {"买入", "加仓", "重仓"}:
            raw_target -= 0.06
            warnings.append("未站稳关键价位，新增仓位被压低。")
        if signals["far_from_ma"] and action in {"买入", "加仓", "重仓"}:
            raw_target -= 0.05
            warnings.append("价格远离均线，避免追高，新增仓位被压低。")

    val_adj_raw, val_notes = valuation_penalty_bonus(signals, action)
    q_adj_raw, q_notes = quality_bonus(signals, action)
    floor = lower_floor(cfg, signals)

    # v20：估值/ROE只作为"仓位刹车"和"信号确认"，不能在无破位、无止盈、无买点时单独把仓位打到0。
    # - 买入/加仓/重仓：估值与质量修正会真实影响目标仓位。
    # - 减仓/止盈：只允许高估负向强化卖出，且不跌破系统防守仓位。
    # - 观望/持有：只保留文字提示，不改变当前仓位。
    buy_actions = {"试仓", "买入", "加仓", "重仓"}
    sell_actions = {"减仓", "大减仓", "止盈"}
    applied_val_adj = 0.0
    applied_q_adj = 0.0

    if signals.get("core_target_model"):
        # 纯目标策略的估值/质量处理只能发生在对应策略 Python 文件内部。
        # 这里不再追加任何估值/ROE提示，避免简易均线等策略被全局信息污染。
        pass
    elif action in buy_actions:
        applied_val_adj = val_adj_raw
        applied_q_adj = q_adj_raw
        raw_target += applied_val_adj + applied_q_adj
        warnings.extend(val_notes)
        warnings.extend(q_notes)
    elif action in sell_actions:
        applied_val_adj = min(val_adj_raw, 0.0)
        raw_target = max(floor, raw_target + applied_val_adj)
        warnings.extend(val_notes)
        if q_notes and signals.get("roe_pct") is not None:
            warnings.append("ROE仅作为买入质量修正；当前不是新增仓位信号，因此不改变目标仓位。")
    else:
        if val_notes:
            warnings.append("估值仅作为新增仓位刹车；未出现破位/止盈信号时，不单独触发减仓。")
        warnings.extend(val_notes)
        if q_notes and signals.get("roe_pct") is not None:
            warnings.append("ROE仅作为买入质量修正；当前不是新增仓位信号，因此不改变目标仓位。")

    is_allocation_fill = bool(signals.get("allocation_fill"))
    if pure_strategy_target:
        # 交易模式：策略给出什么就是什么；定投模式：固定买入 + 策略偏移已经在 raw_target_by_signal 中合成。
        # 这里不再用止损风险预算、赔率过滤、估值刹车或旧版补仓限制改写方向/幅度。
        base_buy_step_limit = 1.0
    elif is_allocation_fill:
        base_buy_step_limit, allocation_step_notes = core_allocation_step_limit(cfg, signals, strategy)
        warnings.extend(allocation_step_notes)
    else:
        base_buy_step_limit = 1.0 if not advanced_bool(cfg, "trade_step_limit_enabled", True) else min(float(strategy["buy_step"]), r_cap)
    buy_step_limit = base_buy_step_limit
    if action in buy_actions:
        if pure_strategy_target:
            buy_step_limit = 1.0
        elif not advanced_bool(cfg, "trade_step_limit_enabled", True):
            buy_step_limit = 1.0
            reject_for_odds = False
            warnings.append("单次操作上限已关闭：买入允许直接调到目标仓位。")
        else:
            buy_mult, buy_mult_notes = buy_step_sensitivity(signals)
            opp_mult, opp_notes, reject_for_odds = buy_opportunity_multiplier(signal_quality, expected_r, frequency_mult)
            if is_allocation_fill:
                # 底仓补足不是赔率型短线交易。它的核心目标是"比无脑定投更早建立长期暴露"，
                # 因此不再被信号质量/赔率/频率二次打折；估值、趋势、风险降速已在
                # core_allocation_step_limit 中完成。
                buy_step_limit = base_buy_step_limit
                reject_for_odds = False
            else:
                buy_step_limit = base_buy_step_limit * buy_mult * opp_mult
            if buy_mult < 0.999 and base_buy_step_limit > 0 and not is_allocation_fill:
                warnings.extend(buy_mult_notes)
            if opp_mult < 0.999 and base_buy_step_limit > 0 and not is_allocation_fill:
                warnings.extend(opp_notes)
            if reject_for_odds:
                raw_target = cur
                reasons.append("信号质量/赔率过滤未通过，本次不新增交易仓。")
    if pure_strategy_target:
        sell_step_limit = 1.0
        sell_limit_notes = []
    else:
        sell_step_limit, sell_limit_notes = dynamic_sell_step_limit(cfg, strategy, signals, action)
        warnings.extend(sell_limit_notes)

    target = raw_target
    if action in {"试仓", "买入", "加仓", "重仓"}:
        if r_cap <= 0 and not is_allocation_fill and not pure_strategy_target and advanced_bool(cfg, "trade_step_limit_enabled", True):
            target = cur
            action = "观望" if cur <= 0 else "持有"
            confidence = min(confidence, 54)
            matched = "没有止损距离，禁止新增交易仓"
            reasons.append("没有止损距离就无法计算亏损上限，因此不新增仓位。")
        else:
            limited = min(target, cur + buy_step_limit)
            if limited + 1e-9 < target:
                if is_allocation_fill:
                    warnings.append(
                        f"配置仓补足限制：本次最多新增 {pct2(buy_step_limit)}，目标由 {pct(target)} 限制为 {pct(limited)}。"
                    )
                else:
                    warnings.append(
                        f"仓位限制：按止损距离计算，本次最多新增 {pct2(buy_step_limit)}，目标由 {pct(target)} 限制为 {pct(limited)}。"
                    )
            target = limited
    elif action in {"减仓", "大减仓", "止盈"}:
        limited = max(target, cur - sell_step_limit)
        if limited > target + 1e-9:
            warnings.append(f"单次卖出限制：本次最多降 {pct2(sell_step_limit)}，目标由 {pct(target)} 限制为 {pct(limited)}。")
        target = limited
    elif action == "清仓":
        target = floor

    # 卖出/清仓类信号绝不能因为"系统防守仓位"高于当前仓位而反向买入。
    # 系统防守仓位是风险状态下最多保留到哪里，不是无买点时的补仓信号。
    sell_like_actions = {"减仓", "大减仓", "止盈", "清仓"}
    if action in sell_like_actions and target > cur + 1e-9:
        warnings.append("卖出/清仓类信号下，系统不会为了补足防守仓位而反向买入。")
        target = cur
        action = "持有" if cur > 0 else "观望"
        matched = f"{matched}（当前仓位已低于防守目标）"
        reasons.append("当前仓位已经低于系统防守仓位，本次不追加卖出，也不因防守仓位差额买入。")

    # 计划资金就是100%上限，不再根据标的类型设置上限。
    target = clamp(target, 0.0, 0.9999)

    # 防守性目标低于当前仓位时，动作不能仍显示"观望/持有"。
    # 例如未站上200日线且当前仓位过高，规则会把目标降到防守仓位，动作应明确为"减仓"。
    if target < cur - 0.005 and action in {"观望", "持有"}:
        action = "清仓" if target <= 0.005 else "减仓"
        reasons.append("目标仓位已经低于当前仓位，按防守规则降低仓位。")

    # 目标仓位已经降到接近0时，内部动作也要同步为"清仓"，避免动作和大号结果不一致。
    if target <= 0.005 and cur > 0.005 and action in {"减仓", "大减仓"}:
        action = "清仓"

    if action not in {"清仓", "大减仓"} and abs(target - cur) < 0.005:
        # 调整幅度低于执行阈值时，必须同步把目标仓位归回当前仓位，
        # 避免界面显示"持有"，但建议金额仍出现小额买入/卖出。
        target = cur
        if action in {"试仓", "买入", "加仓", "重仓"}:
            action = "持有" if cur > 0 else "观望"
            reasons.append("计算后的调整幅度低于0.5%，不建议为了小波动频繁操作。")
        elif action in {"减仓", "止盈"}:
            action = "持有"
            reasons.append("减仓幅度过小，先用移动止损观察。")
        elif action == "观望" and cur > 0:
            action = "持有"

    if not pure_strategy_target:
        if risk >= 85 and action in {"清仓", "大减仓"}:
            confidence = max(confidence, 88)
        elif risk >= 60 and action in {"买入", "加仓", "重仓"}:
            confidence = min(confidence, 58)
        elif t_score >= 60 and action in {"加仓", "重仓"}:
            confidence = min(92, confidence + 5)
    confidence = int(clamp(confidence, 1, 99))

    return Decision(
        action=action,
        target_position=target,
        confidence=confidence,
        reason=reasons,
        warnings=warnings,
        matched_rule=matched,
        risk_score=risk,
        trend_score=t_score,
        risk_cap=r_cap,
        buy_step_limit=buy_step_limit,
        sell_step_limit=sell_step_limit,
        stop_distance=signals["stop_loss_pct"],
        valuation_adjustment=applied_val_adj,
        quality_adjustment=applied_q_adj,
        core_floor=floor,
        signal_quality=signal_quality,
        expected_reward_r=expected_r,
        trade_frequency=trade_frequency,
        opportunity_grade=opportunity_grade,
    )


def amount_payload(cfg: Dict[str, Any], result: Decision) -> Dict[str, Any]:
    plan = max(as_float(cfg.get("plan_amount"), 0.0), 0.0)
    current_market_amount = current_position_market_amount(cfg)
    current_cost_amount = current_position_cost_amount(cfg)
    current_pnl_amount = current_position_pnl_amount(cfg)
    current_equity_amount = current_account_equity_amount(cfg)
    remaining_cash = remaining_plan_cash_amount(cfg)
    cur_pos = current_position(cfg)

    # 策略目标仓位按“当前实际总资产”执行：
    # 当前实际总资产 = 计划金额 + 当前累计盈亏。
    # 因此操作建议、目标仓位和历史回测的 equity 口径保持一致。
    target_amount = result.target_position * current_equity_amount
    market_diff = target_amount - current_market_amount
    if market_diff > 1e-9:
        trade_diff = min(market_diff, remaining_cash)
    elif market_diff < -1e-9:
        trade_diff = market_diff
    else:
        trade_diff = 0.0

    # 本金投入进度只用于展示：
    # - 买入时：新增买入额会占用新的外部计划本金；
    # - 卖出时：按卖出市值占当前持仓市值的比例，等比例释放原投入本金。
    # 这个字段不参与买卖决策，决策仍统一使用“当前持仓市值 / 当前总资金”的真实仓位口径。
    if trade_diff >= 0:
        target_cost_amount = current_cost_amount + trade_diff
    elif current_market_amount > 1e-9:
        sell_ratio = clamp((-trade_diff) / current_market_amount, 0.0, 1.0)
        target_cost_amount = current_cost_amount * (1.0 - sell_ratio)
    else:
        target_cost_amount = 0.0
    target_cost_amount = clamp(target_cost_amount, 0.0, plan if plan > 0 else target_cost_amount)
    current_cost_pct = current_cost_amount / plan if plan > 1e-9 else 0.0
    target_cost_pct = target_cost_amount / plan if plan > 1e-9 else 0.0

    return {
        "current_pos": cur_pos,
        "current_market_amount": current_market_amount,
        "current_cost_amount": current_cost_amount,
        "current_pnl_amount": current_pnl_amount,
        "current_equity_amount": current_equity_amount,
        "remaining_plan_cash": remaining_cash,
        "target_amount": target_amount,
        "target_cost_amount": target_cost_amount,
        "current_cost_pct": current_cost_pct,
        "target_cost_pct": target_cost_pct,
        "market_diff": market_diff,
        "diff": trade_diff,
        "direction": "买入" if trade_diff > 1e-9 else ("卖出" if trade_diff < -1e-9 else "不操作"),
    }


DATA_DISPLAY_REASON_PATTERNS = (
    r"->",
    r"\{.*\}",
    r"基础仓位.*\d",
    r"目标仓位[:：].*\d",
    r"最终.*目标.*\d",
    r"最终.*仓位.*\d",
    r"仓位修正.*\d",
    r"合计修正.*\d",
    r"原始目标.*\d",
    r"风格调整.*\d",
    r"允许区间",
    r"投票结果",
    r"合计 [+-]?\d",
    # 以下都是执行/合成/边界信息，不属于“当前策略理由”。
    r"定投增强底盘",
    r"策略偏移",
    r"固定买入",
    r"当前总体策略",
    r"不再套用",
    r"执行层",
    r"仓位边界限制",
    r"策略原始调整",
    r"原始调整",
    r"边界后实际调整",
    r"相对当前仓位",
    r"本次最多",
    r"目标由.*限制",
    r"低于执行阈值",
    r"计算后",
)


def _is_data_display_reason(text: str) -> bool:
    """右侧解释区只放理由，不放计算过程、票数、百分比等数据展示。"""
    compact = str(text or "")
    return any(re.search(pattern, compact) for pattern in DATA_DISPLAY_REASON_PATTERNS)


def join_reason_text(items: List[Any]) -> str:
    """把多条解释合并成一句；过滤掉数据展示型明细，只保留理由说明。"""
    cleaned: List[str] = []
    seen = set()
    for item in items:
        text = str(item).strip()
        text = re.sub(r"[。；;\s]+$", "", text)
        if not text or _is_data_display_reason(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        return "当前信号不足，按规则维持原仓位。"
    # 结果区只保留当前理由，避免把计算过程堆成一长串。
    return "；".join(cleaned[:3]) + "。"


def decision_to_payload(cfg: Dict[str, Any], result: Decision) -> Dict[str, Any]:
    amount = amount_payload(cfg, result)
    cur_pos = amount["current_pos"]
    equity_amount = max(float(amount.get("current_equity_amount") or 0.0), 1e-9)
    trade_delta = float(amount.get("diff") or 0.0) / equity_amount
    strategy = get_strategy(cfg)

    reason_text = join_reason_text(result.reason)

    # 顶部【实时计算结果】显示“真实本次操作占比”，与回测的【操作占比%】保持同一口径。
    # 目标仓位仍显示在指标区；标题不再用策略参数里的原始调整幅度误导实际成交。
    if trade_delta > 0.005:
        action_label = "加仓" if cur_pos > 0.005 else "买入"
        action_delta_text = f"+{pct(trade_delta)}"
        action_text = f"{action_label}{action_delta_text}"
        action_tone = "buy"
        headline = action_text
        subline = reason_text
    elif trade_delta < -0.005:
        action_label = "清仓" if result.target_position <= 0.005 or result.action == "清仓" else ("止盈" if result.action == "止盈" else "减仓")
        action_delta_text = f"-{pct(abs(trade_delta))}"
        action_text = f"{action_label}{action_delta_text}"
        action_tone = "sell"
        headline = action_text
        subline = reason_text
    else:
        action_label = "持有" if cur_pos > 0.005 else "观望"
        action_delta_text = f"维持 {pct(result.target_position)}"
        action_text = action_label
        action_tone = "hold"
        headline = action_delta_text
        subline = reason_text

    if amount["direction"] == "买入":
        amount_action = f"买入 {money(amount['diff'])}"
    elif amount["direction"] == "卖出":
        amount_action = f"卖出 {money(-amount['diff'])}"
    else:
        amount_action = "不操作"

    invested_pct_text = f"{pct(amount['current_cost_pct'])}→{pct(amount['target_cost_pct'])}"
    position_adjust_text = f"{pct(amount['current_pos'])}→{pct(result.target_position)}"
    metrics = [
        {"label": "建议金额", "value": amount_action},
        {"label": "仓位调整", "value": position_adjust_text},
        {"label": "已投入本金%", "value": invested_pct_text},
        {"label": "当前总资金", "value": money(amount["current_equity_amount"])},
        {"label": "信号质量", "value": f"{result.signal_quality}/100"},
        {"label": "预期赔率", "value": "--" if result.expected_reward_r <= 0 else f"{result.expected_reward_r:.2f}R（{result.opportunity_grade}）"},
        {"label": "操作频率", "value": result.trade_frequency},
        {"label": "止损距离", "value": pct2(result.stop_distance)},
        {"label": "风险仓位上限", "value": pct2(result.risk_cap)},
        {"label": "本次买入上限", "value": pct2(result.buy_step_limit)},
        {"label": "本次卖出上限", "value": pct2(result.sell_step_limit)},
        {"label": "估值修正", "value": pct2(result.valuation_adjustment)},
        {"label": "ROE修正", "value": pct2(result.quality_adjustment)},
        {"label": "系统防守仓位", "value": pct2(result.core_floor)},
        {"label": "标的", "value": f"{cfg.get('symbol_name') or '--'} {cfg.get('symbol') or ''}".strip(), "wide": True},
        {"label": "总体策略", "value": strategy_family_summary(cfg).replace("<br>", " / "), "wide": True},
        {"label": "参数风格", "value": f"{strategy['name']}：{strategy['desc']}", "wide": True},
        {"label": "仓位模式", "value": "定投增强策略" if cfg.get("position_mode") == "core_satellite" else "纯交易仓", "wide": True},
        {"label": "命中规则", "value": result.matched_rule, "wide": True},
    ]

    # 趋势交易专属指标：只有趋势信号策略 + 纯交易仓才展示。
    # 简易均线、小因子、定投增强等模式不会使用单笔风险预算/止损风险仓位，避免界面继续误导。
    show_trend_trade_risk = is_signal_driven_family(cfg) and cfg.get("position_mode") == "strict_trade"
    if not show_trend_trade_risk:
        trend_only_metric_labels = {
            "信号质量", "预期赔率", "操作频率", "止损距离", "风险仓位上限",
            "本次买入上限", "本次卖出上限", "估值修正", "ROE修正", "系统防守仓位",
        }
        metrics = [item for item in metrics if item.get("label") not in trend_only_metric_labels]


    return {
        "action": result.action,
        "action_text": action_text,
        "action_tone": action_tone,
        "action_delta_text": action_delta_text,
        "target_position": result.target_position,
        "target_position_text": pct(result.target_position),
        "confidence": result.confidence,
        "reason": [item for item in result.reason if str(item).strip() and not _is_data_display_reason(str(item).strip())],
        "warnings": result.warnings,
        "matched_rule": result.matched_rule,
        "risk_score": result.risk_score,
        "headline": headline,
        "subline": subline,
        "metrics": metrics,
        "extended_metrics": [
            {"label": "风险分", "value": f"{result.risk_score}/100"},
            {"label": "趋势分", "value": f"{result.trend_score:+d}"},
        ],
    }


# ----------------------------- 数据获取与自动填充 -----------------------------

def _normalize_query(q: str) -> str:
    return re.sub(r"\s+", "", str(q or "").strip().lower())


CN_A_SHARE_CODE_RE = re.compile(r"^(000|001|002|003|300|301|600|601|603|605|688|689|900)\d{3}$")
CN_EXCHANGE_FUND_PREFIXES = ("15", "51", "52", "56", "58")
CN_OPEN_FUND_PREFIXES = (
    "00", "01", "02", "04", "05", "07", "08", "09", "10", "11", "12", "13", "14",
    "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27",
    "31", "32", "40", "48", "50", "53",
)
CN_FUND_NAME_HINTS = ("基金", "QDII", "联接", "LOF", "FOF", "增强", "指数")


def guess_cn_asset_kind(symbol: str, symbol_name: str = "") -> str:
    """根据 6 位国内代码和名称猜测标的类型。

    017641 这类场外基金代码不会出现在股票 K 线接口里；旧逻辑只认 00/16/20/27/51/52/56/58，
    会把 01 开头基金误判成 stock，进而走 Yahoo/东方财富股票 K 线。
    """
    raw = str(symbol or "").strip().upper()
    digits = re.sub(r"\D", "", raw)
    name = str(symbol_name or "").upper()
    if not re.fullmatch(r"\d{6}", digits):
        return "auto"
    if raw in INDEX_CODE_MAP or digits in INDEX_CODE_MAP:
        return "index"
    if digits in FUND_INDEX_MAP:
        return "etf" if digits.startswith(CN_EXCHANGE_FUND_PREFIXES) else "fund"
    if digits.startswith(CN_EXCHANGE_FUND_PREFIXES):
        return "etf"
    if any(hint in name for hint in CN_FUND_NAME_HINTS):
        return "etf" if digits.startswith(CN_EXCHANGE_FUND_PREFIXES) else "fund"
    if CN_A_SHARE_CODE_RE.fullmatch(digits):
        return "stock"
    if digits.startswith(CN_OPEN_FUND_PREFIXES):
        return "fund"
    return "stock"


def resolve_asset_kind(symbol: str, market: str, asset_kind: str = "auto", symbol_name: str = "") -> str:
    """统一修正前端/旧配置传来的 asset_kind。

    重点：如果旧配置里已把 017641 保存成 stock，也要在抓取和回测前自动改回 fund。
    """
    kind = str(asset_kind or "auto").strip().lower()
    market_u = str(market or "auto").strip().upper()
    guessed = guess_cn_asset_kind(symbol, symbol_name)
    is_cn_code = bool(re.fullmatch(r"\d{6}", re.sub(r"\D", "", str(symbol or ""))))
    if (market_u == "CN" or is_cn_code) and guessed != "auto":
        if kind in {"", "auto"}:
            return guessed
        if kind == "stock" and guessed in {"fund", "etf", "index"}:
            return guessed
    return kind or "auto"


def is_cn_open_fund_like(symbol: str, market: str, asset_kind: str = "", symbol_name: str = "") -> bool:
    kind = resolve_asset_kind(symbol, market, asset_kind or "auto", symbol_name)
    market_u = str(market or "auto").strip().upper()
    is_cn_code = bool(re.fullmatch(r"\d{6}", re.sub(r"\D", "", str(symbol or ""))))
    return kind in {"fund", "fof", "qdii", "fund_of_funds"} and (market_u == "CN" or is_cn_code)


def is_danjuan_nav_like(symbol: str, market: str, asset_kind: str = "", symbol_name: str = "") -> bool:
    """蛋卷基金净值接口可尝试的标的：场外基金、ETF、LOF/QDII 等 6 位基金类代码。"""
    kind = resolve_asset_kind(symbol, market, asset_kind or "auto", symbol_name)
    market_u = str(market or "auto").strip().upper()
    digits = re.sub(r"\D", "", str(symbol or ""))
    is_cn_code = bool(re.fullmatch(r"\d{6}", digits))
    return kind in {"fund", "fof", "qdii", "fund_of_funds", "etf"} and (market_u == "CN" or is_cn_code)


def is_danjuan_only_source(source: Any) -> bool:
    return str(source or "").strip().lower() in {"danjuan_only", "only_danjuan", "只使用蛋卷"}


def local_danjuan_search(query: str) -> List[Dict[str, str]]:
    """只使用蛋卷时的轻量搜索：不调用 yfinance/AKShare，只返回本地/代码可确定的蛋卷候选。"""
    q = str(query or "").strip()
    items = []
    for item in local_symbol_search(q):
        symbol = str(item.get("symbol") or "").strip().upper()
        market = str(item.get("market") or ("CN" if re.fullmatch(r"\d{6}", symbol) else "auto")).strip().upper()
        kind = resolve_asset_kind(symbol, market, item.get("asset_kind", "auto"), item.get("name", ""))
        if is_danjuan_nav_like(symbol, market, kind, item.get("name", "")):
            out = dict(item)
            out["symbol"] = symbol
            out["market"] = market
            out["asset_kind"] = kind
            out["source"] = "danjuan_only"
            items.append(out)
    raw = q.upper()
    if re.fullmatch(r"\d{6}", raw) and not any(str(x.get("symbol") or "").upper() == raw for x in items):
        kind = guess_cn_asset_kind(raw)
        if is_danjuan_nav_like(raw, "CN", kind, raw):
            items.append({"symbol": raw, "name": raw, "market": "CN", "asset_kind": kind, "source": "danjuan_only"})
    return items



def _walk_json_nodes(value: Any):
    """递归遍历 JSON 节点，用于兼容蛋卷搜索接口返回字段变化。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_nodes(child)


def _first_text(item: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalise_danjuan_search_item(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    code = _first_text(item, ("fd_code", "fund_code", "code", "symbol", "fundcode", "fundCode"))
    code = re.sub(r"\D", "", code)
    if not re.fullmatch(r"\d{6}", code):
        return None
    name = _first_text(item, ("fd_name", "fund_name", "name", "fname", "fundName", "fd_full_name", "full_name")) or code
    kind = guess_cn_asset_kind(code, name)
    if not is_danjuan_nav_like(code, "CN", kind, name):
        return None
    return {"symbol": code, "name": name, "market": "CN", "asset_kind": kind, "source": "danjuan_only"}


def search_danjuan_funds(query: str, options: Optional[FetchOptions] = None) -> List[Dict[str, str]]:
    """蛋卷搜索。用于“只使用蛋卷”模式下支持中文名称/简称搜索。

    蛋卷 Web 接口字段偶尔会调整，所以这里同时尝试几个常见入口，并用宽松 JSON 解析提取
    fd_code/fund_code/code + fd_name/name 这类基金候选。失败时返回空列表，不回退到其他数据源。
    """
    q = str(query or "").strip()
    if not q:
        return []
    import requests  # type: ignore

    options = options or FetchOptions()
    encoded = quote_plus(q)
    candidate_urls = [
        f"https://danjuanfunds.com/djapi/fund/search?key={encoded}",
        f"https://danjuanfunds.com/djapi/fund/search?keyword={encoded}",
        f"https://danjuanfunds.com/djapi/fund/search?kw={encoded}",
        f"https://danjuanfunds.com/djapi/search/fund?key={encoded}",
    ]
    headers = _danjuan_headers(options)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = "https://danjuanfunds.com/"

    results: List[Dict[str, str]] = []
    seen = set()
    last_error = ""
    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
            text = resp.text or ""
            resp.raise_for_status()
            try:
                payload = resp.json()
            except Exception as exc:
                last_error = f"蛋卷搜索返回不是 JSON：{exc}; preview={text[:120]}"
                continue
            for node in _walk_json_nodes(payload):
                item = _normalise_danjuan_search_item(node)
                if not item:
                    continue
                key = item["symbol"]
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if len(results) >= 20:
                    break
            if results:
                break
        except Exception as exc:
            last_error = str(exc)[:180]
            continue
    # 保持搜索接口容错：在线蛋卷搜索失败时返回本地候选即可，不污染 UI。
    return results


def local_symbol_search(query: str) -> List[Dict[str, str]]:
    q = _normalize_query(query)
    if not q:
        return []

    alias_symbols = set(ALIASES.get(q, []))
    results: List[Dict[str, str]] = []

    for item in LOCAL_SYMBOLS:
        text = _normalize_query(item["symbol"] + item["name"])
        if item["symbol"] in alias_symbols or q in text:
            results.append(item.copy())

    # 如果用户直接输入美股代码或6位国内代码，也直接给候选。
    raw = str(query or "").strip().upper()
    if re.fullmatch(r"[A-Z]{1,6}(\.[A-Z]{1,4})?", raw):
        results.insert(0, {"symbol": raw, "name": raw, "market": "US", "asset_kind": "stock", "source": "yfinance"})
    if re.fullmatch(r"\d{6}", raw):
        known = next((item.copy() for item in LOCAL_SYMBOLS if str(item.get("symbol") or "").upper() == raw), None)
        if known:
            known["asset_kind"] = resolve_asset_kind(raw, known.get("market", "CN"), known.get("asset_kind", "auto"), known.get("name", ""))
            results.insert(0, known)
        else:
            market = "CN"
            source = "akshare"
            kind = guess_cn_asset_kind(raw)
            results.insert(0, {"symbol": raw, "name": raw, "market": market, "asset_kind": kind, "source": source})

    # 去重
    seen = set()
    unique: List[Dict[str, str]] = []
    for item in results:
        key = (item["symbol"], item.get("market", ""), item.get("asset_kind", ""))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:10]


def search_yfinance(query: str) -> List[Dict[str, str]]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    results: List[Dict[str, str]] = []
    try:
        # 新版 yfinance 支持 Search；如果本地版本不支持，会被 except 捕获。
        search_obj = yf.Search(query, max_results=8)
        quotes = getattr(search_obj, "quotes", []) or []
        for q in quotes:
            symbol = q.get("symbol") or q.get("ticker")
            if not symbol:
                continue
            quote_type = str(q.get("quoteType", "stock")).lower()
            kind = "etf" if "etf" in quote_type else ("index" if "index" in quote_type else "stock")
            results.append({
                "symbol": symbol,
                "name": q.get("longname") or q.get("shortname") or symbol,
                "market": "US",
                "asset_kind": kind,
                "source": "yfinance",
            })
    except Exception:
        pass
    return results


def search_akshare(query: str) -> List[Dict[str, str]]:
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return []

    q = str(query or "").strip()
    if not q:
        return []
    results: List[Dict[str, str]] = []

    def add(symbol: Any, name: Any, kind: str):
        if symbol is None:
            return
        s = str(symbol).strip()
        n = str(name or s).strip()
        if q.lower() in s.lower() or q.lower() in n.lower():
            results.append({"symbol": s, "name": n, "market": "CN", "asset_kind": kind, "source": "akshare"})

    # 这些接口会联网，失败就跳过，不影响本地候选。
    try:
        df = ak.stock_info_a_code_name()
        for _, row in df.head(6000).iterrows():
            add(row.get("code") or row.get("证券代码"), row.get("name") or row.get("证券简称"), "stock")
            if len(results) >= 8:
                break
    except Exception:
        pass

    try:
        df = ak.fund_name_em()
        code_col = "基金代码" if "基金代码" in df.columns else df.columns[0]
        name_col = "基金简称" if "基金简称" in df.columns else df.columns[1]
        for _, row in df.head(12000).iterrows():
            add(row.get(code_col), row.get(name_col), "fund")
            if len(results) >= 16:
                break
    except Exception:
        pass

    return results[:10]


def is_placeholder_symbol_candidate(item: Dict[str, str]) -> bool:
    """判断搜索结果是不是"只按代码猜出来"的占位候选。

    例如直接输入 001422 时，本地兜底旧逻辑会先造出
    {symbol: 001422, name: 001422, asset_kind: stock}，但在线基金列表随后能返回
    "景顺长城安享回报混合A"。这种占位项不应该显示在真正命名结果前面，
    否则用户会误点成股票。
    """
    symbol = str(item.get("symbol") or "").strip().upper()
    name = str(item.get("name") or "").strip().upper()
    return not name or bool(symbol and name == symbol)


def symbol_result_rank(item: Dict[str, str], exact_query: str = "") -> Tuple[int, int, int, str]:
    symbol = str(item.get("symbol") or "").strip().upper()
    source = str(item.get("source") or "").strip().lower()
    kind = str(item.get("asset_kind") or "").strip().lower()
    exact_score = 0 if exact_query and symbol == exact_query else 1
    placeholder_score = 1 if is_placeholder_symbol_candidate(item) else 0
    # 精确代码搜索时，基金/ETF/指数优先于股票占位项；真实有名称的股票仍会保留。
    kind_order = {"fund": 0, "fof": 0, "qdii": 0, "etf": 1, "index": 2, "stock": 3, "auto": 4}.get(kind, 5)
    source_order = {"akshare": 0, "danjuan_only": 0, "danjuan": 1, "eastmoney": 2, "yfinance": 3}.get(source, 4)
    return (exact_score, placeholder_score, kind_order + source_order, symbol)


def dedupe_symbols(items: List[Dict[str, str]], query: str = "") -> List[Dict[str, str]]:
    exact_query = str(query or "").strip().upper()

    normalized: List[Dict[str, str]] = []
    for raw in items:
        item = dict(raw or {})
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        item["symbol"] = symbol
        item["name"] = str(item.get("name") or symbol).strip()
        item["market"] = str(item.get("market") or ("CN" if re.fullmatch(r"\d{6}", symbol) else "US")).strip().upper()
        item["asset_kind"] = resolve_asset_kind(symbol, item.get("market", ""), item.get("asset_kind", "auto"), item.get("name", ""))
        item["source"] = str(item.get("source") or "auto").strip()
        normalized.append(item)

    # 如果同一个代码已经有真实名称结果，删除 name == symbol 的占位结果。
    symbols_with_named_result = {
        str(item.get("symbol") or "").upper()
        for item in normalized
        if not is_placeholder_symbol_candidate(item)
    }

    seen = set()
    out: List[Dict[str, str]] = []
    for item in sorted(normalized, key=lambda x: symbol_result_rank(x, exact_query)):
        symbol = str(item.get("symbol") or "").upper()
        if symbol in symbols_with_named_result and is_placeholder_symbol_candidate(item):
            continue
        key = (symbol, item.get("market"), item.get("asset_kind"), item.get("source"), item.get("name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def pandas_to_records(df: Any) -> List[Dict[str, float]]:
    import pandas as pd  # type: ignore

    if df is None or len(df) == 0:
        return []
    data = df.copy()
    # 扁平化 yfinance 可能返回的 MultiIndex。
    if hasattr(data.columns, "levels"):
        data.columns = [str(c[0]).lower() for c in data.columns]
    data.columns = [str(c).strip().lower() for c in data.columns]

    rename_map = {
        "open": "open", "开盘": "open",
        "high": "high", "最高": "high",
        "low": "low", "最低": "low",
        "close": "close", "收盘": "close", "单位净值": "close", "累计净值": "close",
        "volume": "volume", "成交量": "volume",
        "amount": "amount", "成交额": "amount",
    }
    new_cols = {}
    for col in data.columns:
        if col in rename_map:
            new_cols[col] = rename_map[col]
    data = data.rename(columns=new_cols)

    if "date" not in data.columns:
        # AKShare 常见日期列
        for c in ["日期", "净值日期", "trade_date", "datetime"]:
            if c.lower() in data.columns:
                data = data.rename(columns={c.lower(): "date"})
                break
    if "date" not in data.columns:
        data = data.reset_index().rename(columns={"index": "date", "date": "date"})

    need = ["date", "open", "high", "low", "close", "volume", "amount"]
    for col in need:
        if col not in data.columns:
            data[col] = 0.0

    for col in ["open", "high", "low", "close", "volume", "amount"]:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    data = data.dropna(subset=["close"]).tail(260)
    out: List[Dict[str, float]] = []
    for _, row in data.iterrows():
        out.append({
            "date": str(row.get("date"))[:10],
            "open": float(row.get("open") or row.get("close") or 0),
            "high": float(row.get("high") or row.get("close") or 0),
            "low": float(row.get("low") or row.get("close") or 0),
            "close": float(row.get("close") or 0),
            "volume": float(row.get("volume") or 0),
            "amount": float(row.get("amount") or 0),
        })
    return out



@dataclass
class FetchOptions:
    proxy_mode: str = "system"
    proxy_url: str = ""
    timeout: float = 12.0
    retry_count: int = 2
    danjuan_cookie: str = ""
    valuation_method: str = "system_calc"


def fetch_options_from_cfg(cfg: Dict[str, Any]) -> FetchOptions:
    mode = str(cfg.get("proxy_mode", "system") or "system")
    if mode not in {"system", "custom", "none"}:
        mode = "system"
    return FetchOptions(
        proxy_mode=mode,
        proxy_url=str(cfg.get("proxy_url", "") or "").strip(),
        timeout=clamp(as_float(cfg.get("request_timeout_sec"), 12.0), 3.0, 60.0),
        retry_count=int(clamp(as_float(cfg.get("retry_count"), 2.0), 0.0, 5.0)),
        danjuan_cookie=str(cfg.get("danjuan_cookie", "") or "").strip(),
        valuation_method=str(cfg.get("valuation_method", "system_calc") or "system_calc"),
    )


def request_proxies(options: FetchOptions) -> Optional[Dict[str, str]]:
    """requests 的代理配置：None=跟随系统环境；{}=明确不用代理。"""
    if options.proxy_mode == "custom" and options.proxy_url:
        return {"http": options.proxy_url, "https": options.proxy_url}
    if options.proxy_mode == "none":
        return {}
    return None


@contextlib.contextmanager
def proxy_environment(options: FetchOptions):
    """给 yfinance / akshare 这类内部请求库提供临时代理环境。"""
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
    old = {k: os.environ.get(k) for k in keys}
    try:
        if options.proxy_mode == "custom" and options.proxy_url:
            os.environ["HTTP_PROXY"] = options.proxy_url
            os.environ["HTTPS_PROXY"] = options.proxy_url
            os.environ["http_proxy"] = options.proxy_url
            os.environ["https_proxy"] = options.proxy_url
        elif options.proxy_mode == "none":
            for k in keys:
                os.environ.pop(k, None)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def retry_call(label: str, fn: Callable[[], Tuple[List[Dict[str, float]], Dict[str, Any]]], options: FetchOptions) -> Tuple[List[Dict[str, float]], Dict[str, Any], List[Dict[str, Any]]]:
    trace: List[Dict[str, Any]] = []
    last_error = ""
    total = max(1, options.retry_count + 1)
    for i in range(total):
        started = time.time()
        try:
            records, fundamentals = fn()
            if len(records) < 60:
                raise ValueError(f"行情数据太少：{len(records)} 条")
            trace.append({
                "source": label,
                "attempt": i + 1,
                "ok": True,
                "rows": len(records),
                "elapsed_ms": int((time.time() - started) * 1000),
            })
            return records, fundamentals, trace
        except Exception as e:
            last_error = str(e)
            trace.append({
                "source": label,
                "attempt": i + 1,
                "ok": False,
                "error": last_error[:240],
                "elapsed_ms": int((time.time() - started) * 1000),
            })
            if i < total - 1:
                time.sleep(min(0.35 * (i + 1), 1.2))
    raise RuntimeError(f"{label} 获取失败：{last_error}")


def yahoo_symbol_candidates(symbol: str, market: str, asset_kind: str) -> List[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    if market == "CN" or re.fullmatch(r"\d{6}", raw):
        # 上海：股票6开头、场内基金/ETF多数5开头；深圳：0/1/2/3开头。
        if raw.startswith(("5", "6", "9")):
            return [f"{raw}.SS"]
        return [f"{raw}.SZ", f"{raw}.SS"]
    return [raw]




def eastmoney_secid(symbol: str) -> str:
    """东方财富 secid：沪市多数为 1，深市多数为 0。

    注意：常见中证/上证指数代码 000300、000016、000905、000852、000688
    虽然以 0 开头，但东方财富 secid 应使用 1.xxxxxx。
    """
    s = re.sub(r"\D", "", str(symbol or ""))
    if not re.fullmatch(r"\d{6}", s):
        s = "510300"
    prefix = "1" if s.startswith(("5", "6", "9", "000")) else "0"
    return f"{prefix}.{s}"


def eastmoney_headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://quote.eastmoney.com/",
        "Connection": "keep-alive",
    }


def fetch_eastmoney_kline(symbol: str, options: FetchOptions) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    """东方财富历史 K 线 HTTP 兜底链路，主要给国内股票/ETF/场内基金使用。"""
    import requests  # type: ignore

    secid = eastmoney_secid(symbol)
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "1",
        "beg": "20200101",
        "end": "29991231",
        "lmt": "1000",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    last_error = ""
    for base_url in [
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "http://push2his.eastmoney.com/api/qt/stock/kline/get",
    ]:
        try:
            resp = requests.get(base_url, params=params, headers=eastmoney_headers(), timeout=options.timeout, proxies=request_proxies(options))
            resp.raise_for_status()
            payload = resp.json() if resp.text else {}
            data = payload.get("data") if isinstance(payload, dict) else None
            klines = (data or {}).get("klines") or []
            if not klines:
                raise ValueError(f"东方财富无K线：status={resp.status_code}")
            records: List[Dict[str, float]] = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 6:
                    continue
                records.append({
                    "date": parts[0],
                    "open": as_float(parts[1]),
                    "close": as_float(parts[2]),
                    "high": as_float(parts[3]),
                    "low": as_float(parts[4]),
                    "volume": as_float(parts[5]),
                    "amount": as_float(parts[6]) if len(parts) > 6 else 0.0,
                })
            return records[-260:], {"long_name": (data or {}).get("name"), "eastmoney_secid": secid, "eastmoney_url": resp.url}
        except Exception as e:
            last_error = str(e)
            continue
    raise RuntimeError(last_error or "东方财富K线获取失败")


def fetch_danjuan_fund_detail(symbol: str, options: Optional[FetchOptions] = None) -> Dict[str, Any]:
    """蛋卷/雪球基金详情：用于校验 017641 这类场外基金身份，并补充基金名称/净值/规模等信息。"""
    import requests  # type: ignore

    options = options or FetchOptions()
    code = re.sub(r"\D", "", str(symbol or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("蛋卷基金详情仅支持 6 位基金代码")
    url = f"https://danjuanfunds.com/djapi/fund/{code}"
    headers = _danjuan_headers(options)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = f"https://danjuanfunds.com/fund/{code}"
    resp = requests.get(url, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    text = resp.text or ""
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as e:
        raise ValueError(f"蛋卷基金详情返回不是 JSON：{e}; preview={text[:180]}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("fd_code"):
        raise ValueError(f"蛋卷未识别该基金代码：{code}")
    return data


def danjuan_fund_fundamentals(symbol: str, options: Optional[FetchOptions] = None) -> Dict[str, Any]:
    data = fetch_danjuan_fund_detail(symbol, options)
    derived = data.get("fund_derived") or {}
    fundamentals: Dict[str, Any] = {
        "long_name": data.get("fd_name") or data.get("fd_full_name") or symbol,
        "fund_full_name": data.get("fd_full_name"),
        "fund_type": data.get("type_desc"),
        "fund_manager": data.get("manager_name"),
        "fund_size": data.get("totshare"),
        "latest_nav": _normalise_number(derived.get("unit_nav")),
        "latest_nav_date": derived.get("end_date"),
        "nav_growth_day_pct": _normalise_number(derived.get("nav_grtd")),
        "nav_growth_1y_pct": _normalise_number(derived.get("nav_grl1y")),
        "nav_growth_base_pct": _normalise_number(derived.get("nav_grbase")),
        "fund_source": "danjuan_fund_detail",
    }
    for item in data.get("sec_header_base_data") or []:
        if not isinstance(item, dict):
            continue
        if item.get("data_name") == "最大回撤" and item.get("data_value_number") is not None:
            fundamentals["max_drawdown"] = item.get("data_value_number")
    return {k: v for k, v in fundamentals.items() if v is not None and v != ""}


def _danjuan_nav_items_to_records(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        d = item.get("date") or item.get("FSRQ") or item.get("净值日期")
        nav = item.get("nav") or item.get("value") or item.get("DWJZ") or item.get("单位净值")
        try:
            ds = parse_date_safe(d).isoformat()
            close = as_float(nav, 0.0)
            if close <= 0:
                continue
            records.append({"date": ds, "open": close, "high": close, "low": close, "close": close, "volume": 0.0})
        except Exception:
            continue
    return normalize_history_records(records)


def backtest_fetch_danjuan_fund_nav(symbol: str, start: date, end: date, options: FetchOptions) -> Tuple[List[Dict[str, Any]], str]:
    """蛋卷/雪球基金历史净值分页接口。

    /djapi/fund/nav/history/{code} 返回的是场外基金净值，不是股票 K 线；
    回测时用净值同步填充 OHLC，volume=0。
    """
    import requests  # type: ignore

    code = re.sub(r"\D", "", str(symbol or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError("蛋卷基金历史净值仅支持 6 位基金代码")
    headers = _danjuan_headers(options)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = f"https://danjuanfunds.com/fund/{code}"
    all_items: List[Dict[str, Any]] = []
    seen_dates: set = set()
    total_pages: Optional[int] = None
    url = f"https://danjuanfunds.com/djapi/fund/nav/history/{code}"
    for page in range(1, 501):
        params = {"page": page, "size": 20}
        resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
        text = resp.text or ""
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception as e:
            raise RuntimeError(f"蛋卷基金历史净值返回不是 JSON：{e}; preview={text[:180]}")
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data or {}
        items = data.get("items") or []
        if not items:
            break
        try:
            total_pages = int(data.get("total_pages") or total_pages or 0) or total_pages
        except Exception:
            pass
        page_new = 0
        oldest: Optional[date] = None
        for item in items:
            try:
                dd = parse_date_safe(item.get("date") or item.get("FSRQ") or item.get("净值日期"))
            except Exception:
                continue
            oldest = dd if oldest is None else min(oldest, dd)
            if dd > end:
                continue
            key = dd.isoformat()
            if key in seen_dates:
                continue
            seen_dates.add(key)
            page_new += 1
            if dd >= start:
                all_items.append(item)
        if total_pages and page >= total_pages:
            break
        if oldest is not None and oldest < start:
            break
        if page_new <= 0 and page > 1:
            break
    records = _danjuan_nav_items_to_records(all_items)
    if not records:
        raise RuntimeError("蛋卷基金历史净值返回空数据")
    return records, "danjuan_fund_nav"


def fetch_danjuan_fund_nav_recent(symbol: str, options: Optional[FetchOptions] = None) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    options = options or FetchOptions()
    end = date.today()
    start = end - timedelta(days=1100)
    records, _ = backtest_fetch_danjuan_fund_nav(symbol, start, end, options)
    fundamentals = danjuan_fund_fundamentals(symbol, options)
    return records[-260:], fundamentals


def yfinance_download_with_fallback(yf: Any, symbol: str, options: FetchOptions) -> Any:
    """兼容新版 yfinance MultiIndex/空表问题：download 失败时改用 Ticker.history。"""
    kwargs = dict(
        period="2y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=options.timeout,
    )
    try:
        hist = yf.download(symbol, multi_level_index=False, **kwargs)
    except TypeError:
        hist = yf.download(symbol, **kwargs)
    if hist is not None and len(hist) > 0:
        return hist
    return yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)

def fetch_yahoo_chart_api(symbol: str, options: FetchOptions) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    import requests  # type: ignore

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2y", "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
    headers = {"User-Agent": "Mozilla/5.0 trend-risk-position-tool"}
    resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    resp.raise_for_status()
    payload = resp.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        err = ((payload.get("chart") or {}).get("error") or {}).get("description") or "Yahoo无返回"
        raise ValueError(err)
    ts = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = adj or (quote.get("close") or [])
    volumes = quote.get("volume") or []
    records: List[Dict[str, float]] = []
    for idx, t in enumerate(ts):
        c = closes[idx] if idx < len(closes) else None
        if c is None:
            continue
        from datetime import timezone
        date = datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d")
        records.append({
            "date": date,
            "open": float(opens[idx] if idx < len(opens) and opens[idx] is not None else c),
            "high": float(highs[idx] if idx < len(highs) and highs[idx] is not None else c),
            "low": float(lows[idx] if idx < len(lows) and lows[idx] is not None else c),
            "close": float(c),
            "volume": float(volumes[idx] if idx < len(volumes) and volumes[idx] is not None else 0),
            "amount": 0.0,
        })
    meta = result.get("meta") or {}
    return records[-260:], {"currency": meta.get("currency"), "long_name": meta.get("symbol")}


def fetch_stooq_daily(symbol: str, options: FetchOptions) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    import pandas as pd  # type: ignore
    import requests  # type: ignore

    raw = str(symbol or "").strip().lower()
    if "." not in raw:
        raw = f"{raw}.us"
    url = "https://stooq.com/q/d/l/"
    params = {"s": raw, "i": "d"}
    headers = {"User-Agent": "Mozilla/5.0 trend-risk-position-tool"}
    resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or "No data" in text or len(text.splitlines()) < 30:
        raise ValueError("Stooq无有效数据")
    df = pd.read_csv(StringIO(text))
    records = pandas_to_records(df)
    return records, {"long_name": symbol}


def fetch_yfinance(symbol: str, options: Optional[FetchOptions] = None) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    import yfinance as yf  # type: ignore

    options = options or FetchOptions()
    with proxy_environment(options):
        hist = yfinance_download_with_fallback(yf, symbol, options)
        records = pandas_to_records(hist)
        fundamentals: Dict[str, Any] = {}
        try:
            info = yf.Ticker(symbol).get_info()
            fundamentals["current_pe"] = info.get("trailingPE") or info.get("forwardPE")
            roe = info.get("returnOnEquity")
            fundamentals["roe_pct"] = float(roe) * 100 if roe is not None else None
            fundamentals["currency"] = info.get("currency")
            fundamentals["long_name"] = info.get("longName") or info.get("shortName")
        except Exception:
            pass
    return records, fundamentals


def fetch_akshare(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    s = re.sub(r"\D", "", symbol)
    records: List[Dict[str, float]] = []
    fundamentals: Dict[str, Any] = {}

    with proxy_environment(options):
        if asset_kind == "index":
            try:
                df = ak.index_zh_a_hist(symbol=s, period="daily", start_date="20200101", end_date=datetime.now().strftime("%Y%m%d"))
                records = pandas_to_records(df)
            except Exception:
                pass
        elif asset_kind == "fund":
            # ETF/场内基金优先用ETF行情；开放基金退化为净值曲线。
            try:
                df = ak.fund_etf_hist_em(symbol=s, period="daily", adjust="qfq")
                records = pandas_to_records(df)
            except Exception:
                try:
                    df = ak.fund_open_fund_info_em(symbol=s, indicator="单位净值走势")
                    records = pandas_to_records(df)
                except Exception:
                    pass
        else:
            try:
                df = ak.stock_zh_a_hist(symbol=s, period="daily", adjust="qfq")
                records = pandas_to_records(df)
            except Exception:
                pass

        # A股个股：优先用 AKShare / 乐咕历史估值自算 PE/PB 百分位；失败不影响行情。
        try:
            extra = fetch_akshare_lg_valuation(symbol=s, options=options)
            fundamentals = merge_missing_fundamentals(fundamentals, extra)
        except Exception:
            pass

    return records, fundamentals




def _norm_index_code(symbol: str, asset_kind: str = "", symbol_name: str = "") -> Optional[str]:
    raw = str(symbol or "").strip().upper()
    digits = re.sub(r"\D", "", raw)
    name = str(symbol_name or "").upper()

    if raw in INDEX_CODE_MAP:
        return INDEX_CODE_MAP[raw]
    if digits in FUND_INDEX_MAP:
        return FUND_INDEX_MAP[digits]
    if digits in INDEX_CODE_MAP:
        return INDEX_CODE_MAP[digits]

    text = f"{raw} {name}"
    for rule in INDEX_KEYWORD_RULES:
        keys = rule.get("keywords") or []
        code = str(rule.get("index_code") or "").strip().upper()
        if code and any(str(k).upper() in text for k in keys):
            return code

    if str(asset_kind or "").lower() == "index" and re.fullmatch(r"\d{6}", digits):
        if digits.startswith("399"):
            return f"SZ{digits}"
        if digits.startswith("000"):
            return f"SH{digits}"
    return None


def _column_by_names(columns: List[Any], exact_names: set[str], contains_any: Tuple[str, ...] = (), exclude_any: Tuple[str, ...] = ()) -> Optional[Any]:
    for col in columns:
        text = str(col).strip().lower().replace(" ", "").replace("_", "")
        if any(x in text for x in exclude_any):
            continue
        if text in exact_names:
            return col
    for col in columns:
        text = str(col).strip().lower().replace(" ", "").replace("_", "")
        if any(x in text for x in exclude_any):
            continue
        if contains_any and any(x in text for x in contains_any):
            return col
    return None


def _normalize_percentile_value(value: Any) -> Optional[float]:
    v = as_float(value, float("nan"))
    if math.isnan(v) or math.isinf(v):
        return None
    # 有些源用 0~1 表示百分位。
    if 0 <= v <= 1:
        v *= 100
    return round(clamp(v, 0.0, 100.0), 2)


def extract_valuation_from_df(df: Any, source_label: str) -> Dict[str, Any]:
    """从不同来源的估值表里尽量解析 PE/PB/ROE 与百分位。

    优先使用源内自带百分位；没有百分位时，用历史 PE/PB 序列自行计算当前值所在百分位。
    """
    import pandas as pd  # type: ignore

    if df is None or getattr(df, "empty", True):
        raise ValueError("估值表为空")
    cols = list(df.columns)
    result: Dict[str, Any] = {"valuation_source": source_label}

    pe_pct_col = _column_by_names(cols, {"pe百分位", "pe分位", "pepercentile", "pepercentilettm", "pe分位点"}, ("pe百分位", "pe分位", "pepercentile"))
    pb_pct_col = _column_by_names(cols, {"pb百分位", "pb分位", "pbpercentile", "pb分位点"}, ("pb百分位", "pb分位", "pbpercentile"))
    pe_col = _column_by_names(cols, {"pe", "pettm", "pettm", "市盈率", "市盈率ttm", "滚动市盈率"}, ("市盈率", "pe"), ("百分位", "分位", "percentile"))
    pb_col = _column_by_names(cols, {"pb", "市净率"}, ("市净率", "pb"), ("百分位", "分位", "percentile"))
    roe_col = _column_by_names(cols, {"roe", "净资产收益率", "roe加权"}, ("roe", "净资产收益率"), ("百分位", "分位", "percentile"))

    def last_valid(col: Any) -> Optional[float]:
        if col is None:
            return None
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            return None
        return float(series.iloc[-1])

    if pe_pct_col is not None:
        result["pe_percentile"] = _normalize_percentile_value(last_valid(pe_pct_col))
    if pb_pct_col is not None:
        result["pb_percentile"] = _normalize_percentile_value(last_valid(pb_pct_col))

    if pe_col is not None:
        pe_series = pd.to_numeric(df[pe_col], errors="coerce").dropna()
        pe_series = pe_series[pe_series > 0]
        if len(pe_series) >= 1:
            current = float(pe_series.iloc[-1])
            result["current_pe"] = round(current, 4)
            if result.get("pe_percentile") is None and len(pe_series) >= 30:
                result["pe_percentile"] = round(float((pe_series <= current).mean() * 100), 2)

    if pb_col is not None:
        pb_series = pd.to_numeric(df[pb_col], errors="coerce").dropna()
        pb_series = pb_series[pb_series > 0]
        if len(pb_series) >= 1:
            current_pb = float(pb_series.iloc[-1])
            result["current_pb"] = round(current_pb, 4)
            if result.get("pb_percentile") is None and len(pb_series) >= 30:
                result["pb_percentile"] = round(float((pb_series <= current_pb).mean() * 100), 2)

    if roe_col is not None:
        roe = last_valid(roe_col)
        if roe is not None:
            if -1 <= roe <= 1:
                roe *= 100
            result["roe_pct"] = round(float(roe), 2)

    clean = {k: v for k, v in result.items() if v is not None}
    if not any(k in clean for k in ("pe_percentile", "pb_percentile", "current_pe", "roe_pct")):
        raise ValueError("未解析到 PE/PB/ROE 字段")
    return clean




def _date_column(cols: List[Any]) -> Optional[Any]:
    return _column_by_names(
        cols,
        {"日期", "date", "trade_date", "tradedate", "时间", "统计日期"},
        ("日期", "date", "trade"),
    )


def _as_date_string(value: Any) -> Optional[str]:
    """尽量把不同数据源的日期值标准化为 YYYY-MM-DD。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("/", "-").replace(".", "-")
    try:
        # 兼容 20241001 / 2024-10-01 / pandas Timestamp
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except Exception:
        return None


def _rolling_percentiles_by_date(items: List[Tuple[str, Optional[float]]], years: int = 10, min_count: int = 30) -> Dict[str, Optional[float]]:
    """只使用当日及以前数据计算百分位，避免回测未来函数。

    近似蛋卷常见的"近10年百分位"：若10年窗口数据不足 min_count，则退化为当日前扩展窗口。
    """
    parsed: List[Tuple[date, str, float]] = []
    for ds, val in items:
        if val is None or val <= 0 or math.isnan(float(val)) or math.isinf(float(val)):
            continue
        try:
            parsed.append((datetime.fromisoformat(ds).date(), ds, float(val)))
        except Exception:
            continue
    parsed.sort(key=lambda x: x[0])
    out: Dict[str, Optional[float]] = {}
    all_past: List[Tuple[date, float]] = []
    for d, ds, val in parsed:
        all_past.append((d, val))
        window_start = d - timedelta(days=int(years * 365.25))
        window = [v for dt0, v in all_past if dt0 >= window_start]
        hist = window if len(window) >= min_count else [v for _, v in all_past]
        if len(hist) < max(5, min_count // 3):
            out[ds] = None
        else:
            out[ds] = round(float(sum(x <= val for x in hist) / len(hist) * 100), 2)
    return out


def extract_valuation_series_from_df(df: Any, source_label: str) -> Dict[str, Dict[str, Any]]:
    """从估值历史表生成逐日估值序列。

    返回：{YYYY-MM-DD: {current_pe, pe_percentile, current_pb, pb_percentile, roe_pct, valuation_source}}
    百分位优先使用源自带字段；没有时按当日以前历史自算，避免未来函数。
    """
    import pandas as pd  # type: ignore

    if df is None or getattr(df, "empty", True):
        raise ValueError("历史估值表为空")
    cols = list(df.columns)
    date_col = _date_column(cols)
    if date_col is None:
        raise ValueError("历史估值表缺少日期列")

    pe_pct_col = _column_by_names(cols, {"pe百分位", "pe分位", "pepercentile", "pepercentilettm", "pe分位点"}, ("pe百分位", "pe分位", "pepercentile"))
    pb_pct_col = _column_by_names(cols, {"pb百分位", "pb分位", "pbpercentile", "pb分位点"}, ("pb百分位", "pb分位", "pbpercentile"))
    pe_col = _column_by_names(cols, {"pe", "pettm", "市盈率", "市盈率ttm", "滚动市盈率"}, ("市盈率", "pe"), ("百分位", "分位", "percentile"))
    pb_col = _column_by_names(cols, {"pb", "市净率"}, ("市净率", "pb"), ("百分位", "分位", "percentile"))
    roe_col = _column_by_names(cols, {"roe", "净资产收益率", "roe加权"}, ("roe", "净资产收益率"), ("百分位", "分位", "percentile"))

    rows: List[Tuple[str, int]] = []
    for idx, raw in enumerate(df[date_col].tolist()):
        ds = _as_date_string(raw)
        if ds:
            rows.append((ds, idx))
    rows.sort(key=lambda x: x[0])
    if not rows:
        raise ValueError("历史估值表日期无法解析")

    def numeric_at(col: Any, idx: int) -> Optional[float]:
        if col is None:
            return None
        try:
            v = pd.to_numeric(df[col], errors="coerce").iloc[idx]
            if pd.isna(v):
                return None
            v = float(v)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        except Exception:
            return None

    pe_items = [(ds, numeric_at(pe_col, idx)) for ds, idx in rows]
    pb_items = [(ds, numeric_at(pb_col, idx)) for ds, idx in rows]
    pe_pct_calc = _rolling_percentiles_by_date(pe_items)
    pb_pct_calc = _rolling_percentiles_by_date(pb_items)

    series: Dict[str, Dict[str, Any]] = {}
    for ds, idx in rows:
        item: Dict[str, Any] = {"valuation_source": source_label, "valuation_note": "历史估值序列"}
        pe_v = numeric_at(pe_col, idx)
        pb_v = numeric_at(pb_col, idx)
        roe_v = numeric_at(roe_col, idx)
        if pe_v is not None and pe_v > 0:
            item["current_pe"] = round(pe_v, 4)
        if pb_v is not None and pb_v > 0:
            item["current_pb"] = round(pb_v, 4)
        if pe_pct_col is not None:
            item["pe_percentile"] = _normalize_percentile_value(numeric_at(pe_pct_col, idx))
        if item.get("pe_percentile") is None:
            item["pe_percentile"] = pe_pct_calc.get(ds)
        if pb_pct_col is not None:
            item["pb_percentile"] = _normalize_percentile_value(numeric_at(pb_pct_col, idx))
        if item.get("pb_percentile") is None:
            item["pb_percentile"] = pb_pct_calc.get(ds)
        if roe_v is not None:
            if -1 <= roe_v <= 1 and roe_v != 0:
                roe_v *= 100
            item["roe_pct"] = round(roe_v, 2)
        clean = {k: v for k, v in item.items() if v is not None}
        if any(k in clean for k in ("pe_percentile", "pb_percentile", "current_pe", "current_pb", "roe_pct")):
            series[ds] = clean
    if not series:
        raise ValueError("历史估值序列没有有效 PE/PB/ROE 字段")
    return series


def merge_valuation_series(base: Dict[str, Dict[str, Any]], extra: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    merged = {k: dict(v) for k, v in (base or {}).items()}
    for ds, item in (extra or {}).items():
        cur = merged.setdefault(ds, {})
        for k, v in item.items():
            if cur.get(k) in (None, "") and v not in (None, ""):
                cur[k] = v
    return merged


def recompute_history_percentiles(series: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """对已合并的 PE/PB 历史序列重新计算滚动百分位。

    用途：蛋卷 pe_history/pb_history 可能是图表采样点，而 detail 接口会更快给出
    最新交易日数据。把 detail 最新点补进序列后，必须重新计算百分位，避免
    "估值序列最新日期"落后于页面最新日期。
    """
    if not series:
        return series
    keys = sorted(series.keys())

    def safe_num(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            v = float(value)
            if math.isnan(v) or math.isinf(v) or v <= 0:
                return None
            return v
        except Exception:
            return None

    pe_items = [(ds, safe_num((series.get(ds) or {}).get("current_pe"))) for ds in keys]
    pb_items = [(ds, safe_num((series.get(ds) or {}).get("current_pb"))) for ds in keys]
    pe_pct = _rolling_percentiles_by_date(pe_items)
    pb_pct = _rolling_percentiles_by_date(pb_items)

    out = {k: dict(v) for k, v in series.items()}
    for ds in keys:
        if pe_pct.get(ds) is not None:
            out.setdefault(ds, {})["pe_percentile"] = pe_pct[ds]
        if pb_pct.get(ds) is not None:
            out.setdefault(ds, {})["pb_percentile"] = pb_pct[ds]
    return out


def merge_danjuan_detail_current_into_history_series(
    series: Dict[str, Dict[str, Any]],
    symbol: str,
    asset_kind: str,
    options: FetchOptions,
    symbol_name: str = "",
) -> Dict[str, Dict[str, Any]]:
    """把蛋卷 detail 当前页面最新点补进历史估值序列。

    pe_history/pb_history/roe_history 主要用于回测历史曲线，但它们可能比 detail
    当前页面慢一个交易日。detail 里的 ts/date 是页面最新估值日期；当它晚于历史
    曲线最后一日时，追加该点并重新自算 PE/PB 百分位。
    """
    if not series:
        return series
    try:
        detail = fetch_danjuan_detail_valuation(symbol, asset_kind, options, symbol_name)
    except Exception:
        return series
    ds = _as_date_string(detail.get("valuation_date") or detail.get("date"))
    if not ds:
        return series

    merged = {k: dict(v) for k, v in series.items()}
    item = merged.setdefault(ds, {})
    changed = False
    for key in ("current_pe", "current_pb", "roe_pct"):
        if detail.get(key) is not None:
            item[key] = detail.get(key)
            changed = True
    if changed:
        item["valuation_source"] = "danjuan_detail_current+history"
        item["valuation_note"] = "蛋卷历史序列补入 detail 最新估值点后自算百分位"
        item["valuation_page_date"] = ds
        item["valuation_page_pe_percentile"] = detail.get("pe_percentile")
        item["valuation_page_pb_percentile"] = detail.get("pb_percentile")
        merged = recompute_history_percentiles(merged)
    return merged


def _validate_history_percentile_series(series: Dict[str, Dict[str, Any]], label: str, min_rows: int = 30) -> Dict[str, Dict[str, Any]]:
    """系统自算 PE 百分位必须拿到真正的历史序列。

    只返回 1 行的"当前估值"不能用于系统自算百分位；这种情况应当明确失败，
    避免日志显示成功但 PE 百分位仍为空。
    """
    rows = len(series or {})
    pe_rows = sum(1 for v in (series or {}).values() if v.get("current_pe") is not None)
    pe_pct_rows = sum(1 for v in (series or {}).values() if v.get("pe_percentile") is not None)
    if rows < min_rows or pe_pct_rows <= 0:
        raise ValueError(f"{label} 历史估值序列不足：{rows} 条，PE有效点 {pe_rows} 条，PE百分位点 {pe_pct_rows} 条；系统自算至少需要 {min_rows} 条历史估值")
    return series




def _diag_text(value: Any, limit: int = 260) -> str:
    """用于数据源日志的短文本，避免前端被超长错误刷屏。"""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _df_debug_summary(df: Any) -> str:
    """返回 DataFrame 的行列概况，专门用于 AKShare 历史估值诊断。"""
    try:
        rows = len(df) if df is not None else 0
    except Exception:
        rows = -1
    try:
        cols = [str(c) for c in list(getattr(df, "columns", []))]
    except Exception:
        cols = []
    preview_cols = ",".join(cols[:12]) if cols else "无列名"
    if len(cols) > 12:
        preview_cols += f",...共{len(cols)}列"
    return f"返回{rows}行，列=[{preview_cols}]"


def _valuation_series_debug_summary(seq: Dict[str, Dict[str, Any]]) -> str:
    rows = len(seq or {})
    pe_rows = sum(1 for v in (seq or {}).values() if v.get("current_pe") is not None)
    pe_pct_rows = sum(1 for v in (seq or {}).values() if v.get("pe_percentile") is not None)
    pb_rows = sum(1 for v in (seq or {}).values() if v.get("current_pb") is not None)
    pb_pct_rows = sum(1 for v in (seq or {}).values() if v.get("pb_percentile") is not None)
    sample_dates = list((seq or {}).keys())[:3]
    return f"序列{rows}条，PE有效{pe_rows}条，PE百分位{pe_pct_rows}条，PB有效{pb_rows}条，PB百分位{pb_pct_rows}条，样例日期={sample_dates}"


LEGULEGU_INDEX_PAGE_MAP: Dict[str, str] = {
    "SH000300": "hs300-ttm-lyr",   # 沪深300
    "SH000016": "sz50-ttm-lyr",    # 上证50
    "SH000010": "sz180-ttm-lyr",   # 上证180
    "SH000905": "zz500-ttm-lyr",   # 中证500
    "SH000906": "zz800-ttm-lyr",   # 中证800
    "SH000852": "zz1000-ttm-lyr",  # 中证1000；若乐咕页面无此 slug，会在诊断中提示
    "SZ399330": "sz399330-ttm-lyr", # 深证100
    "SZ399303": "gz2000-ttm-lyr",  # 国证2000
    "SZ399673": "sz399673-ttm-lyr", # 创业板50
}

# AKShare 当前版本可用的乐咕指数历史 PE 接口：ak.stock_index_pe_lg。
# 注意它不覆盖所有指数；例如科创50不在该接口支持列表里。
AK_STOCK_INDEX_PE_LG_SYMBOL_MAP: Dict[str, str] = {
    "SH000016": "上证50",
    "SH000300": "沪深300",
    "SH000905": "中证500",
    "SH000852": "中证1000",
    "SH000010": "上证180",
    "SH000009": "上证380",
    "SZ399330": "深证100",
    "SH000922": "中证红利",  # 若接口不支持会在诊断中失败，不影响其他链路。
}



def _legulegu_slugs_for_index(code: str, name: str, digits: str) -> List[str]:
    """给系统自算历史 PE 百分位准备乐咕页面候选。

    AKShare 1.15.52 之后已移除 funddb 相关接口，当前版本不再有
    index_value_hist_funddb。这里直接访问乐咕公开页面并自行解析历史 PE，
    作为"系统自算"而非蛋卷估值。
    """
    slugs: List[str] = []
    if code in LEGULEGU_INDEX_PAGE_MAP:
        slugs.append(LEGULEGU_INDEX_PAGE_MAP[code])
    if digits == "000300" and "hs300-ttm-lyr" not in slugs:
        slugs.append("hs300-ttm-lyr")
    if digits == "000016" and "sz50-ttm-lyr" not in slugs:
        slugs.append("sz50-ttm-lyr")
    if digits == "000905" and "zz500-ttm-lyr" not in slugs:
        slugs.append("zz500-ttm-lyr")
    if digits == "399330" and "sz399330-ttm-lyr" not in slugs:
        slugs.append("sz399330-ttm-lyr")
    if digits == "399303" and "gz2000-ttm-lyr" not in slugs:
        slugs.append("gz2000-ttm-lyr")
    # 名称兜底：只加明确能从公开搜索确认的常见页面。
    if "沪深300" in name and "hs300-ttm-lyr" not in slugs:
        slugs.append("hs300-ttm-lyr")
    if "上证50" in name and "sz50-ttm-lyr" not in slugs:
        slugs.append("sz50-ttm-lyr")
    if "中证500" in name and "zz500-ttm-lyr" not in slugs:
        slugs.append("zz500-ttm-lyr")
    if "中证800" in name and "zz800-ttm-lyr" not in slugs:
        slugs.append("zz800-ttm-lyr")
    if "深证100" in name and "sz399330-ttm-lyr" not in slugs:
        slugs.append("sz399330-ttm-lyr")
    if "国证2000" in name and "gz2000-ttm-lyr" not in slugs:
        slugs.append("gz2000-ttm-lyr")
    return slugs


def _legulegu_headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 trend-risk-position-tool",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://legulegu.com/",
    }


def _pick_legulegu_pe_from_numbers(nums: List[float]) -> Optional[float]:
    """从乐咕页面一行日期附近的数字中选 PE。

    乐咕 PE 页面通常同时出现：静态中位数、静态等权、静态PE、TTM中位数、
    TTM等权、TTM PE。优先取第 6 个数字（TTM 市值加权 PE），不足时取最后
    一个合理 PE。此函数只用于系统自算百分位；若解析不到足够历史点，会失败，
    不会伪造 PE 百分位。
    """
    clean = [float(x) for x in nums if 0 < float(x) < 300]
    if not clean:
        return None
    if len(clean) >= 6:
        return clean[5]
    return clean[-1]


def _extract_legulegu_history_from_html(html: str, source_label: str) -> Any:
    import pandas as pd  # type: ignore
    import html as html_lib

    if not html:
        raise ValueError("乐咕页面为空")
    text = html_lib.unescape(html)
    rows: List[Tuple[str, float]] = []

    # 1) 常见图表/脚本结构：['2020-01-01', 1, 2, ...] 或 ["2020-01-01", ...]
    arr_pattern = re.compile(r"\[\s*['\"](?P<date>20\d{2}[-/]\d{1,2}[-/]\d{1,2})['\"]\s*,(?P<body>[^\]]{1,600})\]", re.S)
    for m in arr_pattern.finditer(text):
        ds = _as_date_string(m.group("date"))
        if not ds:
            continue
        nums = []
        for raw in re.findall(r"[-+]?\d+(?:\.\d+)?", m.group("body")):
            try:
                nums.append(float(raw))
            except Exception:
                pass
        pe = _pick_legulegu_pe_from_numbers(nums)
        if pe is not None:
            rows.append((ds, pe))

    # 2) HTML 表格结构：日期后面跟多个 <td> 数字。
    tr_pattern = re.compile(r"<tr[^>]*>(?P<tr>.*?)</tr>", re.S | re.I)
    td_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
    tag_re = re.compile(r"<[^>]+>")
    for tr_m in tr_pattern.finditer(text):
        cells = [tag_re.sub("", c).strip() for c in td_pattern.findall(tr_m.group("tr"))]
        if not cells:
            continue
        ds = None
        nums: List[float] = []
        for cell in cells:
            cell_text = cell.replace(",", "")
            if ds is None:
                dm = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", cell_text)
                if dm:
                    ds = _as_date_string(dm.group(0))
                    continue
            try:
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", cell_text):
                    nums.append(float(cell_text.replace("%", "")))
            except Exception:
                pass
        if ds:
            pe = _pick_legulegu_pe_from_numbers(nums)
            if pe is not None:
                rows.append((ds, pe))

    # 3) 文本兜底：日期附近的数值，避免某些页面不用表格/数组。
    # 限制窗口长度和 PE 合理范围，解析不到足够点则失败，不污染计算。
    if len(rows) < 30:
        date_pat = re.compile(r"(?P<date>20\d{2}[-/]\d{1,2}[-/]\d{1,2})(?P<body>.{0,260})", re.S)
        for m in date_pat.finditer(text.replace("\n", " ")):
            ds = _as_date_string(m.group("date"))
            if not ds:
                continue
            nums = []
            for raw in re.findall(r"[-+]?\d+(?:\.\d+)?", m.group("body")):
                try:
                    nums.append(float(raw))
                except Exception:
                    pass
            pe = _pick_legulegu_pe_from_numbers(nums)
            if pe is not None:
                rows.append((ds, pe))

    # 去重：同日取最后一次出现的值。
    by_date: Dict[str, float] = {}
    for ds, pe in rows:
        if pe and 0 < pe < 300:
            by_date[ds] = pe
    if len(by_date) < 30:
        raise ValueError(f"乐咕页面未解析到足够历史 PE：{len(by_date)} 条")
    data = [{"日期": ds, "市盈率": pe} for ds, pe in sorted(by_date.items())]
    return pd.DataFrame(data)


def fetch_akshare_stock_index_pe_lg_series(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None, symbol_name: str = "") -> Dict[str, Dict[str, Any]]:
    """用 AKShare 当前可用接口 stock_index_pe_lg 获取指数历史 PE 序列。

    该接口返回乐咕乐股指数历史 PE，包含【滚动市盈率】等列。
    我们优先使用【滚动市盈率】作为 TTM PE，并在本地按当日及以前
    数据计算历史 PE 百分位，避免使用未来数据。
    """
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到 AKShare stock_index_pe_lg 指数代码")
    name = AK_STOCK_INDEX_PE_LG_SYMBOL_MAP.get(code)
    if not name:
        raise ValueError(f"AKShare stock_index_pe_lg 暂不支持该指数：{code} / {AK_INDEX_NAME_MAP.get(code, symbol_name or code)}")
    with proxy_environment(options):
        fn = getattr(ak, "stock_index_pe_lg", None)
        if fn is None:
            raise ValueError("当前 AKShare 版本没有 ak.stock_index_pe_lg")
        df = fn(symbol=name)
    seq = extract_valuation_series_from_df(df, f"akshare:stock_index_pe_lg:{name}:history")
    return _validate_history_percentile_series(seq, f"akshare:stock_index_pe_lg:{name}")


def fetch_legulegu_index_valuation_series(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None, symbol_name: str = "") -> Dict[str, Dict[str, Any]]:
    import requests  # type: ignore

    options = options or FetchOptions()
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到乐咕指数代码")
    name = AK_INDEX_NAME_MAP.get(code, code)
    digits = re.sub(r"\D", "", code)
    slugs = _legulegu_slugs_for_index(code, name, digits)
    if not slugs:
        raise ValueError(f"乐咕暂未配置该指数页面：code={code}, name={name}")
    diagnostics: List[str] = [f"输入symbol={symbol}, code={code}, name={name}, slugs={slugs}"]
    with proxy_environment(options):
        for slug in slugs:
            url = f"https://legulegu.com/stockdata/{slug}"
            try:
                resp = requests.get(url, headers=_legulegu_headers(), timeout=options.timeout, proxies=request_proxies(options))
                preview = (resp.text or "")[:80].replace("\n", " ")
                resp.raise_for_status()
                df = _extract_legulegu_history_from_html(resp.text, f"legulegu:{slug}:history")
                seq = extract_valuation_series_from_df(df, f"legulegu:{slug}:history")
                seq = _validate_history_percentile_series(seq, f"legulegu:{slug}")
                diagnostics.append(f"{url} -> {_df_debug_summary(df)} -> {_valuation_series_debug_summary(seq)}")
                return seq
            except Exception as e:
                diagnostics.append(f"{url} -> 失败：{_diag_text(e)}; preview={_diag_text(preview, 120) if 'preview' in locals() else '--'}")
    raise ValueError("乐咕历史估值诊断：" + "；".join(diagnostics[-8:]))

def fetch_akshare_lg_valuation_series(symbol: str, options: Optional[FetchOptions] = None) -> Dict[str, Dict[str, Any]]:
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    s = re.sub(r"\D", "", symbol or "")
    if not re.fullmatch(r"\d{6}", s):
        raise ValueError("不是A股6位代码")
    with proxy_environment(options):
        errors: List[str] = []
        for fn_name in ("stock_a_indicator_lg", "stock_a_lg_indicator"):
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            try:
                df = fn(symbol=s)
                seq = extract_valuation_series_from_df(df, f"akshare:{fn_name}:history")
                return _validate_history_percentile_series(seq, f"akshare:{fn_name}")
            except Exception as e:
                errors.append(str(e))
        raise ValueError("；".join(errors[-2:]) or "当前 AKShare 没有可用乐咕历史估值接口")


def fetch_akshare_index_valuation_series(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None, symbol_name: str = "") -> Dict[str, Dict[str, Any]]:
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到指数估值代码")
    name = AK_INDEX_NAME_MAP.get(code, code)
    digits = re.sub(r"\D", "", code)

    diagnostics: List[str] = []
    diagnostics.append(f"输入symbol={symbol}, asset_kind={asset_kind}, symbol_name={symbol_name or '--'}")
    diagnostics.append(f"映射结果 code={code}, name={name}, digits={digits}")
    diagnostics.append(f"akshare版本={getattr(ak, '__version__', 'unknown')}")

    # 系统自算只接受"历史估值"接口，不接受只返回当前 1 行的估值接口。
    # 有些 AKShare 版本/源会把 index_value_hist_funddb 返回成 1 行，必须判为失败。
    symbols_to_try: List[str] = []
    for sym in (name, f"{name}指数", code, digits):
        if sym and sym not in symbols_to_try:
            symbols_to_try.append(sym)
    diagnostics.append(f"尝试symbol={symbols_to_try}")

    # 优先使用 AKShare 当前版本可用的 stock_index_pe_lg 历史 PE 接口。
    # 这条链路能直接拿到多年【滚动市盈率】历史序列，然后本地自算百分位。
    try:
        pe_lg_seq = fetch_akshare_stock_index_pe_lg_series(symbol, asset_kind, options, symbol_name)
        diagnostics.append(f"stock_index_pe_lg成功：{_valuation_series_debug_summary(pe_lg_seq)}")
        return pe_lg_seq
    except Exception as e:
        diagnostics.append(f"stock_index_pe_lg失败：{_diag_text(e)}")

    calls: List[Tuple[str, Dict[str, Any]]] = []
    for sym in symbols_to_try:
        calls.extend([
            ("index_value_hist_funddb", {"symbol": sym, "indicator": "市盈率"}),
            ("index_value_hist_funddb", {"symbol": sym, "indicator": "市净率"}),
            ("index_value_hist_funddb", {"symbol": sym}),
        ])

    merged: Dict[str, Dict[str, Any]] = {}
    with proxy_environment(options):
        for fn_name, kwargs in calls:
            fn = getattr(ak, fn_name, None)
            if fn is None:
                diagnostics.append(f"函数不存在：ak.{fn_name}")
                continue
            try:
                df = fn(**kwargs)
                df_info = _df_debug_summary(df)
                try:
                    seq = extract_valuation_series_from_df(df, f"akshare:{fn_name}:{kwargs.get('symbol')}:history")
                except Exception as parse_error:
                    diagnostics.append(f"{fn_name}({kwargs}) -> {df_info} -> 解析失败：{_diag_text(parse_error)}")
                    continue

                seq_info = _valuation_series_debug_summary(seq)
                diagnostics.append(f"{fn_name}({kwargs}) -> {df_info} -> {seq_info}")
                if len(seq) < 30:
                    continue
                merged = merge_valuation_series(merged, seq)
            except Exception as e:
                diagnostics.append(f"{fn_name}({kwargs}) -> 请求失败：{_diag_text(e)}")
            if len(merged) >= 120 and any(v.get("pe_percentile") is not None for v in merged.values()):
                break

    # AKShare 1.15.52 之后已移除 funddb 相关接口；如果当前版本没有
    # index_value_hist_funddb，则直接走乐咕页面解析历史 PE，再系统自算百分位。
    try:
        legu_seq = fetch_legulegu_index_valuation_series(symbol, asset_kind, options, symbol_name)
        diagnostics.append(f"乐咕直接历史估值成功：{_valuation_series_debug_summary(legu_seq)}")
        merged = merge_valuation_series(merged, legu_seq)
    except Exception as e:
        diagnostics.append(f"乐咕直接历史估值失败：{_diag_text(e)}")

    if not merged:
        raise ValueError("AKShare/乐咕历史指数估值诊断：" + "；".join(diagnostics[-24:]))
    try:
        return _validate_history_percentile_series(merged, f"akshare_or_legulegu:index:{name}")
    except Exception as e:
        raise ValueError(f"{e}；AKShare/乐咕历史指数估值诊断：" + "；".join(diagnostics[-24:]))

def fetch_historical_valuation_series_uncached(symbol: str, market: str, asset_kind: str, cfg: Dict[str, Any], symbol_name: str = "") -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """回测用历史估值序列。优先国内指数/ETF/基金映射；A股个股尝试乐咕。"""
    options = fetch_options_from_cfg(cfg)
    market_u = str(market or "auto").upper()
    kind = str(asset_kind or "auto").lower()
    errors: List[str] = []
    system_candidates: List[Tuple[str, Callable[[], Dict[str, Dict[str, Any]]]]] = []
    danjuan_candidates: List[Tuple[str, Callable[[], Dict[str, Dict[str, Any]]]]] = []
    if (market_u == "CN" or kind in {"index", "fund", "etf"} or _norm_index_code(symbol, kind, symbol_name)):
        system_candidates.append(("akshare_index_history", lambda: fetch_akshare_index_valuation_series(symbol, kind, options, symbol_name)))
        danjuan_candidates.append(("danjuan_history_pe_pb_roe", lambda: fetch_danjuan_full_history_series(symbol, kind, options, symbol_name)))
    if (market_u == "CN" or re.fullmatch(r"\d{6}", re.sub(r"\D", "", symbol or ""))) and kind not in {"fund", "etf", "index"}:
        system_candidates.append(("akshare_lg_history", lambda: fetch_akshare_lg_valuation_series(symbol, options)))

    method = str(cfg.get("valuation_method", "system_calc") or "system_calc")
    if method == "danjuan":
        candidates = danjuan_candidates + system_candidates
    elif method == "system_calc":
        candidates = system_candidates
    else:
        candidates = system_candidates + danjuan_candidates
    for label, fn in candidates:
        try:
            seq = fn()
            if seq:
                return seq, [f"历史估值：{label} 成功，{len(seq)} 条"]
        except Exception as e:
            errors.append(f"历史估值：{label} 失败：{str(e)[:160]}")
    return {}, errors or ["历史估值：当前标的暂不支持历史估值序列"]


def historical_valuation_for_date(series: Dict[str, Dict[str, Any]], ds: str) -> Dict[str, Any]:
    """取不晚于 ds 的最近历史估值，避免未来函数。

    PE/PB/ROE 的历史接口日期可能不同步；这里按字段分别向前查找，
    避免某天只有 ROE/PB 而把较早的 PE 百分位覆盖丢失。
    """
    if not series:
        return {}
    keys = sorted([k for k in series.keys() if k <= ds], reverse=True)
    if not keys:
        return {}
    fields = [
        "current_pe", "pe_percentile", "current_pb", "pb_percentile", "roe_pct",
        "valuation_source", "valuation_note", "valuation_extra_note",
    ]
    out: Dict[str, Any] = {}
    used_dates: List[str] = []
    for key in keys:
        item = series.get(key) or {}
        for field in fields:
            if out.get(field) in (None, "") and item.get(field) not in (None, ""):
                out[field] = item.get(field)
                if key not in used_dates:
                    used_dates.append(key)
        if all(out.get(f) not in (None, "") for f in ["current_pe", "pe_percentile", "current_pb", "pb_percentile", "roe_pct"]):
            break
    if used_dates:
        out["valuation_date"] = max(used_dates)
    return out



def latest_valuation_from_history_series(series: Dict[str, Dict[str, Any]], source_label: str = "historical_latest") -> Dict[str, Any]:
    """实时页面使用：从历史估值序列取最新一日。

    系统自算 PE 百分位必须基于真实历史序列。
    如果历史点数不足或没有算出 pe_percentile，就明确失败，
    不再把"当前 1 行估值"误报为系统自算成功。
    """
    series = _validate_history_percentile_series(series, source_label)
    rows = len(series)
    pe_rows = sum(1 for v in series.values() if v.get("current_pe") is not None)
    pe_pct_rows = sum(1 for v in series.values() if v.get("pe_percentile") is not None)
    latest_ds = max(series.keys())
    item = historical_valuation_for_date(series, latest_ds)
    if item.get("pe_percentile") is not None:
        item["valuation_source"] = f"{source_label}:{item.get('valuation_source') or 'history'}"
        item["valuation_note"] = f"系统自算历史百分位：{rows}条，PE有效{pe_rows}条"
        item["valuation_date"] = item.get("valuation_date") or latest_ds
        item["_history_rows"] = rows
        item["_history_pe_rows"] = pe_rows
        item["_history_pe_percentile_rows"] = pe_pct_rows
        return item
    raise ValueError(f"历史估值序列没有可用 PE 百分位：{rows} 条")

def fetch_akshare_lg_valuation_latest_from_history(symbol: str, options: Optional[FetchOptions] = None) -> Dict[str, Any]:
    series = fetch_akshare_lg_valuation_series(symbol, options)
    return latest_valuation_from_history_series(series, "akshare_lg_history_latest")


def fetch_akshare_index_valuation_latest_from_history(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None, symbol_name: str = "") -> Dict[str, Any]:
    series = fetch_akshare_index_valuation_series(symbol, asset_kind, options, symbol_name)
    return latest_valuation_from_history_series(series, "akshare_index_history_latest")

def fetch_akshare_lg_valuation(symbol: str, options: Optional[FetchOptions] = None) -> Dict[str, Any]:
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    s = re.sub(r"\D", "", symbol or "")
    if not re.fullmatch(r"\d{6}", s):
        raise ValueError("不是A股6位代码")
    with proxy_environment(options):
        last_error = ""
        for fn_name in ("stock_a_indicator_lg", "stock_a_lg_indicator"):
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            try:
                df = fn(symbol=s)
                return extract_valuation_from_df(df, f"akshare:{fn_name}")
            except Exception as e:
                last_error = str(e)
        raise ValueError(last_error or "当前 AKShare 没有可用乐咕估值接口")


def fetch_akshare_index_valuation(symbol: str, asset_kind: str, options: Optional[FetchOptions] = None, symbol_name: str = "") -> Dict[str, Any]:
    import akshare as ak  # type: ignore

    options = options or FetchOptions()
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到指数估值代码")
    name = AK_INDEX_NAME_MAP.get(code, code)
    digits = re.sub(r"\D", "", code)

    # 不同 AKShare 版本函数名和参数有变化，这里按最常见形态逐个试。
    calls: List[Tuple[str, Dict[str, Any]]] = [
        ("index_value_hist_funddb", {"symbol": name}),
        ("index_value_hist_funddb", {"symbol": code}),
        ("index_value_hist_funddb", {"symbol": digits}),
        ("index_value_name_funddb", {"symbol": name}),
        ("stock_zh_index_value_csindex", {"symbol": digits}),
        ("index_value_csindex", {"symbol": digits}),
    ]
    with proxy_environment(options):
        last_error = ""
        for fn_name, kwargs in calls:
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                return extract_valuation_from_df(df, f"akshare:{fn_name}:{kwargs.get('symbol')}")
            except Exception as e:
                last_error = str(e)
        raise ValueError(last_error or "AKShare 未返回指数估值")


def _normalise_percent(value: Any) -> Optional[float]:
    """把 0~1 或 0~100 的百分位统一成 0~100。"""
    if value is None or value == "":
        return None
    try:
        text = str(value).strip().replace("%", "").replace(",", "")
        v = float(text)
        if math.isnan(v) or math.isinf(v):
            return None
        if 0 < v <= 1:
            v *= 100
        return round(clamp(v, 0.0, 100.0), 4)
    except Exception:
        return None


def _normalise_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        text = str(value).strip().replace("%", "").replace(",", "")
        v = float(text)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 6)
    except Exception:
        return None


def _json_get_path(payload: Any, path: Tuple[str, ...]) -> Any:
    cur = payload
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur.get(part)
    return cur


DANJUAN_DETAIL_VALUATION_PATHS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    # 蛋卷 detail 接口优先读 data.index_eva，避免递归扫描时被图表、同类指数、说明项等嵌套字段带偏。
    "pe_percentile": (
        ("data", "index_eva", "pe_percentile"),
        ("data", "index_eva", "pe_percent"),
        ("data", "index_eva", "pe_ttm_percentile"),
        ("data", "index_eva", "pe_percentile_ttm"),
        ("data", "pe_percentile"),
        ("data", "pe_percent"),
    ),
    "pb_percentile": (
        ("data", "index_eva", "pb_percentile"),
        ("data", "index_eva", "pb_percent"),
        ("data", "pb_percentile"),
        ("data", "pb_percent"),
    ),
    "current_pe": (
        ("data", "index_eva", "pe"),
        ("data", "index_eva", "pe_ttm"),
        ("data", "pe"),
        ("data", "pe_ttm"),
    ),
    "current_pb": (
        ("data", "index_eva", "pb"),
        ("data", "index_eva", "pb_lf"),
        ("data", "pb"),
        ("data", "pb_lf"),
    ),
    "roe_pct": (
        ("data", "index_eva", "roe"),
        ("data", "index_eva", "roe_pct"),
        ("data", "roe"),
        ("data", "roe_pct"),
    ),
}


def _first_json_path_value(payload: Any, paths: Tuple[Tuple[str, ...], ...]) -> Any:
    for path in paths:
        value = _json_get_path(payload, path)
        if value is not None and value != "":
            return value
    return None


def _extract_valuation_from_json(payload: Any, source: str) -> Dict[str, Any]:
    """按蛋卷 detail 的显式 JSON Path 提取估值字段。

    旧版会递归扫描整个 JSON，兼容性强但有误取风险：如果返回体里同时带有
    图表、同类指数、历史序列或说明字段，可能先命中无关的 PE/PB/ROE。
    这里只允许白名单路径，优先 data.index_eva，缺失时才尝试 data 顶层兼容路径。
    """
    result: Dict[str, Any] = {"valuation_source": source, "valuation_note": "--"}

    raw_pe_pct = _first_json_path_value(payload, DANJUAN_DETAIL_VALUATION_PATHS["pe_percentile"])
    raw_pb_pct = _first_json_path_value(payload, DANJUAN_DETAIL_VALUATION_PATHS["pb_percentile"])
    raw_pe = _first_json_path_value(payload, DANJUAN_DETAIL_VALUATION_PATHS["current_pe"])
    raw_pb = _first_json_path_value(payload, DANJUAN_DETAIL_VALUATION_PATHS["current_pb"])
    raw_roe = _first_json_path_value(payload, DANJUAN_DETAIL_VALUATION_PATHS["roe_pct"])

    pe_pct = _normalise_percent(raw_pe_pct)
    if pe_pct is not None:
        result["pe_percentile"] = pe_pct

    pb_pct = _normalise_percent(raw_pb_pct)
    if pb_pct is not None:
        result["pb_percentile"] = pb_pct

    pe = _normalise_number(raw_pe)
    if pe is not None and pe > 0:
        result["current_pe"] = pe

    pb = _normalise_number(raw_pb)
    if pb is not None and pb > 0:
        result["current_pb"] = pb

    roe = _normalise_number(raw_roe)
    if roe is not None:
        # 有些接口用 0.18 表示 18%，有些直接用 18。
        if -1 <= roe <= 1 and roe != 0:
            roe *= 100
        result["roe_pct"] = round(roe, 4)

    if not any(result.get(k) is not None for k in ("pe_percentile", "pb_percentile", "roe_pct", "current_pe", "current_pb")):
        tried = [".".join(path) for paths in DANJUAN_DETAIL_VALUATION_PATHS.values() for path in paths]
        raise ValueError("蛋卷 JSON 未在显式路径中解析到估值字段；已尝试：" + ", ".join(tried[:12]))
    return result


def _danjuan_headers(options: FetchOptions) -> Dict[str, str]:
    headers = {
        "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        "Accept": "*/*",
        "Host": "danjuanfunds.com",
        "Connection": "keep-alive",
        "Referer": "https://danjuanfunds.com/djmodule/value-center",
    }
    if options.danjuan_cookie:
        headers["Cookie"] = options.danjuan_cookie
    return headers


def fetch_danjuan_json_raw(code: str, options: FetchOptions) -> Tuple[Any, Dict[str, Any]]:
    import requests  # type: ignore

    url = f"https://danjuanfunds.com/djapi/index_eva/detail/{code}"
    resp = requests.get(url, headers=_danjuan_headers(options), timeout=options.timeout, proxies=request_proxies(options))
    text = resp.text
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as e:
        raise ValueError(f"蛋卷返回不是 JSON：{e}; preview={text[:180]}")
    return payload, {"url": url, "status_code": resp.status_code, "response_preview": text[:800]}

def fetch_danjuan_detail_valuation(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str = "") -> Dict[str, Any]:
    """蛋卷（雪球）detail 当前页面估值。

    实时仓位助手固定使用这个接口读取当前 PE/PB/ROE/百分位，避免把
    7天采样的历史图表序列和页面当前分位混为一谈。
    """
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到蛋卷指数代码")
    payload, meta = fetch_danjuan_json_raw(code, options)
    detail = _extract_valuation_from_json(payload, f"danjuan_detail:{code}")
    raw_data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(raw_data, dict):
        detail_date = _danjuan_ts_to_date(raw_data.get("ts") or raw_data.get("date"))
        if detail_date:
            detail["valuation_date"] = detail_date
        if raw_data.get("date") is not None:
            detail["valuation_display_date"] = str(raw_data.get("date"))
        if raw_data.get("updated_at") is not None:
            detail["valuation_updated_at"] = raw_data.get("updated_at")
    detail["valuation_endpoint"] = meta.get("url")
    detail["valuation_note"] = "蛋卷（雪球）当前页面估值"
    return detail


def fetch_danjuan_metric_history_raw(code: str, metric: str, options: FetchOptions) -> Tuple[Any, Dict[str, Any]]:
    import requests  # type: ignore

    metric = str(metric).strip().lower()
    if metric not in {"pe", "pb", "roe"}:
        raise ValueError(f"不支持的蛋卷历史估值指标：{metric}")
    url = f"https://danjuanfunds.com/djapi/index_eva/{metric}_history/{code}?day=all"
    headers = _danjuan_headers(options)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = f"https://danjuanfunds.com/dj-valuation-table-detail/{code}"
    resp = requests.get(url, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    text = resp.text or ""
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as e:
        raise ValueError(f"蛋卷历史{metric.upper()}返回不是 JSON：{e}; preview={text[:180]}")
    return payload, {"url": url, "status_code": resp.status_code, "response_preview": text[:800]}


def _find_danjuan_metric_history_items(payload: Any, value_key: str) -> List[Dict[str, Any]]:
    """从蛋卷历史 JSON 中递归找带 value_key/ts 的历史数组。"""
    value_key = str(value_key)
    candidates: List[List[Dict[str, Any]]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            dicts = [i for i in x if isinstance(i, dict)]
            if dicts and sum(1 for i in dicts if value_key in i and ("ts" in i or "date" in i or "time" in i)) >= max(3, len(dicts) // 2):
                candidates.append(dicts)
            for i in x[:20]:
                walk(i)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(payload)
    if not candidates:
        return []
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def fetch_danjuan_metric_history_series(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str, metric: str) -> Dict[str, Dict[str, Any]]:
    """蛋卷历史 PE/PB/ROE 曲线 → 本地生成逐日估值序列。"""
    import pandas as pd  # type: ignore

    metric = str(metric).strip().lower()
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到蛋卷指数代码")
    value_key = {"pe": "pe", "pb": "pb", "roe": "roe"}.get(metric)
    col_name = {"pe": "滚动市盈率", "pb": "市净率", "roe": "roe"}.get(metric)
    if not value_key or not col_name:
        raise ValueError(f"不支持的蛋卷历史估值指标：{metric}")

    payload, meta = fetch_danjuan_metric_history_raw(code, metric, options)
    items = _find_danjuan_metric_history_items(payload, value_key)
    rows: List[Dict[str, Any]] = []
    for item in items:
        try:
            value = float(str(item.get(value_key, "")).replace(",", ""))
        except Exception:
            continue
        ds = _danjuan_ts_to_date(item.get("ts") or item.get("date") or item.get("time"))
        if ds and value > 0:
            # ROE 接口通常用 0.1204 表示 12.04%，extract_valuation_series_from_df 会统一转成百分数。
            upper_limit = 500 if metric in {"pe", "pb"} else 10
            if value < upper_limit:
                rows.append({"日期": ds, col_name: value})

    by_date: Dict[str, float] = {}
    for row in rows:
        by_date[str(row["日期"])] = float(row[col_name])
    if len(by_date) < 10:
        raise ValueError(f"蛋卷历史{metric.upper()}序列不足：{len(by_date)} 条；url={meta.get('url')}")
    df = pd.DataFrame([{"日期": ds, col_name: value} for ds, value in sorted(by_date.items())])
    seq = extract_valuation_series_from_df(df, f"danjuan_{metric}_history:{code}")
    if metric == "pe":
        return _validate_history_percentile_series(seq, f"danjuan_{metric}_history:{code}")
    return seq


def fetch_danjuan_full_history_series(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str = "") -> Dict[str, Dict[str, Any]]:
    """蛋卷历史 PE/PB/ROE 曲线合并。

    PE 是主序列；PB 和 ROE 能获取就补充，失败不影响 PE 百分位主计算。
    """
    errors: List[str] = []
    try:
        seq = fetch_danjuan_metric_history_series(symbol, asset_kind, options, symbol_name, "pe")
    except Exception as e:
        raise ValueError(f"蛋卷历史PE失败：{e}")
    for metric in ("pb", "roe"):
        try:
            extra = fetch_danjuan_metric_history_series(symbol, asset_kind, options, symbol_name, metric)
            seq = merge_valuation_series(seq, extra)
        except Exception as e:
            errors.append(f"{metric.upper()}失败：{str(e)[:120]}")
    # detail 当前页面可能比历史曲线多一个最新交易日；补入后重新自算百分位。
    seq = merge_danjuan_detail_current_into_history_series(seq, symbol, asset_kind, options, symbol_name)
    # 保留补充失败提示，但不污染用于计算的字段。
    if errors:
        for item in seq.values():
            item.setdefault("valuation_extra_note", "；".join(errors))
    return _validate_history_percentile_series(seq, "danjuan_full_history")


def fetch_danjuan_pe_history_raw(code: str, options: FetchOptions) -> Tuple[Any, Dict[str, Any]]:
    import requests  # type: ignore

    url = f"https://danjuanfunds.com/djapi/index_eva/pe_history/{code}?day=all"
    headers = _danjuan_headers(options)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Referer"] = f"https://danjuanfunds.com/dj-valuation-table-detail/{code}"
    resp = requests.get(url, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    text = resp.text or ""
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as e:
        raise ValueError(f"蛋卷历史PE返回不是 JSON：{e}; preview={text[:180]}")
    return payload, {"url": url, "status_code": resp.status_code, "response_preview": text[:800]}


def _find_danjuan_pe_history_items(payload: Any) -> List[Dict[str, Any]]:
    """从蛋卷 pe_history JSON 中递归找带 pe/ts 的历史数组。"""
    candidates: List[List[Dict[str, Any]]] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            dicts = [i for i in x if isinstance(i, dict)]
            if dicts and sum(1 for i in dicts if "pe" in i and ("ts" in i or "date" in i or "time" in i)) >= max(3, len(dicts) // 2):
                candidates.append(dicts)
            for i in x[:20]:
                walk(i)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(payload)
    if not candidates:
        return []
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _danjuan_ts_to_date(value: Any) -> Optional[str]:
    """蛋卷接口 ts 是按北京时间交易日给的 00:00:00。

    之前用 UTC 解析会把 2026-06-05 00:00:00+08:00 显示成
    2026-06-04，导致"估值序列最新日期"比页面 date 慢一天。
    这里固定按 UTC+8 转日期，和蛋卷页面的 date 字段对齐。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        try:
            china_tz = timezone(timedelta(hours=8))
            return datetime.fromtimestamp(ts, china_tz).date().isoformat()
        except Exception:
            return None
    return _as_date_string(value)


def fetch_danjuan_pe_history_series(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str = "") -> Dict[str, Dict[str, Any]]:
    """蛋卷 pe_history 历史 PE 序列 → 本地自算历史 PE 百分位。

    与 /detail 接口不同，这里使用历史 PE 曲线，不直接采用当前 PE 百分位。
    适合科创50等 AKShare stock_index_pe_lg 暂不支持但蛋卷支持的指数。
    """
    import pandas as pd  # type: ignore

    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到蛋卷指数代码")
    payload, meta = fetch_danjuan_pe_history_raw(code, options)
    items = _find_danjuan_pe_history_items(payload)
    rows: List[Dict[str, Any]] = []
    for item in items:
        try:
            pe = float(str(item.get("pe", "")).replace(",", ""))
        except Exception:
            continue
        ds = _danjuan_ts_to_date(item.get("ts") or item.get("date") or item.get("time"))
        if ds and pe > 0 and pe < 500:
            rows.append({"日期": ds, "滚动市盈率": pe})
    # 同日去重，保留最后一条。
    by_date: Dict[str, float] = {}
    for row in rows:
        by_date[str(row["日期"])] = float(row["滚动市盈率"])
    if len(by_date) < 30:
        raise ValueError(f"蛋卷历史PE序列不足：{len(by_date)} 条；url={meta.get('url')}")
    df = pd.DataFrame([{"日期": ds, "滚动市盈率": pe} for ds, pe in sorted(by_date.items())])
    seq = extract_valuation_series_from_df(df, f"danjuan_pe_history:{code}")
    return _validate_history_percentile_series(seq, f"danjuan_pe_history:{code}")


def fetch_danjuan_valuation_latest_from_history(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str = "") -> Dict[str, Any]:
    series = fetch_danjuan_full_history_series(symbol, asset_kind, options, symbol_name)
    return latest_valuation_from_history_series(series, "danjuan_history_latest")


def fetch_danjuan_valuation(symbol: str, asset_kind: str, options: FetchOptions, symbol_name: str = "") -> Dict[str, Any]:
    """蛋卷估值：优先用 pe_history 历史 PE 序列自算 PE 百分位，再用 detail 补 PB/ROE。"""
    code = _norm_index_code(symbol, asset_kind, symbol_name)
    if not code:
        raise ValueError("未匹配到蛋卷指数代码")

    result: Dict[str, Any] = {}
    history_error = ""
    try:
        result = fetch_danjuan_valuation_latest_from_history(symbol, asset_kind, options, symbol_name)
    except Exception as e:
        history_error = str(e)

    detail_error = ""
    try:
        payload, meta = fetch_danjuan_json_raw(code, options)
        detail = _extract_valuation_from_json(payload, f"danjuan_json:{code}")
        detail["valuation_endpoint"] = meta.get("url")
        # 只补空值，避免 detail 的当前 PE 百分位覆盖 pe_history 自算结果。
        result = merge_missing_fundamentals(result, detail)
        result.setdefault("valuation_endpoint", meta.get("url"))
    except Exception as e:
        detail_error = str(e)

    if result:
        if result.get("valuation_source") is None:
            result["valuation_source"] = f"danjuan:{code}"
        result.setdefault("valuation_note", "--")
        return result
    raise ValueError("蛋卷估值失败：" + "；".join(x for x in [history_error, detail_error] if x))


def merge_missing_fundamentals(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """只补空值，避免覆盖更精确的数据；估值来源只在补到估值字段时更新。"""
    merged = dict(base or {})
    filled_any = False
    for key, value in (extra or {}).items():
        if value is None or value == "":
            continue
        if key in {"valuation_source", "valuation_note"}:
            continue
        if merged.get(key) in (None, ""):
            merged[key] = value
            if key in {"pe_percentile", "pb_percentile", "current_pe", "current_pb", "roe_pct"}:
                filled_any = True
    if filled_any and extra.get("valuation_source") and not merged.get("valuation_source"):
        merged["valuation_source"] = extra.get("valuation_source")
    return merged


def retry_valuation_call(label: str, fn: Callable[[], Dict[str, Any]], options: FetchOptions) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    trace: List[Dict[str, Any]] = []
    total = max(1, min(options.retry_count + 1, 3))  # 估值链路通常较慢，最多3次即可。
    last_error = ""
    for i in range(total):
        started = time.time()
        try:
            data = fn()
            if "history_latest" in label and data.get("pe_percentile") is None:
                raise ValueError("历史估值链路未返回 PE 百分位")
            if not any(data.get(k) is not None for k in ("pe_percentile", "pb_percentile", "current_pe", "roe_pct")):
                raise ValueError("未返回有效估值字段")
            trace.append({
                "source": label,
                "attempt": f"valuation-{i + 1}",
                "ok": True,
                "rows": int(data.get("_history_rows") or 1),
                "elapsed_ms": int((time.time() - started) * 1000),
            })
            return data, trace
        except Exception as e:
            last_error = str(e)
            trace.append({
                "source": label,
                "attempt": f"valuation-{i + 1}",
                "ok": False,
                "error": last_error[:1600],
                "elapsed_ms": int((time.time() - started) * 1000),
            })
            if i < total - 1:
                time.sleep(min(0.35 * (i + 1), 1.0))
    return None, trace


def enrich_fundamentals_with_public_valuation(
    symbol: str,
    market: str,
    asset_kind: str,
    fundamentals: Dict[str, Any],
    options: FetchOptions,
    symbol_name: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """多来源补充估值：AKShare/乐咕 -> AKShare指数估值 -> 蛋卷兜底。

    未拿到 PE 百分位时不写长提示；前端【估值提示】显示 --。
    """
    base = dict(fundamentals or {})
    traces: List[Dict[str, Any]] = []
    market = str(market or "auto").upper()
    kind = str(asset_kind or "auto").lower()
    digits = re.sub(r"\D", "", symbol or "")

    history_candidates: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    current_candidates: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    if (market == "CN" or re.fullmatch(r"\d{6}", digits)) and kind not in {"fund", "etf", "index"}:
        history_candidates.append(("valuation:akshare_lg_history_latest", lambda: fetch_akshare_lg_valuation_latest_from_history(symbol, options)))
        current_candidates.append(("valuation:akshare_lg", lambda: fetch_akshare_lg_valuation(symbol, options)))
    if market == "CN" or kind in {"index", "fund", "etf"} or _norm_index_code(symbol, kind, symbol_name):
        history_candidates.append(("valuation:akshare_index_history_latest", lambda: fetch_akshare_index_valuation_latest_from_history(symbol, kind, options, symbol_name)))
        current_candidates.append(("valuation:akshare_index", lambda: fetch_akshare_index_valuation(symbol, kind, options, symbol_name)))
    danjuan_history_candidates: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    danjuan_candidates: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    danjuan_detail_candidates: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    if market == "CN" or kind in {"index", "fund", "etf"} or _norm_index_code(symbol, kind, symbol_name):
        danjuan_detail_candidates.append(("valuation:danjuan_detail", lambda: fetch_danjuan_detail_valuation(symbol, kind, options, symbol_name)))
        danjuan_history_candidates.append(("valuation:danjuan_history_latest", lambda: fetch_danjuan_valuation_latest_from_history(symbol, kind, options, symbol_name)))
        danjuan_candidates.append(("valuation:danjuan", lambda: fetch_danjuan_valuation(symbol, kind, options, symbol_name)))

    method = str(getattr(options, "valuation_method", "system_calc") or "system_calc")
    if method == "danjuan_detail":
        # 实时仓位助手固定使用蛋卷 detail 当前页面分位，不受回测来源设置影响。
        candidates = danjuan_detail_candidates + current_candidates
    elif method == "danjuan":
        # 蛋卷优先时，也优先使用 pe_history 历史PE曲线自算百分位；detail 只作补充。
        candidates = danjuan_history_candidates + danjuan_candidates + history_candidates + current_candidates
    elif method == "system_calc":
        # 用户明确选择系统自算时，只跑 AKShare/乐咕历史序列，不走蛋卷。
        candidates = history_candidates
    else:
        # 兼容旧配置 auto：先系统自算，再用蛋卷历史PE兜底，最后才使用当前估值。
        candidates = history_candidates + danjuan_history_candidates + current_candidates + danjuan_candidates

    # 实时仓位页 method=danjuan_detail 时，即使行情源已有估值，也优先用蛋卷 detail 当前页面值覆盖/校准。
    # 其他模式下，如果行情源已经给了 PE 百分位，就不用再跑网络估值。
    if method != "danjuan_detail" and base.get("pe_percentile") is not None:
        base.setdefault("valuation_note", "--")
        return base, traces

    for label, fn in candidates:
        extra, trace = retry_valuation_call(label, fn, options)
        traces.extend(trace)
        if extra:
            if method == "danjuan_detail" and label == "valuation:danjuan_detail":
                # 实时仓位助手以蛋卷 detail 当前页面为准，直接覆盖估值相关字段。
                for k, v in extra.items():
                    if v is not None and v != "":
                        base[k] = v
            else:
                base = merge_missing_fundamentals(base, extra)
        if base.get("pe_percentile") is not None:
            break

    base.setdefault("valuation_note", "--")
    return base, traces

def fetch_market_data(symbol: str, market: str, asset_kind: str, source: str, cfg: Dict[str, Any]) -> Tuple[List[Dict[str, float]], Dict[str, Any], List[Dict[str, Any]], str]:
    """多链路获取：按源顺序重试，返回成功链路和每次尝试的日志。"""
    options = fetch_options_from_cfg(cfg)
    source = str(source or "auto").lower()
    market = str(market or "auto").upper()
    symbol_name = str(cfg.get("symbol_name") or "")
    asset_kind = resolve_asset_kind(symbol, market, asset_kind, symbol_name)
    is_cn_fund = is_cn_open_fund_like(symbol, market, asset_kind, symbol_name)
    is_danjuan_nav = is_danjuan_nav_like(symbol, market, asset_kind, symbol_name)
    danjuan_only = is_danjuan_only_source(source)
    if danjuan_only:
        source = "danjuan_only"
    trace_all: List[Dict[str, Any]] = []
    candidates: List[Tuple[str, Callable[[], Tuple[List[Dict[str, float]], Dict[str, Any]]]]] = []

    def add(label: str, fn: Callable[[], Tuple[List[Dict[str, float]], Dict[str, Any]]]):
        if label not in [x[0] for x in candidates]:
            candidates.append((label, fn))

    if source in {"auto", "akshare", "fund", "danjuan", "danjuan_only"} and (is_cn_fund or (danjuan_only and is_danjuan_nav)):
        add("danjuan_fund_nav", lambda: fetch_danjuan_fund_nav_recent(symbol, options))

    if danjuan_only and not candidates:
        raise RuntimeError("只使用蛋卷目前仅支持可通过蛋卷基金净值接口获取的基金 / ETF / QDII 标的")

    if source in {"auto", "akshare"} and (market == "CN" or re.fullmatch(r"\d{6}", symbol)):
        add("akshare", lambda: fetch_akshare(symbol, asset_kind, options))

    # 国内行情 HTTP 兜底：AKShare 失败时，直接走东方财富 K 线接口；场外基金不走股票K线。
    if source in {"auto", "akshare", "eastmoney"} and not is_cn_fund and (market == "CN" or re.fullmatch(r"\d{6}", symbol)):
        add("eastmoney_kline", lambda: fetch_eastmoney_kline(symbol, options))

    if source in {"auto", "yfinance"} and not is_cn_fund:
        for ys in yahoo_symbol_candidates(symbol, market, asset_kind):
            add(f"yfinance:{ys}", lambda ys=ys: fetch_yfinance(ys, options))

    # Yahoo Chart API 是 yfinance 的轻量备用链路，常用于 yfinance 间歇失败时兜底；场外基金不走 Yahoo。
    if source in {"auto", "yfinance", "yahoo"} and not is_cn_fund:
        for ys in yahoo_symbol_candidates(symbol, market, asset_kind):
            add(f"yahoo_chart:{ys}", lambda ys=ys: fetch_yahoo_chart_api(ys, options))

    # Stooq 主要兜底美股/ETF日线，不提供完整基本面。
    if market != "CN" and source in {"auto", "yfinance", "stooq"}:
        add(f"stooq:{symbol}", lambda: fetch_stooq_daily(symbol, options))

    # 用户强制 akshare 但国内股票/ETF链路全挂时，仍尝试 Yahoo 后缀兜底；场外基金不走 Yahoo。
    if source == "akshare" and not is_cn_fund:
        for ys in yahoo_symbol_candidates(symbol, market, asset_kind):
            add(f"yahoo_chart:{ys}", lambda ys=ys: fetch_yahoo_chart_api(ys, options))

    if not candidates and not danjuan_only:
        add("yfinance", lambda: fetch_yfinance(symbol, options))

    for label, fn in candidates:
        try:
            records, fundamentals, trace = retry_call(label, fn, options)
            trace_all.extend(trace)
            valuation_options = FetchOptions(
                proxy_mode=options.proxy_mode,
                proxy_url=options.proxy_url,
                timeout=options.timeout,
                retry_count=options.retry_count,
                danjuan_cookie=options.danjuan_cookie,
                valuation_method="danjuan_detail",
            )
            cfg_symbol_name = str(cfg.get("symbol_name") or symbol_name or "").strip()
            fund_symbol_name = str(
                fundamentals.get("long_name")
                or fundamentals.get("fund_full_name")
                or fundamentals.get("name")
                or ""
            ).strip()
            # “只使用蛋卷”搜索如果只传了代码，估值映射会缺少中文指数关键词；
            # 优先使用蛋卷基金详情返回的真实基金名称，便于匹配 NDX/SP500/沪深300 等指数估值。
            valuation_symbol_name = cfg_symbol_name
            if (
                not valuation_symbol_name
                or valuation_symbol_name.upper() == str(symbol or "").strip().upper()
                or re.fullmatch(r"\d{6}", valuation_symbol_name)
            ):
                valuation_symbol_name = fund_symbol_name or valuation_symbol_name
            fundamentals, valuation_trace = enrich_fundamentals_with_public_valuation(
                symbol=symbol,
                market=market,
                asset_kind=asset_kind,
                fundamentals=fundamentals,
                options=valuation_options,
                symbol_name=valuation_symbol_name,
            )
            trace_all.extend(valuation_trace)
            return records, fundamentals, trace_all, label
        except Exception as e:
            # retry_call 已经记录了每次失败；这里保留兜底错误，继续下一链路。
            if trace_all and trace_all[-1].get("source") == label:
                continue
            trace_all.append({"source": label, "attempt": "all", "ok": False, "error": str(e)[:240]})
            continue

    detail = "；".join([f"{x.get('source')}#{x.get('attempt')}: {x.get('error', '失败')}" for x in trace_all if not x.get("ok")])
    raise RuntimeError(detail or "所有数据源均失败")

def compute_indicators(records: List[Dict[str, float]], fundamentals: Dict[str, Any]) -> Dict[str, Any]:
    if not records:
        raise ValueError("没有拿到行情数据")

    closes = [r["close"] for r in records if r.get("close")]
    highs = [r.get("high") or r.get("close") for r in records]
    lows = [r.get("low") or r.get("close") for r in records]
    vols = [r.get("volume") or 0 for r in records]
    if len(closes) < 60:
        raise ValueError("行情数据太少，至少需要约60个交易日")

    def sma(values: List[float], n: int) -> Optional[float]:
        if len(values) < n:
            return None
        return sum(values[-n:]) / n

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else close
    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    ma50_prev = sma(closes[:-20], 50) if len(closes) >= 70 else None
    ma200_prev = sma(closes[:-20], 200) if len(closes) >= 220 else None

    def period_return(n: int) -> Optional[float]:
        if len(closes) <= n or closes[-n - 1] <= 0:
            return None
        return close / closes[-n - 1] - 1.0

    def annualized_vol(n: int) -> Optional[float]:
        if len(closes) <= n:
            return None
        rets: List[float] = []
        window = closes[-(n + 1):]
        for j in range(1, len(window)):
            prev = window[j - 1]
            if prev > 0:
                rets.append(window[j] / prev - 1.0)
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(max(var, 0.0)) * math.sqrt(252.0)

    def ema_series(values: List[float], n: int) -> List[Optional[float]]:
        if not values or n <= 0:
            return []
        alpha = 2.0 / (n + 1.0)
        out: List[Optional[float]] = []
        ema: Optional[float] = None
        for idx, value in enumerate(values):
            if idx + 1 < n:
                out.append(None)
                continue
            if idx + 1 == n:
                ema = sum(values[:n]) / n
            else:
                ema = value * alpha + float(ema) * (1.0 - alpha)
            out.append(ema)
        return out

    def macd_last(values: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if len(values) < 35:
            return None, None, None
        ema12 = ema_series(values, 12)
        ema26 = ema_series(values, 26)
        dif_series: List[Optional[float]] = []
        dif_values: List[float] = []
        for a, b in zip(ema12, ema26):
            if a is None or b is None:
                dif_series.append(None)
                continue
            dif = a - b
            dif_series.append(dif)
            dif_values.append(dif)
        dea_values = ema_series(dif_values, 9)
        if not dif_values or not dea_values or dea_values[-1] is None:
            return None, None, None
        dif = dif_values[-1]
        dea = float(dea_values[-1])
        # 按中文行情软件常用口径：MACD柱 = (DIF - DEA) * 2。
        bar = (dif - dea) * 2.0
        return dif, dea, bar

    def rsi_last(values: List[float], n: int) -> Optional[float]:
        if len(values) <= n:
            return None
        gains: List[float] = []
        losses: List[float] = []
        window = values[-(n + 1):]
        for idx in range(1, len(window)):
            diff = window[idx] - window[idx - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains) / n
        avg_loss = sum(losses) / n
        if avg_loss <= 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def boll_last(values: List[float], n: int = 20, k: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        if len(values) < n:
            return None, None, None, None, None
        window = values[-n:]
        mid = sum(window) / n
        var = sum((v - mid) ** 2 for v in window) / n
        std = math.sqrt(max(var, 0.0))
        upper = mid + k * std
        lower = mid - k * std
        width_pct = (upper - lower) / mid * 100.0 if mid else None
        percent_b = (values[-1] - lower) / (upper - lower) * 100.0 if upper > lower else 50.0
        return mid, upper, lower, width_pct, percent_b

    def slope_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
        if current is None or previous is None or previous <= 0:
            return None
        return current / previous - 1.0

    high_252 = max(closes[-252:]) if len(closes) >= 60 else max(closes)
    drawdown_252d = close / high_252 - 1.0 if high_252 > 0 else None

    trs: List[float] = []
    for i in range(1, len(records)):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    stop_loss_pct = (atr14 * 2 / close * 100) if atr14 and close else 6.0

    vol20 = sum(vols[-20:]) / 20 if len(vols) >= 20 and any(vols[-20:]) else None
    volume_ratio = vols[-1] / vol20 if vol20 else None

    # 简易均线策略的自动信号：只基于价格与 20 日均线的距离生成 ma_position。
    # 这同时服务于实时拉取后的自动填充与历史回测，否则回测中 ma_position 会一直缺省为 at_ma，导致没有交易记录。
    distance_ma20_pct = (close / ma20 - 1.0) * 100.0 if ma20 else None
    ma_position = "at_ma"
    if distance_ma20_pct is not None:
        if distance_ma20_pct <= -5.0:
            ma_position = "far_below"
        elif distance_ma20_pct < -1.0:
            ma_position = "below"
        elif distance_ma20_pct <= 1.0:
            ma_position = "at_ma"
        elif distance_ma20_pct < 5.0:
            ma_position = "above"
        else:
            ma_position = "far_above"

    macd_dif, macd_dea, macd_bar = macd_last(closes)
    rsi6 = rsi_last(closes, 6)
    rsi14 = rsi_last(closes, 14)
    boll_mid, boll_upper, boll_lower, boll_width_pct, boll_percent_b = boll_last(closes, 20, 2.0)

    # 自动趋势状态。
    market_state = "sideways"
    if ma200:
        ma200_down = ma200_prev is not None and ma200 < ma200_prev
        if close < ma200 and ma200_down:
            market_state = "bear"
        elif close < ma200:
            market_state = "below_200"
        elif ma50 and close > ma50 > ma200:
            market_state = "strong_bull"
        else:
            market_state = "above_200"

    # 自动入场/退出/量价：保守生成，允许用户手动覆盖。
    entry_state = "none"
    prev_60_high = max(closes[-61:-1]) if len(closes) >= 61 else None
    prev_20_high = max(closes[-21:-1]) if len(closes) >= 21 else None
    last_high = highs[-1] if highs else close
    last_low = lows[-1] if lows else close
    last_open = records[-1].get("open") or close

    touched_ma20 = bool(ma20 and last_low <= ma20 * 1.01 and close >= ma20)
    touched_ma50 = bool(ma50 and last_low <= ma50 * 1.01 and close >= ma50)
    pullback_hold_auto = market_state in {"above_200", "strong_bull"} and (touched_ma20 or touched_ma50) and close >= prev_close

    if market_state in {"above_200", "strong_bull"} and prev_60_high and close > prev_60_high:
        entry_state = "breakout"
    if market_state == "strong_bull" and prev_20_high and close > prev_20_high:
        entry_state = "continuation_high"
    if entry_state == "none" and pullback_hold_auto:
        entry_state = "pullback_hold"
    if ma50 and close > ma50 and prev_close < ma50 and market_state in {"below_200", "above_200"}:
        entry_state = "reversal_50"

    breakout_failed_auto = bool(prev_60_high and last_high > prev_60_high and close <= prev_60_high)
    exit_state = "none"
    if ma200 and close < ma200:
        exit_state = "below_200"
    elif ma50 and close < ma50:
        exit_state = "below_50"
    elif ma20 and close < ma20:
        exit_state = "below_20"
    elif breakout_failed_auto:
        exit_state = "failed_breakout"

    far_from_ma = False
    if ma20 and atr14:
        far_from_ma = (close - ma20) > atr14 * 2.2

    candle_range = max(last_high - last_low, 0.0)
    upper_wick = max(last_high - max(close, last_open), 0.0)
    upper_shadow_auto = bool(candle_range > 0 and upper_wick / candle_range >= 0.45 and volume_ratio and volume_ratio >= 1.25)
    failed_close_auto = bool(breakout_failed_auto or (prev_20_high and last_high > prev_20_high and close <= prev_20_high and volume_ratio and volume_ratio >= 1.2))

    data = {
        "last_date": records[-1].get("date"),
        "close": round(close, 4),
        "ma20": round(ma20, 4) if ma20 else None,
        "ma50": round(ma50, 4) if ma50 else None,
        "ma200": round(ma200, 4) if ma200 else None,
        "atr14": round(atr14, 4) if atr14 else None,
        "atr_pct": round((atr14 / close * 100.0), 2) if atr14 and close else None,
        "stop_loss_pct": round(clamp(stop_loss_pct, 1.0, 80.0), 2),
        "volume_ratio_20d": round(volume_ratio, 2) if volume_ratio else None,
        "macd_dif": round(macd_dif, 6) if macd_dif is not None else None,
        "macd_dea": round(macd_dea, 6) if macd_dea is not None else None,
        "macd_bar": round(macd_bar, 6) if macd_bar is not None else None,
        "macd_dif_pct": round(macd_dif / close * 100.0, 4) if macd_dif is not None and close else None,
        "macd_dea_pct": round(macd_dea / close * 100.0, 4) if macd_dea is not None and close else None,
        "macd_bar_pct": round(macd_bar / close * 100.0, 4) if macd_bar is not None and close else None,
        "rsi6": round(rsi6, 2) if rsi6 is not None else None,
        "rsi14": round(rsi14, 2) if rsi14 is not None else None,
        "boll_mid": round(boll_mid, 4) if boll_mid is not None else None,
        "boll_upper": round(boll_upper, 4) if boll_upper is not None else None,
        "boll_lower": round(boll_lower, 4) if boll_lower is not None else None,
        "boll_width_pct": round(boll_width_pct, 2) if boll_width_pct is not None else None,
        "boll_percent_b": round(boll_percent_b, 2) if boll_percent_b is not None else None,
        "return_20d": round(period_return(20) * 100.0, 2) if period_return(20) is not None else None,
        "return_60d": round(period_return(60) * 100.0, 2) if period_return(60) is not None else None,
        "return_120d": round(period_return(120) * 100.0, 2) if period_return(120) is not None else None,
        "volatility_20d": round(annualized_vol(20) * 100.0, 2) if annualized_vol(20) is not None else None,
        "volatility_60d": round(annualized_vol(60) * 100.0, 2) if annualized_vol(60) is not None else None,
        "drawdown_252d": round(drawdown_252d * 100.0, 2) if drawdown_252d is not None else None,
        "ma50_slope_20d": round(slope_pct(ma50, ma50_prev) * 100.0, 2) if slope_pct(ma50, ma50_prev) is not None else None,
        "ma200_slope_20d": round(slope_pct(ma200, ma200_prev) * 100.0, 2) if slope_pct(ma200, ma200_prev) is not None else None,
        "distance_ma20_pct": round(distance_ma20_pct, 2) if distance_ma20_pct is not None else None,
        "distance_ma50_pct": round((close / ma50 - 1.0) * 100.0, 2) if ma50 else None,
        "distance_ma200_pct": round((close / ma200 - 1.0) * 100.0, 2) if ma200 else None,
        "ma_position": ma_position,
        "market_state": market_state,
        "entry_state": entry_state,
        "exit_state": exit_state,
        "far_from_ma": far_from_ma,
        "volume_confirm": bool(volume_ratio and volume_ratio >= 1.35 and entry_state in {"breakout", "continuation_high"}),
        "pullback_volume_dry": bool(volume_ratio and volume_ratio <= 0.90 and entry_state == "pullback_hold"),
        "upper_shadow": upper_shadow_auto,
        "failed_close": failed_close_auto,
        "current_pe": fundamentals.get("current_pe"),
        "pe_percentile": fundamentals.get("pe_percentile"),
        "current_pb": fundamentals.get("current_pb"),
        "pb_percentile": fundamentals.get("pb_percentile"),
        "roe_pct": fundamentals.get("roe_pct"),
        "valuation_source": fundamentals.get("valuation_source"),
        "valuation_note": fundamentals.get("valuation_note") or "--",
        "currency": fundamentals.get("currency"),
        "long_name": fundamentals.get("long_name"),
        "source_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return data


def enrich_indicators_with_user_position(indicators: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """根据用户填写的当前涨跌幅/持仓盈亏率，自动推断盈利阶段和初始止损。

    用户直接填"当前涨跌幅 %"，脚本不再要求买入价/成本价。
    - 盈利 R 倍数 = 当前涨跌幅 / 止损距离。
    - 初始止损只在"有持仓 + 当前涨跌幅 <= -止损距离"时自动触发。

    注意：风险仓位上限低于当前仓位，不等于触发初始止损；
    触发初始止损只代表价格已经跌到买入前设定的止损线。
    """
    profit_pct = as_float(cfg.get("current_profit_pct"), 0.0)
    stop_pct = as_float(indicators.get("stop_loss_pct"), 0.0)
    current_amount = max(as_float(cfg.get("current_position_amount"), 0.0), 0.0)

    indicators["current_profit_pct"] = round(profit_pct, 2)
    indicators["profit_pct"] = round(profit_pct, 2)
    indicators["profit_r"] = None
    indicators["profit_state"] = "none"

    if profit_pct > 0 and stop_pct > 0:
        profit_r = profit_pct / stop_pct
        indicators["profit_r"] = round(profit_r, 2)
        if profit_r >= 3:
            indicators["profit_state"] = "profit_3r"
        elif profit_r >= 2:
            indicators["profit_state"] = "profit_2r"
        elif profit_r >= 1:
            indicators["profit_state"] = "profit_1r"

    # 自动触发【初始止损】的唯一量化条件：
    # 有持仓，并且当前持仓盈亏已经跌穿买入前设定的止损距离。
    # 但核心宽基的 core_satellite 模式不把增强仓当作一笔短线交易来硬清仓；
    # 初始止损只压低交易增强仓，增强仓由均线/系统风险逐步降速。
    if current_amount > 0 and stop_pct > 0 and profit_pct <= -stop_pct:
        if core_asset_profile(cfg):
            if indicators.get("exit_state") == "none":
                indicators["exit_state"] = "below_50"
            indicators["stop_triggered_auto"] = True
            indicators["core_stop_softened"] = True
        else:
            indicators["exit_state"] = "hit_stop"
            indicators["stop_triggered_auto"] = True
            indicators["core_stop_softened"] = False
    else:
        indicators["stop_triggered_auto"] = False
        indicators["core_stop_softened"] = False

    return indicators



def _preview_text(value: Any, limit: int = 900) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
    except Exception:
        text = repr(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _make_test_result(name: str, url: str, started: float, ok: bool, **kwargs: Any) -> Dict[str, Any]:
    item = {
        "name": name,
        "url": url,
        "ok": bool(ok),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    item.update(kwargs)
    return item


def run_connection_tests(cfg: Dict[str, Any], symbol: str = "", market: str = "", asset_kind: str = "") -> List[Dict[str, Any]]:
    """设置页接口连通性测试。每条测试只跑一次，不走业务层多次重试。"""
    import requests  # type: ignore

    options = fetch_options_from_cfg(cfg)
    raw_symbol = str(symbol or cfg.get("symbol") or "NVDA").strip() or "NVDA"
    raw_market = str(market or cfg.get("market") or "US").upper()
    raw_kind = str(asset_kind or cfg.get("asset_kind") or "stock").lower()
    if raw_market == "AUTO":
        raw_market = "CN" if re.fullmatch(r"\d{6}", raw_symbol) else "US"
    raw_kind = resolve_asset_kind(raw_symbol, raw_market, raw_kind, str(cfg.get("symbol_name") or ""))
    yahoo_symbol = (yahoo_symbol_candidates(raw_symbol, raw_market, raw_kind) or ["NVDA"])[0]
    danjuan_code = _norm_index_code(raw_symbol, raw_kind, str(cfg.get("symbol_name") or "")) or "SH000300"

    results: List[Dict[str, Any]] = []

    # 1) Yahoo Chart API：直接 HTTP，便于判断网络/代理是否能访问 Yahoo。
    started = time.time()
    yahoo_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        params = {"range": "1mo", "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
        resp = requests.get(yahoo_url, params=params, headers={"User-Agent": "Mozilla/5.0 trend-risk-position-tool"}, timeout=options.timeout, proxies=request_proxies(options))
        text = resp.text
        payload = resp.json() if text else {}
        chart = payload.get("chart") or {}
        result = (chart.get("result") or [None])[0]
        timestamps = result.get("timestamp") if result else []
        ok = resp.ok and bool(timestamps)
        results.append(_make_test_result(
            "yahoo_chart",
            resp.url,
            started,
            ok,
            status_code=resp.status_code,
            rows=len(timestamps or []),
            parsed={"symbol": yahoo_symbol, "rows": len(timestamps or [])},
            response_preview=_preview_text(payload),
            error=None if ok else _preview_text((chart.get("error") or {}) or text, 240),
        ))
    except Exception as e:
        results.append(_make_test_result("yahoo_chart", yahoo_url, started, False, error=str(e)[:300]))

    # 2) yfinance：测试 Python 库链路。先 download，再 Ticker.history 兜底。
    started = time.time()
    try:
        import yfinance as yf  # type: ignore
        with proxy_environment(options):
            try:
                hist = yf.download(yahoo_symbol, period="1mo", interval="1d", auto_adjust=True, progress=False, threads=False, timeout=options.timeout, multi_level_index=False)
            except TypeError:
                hist = yf.download(yahoo_symbol, period="1mo", interval="1d", auto_adjust=True, progress=False, threads=False, timeout=options.timeout)
            rows_download = 0 if hist is None else len(hist)
            used = "download"
            if rows_download <= 0:
                hist2 = yf.Ticker(yahoo_symbol).history(period="1mo", interval="1d", auto_adjust=True)
                if hist2 is not None and len(hist2) > 0:
                    hist = hist2
                    used = "Ticker.history"
        rows = 0 if hist is None else len(hist)
        preview = hist.tail(3).to_string() if rows else ""
        results.append(_make_test_result(
            "yfinance",
            f"yf.{used}({yahoo_symbol}, period=1mo)",
            started,
            rows > 0,
            status_code=None,
            rows=rows,
            parsed={"symbol": yahoo_symbol, "rows": rows, "method": used, "columns": list(map(str, getattr(hist, "columns", [])))[:12]},
            response_preview=_preview_text(preview),
            error=None if rows > 0 else "yfinance download 与 Ticker.history 均返回空数据；优先看 yahoo_chart 是否可用",
        ))
    except Exception as e:
        results.append(_make_test_result("yfinance", f"yf.download/Ticker.history({yahoo_symbol})", started, False, error=str(e)[:300]))

    # 3) 蛋卷 JSON：按你给的 /djapi/index_eva/detail/{code} 测试。
    started = time.time()
    danjuan_url = f"https://danjuanfunds.com/djapi/index_eva/detail/{danjuan_code}"
    try:
        payload, meta = fetch_danjuan_json_raw(danjuan_code, options)
        parsed = _extract_valuation_from_json(payload, f"danjuan_json:{danjuan_code}")
        results.append(_make_test_result(
            "danjuan_json",
            meta.get("url") or danjuan_url,
            started,
            True,
            status_code=meta.get("status_code"),
            rows=1,
            parsed={k: parsed.get(k) for k in ["current_pe", "pe_percentile", "current_pb", "pb_percentile", "roe_pct", "valuation_source"]},
            response_preview=meta.get("response_preview") or _preview_text(payload),
        ))
    except Exception as e:
        results.append(_make_test_result("danjuan_json", danjuan_url, started, False, error=str(e)[:300]))

    # 4) Stooq：美股历史行情备用链路。
    started = time.time()
    stooq_symbol = raw_symbol.lower() if "." in raw_symbol else f"{raw_symbol.lower()}.us"
    stooq_url = "https://stooq.com/q/d/l/"
    try:
        resp = requests.get(stooq_url, params={"s": stooq_symbol, "i": "d"}, headers={"User-Agent": "Mozilla/5.0 trend-risk-position-tool"}, timeout=options.timeout, proxies=request_proxies(options))
        text = resp.text.strip()
        lines = text.splitlines()
        ok = resp.ok and len(lines) > 3 and "No data" not in text[:80]
        results.append(_make_test_result(
            "stooq_csv",
            resp.url,
            started,
            ok,
            status_code=resp.status_code,
            rows=max(0, len(lines) - 1),
            parsed={"symbol": stooq_symbol, "rows": max(0, len(lines) - 1)},
            response_preview=_preview_text("\n".join(lines[:5])),
            error=None if ok else "Stooq 返回空数据或 No data",
        ))
    except Exception as e:
        results.append(_make_test_result("stooq_csv", stooq_url, started, False, error=str(e)[:300]))

    # 5) 东方财富 K线：国内行情备用链路。HTTPS + Referer/User-Agent，失败时再试 HTTP。
    started = time.time()
    em_symbol = re.sub(r"\D", "", raw_symbol) if re.fullmatch(r"\d{6}", re.sub(r"\D", "", raw_symbol)) else "510300"
    secid = eastmoney_secid(em_symbol)
    em_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    try:
        params = {
            "secid": secid, "klt": "101", "fqt": "1", "beg": "20240101", "end": "29991231", "lmt": "120",
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        tried: List[Dict[str, Any]] = []
        payload: Any = {}
        resp_url = em_url
        status_code = None
        klines: List[Any] = []
        for base_url in ["https://push2his.eastmoney.com/api/qt/stock/kline/get", "http://push2his.eastmoney.com/api/qt/stock/kline/get"]:
            resp = requests.get(base_url, params=params, headers=eastmoney_headers(), timeout=options.timeout, proxies=request_proxies(options))
            status_code = resp.status_code
            resp_url = resp.url
            text = resp.text
            try:
                payload = resp.json() if text else {}
            except Exception:
                payload = {"raw": text[:240]}
            klines = ((payload.get("data") or {}).get("klines") or []) if isinstance(payload, dict) else []
            tried.append({"url": resp.url, "status_code": resp.status_code, "rows": len(klines)})
            if resp.ok and klines:
                break
        ok = bool(klines)
        results.append(_make_test_result(
            "eastmoney_kline",
            resp_url,
            started,
            ok,
            status_code=status_code,
            rows=len(klines),
            parsed={"secid": secid, "rows": len(klines), "tried": tried},
            response_preview=_preview_text(payload),
            error=None if ok else "东方财富未返回 K 线；已尝试 HTTPS/HTTP 与常规 Referer",
        ))
    except Exception as e:
        results.append(_make_test_result("eastmoney_kline", em_url, started, False, error=str(e)[:300]))

    return results



# -----------------------------
# 历史回测模拟（网页端）
# -----------------------------

BACKTEST_METRIC_NOTES: Dict[str, str] = {
    "策略总收益": "期末策略权益相对初始资金的收益。",
    "买入持有总收益": "从回测起点全仓买入并持有到最后的收益，用作基准。",
    "定投策略总收益": "按回测操作周期把100%计划资金均分后定额买入，并持有到期末的收益。",
    "策略年化收益": "把策略总收益折算成年化结果。",
    "定投策略年化收益": "把定投策略总收益折算成年化结果。",
    "买入持有年化收益": "买入持有基准的年化结果。",
    "策略最大回撤": "策略权益从阶段高点到之后低点的最大跌幅。",
    "定投策略最大回撤": "定投基准权益从阶段高点到之后低点的最大跌幅。",
    "买入持有最大回撤": "买入持有基准的最大回撤。",
    "标的核心得分": "按长期资产质量、估值安全边际、风险与回撤、收益质量、产品与交易可用性综合评估这只股票/基金是否适合作为长期核心资产。",
    "系统策略得分": "按年化超额收益、最大回撤控制、夏普比率、卡玛比率、执行质量综合评估系统策略表现。",
    "定投策略得分": "按年化超额收益、最大回撤控制、夏普比率、卡玛比率、执行质量综合评估定投基准表现。",
    "持有策略得分": "按年化超额收益、最大回撤控制、夏普比率、卡玛比率、执行质量综合评估一次性买入持有基准表现。",
    "卡玛比率": "（策略年化收益 - 无风险收益率）/ 策略最大回撤绝对值，越高表示单位回撤换来的超额收益越高。",
    "定投策略卡玛比率": "（定投策略年化收益 - 无风险收益率）/ 定投策略最大回撤绝对值，用作定投基准的风险收益比。",
    "持有策略卡玛比率": "（买入持有年化收益 - 无风险收益率）/ 买入持有最大回撤绝对值，用作买入持有基准的风险收益比。",
    "无风险收益率": "用于夏普比率和卡玛比率的年化无风险收益率；默认 2%，可在设置页调整。",
    "年化波动": "日收益波动率年化。",
    "夏普比率": "按日收益序列计算年化超额收益 / 年化波动率，已扣除无风险收益率。",
    "定投策略夏普比率": "定投基准的年化超额收益 / 年化波动率，已扣除无风险收益率。",
    "持有策略夏普比率": "买入持有基准的年化超额收益 / 年化波动率，已扣除无风险收益率。",
    "交易次数": "回测期间实际执行的买入和卖出次数。",
    "已实现胜率": "只按卖出时已实现盈亏统计，未平仓浮盈浮亏不计入。",
    "盈亏因子": "已实现盈利总额 / 已实现亏损总额绝对值。",
    "平均仓位": "回测期间平均持仓暴露。",
    "换手率": "累计成交金额 / 初始资金。",
    "期末权益": "回测结束时策略账户权益。",

    "相对定投收益差": "策略总收益 - 定投策略总收益。为负表示策略输给定投。",
    "相对持有收益差": "策略总收益 - 买入持有总收益。为负表示策略输给长期持有。",
    "相对定投回撤改善": "定投最大回撤绝对值 - 策略最大回撤绝对值。为正表示策略比定投更抗跌。",
    "相对持有回撤改善": "买入持有最大回撤绝对值 - 策略最大回撤绝对值。为正表示策略比持有更抗跌。",
    "估算交易成本拖累": "按换手率、手续费和滑点粗略估计的收益拖累，用于判断是否过度交易。",
    "估算现金拖累": "当买入持有收益为正时，用平均低仓位粗略估计错过上涨的收益拖累。",
    "估值序列最新日期": "历史估值序列中用于对比的最新日期。",
    "估值页面最新日期": "蛋卷（雪球）detail 当前页面估值接口返回的对比日期；若接口未提供则显示 --。",
    "历史PE百分位": "用历史 PE 序列本地计算得到的最新 PE 百分位。",
    "页面PE百分位": "蛋卷（雪球）detail 当前页面直接返回的 PE 百分位。",
    "PE百分位误差": "历史序列自算 PE 百分位 - 页面直接 PE 百分位。",
    "历史PB": "历史 PB 序列最新值。",
    "页面PB": "蛋卷（雪球）detail 当前页面直接返回的 PB。",
    "PB误差": "历史 PB 最新值 - 页面 PB。",
    "历史PB百分位": "用历史 PB 序列本地计算得到的最新 PB 百分位。",
    "页面PB百分位": "蛋卷（雪球）detail 当前页面直接返回的 PB 百分位。",
    "PB百分位误差": "历史序列自算 PB 百分位 - 页面直接 PB 百分位。",
    "历史ROE": "历史 ROE 序列最新值。",
    "页面ROE": "蛋卷（雪球）detail 当前页面直接返回的 ROE。",
    "ROE误差": "历史 ROE 最新值 - 页面 ROE。",
}


def parse_date_safe(value: Any, default: Optional[date] = None) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        if default is not None:
            return default
        raise


def normalize_history_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for r in records:
        try:
            raw_date = r.get("date") or r.get("Date") or r.get("日期")
            d = parse_date_safe(raw_date).isoformat()
            close = as_float(r.get("close", r.get("Close", r.get("收盘"))), 0.0)
            if close <= 0:
                continue
            open_ = as_float(r.get("open", r.get("Open", r.get("开盘"))), close)
            high = as_float(r.get("high", r.get("High", r.get("最高"))), close)
            low = as_float(r.get("low", r.get("Low", r.get("最低"))), close)
            volume = as_float(r.get("volume", r.get("Volume", r.get("成交量"))), 0.0)
            amount = as_float(r.get("amount", r.get("Amount", r.get("成交额"))), 0.0)
            cleaned.append({"date": d, "open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount})
        except Exception:
            continue
    dedup = {r["date"]: r for r in cleaned}
    return [dedup[k] for k in sorted(dedup)]




HISTORY_PRICE_RAW_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
HISTORY_PRICE_DERIVED_COLUMNS = [
    "ma20", "ma50", "ma200", "atr14", "atr_pct", "return_20d", "return_60d", "return_120d",
    "volatility_20d", "volatility_60d", "drawdown_252d", "ma50_slope_20d", "ma200_slope_20d",
    "distance_ma50_pct", "distance_ma200_pct", "volume_ratio_20d",
    "macd_dif", "macd_dea", "macd_bar", "macd_dif_pct", "macd_dea_pct", "macd_bar_pct",
    "rsi6", "rsi14", "boll_mid", "boll_upper", "boll_lower", "boll_width_pct", "boll_percent_b",
]
HISTORY_VALUATION_COLUMNS = [
    "date", "current_pe", "pe_percentile", "current_pb", "pb_percentile", "roe_pct",
    "valuation_source", "valuation_note", "valuation_extra_note", "valuation_date",
]


def history_cache_enabled(cfg: Dict[str, Any]) -> bool:
    """默认开启历史落盘缓存；除非配置里显式写 history_cache_enabled=false。"""
    value = cfg.get("history_cache_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no", "关闭", "否"}
    return bool(value)


def _cache_safe_part(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    raw = re.sub(r"\s+", "_", raw)
    raw = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_.-]+", "_", raw)
    return raw[:80] or "unknown"


def _history_cache_file(folder: str, *parts: Any) -> str:
    os.makedirs(folder, exist_ok=True)
    name = "__".join(_cache_safe_part(p) for p in parts) + ".csv"
    return os.path.join(folder, name)


def _history_meta_path(csv_path: str) -> str:
    return csv_path[:-4] + ".meta.json" if csv_path.endswith(".csv") else csv_path + ".meta.json"


def _read_history_meta(csv_path: str) -> Dict[str, Any]:
    try:
        with open(_history_meta_path(csv_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_history_meta(csv_path: str, meta: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        payload = dict(meta or {})
        payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_history_meta_path(csv_path), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _read_history_csv(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    return rows


def _write_history_csv(path: str, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    all_columns = list(columns)
    for row in rows:
        for key in row.keys():
            if key not in all_columns:
                all_columns.append(key)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in all_columns})


def _date_range_of_rows(rows: List[Dict[str, Any]]) -> Tuple[Optional[date], Optional[date]]:
    dates: List[date] = []
    for row in rows:
        try:
            dates.append(parse_date_safe(row.get("date") or row.get("日期")))
        except Exception:
            continue
    if not dates:
        return None, None
    return min(dates), max(dates)


def _filter_history_rows(rows: List[Dict[str, Any]], start: date, end: date) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            ds = parse_date_safe(row.get("date") or row.get("日期"))
        except Exception:
            continue
        if start <= ds <= end:
            out.append(row)
    return sorted(out, key=lambda r: str(r.get("date") or r.get("日期") or ""))


def _merge_history_rows(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for rows in groups:
        for row in rows or []:
            try:
                ds = parse_date_safe(row.get("date") or row.get("日期")).isoformat()
            except Exception:
                continue
            item = dict(merged.get(ds, {}))
            # 新数据覆盖旧数据中的空值，也覆盖原始行情字段，确保网络补抓后的修正可以生效。
            for key, value in row.items():
                if value not in (None, "") or key not in item:
                    item[key] = value
            item["date"] = ds
            merged[ds] = item
    return [merged[k] for k in sorted(merged)]


def _cache_missing_ranges(cached_rows: List[Dict[str, Any]], start: date, end: date) -> List[Tuple[date, date]]:
    min_d, max_d = _date_range_of_rows(cached_rows)
    if min_d is None or max_d is None:
        return [(start, end)]
    ranges: List[Tuple[date, date]] = []
    if start < min_d:
        ranges.append((start, min(end, min_d - timedelta(days=1))))
    if end > max_d:
        ranges.append((max(start, max_d + timedelta(days=1)), end))
    return [(a, b) for a, b in ranges if a <= b]


def _expand_missing_range_for_fetch(
    missing_start: date,
    missing_end: date,
    cached_rows: List[Dict[str, Any]],
    requested_start: date,
    requested_end: date,
    is_fund: bool,
) -> Tuple[date, date]:
    """短缺口也要能补。

    旧的网络抓取函数会要求至少 20/60 条记录；如果只缺最近三天，直接抓三天会被判定数据太少。
    因此这里允许向缓存重叠区扩展一小段，只补必要方向附近的数据，不回到整段重抓。
    """
    min_d, max_d = _date_range_of_rows(cached_rows)
    buffer_days = 90 if is_fund else 180
    fetch_start, fetch_end = missing_start, missing_end
    if min_d and missing_end < min_d:
        fetch_end = min(requested_end, missing_end + timedelta(days=buffer_days))
    if max_d and missing_start > max_d:
        fetch_start = max(requested_start, missing_start - timedelta(days=buffer_days))
    return fetch_start, fetch_end


def _price_cache_key(symbol: str, market: str, source: str, asset_kind: str, cfg: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    return (
        "price_v2",
        str(market or "auto").upper(),
        str(resolve_asset_kind(symbol, market, asset_kind, str(cfg.get("symbol_name") or "")) or "auto").lower(),
        normalize_backtest_source(source, market),
        str(symbol or "").strip().upper() if re.search(r"[A-Za-z]", str(symbol or "")) else re.sub(r"\D", "", str(symbol or "")) or str(symbol or "").strip(),
    )


def _valuation_cache_key(symbol: str, market: str, asset_kind: str, cfg: Dict[str, Any], symbol_name: str) -> Tuple[str, str, str, str, str, str]:
    return (
        "valuation_v2",
        str(market or "auto").upper(),
        str(resolve_asset_kind(symbol, market, asset_kind, symbol_name) or "auto").lower(),
        str(cfg.get("valuation_method") or "system_calc"),
        str(symbol or "").strip().upper() if re.search(r"[A-Za-z]", str(symbol or "")) else re.sub(r"\D", "", str(symbol or "")) or str(symbol or "").strip(),
        _cache_safe_part(symbol_name or "noname"),
    )


def _as_optional_float_cell(value: Any) -> Any:
    if value in (None, ""):
        return ""
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return ""
        return round(v, 6)
    except Exception:
        return value


def build_trend_chart_series(records: List[Dict[str, Any]], limit: int = 260) -> List[Dict[str, Any]]:
    """把本次拉取到的行情序列整理成前端趋势图可直接绘制的数据。

    只使用已有行情 records 计算，不新增网络请求；limit 默认约等于一年交易日，
    保留少量常用技术字段，避免 /api/fetch 返回体过大。
    """
    try:
        enriched = enrich_history_records_for_cache(records)
    except Exception:
        enriched = normalize_history_records(records)
    keys = [
        "date",
        "close",
        "ma20",
        "ma50",
        "ma200",
        "drawdown_252d",
        "rsi14",
        "macd_bar_pct",
        "return_60d",
    ]
    out: List[Dict[str, Any]] = []
    for row in enriched[-max(int(limit or 260), 60):]:
        item: Dict[str, Any] = {}
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                item[key] = None
                continue
            if key == "date":
                item[key] = str(value)[:10]
                continue
            try:
                v = float(value)
                item[key] = None if math.isnan(v) or math.isinf(v) else round(v, 6)
            except Exception:
                item[key] = value
        if item.get("date") and item.get("close") is not None:
            out.append(item)
    return out


def enrich_history_records_for_cache(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保存行情/净值时同步写入常用技术衍生字段。

    回测仍会按当日以前数据动态计算信号，避免未来函数；这些字段主要用于下次读取、人工检查、
    以及后续策略直接复用缓存表，不必重复从网络拉取。每一行的指标只使用该行及以前的数据。
    """
    base = normalize_history_records(records)
    closes = [float(r["close"]) for r in base]
    highs = [float(r.get("high") or r.get("close") or 0.0) for r in base]
    lows = [float(r.get("low") or r.get("close") or 0.0) for r in base]
    vols = [float(r.get("volume") or 0.0) for r in base]

    def sma_at(values: List[float], idx: int, n: int) -> Optional[float]:
        if idx + 1 < n:
            return None
        return sum(values[idx - n + 1: idx + 1]) / n

    def ret_at(idx: int, n: int) -> Optional[float]:
        if idx < n or closes[idx - n] <= 0:
            return None
        return closes[idx] / closes[idx - n] - 1.0

    def ann_vol_at(idx: int, n: int) -> Optional[float]:
        if idx < n:
            return None
        rets: List[float] = []
        for j in range(idx - n + 1, idx + 1):
            prev = closes[j - 1]
            if prev > 0:
                rets.append(closes[j] / prev - 1.0)
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(max(var, 0.0)) * math.sqrt(252.0)

    def ema_values(values: List[float], n: int) -> List[Optional[float]]:
        if not values or n <= 0:
            return []
        alpha = 2.0 / (n + 1.0)
        out: List[Optional[float]] = []
        ema: Optional[float] = None
        for idx, value in enumerate(values):
            if idx + 1 < n:
                out.append(None)
                continue
            if idx + 1 == n:
                ema = sum(values[:n]) / n
            else:
                ema = value * alpha + float(ema) * (1.0 - alpha)
            out.append(ema)
        return out

    def macd_at(idx: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if idx < 34:
            return None, None, None
        window = closes[:idx + 1]
        ema12 = ema_values(window, 12)
        ema26 = ema_values(window, 26)
        dif_values: List[float] = []
        for a, b in zip(ema12, ema26):
            if a is not None and b is not None:
                dif_values.append(a - b)
        dea_values = ema_values(dif_values, 9)
        if not dif_values or not dea_values or dea_values[-1] is None:
            return None, None, None
        dif = dif_values[-1]
        dea = float(dea_values[-1])
        return dif, dea, (dif - dea) * 2.0

    def rsi_at(idx: int, n: int) -> Optional[float]:
        if idx < n:
            return None
        gains: List[float] = []
        losses: List[float] = []
        for j in range(idx - n + 1, idx + 1):
            diff = closes[j] - closes[j - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains) / n
        avg_loss = sum(losses) / n
        if avg_loss <= 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def boll_at(idx: int, n: int = 20, k: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        if idx + 1 < n:
            return None, None, None, None, None
        window = closes[idx - n + 1: idx + 1]
        mid = sum(window) / n
        var = sum((v - mid) ** 2 for v in window) / n
        std = math.sqrt(max(var, 0.0))
        upper = mid + k * std
        lower = mid - k * std
        width_pct = (upper - lower) / mid * 100.0 if mid else None
        percent_b = (closes[idx] - lower) / (upper - lower) * 100.0 if upper > lower else 50.0
        return mid, upper, lower, width_pct, percent_b

    trs: List[float] = [0.0]
    for idx in range(1, len(base)):
        prev_close = closes[idx - 1]
        trs.append(max(highs[idx] - lows[idx], abs(highs[idx] - prev_close), abs(lows[idx] - prev_close)))

    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(base):
        close = closes[idx]
        ma20 = sma_at(closes, idx, 20)
        ma50 = sma_at(closes, idx, 50)
        ma200 = sma_at(closes, idx, 200)
        ma50_prev = sma_at(closes, idx - 20, 50) if idx >= 20 else None
        ma200_prev = sma_at(closes, idx - 20, 200) if idx >= 20 else None
        atr14 = sum(trs[idx - 13: idx + 1]) / 14 if idx >= 14 else None
        high_252 = max(closes[max(0, idx - 251): idx + 1]) if idx >= 0 else None
        vol20 = sum(vols[idx - 19: idx + 1]) / 20 if idx >= 19 and any(vols[idx - 19: idx + 1]) else None
        macd_dif, macd_dea, macd_bar = macd_at(idx)
        rsi6 = rsi_at(idx, 6)
        rsi14 = rsi_at(idx, 14)
        boll_mid, boll_upper, boll_lower, boll_width_pct, boll_percent_b = boll_at(idx, 20, 2.0)

        enriched = dict(row)
        enriched.update({
            "ma20": _as_optional_float_cell(ma20),
            "ma50": _as_optional_float_cell(ma50),
            "ma200": _as_optional_float_cell(ma200),
            "atr14": _as_optional_float_cell(atr14),
            "atr_pct": _as_optional_float_cell((atr14 / close * 100.0) if atr14 and close else None),
            "return_20d": _as_optional_float_cell(ret_at(idx, 20) * 100.0 if ret_at(idx, 20) is not None else None),
            "return_60d": _as_optional_float_cell(ret_at(idx, 60) * 100.0 if ret_at(idx, 60) is not None else None),
            "return_120d": _as_optional_float_cell(ret_at(idx, 120) * 100.0 if ret_at(idx, 120) is not None else None),
            "volatility_20d": _as_optional_float_cell(ann_vol_at(idx, 20) * 100.0 if ann_vol_at(idx, 20) is not None else None),
            "volatility_60d": _as_optional_float_cell(ann_vol_at(idx, 60) * 100.0 if ann_vol_at(idx, 60) is not None else None),
            "drawdown_252d": _as_optional_float_cell((close / high_252 - 1.0) * 100.0 if high_252 and high_252 > 0 else None),
            "ma50_slope_20d": _as_optional_float_cell((ma50 / ma50_prev - 1.0) * 100.0 if ma50 and ma50_prev and ma50_prev > 0 else None),
            "ma200_slope_20d": _as_optional_float_cell((ma200 / ma200_prev - 1.0) * 100.0 if ma200 and ma200_prev and ma200_prev > 0 else None),
            "distance_ma50_pct": _as_optional_float_cell((close / ma50 - 1.0) * 100.0 if ma50 else None),
            "distance_ma200_pct": _as_optional_float_cell((close / ma200 - 1.0) * 100.0 if ma200 else None),
            "volume_ratio_20d": _as_optional_float_cell((vols[idx] / vol20) if vol20 else None),
            "macd_dif": _as_optional_float_cell(macd_dif),
            "macd_dea": _as_optional_float_cell(macd_dea),
            "macd_bar": _as_optional_float_cell(macd_bar),
            "macd_dif_pct": _as_optional_float_cell((macd_dif / close * 100.0) if macd_dif is not None and close else None),
            "macd_dea_pct": _as_optional_float_cell((macd_dea / close * 100.0) if macd_dea is not None and close else None),
            "macd_bar_pct": _as_optional_float_cell((macd_bar / close * 100.0) if macd_bar is not None and close else None),
            "rsi6": _as_optional_float_cell(rsi6),
            "rsi14": _as_optional_float_cell(rsi14),
            "boll_mid": _as_optional_float_cell(boll_mid),
            "boll_upper": _as_optional_float_cell(boll_upper),
            "boll_lower": _as_optional_float_cell(boll_lower),
            "boll_width_pct": _as_optional_float_cell(boll_width_pct),
            "boll_percent_b": _as_optional_float_cell(boll_percent_b),
        })
        out.append(enriched)
    return out


def _load_price_history_cache(path: str) -> List[Dict[str, Any]]:
    rows = _read_history_csv(path)
    if not rows:
        return []
    # 保留衍生列，同时把基础 OHLCV 标准化成数值。
    normalized = normalize_history_records(rows)
    extra_by_date = {str(r.get("date") or r.get("日期") or "")[:10]: r for r in rows}
    out: List[Dict[str, Any]] = []
    for row in normalized:
        extra = extra_by_date.get(row["date"], {})
        merged = dict(row)
        for key in HISTORY_PRICE_DERIVED_COLUMNS:
            if key in extra:
                merged[key] = _as_optional_float_cell(extra.get(key))
        out.append(merged)
    return out


def _save_price_history_cache(path: str, rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    enriched = enrich_history_records_for_cache(rows)
    _write_history_csv(path, enriched, HISTORY_PRICE_RAW_COLUMNS + HISTORY_PRICE_DERIVED_COLUMNS)
    _write_history_meta(path, meta)


def backtest_fetch_records(
    symbol: str,
    market: str,
    source: str,
    start: date,
    end: date,
    cfg: Dict[str, Any],
    asset_kind: str = "",
) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """带落盘缓存的历史行情/净值抓取。

    第一次回测：网络下载 -> data/history_cache/prices/*.csv。
    后续回测：若日期已覆盖则直接读本地；若只缺前后区间，则只补缺失方向附近的数据并合并去重。
    """
    if not history_cache_enabled(cfg):
        return backtest_fetch_records_uncached(symbol, market, source, start, end, cfg, asset_kind)

    resolved_kind = resolve_asset_kind(symbol, market, asset_kind, str(cfg.get("symbol_name") or ""))
    is_fund = is_cn_open_fund_like(symbol, market, resolved_kind, str(cfg.get("symbol_name") or ""))
    key = _price_cache_key(symbol, market, source, resolved_kind, cfg)
    path = _history_cache_file(PRICE_HISTORY_CACHE_DIR, *key)
    cached_rows = _load_price_history_cache(path)
    meta = _read_history_meta(path)
    fetch_errors: List[str] = []
    missing = _cache_missing_ranges(cached_rows, start, end)

    if not missing:
        subset = _filter_history_rows(cached_rows, start, end)
        label = str(meta.get("source_label") or "price")
        fetch_errors.append(f"历史行情缓存命中：{os.path.relpath(path, APP_DIR)}，本地 {len(cached_rows)} 行，回测使用 {len(subset)} 行")
        return normalize_history_records(subset), f"local_cache:{label}", fetch_errors

    all_new: List[Dict[str, Any]] = []
    source_labels: List[str] = []
    for miss_start, miss_end in missing:
        net_start, net_end = _expand_missing_range_for_fetch(miss_start, miss_end, cached_rows, start, end, is_fund)
        try:
            rows, label, errors = backtest_fetch_records_uncached(symbol, market, source, net_start, net_end, cfg, resolved_kind)
            all_new.extend(rows)
            source_labels.append(label)
            fetch_errors.extend(errors)
            fetch_errors.append(f"历史行情缓存补充：{net_start} ~ {net_end}，来源 {label}，新增/覆盖 {len(rows)} 行")
        except Exception as exc:
            fetch_errors.append(f"历史行情缓存补充失败：{net_start} ~ {net_end}：{str(exc)[:220]}")

    if all_new:
        merged = _merge_history_rows(cached_rows, all_new)
        label = "+".join(sorted(set(source_labels))) or str(meta.get("source_label") or normalize_backtest_source(source, market))
        _save_price_history_cache(path, merged, {
            "type": "price_or_nav",
            "symbol": symbol,
            "market": market,
            "asset_kind": resolved_kind,
            "source": normalize_backtest_source(source, market),
            "source_label": label,
            "start": _date_range_of_rows(merged)[0].isoformat() if _date_range_of_rows(merged)[0] else "",
            "end": _date_range_of_rows(merged)[1].isoformat() if _date_range_of_rows(merged)[1] else "",
            "rows": len(merged),
        })
        cached_rows = _load_price_history_cache(path)
        meta = _read_history_meta(path)
    elif not cached_rows:
        raise RuntimeError("历史行情网络下载失败，且本地没有可用缓存：" + "；".join(fetch_errors))

    subset = _filter_history_rows(cached_rows, start, end)
    if not subset:
        raise RuntimeError("本地历史行情缓存没有覆盖本次回测区间，且网络补充失败：" + "；".join(fetch_errors))
    label = str(meta.get("source_label") or (source_labels[-1] if source_labels else "price"))
    fetch_errors.append(f"历史行情缓存已保存：{os.path.relpath(path, APP_DIR)}，本地 {len(cached_rows)} 行，回测使用 {len(subset)} 行")
    return normalize_history_records(subset), f"cache+{label}", fetch_errors


def _valuation_series_to_rows(series: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for ds in sorted(series or {}):
        item = dict(series.get(ds) or {})
        item["date"] = ds
        rows.append(item)
    return rows


def _rows_to_valuation_series(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        try:
            ds = parse_date_safe(row.get("date") or row.get("valuation_date")).isoformat()
        except Exception:
            continue
        item: Dict[str, Any] = {}
        for key in HISTORY_VALUATION_COLUMNS:
            if key == "date":
                continue
            value = row.get(key)
            if value in (None, ""):
                continue
            if key in {"current_pe", "pe_percentile", "current_pb", "pb_percentile", "roe_pct"}:
                item[key] = as_float(value, None)  # type: ignore[arg-type]
            else:
                item[key] = value
        out[ds] = item
    return out


def _load_valuation_history_cache(path: str) -> Dict[str, Dict[str, Any]]:
    return _rows_to_valuation_series(_read_history_csv(path))


def _save_valuation_history_cache(path: str, series: Dict[str, Dict[str, Any]], meta: Dict[str, Any]) -> None:
    rows = _valuation_series_to_rows(series)
    _write_history_csv(path, rows, HISTORY_VALUATION_COLUMNS)
    _write_history_meta(path, meta)


def _valuation_cache_has_coverage(series: Dict[str, Dict[str, Any]], start: Optional[date], end: Optional[date]) -> bool:
    if not start or not end or not series:
        return bool(series)
    rows = _valuation_series_to_rows(series)
    min_d, max_d = _date_range_of_rows(rows)
    return bool(min_d and max_d and min_d <= start and max_d >= end)


def fetch_historical_valuation_series(
    symbol: str,
    market: str,
    asset_kind: str,
    cfg: Dict[str, Any],
    symbol_name: str = "",
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """带落盘缓存的历史估值序列。

    多数估值接口本身返回全历史序列，不一定支持按日期补段；这里优先本地命中，未覆盖时刷新并合并。
    """
    if not history_cache_enabled(cfg):
        return fetch_historical_valuation_series_uncached(symbol, market, asset_kind, cfg, symbol_name)

    key = _valuation_cache_key(symbol, market, asset_kind, cfg, symbol_name)
    path = _history_cache_file(VALUATION_HISTORY_CACHE_DIR, *key)
    cached = _load_valuation_history_cache(path)
    trace: List[str] = []
    if _valuation_cache_has_coverage(cached, start, end):
        trace.append(f"历史估值缓存命中：{os.path.relpath(path, APP_DIR)}，本地 {len(cached)} 行")
        return cached, trace

    try:
        fresh, raw_trace = fetch_historical_valuation_series_uncached(symbol, market, asset_kind, cfg, symbol_name)
        trace.extend(raw_trace)
        merged = merge_valuation_series(cached, fresh) if cached else fresh
        if merged:
            _save_valuation_history_cache(path, merged, {
                "type": "valuation",
                "symbol": symbol,
                "market": market,
                "asset_kind": resolve_asset_kind(symbol, market, asset_kind, symbol_name),
                "valuation_method": str(cfg.get("valuation_method") or "system_calc"),
                "symbol_name": symbol_name,
                "start": min(merged.keys()) if merged else "",
                "end": max(merged.keys()) if merged else "",
                "rows": len(merged),
            })
            trace.append(f"历史估值缓存已保存：{os.path.relpath(path, APP_DIR)}，本地 {len(merged)} 行")
            return merged, trace
        return fresh, trace
    except Exception as exc:
        if cached:
            trace.append(f"历史估值网络刷新失败，改用本地缓存：{str(exc)[:180]}；缓存 {len(cached)} 行")
            return cached, trace
        raise

def backtest_yahoo_symbol(symbol: str, market: str) -> str:
    s = str(symbol or "").strip()
    if market.upper() == "CN" and re.fullmatch(r"\d{6}", s):
        # Yahoo 对国内标的需要交易所后缀：沪市 .SS，深市 .SZ。
        # 常见指数如 000300/000016/000905/000852/000688 属于上交所指数，不能按普通 0 开头股票映射到 .SZ。
        if s.startswith(("5", "6", "9", "000")):
            return f"{s}.SS"
        return f"{s}.SZ"
    return s


def backtest_fetch_yahoo(symbol: str, market: str, start: date, end: date, options: FetchOptions) -> Tuple[List[Dict[str, Any]], str]:
    import requests  # type: ignore
    ys = backtest_yahoo_symbol(symbol, market)
    p1 = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}"
    params = {"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
    headers = {"User-Agent": "Mozilla/5.0 trend-risk-backtest/1.0", "Accept": "application/json,*/*"}
    resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    resp.raise_for_status()
    payload = resp.json()
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo Chart 无数据：{payload.get('chart', {}).get('error')}")
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    records: List[Dict[str, Any]] = []
    for i, t in enumerate(ts):
        close_arr = quote.get("close") or []
        if i >= len(close_arr) or close_arr[i] is None:
            continue
        def q(name: str, default: float) -> float:
            arr = quote.get(name) or []
            return float(arr[i]) if i < len(arr) and arr[i] is not None else float(default)
        close = float(close_arr[i])
        records.append({
            "date": datetime.fromtimestamp(t, timezone.utc).date().isoformat(),
            "open": q("open", close),
            "high": q("high", close),
            "low": q("low", close),
            "close": close,
            "volume": q("volume", 0.0),
        })
    return normalize_history_records(records), "yahoo_chart"


def backtest_fetch_stooq(symbol: str, market: str, start: date, end: date, options: FetchOptions) -> Tuple[List[Dict[str, Any]], str]:
    import requests  # type: ignore
    s = str(symbol or "").strip().lower()
    if market.upper() == "US" and "." not in s and not s.startswith("^"):
        s = f"{s}.us"
    url = "https://stooq.com/q/d/l/"
    params = {"s": s, "i": "d", "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")}
    headers = {"User-Agent": "Mozilla/5.0 trend-risk-backtest/1.0", "Accept": "text/csv,*/*"}
    resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
    resp.raise_for_status()
    text = resp.text
    if "No data" in text or len(text.strip().splitlines()) <= 1:
        raise RuntimeError("Stooq 无数据")
    rows = list(csv.DictReader(StringIO(text)))
    return normalize_history_records(rows), "stooq_csv"


def backtest_fetch_eastmoney(symbol: str, start: date, end: date, options: FetchOptions) -> Tuple[List[Dict[str, Any]], str]:
    import requests  # type: ignore
    if not re.fullmatch(r"\d{6}", str(symbol or "")):
        raise RuntimeError("东方财富历史K线仅支持 6 位国内代码")
    params = {
        "secid": eastmoney_secid(str(symbol)),
        "klt": "101",
        "fqt": "1",
        "beg": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "lmt": "1000000",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    last_error = ""
    for base_url in ["https://push2his.eastmoney.com/api/qt/stock/kline/get", "http://push2his.eastmoney.com/api/qt/stock/kline/get"]:
        try:
            resp = requests.get(base_url, params=params, headers=eastmoney_headers(), timeout=options.timeout, proxies=request_proxies(options))
            resp.raise_for_status()
            payload = resp.json()
            klines = ((payload.get("data") or {}).get("klines") or [])
            records = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 7:
                    continue
                records.append({"date": parts[0], "open": parts[1], "close": parts[2], "high": parts[3], "low": parts[4], "volume": parts[5]})
            out = normalize_history_records(records)
            if out:
                return out, "eastmoney_kline"
            last_error = "东方财富返回空K线"
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "东方财富历史K线失败")




def _strip_html_text(text: str) -> str:
    """把天天基金 F10DataApi 返回的 HTML 单元格转成纯文本。"""
    cleaned = re.sub(r"<[^>]+>", "", str(text or ""))
    return html_lib.unescape(cleaned).strip()


def backtest_fetch_eastmoney_fund_nav(symbol: str, start: date, end: date, options: FetchOptions) -> Tuple[List[Dict[str, Any]], str]:
    """天天基金历史净值：用于场外基金 / ETF联接 / QDII基金回测。

    这类标的没有股票K线意义上的 open/high/low/volume，回测中用单位净值作为 close，
    open/high/low 同步设为 close，volume 设为 0。这样能回测趋势/仓位逻辑，但量价辅助会自然弱化。

    v58 修复点：东方财富历史净值接口经常把 pageSize 限制为 20。如果请求 pageSize=200，
    旧代码会误以为"20 < 200 = 最后一页"，导致只抓第一页。这里统一按 20 条分页，并持续
    翻页到空页/重复页/超过页数为止；同时增加 TiantianFundApi 的 fundMNHisNetList 兜底。
    """
    import requests  # type: ignore

    code = re.sub(r"\D", "", str(symbol or ""))
    if not re.fullmatch(r"\d{6}", code):
        raise RuntimeError("天天基金历史净值仅支持 6 位基金代码")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/javascript,*/*;q=0.01",
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html",
        "Connection": "keep-alive",
    }

    def nav_to_records(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        seen_dates = set()
        for item in items:
            d = item.get("FSRQ") or item.get("净值日期") or item.get("date")
            nav = item.get("DWJZ") or item.get("单位净值") or item.get("LJJZ") or item.get("累计净值") or item.get("close")
            try:
                ds = parse_date_safe(d).isoformat()
                if ds in seen_dates:
                    continue
                close = as_float(nav, 0.0)
                if close <= 0:
                    continue
                seen_dates.add(ds)
                records.append({"date": ds, "open": close, "high": close, "low": close, "close": close, "volume": 0.0})
            except Exception:
                continue
        return normalize_history_records(records)

    def should_continue_by_dates(items: List[Dict[str, Any]], seen_dates: set) -> Tuple[bool, int]:
        """返回 (是否继续翻页, 新增日期数量)。接口通常按日期倒序返回。"""
        new_count = 0
        oldest = None
        for item in items:
            d = item.get("FSRQ") or item.get("净值日期") or item.get("date")
            try:
                dd = parse_date_safe(d)
                oldest = dd if oldest is None else min(oldest, dd)
                if dd.isoformat() not in seen_dates:
                    new_count += 1
                    seen_dates.add(dd.isoformat())
            except Exception:
                continue
        # 如果接口没有严格按日期过滤，翻到早于预热起点即可停止；否则继续直到空页/重复页。
        if oldest is not None and oldest < start:
            return False, new_count
        return new_count > 0, new_count

    errors: List[str] = []

    # 1) 东方财富 JSON 接口：分页拉取历史净值。
    # 注意：该接口常把 pageSize 限制为 20，不能用 len(items) < page_size 判断结束。
    try:
        url = "https://api.fund.eastmoney.com/f10/lsjz"
        all_items: List[Dict[str, Any]] = []
        page_size = 20
        page_count = None
        seen_dates: set = set()
        for page in range(1, 501):
            params = {
                "fundCode": code,
                "pageIndex": page,
                "pageSize": page_size,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            }
            resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("Data") if isinstance(payload, dict) else None
            data = data or {}
            items = data.get("LSJZList") or []
            if page_count is None:
                for key in ("PageCount", "Pages", "TotalPage"):
                    try:
                        if data.get(key):
                            page_count = int(data.get(key))
                            break
                    except Exception:
                        pass
            if not items:
                break
            all_items.extend(items)
            if page_count and page >= page_count:
                break
            keep_going, new_count = should_continue_by_dates(items, seen_dates)
            if not keep_going:
                break
        records = nav_to_records(all_items)
        if records:
            return records, "eastmoney_fund_nav"
        errors.append(f"api.fund.eastmoney.com 返回空净值 rows={len(all_items)}")
    except Exception as exc:
        errors.append(f"api.fund.eastmoney.com 失败：{str(exc)[:180]}")

    # 2) 老 F10DataApi 接口：返回 JS/HTML，分页兜底。
    try:
        url = "https://fundf10.eastmoney.com/F10DataApi.aspx"
        rows: List[Dict[str, Any]] = []
        page_count = None
        per = 20
        seen_dates: set = set()
        for page in range(1, 501):
            params = {
                "type": "lsjz",
                "code": code,
                "page": page,
                "per": per,
                "sdate": start.isoformat(),
                "edate": end.isoformat(),
            }
            resp = requests.get(url, params=params, headers=headers, timeout=options.timeout, proxies=request_proxies(options))
            resp.raise_for_status()
            text = resp.text or ""
            if page_count is None:
                m = re.search(r"pages\s*[:=]\s*(\d+)", text, flags=re.I)
                if m:
                    try:
                        page_count = int(m.group(1))
                    except Exception:
                        page_count = None
            page_items: List[Dict[str, Any]] = []
            for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
                if len(cells) < 2:
                    continue
                d = _strip_html_text(cells[0])
                nav = _strip_html_text(cells[1])
                if re.match(r"\d{4}-\d{2}-\d{2}", d):
                    page_items.append({"FSRQ": d, "DWJZ": nav})
            if not page_items:
                break
            rows.extend(page_items)
            if page_count and page >= page_count:
                break
            keep_going, new_count = should_continue_by_dates(page_items, seen_dates)
            if not keep_going:
                break
        records = nav_to_records(rows)
        if records:
            return records, "eastmoney_fund_nav_f10"
        errors.append(f"fundf10 F10DataApi 未解析到净值 rows={len(rows)}")
    except Exception as exc:
        errors.append(f"fundf10 F10DataApi 失败：{str(exc)[:180]}")

    # 3) TiantianFundApi 文档中的 fundMNHisNetList 路由兜底。
    # 这是第三方封装服务，优先级低于东方财富原始接口；主要用于排查/兜底场外基金净值分页问题。
    try:
        url = "https://tiantian-fund-api.vercel.app/api/action"
        all_items: List[Dict[str, Any]] = []
        seen_dates: set = set()
        page_size = 30
        for page in range(1, 501):
            params = {
                "action_name": "fundMNHisNetList",
                "FCODE": code,
                "pageIndex": page,
                "pagesize": page_size,
            }
            resp = requests.get(url, params=params, headers={**headers, "Referer": "https://kouchao.github.io/TiantianFundApi/apis/"}, timeout=options.timeout, proxies=request_proxies(options))
            resp.raise_for_status()
            payload = resp.json()
            items = []
            if isinstance(payload, dict):
                items = payload.get("Datas") or payload.get("data") or payload.get("Data") or []
                if isinstance(items, dict):
                    items = items.get("Datas") or items.get("LSJZList") or []
            if not items:
                break
            # 文档接口未必支持日期过滤，因此这里手动截取日期范围。
            filtered: List[Dict[str, Any]] = []
            for item in items:
                try:
                    dd = parse_date_safe(item.get("FSRQ") or item.get("date"))
                    if dd <= end:
                        filtered.append(item)
                except Exception:
                    continue
            all_items.extend(filtered)
            keep_going, new_count = should_continue_by_dates(filtered or items, seen_dates)
            if not keep_going:
                break
        records = [r for r in nav_to_records(all_items) if start <= parse_date_safe(r["date"]) <= end]
        # 这里不要只返回过滤后的 start/end；回测预热需要 start 之前的数据，所以重新按 fetch_start/end 过滤。
        records = nav_to_records([item for item in all_items])
        records = [r for r in records if start <= parse_date_safe(r["date"]) <= end]
        if records:
            return records, "tiantian_fund_api"
        errors.append(f"TiantianFundApi 未返回有效净值 rows={len(all_items)}")
    except Exception as exc:
        errors.append(f"TiantianFundApi 失败：{str(exc)[:180]}")

    raise RuntimeError("天天基金历史净值失败：" + "；".join(errors))

def normalize_backtest_source(source: str, market: str) -> str:
    """把前端/设置页的数据源名称转换成回测历史行情源。

    仓位助手里的 data_source 可能是 akshare、yfinance、danjuan 等，
    其中不少只适合实时/估值获取，不是回测历史行情源。
    回测需要的是 eastmoney / yahoo / stooq / auto。
    """
    raw = str(source or "auto").strip().lower()
    if raw in {"", "auto", "multi", "自动", "自动多链路"}:
        return "auto"
    if is_danjuan_only_source(raw):
        return "danjuan_only"
    if raw in {"akshare", "funddb", "danjuan", "danjuan_json", "danjuan_html", "lixinger"}:
        # AKShare/蛋卷在主程序里常用于估值或实时数据；回测历史行情改走可直接拉历史K线的链路。
        return "auto" if market.upper() == "CN" else "auto"
    if raw in {"yfinance", "yahoo", "yahoo_chart", "yahoo finance"}:
        return "yahoo"
    if raw in {"eastmoney", "eastmoney_kline", "东方财富"}:
        return "eastmoney"
    if raw in {"fund", "fund_nav", "eastmoney_fund", "eastmoney_fund_nav", "天天基金"}:
        return "fund"
    if raw in {"stooq", "stooq_csv"}:
        return "stooq"
    return "auto"


def backtest_fetch_records_uncached(symbol: str, market: str, source: str, start: date, end: date, cfg: Dict[str, Any], asset_kind: str = "") -> Tuple[List[Dict[str, Any]], str, List[str]]:
    options = fetch_options_from_cfg(cfg)
    errors: List[str] = []
    source = normalize_backtest_source(source, market)
    candidates: List[Callable[[], Tuple[List[Dict[str, Any]], str]]] = []

    asset_kind = resolve_asset_kind(symbol, market, asset_kind, str(cfg.get("symbol_name") or ""))
    symbol_name = str(cfg.get("symbol_name") or "")
    is_cn_fund = is_cn_open_fund_like(symbol, market, asset_kind, symbol_name)
    is_danjuan_nav = is_danjuan_nav_like(symbol, market, asset_kind, symbol_name)
    danjuan_only = source == "danjuan_only"

    # 场外基金 / ETF联接 / QDII基金优先使用基金净值。
    # 不能直接用股票K线接口，否则会出现 017641.SZ / 007721.SZ 这类无效链路。
    if source in {"auto", "fund", "eastmoney", "akshare", "danjuan", "danjuan_only"} and (is_cn_fund or (danjuan_only and is_danjuan_nav)):
        candidates.append(lambda: backtest_fetch_danjuan_fund_nav(symbol, start, end, options))
        if not danjuan_only:
            candidates.append(lambda: backtest_fetch_eastmoney_fund_nav(symbol, start, end, options))

    if danjuan_only and not candidates:
        raise RuntimeError("只使用蛋卷目前仅支持可通过蛋卷基金净值接口获取的基金 / ETF / QDII 标的")

    # 国内股票/ETF/指数才优先东方财富K线；场外基金不走这条链路。
    if source in {"auto", "eastmoney"} and not is_cn_fund and (market.upper() == "CN" or re.fullmatch(r"\d{6}", symbol or "")):
        candidates.append(lambda: backtest_fetch_eastmoney(symbol, start, end, options))
    if source in {"auto", "yahoo"} and not is_cn_fund:
        candidates.append(lambda: backtest_fetch_yahoo(symbol, market, start, end, options))
    if source in {"auto", "stooq"} and not is_cn_fund:
        candidates.append(lambda: backtest_fetch_stooq(symbol, market, start, end, options))

    if not candidates:
        raise RuntimeError("没有可用的历史行情数据源")
    min_required = 20 if is_cn_fund else 60
    for fn in candidates:
        try:
            records, label = fn()
            if len(records) >= min_required:
                if is_cn_fund and len(records) < 60:
                    errors.append(f"{label}: 净值点仅 {len(records)} 条，结果可信度偏低；建议拉长回测周期或提高操作周期")
                return records, label, errors
            errors.append(f"{label}: 数据太少 {len(records)} 条，至少需要 {min_required} 条")
        except Exception as exc:
            errors.append(str(exc)[:240])
    raise RuntimeError("；".join(errors) or "所有历史行情源失败")


def backtest_max_drawdown(values: List[float]) -> float:
    peak = -float("inf")
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def backtest_annual_vol(returns: List[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(252)


def backtest_cagr(e0: float, e1: float, days: int) -> float:
    if e0 <= 0 or e1 <= 0 or days <= 0:
        return 0.0
    years = days / 365.25
    return (e1 / e0) ** (1.0 / years) - 1.0 if years > 0 else 0.0


def backtest_series_returns(values: List[float]) -> List[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]


def backtest_daily_risk_free_rate(annual_risk_free_rate: float) -> float:
    """把年化无风险收益率换算成日化收益率，用于夏普比率的日收益序列。"""
    rf = clamp(float(annual_risk_free_rate or 0.0), -0.99, 1.0)
    return (1.0 + rf) ** (1.0 / 252.0) - 1.0


def backtest_risk_free_rate(data: Dict[str, Any], cfg: Dict[str, Any]) -> float:
    """回测风险调整指标使用的年化无风险收益率。

    前端设置项是百分数，例如 2 表示 2%。为了兼容未来接口，也允许
    /api/backtest 的 payload 直接传 risk_free_rate_pct 覆盖。
    """
    raw = data.get("risk_free_rate_pct", cfg.get("backtest_risk_free_rate_pct", DEFAULT_CONFIG.get("backtest_risk_free_rate_pct", 2.0)))
    return clamp(as_float(raw, 2.0), -20.0, 30.0) / 100.0


def backtest_sharpe_ratio(values: List[float], annual_risk_free_rate: float = 0.0) -> float:
    returns = backtest_series_returns(values)
    vol = backtest_annual_vol(returns)
    if not returns or vol <= 0:
        return 0.0
    daily_rf = backtest_daily_risk_free_rate(annual_risk_free_rate)
    excess_daily_mean = sum((r - daily_rf) for r in returns) / len(returns)
    return (excess_daily_mean * 252.0 / vol)


def backtest_calmar_ratio(cagr: float, max_drawdown: float, annual_risk_free_rate: float = 0.0) -> float:
    dd = abs(float(max_drawdown or 0.0))
    excess_cagr = float(cagr or 0.0) - float(annual_risk_free_rate or 0.0)
    if dd <= 1e-9:
        return 0.0 if excess_cagr <= 0 else 999.0
    return excess_cagr / dd


def _score_component(diff: float, scale: float) -> float:
    if not math.isfinite(diff) or scale <= 0:
        return 0.0
    return math.tanh(diff / scale) * 100.0



def _score_piecewise(value: float, points: List[Tuple[float, float]]) -> float:
    """按分段线性规则把原始指标折成 0~100 分。"""
    try:
        v = float(value or 0.0)
    except Exception:
        return 0.0
    if not math.isfinite(v) or not points:
        return 0.0
    pts = sorted((float(x), float(y)) for x, y in points)
    if v <= pts[0][0]:
        return max(0.0, min(100.0, pts[0][1]))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if v <= x1:
            if abs(x1 - x0) <= 1e-12:
                return max(0.0, min(100.0, y1))
            t = (v - x0) / (x1 - x0)
            return max(0.0, min(100.0, y0 + (y1 - y0) * t))
    return max(0.0, min(100.0, pts[-1][1]))


def _strategy_excess_return_score(cagr: float, annual_risk_free_rate: float) -> float:
    """年化超额收益得分：收益必须先跑赢无风险收益率。"""
    excess = float(cagr or 0.0) - float(annual_risk_free_rate or 0.0)
    return _score_piecewise(excess, [
        (0.00, 0),
        (0.02, 25),
        (0.05, 55),
        (0.08, 78),
        (0.12, 95),
        (0.16, 100),
    ])


def _drawdown_control_score(max_drawdown: float) -> float:
    """最大回撤控制得分：回撤越小越好，但不能单独决定策略高分。"""
    dd = abs(float(max_drawdown or 0.0))
    return _score_piecewise(dd, [
        (0.00, 100),
        (0.05, 95),
        (0.10, 85),
        (0.20, 65),
        (0.35, 38),
        (0.50, 15),
        (0.70, 0),
    ])


def _risk_adjusted_score_component(value: float) -> float:
    """夏普/卡玛得分：0 以下为 0 分，1 附近良好，2 以上满分。"""
    try:
        v = float(value or 0.0)
    except Exception:
        return 0.0
    if not math.isfinite(v) or v <= 0:
        return 0.0
    return _score_piecewise(v, [
        (0.00, 0),
        (0.50, 35),
        (1.00, 65),
        (1.50, 85),
        (2.00, 100),
    ])


def _system_execution_quality_score(avg_exposure_pct: float, trade_count: int) -> float:
    """系统策略执行质量：避免低仓位/少交易把回撤压低后被误判为好策略。"""
    exp_score = _score_piecewise(float(avg_exposure_pct or 0.0), [
        (0.0, 0),
        (5.0, 8),
        (10.0, 20),
        (25.0, 55),
        (45.0, 82),
        (70.0, 100),
        (95.0, 92),
        (100.0, 88),
    ])
    trade_score = _score_piecewise(float(trade_count or 0), [
        (0, 0),
        (1, 35),
        (2, 55),
        (4, 78),
        (8, 100),
    ])
    return max(0.0, min(100.0, exp_score * 0.70 + trade_score * 0.30))


def _dca_execution_quality_score(buy_count: int) -> float:
    if buy_count <= 0:
        return 0.0
    if buy_count == 1:
        return 70.0
    if buy_count <= 3:
        return 82.0
    return 95.0


def backtest_strategy_score(
    cagr: float,
    max_drawdown: float,
    calmar: float,
    sharpe: float,
    annual_risk_free_rate: float,
    execution_quality: float,
) -> float:
    """策略表现百分制评分。

    权重：年化超额收益 30%、最大回撤控制 25%、卡玛 20%、夏普 20%、执行质量 5%。
    这样不会再只靠低回撤拿高分，也不会重复使用"系统相对定投"的相减评分。
    """
    score = (
        _strategy_excess_return_score(cagr, annual_risk_free_rate) * 0.30
        + _drawdown_control_score(max_drawdown) * 0.25
        + _risk_adjusted_score_component(calmar) * 0.20
        + _risk_adjusted_score_component(sharpe) * 0.20
        + max(0.0, min(100.0, float(execution_quality or 0.0))) * 0.05
    )
    return max(0.0, min(100.0, score))


def _latest_raw_valuation(series: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if not series:
        return {}
    try:
        latest_ds = max(series.keys())
        return historical_valuation_for_date(series, latest_ds)
    except Exception:
        return {}


def _as_pct_number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        text = str(value).strip().replace("%", "")
        if not text or text in {"--", "nan", "None"}:
            return None
        return float(text)
    except Exception:
        return None


def _valuation_percentile_score(value: Optional[float], points: List[Tuple[float, float]]) -> float:
    if value is None:
        return 50.0
    return _score_piecewise(float(value), points)


def backtest_asset_core_score(
    symbol: str,
    market: str,
    asset_kind: str,
    symbol_name: str,
    valuation_series: Dict[str, Dict[str, Any]],
    hold_cagr: float,
    hold_mdd: float,
    hold_sharpe: float,
    hold_calmar: float,
    hold_vol: float,
    annual_risk_free_rate: float,
    data_source: str,
) -> float:
    """标的核心得分：评估这只股票/基金本身是否适合做长期核心资产。"""
    kind = (asset_kind or "").lower()
    name = f"{symbol_name or ''} {symbol or ''}".lower()
    latest_val = _latest_raw_valuation(valuation_series)
    pe_pct = _as_pct_number(latest_val.get("pe_percentile"))
    pb_pct = _as_pct_number(latest_val.get("pb_percentile"))
    roe = _as_pct_number(latest_val.get("roe_pct"))

    is_fund_like = any(x in kind for x in ["fund", "etf", "index", "qdii", "fof"])
    is_broad_core = is_fund_like and any(x in name for x in [
        "标普", "sp500", "s&p", "纳指", "nasdaq", "沪深300", "上证50", "中证500", "中证1000", "宽基", "指数"
    ])

    # 1) 长期资产质量 30 分
    roe_score = _score_piecewise(roe if roe is not None else 10.0, [(0, 0), (8, 3), (12, 7), (18, 11), (25, 12)])
    long_logic = 8.0 if is_broad_core else (6.5 if is_fund_like else 4.5)
    diversification = 5.0 if is_fund_like else 2.0
    growth_stability = _score_piecewise(hold_cagr - annual_risk_free_rate, [(0, 1), (0.03, 2.5), (0.06, 4), (0.10, 5)])
    quality_score = min(30.0, roe_score + long_logic + diversification + growth_stability)

    # 2) 估值安全边际 20 分
    pe_score = _valuation_percentile_score(pe_pct, [(0, 10), (30, 10), (60, 7), (80, 4), (90, 2), (100, 0)])
    pb_score = _valuation_percentile_score(pb_pct, [(0, 5), (40, 5), (80, 2.5), (100, 0)])
    if roe is not None and roe >= 18:
        match_score = _valuation_percentile_score(pe_pct, [(0, 5), (60, 5), (80, 3.5), (90, 2), (100, 1)])
    elif roe is not None and roe < 8:
        match_score = _valuation_percentile_score(pe_pct, [(0, 4), (60, 2.5), (80, 1), (100, 0)])
    else:
        match_score = _valuation_percentile_score(pe_pct, [(0, 5), (50, 4), (80, 2), (100, 0.5)])
    valuation_score = min(20.0, pe_score + pb_score + match_score)

    # 3) 风险与回撤 20 分
    dd_score = _drawdown_control_score(hold_mdd) / 100.0 * 8.0
    vol_score = _score_piecewise(float(hold_vol or 0.0), [(0.00, 5), (0.12, 5), (0.20, 3.8), (0.35, 2.0), (0.55, 0.5), (0.80, 0)])
    recovery_score = _risk_adjusted_score_component(hold_calmar) / 100.0 * 4.0
    extreme_score = 3.0 if is_broad_core else (2.4 if is_fund_like else 1.6)
    if "qdii" in kind or "qdii" in name:
        extreme_score -= 0.5
    risk_score = min(20.0, dd_score + vol_score + recovery_score + max(0.0, extreme_score))

    # 4) 收益质量 15 分
    excess_score = _strategy_excess_return_score(hold_cagr, annual_risk_free_rate) / 100.0 * 5.0
    sharpe_score = _risk_adjusted_score_component(hold_sharpe) / 100.0 * 4.0
    calmar_score = _risk_adjusted_score_component(hold_calmar) / 100.0 * 4.0
    persistence_score = 2.0 if hold_cagr > annual_risk_free_rate else (1.0 if hold_cagr > 0 else 0.0)
    return_quality_score = min(15.0, excess_score + sharpe_score + calmar_score + persistence_score)

    # 5) 产品与交易可用性 15 分
    liquidity_score = 3.5 if is_fund_like else 2.8
    cost_tracking_score = 3.0 if is_broad_core else (2.4 if is_fund_like else 2.0)
    data_score = 3.0 if valuation_series else (2.0 if data_source else 1.0)
    convenience_score = 1.5 if ("qdii" in kind or "qdii" in name) else 2.0
    clarity_score = 2.0 if is_broad_core else (1.5 if is_fund_like else 1.0)
    product_score = min(15.0, liquidity_score + cost_tracking_score + data_score + convenience_score + clarity_score)

    total = quality_score + valuation_score + risk_score + return_quality_score + product_score
    return max(0.0, min(100.0, total))

def backtest_score_100(v: float) -> str:
    return f"{max(0.0, min(100.0, float(v or 0.0))):.1f} / 100"


def simulate_periodic_dca(
    records: List[Dict[str, Any]],
    start_index: int,
    rebalance_days: int,
    initial_cash: float,
    fee: float,
    slip: float,
) -> Tuple[List[float], int, float]:
    """无脑定额定投基准。

    口径：按回测的操作周期生成买入日，把 100% 计划资金均分成若干份，
    在每个定投日买入并持有到期末。

    注意：最后一个可观测日不定投。否则最后一天刚买入就结束，几乎看不到
    持有收益，会把定投基准算得偏假。比如一段 10 个月的月度回测，定投日
    应该是第 1~9 个月，每次买入计划资金的 1/9，而不是第 10 个月也买。
    """
    if initial_cash <= 0 or start_index < 0 or start_index >= len(records) - 1:
        return [], 0, 0.0

    step = max(1, int(rebalance_days))
    # 买入日按"当前可观测日"计算，而不是下一日；但必须排除最后一个记录。
    buy_indices = set(range(start_index, len(records) - 1, step))
    buy_count_plan = len(buy_indices)
    installment = initial_cash / buy_count_plan if buy_count_plan > 0 else 0.0

    cash = float(initial_cash)
    shares = 0.0
    invested = 0.0
    buy_count = 0
    equity_curve: List[float] = []

    for i in range(start_index, len(records) - 1):
        rec = records[i]
        next_rec = records[i + 1]
        buy_price_raw = float(rec.get("close") or rec.get("open") or 0.0)
        exec_close = float(next_rec["close"])

        if i in buy_indices and installment > 0:
            gross = min(installment, max(cash / (1.0 + fee), 0.0))
            if gross > 0 and buy_price_raw > 0:
                price = buy_price_raw * (1.0 + slip)
                shares += gross / price
                cash -= gross + gross * fee
                invested += gross
                buy_count += 1

        equity_curve.append(cash + shares * exec_close)

    return equity_curve, buy_count, invested


def backtest_money(v: float) -> str:
    return f"{v:,.2f}"


def backtest_pct(v: float) -> str:
    return f"{v:.2f}%"


def _fmt_metric_number(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "--"
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return "--"
        return f"{v:.2f}{suffix}"
    except Exception:
        return "--"


def _fmt_metric_diff(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "--"
    try:
        if math.isnan(value) or math.isinf(value):
            return "--"
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}{suffix}"
    except Exception:
        return "--"


def latest_history_item(series: Dict[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    if not series:
        return "--", {}
    ds = max(series.keys())
    return ds, dict(series.get(ds) or {})


def append_valuation_comparison_metrics(
    metrics: Dict[str, Any],
    valuation_series: Dict[str, Dict[str, Any]],
    symbol: str,
    asset_kind: str,
    cfg: Dict[str, Any],
    symbol_name: str = "",
) -> List[str]:
    """回测完成后，把历史序列最新测算值与蛋卷 detail 当前页面值对比写入核心指标。

    这只是口径校验，不参与交易结果计算；接口失败时不会让回测失败。
    """
    notes: List[str] = []
    if not valuation_series:
        return notes
    hist_date, hist = latest_history_item(valuation_series)
    if not hist:
        return notes
    metrics["估值序列最新日期"] = hist_date

    options = fetch_options_from_cfg(cfg)
    try:
        page = fetch_danjuan_detail_valuation(symbol, asset_kind, options, symbol_name)
    except Exception as exc:
        notes.append(f"估值页面对比失败：{str(exc)[:160]}")
        return notes

    metrics["估值页面最新日期"] = str(page.get("valuation_date") or page.get("date") or "--")

    def diff(a: Any, b: Any) -> Optional[float]:
        try:
            if a is None or b is None:
                return None
            return float(a) - float(b)
        except Exception:
            return None

    hist_pe_pct = hist.get("pe_percentile")
    page_pe_pct = page.get("pe_percentile")
    metrics["历史PE百分位"] = _fmt_metric_number(hist_pe_pct, "%")
    metrics["页面PE百分位"] = _fmt_metric_number(page_pe_pct, "%")
    metrics["PE百分位误差"] = _fmt_metric_diff(diff(hist_pe_pct, page_pe_pct), "%")

    hist_pb = hist.get("current_pb")
    page_pb = page.get("current_pb")
    metrics["历史PB"] = _fmt_metric_number(hist_pb)
    metrics["页面PB"] = _fmt_metric_number(page_pb)
    metrics["PB误差"] = _fmt_metric_diff(diff(hist_pb, page_pb))

    hist_pb_pct = hist.get("pb_percentile")
    page_pb_pct = page.get("pb_percentile")
    metrics["历史PB百分位"] = _fmt_metric_number(hist_pb_pct, "%")
    metrics["页面PB百分位"] = _fmt_metric_number(page_pb_pct, "%")
    metrics["PB百分位误差"] = _fmt_metric_diff(diff(hist_pb_pct, page_pb_pct), "%")

    hist_roe = hist.get("roe_pct")
    page_roe = page.get("roe_pct")
    metrics["历史ROE"] = _fmt_metric_number(hist_roe, "%")
    metrics["页面ROE"] = _fmt_metric_number(page_roe, "%")
    metrics["ROE误差"] = _fmt_metric_diff(diff(hist_roe, page_roe), "%")
    return notes


def build_backtest_diagnosis_metrics(
    metrics: Dict[str, Any],
    *,
    total_ret: float,
    dca_ret: float,
    bench_ret: float,
    strategy_mdd: float,
    dca_mdd: float,
    bench_mdd: float,
    avg_exp_pct: float,
    turnover_value: float,
    initial_cash: float,
    fee: float,
    effective_slip: float,
    trade_count: int,
) -> None:
    """补充回测失败归因指标。

    这些是诊断指标，不参与策略交易计算：
    - 看策略到底输给了定投还是持有；
    - 看收益差是否换来了更低回撤；
    - 粗略估算现金拖累和交易成本拖累。
    """
    ret_gap_dca = float(total_ret or 0.0) - float(dca_ret or 0.0)
    ret_gap_hold = float(total_ret or 0.0) - float(bench_ret or 0.0)
    dd_improve_dca = abs(float(dca_mdd or 0.0)) - abs(float(strategy_mdd or 0.0))
    dd_improve_hold = abs(float(bench_mdd or 0.0)) - abs(float(strategy_mdd or 0.0))

    turnover_ratio = float(turnover_value or 0.0) / max(float(initial_cash or 0.0), 1e-9)
    estimated_cost_drag = turnover_ratio * max(float(fee or 0.0) + float(effective_slip or 0.0), 0.0)
    exposure_ratio = clamp(float(avg_exp_pct or 0.0) / 100.0, 0.0, 2.0)
    estimated_cash_drag = max(float(bench_ret or 0.0), 0.0) * max(0.0, 1.0 - exposure_ratio)


    metrics["相对定投收益差"] = backtest_pct(ret_gap_dca * 100)
    metrics["相对持有收益差"] = backtest_pct(ret_gap_hold * 100)
    metrics["相对定投回撤改善"] = backtest_pct(dd_improve_dca * 100)
    metrics["相对持有回撤改善"] = backtest_pct(dd_improve_hold * 100)
    metrics["估算交易成本拖累"] = backtest_pct(estimated_cost_drag * 100)
    metrics["估算现金拖累"] = backtest_pct(estimated_cash_drag * 100)


def backtest_metrics_rows(metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 展示顺序按"主指标优先、诊断项靠后"排列。
    # 前端会按同一顺序渲染；导出的核心指标 CSV 也保持这个顺序。
    order = [
        "标的核心得分", "系统策略得分", "定投策略得分", "持有策略得分",
        "策略年化收益", "定投策略年化收益", "买入持有年化收益",
        "策略最大回撤", "定投策略最大回撤", "买入持有最大回撤",
        "夏普比率", "定投策略夏普比率", "持有策略夏普比率",
        "卡玛比率", "定投策略卡玛比率", "持有策略卡玛比率",
        "无风险收益率",
        "策略总收益", "定投策略总收益", "买入持有总收益",
        "年化波动", "交易次数", "已实现胜率", "盈亏因子", "平均仓位", "换手率", "期末权益",
        "估值序列最新日期", "估值页面最新日期",
        "历史PE百分位", "页面PE百分位", "PE百分位误差",
        "历史PB", "页面PB", "PB误差",
        "历史PB百分位", "页面PB百分位", "PB百分位误差",
        "历史ROE", "页面ROE", "ROE误差",
        # 诊断项放在后面，避免一进回测结果就被“结论/拖累”抢占主指标位置。
        "相对定投收益差", "相对持有收益差", "相对定投回撤改善", "相对持有回撤改善",
        "估算交易成本拖累", "估算现金拖累",
    ]
    keys = [k for k in order if k in metrics] + [k for k in metrics.keys() if k not in order]
    return [{"指标": k, "数值": metrics.get(k, "--"), "备注": BACKTEST_METRIC_NOTES.get(k, "")} for k in keys]



def payload_metric_value(payload_result: Dict[str, Any], label: str, default: str = "") -> str:
    metrics = payload_result.get("metrics") or {}
    if isinstance(metrics, dict):
        return str(metrics.get(label, default) or default)
    if isinstance(metrics, list):
        for item in metrics:
            if isinstance(item, dict) and item.get("label") == label:
                return str(item.get("value", default) or default)
    return default

def write_backtest_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)



def build_backtest_trend_chart_series(records: List[Dict[str, Any]], start_index: int = 0, max_points: int = 2600) -> List[Dict[str, Any]]:
    """把历史回测区间行情整理成趋势图序列。

    与实时拉取趋势图不同，回测图需要保留整个回测区间，并且 MA20/50/200
    需要使用开始日期之前的预热数据计算后再裁剪，避免图表开头均线大量为空。
    """
    try:
        enriched = enrich_history_records_for_cache(records)
    except Exception:
        enriched = normalize_history_records(records)
    try:
        start = max(int(start_index or 0), 0)
    except Exception:
        start = 0
    subset = enriched[start:]
    if max_points and len(subset) > max_points:
        subset = subset[-max_points:]
    keys = [
        "date",
        "close",
        "ma20",
        "ma50",
        "ma200",
        "drawdown_252d",
        "rsi14",
        "macd_bar_pct",
        "return_60d",
    ]
    out: List[Dict[str, Any]] = []
    for row in subset:
        item: Dict[str, Any] = {}
        for key in keys:
            value = row.get(key)
            if value in (None, ""):
                item[key] = None
                continue
            if key == "date":
                item[key] = str(value)[:10]
                continue
            try:
                v = float(value)
                item[key] = None if math.isnan(v) or math.isinf(v) else round(v, 6)
            except Exception:
                item[key] = value
        if item.get("date") and item.get("close") is not None:
            out.append(item)
    return out


def backtest_trade_headline(direction: str, trade_pct: float, current_value_before: float, target_pos: float, result_action: str = "") -> str:
    """按真实成交占比生成回测交易记录里的【操作建议】。

    策略参数可能是“目标仓位调整幅度”，但实际成交需要按当前总资产和目标仓位执行。
    因此这里以真实成交金额 / 当时总资产为准，保证【操作建议】与【操作占比%】一致。
    """
    pct_text = pct(abs(float(trade_pct or 0.0)))
    direction_text = str(direction or "")
    if direction_text == "买入":
        label = "加仓" if current_value_before > 1e-9 else "买入"
        return f"{label}+{pct_text}"
    if direction_text == "卖出":
        if target_pos <= 0.005 or str(result_action or "") == "清仓":
            label = "清仓"
        elif str(result_action or "") == "止盈":
            label = "止盈"
        else:
            label = "减仓"
        return f"{label}-{pct_text}"
    return f"维持 {pct(target_pos)}"


def build_backtest_trade_points(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把交易记录压缩成前端趋势图买卖点。"""
    def _safe_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            text = str(value).strip().replace(",", "")
            if text.endswith("%"):
                text = text[:-1]
            v = float(text)
            return None if math.isnan(v) or math.isinf(v) else v
        except Exception:
            return None

    out: List[Dict[str, Any]] = []
    for row in trades or []:
        date_value = str(row.get("执行日") or row.get("信号日") or "")[:10]
        if not date_value:
            continue
        price = _safe_float(row.get("成交价"))
        target_pct = _safe_float(row.get("目标仓位%"))
        trade_pct = _safe_float(row.get("操作占比%"))
        point = {
            "date": date_value,
            "signal_date": str(row.get("信号日") or "")[:10],
            "direction": str(row.get("方向") or ""),
            "price": round(price, 6) if price is not None else None,
            "target_position_pct": round(target_pct, 2) if target_pct is not None else None,
            "trade_pct": round(trade_pct, 2) if trade_pct is not None else None,
            "asset_pct": str(row.get("当前总资产") or ""),
            "action": str(row.get("操作建议") or ""),
            "rule": str(row.get("命中规则") or ""),
        }
        out.append(point)
    return out

def run_backtest_web(data: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(data.get("symbol") or cfg.get("symbol") or "").strip()
    if not symbol:
        raise RuntimeError("请先填写回测标的代码")
    market = str(data.get("market") or cfg.get("market") or "US").upper()
    asset_kind = str(data.get("asset_kind") or cfg.get("asset_kind") or "stock")
    asset_kind = resolve_asset_kind(symbol, market, asset_kind, str(data.get("symbol_name") or cfg.get("symbol_name") or ""))
    source = str(data.get("source") or data.get("data_source") or cfg.get("data_source") or "auto")
    start_user = parse_date_safe(data.get("start_date") or "2020-01-01")
    end_user = parse_date_safe(data.get("end_date") or date.today().isoformat())
    if end_user <= start_user:
        raise RuntimeError("结束日期必须晚于开始日期")

    # 需要提前拉取一段历史数据用于 MA200/ATR 等指标预热。
    # 场外基金/QDII净值可能不是每日都有，且没有真实 OHLCV；若强制 220 根预热，
    # 一年回测很容易因净值点不足失败。因此基金回测使用更短预热，重点测净值趋势/仓位逻辑。
    is_cn_fund_for_warmup = market.upper() == "CN" and str(asset_kind or "").lower() in {"fund", "fof", "qdii", "fund_of_funds"}
    default_warmup = 60 if is_cn_fund_for_warmup else 220
    min_warmup = 20 if is_cn_fund_for_warmup else 80
    warmup_bars = int(clamp(as_float(data.get("warmup_bars"), default_warmup), min_warmup, 500))
    fetch_start = start_user - timedelta(days=max(180 if is_cn_fund_for_warmup else 420, int(warmup_bars * 3.2)))

    bt_cfg = ensure_config()
    bt_cfg.update(cfg)
    for key in ["proxy_mode", "proxy_url", "request_timeout_sec", "retry_count", "danjuan_cookie"]:
        if key in data:
            bt_cfg[key] = data.get(key)

    records, source_label, fetch_errors = backtest_fetch_records(symbol, market, source, fetch_start, end_user, bt_cfg, asset_kind)
    records = [r for r in records if parse_date_safe(r["date"]) <= end_user]
    if is_cn_fund_for_warmup and len(records) >= 25 and len(records) < warmup_bars + 5:
        # 对净值频率较低的 QDII/FOF，允许自适应缩短预热；少于25条仍不建议回测。
        warmup_bars = max(20, min(warmup_bars, len(records) // 2))
    if len(records) < warmup_bars + 5:
        raise RuntimeError(f"历史数据太少：需要至少 {warmup_bars + 5} 条，当前 {len(records)} 条")

    start_index = None
    for idx, r in enumerate(records):
        if idx >= warmup_bars and parse_date_safe(r["date"]) >= start_user:
            start_index = idx
            break
    if start_index is None or start_index >= len(records) - 2:
        raise RuntimeError("可回测区间太短，或开始日期前没有足够预热数据")

    # 回测优先使用左侧【标的与资金】里的核心配置，避免回测页重复字段与主配置不一致。
    initial_cash = max(as_float(cfg.get("plan_amount"), as_float(data.get("initial_cash"), 100000.0)), 1.0)
    strategy_family = str(cfg.get("strategy_family") or data.get("strategy_family") or DEFAULT_STRATEGY_FAMILY)
    if strategy_family not in STRATEGY_FAMILIES:
        strategy_family = DEFAULT_STRATEGY_FAMILY
    strategy = str(cfg.get("strategy") or data.get("strategy") or "balanced")
    if strategy not in STRATEGY_PRESETS:
        strategy = "balanced"
    position_mode = str(cfg.get("position_mode") or data.get("position_mode") or "core_satellite")
    if position_mode not in {"core_satellite", "strict_trade"}:
        position_mode = "core_satellite"
    risk_per_trade_pct = clamp(as_float(cfg.get("risk_per_trade_pct"), data.get("risk_per_trade_pct", 1.0)), 0.1, 100.0)
    rebalance_days = int(clamp(as_float(data.get("rebalance_days"), 5), 1, 60))
    min_trade_pct = clamp(as_float(data.get("min_trade_pct"), 0.5), 0.0, 20.0) / 100.0
    fee = clamp(as_float(data.get("fee_bps"), 2.0), 0.0, 1000.0) / 10000.0
    slip = clamp(as_float(data.get("slippage_bps"), 3.0), 0.0, 1000.0) / 10000.0
    is_cn_fund_for_slippage = is_cn_open_fund_like(symbol, market, asset_kind, str(data.get("symbol_name") or cfg.get("symbol_name") or ""))
    effective_slip = 0.0 if is_cn_fund_for_slippage else slip
    export_files = bool(data.get("export_files"))
    risk_free_rate = backtest_risk_free_rate(data, cfg)

    valuation_mode = str(data.get("valuation_mode") or "none")
    if valuation_mode not in {"none", "fixed", "historical"}:
        valuation_mode = "none"
    pe = parse_optional_pct(data, "pe_percentile") if valuation_mode == "fixed" else None
    pb = parse_optional_pct(data, "pb_percentile") if valuation_mode == "fixed" else None
    roe = parse_optional_pct(data, "roe_pct") if valuation_mode == "fixed" else None
    fundamentals = {
        "pe_percentile": pe,
        "pb_percentile": pb,
        "roe_pct": roe,
        "valuation_note": "回测固定估值输入" if valuation_mode == "fixed" and any(v is not None for v in [pe, pb, roe]) else "回测不使用估值修正",
    }
    valuation_series: Dict[str, Dict[str, Any]] = {}
    valuation_trace: List[str] = []
    if valuation_mode == "historical":
        valuation_series, valuation_trace = fetch_historical_valuation_series(symbol, market, asset_kind, bt_cfg, str(cfg.get("symbol_name") or data.get("symbol_name") or ""), fetch_start, end_user)
        if valuation_series:
            fundamentals = {"valuation_note": "使用历史估值序列"}
        else:
            fundamentals = {"valuation_note": "历史估值序列不可用，本次回测不使用估值修正"}

    cash = float(initial_cash)
    shares = 0.0
    avg_cost: Optional[float] = None
    turnover_value = 0.0
    realized_pnls: List[float] = []
    equity_curve: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    next_signal_index = start_index
    bench_start_close = float(records[start_index]["close"])

    last_signal = "等待"
    last_target = 0.0

    for i in range(start_index, len(records) - 1):
        signal_rec = records[i]
        next_rec = records[i + 1]
        close = float(signal_rec["close"])
        exec_open = float(next_rec.get("open") or next_rec["close"])
        exec_close = float(next_rec["close"])
        equity_signal = cash + shares * close
        pos_value_signal = shares * close
        profit_pct = ((close / avg_cost - 1.0) * 100.0) if (avg_cost and shares > 0) else 0.0
        profit_factor_signal = max(1.0 + profit_pct / 100.0, 0.0001)
        position_cost_signal = pos_value_signal / profit_factor_signal if shares > 0 else 0.0
        # 回测里当前实际总资产 equity_signal = 现金 + 持仓市值。
        # 按“计划金额 + 盈亏 = 实际本金”的口径，传给实时决策层的计划金额应是：
        # 现金 + 已投入成本，而不是 equity_signal 本身，避免把浮盈重复计入一次。
        plan_amount_signal = max(cash + position_cost_signal, 0.0)

        target_pos = (pos_value_signal / equity_signal) if equity_signal > 0 else 0.0
        payload_result: Dict[str, Any] = {"headline": last_signal, "metrics": {}}
        result_action = ""
        is_review_day = i >= next_signal_index
        if is_review_day:
            decision_cfg = DEFAULT_CONFIG.copy()
            decision_cfg.update({key: cfg.get(key, DEFAULT_CONFIG.get(key)) for key in ADVANCED_PARAM_KEYS})
            decision_cfg.update({
                "plan_amount": plan_amount_signal,
                "current_position_amount": pos_value_signal,
                "current_profit_pct": profit_pct,
                "strategy_family": strategy_family,
                "strategy": strategy,
                "strategy_mode": cfg.get("strategy_mode", "single"),
                "strategy_mix": cfg.get("strategy_mix", {}),
                "position_mode": position_mode,
                "risk_per_trade_pct": risk_per_trade_pct,
                "symbol": symbol,
                "symbol_name": str(data.get("symbol_name") or cfg.get("symbol_name") or ""),
                "market": market,
                "asset_kind": asset_kind,
                "data_source": source,
            })
            try:
                day_fundamentals = fundamentals.copy()
                if valuation_mode == "historical" and valuation_series:
                    day_fundamentals = merge_missing_fundamentals(day_fundamentals, historical_valuation_for_date(valuation_series, str(signal_rec.get("date") or "")))
                    day_fundamentals.setdefault("valuation_note", "使用历史估值序列")
                indicators = compute_indicators(records[: i + 1], day_fundamentals)
                indicators = enrich_indicators_with_user_position(indicators, decision_cfg)
                result = compute_decision(decision_cfg, indicators)
                result_action = str(result.action or "")
                payload_result = decision_to_payload(decision_cfg, result)
                target_pos = float(result.target_position)
                last_signal = payload_result.get("headline") or result.action
                last_target = target_pos
            except Exception as exc:
                last_signal = f"跳过：{str(exc)[:60]}"
                target_pos = (pos_value_signal / equity_signal) if equity_signal > 0 else 0.0
                last_target = target_pos
            next_signal_index = i + rebalance_days
        else:
            # 非检查日不重新计算信号，继续展示上一次目标仓位；是否成交只由操作周期日决定。
            # 这不是新增一层阈值，而是避免把"上次目标仓位"解释成每日再平衡指令。
            target_pos = last_target

        equity_exec = cash + shares * exec_open
        current_value_exec = shares * exec_open
        target_value_exec = target_pos * equity_exec
        trade_value = target_value_exec - current_value_exec

        headline_text = str(payload_result.get("headline") or last_signal or "").strip()
        current_pos_signal = (pos_value_signal / equity_signal) if equity_signal > 0 else 0.0
        maintain_signal = headline_text.startswith("维持") or (
            headline_text in {"持有", "观望", "等待"}
            and abs(target_pos - current_pos_signal) < min_trade_pct
        )

        # 只有检查日产生的真实调仓信号才成交；"维持 XX%"只记录目标，不做机械再平衡。
        if (not is_review_day) or maintain_signal:
            trade_value = 0.0
        elif abs(trade_value) / max(equity_exec, 1e-9) < min_trade_pct:
            trade_value = 0.0

        if trade_value > 0:
            price = exec_open * (1.0 + effective_slip)
            gross = min(trade_value, max(cash, 0.0))
            if gross > 0:
                buy_shares = gross / price
                old_cost = (avg_cost or price) * shares
                shares += buy_shares
                avg_cost = (old_cost + gross) / shares if shares > 0 else None
                cash -= gross + gross * fee
                turnover_value += gross
                current_total_asset = cash + shares * price
                trade_pct_value = gross / max(equity_exec, 1e-9)
                trades.append({
                    "信号日": signal_rec["date"], "执行日": next_rec["date"], "方向": "买入", "成交价": round(price, 4),
                    "成交金额": round(gross, 2), "操作占比%": round(trade_pct_value * 100, 2),
                    "当前总资产": backtest_pct(current_total_asset / initial_cash * 100), "目标仓位%": round(target_pos * 100, 2),
                    "操作建议": backtest_trade_headline("买入", trade_pct_value, current_value_exec, target_pos, result_action),
                    "命中规则": payload_metric_value(payload_result, "命中规则"),
                })
        elif trade_value < 0 and shares > 0:
            price = exec_open * (1.0 - effective_slip)
            gross = min(-trade_value, shares * price)
            sell_shares = gross / price
            pnl = (price - (avg_cost or price)) * sell_shares
            realized_pnls.append(pnl)
            shares -= sell_shares
            cash += gross - gross * fee
            turnover_value += gross
            if shares <= 1e-9:
                shares = 0.0
                avg_cost = None
            current_total_asset = cash + shares * price
            trade_pct_value = gross / max(equity_exec, 1e-9)
            trades.append({
                "信号日": signal_rec["date"], "执行日": next_rec["date"], "方向": "卖出", "成交价": round(price, 4),
                "成交金额": round(gross, 2), "操作占比%": round(trade_pct_value * 100, 2),
                "当前总资产": backtest_pct(current_total_asset / initial_cash * 100), "目标仓位%": round(target_pos * 100, 2),
                "操作建议": backtest_trade_headline("卖出", trade_pct_value, current_value_exec, target_pos, result_action),
                "命中规则": payload_metric_value(payload_result, "命中规则"),
            })

        equity_close = cash + shares * exec_close
        exposure = (shares * exec_close / equity_close) if equity_close > 0 else 0.0
        benchmark_equity = initial_cash * (exec_close / bench_start_close)
        equity_curve.append({
            "日期": next_rec["date"], "策略权益": round(equity_close, 2), "买入持有权益": round(benchmark_equity, 2),
            "收盘价": round(exec_close, 4), "仓位比例%": round(exposure * 100, 2), "现金": round(cash, 2),
            "操作建议": last_signal, "目标仓位%": round(target_pos * 100, 2),
        })

    if not equity_curve:
        raise RuntimeError("没有生成权益曲线")

    dca_eq, dca_buy_count, dca_invested = simulate_periodic_dca(
        records, start_index, rebalance_days, initial_cash, fee, effective_slip
    )
    for row, dca_value in zip(equity_curve, dca_eq):
        row["定投策略权益"] = round(dca_value, 2)

    eq = [float(x["策略权益"]) for x in equity_curve]
    bench = [float(x["买入持有权益"]) for x in equity_curve]
    dca_bench = dca_eq if dca_eq else [initial_cash for _ in equity_curve]
    returns = backtest_series_returns(eq)
    bench_returns = backtest_series_returns(bench)
    dca_returns = backtest_series_returns(dca_bench)
    total_ret = eq[-1] / initial_cash - 1.0
    bench_ret = bench[-1] / initial_cash - 1.0
    dca_ret = dca_bench[-1] / initial_cash - 1.0
    days = (parse_date_safe(equity_curve[-1]["日期"]) - parse_date_safe(equity_curve[0]["日期"])).days
    vol = backtest_annual_vol(returns)
    bench_vol = backtest_annual_vol(bench_returns)
    dca_vol = backtest_annual_vol(dca_returns)
    c = backtest_cagr(initial_cash, eq[-1], days)
    bc = backtest_cagr(initial_cash, bench[-1], days)
    dca_c = backtest_cagr(initial_cash, dca_bench[-1], days)
    strategy_mdd = backtest_max_drawdown(eq)
    dca_mdd = backtest_max_drawdown(dca_bench)
    bench_mdd = backtest_max_drawdown(bench)
    sharpe = backtest_sharpe_ratio(eq, risk_free_rate)
    dca_sharpe = backtest_sharpe_ratio(dca_bench, risk_free_rate)
    hold_sharpe = backtest_sharpe_ratio(bench, risk_free_rate)
    calmar = backtest_calmar_ratio(c, strategy_mdd, risk_free_rate)
    dca_calmar = backtest_calmar_ratio(dca_c, dca_mdd, risk_free_rate)
    hold_calmar = backtest_calmar_ratio(bc, bench_mdd, risk_free_rate)
    wins = [x for x in realized_pnls if x > 0]
    losses = [x for x in realized_pnls if x < 0]
    win_rate = len(wins) / len(realized_pnls) if realized_pnls else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0)
    avg_exp = sum(float(x["仓位比例%"] or 0) for x in equity_curve) / len(equity_curve)
    strategy_execution_score = _system_execution_quality_score(avg_exp, len(trades))
    dca_execution_score = _dca_execution_quality_score(dca_buy_count)
    hold_execution_score = 95.0
    strategy_score = backtest_strategy_score(c, strategy_mdd, calmar, sharpe, risk_free_rate, strategy_execution_score)
    dca_score = backtest_strategy_score(dca_c, dca_mdd, dca_calmar, dca_sharpe, risk_free_rate, dca_execution_score)
    hold_score = backtest_strategy_score(bc, bench_mdd, hold_calmar, hold_sharpe, risk_free_rate, hold_execution_score)
    asset_score = backtest_asset_core_score(
        symbol=symbol,
        market=market,
        asset_kind=asset_kind,
        symbol_name=str(cfg.get("symbol_name") or data.get("symbol_name") or ""),
        valuation_series=valuation_series if valuation_mode == "historical" else {},
        hold_cagr=bc,
        hold_mdd=bench_mdd,
        hold_sharpe=hold_sharpe,
        hold_calmar=hold_calmar,
        hold_vol=bench_vol,
        annual_risk_free_rate=risk_free_rate,
        data_source=source_label,
    )

    metrics = {
        "标的核心得分": backtest_score_100(asset_score),
        "系统策略得分": backtest_score_100(strategy_score),
        "定投策略得分": backtest_score_100(dca_score),
        "持有策略得分": backtest_score_100(hold_score),
        "策略总收益": backtest_pct(total_ret * 100),
        "定投策略总收益": backtest_pct(dca_ret * 100),
        "买入持有总收益": backtest_pct(bench_ret * 100),
        "策略年化收益": backtest_pct(c * 100),
        "定投策略年化收益": backtest_pct(dca_c * 100),
        "买入持有年化收益": backtest_pct(bc * 100),
        "策略最大回撤": backtest_pct(strategy_mdd * 100),
        "定投策略最大回撤": backtest_pct(dca_mdd * 100),
        "买入持有最大回撤": backtest_pct(bench_mdd * 100),
        "卡玛比率": round(calmar, 3),
        "定投策略卡玛比率": round(dca_calmar, 3),
        "持有策略卡玛比率": round(hold_calmar, 3),
        "无风险收益率": backtest_pct(risk_free_rate * 100),
        "年化波动": backtest_pct(vol * 100),
        "夏普比率": round(sharpe, 3),
        "定投策略夏普比率": round(dca_sharpe, 3),
        "持有策略夏普比率": round(hold_sharpe, 3),
        "交易次数": len(trades),
        "已实现胜率": backtest_pct(win_rate * 100),
        "盈亏因子": round(profit_factor, 3),
        "平均仓位": backtest_pct(avg_exp),
        "换手率": backtest_pct(turnover_value / initial_cash * 100),
        "期末权益": backtest_money(eq[-1]),
    }

    build_backtest_diagnosis_metrics(
        metrics,
        total_ret=total_ret,
        dca_ret=dca_ret,
        bench_ret=bench_ret,
        strategy_mdd=strategy_mdd,
        dca_mdd=dca_mdd,
        bench_mdd=bench_mdd,
        avg_exp_pct=avg_exp,
        turnover_value=turnover_value,
        initial_cash=initial_cash,
        fee=fee,
        effective_slip=effective_slip,
        trade_count=len(trades),
    )
    valuation_compare_notes = append_valuation_comparison_metrics(
        metrics,
        valuation_series if valuation_mode == "historical" else {},
        symbol,
        asset_kind,
        bt_cfg,
        str(cfg.get("symbol_name") or data.get("symbol_name") or ""),
    )
    metric_rows = backtest_metrics_rows(metrics)
    backtest_chart_series = build_backtest_trend_chart_series(records, start_index)
    backtest_trade_points = build_backtest_trade_points(trades)

    exported: Dict[str, str] = {}
    if export_files:
        outdir = os.path.join(APP_DIR, "backtest_reports")
        os.makedirs(outdir, exist_ok=True)
        safe_symbol = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "_", symbol)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{safe_symbol}_{strategy_family}_{strategy}_{position_mode}_{stamp}"
        metric_path = os.path.join(outdir, f"核心指标_{prefix}.csv")
        trade_path = os.path.join(outdir, f"交易记录_{prefix}.csv")
        curve_path = os.path.join(outdir, f"权益曲线_{prefix}.csv")
        write_backtest_csv(metric_path, metric_rows)
        write_backtest_csv(trade_path, trades)
        write_backtest_csv(curve_path, equity_curve)
        exported = {"核心指标": metric_path, "交易记录": trade_path, "权益曲线": curve_path}

    return {
        "summary": {
            "标的": symbol,
            "市场": market,
            "数据源": source_label,
            "回测周期": f"{equity_curve[0]['日期']} ~ {equity_curve[-1]['日期']}",
            "操作周期": f"每 {rebalance_days} 个交易日检查一次",
            "定投基准": f"每 {rebalance_days} 个交易日定额买入；默认投入100%计划资金；排除最后一个可观测日；买入 {dca_buy_count} 次",
            "K线数量": len(records),
            "交易次数": len(trades),
            "估值模式": {"none": "不使用估值修正", "fixed": "固定估值假设", "historical": "历史估值序列"}.get(valuation_mode, valuation_mode),
            "估值序列条数": len(valuation_series) if valuation_series else 0,
            "提示": "历史估值序列只使用当日及以前数据自算百分位；若不可用则不使用估值修正。" if valuation_mode == "historical" else ("PE/PB/ROE作为固定假设参与整段回测。" if valuation_mode == "fixed" else "本次回测不使用估值修正。"),
        },
        "metrics": metric_rows,
        "trades": trades[-300:],
        "trade_count": len(trades),
        "curve_tail": equity_curve[-120:],
        "backtest_chart_series": backtest_chart_series,
        "backtest_trade_points": backtest_trade_points,
        "backtest_chart_meta": {
            "rows": len(backtest_chart_series),
            "trades": len(backtest_trade_points),
            "start": backtest_chart_series[0].get("date") if backtest_chart_series else "",
            "end": backtest_chart_series[-1].get("date") if backtest_chart_series else "",
            "symbol": symbol,
            "market": market,
        },
        "exported": exported,
        "fetch_errors": fetch_errors + valuation_trace + valuation_compare_notes,
    }

@app.route("/", methods=["GET"])
def index():
    cfg = ensure_config()
    strategy = get_strategy(cfg)
    return render_template(
        "index.html",
        cfg=cfg,
        current_pos=current_position(cfg),
        strategy=strategy,
        strategy_families=STRATEGY_FAMILIES,
        strategies=STRATEGY_PRESETS,
        strategy_market_states=STRATEGY_MARKET_STATES,
        pct=pct,
        valuation_method_text=valuation_method_text,
        today=date.today().isoformat(),
    )



@app.post("/api/cache/clear")
def api_cache_clear():
    """清空当前进程内缓存，以及搜索/自动拉取的持久化缓存。

    注意：不会删除历史回测的行情/估值 CSV 缓存，避免误删长期回测数据。
    """
    before = len(_RUNTIME_CACHE)
    runtime_cache_clear()
    persistent = _clear_search_fetch_persistent_cache()
    return jsonify({
        "ok": True,
        "message": f"已清除运行缓存 {before} 条；搜索/拉取本地缓存 {persistent} 个文件。历史回测缓存未删除。",
        "cleared": before,
        "persistent_cleared": persistent,
    })

@app.get("/api/index-map")
def api_index_map_get():
    mapping = load_index_mapping()
    apply_index_mapping(mapping)
    return jsonify({
        "ok": True,
        "path": INDEX_MAP_PATH,
        "mapping": INDEX_MAPPING,
        "counts": {
            "local_symbols": len(LOCAL_SYMBOLS),
            "aliases": len(ALIASES),
            "index_codes": len(INDEX_CODE_MAP),
            "fund_index_map": len(FUND_INDEX_MAP),
            "index_names": len(AK_INDEX_NAME_MAP),
            "keyword_rules": len(INDEX_KEYWORD_RULES),
        },
    })


@app.post("/api/index-map")
def api_index_map_save():
    data = request.get_json(silent=True) or {}
    mapping = data.get("mapping", data)
    if not isinstance(mapping, dict):
        return jsonify({"ok": False, "message": "映射表必须是 JSON 对象"}), 400
    try:
        saved = save_index_mapping(mapping, apply=True)
        runtime_cache_clear()
    except Exception as exc:
        return jsonify({"ok": False, "message": f"映射表保存失败：{exc}"}), 400
    return jsonify({
        "ok": True,
        "message": "映射表已保存并热更新",
        "path": INDEX_MAP_PATH,
        "mapping": saved,
        "counts": {
            "local_symbols": len(LOCAL_SYMBOLS),
            "aliases": len(ALIASES),
            "index_codes": len(INDEX_CODE_MAP),
            "fund_index_map": len(FUND_INDEX_MAP),
            "index_names": len(AK_INDEX_NAME_MAP),
            "keyword_rules": len(INDEX_KEYWORD_RULES),
        },
    })


@app.post("/api/config")
def api_config():
    cfg = ensure_config()
    data = request.get_json(silent=True) or request.form.to_dict()

    cfg["plan_amount"] = max(as_float(data.get("plan_amount"), cfg["plan_amount"]), 0.0)
    cfg["current_position_amount"] = max(as_float(data.get("current_position_amount"), cfg["current_position_amount"]), 0.0)
    cfg.pop("cost_basis_price", None)
    cfg.pop("core_floor_pct", None)
    cfg.pop("asset_type", None)
    cfg["current_profit_pct"] = clamp(as_float(data.get("current_profit_pct"), cfg.get("current_profit_pct", 0.0)), -99.99, 9999.0)

    strategy_family = str(data.get("strategy_family", cfg.get("strategy_family", DEFAULT_STRATEGY_FAMILY)))
    cfg["strategy_family"] = strategy_family if strategy_family in STRATEGY_FAMILIES else DEFAULT_STRATEGY_FAMILY

    strategy = str(data.get("strategy", cfg.get("strategy", "balanced")))
    cfg["strategy"] = strategy if strategy in STRATEGY_PRESETS else "balanced"
    # 参数风格固定为单风格；不再接收前端的组合风格。
    cfg["strategy_mode"] = "single"
    if "strategy_family_params" in data and isinstance(data.get("strategy_family_params"), dict):
        cfg["strategy_family_params"] = data.get("strategy_family_params") or {}
    # 全局偏离度：防守/进攻只保存偏离值，有效参数运行时由均衡基准计算。
    if "deviation" in data and isinstance(data.get("deviation"), dict):
        cfg["deviation"] = data.get("deviation") or {}
    if "strategy_mix" in data and isinstance(data.get("strategy_mix"), dict):
        cfg["strategy_mix"] = data.get("strategy_mix") or {}
        # 兼容旧前端：如果没有传 family_params，就把根级 strategy_mix 作为当前总体策略参数。
        # 当前执行参数风格是全局 cfg["strategy"]，不再写进各总体策略配置。
        if "strategy_family_params" not in data:
            family_params = cfg.get("strategy_family_params") if isinstance(cfg.get("strategy_family_params"), dict) else {}
            family_params[cfg["strategy_family"]] = {"strategy_mix": cfg["strategy_mix"]}
            cfg["strategy_family_params"] = family_params

    mode = str(data.get("position_mode", cfg.get("position_mode", "core_satellite")))
    cfg["position_mode"] = mode if mode in {"core_satellite", "strict_trade"} else "core_satellite"
    # risk_per_trade_pct 已迁移为趋势信号策略的独立参数；根级字段只保留旧配置兼容，不再由左侧全局配置更新。
    cfg["backtest_risk_free_rate_pct"] = clamp(as_float(data.get("backtest_risk_free_rate_pct"), cfg.get("backtest_risk_free_rate_pct", 2.0)), -20.0, 30.0)

    # 均衡基准运行参数：偏离度只基于这些值临时计算；保存层只保留 balanced。
    for key, default, low, high in [
        ("global_risk_multiplier", 1.0, 0.1, 5.0),  # 兼容旧配置；前端不再展示。
        ("dca_base_buy_pct", 25.0, 0.0, 100.0),
        ("core_step_pct", 22.0, 0.0, 100.0),  # 兼容旧配置；前端不再展示。
        ("buy_step_limit_pct", 28.0, 0.0, 100.0),
        ("sell_step_limit_pct", 45.0, 0.0, 100.0),
        ("core_min_position_pct", 5.0, 0.0, 100.0),
        ("core_max_position_pct", 92.0, 0.0, 100.0),
        ("strict_min_position_pct", 0.0, 0.0, 100.0),
        ("strict_max_position_pct", 60.0, 0.0, 100.0),
    ]:
        if key in data:
            cfg[key] = clamp(as_float(data.get(key), cfg.get(key, default)), low, high)

    for key in ["symbol", "symbol_name", "market", "asset_kind", "data_source", "proxy_mode", "proxy_url", "danjuan_cookie", "valuation_method"]:
        if key in data:
            cfg[key] = str(data.get(key) or "")
    if cfg.get("proxy_mode") not in {"system", "custom", "none"}:
        cfg["proxy_mode"] = "system"
    if cfg.get("valuation_method") not in {"system_calc", "danjuan"}:
        # 兼容旧配置：前端已移除 auto，旧 auto 统一迁移为"乐咕乐股"。
        cfg["valuation_method"] = "system_calc"
    cfg["request_timeout_sec"] = clamp(as_float(data.get("request_timeout_sec"), cfg.get("request_timeout_sec", 12.0)), 3.0, 60.0)
    cfg["retry_count"] = int(clamp(as_float(data.get("retry_count"), cfg.get("retry_count", 2)), 0.0, 5.0))
    apply_active_family_params(cfg)

    save_config(cfg)
    strategy_obj = get_strategy(cfg)
    mode_text = "定投增强策略（固定买入 + 策略偏移）" if cfg.get("position_mode") == "core_satellite" else "纯交易仓"
    symbol_text = f"{cfg.get('symbol_name') or '未选择'} {cfg.get('symbol') or ''}".strip()
    return jsonify({
        "ok": True,
        "message": "配置已保存",
        "current_pos_text": pct(current_position(cfg)),
        "strategy_text": f"{full_strategy_summary(cfg)}<br>仓位模式：{mode_text}<br>标的：{symbol_text}<br>数据容错：代理 {cfg.get('proxy_mode')} / 超时 {cfg.get('request_timeout_sec')} 秒 / 重试 {cfg.get('retry_count')} 次<br>估值来源：{valuation_method_text(cfg.get('valuation_method'))}<br>回测无风险收益率：{cfg.get('backtest_risk_free_rate_pct', 2.0)}%<br>定投基准买入：{cfg.get('dca_base_buy_pct', 25.0)}%<br>计划资金=100%上限，不按标的类型封顶。",
        "config": cfg,
    })


def _cfg_for_decision_payload(saved_cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """决策接口允许前端把当前未落盘配置一起传入，避免防抖保存未完成时仍按旧策略计算。"""
    cfg = copy.deepcopy(saved_cfg)
    if "strategy_family" in payload:
        strategy_family = str(payload.get("strategy_family") or cfg.get("strategy_family") or DEFAULT_STRATEGY_FAMILY)
        cfg["strategy_family"] = strategy_family if strategy_family in STRATEGY_FAMILIES else DEFAULT_STRATEGY_FAMILY
    if "strategy" in payload:
        strategy = str(payload.get("strategy") or cfg.get("strategy") or "balanced")
        cfg["strategy"] = strategy if strategy in STRATEGY_PRESETS else "balanced"
    if "position_mode" in payload:
        mode = str(payload.get("position_mode") or cfg.get("position_mode") or "core_satellite")
        cfg["position_mode"] = mode if mode in {"core_satellite", "strict_trade"} else "core_satellite"
    for key in [
        "plan_amount", "current_position_amount", "current_profit_pct",
        "dca_base_buy_pct",
        "global_risk_multiplier",
        "core_step_pct", "buy_step_limit_pct", "sell_step_limit_pct",
        "core_min_position_pct", "core_max_position_pct",
        "strict_min_position_pct", "strict_max_position_pct",
    ]:
        if key in payload:
            cfg[key] = payload.get(key)
    if isinstance(payload.get("strategy_mix"), dict):
        cfg["strategy_mix"] = payload.get("strategy_mix") or {}
    if isinstance(payload.get("strategy_family_params"), dict):
        cfg["strategy_family_params"] = payload.get("strategy_family_params") or {}
    if isinstance(payload.get("deviation"), dict):
        cfg["deviation"] = payload.get("deviation") or {}
    for key in ["symbol", "symbol_name", "market", "asset_kind", "data_source"]:
        if key in payload:
            cfg[key] = payload.get(key)
    apply_active_family_params(cfg)
    return cfg


@app.post("/api/decision")
def api_decision():
    saved_cfg = ensure_config()
    form = request.get_json(silent=True) or request.form.to_dict()
    cfg = _cfg_for_decision_payload(saved_cfg, form if isinstance(form, dict) else {})
    result = compute_decision(cfg, form)
    return jsonify({"ok": True, "result": decision_to_payload(cfg, result)})


@app.get("/api/search")
def api_search():
    query = str(request.args.get("q", "")).strip()
    cfg = ensure_config()
    source = str(request.args.get("data_source") or request.args.get("source") or cfg.get("data_source") or "auto").strip().lower()
    danjuan_only = is_danjuan_only_source(source)
    local_cache_only = str(request.args.get("local_cache") or "").strip().lower() in {"1", "true", "yes"}
    refresh = str(request.args.get("refresh") or "").strip().lower() in {"1", "true", "yes"}

    if local_cache_only:
        # 前端增量搜索第一阶段：立即返回本地持久化缓存 + 内置映射，不联网。
        items = []
        if danjuan_only:
            items.extend(local_danjuan_search(query))
        else:
            items.extend(local_symbol_search(query))
        items.extend(_search_from_persistent_cache(query, "danjuan_only" if danjuan_only else source))
        results = dedupe_symbols(items, query)
        return jsonify({
            "ok": True,
            "results": results[:12],
            "source_mode": "danjuan_only" if danjuan_only else source,
            "cache": {"hit": bool(results), "persistent": True, "local_only": True},
            "partial": True,
        })

    cache_payload = {
        "q": query,
        "data_source": "danjuan_only" if danjuan_only else source,
        "local_symbols": len(LOCAL_SYMBOLS),
        "aliases": len(ALIASES),
        "index_map_mtime": os.path.getmtime(INDEX_MAP_PATH) if os.path.exists(INDEX_MAP_PATH) else 0,
    }
    cached = None
    age = 0
    # refresh=1 用于增量搜索第二阶段：绕过进程缓存，强制合并最新网络结果到本地。
    if not refresh:
        cached, age = runtime_cache_get("search", cache_payload)
    if cached is not None:
        # 即使命中进程缓存，也额外合并本地持久化结果，避免重启前后的候选不一致。
        cached_results = dedupe_symbols(list(cached.get("results") or []) + _search_from_persistent_cache(query, "danjuan_only" if danjuan_only else source), query)
        cached["results"] = cached_results[:12]
        return jsonify(add_cache_meta(cached, True, age))

    if danjuan_only:
        # 严格蛋卷模式：搜索阶段不调用 yfinance / AKShare，
        # 但允许调用蛋卷自己的搜索接口，所以中文名称/简称也能搜到。
        items = []
        items.extend(local_danjuan_search(query))
        items.extend(_search_from_persistent_cache(query, "danjuan_only"))
        try:
            items.extend(search_danjuan_funds(query, fetch_options_from_cfg(cfg)))
        except Exception:
            pass
        results = dedupe_symbols(items, query)
        payload = {"ok": True, "results": results[:12], "source_mode": "danjuan_only"}
        _merge_search_cache_results(query, results, "danjuan_only")
        runtime_cache_set("search", cache_payload, payload)
        return jsonify(add_cache_meta(payload, False))

    items = []
    items.extend(local_symbol_search(query))
    items.extend(_search_from_persistent_cache(query, source))
    # 本地候选优先；在线搜索失败不影响功能。
    try:
        items.extend(search_yfinance(query))
    except Exception:
        pass
    try:
        items.extend(search_akshare(query))
    except Exception:
        pass
    results = dedupe_symbols(items, query)

    # 只有在本地/在线都找不到结果时，才给一个可手动尝试的兜底候选；
    # 不再让 001422 这种"基金代码 + A股前缀重叠"的代码先显示成股票占位项。
    if re.fullmatch(r"\d{6}", query) and not any(str(x.get("symbol") or "").upper() == query.upper() for x in results):
        fallback_kind = guess_cn_asset_kind(query)
        results.append({"symbol": query.upper(), "name": query.upper(), "market": "CN", "asset_kind": fallback_kind, "source": "akshare"})

    payload = {"ok": True, "results": results[:12]}
    _merge_search_cache_results(query, results, source)
    runtime_cache_set("search", cache_payload, payload)
    return jsonify(add_cache_meta(payload, False))



@app.post("/api/connectivity-test")
def api_connectivity_test():
    data = request.get_json(silent=True) or {}
    cfg = ensure_config()
    for key in ["proxy_mode", "proxy_url", "request_timeout_sec", "retry_count", "danjuan_cookie", "valuation_method", "symbol", "symbol_name", "market", "asset_kind", "data_source"]:
        if key in data:
            cfg[key] = data.get(key)
    cache_payload = {
        "data": data,
        "cfg": {key: cfg.get(key) for key in ["proxy_mode", "proxy_url", "request_timeout_sec", "retry_count", "danjuan_cookie", "valuation_method", "symbol", "symbol_name", "market", "asset_kind", "data_source"]},
    }
    cached, age = runtime_cache_get("connectivity", cache_payload)
    if cached is not None:
        return jsonify(add_cache_meta(cached, True, age))
    try:
        results = run_connection_tests(
            cfg,
            symbol=str(data.get("symbol") or cfg.get("symbol") or ""),
            market=str(data.get("market") or cfg.get("market") or ""),
            asset_kind=str(data.get("asset_kind") or cfg.get("asset_kind") or ""),
        )
        payload = {
            "ok": True,
            "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        }
        runtime_cache_set("connectivity", cache_payload, payload)
        return jsonify(add_cache_meta(payload, False))
    except Exception as e:
        return jsonify({"ok": False, "message": f"连通性测试失败：{e}"}), 500

@app.post("/api/fetch")
def api_fetch():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").strip()
    market = str(data.get("market") or "auto").strip()
    asset_kind = str(data.get("asset_kind") or "auto").strip()
    source = str(data.get("source") or data.get("data_source") or "auto").strip()
    if not symbol:
        return jsonify({"ok": False, "message": "缺少代码"}), 400

    if source == "auto":
        source = "akshare" if market == "CN" or re.fullmatch(r"\d{6}", symbol) else "yfinance"
    symbol_name = str(data.get("symbol_name") or "")
    asset_kind = resolve_asset_kind(symbol, market, asset_kind, symbol_name)

    try:
        cfg = ensure_config()
        # 前端可能刚改完配置但保存尚未完成，这里同步使用请求中的容错参数和当前涨跌幅。
        for key in ["proxy_mode", "proxy_url", "request_timeout_sec", "retry_count", "danjuan_cookie", "valuation_method", "current_profit_pct", "current_position_amount", "plan_amount", "symbol_name"]:
            if key in data:
                cfg[key] = data.get(key)

        fetch_cache_path = _fetch_cache_file(symbol, market, asset_kind, source, cfg, symbol_name)
        force_refresh = str(data.get("force_refresh") or "").strip().lower() in {"1", "true", "yes"}

        # 每日持久化缓存：同一自然日内，行情/净值和估值原始结果不重复联网。
        # 注意这里只缓存 records/fundamentals，不缓存最终 indicators；当前仓位、盈亏等用户输入仍然每次即时重算。
        daily_cache = None if force_refresh else _load_daily_fetch_cache(fetch_cache_path)
        if daily_cache is not None:
            records = daily_cache.get("records") or []
            fundamentals = daily_cache.get("fundamentals") or {}
            source_used = str(daily_cache.get("source_used") or source)
            saved_at = str(daily_cache.get("saved_at") or daily_cache.get("cache_date") or "")
            trace = list(daily_cache.get("trace") or [])
            trace.append({
                "source": "local_daily_fetch_cache",
                "ok": True,
                "rows": len(records),
                "elapsed_ms": 0,
                "message": f"本地日缓存命中：{saved_at}",
            })
            indicators = compute_indicators(records, fundamentals)
            indicators = enrich_indicators_with_user_position(indicators, cfg)
            indicators["source_used"] = f"local_daily_cache:{source_used}"
            indicators["fetch_trace"] = trace
            indicators["proxy_mode"] = cfg.get("proxy_mode", "system")
            chart_series = build_trend_chart_series(records)
            payload = {
                "ok": True,
                "symbol": symbol,
                "market": market,
                "asset_kind": asset_kind,
                "source": f"local_daily_cache:{source_used}",
                "trace": trace,
                "indicators": indicators,
                "chart_series": chart_series,
                "chart_meta": {
                    "rows": len(chart_series),
                    "start": chart_series[0].get("date") if chart_series else "",
                    "end": chart_series[-1].get("date") if chart_series else "",
                    "cache_date": daily_cache.get("cache_date"),
                },
                "message": f"数据已从本地日缓存读取：{source_used}。同一天不会重复联网，当前仓位/盈亏已重新计算。",
                "persistent_cache": {"hit": True, "cache_date": daily_cache.get("cache_date"), "path": os.path.relpath(fetch_cache_path, APP_DIR)},
            }
            return jsonify(add_cache_meta(payload, True, 0))

        cache_payload = {
            "symbol": symbol,
            "market": market,
            "asset_kind": asset_kind,
            "source": source,
            "symbol_name": symbol_name,
            "request": {k: v for k, v in data.items() if k != "force_refresh"},
            "cfg": {key: cfg.get(key) for key in ["proxy_mode", "proxy_url", "request_timeout_sec", "retry_count", "danjuan_cookie", "valuation_method", "current_profit_pct", "current_position_amount", "plan_amount", "symbol_name"]},
        }
        cached, age = (None, 0) if force_refresh else runtime_cache_get("fetch", cache_payload)
        if cached is not None:
            cached = add_cache_meta(cached, True, age)
            cached["message"] = f"数据已从进程缓存读取：{cached.get('source') or source}。参数未变化，未重复拉取。"
            return jsonify(cached)

        records, fundamentals, trace, source_used = fetch_market_data(symbol, market, asset_kind, source, cfg)
        _save_daily_fetch_cache(fetch_cache_path, records, fundamentals, trace, source_used, symbol, market, asset_kind, symbol_name)
        indicators = compute_indicators(records, fundamentals)
        indicators = enrich_indicators_with_user_position(indicators, cfg)
        indicators["source_used"] = source_used
        indicators["fetch_trace"] = trace
        indicators["proxy_mode"] = cfg.get("proxy_mode", "system")
        chart_series = build_trend_chart_series(records)
        payload = {
            "ok": True,
            "symbol": symbol,
            "market": market,
            "asset_kind": asset_kind,
            "source": source_used,
            "trace": trace,
            "indicators": indicators,
            "chart_series": chart_series,
            "chart_meta": {
                "rows": len(chart_series),
                "start": chart_series[0].get("date") if chart_series else "",
                "end": chart_series[-1].get("date") if chart_series else "",
                "cache_date": date.today().isoformat(),
            },
            "persistent_cache": {"hit": False, "saved": True, "cache_date": date.today().isoformat(), "path": os.path.relpath(fetch_cache_path, APP_DIR)},
            "message": f"数据已获取：{source_used} 成功。已写入本地日缓存，并自动填入中间表单；你可以手动覆盖。",
        }
        runtime_cache_set("fetch", cache_payload, payload)
        return jsonify(add_cache_meta(payload, False))
    except Exception as e:
        return jsonify({
            "ok": False,
            "message": f"自动获取失败：{e}。可以先手动填写PE百分位/ROE/趋势信号。",
        }), 400


@app.post("/api/backtest")
def api_backtest():
    data = request.get_json(silent=True) or {}
    try:
        cfg = ensure_config()
        cache_payload = {
            "version": 20,
            "data": data,
            "cfg": {key: cfg.get(key) for key in [
                "plan_amount", "strategy_family", "strategy", "strategy_mix", "strategy_family_params", "deviation", "position_mode", "backtest_risk_free_rate_pct",
                "global_risk_multiplier", "dca_base_buy_pct",
                "symbol", "symbol_name", "market", "asset_kind", "data_source",
                "proxy_mode", "proxy_url", "request_timeout_sec", "retry_count",
                "danjuan_cookie", "valuation_method",
                *ADVANCED_PARAM_KEYS,
            ]},
        }
        cached, age = runtime_cache_get("backtest", cache_payload)
        if cached is not None:
            cached = add_cache_meta(cached, True, age)
            if isinstance(cached.get("result"), dict):
                cached["result"]["cache_hit"] = True
            return jsonify(cached)
        result = run_backtest_web(data, cfg)
        payload = {"ok": True, "result": result}
        runtime_cache_set("backtest", cache_payload, payload)
        return jsonify(add_cache_meta(payload, False))
    except Exception as e:
        return jsonify({"ok": False, "message": f"历史回测失败：{e}"}), 400


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
