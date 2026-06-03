"""
美股情报站 backend - FastAPI + yfinance + Gemini + Telegram
启动: python server.py  →  http://localhost:18506/

功能:
- 即时价格 / OHLC / 技术指标 (yfinance)
- 分析师评等 (yfinance.recommendations) 取代台股的三大法人
- 持久化观察清单 (watchlist.json)
- 讯号侦测 (黄金/死亡交叉、KD 超买卖、突破 20 日新高低)
- 多周期 K 线 (日/周/月)
- 英文新闻 + Gemini 自动翻成繁体中文
- Telegram 警示 (每 5 分钟背景扫描)
- 热度榜 / 族群分组
- 季营收 + 季 EPS (yfinance quarterly_income_stmt)
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# 本地 MySQL OHLCV 数据访问层 (取代 yfinance 行情)
# 数据库: us-stock-intel  每只股票一张表, 表名 = 小写代号 + 交易所后缀 (aapl.o / c.n)
# ----------------------------------------------------------------------------
from sqlalchemy import create_engine
from urllib.parse import quote_plus



import pymysql
from functools import lru_cache

MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "Hw1685168"),   # 建议用环境变量, 勿硬编码
    "database": os.environ.get("MYSQL_DB", "us-stock-intel"),
    "charset": "utf8mb4",
}
_ENGINE = create_engine(
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{quote_plus(MYSQL_CONFIG['password'])}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{quote_plus(MYSQL_CONFIG['database'])}"
    f"?charset=utf8mb4",
    pool_size=5, pool_recycle=3600, pool_pre_ping=True,
)
def _mysql_conn():
    return pymysql.connect(**MYSQL_CONFIG)

@lru_cache(maxsize=1)
def _list_tables() -> tuple:
    """列出库内所有表名(小写), 用于代号→表名解析。缓存一次, 重启或调用 _list_tables.cache_clear() 刷新。"""
    try:
        conn = _mysql_conn()
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = tuple(r[0].lower() for r in cur.fetchall())
        conn.close()
        return tables
    except Exception as e:
        print(f"[mysql] SHOW TABLES 失败: {e}")
        return tuple()

def _resolve_table(code_or_yf: str) -> str | None:
    """把代号 (AAPL / aapl / aapl.o) 解析为实际表名。优先精确, 再试 .o/.n 后缀, 最后前缀模糊。"""
    raw = code_or_yf.strip().lower()
    tables = _list_tables()
    if raw in tables:
        return raw
    base = raw.split(".")[0]
    if base in tables:
        return base
    for suf in (".o", ".n", ".a", ".oq", ".k"):
        if base + suf in tables:
            return base + suf
    for t in tables:
        if t.split(".")[0] == base:
            return t
    return None

def get_history(code_or_yf: str, limit: int | None = None,
                start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """从 MySQL 读单只股票 OHLCV, 返回与 yfinance.history() 同构的 DataFrame:
       DatetimeIndex(trade_date) + 列 Open/High/Low/Close/Volume/PreClose。取不到返回空 DataFrame。"""
    table = _resolve_table(code_or_yf)
    if not table:
        return pd.DataFrame()
    where, params = [], []
    if start:
        where.append("trade_date >= %s"); params.append(start)
    if end:
        where.append("trade_date <= %s"); params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT trade_date, `open`, `high`, `low`, `close`, `volume`, `pre_close` "
           f"FROM `{table}` {where_sql} ORDER BY trade_date ASC")
    try:
        conn = _mysql_conn()
        df = pd.read_sql(sql, conn, params=params or None)
        conn.close()
    except Exception as e:
        print(f"[mysql] 读取 {table} 失败: {e}")
        return pd.DataFrame()
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date").sort_index()
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                            "close": "Close", "volume": "Volume", "pre_close": "PreClose"})
    for col in ("Open", "High", "Low", "Close", "PreClose"):
        df[col] = df[col].astype(float)
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    if limit:
        df = df.iloc[-limit:]
    return df
def get_financials_latest(ths_code: str) -> dict | None:
    """从 stock_financials 取某代号最新一季快照 (给估值指标用)。
    ths_code 用带后缀的代号 (如 AAPL.O)；自动做大小写与后缀容错。"""
    code = ths_code.strip().upper()
    # 容错: 表里 ths_code 带后缀大写 (A.N / AAPL.O)
    candidates = [code]
    base = code.split(".")[0]
    for suf in (".O", ".N", ".A", ".OQ"):
        if base + suf not in candidates:
            candidates.append(base + suf)
    sql = ("SELECT * FROM stock_financials WHERE ths_code = %s "
           "ORDER BY trade_date DESC LIMIT 1")
    try:
        conn = _mysql_conn()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for c in candidates:
                cur.execute(sql, (c,))
                row = cur.fetchone()
                if row:
                    conn.close()
                    return row
        conn.close()
    except Exception as e:
        print(f"[mysql fin] {ths_code}: {e}")
    return None

def get_financials_history(ths_code: str, limit: int = 12) -> list[dict]:
    """从 stock_financials 取某代号近 N 季 (由旧到新), 给营收/EPS 趋势图用。"""
    code = ths_code.strip().upper()
    candidates = [code]
    base = code.split(".")[0]
    for suf in (".O", ".N", ".A", ".OQ"):
        if base + suf not in candidates:
            candidates.append(base + suf)
    sql = ("SELECT * FROM stock_financials WHERE ths_code = %s "
           "ORDER BY trade_date DESC LIMIT %s")
    try:
        conn = _mysql_conn()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for c in candidates:
                cur.execute(sql, (c, limit))
                rows = cur.fetchall()
                if rows:
                    conn.close()
                    return list(reversed(rows))   # 改为由旧到新
        conn.close()
    except Exception as e:
        print(f"[mysql fin hist] {ths_code}: {e}")
    return []

def _fin_num(row: dict, key: str):
    """安全取数值字段, 处理 None / Decimal / NaN。"""
    if not row:
        return None
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None   # 过滤 NaN
    except (TypeError, ValueError):
        return None

def download_closes(codes: list[str], start=None, end=None) -> pd.DataFrame:
    """取代 yf.download(...) 取收盘价矩阵。返回 index=日期, columns=原始代号, 值=Close。"""
    out = pd.DataFrame()
    for c in codes:
        try:
            h = get_history(c, start=(str(start)[:10] if start else None),
                            end=(str(end)[:10] if end else None))
            if not h.empty:
                out[c] = h["Close"]
        except Exception:
            continue
    return out.sort_index()

ROOT = Path(__file__).resolve().parent
WATCHLIST_FILE = ROOT / "watchlist.json"
ALERTS_FILE    = ROOT / "alerts.json"
ALERTS_LOG_FILE = ROOT / "alerts_log.json"
FEISHU_FILE = ROOT / "feishu.json"
PORTFOLIO_FILE = ROOT / "portfolio.json"
GEMINI_FILE    = ROOT / "deepseek.json"
REBALANCE_TARGET_FILE = ROOT / "rebalance_target.json"

# 共用分类档 — 跨专案 (firstrade_gui_plus 等也读同一份)
SHARED_CLASSIFICATION = Path("d:/python/shared/stock_classification.json")

# ----------------------------------------------------------------------------
# Default watchlist (首次启动时写入 watchlist.json)
# 优先读 d:/python/shared/stock_classification.json,读不到再用下方内建
# ----------------------------------------------------------------------------
_BUILTIN_WATCHLIST: dict[str, dict[str, Any]] = {
    # === AI 基础设施 (Hardware Layer) ===
    # GPU / 加速器
    "NVDA":  {"name": "NVIDIA",       "tag": "GPU · AI 训练 / 推论",       "yf": "NVDA",  "group": "GPU / 加速器",
             "themes": ["AI 加速器", "AI 模型基建", "Mag 7", "Fabless IC"]},
    "AMD":   {"name": "AMD",          "tag": "GPU / CPU · MI300X",         "yf": "AMD",   "group": "GPU / 加速器",
             "themes": ["AI 加速器", "Fabless IC"]},

    # 网路晶片 / Fabric
    "AVGO":  {"name": "Broadcom",     "tag": "网通 · ASIC / 交换器",       "yf": "AVGO",  "group": "网路 Fabric",
             "themes": ["AI 加速器", "Fabless IC", "资讯安全"]},
    "MRVL":  {"name": "Marvell",      "tag": "资料中心 · DPU / 互连",      "yf": "MRVL",  "group": "网路 Fabric",
             "themes": ["AI 加速器", "Fabless IC"]},
    "ANET":  {"name": "Arista",       "tag": "高速交换器 · 云端网路",      "yf": "ANET",  "group": "网路 Fabric",
             "themes": ["AI 模型基建"]},

    # 伺服器组装
    "SMCI":  {"name": "Super Micro",  "tag": "AI 伺服器 / 液冷整合",       "yf": "SMCI",  "group": "伺服器组装",
             "themes": ["AI 模型基建"]},

    # 电力 / 散热 (AI 缺电题材)
    "VRT":   {"name": "Vertiv",       "tag": "资料中心散热 / UPS",         "yf": "VRT",   "group": "电力 / 散热",
             "themes": ["AI 资料中心电力"]},
    "CEG":   {"name": "Constellation","tag": "核能电力 · AI 缺电题材",     "yf": "CEG",   "group": "电力 / 散热",
             "themes": ["AI 资料中心电力", "核能"]},
    "ETN":   {"name": "Eaton",        "tag": "电力管理 · 配电",            "yf": "ETN",   "group": "电力 / 散热",
             "themes": ["AI 资料中心电力", "工业"]},
    "VST":   {"name": "Vistra",       "tag": "发电 · 天然气/核能",         "yf": "VST",   "group": "电力 / 散热",
             "themes": ["AI 资料中心电力", "核能"]},
    "EOSE":  {"name": "Eos Energy",   "tag": "储能 · 锌电池",              "yf": "EOSE",  "group": "电力 / 散热",
             "themes": ["储能", "AI 资料中心电力"]},

    # Hyperscalers (买家层 / 资本支出端)
    "MSFT":  {"name": "Microsoft",    "tag": "云端 Azure / Copilot",        "yf": "MSFT",  "group": "Hyperscalers",
             "themes": ["云三强", "Mag 7", "AI 模型", "企业软体"]},
    "GOOGL": {"name": "Alphabet",     "tag": "搜寻 / 广告 / GCP",           "yf": "GOOGL", "group": "Hyperscalers",
             "themes": ["云三强", "Mag 7", "AI 模型", "自驾 / Robotaxi", "媒体 / 广告"]},
    "AMZN":  {"name": "Amazon",       "tag": "AWS / 电商",                  "yf": "AMZN",  "group": "Hyperscalers",
             "themes": ["云三强", "Mag 7", "电商 / 消费"]},
    "META":  {"name": "Meta",         "tag": "社群 / Llama / Reality Labs", "yf": "META",  "group": "Hyperscalers",
             "themes": ["Mag 7", "AI 模型", "媒体 / 广告"]},

    # === 半导体循环 (Semiconductor Cycles) ===
    # 半导体设备 (WFE)
    "ASML":  {"name": "ASML",         "tag": "微影设备 · EUV 垄断",        "yf": "ASML",  "group": "半导体设备",
             "themes": ["半导体设备"]},
    "AMAT":  {"name": "Applied Mat.", "tag": "半导体设备 · 蚀刻沉积",      "yf": "AMAT",  "group": "半导体设备",
             "themes": ["半导体设备"]},
    "LRCX":  {"name": "Lam Research", "tag": "半导体设备 · 蚀刻龙头",      "yf": "LRCX",  "group": "半导体设备",
             "themes": ["半导体设备"]},
    "KLAC":  {"name": "KLA Corp",     "tag": "半导体检测 / 量测",          "yf": "KLAC",  "group": "半导体设备",
             "themes": ["半导体设备"]},

    # 晶圆代工
    "TSM":   {"name": "TSMC ADR",     "tag": "晶圆代工 · 全球领导",        "yf": "TSM",   "group": "晶圆代工",
             "themes": ["AI 加速器", "半导体代工"]},

    # IC 设计
    "QCOM":  {"name": "Qualcomm",     "tag": "手机 SoC / 5G",               "yf": "QCOM",  "group": "IC 设计",
             "themes": ["Fabless IC"]},
    "ARM":   {"name": "Arm Holdings", "tag": "IP / 矽智财",                 "yf": "ARM",   "group": "IC 设计",
             "themes": ["Fabless IC", "AI 加速器"]},

    # 记忆体
    "MU":    {"name": "Micron",       "tag": "记忆体 · DRAM / HBM",         "yf": "MU",    "group": "记忆体",
             "themes": ["AI 模型基建"]},

    # EDA / IP 工具
    "SNPS":  {"name": "Synopsys",     "tag": "EDA · 晶片设计工具",          "yf": "SNPS",  "group": "EDA / IP 工具",
             "themes": ["半导体设计"]},
    "CDNS":  {"name": "Cadence",      "tag": "EDA · 矽智财 / 模拟",         "yf": "CDNS",  "group": "EDA / IP 工具",
             "themes": ["半导体设计"]},

    # === 资讯安全 (Cybersecurity) ===
    "CRWD":  {"name": "CrowdStrike",        "tag": "Cybersecurity · 端点防护",       "yf": "CRWD", "group": "资讯安全",
             "themes": ["资讯安全", "AI 应用"]},
    "PANW":  {"name": "Palo Alto Networks", "tag": "Cybersecurity · Network/Cloud",  "yf": "PANW", "group": "资讯安全",
             "themes": ["资讯安全"]},
    "ZS":    {"name": "Zscaler",            "tag": "Cybersecurity · Zero Trust SASE","yf": "ZS",   "group": "资讯安全",
             "themes": ["资讯安全"]},
    "FTNT":  {"name": "Fortinet",           "tag": "Cybersecurity · 防火墙/SD-WAN",  "yf": "FTNT", "group": "资讯安全",
             "themes": ["资讯安全"]},
    "S":     {"name": "SentinelOne",        "tag": "Cybersecurity · AI 端点防护",    "yf": "S",    "group": "资讯安全",
             "themes": ["资讯安全", "AI 应用"]},
    "RBRK":  {"name": "Rubrik",             "tag": "Cybersecurity · 资料安全/备份",  "yf": "RBRK", "group": "资讯安全",
             "themes": ["资讯安全"]},

    # === 云基础 / 开发者工具 (Cloud Infra / DevTools) ===
    "SNOW":  {"name": "Snowflake",          "tag": "Data Cloud · Warehouse",          "yf": "SNOW", "group": "云基础 / 开发者工具",
             "themes": ["AI 模型基建", "企业软体"]},
    "DDOG":  {"name": "Datadog",            "tag": "Observability · APM/Log",         "yf": "DDOG", "group": "云基础 / 开发者工具",
             "themes": ["企业软体"]},
    "NET":   {"name": "Cloudflare",         "tag": "Edge CDN · Zero Trust",           "yf": "NET",  "group": "云基础 / 开发者工具",
             "themes": ["资讯安全", "企业软体"]},
    "MDB":   {"name": "MongoDB",            "tag": "NoSQL DB · Atlas",                "yf": "MDB",  "group": "云基础 / 开发者工具",
             "themes": ["企业软体"]},

    # === 企业软体 / Vertical SaaS ===
    "CRM":   {"name": "Salesforce",         "tag": "CRM / Sales Cloud",               "yf": "CRM",  "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体", "AI 应用"]},
    "NOW":   {"name": "ServiceNow",         "tag": "Workflow Automation",             "yf": "NOW",  "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体", "AI 应用"]},
    "WDAY":  {"name": "Workday",            "tag": "HR / Finance SaaS",               "yf": "WDAY", "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体"]},
    "ADBE":  {"name": "Adobe",              "tag": "Creative Cloud · 内容软体",       "yf": "ADBE", "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体", "AI 应用"]},
    "ORCL":  {"name": "Oracle",             "tag": "资料库 / Oracle Cloud",           "yf": "ORCL", "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体", "AI 模型基建"]},
    "FIG":   {"name": "Figma",              "tag": "设计协作 SaaS",                   "yf": "FIG",  "group": "企业软体 / Vertical SaaS",
             "themes": ["企业软体"]},

    # === AI 应用层 ===
    "PLTR":  {"name": "Palantir",           "tag": "AI 应用 · Gov/Enterprise",        "yf": "PLTR", "group": "AI 应用层",
             "themes": ["AI 应用", "政府 / 国防", "企业软体"]},
    "AI":    {"name": "C3.ai",              "tag": "Enterprise AI Apps",              "yf": "AI",   "group": "AI 应用层",
             "themes": ["AI 应用", "企业软体"]},
    "SOUN":  {"name": "SoundHound AI",      "tag": "Voice AI · Auto/Restaurant",      "yf": "SOUN", "group": "AI 应用层",
             "themes": ["AI 应用"]},

    # === EV / 自驾 ===
    "TSLA":  {"name": "Tesla",        "tag": "EV / 自驾 / Robotaxi",       "yf": "TSLA",  "group": "EV / 自驾",
             "themes": ["EV", "自驾 / Robotaxi", "Mag 7", "AI 应用", "储能"]},

    # === 消费电子 ===
    "AAPL":  {"name": "Apple",        "tag": "消费电子 · iPhone",          "yf": "AAPL",  "group": "消费电子",
             "themes": ["Mag 7", "Fabless IC"]},

    # === 媒体 / 串流 ===
    "NFLX":  {"name": "Netflix",      "tag": "串流媒体",                    "yf": "NFLX",  "group": "媒体 / 串流",
             "themes": ["媒体 / 广告"]},

    # === 金融 ===
    "JPM":   {"name": "JPMorgan",     "tag": "大型银行",                    "yf": "JPM",   "group": "金融 / 银行",
             "themes": ["金融"]},

    # === 金融科技 (Fintech) ===
    "SOFI":  {"name": "SoFi",         "tag": "Fintech · 数位银行",          "yf": "SOFI",  "group": "金融科技",
             "themes": ["金融", "AI 应用"]},

    # === 太空 / 国防 ===
    "ASTS":  {"name": "AST SpaceMobile", "tag": "卫星 · 直连手机",          "yf": "ASTS",  "group": "太空 / 国防",
             "themes": ["太空 / 卫星", "政府 / 国防"]},

    # === 餐饮 / 消费 ===
    "CAVA":  {"name": "Cava Group",   "tag": "地中海连锁快餐",              "yf": "CAVA",  "group": "餐饮 / 消费",
             "themes": ["消费"]},

    # === 油气 / 能源服务 ===
    "TTI":   {"name": "Tetra Tech.",  "tag": "能源服务 · 油气/水管理",      "yf": "TTI",   "group": "能源 / 油气",
             "themes": ["能源"]},

    # === 大盘 ETF ===
    "IVV":   {"name": "iShares S&P 500", "tag": "S&P 500 ETF",              "yf": "IVV",   "group": "大盘 ETF",
             "themes": ["大盘指数"]},
    "QQQ":   {"name": "Invesco QQQ",     "tag": "Nasdaq-100 ETF",           "yf": "QQQ",   "group": "大盘 ETF",
             "themes": ["大盘指数"]},
    "VTI":   {"name": "Vanguard Total",  "tag": "全市场 ETF",                "yf": "VTI",   "group": "大盘 ETF",
             "themes": ["大盘指数"]},

    # === 特殊 ETF / 投资工具 ===
    "DXYZ":  {"name": "Destiny Tech100", "tag": "Pre-IPO 科技基金",         "yf": "DXYZ",  "group": "特殊 ETF / 投资工具",
             "themes": []},
    "TSLR":  {"name": "Defiance 2x TSLA","tag": "TSLA 2x 杠杆 ETF",          "yf": "TSLR",  "group": "特殊 ETF / 投资工具",
             "themes": ["EV"]},
}


def _is_tw_symbol(code: str, meta: dict) -> bool:
    """台股代号（纯数字 / .TW / .TWO）。美股站要过滤掉。"""
    c = str(code).upper().strip()
    yf = str(meta.get("yf", "")).upper()
    return c.isdigit() or c.endswith(".TW") or c.endswith(".TWO") or yf.endswith(".TW") or yf.endswith(".TWO")


def _load_default_watchlist() -> dict[str, dict[str, Any]]:
    """优先读 d:/python/shared/stock_classification.json,失败 fallback 内建。

    shared 档自 2026-05-29 起含台股；美股站只取美股，过滤掉台股代号。
    """
    if SHARED_CLASSIFICATION.exists():
        try:
            data = json.loads(SHARED_CLASSIFICATION.read_text(encoding="utf-8"))
            us_only = {c: m for c, m in data.items() if isinstance(m, dict) and not _is_tw_symbol(c, m)}
            for code, m in us_only.items():
                m.setdefault("yf", code)
                m.setdefault("themes", [])
            print(f"[init] 已从 {SHARED_CLASSIFICATION} 载入 {len(us_only)} 档美股分类（已过滤台股）")
            return us_only
        except Exception as e:
            print(f"[init] {SHARED_CLASSIFICATION} 读取失败,改用内建: {e}")
    return _BUILTIN_WATCHLIST


DEFAULT_WATCHLIST: dict[str, dict[str, Any]] = _load_default_watchlist()

CACHE_TTL = 300
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)


def cache_set_ttl(key: str, val: Any, ttl_seconds: int) -> None:
    """自订 TTL 写入快取 (覆盖预设 CACHE_TTL)。用于 Gemini 等高成本资料。"""
    # 利用「把储存时间设未来」的技巧延后失效
    _cache[key] = (time.time() + ttl_seconds - CACHE_TTL, val)


# ----------------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------------
def load_json(p: Path, default: Any) -> Any:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(p: Path, data: Any) -> None:
    with _lock:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate_watchlist_groups(wl: dict) -> dict:
    """套用 DEFAULT_WATCHLIST 的最新族群分类 + themes 到既有清单。
    规则：
    - 既有代号若在 DEFAULT 内，更新 group / tag / themes 为新版
    - 既有代号不在 DEFAULT 内（user 自行加的）→ 保留 group/tag，themes 预设空
    """
    changed = False
    for code, default_meta in DEFAULT_WATCHLIST.items():
        if code in wl:
            cur = wl[code]
            default_themes = default_meta.get("themes", [])
            if (cur.get("group") != default_meta["group"]
                or cur.get("tag") != default_meta["tag"]
                or cur.get("themes") != default_themes):
                cur["group"]  = default_meta["group"]
                cur["tag"]    = default_meta["tag"]
                cur["themes"] = list(default_themes)
                if not cur.get("name"):
                    cur["name"] = default_meta["name"]
                changed = True
    # 确保 user 自加的也有 themes 栏位 (预设 [])
    for code, cur in wl.items():
        if "themes" not in cur:
            cur["themes"] = []
            changed = True
    if changed:
        save_json(WATCHLIST_FILE, wl)
        print(f"[migrate] watchlist 分类 + themes 已套用最新版")
    return wl


def load_watchlist() -> dict:
    wl = load_json(WATCHLIST_FILE, None)
    if wl is None:
        save_json(WATCHLIST_FILE, DEFAULT_WATCHLIST)
        return DEFAULT_WATCHLIST.copy()
    return _migrate_watchlist_groups(wl)


def load_alerts() -> dict:
    return load_json(ALERTS_FILE, {})

def load_feishu() -> dict:
    return load_json(FEISHU_FILE, {"webhook": "", "secret": ""})

# ----------------------------------------------------------------------------
# 技术指标
# ----------------------------------------------------------------------------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi_indicator(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd_indicator(s: pd.Series):
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def kd_indicator(df: pd.DataFrame, n: int = 9):
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    rsv = (df["Close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d


def safe(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        if np.isnan(v) or np.isinf(v):
            return None
        return float(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    return v


# ----------------------------------------------------------------------------
# 讯号侦测
# ----------------------------------------------------------------------------
def detect_signals(closes: pd.Series, ma5_s: pd.Series, ma20_s: pd.Series,
                   k_s: pd.Series, d_s: pd.Series, rsi_s: pd.Series,
                   highs: pd.Series, lows: pd.Series, period: str = "D") -> list[dict]:
    sigs: list[dict] = []
    # 各周期的 breakout 视窗与标签
    breakout_cfg = {
        "D": (20, "20 日"),
        "W": (20, "20 周"),
        "M": (12, "12 月"),
    }
    bk_n, bk_label = breakout_cfg.get(period, breakout_cfg["D"])

    if len(closes) < max(21, bk_n + 1):
        return sigs

    # 均线黄金 / 死亡交叉 (MA5 vs MA20)
    if not pd.isna(ma5_s.iloc[-1]) and not pd.isna(ma20_s.iloc[-2]):
        if ma5_s.iloc[-2] <= ma20_s.iloc[-2] and ma5_s.iloc[-1] > ma20_s.iloc[-1]:
            sigs.append({"key": "golden_cross", "label": "🌟黄金交叉", "color": "red"})
        elif ma5_s.iloc[-2] >= ma20_s.iloc[-2] and ma5_s.iloc[-1] < ma20_s.iloc[-1]:
            sigs.append({"key": "death_cross", "label": "💀死亡交叉", "color": "green"})

    # KD 黄金 / 死亡交叉
    if len(k_s) >= 2:
        last_k, prev_k = float(k_s.iloc[-1]), float(k_s.iloc[-2])
        last_d, prev_d = float(d_s.iloc[-1]), float(d_s.iloc[-2])
        if prev_k <= prev_d and last_k > last_d and last_k < 50:
            sigs.append({"key": "kd_cross_up", "label": "🔼KD 黄金交叉", "color": "red"})
        elif prev_k >= prev_d and last_k < last_d and last_k > 50:
            sigs.append({"key": "kd_cross_dn", "label": "🔽KD 死亡交叉", "color": "green"})
        if last_k > 80:
            sigs.append({"key": "kd_overbought", "label": "⚠️KD 超买", "color": "orange"})
        elif last_k < 20:
            sigs.append({"key": "kd_oversold", "label": "🚀KD 超卖", "color": "cyan"})

    # RSI 超买 / 超卖
    if not pd.isna(rsi_s.iloc[-1]):
        rsi_v = float(rsi_s.iloc[-1])
        if rsi_v > 75:
            sigs.append({"key": "rsi_overbought", "label": "🔥RSI 超买", "color": "orange"})
        elif rsi_v < 25:
            sigs.append({"key": "rsi_oversold", "label": "❄️RSI 超卖", "color": "cyan"})

    # 突破新高 / 跌破新低
    price = float(closes.iloc[-1])
    high_n = float(highs.iloc[-(bk_n+1):-1].max())
    low_n  = float(lows.iloc[-(bk_n+1):-1].min())
    if price > high_n * 1.001:
        sigs.append({"key": "breakout_high", "label": f"🚀 突破 {bk_label}新高", "color": "red"})
    if price < low_n * 0.999:
        sigs.append({"key": "breakdown_low", "label": f"📉 跌破 {bk_label}新低", "color": "green"})

    return sigs


# ----------------------------------------------------------------------------
# 分析师评等 (yfinance.recommendations) — 取代台股版的三大法人
# ----------------------------------------------------------------------------
def fetch_institutional(yf_code: str, days: int = 10) -> dict | None:
    """回传近 N 个月分析师评等变化。
    沿用台股版 institutional 资料形状让前端不必改：
        fi (外资)   → strongBuy
        it (投信)   → buy
        dealer (自营商) → hold
    使用者一看颜色即可分辨。负面评等(sell/strongSell) 加总后以 negative 一个栏位回传。
    """
    cached = cache_get(f"inst:{yf_code}")
    if cached:
        return cached
    try:
        rec = yf.Ticker(yf_code).recommendations
    except Exception as e:
        print(f"[rec] {yf_code}: {e}")
        return None
    if rec is None or len(rec) == 0:
        return None

    df = rec.copy()
    if "period" in df.columns:
        df = df.set_index("period")
    df = df.sort_index().tail(days)
    if df.empty:
        return None

    # 期间 label: 0m / -1m / -2m...
    labels = []
    for idx in df.index:
        s = str(idx)
        labels.append(s if s.startswith("-") or s == "0m" else s)

    sb = df.get("strongBuy", pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    b  = df.get("buy",        pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    h  = df.get("hold",       pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    sl = df.get("sell",       pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    ssl= df.get("strongSell", pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    negative = [-(s + ss) for s, ss in zip(sl, ssl)]  # 正负标示

    out = {
        "dates":  labels,
        "fi":     sb,        # strongBuy → 对齐「外资」位置
        "it":     b,         # buy
        "dealer": h,         # hold
        "neg":    negative,  # sell + strongSell 合并（显示为负值）
        "_legend": {"fi": "Strong Buy", "it": "Buy", "dealer": "Hold", "neg": "Sell"},
    }
    cache_set(f"inst:{yf_code}", out)
    return out


# ----------------------------------------------------------------------------
# 多周期 K 线资料抓取
# ----------------------------------------------------------------------------
PERIOD_CFG = {
    "D": {"period": "1y",  "interval": "1d",  "n": 80,  "label": "日"},
    "W": {"period": "3y",  "interval": "1wk", "n": 80,  "label": "周"},
    "M": {"period": "10y", "interval": "1mo", "n": 80,  "label": "月"},
}


def fetch_stock(code: str, period: str = "D", force: bool = False) -> dict:
    period = period.upper() if period.upper() in PERIOD_CFG else "D"
    cache_key = f"stock:{code}:{period}"
    if not force:
        cached = cache_get(cache_key)
        if cached:
            return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, f"未追踪股票 {code}")

    cfg = PERIOD_CFG[period]
    hist = get_history(info["yf"])          # 本地 MySQL 日线全量
    if hist.empty:
        raise HTTPException(503, f"MySQL 取不到 {code} ({info['yf']}) 资料")
    hist = hist.dropna(subset=["Close"])
    # MySQL 仅存日线; 周/月线由日线重采样得到
    if period == "W":
        hist = hist.resample("W").agg({"Open": "first", "High": "max", "Low": "min",
                                       "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    elif period == "M":
        hist = hist.resample("ME").agg({"Open": "first", "High": "max", "Low": "min",
                                        "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])

    closes = hist["Close"]
    ma5_s, ma20_s, ma60_s = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    rsi_s = rsi_indicator(closes, 14)
    dif_s, dea_s, macd_s = macd_indicator(closes)
    k_s, d_s = kd_indicator(hist, 9)

    last = hist.iloc[-1]
    prev_close = float(hist.iloc[-2]["Close"]) if len(hist) > 1 else float(last["Close"])
    N = cfg["n"]
    recent = hist.iloc[-N:]

    klines = [
        {
            "date": idx.strftime("%m/%d") if period == "D" else idx.strftime("%Y/%m" if period == "M" else "%m/%d"),
            "ohlc": [
                round(float(r["Open"]),  2),
                round(float(r["Close"]), 2),
                round(float(r["Low"]),   2),
                round(float(r["High"]),  2),
            ],
            "volume": int(r["Volume"]) if r["Volume"] else 0,    # 美股: shares (整数)
        }
        for idx, r in recent.iterrows()
    ]

    def slice_series(s: pd.Series) -> list:
        return [safe(v) for v in s.iloc[-N:].tolist()]

    price = float(last["Close"])
    ma5_v  = safe(ma5_s.iloc[-1])  or price
    ma20_v = safe(ma20_s.iloc[-1]) or price
    ma60_v = safe(ma60_s.iloc[-1]) or price

    if ma5_v > ma20_v > ma60_v:
        ma_status = "多头排列"
    elif ma5_v < ma20_v < ma60_v:
        ma_status = "空头排列"
    else:
        ma_status = "盘整"

    if price > ma20_v and price > ma60_v:
        trend = "多头趋势"
    elif price < ma20_v and price < ma60_v:
        trend = "空头趋势"
    else:
        trend = "盘整"

    # 美股 volume 单位为 shares，不像台股要除以 1000
    avg_vol_5 = float(hist["Volume"].iloc[-5:].mean())
    today_vol = float(last["Volume"])
    vol_change = (today_vol - avg_vol_5) / avg_vol_5 * 100 if avg_vol_5 > 0 else 0.0

    rsi_v = safe(rsi_s.iloc[-1]) or 50.0
    bias = (price - ma20_v) / ma20_v * 100 if ma20_v else 0

    if rsi_v > 75 and bias > 10:
        risk = "high"
        risk_note = f"RSI {rsi_v:.1f} 超买 + 乖离 {bias:.1f}%，主力疑似出货。"
        risk_banner = f"高档过热警示：RSI {rsi_v:.1f} + 乖离 {bias:.1f}%"
    elif rsi_v > 70 or bias > 8:
        risk = "mid"
        risk_note = f"短线过热（RSI {rsi_v:.1f}），等待回测。"
        risk_banner = "中度观察：技术指标过热"
    elif rsi_v < 30:
        risk = "mid"
        risk_note = f"RSI {rsi_v:.1f} 超卖，可分批承接。"
        risk_banner = "短线超卖，留意止稳"
    else:
        risk = "low"
        risk_note = "技术面健康，筹码稳定。"
        risk_banner = "趋势明确 · 操作偏中性"

    span = float(last["High"]) - float(last["Low"])
    if span <= 0:
        outer_pct = 0.5
    else:
        outer_pct = max(0.3, min(0.7, (float(last["Close"]) - float(last["Low"])) / span))
    outer_v = int(round(today_vol * outer_pct))
    inner_v = max(0, int(round(today_vol)) - outer_v)

    high60 = float(recent["High"].max())
    low60  = float(recent["Low"].min())
    if price > ma20_v:
        supports = sorted({int(round(ma20_v)), int(round(ma60_v))})
        resists = sorted({int(round(min(high60, price * 1.05))), int(round(high60))})
    else:
        supports = sorted({int(round(low60)), int(round(ma60_v))})
        resists = sorted({int(round(ma20_v)), int(round(ma5_v))})

    atr = float((hist["High"] - hist["Low"]).iloc[-14:].mean())
    swing = atr if atr > 0 else price * 0.015
    scenario = {
        "up":   {"entry": f"{price:.0f} ~ {price + swing*0.5:.0f}",
                 "sl":    int(round(price - swing * 1.0)),
                 "tp":    f"{int(round(price + swing*2))} / {int(round(price + swing*4))}"},
        "flat": {"entry": f"{price - swing*0.7:.0f} ~ {price - swing*0.2:.0f}",
                 "sl":    int(round(price - swing * 1.5)),
                 "tp":    f"{int(round(price + swing*1))} / {int(round(price + swing*2.5))}"},
        "down": {"entry": f"{price - swing*2:.0f} ~ {price - swing*1.2:.0f}",
                 "sl":    int(round(price - swing * 3)),
                 "tp":    f"{int(round(price - swing*0.3))} / {int(round(price + swing*1))}"},
    }

    inst = fetch_institutional(info["yf"]) or {"dates": [], "fi": [], "it": [], "dealer": [], "neg": []}
    fi_today = inst["fi"][-1] if inst["fi"] else 0      # Strong Buy
    it_today = inst["it"][-1] if inst["it"] else 0      # Buy
    dealer_today = inst["dealer"][-1] if inst["dealer"] else 0  # Hold
    neg_today = abs(inst.get("neg", [0])[-1]) if inst.get("neg") else 0  # Sell + StrongSell
    fi_5  = sum(inst["fi"][-5:])  if inst["fi"] else 0
    fi_10 = sum(inst["fi"])       if inst["fi"] else 0
    it_10 = sum(inst["it"])       if inst["it"] else 0

    bullish = fi_today + it_today
    bearish = neg_today
    if bullish > bearish * 2 and bullish >= 5:
        chip_note = f"分析师偏多：Strong Buy {fi_today} + Buy {it_today} 明显多于 Sell {neg_today}。"
    elif bearish > bullish:
        chip_note = f"分析师偏空：Sell {neg_today} 多于买进评等 ({bullish})。"
    else:
        chip_note = f"分析师中性：Buy {bullish} / Hold {dealer_today} / Sell {bearish}。"

    # 讯号（日/周/月皆计算，自动换 lookback 视窗）
    signals = detect_signals(closes, ma5_s, ma20_s, k_s, d_s, rsi_s, hist["High"], hist["Low"], period=period)

    out = {
        "code":   code,
        "name":   info["name"],
        "tag":    info["tag"],
        "group":  info.get("group", "自选"),
        "themes": info.get("themes", []) or [],
        "period": period,
        "price":   round(price, 2),
        "prev":    round(prev_close, 2),
        "open":    round(float(last["Open"]),  2),
        "high":    round(float(last["High"]),  2),
        "low":     round(float(last["Low"]),   2),
        "volume":  int(round(today_vol)),
        "avgVol":  int(round(avg_vol_5)),
        "inner":   inner_v,
        "outer":   outer_v,
        "ma5":  round(ma5_v, 2),
        "ma20": round(ma20_v, 2),
        "ma60": round(ma60_v, 2),
        "rsi":   round(rsi_v, 2),
        "kd_k":  round(safe(k_s.iloc[-1])  or 0, 2),
        "kd_d":  round(safe(d_s.iloc[-1])  or 0, 2),
        "dif":   round(safe(dif_s.iloc[-1]) or 0, 2),
        "dea":   round(safe(dea_s.iloc[-1]) or 0, 2),
        "macd":  round(safe(macd_s.iloc[-1]) or 0, 2),
        "volChange": round(vol_change, 1),
        "trend":     trend,
        "maStatus":  ma_status,
        "risk":       risk,
        "riskNote":   risk_note,
        "riskBanner": risk_banner,
        "resist":  resists,
        "support": supports,
        "klines":      klines,
        "ma5_series":  slice_series(ma5_s),
        "ma20_series": slice_series(ma20_s),
        "ma60_series": slice_series(ma60_s),
        "scenario": scenario,
        "institutional": inst,
        "chip": {
            "fi_today": fi_today, "it_today": it_today, "dealer_today": dealer_today,
            "fi_5": fi_5, "fi_10": fi_10, "it_10": it_10,
            "conclusion": chip_note,
        },
        "signals": signals,
        "asOf":    str(hist.index[-1].date()),
        "source":  "live",
    }
    cache_set(cache_key, out)

    # 顺便检查警示
    if period == "D":
        try:
            check_alert(code, price, prev_close, info["name"], rsi=rsi_v, signals=signals)
        except Exception as e:
            print(f"[alert check] {code}: {e}")

    return out


# ----------------------------------------------------------------------------
# 新闻 (yfinance 英文 + Gemini 自动翻译成繁体中文)
# ----------------------------------------------------------------------------
import re as _re


def _fetch_yfinance_news(yf_code: str) -> list:
    try:
        raw = yf.Ticker(yf_code).news or []
        out = []
        for n in raw[:12]:
            content = n.get("content") or n
            title = content.get("title") or n.get("title") or ""
            publisher = (
                (content.get("provider") or {}).get("displayName")
                or n.get("publisher") or ""
            )
            link = (
                (content.get("clickThroughUrl") or {}).get("url")
                or (content.get("canonicalUrl") or {}).get("url")
                or n.get("link") or ""
            )
            ts = n.get("providerPublishTime") or 0
            pub_date = content.get("pubDate", "")
            if not ts and pub_date:
                try:
                    ts = int(pd.Timestamp(pub_date).timestamp())
                except Exception:
                    ts = 0
            out.append({"title": title, "publisher": publisher, "link": link, "time": ts})
        return out
    except Exception as e:
        print(f"[yf news] {yf_code}: {e}")
        return []

# DeepSeek (OpenAI 兼容接口) ------------------------------------------------
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODELS = ("deepseek-chat",)   # 需深度推理可改 "deepseek-reasoner"

def _deepseek_call(key: str, prompt: str) -> str:
    """调用 DeepSeek Chat (OpenAI 兼容)。依序尝试模型, 回传首个成功文本。"""
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    last_err = None
    for model_name in DEEPSEEK_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                stream=False,
            )
            txt = (resp.choices[0].message.content or "").strip()
            if txt:
                return txt
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("DeepSeek all models failed")

# 兼容旧调用名: 翻译/情感/AI评论里的 _gemini_call(...) 无需逐处改
_gemini_call = _deepseek_call


def _gemini_translate_batch(titles: list[str], key: str) -> list[str] | None:
    """Gemini 批次翻译（一次 API call 翻全部）。失败回 None。"""
    try:
        prompt = (
            "把以下英文新闻标题翻译成繁体中文（台湾用语），保持简洁自然，"
            "每行一条翻译对应一条原文，顺序对齐，不要加编号或解释：\n\n"
            + "\n".join(titles)
        )
        text = _gemini_call(key, prompt)
        lines = [_re.sub(r"^\s*\d+[\.\)]\s*", "", l).strip()
                 for l in text.split("\n") if l.strip()]
        # 行数要对齐；不齐就退到 None 走 fallback
        if len(lines) == len(titles):
            return lines
        if len(lines) >= len(titles):
            return lines[:len(titles)]
        return None
    except Exception as e:
        print(f"[translate-gemini] {e}")
        return None


def _gtx_translate_one(text: str) -> str | None:
    """Google Translate 免费（unofficial）端点，单笔翻译，无需 API key。"""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-TW", "dt": "t", "q": text},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        # data[0] 为分段阵列，每段 [译文, 原文, ...]
        chunks = data[0] or []
        out = "".join((c[0] or "") for c in chunks if c)
        return out.strip() or None
    except Exception as e:
        print(f"[translate-gtx] {e}")
        return None


def _translate_titles_zh(titles: list[str]) -> list[str]:
    """翻译英文新闻标题为繁体中文。
    优先用 Gemini（品质好+一次批次）；没设 key 或失败时退回 Google Translate
    免费端点（无 key、逐条翻译）；最终 fallback 才回传原英文。
    """
    if not titles:
        return titles

    cache_k = f"trans:{hash(tuple(titles))}"
    cached = cache_get(cache_k)
    if cached:
        return cached

    # 1) Gemini 批次（如果有 key）
    key = _get_gemini_key()
    if key:
        result = _gemini_translate_batch(titles, key)
        if result:
            cache_set(cache_k, result)
            return result

    # 2) Google Translate 免费端点（逐条）
    out = []
    success = 0
    for t in titles:
        zh = _gtx_translate_one(t)
        if zh:
            out.append(zh); success += 1
        else:
            out.append(t)  # 翻译失败保留原文
    if success > 0:
        cache_set(cache_k, out)
        return out

    # 3) 全部失败 → 原文
    return titles


def _gemini_sentiment_batch(titles: list[str], key: str) -> list[str]:
    """批次情感分析。回传 list of 'positive'/'negative'/'neutral'。失败回 ['neutral'] * N"""
    if not titles:
        return []
    try:
        prompt = (
            "判断以下新闻标题对该股票的情感影响，每行回 positive / negative / neutral 之一，"
            "不要加编号或解释，顺序对齐：\n\n" + "\n".join(titles)
        )
        text = _gemini_call(key, prompt)
        lines = [l.strip().lower() for l in text.split("\n") if l.strip()]
        out = []
        for i in range(len(titles)):
            l = lines[i] if i < len(lines) else ""
            if "pos" in l: out.append("positive")
            elif "neg" in l: out.append("negative")
            else: out.append("neutral")
        return out
    except Exception as e:
        print(f"[sentiment] {e}")
        return ["neutral"] * len(titles)


def fetch_news(code: str) -> list:
    cached = cache_get(f"news:{code}")
    if cached:
        return cached
    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        return []

    raw = _fetch_yfinance_news(info["yf"])
    if not raw:
        return []

    # 批次翻译标题（一次 API call）
    orig_titles = [n["title"] for n in raw]
    zh_titles   = _translate_titles_zh(orig_titles)

    # 批次情感分析（若有 Gemini key）— 用中文标题分析
    key = _get_gemini_key()
    sentiments = _gemini_sentiment_batch(zh_titles, key) if key else ["neutral"] * len(raw)

    out = []
    for i, n in enumerate(raw):
        out.append({
            "title":      zh_titles[i] if i < len(zh_titles) else n["title"],
            "title_orig": n["title"],
            "publisher":  n["publisher"],
            "link":       n["link"],
            "time":       n["time"],
            "sentiment":  sentiments[i] if i < len(sentiments) else "neutral",
        })
    cache_set_ttl(f"news:{code}", out, 3600)  # 1 小时 — 翻译+情感不需要 5 分钟更新
    return out


# ----------------------------------------------------------------------------
# 内部人交易 (yfinance.Ticker.insider_transactions，免 key 即时)
# ----------------------------------------------------------------------------
def fetch_insider(code: str, days: int = 180) -> dict:
    """近 N 天的 SEC Form 4 内部人交易，分类 buy/sell 并算净值。
    cache 1 小时（内部人通常几天才动一次）。
    """
    cache_key = f"insider:{code}:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        return {"transactions": [], "summary": {"buy_value": 0, "sell_value": 0, "net_value": 0,
                                                  "buy_count": 0, "sell_count": 0}}
    try:
        df = yf.Ticker(info["yf"]).insider_transactions
    except Exception as e:
        print(f"[insider] {code}: {e}")
        df = None
    if df is None or df.empty:
        out = {"transactions": [], "summary": {"buy_value": 0, "sell_value": 0, "net_value": 0,
                                                 "buy_count": 0, "sell_count": 0}}
        cache_set(cache_key, out)
        return out

    df = df.copy()
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    df = df.dropna(subset=["Start Date"])
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=days)
    df = df[df["Start Date"] >= cutoff].sort_values("Start Date", ascending=False)

    buy_total = 0.0
    sell_total = 0.0
    txs = []
    for _, r in df.iterrows():
        txt = str(r.get("Text", ""))
        if "Purchase" in txt or "Buy" in txt:
            action = "buy"
        elif "Sale" in txt or "Sell" in txt or "Sold" in txt:
            action = "sell"
        else:
            action = "other"
        # yfinance 某些纪录（如赠与、选择权行权）的 Value/Shares 可能是 NaN
        raw_val = r.get("Value")
        val = float(raw_val) if raw_val is not None and not pd.isna(raw_val) else 0.0
        raw_sh = r.get("Shares")
        shares = int(raw_sh) if raw_sh is not None and not pd.isna(raw_sh) else 0
        if action == "buy":  buy_total += val
        elif action == "sell": sell_total += val
        txs.append({
            "date":     r["Start Date"].strftime("%Y-%m-%d"),
            "insider":  str(r.get("Insider", "")),
            "position": str(r.get("Position", "")),
            "action":   action,
            "shares":   shares,
            "value":    int(val),
            "text":     txt[:60],
        })

    net = buy_total - sell_total
    out = {
        "code": code,
        "days": days,
        "transactions": txs[:30],
        "summary": {
            "buy_value":  int(buy_total),
            "sell_value": int(sell_total),
            "net_value":  int(net),
            "buy_count":  sum(1 for t in txs if t["action"] == "buy"),
            "sell_count": sum(1 for t in txs if t["action"] == "sell"),
            "total_count": len(txs),
        }
    }
    # cache 1 小时
    _cache[cache_key] = (time.time() + 3600 - CACHE_TTL, out)
    return out


# ----------------------------------------------------------------------------
# 13F 机构持股 (yfinance Ticker.institutional_holders / major_holders)
# ----------------------------------------------------------------------------
def fetch_institutional_holders(code: str) -> dict:
    """Top 10 机构持股 + Top 5 mutual fund + 整体机构/内部人 %。
    SEC 13F 是季报延迟 45 天，cache 6 小时即可。
    """
    cache_key = f"inst13f:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)
    try:
        t = yf.Ticker(info["yf"])
        inst_df = t.institutional_holders
        mf_df   = t.mutualfund_holders
        major   = t.major_holders
    except Exception as e:
        print(f"[13F] {code}: {e}")
        inst_df = mf_df = major = None

    def parse_holder(df, top_n):
        out = []
        if df is None or df.empty:
            return out
        for _, r in df.head(top_n).iterrows():
            shares = r.get("Shares")
            value  = r.get("Value")
            pct_h  = r.get("pctHeld")
            pct_c  = r.get("pctChange")
            date_r = r.get("Date Reported")
            out.append({
                "holder":     str(r.get("Holder", "")),
                "shares":     int(shares) if pd.notna(shares) else 0,
                "value":      int(value)  if pd.notna(value)  else 0,
                "pct_held":   round(float(pct_h) * 100, 2) if pd.notna(pct_h) else 0,
                "pct_change": round(float(pct_c) * 100, 2) if pd.notna(pct_c) else 0,
                "date":       str(date_r) if pd.notna(date_r) else "",
            })
        return out

    inst = parse_holder(inst_df, 10)
    mf   = parse_holder(mf_df, 5)

    pct_insider = pct_institutions = 0.0
    n_institutions = 0
    if major is not None and not major.empty:
        try:
            if "insidersPercentHeld" in major.index:
                pct_insider = float(major.loc["insidersPercentHeld"].iloc[0]) * 100
            if "institutionsPercentHeld" in major.index:
                pct_institutions = float(major.loc["institutionsPercentHeld"].iloc[0]) * 100
            if "institutionsCount" in major.index:
                n_institutions = int(major.loc["institutionsCount"].iloc[0])
        except Exception:
            pass

    top10_pct = sum(h["pct_held"] for h in inst)

    out = {
        "code": code,
        "institutional": inst,
        "mutualfund":    mf,
        "summary": {
            "pct_insider":      round(pct_insider, 2),
            "pct_institutions": round(pct_institutions, 2),
            "top10_pct":        round(top10_pct, 2),
            "n_institutions":   n_institutions,
            "n_top_holders":    len(inst),
        }
    }
    # cache 6 小时 (13F 季报，每天顶多动一两家)
    _cache[cache_key] = (time.time() + 6 * 3600 - CACHE_TTL, out)
    return out


# ----------------------------------------------------------------------------
# 估值指标 (yfinance Ticker.info) + 同族群相对位置
# ----------------------------------------------------------------------------
def fetch_valuation(code: str) -> dict:
    """单股估值 dict (cache 6 hr)。"""
    cache_key = f"valn:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info_meta = wl.get(code)
    if not info_meta:
        raise HTTPException(404)
    row = get_financials_latest(info_meta["yf"])

    # ROE 近似 = 净利润TTM / (市值/PB) 不可得时留空; 这里用 eps/pb 等已有字段映射
    pe_ttm = _fin_num(row, "pe_ttm")
    eps_ttm = _fin_num(row, "eps_ttm")
    rev_ttm = _fin_num(row, "operating_revenue_ttm") or _fin_num(row, "total_operating_revenue")
    np_ttm = _fin_num(row, "np_atsopc_ttm")

    # 利润率 = 归母净利润 / 营收
    profit_margin = (np_ttm / rev_ttm) if (np_ttm is not None and rev_ttm) else None

    out = {
        "code": code,
        "name": info_meta.get("name", code),
        "group": info_meta.get("group", "自选"),
        "trailing_pe": pe_ttm,
        "forward_pe": _fin_num(row, "pe_multiple_markets"),   # 多市场 P/E 作 forward 近似
        "price_book": _fin_num(row, "pb_latest"),
        "price_sales": _fin_num(row, "ps_lyr"),
        "peg": _fin_num(row, "his_peg"),
        "ev_ebitda": None,                                    # 表中无, 留空
        "div_yield": _fin_num(row, "dividend_yield_ttm"),
        "profit_margin": profit_margin,
        "roe": None,                                          # 表中无直接 ROE, 留空 (或后续用净利/净资产推算)
        "earnings_growth": None,                              # 表中无, 留空
        "revenue_growth": None,                               # 单季快照无同比, 留空
        "beta": None,                                         # 表中无
        "market_cap": None,                                   # 表中无
        "eps_ttm": eps_ttm,                                   # 额外补充, 前端可用
        "as_of": str(row.get("trade_date")) if row and row.get("trade_date") else None,
    }
    _cache[cache_key] = (time.time() + 6 * 3600 - CACHE_TTL, out)
    return out

def _percentile_rank(values: list[float], target: float) -> float | None:
    """计算 target 在 values 中的百分位 (0-100)，target 是低值 → 百分位低。"""
    vals = [v for v in values if v is not None and v > 0]
    if not vals or target is None:
        return None
    below = sum(1 for v in vals if v < target)
    return round(below / len(vals) * 100, 1)


def fetch_valuation_ranking() -> list:
    """所有 watchlist 股的估值 + 同族群相对位置。"""
    cache_key = "valn-ranking"
    cached = cache_get(cache_key)
    if cached:
        return cached

    from concurrent.futures import ThreadPoolExecutor
    codes = list(load_watchlist().keys())

    def safe(c):
        try: return fetch_valuation(c)
        except: return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = [r for r in ex.map(safe, codes) if r]

    # 各族群 PE / PEG / P/B 中位数
    from statistics import median
    by_group: dict[str, list[dict]] = {}
    for it in items:
        by_group.setdefault(it["group"], []).append(it)

    group_stats: dict[str, dict] = {}
    for g, members in by_group.items():
        pes  = [m["trailing_pe"] for m in members if m["trailing_pe"] and m["trailing_pe"] > 0]
        pegs = [m["peg"]          for m in members if m["peg"] and m["peg"] > 0]
        pbs  = [m["price_book"]   for m in members if m["price_book"] and m["price_book"] > 0]
        group_stats[g] = {
            "pe_median":   round(median(pes), 2)  if pes  else None,
            "peg_median":  round(median(pegs), 2) if pegs else None,
            "pb_median":   round(median(pbs), 2)  if pbs  else None,
            "n_members":   len(members),
        }

    # 计算每股相对位置（百分位 + 偏离 %）
    for it in items:
        g = it["group"]
        g_pes  = [m["trailing_pe"] for m in by_group[g] if m["trailing_pe"]]
        g_pegs = [m["peg"]          for m in by_group[g] if m["peg"]]
        gs = group_stats[g]
        it["pe_percentile"]  = _percentile_rank(g_pes,  it["trailing_pe"])
        it["peg_percentile"] = _percentile_rank(g_pegs, it["peg"])
        it["pe_vs_group"]    = (round((it["trailing_pe"] - gs["pe_median"]) / gs["pe_median"] * 100, 1)
                                if gs.get("pe_median") and it.get("trailing_pe") else None)
        it["peg_vs_group"]   = (round((it["peg"] - gs["peg_median"]) / gs["peg_median"] * 100, 1)
                                if gs.get("peg_median") and it.get("peg") else None)
        it["group_stats"]    = gs

    cache_set(cache_key, items)
    return items


def insider_signals(code: str) -> list[dict]:
    """从近 90 日内部人交易产生讯号徽章。"""
    try:
        ins = fetch_insider(code, days=90)
    except Exception:
        return []
    s = ins.get("summary", {})
    net = s.get("net_value", 0)
    sigs = []
    if net >= 500_000:
        sigs.append({"key": "insider_buy", "label": "🐳 内部人大买", "color": "red"})
    elif net <= -5_000_000:
        sigs.append({"key": "insider_sell_heavy", "label": "📉 内部人大卖", "color": "green"})
    elif net <= -1_000_000:
        sigs.append({"key": "insider_sell", "label": "⚠️ 内部人卖超", "color": "orange"})
    return sigs


# ----------------------------------------------------------------------------
# 探测股票代号 (.TW 或 .TWO)
# ----------------------------------------------------------------------------
def probe_yfinance(code: str) -> dict | None:
    """美股代号直接用 ticker 本身，不必加 .TW / .TWO 后缀。"""
    code = code.strip().upper()
    try:
        t = yf.Ticker(code)
        h = t.history(period="5d", interval="1d", auto_adjust=False)
        if h.empty:
            return None
        try:
            inf = t.info or {}
        except Exception:
            inf = {}
        name = inf.get("longName") or inf.get("shortName") or code
        sector = inf.get("sector") or inf.get("industry") or "—"
        return {
            "code":   code,
            "yf":     code,
            "name":   name,
            "sector": sector,
            "price":  float(h.iloc[-1]["Close"]),
        }
    except Exception as e:
        print(f"[probe] {code}: {e}")
        return None


# ----------------------------------------------------------------------------
# Telegram 警示
# ----------------------------------------------------------------------------
import hashlib
import hmac
import base64

def _feishu_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名校验 (启用签名时必填)。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")

def send_feishu(text: str) -> bool:
    """通过飞书自定义机器人 Webhook 发送纯文本消息。"""
    cfg = load_feishu()
    webhook = cfg.get("webhook", "")
    secret = cfg.get("secret", "")
    if not webhook:
        return False
    # 飞书纯文本不支持 Telegram 的 *粗体*/_斜体_ 标记, 去掉避免显示星号
    plain = text.replace("*", "").replace("_", "")
    payload = {"msg_type": "text", "content": {"text": plain}}
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _feishu_sign(secret, ts)
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # 飞书成功返回 code=0; 非 0 表示失败 (如签名错误/频控)
            if data.get("code", 0) == 0 or data.get("StatusCode", 0) == 0:
                return True
            print(f"[feishu] 返回错误: {data}")
            return False
        print(f"[feishu] HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[feishu] {e}")
        return False

# 兼容旧调用名: check_alert / _scan_theme_group_alerts 等里的 send_telegram(...) 无需逐处改
send_telegram = send_feishu

def check_alert(code: str, price: float, prev: float, name: str = "",
                rsi: float = None, signals: list = None) -> None:
    """检查警示规则：
    - above / below: 价格突破/跌破
    - rsi_above / rsi_below: RSI 越过阈值
    - on_golden_cross / on_death_cross: 黄金/死亡交叉
    - on_signal_burst: 讯号总数 >= N
    - drawdown_pct: 从近 60d 高点回档 >= N%
    """
    alerts = load_alerts()
    rule = alerts.get(code)
    if not rule:
        return
    last_price   = rule.get("last_price", prev)
    last_rsi     = rule.get("last_rsi", rsi if rsi is not None else 50)
    last_sig_keys = set(rule.get("last_sig_keys", []))
    sig_keys = set(s.get("key", "") for s in (signals or []))
    triggered = []

    # 1. 价格突破/跌破
    above = rule.get("above")
    below = rule.get("below")
    if above is not None and float(last_price) < float(above) <= price:
        triggered.append(f"🚀 *{name} ({code})* 突破上方警示\n价位: *{above}* → 现价 *{price:.2f}*")
    if below is not None and float(last_price) > float(below) >= price:
        triggered.append(f"⚠️ *{name} ({code})* 跌破下方警示\n价位: *{below}* → 现价 *{price:.2f}*")

    # 2. RSI 越过阈值
    if rsi is not None:
        r_above = rule.get("rsi_above")
        r_below = rule.get("rsi_below")
        if r_above is not None and float(last_rsi) < float(r_above) <= rsi:
            triggered.append(f"🔥 *{name} ({code})* RSI 突破 *{r_above}* → 目前 *{rsi:.1f}* (过热警示)")
        if r_below is not None and float(last_rsi) > float(r_below) >= rsi:
            triggered.append(f"❄️ *{name} ({code})* RSI 跌破 *{r_below}* → 目前 *{rsi:.1f}* (超卖反弹机会)")

    # 3. 讯号事件 (今天才出现、昨天没有)
    new_signals = sig_keys - last_sig_keys
    if rule.get("on_golden_cross") and "golden_cross" in new_signals:
        triggered.append(f"🌟 *{name} ({code})* 出现 *黄金交叉* (MA5 上穿 MA20)")
    if rule.get("on_death_cross") and "death_cross" in new_signals:
        triggered.append(f"💀 *{name} ({code})* 出现 *死亡交叉* (MA5 下穿 MA20)")
    if rule.get("on_kd_cross_up") and "kd_cross_up" in new_signals:
        triggered.append(f"🔼 *{name} ({code})* 出现 *KD 黄金交叉*")
    if rule.get("on_breakout") and "breakout_high" in new_signals:
        triggered.append(f"🚀 *{name} ({code})* 突破 *20 日新高*")
    if rule.get("on_breakdown") and "breakdown_low" in new_signals:
        triggered.append(f"📉 *{name} ({code})* 跌破 *20 日新低*")

    # 4. 讯号爆发 (讯号数量越过阈值)
    burst_n = rule.get("on_signal_burst")
    if burst_n is not None and len(sig_keys) >= int(burst_n) and len(last_sig_keys) < int(burst_n):
        labels = "、".join(s.get("label", "") for s in (signals or [])[:5])
        triggered.append(f"🎯 *{name} ({code})* 讯号爆发！{len(sig_keys)} 个讯号:\n{labels}")

    # 5. Drawdown 警示 (从 60d 高点回档 X%)
    dd_pct = rule.get("drawdown_pct")
    if dd_pct is not None:
        # 动态追踪 60d 高点
        peak = rule.get("peak_price")
        if peak is None or price > float(peak):
            peak = price
        rule["peak_price"] = peak
        last_dd = rule.get("last_drawdown", 0) or 0
        cur_dd  = (peak - price) / peak * 100 if peak else 0
        # 只在第一次跨越阈值时推
        if cur_dd >= float(dd_pct) and last_dd < float(dd_pct):
            triggered.append(
                f"🩸 *{name} ({code})* 从 60d 高点回档 *{cur_dd:.1f}%*\n"
                f"高点 *${peak:.2f}* → 现价 *${price:.2f}* (阈值 {dd_pct}%)"
            )
        rule["last_drawdown"] = cur_dd

    # 更新状态
    rule["last_price"]   = price
    rule["last_rsi"]     = rsi if rsi is not None else last_rsi
    rule["last_sig_keys"] = list(sig_keys)
    alerts[code] = rule
    save_json(ALERTS_FILE, alerts)
    for msg in triggered:
        send_telegram(msg)
        _append_alert_log(code, name, price, msg)


def _append_alert_log(code: str, name: str, price: float, msg: str) -> None:
    """把触发的警示写到 alerts_log.json,给「警示回顾」用。"""
    try:
        log = load_json(ALERTS_LOG_FILE, [])
        # 判断类型 (从讯息 emoji 开头)
        kind = "price"
        for prefix, k in [("🚀", "breakout_up"), ("⚠️ ", "breakdown_below"),
                          ("🔥", "rsi_overbought"), ("❄️", "rsi_oversold"),
                          ("🌟", "golden_cross"), ("💀", "death_cross"),
                          ("🔼", "kd_up"), ("📉", "breakdown_low"),
                          ("🎯", "signal_burst"), ("🩸", "drawdown")]:
            if msg.startswith(prefix):
                kind = k; break
        log.append({
            "ts":    time.time(),
            "code":  code,
            "name":  name,
            "price": float(price),
            "kind":  kind,
            "msg":   msg[:200],
        })
        # 只保留最近 500 笔
        if len(log) > 500:
            log = log[-500:]
        save_json(ALERTS_LOG_FILE, log)
    except Exception as e:
        print(f"[alert_log] {e}")


def alert_worker():
    """背景每 5 分钟扫描有设警示的股票 + 每天检查主题/族群轮动。"""
    print("[alert_worker] 启动，每 5 分钟检查一次")
    last_theme_scan = 0
    while True:
        time.sleep(300)
        try:
            wl = load_watchlist()
            alerts = load_alerts()
            for code in alerts:
                if code not in wl:
                    continue
                try:
                    fetch_stock(code, force=True)  # 内部会 check_alert
                except Exception as e:
                    print(f"[alert_worker] {code}: {e}")
            # 每 24h 扫一次主题/族群 alert
            if time.time() - last_theme_scan > 86400:
                try:
                    _scan_theme_group_alerts()
                except Exception as e:
                    print(f"[alert_worker theme] {e}")
                last_theme_scan = time.time()
        except Exception as e:
            print(f"[alert_worker] {e}")


# 主题/族群 alert 设定档
THEME_ALERTS_FILE = ROOT / "theme_alerts.json"


def _scan_theme_group_alerts():
    """每日扫描主题轮动、族群轮动,若达阈值就推 Telegram。
    theme_alerts.json 结构:
    {
      "themes": {"资讯安全": {"ret_1w_above": 5, "ret_1m_above": 15}, ...},
      "groups": {"GPU / 加速器": {"ret_1w_above": 8}, ...},
      "_last_pushed": {"资讯安全:ret_1w_above:5": ts, ...}  // 去重 24h
    }
    """
    cfg = load_json(THEME_ALERTS_FILE, {})
    if not cfg.get("themes") and not cfg.get("groups"):
        return
    last_pushed = cfg.get("_last_pushed", {})
    now_ts = time.time()
    msgs = []

    def _check(domain, rules, current_data):
        nonlocal msgs
        for key, thresholds in rules.items():
            cur = next((x for x in current_data if x.get(domain) == key), None)
            if not cur: continue
            for rule, threshold in thresholds.items():
                # rule 格式: ret_1w_above / ret_1m_above / momentum_above / ret_1w_below
                parts = rule.rsplit("_", 1)
                if len(parts) != 2: continue
                metric, direction = parts
                metric_val = cur.get(metric)
                if metric_val is None: continue
                trig = (direction == "above" and metric_val >= threshold) or \
                       (direction == "below" and metric_val <= threshold)
                if not trig: continue
                dedup_key = f"{key}:{rule}:{threshold}"
                if now_ts - last_pushed.get(dedup_key, 0) < 86400:
                    continue  # 同一条 24h 不重复
                arrow = "🚀" if direction == "above" else "⚠️"
                msgs.append(f"{arrow} *{domain.upper()} {key}* — {metric} = *{metric_val:+.2f}%* "
                           f"(触发: {'≥' if direction == 'above' else '≤'} {threshold}%)")
                last_pushed[dedup_key] = now_ts

    try:
        themes_data = api_theme_rotation()
        _check("theme", cfg.get("themes", {}), themes_data)
    except Exception as e:
        print(f"[theme_alert] {e}")

    try:
        groups_data = api_group_rotation()
        _check("group", cfg.get("groups", {}), groups_data)
    except Exception as e:
        print(f"[group_alert] {e}")

    if msgs:
        for m in msgs:
            send_telegram(m)
        cfg["_last_pushed"] = last_pushed
        save_json(THEME_ALERTS_FILE, cfg)
        print(f"[theme/group alert] 推送 {len(msgs)} 则")


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="美股情报站 API", version="1.0")


@app.get("/api/theme-alerts")
def api_get_theme_alerts():
    return load_json(THEME_ALERTS_FILE, {"themes": {}, "groups": {}})


@app.post("/api/theme-alerts")
def api_set_theme_alerts(cfg: dict):
    """payload: {"themes": {"资讯安全": {"ret_1w_above": 5}}, "groups": {...}}"""
    existing = load_json(THEME_ALERTS_FILE, {})
    existing["themes"] = cfg.get("themes", existing.get("themes", {}))
    existing["groups"] = cfg.get("groups", existing.get("groups", {}))
    save_json(THEME_ALERTS_FILE, existing)
    return {"ok": True}

# CORS：允许从 GitHub Pages、file://、其他主机载入的前端访问本机 server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def fetch_summary(code: str) -> dict:
    """轻量版：只抓最近 30 个交易日，不算复杂指标、不打 FinMind。

    主要给清单页用，冷快取下也能在 < 5 秒内回应 16 档。
    若 fetch_stock 已有完整快取则直接重用。
    """
    full = cache_get(f"stock:{code}:D")
    if full:
        return {
            "code":   code,
            "name":   full["name"],
            "tag":    full["tag"],
            "group":  full.get("group", "自选"),
            "price":  full["price"],
            "prev":   full["prev"],
            "asOf":   full["asOf"],
            "signals": full.get("signals", []),
        }

    cached = cache_get(f"summary:{code}")
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, f"未追踪股票 {code}")

    hist = get_history(info["yf"], limit=45)   # 近 ~2 个月日线 (本地)
    if hist.empty:
        raise HTTPException(503, f"MySQL 取不到 {code}")



    closes = hist["Close"].dropna()
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else last

    # 讯号：用本地的 30 根日 K 简算
    ma5_s  = sma(closes, 5)
    ma20_s = sma(closes, 20)
    rsi_s  = rsi_indicator(closes, 14)
    k_s, d_s = kd_indicator(hist, 9)
    sigs = detect_signals(closes, ma5_s, ma20_s, k_s, d_s, rsi_s, hist["High"], hist["Low"], period="D")
    # 加上内部人讯号（cache 1 hr，不会拖慢 summary 列表）
    sigs.extend(insider_signals(code))

    # 短线胜率启发式 (与前端 winRate 计算一致)
    rsi_v = float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else 50.0
    ma5v  = float(ma5_s.iloc[-1])  if not pd.isna(ma5_s.iloc[-1])  else last
    ma20v = float(ma20_s.iloc[-1]) if not pd.isna(ma20_s.iloc[-1]) else last
    base = 50
    if ma5v > ma20v: base += 15
    elif ma5v < ma20v: base -= 15
    if rsi_v > 70: base -= 10
    elif rsi_v < 30: base += 10
    win_rate = max(20, min(85, base))

    out = {
        "code":   code,
        "name":   info["name"],
        "tag":    info["tag"],
        "group":  info.get("group", "自选"),
        "themes": info.get("themes", []) or [],
        "price":  round(last, 2),
        "prev":   round(prev, 2),
        "asOf":   str(hist.index[-1].date()),
        "signals": sigs,
        "rsi":          round(rsi_v, 2),
        "win_rate":     win_rate,
        "signal_count": len(sigs),
    }
    cache_set(f"summary:{code}", out)
    return out


@app.get("/api/stocks")
def api_list():
    """观察清单摘要 (轻量版，并行抓取)。"""
    from concurrent.futures import ThreadPoolExecutor

    wl = load_watchlist()
    codes = list(wl.keys())

    def safe_summary(code):
        try:
            return fetch_summary(code)
        except Exception as e:
            meta = wl.get(code, {})
            return {
                "code": code, "name": meta.get("name", code), "tag": meta.get("tag", ""),
                "group": meta.get("group", "自选"), "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(safe_summary, codes))
    return results


@app.get("/api/stock/{code}")
def api_stock(code: str, period: str = "D"):
    try:
        return fetch_stock(code, period=period)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/news/{code}")
def api_news(code: str):
    return fetch_news(code)


@app.get("/api/insider/{code}")
def api_insider(code: str, days: int = 180):
    return fetch_insider(code, days=days)


@app.get("/api/institutional/{code}")
def api_institutional(code: str):
    """Top 10 机构持股 + Top 5 mutual fund + 整体 % (13F 来自 yfinance)。"""
    return fetch_institutional_holders(code)


@app.get("/api/valuation/{code}")
def api_valuation(code: str):
    """单股估值指标 (P/E, PEG, P/B 等) 含同族群相对位置。"""
    v = fetch_valuation(code)
    # 补上族群相对位置 (从整个 ranking 拿)
    try:
        all_items = fetch_valuation_ranking()
        match = next((x for x in all_items if x["code"] == code), None)
        if match:
            v["pe_percentile"]  = match.get("pe_percentile")
            v["peg_percentile"] = match.get("peg_percentile")
            v["pe_vs_group"]    = match.get("pe_vs_group")
            v["peg_vs_group"]   = match.get("peg_vs_group")
            v["group_stats"]    = match.get("group_stats", {})
    except Exception:
        pass
    return v


@app.get("/api/valuation")
def api_valuation_all():
    """全 watchlist 估值排行 + 各族群中位数。"""
    return fetch_valuation_ranking()


@app.get("/api/groups")
def api_groups():
    """族群清单 + 每族群成员代号。"""
    wl = load_watchlist()
    groups: dict[str, list[str]] = {}
    for code, s in wl.items():
        g = s.get("group", "自选")
        groups.setdefault(g, []).append(code)
    return groups


# 4 套权重 prefab — 由 ?weights= 切换
SCORE_WEIGHT_PRESETS = {
    "balanced": {
        # 估值
        "peg_strong": 25, "peg_weak": 12, "relpe_strong": 20, "relpe_weak": 10,
        # 筹码
        "inst_high": 15, "inst_mid": 8,
        # 动能
        "win_high": 15, "win_mid": 8, "bull_multi": 10, "bull_one": 5,
        "rs5_strong": 8, "rs5_weak": -8,
        # 风险
        "bear_each": -8, "overheat_each": -5,
        "rsi_overbought": -12, "rsi_oversold": 8,
        "trend_bull": 5, "trend_bear": -5,
    },
    "value": {  # 价值派 — 加重估值、轻看动能
        "peg_strong": 40, "peg_weak": 22, "relpe_strong": 35, "relpe_weak": 18,
        "inst_high": 18, "inst_mid": 10,
        "win_high": 8,  "win_mid": 4,  "bull_multi": 5, "bull_one": 2,
        "rs5_strong": 4, "rs5_weak": -4,
        "bear_each": -5, "overheat_each": -3,
        "rsi_overbought": -8, "rsi_oversold": 15,    # 反转机会加重
        "trend_bull": 3, "trend_bear": -3,
    },
    "momentum": {  # 动能派 — 加重讯号/RS/胜率
        "peg_strong": 10, "peg_weak": 5, "relpe_strong": 8, "relpe_weak": 4,
        "inst_high": 8,  "inst_mid": 4,
        "win_high": 30, "win_mid": 15, "bull_multi": 25, "bull_one": 12,
        "rs5_strong": 20, "rs5_weak": -15,
        "bear_each": -12, "overheat_each": -3,        # 动能派可忍过热
        "rsi_overbought": -5, "rsi_oversold": 5,
        "trend_bull": 12, "trend_bear": -12,
    },
    "chip": {  # 筹码派 — 加重机构/内部人
        "peg_strong": 15, "peg_weak": 8, "relpe_strong": 12, "relpe_weak": 6,
        "inst_high": 35, "inst_mid": 18,
        "win_high": 12, "win_mid": 6, "bull_multi": 15, "bull_one": 7,
        "rs5_strong": 10, "rs5_weak": -8,
        "bear_each": -15, "overheat_each": -5,        # 机构不喜欢风险
        "rsi_overbought": -10, "rsi_oversold": 8,
        "trend_bull": 5, "trend_bear": -8,
    },
}
_SCORE_WEIGHTS = SCORE_WEIGHT_PRESETS["balanced"]  # 全域，由 api_ranking 设定


def _detect_market_regime() -> tuple[str, str]:
    """侦测市场 regime,回 (prefab 名称, 说明)。
    动能盘 → momentum 权重;防御盘 → value;其余 → balanced。
    用 /api/breadth 的 watchlist 宽度 + 大盘动量判断。"""
    try:
        b = api_breadth()
    except Exception:
        return "balanced", "无法判断,用均衡"
    pct20 = b.get("pct_above_50", 50)   # > MA20 比例
    pct60 = b.get("pct_above_200", 50)  # > MA60 比例
    ad    = b.get("ad_ratio", 1)
    spy_chg = b.get("spy_change") or 0

    # 动能盘:广度健康 + 多数股在均线上
    if pct20 >= 60 and pct60 >= 55:
        return "momentum", f"动能盘 (>{pct20:.0f}% 在 MA20 上, A/D {ad})"
    # 防御盘:广度差 / 普遍弱势
    if pct20 < 40 or pct60 < 40:
        return "value", f"防御盘 (仅 {pct20:.0f}% 在 MA20 上)"
    return "balanced", f"均衡盘 ({pct20:.0f}% 在 MA20 上)"


@app.get("/api/market-regime")
def api_market_regime():
    """回传目前市场 regime 与对应建议权重。"""
    prefab, note = _detect_market_regime()
    label = {"momentum": "🚀 动能盘", "value": "🛡️ 防御盘", "balanced": "⚖️ 均衡盘"}.get(prefab, prefab)
    return {"regime": prefab, "label": label, "note": note,
            "suggest_weights": prefab}


@app.get("/api/ranking")
def api_ranking(by: str = "change", weights: str = "balanced"):
    global _SCORE_WEIGHTS
    regime_note = ""
    if weights == "auto":
        prefab, regime_note = _detect_market_regime()
        _SCORE_WEIGHTS = SCORE_WEIGHT_PRESETS.get(prefab, SCORE_WEIGHT_PRESETS["balanced"])
    else:
        _SCORE_WEIGHTS = SCORE_WEIGHT_PRESETS.get(weights, SCORE_WEIGHT_PRESETS["balanced"])
    """热度榜排序：
    - change / down: 涨幅 / 跌幅
    - volume:        成交量
    - fi / fi_sell:  Strong Buy 多 / 少
    - rsi / rsi_low: RSI 高 / 低
    - win:           短线胜率
    - signals:       讯号数
    - bias:          乖离率
    """
    items = []
    for code in load_watchlist():
        try:
            d = fetch_stock(code)
            change_pct = (d["price"] - d["prev"]) / d["prev"] * 100 if d["prev"] else 0
            sigs = d.get("signals", [])
            ma_status = d.get("maStatus", "")
            rsi_v = d["rsi"]
            base = 50
            if "多头" in ma_status: base += 15
            elif "空头" in ma_status: base -= 15
            if rsi_v > 70: base -= 10
            elif rsi_v < 30: base += 10
            win_rate = max(20, min(85, base))
            ma20 = d.get("ma20", 0) or 1
            bias = (d["price"] - ma20) / ma20 * 100 if ma20 else 0
            # 13F 共识度（cache 6 hr）
            inst_pct = inst_top10 = 0
            try:
                ih = fetch_institutional_holders(code)
                inst_pct   = ih["summary"].get("pct_institutions", 0)
                inst_top10 = ih["summary"].get("top10_pct", 0)
            except Exception:
                pass
            # 估值指标 (cache 6 hr)
            peg = pe = pe_vs_group = None
            try:
                v = fetch_valuation(code)
                peg = v.get("peg")
                pe  = v.get("trailing_pe")
            except Exception:
                pass
            items.append({
                "code":       code,
                "name":       d["name"],
                "tag":        d["tag"],
                "group":      d.get("group", "自选"),
                "price":      d["price"],
                "prev":       d["prev"],
                "change_pct": round(change_pct, 2),
                "volume":     d["volume"],
                "avgVol":     d["avgVol"],
                "vol_change": d["volChange"],
                "fi_today":   d["chip"]["fi_today"],
                "it_today":   d["chip"]["it_today"],
                "rsi":        rsi_v,
                "trend":      d["trend"],
                "signals":    sigs,
                "signal_count": len(sigs),
                "win_rate":   win_rate,
                "bias":       round(bias, 2),
                "risk":       d["risk"],
                "inst_pct":    inst_pct,
                "inst_top10":  inst_top10,
                "pe":          pe,
                "peg":         peg,
            })
        except Exception:
            pass
    # 计算族群相对 PE (在 ranking 端点即时算，因 valuation_ranking 也有 cache)
    try:
        valn_items = fetch_valuation_ranking()
        rel_map = {v["code"]: v.get("pe_vs_group") for v in valn_items}
        for it in items:
            it["pe_vs_group"] = rel_map.get(it["code"])
    except Exception:
        for it in items:
            it.setdefault("pe_vs_group", None)

    # 相对族群强度 (RS) — 1d 用今日涨跌、5d/20d 用 rotation 的多日报酬
    from statistics import median as _median
    group_1d: dict[str, list[float]] = {}
    for it in items:
        group_1d.setdefault(it["group"], []).append(it["change_pct"])
    g_med_1d = {g: _median(vs) for g, vs in group_1d.items() if vs}
    for it in items:
        med = g_med_1d.get(it["group"], 0)
        it["rs_1d"] = round(it["change_pct"] - med, 2)

    # 5d / 20d RS — 用 rotation_5d_returns cache
    try:
        rs_cache = cache_get("rs:5_20")
        if rs_cache is None:
            yf_codes_all = [load_watchlist()[c]["yf"] for c in load_watchlist()]
            code_list = list(load_watchlist().keys())
            end_d = pd.Timestamp.today()
            start_d = end_d - pd.Timedelta(days=60)
            data = download_closes(code_list, start=start_d, end=end_d)
            cc = pd.DataFrame()
            for c, yfc in zip(code_list, yf_codes_all):
                try:
                    col = data[yfc]["Close"] if len(yf_codes_all) > 1 else data["Close"]
                    cc[c] = col
                except Exception:
                    continue
            cc = cc.dropna(how="all")
            rs_cache = {}
            if len(cc) >= 21:
                ret_5d  = ((cc.iloc[-1] - cc.iloc[-6])  / cc.iloc[-6]  * 100).to_dict()
                ret_20d = ((cc.iloc[-1] - cc.iloc[-21]) / cc.iloc[-21] * 100).to_dict()
                rs_cache = {"r5": ret_5d, "r20": ret_20d}
            _cache["rs:5_20"] = (time.time() + 1800 - CACHE_TTL, rs_cache)

        r5 = rs_cache.get("r5", {})
        r20 = rs_cache.get("r20", {})

        g5 = {}
        g20 = {}
        for it in items:
            v5  = r5.get(it["code"])
            v20 = r20.get(it["code"])
            if v5 is not None and not pd.isna(v5):
                g5.setdefault(it["group"], []).append(v5)
            if v20 is not None and not pd.isna(v20):
                g20.setdefault(it["group"], []).append(v20)
        med5  = {g: _median(vs) for g, vs in g5.items()  if vs}
        med20 = {g: _median(vs) for g, vs in g20.items() if vs}

        for it in items:
            v5  = r5.get(it["code"])
            v20 = r20.get(it["code"])
            it["ret_5d"]  = round(float(v5), 2)  if v5 is not None and not pd.isna(v5) else None
            it["ret_20d"] = round(float(v20), 2) if v20 is not None and not pd.isna(v20) else None
            it["rs_5d"]   = round(it["ret_5d"]  - med5.get(it["group"], 0), 2)  if it["ret_5d"]  is not None else None
            it["rs_20d"]  = round(it["ret_20d"] - med20.get(it["group"], 0), 2) if it["ret_20d"] is not None else None
    except Exception as e:
        print(f"[rs] {e}")
        for it in items:
            it.setdefault("rs_5d", None)
            it.setdefault("rs_20d", None)

    # 多因子综合评分 — 4 种权重 prefab
    # 透过 api_ranking(?weights=balanced|value|momentum|chip) 切换
    for it in items:
        score = 0
        reasons_plus = []
        reasons_minus = []
        peg = it.get("peg")
        rel = it.get("pe_vs_group")
        inst = it.get("inst_pct", 0) or 0
        wr = it.get("win_rate", 0) or 0
        sigs = it.get("signals", []) or []
        bull = sum(1 for s in sigs if s.get("color") == "red")
        bear = sum(1 for s in sigs if s.get("color") == "green")
        overheat = sum(1 for s in sigs if s.get("color") == "orange")
        rsi_v = it.get("rsi", 50) or 50
        trend = it.get("trend", "")
        rs5 = it.get("rs_5d") or 0

        W = _SCORE_WEIGHTS  # 引用全域权重表 (依 weights 参数选定)

        # 1. PEG (估值)
        if peg is not None and 0 < peg < 1:
            score += W["peg_strong"]; reasons_plus.append(f"PEG {peg:.2f} <1")
        elif peg is not None and 0 < peg < 1.5:
            score += W["peg_weak"]; reasons_plus.append(f"PEG {peg:.2f}")
        # 2. 同族群相对 PE (估值)
        if rel is not None and rel < -20:
            score += W["relpe_strong"]; reasons_plus.append(f"族群 PE -{abs(rel):.0f}%")
        elif rel is not None and rel < -10:
            score += W["relpe_weak"]; reasons_plus.append(f"族群 PE -{abs(rel):.0f}%")
        # 3. 机构共识 (筹码)
        if inst > 80:
            score += W["inst_high"]; reasons_plus.append(f"机构 {inst:.0f}%")
        elif inst > 60:
            score += W["inst_mid"]; reasons_plus.append(f"机构 {inst:.0f}%")
        # 4. 短线胜率 (动能)
        if wr >= 65:
            score += W["win_high"]; reasons_plus.append(f"胜率 {wr}%")
        elif wr >= 55:
            score += W["win_mid"]; reasons_plus.append(f"胜率 {wr}%")
        # 5. 讯号 (动能)
        if bull >= 2:
            score += W["bull_multi"]; reasons_plus.append(f"{bull} 多头讯号")
        elif bull == 1:
            score += W["bull_one"]
        score += W["bear_each"] * bear
        score += W["overheat_each"] * overheat
        if bear: reasons_minus.append(f"{bear} 空头讯号")
        if overheat: reasons_minus.append(f"{overheat} 过热警示")
        # 6. RSI 极端
        if rsi_v > 75:
            score += W["rsi_overbought"]; reasons_minus.append(f"RSI {rsi_v:.0f} 超买")
        elif rsi_v < 30:
            score += W["rsi_oversold"]; reasons_plus.append(f"RSI {rsi_v:.0f} 超卖")
        # 7. 趋势
        if "多头" in trend:
            score += W["trend_bull"]
        elif "空头" in trend:
            score += W["trend_bear"]; reasons_minus.append("空头趋势")
        # 8. RS 5d (动能 — 比族群跑得快)
        if rs5 > 5:
            score += W["rs5_strong"]; reasons_plus.append(f"5日比族群 +{rs5:.1f}%")
        elif rs5 < -5:
            score += W["rs5_weak"]; reasons_minus.append(f"5日比族群 {rs5:.1f}%")

        it["score"] = score
        it["score_plus"]  = reasons_plus
        it["score_minus"] = reasons_minus

    def _peg_key(x):
        v = x.get("peg")
        return v if (v is not None and v > 0) else 9999
    def _relpe_key(x):
        v = x.get("pe_vs_group")
        return v if v is not None else 9999

    def _rs1d(x): return -(x.get("rs_1d")  or -999)
    def _rs5d(x): return -(x.get("rs_5d")  or -999)
    def _rs20(x): return -(x.get("rs_20d") or -999)

    keymap = {
        "change":   lambda x: -x["change_pct"],
        "down":     lambda x:  x["change_pct"],
        "volume":   lambda x: -x["volume"],
        "fi":       lambda x: -x["fi_today"],
        "fi_sell":  lambda x:  x["fi_today"],
        "rsi":      lambda x: -x["rsi"],
        "rsi_low":  lambda x:  x["rsi"],
        "win":      lambda x: -x["win_rate"],
        "signals":  lambda x: -x["signal_count"],
        "bias":     lambda x: -abs(x["bias"]),
        "inst":     lambda x: -x["inst_pct"],
        "inst10":   lambda x: -x["inst_top10"],
        "rs1d":     _rs1d,                             # 📊 族群相对强度 1d
        "rs5d":     _rs5d,                             # 📊 族群相对强度 5d
        "rs20d":    _rs20,                             # 📊 族群相对强度 20d
        "score":    lambda x: -x["score"],          # 🎯 综合评分
        "peg":      _peg_key,            # PEG 最低（< 1 = 相对便宜）
        "relpe":    _relpe_key,          # 相对族群 P/E 最低（同族群最便宜）
    }
    items.sort(key=keymap.get(by, keymap["change"]))
    return items


# ----- 观察清单管理 -----
class AddStockReq(BaseModel):
    code:  str
    name:  Optional[str] = None
    tag:   Optional[str] = None
    group: Optional[str] = "自选"


@app.get("/api/watchlist")
def api_get_watchlist():
    return load_watchlist()


@app.post("/api/watchlist")
def api_add_watchlist(req: AddStockReq):
    # 美股代号为字母 (AAPL / NVDA)、ETF 含数字或加 . (BRK.B)、指数含 ^ (^GSPC)
    code = req.code.strip().upper()
    if not code or len(code) > 10 or not all(c.isalnum() or c in ".-" for c in code):
        raise HTTPException(400, "代号格式不正确（仅允许英数字、. 或 -，最多 10 字元）")
    wl = load_watchlist()
    if code in wl:
        return {"ok": True, "msg": "已在观察清单", "data": wl[code]}
    probe = probe_yfinance(code)
    if not probe:
        raise HTTPException(404, f"yfinance 找不到 {code}（试过 .TW / .TWO）")
    name = req.name or probe["name"]
    if len(name) > 12:
        name = name[:12]
    wl[code] = {
        "name":  name,
        "tag":   req.tag or probe.get("sector", "—"),
        "yf":    probe["yf"],
        "group": req.group or "自选",
    }
    save_json(WATCHLIST_FILE, wl)
    # 清掉清单快取，下次列表会重捞
    _cache.pop(f"stock:{code}:D", None)
    return {"ok": True, "data": wl[code], "probe": probe}


@app.delete("/api/watchlist/{code}")
def api_del_watchlist(code: str):
    wl = load_watchlist()
    if code not in wl:
        raise HTTPException(404)
    del wl[code]
    save_json(WATCHLIST_FILE, wl)
    return {"ok": True}


@app.get("/api/probe/{code}")
def api_probe(code: str):
    p = probe_yfinance(code)
    if not p:
        raise HTTPException(404, "yfinance 找不到")
    return p


# ----- feishu -----
class FeishuReq(BaseModel):
    webhook: str
    secret: Optional[str] = ""

@app.post("/api/telegram")
def api_telegram_set(req: FeishuReq):
    save_json(FEISHU_FILE, {
        "webhook": req.webhook.strip(),
        "secret": (req.secret or "").strip(),
    })
    ok = send_feishu(f"✅ 美股情报站 飞书连线测试成功\n时间: {pd.Timestamp.now():%Y-%m-%d %H:%M:%S}")
    return {"ok": True, "test_sent": ok}

@app.get("/api/telegram")
def api_telegram_get():
    cfg = load_feishu()
    return {
        "configured": bool(cfg.get("webhook")),
        "webhook": cfg.get("webhook", ""),
    }


# ----- Alerts -----
class AlertReq(BaseModel):
    above: Optional[float] = None
    below: Optional[float] = None
    rsi_above: Optional[float] = None
    rsi_below: Optional[float] = None
    on_golden_cross: Optional[bool] = None
    on_death_cross:  Optional[bool] = None
    on_kd_cross_up:  Optional[bool] = None
    on_breakout:     Optional[bool] = None
    on_breakdown:    Optional[bool] = None
    on_signal_burst: Optional[int]  = None


@app.get("/api/alerts")
def api_get_alerts():
    return load_alerts()


@app.post("/api/alerts/{code}")
def api_set_alert(code: str, req: AlertReq):
    alerts = load_alerts()
    rule = alerts.get(code, {})
    # 将提供的栏位写入 rule (None 视为清除)
    for field in ["above", "below", "rsi_above", "rsi_below",
                  "on_golden_cross", "on_death_cross", "on_kd_cross_up",
                  "on_breakout", "on_breakdown", "on_signal_burst"]:
        val = getattr(req, field, None)
        if val is not None:
            rule[field] = val
        else:
            rule.pop(field, None)
    # 初始化 last_price / last_rsi (用于后续变化侦测)
    if "last_price" not in rule:
        try:
            d = fetch_stock(code)
            rule["last_price"] = d["price"]
            rule["last_rsi"]   = d.get("rsi", 50)
            rule["last_sig_keys"] = [s["key"] for s in d.get("signals", [])]
        except Exception:
            rule["last_price"] = 0
    alerts[code] = rule
    save_json(ALERTS_FILE, alerts)
    return {"ok": True, "data": rule}


@app.delete("/api/alerts/{code}")
def api_del_alert(code: str):
    alerts = load_alerts()
    alerts.pop(code, None)
    save_json(ALERTS_FILE, alerts)
    return {"ok": True}


@app.get("/api/alerts-log")
def api_alerts_log(days: int = 30):
    """过去 N 天触发的警示 + 每笔 1d/5d/20d 后续走势回顾。"""
    cache_key = f"alerts_log:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    log = load_json(ALERTS_LOG_FILE, [])
    if not log:
        return {"entries": [], "stats": {}, "asOf": str(pd.Timestamp.today().date())}
    cutoff_ts = time.time() - days * 86400
    recent = [e for e in log if e.get("ts", 0) >= cutoff_ts]
    if not recent:
        cache_set(cache_key, {"entries": [], "stats": {}, "asOf": str(pd.Timestamp.today().date())})
        return _cache[cache_key][1]

    # 批次抓所有相关代号的历史价,算每笔触发后 1d/5d/20d 报酬
    wl = load_watchlist()
    codes = sorted({e["code"] for e in recent if e["code"] in wl})
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=days + 60)
    closes = download_closes(codes, start=start, end=end)
    closes = closes.dropna(how="all")

    # yf_codes = [wl[c]["yf"] for c in codes]
    # end = pd.Timestamp.today()
    # start = end - pd.Timedelta(days=days + 60)
    # closes = {}
    # if yf_codes:
    #     try:
    #         data = yf.download(yf_codes, start=start, end=end, auto_adjust=False,
    #                            progress=False, group_by="ticker", threads=True)
    #         for c, yfc in zip(codes, yf_codes):
    #             try:
    #                 closes[c] = data[yfc]["Close"].dropna() if len(yf_codes) > 1 else data["Close"].dropna()
    #             except Exception:
    #                 continue
    #     except Exception as e:
    #         print(f"[alerts_log yf] {e}")

    out_entries = []
    for e in reversed(recent):  # 新到旧
        c = e["code"]
        trigger_price = float(e.get("price", 0))
        ts = e.get("ts", 0)
        dt = pd.Timestamp(ts, unit="s")
        ret_1d = ret_5d = ret_20d = None
        c_series = closes.get(c)
        if c_series is not None and len(c_series) > 0:
            # 找触发后的第一个收盘日
            try:
                after = c_series[c_series.index >= dt.tz_localize(None) if c_series.index.tz else c_series.index >= dt]
                if len(after) >= 2:
                    ret_1d = round((float(after.iloc[1]) - trigger_price) / trigger_price * 100, 2) if trigger_price else None
                if len(after) >= 6:
                    ret_5d = round((float(after.iloc[5]) - trigger_price) / trigger_price * 100, 2) if trigger_price else None
                if len(after) >= 21:
                    ret_20d = round((float(after.iloc[20]) - trigger_price) / trigger_price * 100, 2) if trigger_price else None
            except Exception:
                pass
        out_entries.append({
            "ts":       int(ts),
            "date":     dt.strftime("%Y-%m-%d %H:%M"),
            "code":     c,
            "name":     e.get("name", c),
            "price":    trigger_price,
            "kind":     e.get("kind", "—"),
            "msg":      e.get("msg", ""),
            "ret_1d":   ret_1d,
            "ret_5d":   ret_5d,
            "ret_20d":  ret_20d,
        })

    # 统计各 kind 的胜率
    stats = {}
    for k in {x["kind"] for x in out_entries}:
        same = [x for x in out_entries if x["kind"] == k]
        wins_5d = sum(1 for x in same if x["ret_5d"] is not None and x["ret_5d"] > 0)
        with_5d = sum(1 for x in same if x["ret_5d"] is not None)
        avg_5d = round(sum(x["ret_5d"] for x in same if x["ret_5d"] is not None) / with_5d, 2) if with_5d else None
        stats[k] = {
            "n": len(same),
            "win_rate_5d": round(wins_5d / with_5d * 100, 1) if with_5d else None,
            "avg_5d":      avg_5d,
        }

    result = {"entries": out_entries, "stats": stats, "asOf": str(end.date())}
    cache_set(cache_key, result)
    return result


# ----- Misc -----
@app.get("/api/refresh")
def api_refresh():
    _cache.clear()
    return {"ok": True, "msg": "快取已清除"}


@app.post("/api/seed-defaults")
def api_seed_defaults():
    """把 DEFAULT_WATCHLIST 中还没在 watchlist.json 的股票补进去。
    用于升级族群分类后一键加入新建议标的（不会碰使用者既有清单）。
    """
    wl = load_watchlist()
    added = []
    for code, meta in DEFAULT_WATCHLIST.items():
        if code not in wl:
            wl[code] = dict(meta)
            added.append(code)
    if added:
        save_json(WATCHLIST_FILE, wl)
        # 清快取让清单与排行重新计算
        for k in list(_cache.keys()):
            if k.startswith("summary:") or k.startswith("stock:") or k == "valn-ranking":
                _cache.pop(k, None)
    return {"ok": True, "added": added, "n_added": len(added),
            "msg": f"已新增 {len(added)} 档到观察清单"}


# ============================================================================
# 1) Portfolio (个人持股；同代号可多笔，各自有 id)
# ============================================================================
import uuid as _uuid


def load_portfolio() -> list:
    """永远回 list。旧版 dict 格式 (code → holding) 会自动迁移成新版 list。"""
    raw = load_json(PORTFOLIO_FILE, [])
    if isinstance(raw, dict):
        migrated = []
        for code, h in raw.items():
            migrated.append({
                "id":         _uuid.uuid4().hex[:12],
                "code":       code,
                "shares":     float(h.get("shares", 0) or 0),
                "cost_price": float(h.get("cost_price", 0) or 0),
                "buy_date":   h.get("buy_date", "") or "",
                "note":       h.get("note", "") or "",
            })
        save_json(PORTFOLIO_FILE, migrated)
        print(f"[portfolio] 旧 dict 格式迁移成 list，{len(migrated)} 笔")
        return migrated
    return raw if isinstance(raw, list) else []


class HoldingReq(BaseModel):
    code:       str
    shares:     float       # 美股股数（1 股 = 1 股）
    cost_price: float
    buy_date:   Optional[str] = None
    note:       Optional[str] = ""


def _trailing_stop_advice(yf_code: str, buy_date: str, cost_price: float, current_price: float) -> dict:
    """计算移动停利建议。

    规则组合：
    - 从买入日后的最高点 -10% = 移动停利
    - 保本停利：若已 +5% 起，停损上移至成本价
    - 目标停利：+20%
    """
    try:
        kwargs = {"period": "1y", "interval": "1d", "auto_adjust": False}
        if buy_date:
            kwargs = {"start": buy_date, "interval": "1d", "auto_adjust": False}
        hist = yf.Ticker(yf_code).history(**kwargs)
        if hist.empty:
            return {"trail_stop": None, "post_high": None, "breakeven": None,
                    "target": round(cost_price * 1.20, 2), "rule": "资料不足"}
        post_high = float(hist["High"].max())
    except Exception as e:
        print(f"[trail] {yf_code}: {e}")
        return {"trail_stop": None, "post_high": None, "breakeven": None,
                "target": round(cost_price * 1.20, 2), "rule": "计算失败"}

    trail_pct  = 0.10
    trail_stop = round(post_high * (1 - trail_pct), 2)
    target     = round(cost_price * 1.20, 2)
    breakeven  = round(cost_price, 2) if current_price >= cost_price * 1.05 else None
    ret_pct    = (current_price - cost_price) / cost_price * 100 if cost_price else 0

    if ret_pct >= 20:
        rule = f"已 +{ret_pct:.0f}% 达目标，建议分批停利"
    elif current_price <= trail_stop:
        rule = f"已触发 -10% 移动停利 ({trail_stop})，建议出场"
    elif breakeven:
        rule = f"已保本；停利上移至高点 -10% = {trail_stop}"
    else:
        rule = f"持有观察，停损 {round(cost_price * 0.92, 2)} (-8%)"

    return {
        "trail_stop": trail_stop,
        "post_high":  round(post_high, 2),
        "breakeven":  breakeven,
        "target":     target,
        "rule":       rule,
    }


@app.get("/api/portfolio")
def api_get_portfolio():
    """回传所有持股 + 市值/损益/移动停利建议（用即时收盘价计算）。"""
    p = load_portfolio()
    if not p:
        return {"holdings": [], "summary": {"total_cost": 0, "total_value": 0,
                                              "total_pnl": 0, "total_pnl_pct": 0, "count": 0}}
    holdings = []
    total_cost = 0.0
    total_value = 0.0
    wl = load_watchlist()
    for h in p:
        code = h["code"]
        try:
            s = fetch_summary(code)
            price = float(s["price"])
            name  = s["name"]
            tag   = s.get("tag", "")
        except Exception:
            price, name, tag = float(h["cost_price"]), code, ""
        yf_code = wl.get(code, {}).get("yf", code)
        shares = float(h["shares"])
        cost   = shares * float(h["cost_price"])
        value  = shares * price
        pnl    = value - cost
        pnl_pct = (price - float(h["cost_price"])) / float(h["cost_price"]) * 100 if h["cost_price"] else 0
        total_cost += cost
        total_value += value

        trail = _trailing_stop_advice(yf_code, h.get("buy_date", ""), float(h["cost_price"]), price)

        holdings.append({
            "id":         h.get("id") or _uuid.uuid4().hex[:12],
            "code":       code,
            "name":       name,
            "tag":        tag,
            "shares":     shares,
            "cost_price": float(h["cost_price"]),
            "buy_date":   h.get("buy_date", ""),
            "note":       h.get("note", ""),
            "current":    round(price, 2),
            "cost":       round(cost, 0),
            "value":      round(value, 0),
            "pnl":        round(pnl, 0),
            "pnl_pct":    round(pnl_pct, 2),
            "trail_stop": trail.get("trail_stop"),
            "post_high":  trail.get("post_high"),
            "breakeven":  trail.get("breakeven"),
            "target":     trail.get("target"),
            "rule":       trail.get("rule"),
        })
    for h in holdings:
        h["weight"] = round(h["value"] / total_value * 100, 1) if total_value > 0 else 0
    # 预设按名称字母排（前端可再以栏位点击重新排序）
    holdings.sort(key=lambda x: (x.get("name") or x.get("code") or "").lower())

    summary = {
        "total_cost":    round(total_cost, 0),
        "total_value":   round(total_value, 0),
        "total_pnl":     round(total_value - total_cost, 0),
        "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        "count":         len(holdings),
    }
    return {"holdings": holdings, "summary": summary}


@app.post("/api/portfolio")
def api_add_portfolio(req: HoldingReq):
    code = req.code.strip().upper()
    if not code or not all(c.isalnum() or c in ".-" for c in code):
        raise HTTPException(400, "代号格式不正确")
    if req.shares <= 0 or req.cost_price <= 0:
        raise HTTPException(400, "股数与成本价需 > 0")

    p = load_portfolio()  # list

    # 若不在 watchlist，顺手加入（便于追踪），并清掉相关快取让 chip 即时更新
    wl = load_watchlist()
    added_to_watchlist = False
    if code not in wl:
        probe = probe_yfinance(code)
        if probe:
            wl[code] = {
                "name":  probe["name"][:12],
                "tag":   probe.get("sector", "—"),
                "yf":    probe["yf"],
                "group": "持股",
            }
            save_json(WATCHLIST_FILE, wl)
            added_to_watchlist = True
            # Clear caches so /api/stocks chip count refresh
            for k in list(_cache.keys()):
                if k.startswith("summary:") or k.startswith("stock:"):
                    _cache.pop(k, None)

    new_holding = {
        "id":         _uuid.uuid4().hex[:12],
        "code":       code,
        "shares":     float(req.shares),
        "cost_price": float(req.cost_price),
        "buy_date":   req.buy_date or "",
        "note":       req.note or "",
    }
    p.append(new_holding)
    save_json(PORTFOLIO_FILE, p)
    return {"ok": True, "data": new_holding, "added_to_watchlist": added_to_watchlist}


@app.delete("/api/portfolio/{holding_id}")
def api_del_portfolio(holding_id: str):
    """删除单笔持股 (用 id)。向下相容：若传入的是 code 且唯一，也接受。"""
    p = load_portfolio()
    new_p = [h for h in p if h.get("id") != holding_id]
    if len(new_p) == len(p):
        new_p = [h for h in p if h.get("code") != holding_id]
        if len(new_p) == len(p):
            raise HTTPException(404)
    save_json(PORTFOLIO_FILE, new_p)
    return {"ok": True, "removed": len(p) - len(new_p)}


# ============================================================================
# 2) Signal backtest (讯号绩效回测)
# ============================================================================
@app.get("/api/signal-stats/{code}/{signal_key}")
def api_signal_stats(code: str, signal_key: str, forward: int = 5):
    """扫过去 2 年该讯号出现的历史，计算后 N 天平均报酬与胜率。"""
    cache_key = f"sigstat:{code}:{signal_key}:{forward}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)

    try:
        hist = yf.Ticker(info["yf"]).history(period="2y", interval="1d", auto_adjust=False)
    except Exception as e:
        raise HTTPException(503, str(e))
    if len(hist) < 60:
        raise HTTPException(503, "历史资料不足")
    hist = hist.dropna(subset=["Close"])

    closes = hist["Close"]
    ma5_s, ma20_s = sma(closes, 5), sma(closes, 20)
    rsi_s = rsi_indicator(closes, 14)
    k_s, d_s = kd_indicator(hist, 9)

    occurrences = []
    for i in range(60, len(hist) - forward):
        snap_close = closes.iloc[: i + 1]
        snap_ma5   = ma5_s.iloc[: i + 1]
        snap_ma20  = ma20_s.iloc[: i + 1]
        snap_rsi   = rsi_s.iloc[: i + 1]
        snap_k     = k_s.iloc[: i + 1]
        snap_d     = d_s.iloc[: i + 1]
        snap_h     = hist["High"].iloc[: i + 1]
        snap_l     = hist["Low"].iloc[: i + 1]
        sigs = detect_signals(snap_close, snap_ma5, snap_ma20, snap_k, snap_d,
                              snap_rsi, snap_h, snap_l, period="D")
        if any(s["key"] == signal_key for s in sigs):
            entry = float(closes.iloc[i])
            future = float(closes.iloc[i + forward])
            ret = (future - entry) / entry * 100
            occurrences.append({
                "date":   hist.index[i].strftime("%Y-%m-%d"),
                "entry":  round(entry, 2),
                "future": round(future, 2),
                "return": round(ret, 2),
            })

    if not occurrences:
        result = {"signal": signal_key, "count": 0, "msg": "历史中无此讯号"}
    else:
        rets = [o["return"] for o in occurrences]
        win = sum(1 for r in rets if r > 0)
        result = {
            "signal":      signal_key,
            "code":        code,
            "name":        info["name"],
            "forward_days": forward,
            "count":       len(occurrences),
            "win_rate":    round(win / len(rets) * 100, 1),
            "avg_return":  round(sum(rets) / len(rets), 2),
            "best":        round(max(rets), 2),
            "worst":       round(min(rets), 2),
            "occurrences": occurrences[-15:],
        }
    cache_set(cache_key, result)
    return result


# ============================================================================
# 3) AI commentary (Gemini)
# ============================================================================
def _get_gemini_key() -> str:
    cfg = load_json(GEMINI_FILE, {})
    return (cfg.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")).strip()


class GeminiReq(BaseModel):
    api_key: str


@app.post("/api/gemini")
def api_gemini_set(req: GeminiReq):
    save_json(GEMINI_FILE, {"api_key": req.api_key.strip()})
    return {"ok": True}


@app.get("/api/gemini")
def api_gemini_get():
    return {"configured": bool(_get_gemini_key())}


@app.get("/api/ai-comment/{code}")
def api_ai_comment(code: str):
    """用 Gemini 对单档产生 3-5 句中文评论。"""
    key = _get_gemini_key()
    if not key:
        return {"ok": False, "msg": "尚未设定 Deepseek API key（从工具列「🤖 AI」按钮设定）"}

    cache_key = f"ai:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        d = fetch_stock(code)
    except Exception as e:
        raise HTTPException(503, str(e))

    portfolio = load_portfolio()  # list (multi-entry)
    holdings_of_code = [h for h in portfolio if h.get("code") == code]

    sigs = "、".join(s["label"] for s in d.get("signals", [])) or "无强烈讯号"

    # 内部人交易具体数字 (近 6 个月)
    insider_line = ""
    try:
        ins = fetch_insider(code, days=180)
        s = ins["summary"]
        if s.get("total_count", 0) > 0:
            net = s["net_value"]
            net_str = f"+${net/1e6:.1f}M" if net >= 0 else f"-${abs(net)/1e6:.1f}M"
            insider_line = (f"\n内部人 6 个月：净值 {net_str}"
                            f"（买 {s['buy_count']} 笔 ${s['buy_value']/1e6:.1f}M, "
                            f"卖 {s['sell_count']} 笔 ${s['sell_value']/1e6:.1f}M）")
    except Exception:
        pass

    # 13F 机构持股共识度
    inst_line = ""
    try:
        ih = fetch_institutional_holders(code)
        isum = ih["summary"]
        if isum.get("pct_institutions", 0) > 0:
            top_holder = ih["institutional"][0]["holder"] if ih.get("institutional") else "—"
            inst_line = (f"\n13F 机构共识：总机构 {isum['pct_institutions']}% / "
                         f"Top 10 集中 {isum['top10_pct']}% / 内部人 {isum['pct_insider']}% "
                         f"(最大持有: {top_holder[:30]})")
    except Exception:
        pass

    # 使用者持股 + 移动停利建议
    pos = ""
    if holdings_of_code:
        total_shares = sum(float(h.get("shares", 0)) for h in holdings_of_code)
        total_cost   = sum(float(h.get("shares", 0)) * float(h.get("cost_price", 0))
                           for h in holdings_of_code)
        avg_cost = total_cost / total_shares if total_shares > 0 else 0
        ret = (d["price"] - avg_cost) / avg_cost * 100 if avg_cost else 0
        n = len(holdings_of_code)
        trail = _trailing_stop_advice(d.get("yf", code), "", avg_cost, d["price"])
        pos = (f"\n使用者持股：{total_shares} 股 ({n} 笔)，平均成本 ${avg_cost:.2f}，"
               f"损益 {ret:+.2f}%，移动停利建议：{trail.get('rule','—')}")

    prompt = f"""你是美股技术分析助理。针对以下个股,用繁体中文写**结构化投资论点**,严格依照下列格式回复:

【论点】
2-3 句说明为什么这档值得买/持有,结合技术面 + 筹码面 (内部人 + 13F)。

【风险】
2-3 句具体写出什么状况下要警惕或重新评估 (例如: 跌破 $X、RSI 过热、机构出货、财报不如预期)。

【触发】
明确的可执行讯号,2-3 条,每条一行,格式如「📈 突破 $X → 加码 1/3」「⚠️ RSI > 80 → 减码 1/2」「🛑 跌破 $X 停利出场」。

不要免责声明,不要写「以上分析仅供参考」之类的话。三段都用上述【】标题开头。

【{d['name']} ({d['code']}) {d['tag']}】
收盘 ${d['price']}（前日 ${d['prev']}, {(d['price']-d['prev'])/d['prev']*100:+.2f}%）
趋势：{d['trend']}, 均线：{d['maStatus']}（5/20/60 = {d['ma5']}/{d['ma20']}/{d['ma60']}）
RSI(14) = {d['rsi']}, KD(9,3) K/D = {d['kd_k']}/{d['kd_d']}, MACD {d['macd']}
量能变化 {d['volChange']:+.1f}%（5 日均量 {d['avgVol']:,} 股）
分析师评等：Strong Buy 累计 {d['chip']['fi_10']} 家、Buy {d['chip']['it_10']} 家
近期讯号：{sigs}
压力 ${d['resist']} / 支撑 ${d['support']}{insider_line}{inst_line}{pos}
"""
    try:
        text = _gemini_call(key, prompt)   # 已别名到 DeepSeek
    except Exception as e:
        msg = str(e)
        if "402" in msg or "Insufficient Balance" in msg:
            return {"ok": False, "msg": "DeepSeek 余额不足，请到 platform.deepseek.com 充值后再试。"}
        if "401" in msg or "Authentication" in msg:
            return {"ok": False, "msg": "DeepSeek API key 无效，请在工具列「🤖 AI」重新设定。"}
        return {"ok": False, "msg": f"DeepSeek 调用失败: {e}"}


    # 解析三段结构
    import re
    def _extract_prose(full: str, label: str) -> str:
        """论点 / 风险 是连续散文,把多重空白合并成单一空格"""
        m = re.search(rf"【{label}】\s*([\s\S]*?)(?=【|$)", full)
        if not m: return ""
        return re.sub(r"\s+", " ", m.group(1)).strip()

    def _extract_lines(full: str, label: str) -> str:
        """触发是条列,保留换行"""
        m = re.search(rf"【{label}】\s*([\s\S]*?)(?=【|$)", full)
        return m.group(1).strip() if m else ""

    thesis  = _extract_prose(text, "论点")
    risks   = _extract_prose(text, "风险")
    triggers_raw = _extract_lines(text, "触发")
    triggers_list = [ln.strip().lstrip("-•·*").strip()
                     for ln in triggers_raw.split("\n")
                     if ln.strip() and len(ln.strip()) > 5]

    out = {
        "ok": True, "code": code,
        "comment":  text,  # 保留原始,前端可降级显示
        "thesis":   thesis,
        "risks":    risks,
        "triggers": triggers_list,
        "asOf":     d["asOf"],
    }
    cache_set_ttl(cache_key, out, 43200)  # 12 小时 — 评论不会分钟级变化
    return out


# ============================================================================
# 4) Group heatmap (族群热度地图)
# ============================================================================
@app.get("/api/group-heatmap")
def api_group_heatmap():
    from concurrent.futures import ThreadPoolExecutor
    wl = load_watchlist()

    def safe(code):
        try:
            return fetch_summary(code)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(safe, wl.keys()) if r and "error" not in r]

    groups: dict[str, list] = {}
    for s in results:
        groups.setdefault(s.get("group", "自选"), []).append(s)

    out = []
    for g, members in groups.items():
        changes = [(m["price"] - m["prev"]) / m["prev"] * 100 for m in members if m.get("prev")]
        avg = sum(changes) / len(changes) if changes else 0
        wins = sum(1 for c in changes if c > 0)
        members_sorted = sorted(members, key=lambda x: -((x["price"] - x["prev"]) / x["prev"] * 100 if x.get("prev") else 0))
        out.append({
            "group":      g,
            "count":      len(members),
            "avg_change": round(avg, 2),
            "wins":       wins,
            "losses":     len(changes) - wins,
            "max_up":     round(max(changes), 2) if changes else 0,
            "max_down":   round(min(changes), 2) if changes else 0,
            "members": [{
                "code":   m["code"],
                "name":   m["name"],
                "price":  m["price"],
                "change": round((m["price"] - m["prev"]) / m["prev"] * 100, 2) if m.get("prev") else 0,
            } for m in members_sorted],
        })
    out.sort(key=lambda x: -x["avg_change"])
    return out


@app.get("/api/group-rotation")
def api_group_rotation():
    """族群轮动侦测：每族群的 1W / 1M / 3M 平均报酬 + 动量加速度。
    用于观察「资金正从 X 族群流向 Y 族群」讯号。"""
    cache_key = "rotation:90d"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    codes = list(wl.keys())
    closes = download_closes(codes, start=start, end=end)
    closes = closes.dropna(how="all")

    # yf_codes = [wl[c]["yf"] for c in codes]
    # end = pd.Timestamp.today()
    # start = end - pd.Timedelta(days=120)
    # try:
    #     data = yf.download(yf_codes, start=start, end=end, auto_adjust=False,
    #                        progress=False, group_by="ticker", threads=True)
    # except Exception as e:
    #     raise HTTPException(503, f"yfinance batch fail: {e}")
    #
    # closes = pd.DataFrame()
    # for c, yfc in zip(codes, yf_codes):
    #     try:
    #         col = data[yfc]["Close"] if len(yf_codes) > 1 else data["Close"]
    #         closes[c] = col
    #     except Exception:
    #         continue
    # closes = closes.dropna(how="all")
    if len(closes) < 30:
        raise HTTPException(503, "资料不足")

    def n_day_ret(n):
        if len(closes) < n + 1:
            return {}
        return ((closes.iloc[-1] - closes.iloc[-n-1]) / closes.iloc[-n-1] * 100).to_dict()

    ret_1w = n_day_ret(5)
    ret_1m = n_day_ret(20)
    ret_3m = n_day_ret(60) if len(closes) >= 61 else {}

    by_group: dict[str, list[str]] = {}
    for code in closes.columns:
        g = wl.get(code, {}).get("group", "其他")
        by_group.setdefault(g, []).append(code)

    from statistics import mean as _mean
    rotation = []
    for g, members in by_group.items():
        def clean(d):
            vals = [v for v in (d.get(c) for c in members) if v is not None and not pd.isna(v)]
            return vals

        r1w_v = clean(ret_1w)
        r1m_v = clean(ret_1m)
        r3m_v = clean(ret_3m)
        r1w = _mean(r1w_v) if r1w_v else 0
        r1m = _mean(r1m_v) if r1m_v else 0
        r3m = _mean(r3m_v) if r3m_v else 0
        # 动量加速度 = 本周均报酬 vs 过去四周平均（年化视角）
        # 正 = 加速、负 = 减速
        momentum = round(r1w - (r1m / 4), 2) if r1m_v and r1w_v else 0

        rotation.append({
            "group":    g,
            "n":        len(members),
            "ret_1w":   round(r1w, 2),
            "ret_1m":   round(r1m, 2),
            "ret_3m":   round(r3m, 2),
            "momentum": momentum,
            "members":  members,
        })
    rotation.sort(key=lambda x: -x["momentum"])
    cache_set(cache_key, rotation)
    return rotation


# ============================================================================
# 5) Fundamentals (季营收 + 季 EPS via yfinance.quarterly_income_stmt)
# ============================================================================
@app.get("/api/fundamentals/{code}")
def api_fundamentals(code: str):
    """从 yfinance 抓季营收 + 季 EPS（美股按季公布）。"""
    cached = cache_get(f"fund:{code}")
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)
    yf_code = info["yf"]
    revenue: list[dict] = []
    eps: list[dict] = []
    try:
        rows = get_financials_history(yf_code, limit=12)   # 由旧到新
        # 营收序列 (优先 operating_revenue_ttm, 退而求其次 total_operating_revenue)
        rev_vals = []
        for r in rows:
            dt = r.get("trade_date")
            rev = _fin_num(r, "operating_revenue_ttm") or _fin_num(r, "total_operating_revenue")
            rev_vals.append((dt, rev))
        for i, (dt, rev) in enumerate(rev_vals):
            if rev is None or rev <= 0 or dt is None:
                continue
            yoy = None
            # YoY: 与 4 季前对比 (若库里有连续季度)
            if i >= 4 and rev_vals[i - 4][1] and rev_vals[i - 4][1] > 0:
                yoy = round((rev - rev_vals[i - 4][1]) / rev_vals[i - 4][1] * 100, 1)
            quarter = (dt.month - 1) // 3 + 1
            revenue.append({
                "ym": f"{dt.year}/Q{quarter}",
                "revenue": int(rev),     # 单位: 美元
                "yoy": yoy,
            })
        # EPS 序列
        for r in rows:
            dt = r.get("trade_date")
            v = _fin_num(r, "eps_ttm")
            if dt is None or v is None:
                continue
            eps.append({
                "date": dt.strftime("%Y-%m-%d"),
                "value": round(v, 2),
            })
        revenue = revenue[-12:]
        eps = eps[-12:]
    except Exception as e:
        print(f"[fund] {code}: {e}")

    out = {"code": code, "revenue": revenue, "eps": eps}
    cache_set(f"fund:{code}", out)
    return out

    # yf_code = info["yf"]
    #
    # revenue: list[dict] = []
    # eps: list[dict] = []
    # try:
    #     t = yf.Ticker(yf_code)
    #     qis = t.quarterly_income_stmt
    #     if qis is not None and not qis.empty:
    #         cols = sorted(qis.columns)  # 由旧到新
    #         qis = qis[cols]
    #         # Total Revenue
    #         row_rev = None
    #         for key in ("Total Revenue", "Operating Revenue", "Revenue"):
    #             if key in qis.index:
    #                 row_rev = qis.loc[key]
    #                 break
    #         if row_rev is not None:
    #             # YoY: 对齐 4 季前
    #             vals = [float(v) if not pd.isna(v) else 0.0 for v in row_rev.values]
    #             for i, dt in enumerate(cols):
    #                 if vals[i] <= 0:
    #                     continue
    #                 yoy = None
    #                 if i >= 4 and vals[i - 4] > 0:
    #                     yoy = round((vals[i] - vals[i - 4]) / vals[i - 4] * 100, 1)
    #                 quarter = (dt.month - 1) // 3 + 1
    #                 revenue.append({
    #                     "ym":      f"{dt.year}/Q{quarter}",
    #                     "revenue": int(vals[i]),     # USD ($)
    #                     "yoy":     yoy,
    #                 })
    #             revenue = revenue[-12:]
    #         # Basic / Diluted EPS
    #         row_eps = None
    #         for key in ("Diluted EPS", "Basic EPS"):
    #             if key in qis.index:
    #                 row_eps = qis.loc[key]
    #                 break
    #         if row_eps is not None:
    #             for dt, v in zip(cols, row_eps.values):
    #                 if pd.isna(v):
    #                     continue
    #                 eps.append({
    #                     "date":  dt.strftime("%Y-%m-%d"),
    #                     "value": round(float(v), 2),
    #                 })
    #             eps = eps[-12:]
    # except Exception as e:
    #     print(f"[fund] {code}: {e}")
    #
    # out = {"code": code, "revenue": revenue, "eps": eps}
    # cache_set(f"fund:{code}", out)
    # return out


# ============================================================================
# 6) Index overlay (S&P 500 大盘连动)
# ============================================================================
@app.get("/api/index")
def api_index(period: str = "D"):
    """S&P 500 ^GSPC，回传跟个股对齐用的 normalized 线。"""
    period = period.upper() if period.upper() in PERIOD_CFG else "D"
    cache_key = f"gspc:{period}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    cfg = PERIOD_CFG[period]
    try:
        h = yf.Ticker("^GSPC").history(period=cfg["period"], interval=cfg["interval"], auto_adjust=False)
    except Exception as e:
        return {"dates": [], "close": [], "error": str(e)}
    h = h.dropna(subset=["Close"]).iloc[-cfg["n"]:]
    if h.empty:
        return {"dates": [], "close": []}

    base = float(h["Close"].iloc[0])
    out = {
        "dates":   [idx.strftime("%m/%d") if period == "D" else idx.strftime("%Y/%m" if period == "M" else "%m/%d") for idx in h.index],
        "close":   [round(float(c), 2) for c in h["Close"].tolist()],
        "norm":    [round(float(c) / base * 100, 2) for c in h["Close"].tolist()],  # 起点 = 100
        "current": round(float(h["Close"].iloc[-1]), 2),
        "prev":    round(float(h["Close"].iloc[-2]), 2) if len(h) > 1 else round(base, 2),
    }
    cache_set(cache_key, out)
    return out


# ============================================================================
# 7) Backtest engine
# ============================================================================
@app.get("/api/backtest")
def api_backtest(
    strategy:    str   = "momentum",
    start:       str   = "",
    end:         str   = "",
    capital:     float = 100_000.0,
    hold_days:   int   = 5,
    n_positions: int   = 5,
    threshold:   float = 65,           # for win_rate / score
    group:       str   = "七巨头",
    universe:    str   = "watchlist",  # 'watchlist' | 'group:七巨头' | 'magseven'
):
    """每日 simulate 的简化回测引擎。
    策略：
      momentum       – 每日买涨幅前 N
      meanrev        – 每日买跌幅前 N
      win_rate       – 短线胜率 >= threshold (取 N 档)
      group          – 都买指定族群
      hot_group      – 都买当日涨幅最高的族群
      score          – 多因子综合评分 top N (MA20 + RSI + 动能 + 量能)
      score_adaptive – regime 自适应评分:每日侦测动能/防御/均衡盘,自动换权重
      golden_cross   – MA5 上穿 MA20 进场,死叉出场 (覆盖 hold_days)
      rsi_oversold   – RSI < threshold 且 MA5>MA20 进场,RSI > 60 出场
      new_high       – 创 60 日新高 + 量能 > 1.3x 均量 进场
      rs_rotation    – 每月底买近 20 日涨幅最强族群前 2 档,持有 1 个月
      earnings_drift – 连 2 季 (简化:近 90 日涨 > 10%) 强势股,提前持有 hold_days
    持股 hold_days 天后卖出，等比配重，benchmark = ^GSPC。
    """
    if not start:
        start = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    if not end:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")

    wl = load_watchlist()
    if universe.startswith("group:"):
        g = universe.split(":", 1)[1]
        codes = [c for c, m in wl.items() if (m.get("group") or "") == g]
    elif universe == "magseven":
        codes = [c for c, m in wl.items() if (m.get("group") or "") == "七巨头"]
    else:
        codes = list(wl.keys())
    if not codes:
        raise HTTPException(400, "Universe is empty")

    yf_codes = [wl[c]["yf"] for c in codes]
    cache_key = f"bt:{','.join(yf_codes)}:{start}:{end}"
    closes = None
    cached_prices = cache_get(cache_key)
    if cached_prices is not None:
        closes = cached_prices
    else:
        try:
            data = yf.download(yf_codes, start=start, end=end, auto_adjust=False,
                               progress=False, group_by="ticker", threads=True)
        except Exception as e:
            raise HTTPException(503, f"yfinance download fail: {e}")
        closes = pd.DataFrame()
        for c, yfc in zip(codes, yf_codes):
            try:
                col = data[yfc]["Close"] if len(yf_codes) > 1 else data["Close"]
                closes[c] = col
            except (KeyError, AttributeError):
                continue
        closes = closes.dropna(how="all")
        _cache[cache_key] = (time.time() + 1800 - CACHE_TTL, closes)  # 30 min cache

    if closes.empty or len(closes) < hold_days + 5:
        raise HTTPException(503, "历史资料不足")

    try:
        bench_raw = yf.download("^GSPC", start=start, end=end, auto_adjust=False, progress=False)
        bench = bench_raw["Close"]
        if hasattr(bench, 'columns'):  # 有时是 DataFrame 不是 Series
            bench = bench.iloc[:, 0]
    except Exception:
        bench = None

    def signals_at_day(day_idx: int) -> list:
        if day_idx < 20:
            return []
        today_close = closes.iloc[day_idx]
        prev_close  = closes.iloc[day_idx - 1]
        day_returns = ((today_close - prev_close) / prev_close * 100).dropna()
        if day_returns.empty:
            return []

        if strategy == "momentum":
            return day_returns.nlargest(n_positions).index.tolist()
        if strategy == "meanrev":
            return day_returns.nsmallest(n_positions).index.tolist()
        if strategy == "win_rate":
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 20: continue
                ma5  = hist.iloc[-5:].mean()
                ma20 = hist.iloc[-20:].mean()
                delta = hist.diff().iloc[-14:].dropna()
                gain = delta[delta > 0].sum()
                loss = -delta[delta < 0].sum()
                rsi = 100 - 100/(1 + (gain/loss)) if loss > 0 else 100
                base = 50
                if ma5 > ma20: base += 15
                elif ma5 < ma20: base -= 15
                if rsi > 70: base -= 10
                elif rsi < 30: base += 10
                if base >= threshold:
                    picks.append((c, base))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions]]
        if strategy == "group":
            return [c for c in closes.columns if (wl.get(c, {}).get("group") or "") == group]
        if strategy == "hot_group":
            grp_returns = {}
            for c in day_returns.index:
                g = wl.get(c, {}).get("group", "其他")
                grp_returns.setdefault(g, []).append(day_returns[c])
            if not grp_returns: return []
            avgs = {g: sum(v)/len(v) for g, v in grp_returns.items() if v}
            best_g = max(avgs, key=avgs.get)
            return [c for c in closes.columns if (wl.get(c, {}).get("group") or "") == best_g]

        if strategy == "score":
            # 多因子综合评分
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 20: continue
                ma5  = hist.iloc[-5:].mean()
                ma20 = hist.iloc[-20:].mean()
                cur  = hist.iloc[-1]
                delta = hist.diff().iloc[-14:].dropna()
                gain = delta[delta > 0].sum(); loss = -delta[delta < 0].sum()
                rsi = 100 - 100/(1 + (gain/loss)) if loss > 0 else 100
                ret5  = (cur - hist.iloc[-6]) / hist.iloc[-6] * 100 if len(hist) > 5 else 0
                ret20 = (cur - hist.iloc[-21]) / hist.iloc[-21] * 100 if len(hist) > 20 else 0
                sc = 0
                if ma5 > ma20: sc += 15
                else: sc -= 10
                if cur > ma20: sc += 10
                if 40 <= rsi <= 70: sc += 10
                elif rsi > 75: sc -= 15
                elif rsi < 30: sc += 8
                if ret5 > 3: sc += 10
                if ret20 > 8: sc += 12
                elif ret20 < -10: sc -= 15
                picks.append((c, sc))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions] if p[1] >= 15]

        if strategy == "score_adaptive":
            # 先侦测市场 regime (只用过去资料,无未来偷看)
            # 1) 全体股票横断面: 平均 20 日报酬 + 站上 MA20 的比例 (breadth)
            rets20, above_ma20 = [], 0
            n_valid = 0
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 21: continue
                n_valid += 1
                r20 = (hist.iloc[-1] - hist.iloc[-21]) / hist.iloc[-21] * 100
                rets20.append(r20)
                if hist.iloc[-1] > hist.iloc[-20:].mean():
                    above_ma20 += 1
            if n_valid < 5:
                return []
            avg_ret20 = sum(rets20) / len(rets20)
            breadth = above_ma20 / n_valid * 100

            # 2) 判 regime
            if breadth >= 60 and avg_ret20 > 3:
                regime = "momentum"      # 广度健康 + 普涨 → 动能盘
            elif breadth < 40 or avg_ret20 < -3:
                regime = "defensive"     # 广度差 / 普跌 → 防御盘
            else:
                regime = "balanced"

            # 3) 依 regime 给不同权重
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 21: continue
                ma5  = hist.iloc[-5:].mean()
                ma20 = hist.iloc[-20:].mean()
                cur  = hist.iloc[-1]
                delta = hist.diff().iloc[-14:].dropna()
                gain = delta[delta > 0].sum(); loss = -delta[delta < 0].sum()
                rsi = 100 - 100/(1 + (gain/loss)) if loss > 0 else 100
                ret5  = (cur - hist.iloc[-6]) / hist.iloc[-6] * 100
                ret20 = (cur - hist.iloc[-21]) / hist.iloc[-21] * 100
                sc = 0
                if regime == "momentum":
                    # 动能盘: 重 ret / 趋势,轻均值回归
                    if ma5 > ma20: sc += 20
                    else: sc -= 15
                    if cur > ma20: sc += 12
                    if ret5 > 3: sc += 18
                    if ret20 > 8: sc += 22
                    elif ret20 < -5: sc -= 10
                    if rsi > 80: sc -= 8       # 只有极端过热才扣 (动能盘容许高 RSI)
                elif regime == "defensive":
                    # 防御盘: 重均值回归 / 超卖反弹,避免追高
                    if rsi < 30: sc += 25       # 超卖大加分
                    elif rsi < 40: sc += 12
                    elif rsi > 70: sc -= 20      # 过热重扣
                    if ma5 > ma20: sc += 8       # 趋势仍给小分
                    if ret20 < -15: sc += 10     # 跌深反弹候选
                    elif ret20 > 10: sc -= 8     # 涨多回吐风险
                else:  # balanced
                    if ma5 > ma20: sc += 15
                    else: sc -= 10
                    if cur > ma20: sc += 10
                    if 40 <= rsi <= 70: sc += 10
                    elif rsi > 75: sc -= 15
                    elif rsi < 30: sc += 8
                    if ret5 > 3: sc += 10
                    if ret20 > 8: sc += 12
                    elif ret20 < -10: sc -= 15
                picks.append((c, sc))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions] if p[1] >= 15]

        if strategy == "golden_cross":
            # 黄金交叉触发 (MA5 上穿 MA20 当日)
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 22: continue
                ma5_t  = hist.iloc[-5:].mean()
                ma20_t = hist.iloc[-20:].mean()
                ma5_y  = hist.iloc[-6:-1].mean()
                ma20_y = hist.iloc[-21:-1].mean()
                # 昨天 MA5 < MA20,今天 >=
                if ma5_y < ma20_y and ma5_t >= ma20_t:
                    picks.append(c)
            return picks[:n_positions]

        if strategy == "rsi_oversold":
            # RSI < threshold 且 MA5 > MA20 (多头中的回调)
            picks = []
            thr = float(threshold) if threshold < 50 else 35
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 20: continue
                ma5  = hist.iloc[-5:].mean()
                ma20 = hist.iloc[-20:].mean()
                delta = hist.diff().iloc[-14:].dropna()
                gain = delta[delta > 0].sum(); loss = -delta[delta < 0].sum()
                rsi = 100 - 100/(1 + (gain/loss)) if loss > 0 else 100
                if rsi < thr and ma5 >= ma20:
                    picks.append((c, rsi))
            picks.sort(key=lambda x: x[1])  # RSI 越低越前
            return [p[0] for p in picks[:n_positions]]

        if strategy == "new_high":
            # 创 60d 新高 + 量能 >= 1.3x 20d avg
            picks = []
            if day_idx < 60: return []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 61: continue
                cur = hist.iloc[-1]
                prev60 = hist.iloc[-61:-1].max()
                if cur >= prev60 * 0.999:
                    picks.append((c, cur / prev60 - 1))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions]]

        if strategy == "rs_rotation":
            # 每月初(每 21 个交易日)重新调仓,买近 20d 涨幅最强族群前 2 档
            # 简化：每天都检查,但只在「离上次调仓 >= 20 个交易日」时返回新选股
            # 持有 hold_days 自动会处理
            if day_idx < 21: return []
            picks_by_group = {}
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 21: continue
                ret20 = (hist.iloc[-1] - hist.iloc[-21]) / hist.iloc[-21] * 100
                g = wl.get(c, {}).get("group", "其他")
                picks_by_group.setdefault(g, []).append((c, ret20))
            # 找最强族群
            grp_avg = {g: sum(r for _, r in lst)/len(lst) for g, lst in picks_by_group.items() if lst}
            if not grp_avg: return []
            best_g = max(grp_avg, key=grp_avg.get)
            # 该族群内最强前 2
            best_in_g = sorted(picks_by_group[best_g], key=lambda x: -x[1])[:2]
            return [c for c, _ in best_in_g]

        if strategy == "earnings_drift":
            # 简化:近 90 日涨 > 10% (动能持续) 且最近 5 日涨幅前 N
            # 真实版需要 earnings calendar 配合,这里用代理:强势股短线动能
            if day_idx < 90: return []
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 91: continue
                ret90 = (hist.iloc[-1] - hist.iloc[-91]) / hist.iloc[-91] * 100
                ret5  = (hist.iloc[-1] - hist.iloc[-6]) / hist.iloc[-6] * 100 if len(hist) > 5 else 0
                if ret90 >= 10:
                    picks.append((c, ret5))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions]]

        return []

    initial   = capital
    cash      = capital
    positions = {}
    equity_curve = []
    trades = []

    dates = closes.index.tolist()
    for day_idx, dt in enumerate(dates):
        today_close = closes.iloc[day_idx]
        # Close positions whose hold period reached
        for code in list(positions.keys()):
            pos = positions[code]
            if day_idx - pos["entry_idx"] >= hold_days:
                exit_p = today_close.get(code)
                if exit_p is None or pd.isna(exit_p):
                    continue
                exit_p = float(exit_p)
                cash += pos["shares"] * exit_p
                trades.append({
                    "code":       code,
                    "open_date":  pos["entry_date"],
                    "close_date": dt.strftime("%Y-%m-%d"),
                    "entry":      round(pos["entry_price"], 2),
                    "exit":       round(exit_p, 2),
                    "shares":     round(pos["shares"], 2),
                    "pnl":        round((exit_p - pos["entry_price"]) * pos["shares"], 0),
                    "ret_pct":    round((exit_p - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                })
                del positions[code]

        # Open new positions
        if len(positions) < n_positions:
            buys = signals_at_day(day_idx)
            for code in buys:
                if code in positions: continue
                if len(positions) >= n_positions: break
                price = today_close.get(code)
                if price is None or pd.isna(price): continue
                price = float(price)
                if price <= 0: continue
                slot_value = min(capital / n_positions, cash)
                if slot_value < 100: continue
                shares = slot_value / price
                cash -= shares * price
                positions[code] = {
                    "entry_date":  dt.strftime("%Y-%m-%d"),
                    "entry_idx":   day_idx,
                    "shares":      shares,
                    "entry_price": price,
                }

        # Mark-to-market
        mv = 0.0
        for code, pos in positions.items():
            p = today_close.get(code)
            if p is None or pd.isna(p): p = pos["entry_price"]
            mv += pos["shares"] * float(p)
        equity = cash + mv

        bench_v = None
        if bench is not None and len(bench) > 0:
            try:
                base_b = float(bench.iloc[0])
                cur_b = float(bench.loc[dt]) if dt in bench.index else None
                if cur_b and base_b:
                    bench_v = round(cur_b / base_b * initial, 0)
            except Exception:
                bench_v = None

        equity_curve.append({
            "date":      dt.strftime("%Y-%m-%d"),
            "equity":    round(equity, 0),
            "benchmark": bench_v,
        })

    # Summary
    if equity_curve:
        final_eq = equity_curve[-1]["equity"]
        total_ret = (final_eq - initial) / initial * 100
        peak, mdd = initial, 0
        for e in equity_curve:
            peak = max(peak, e["equity"])
            dd = (e["equity"] - peak) / peak * 100
            mdd = min(mdd, dd)
        bench_final = equity_curve[-1].get("benchmark")
        bench_ret = ((bench_final - initial) / initial * 100) if bench_final else None
        winning = sum(1 for t in trades if t["ret_pct"] > 0)
        wr = round(winning / len(trades) * 100, 1) if trades else 0
        avg_win = round(sum(t["ret_pct"] for t in trades if t["ret_pct"] > 0) / max(winning, 1), 2)
        loser_cnt = len(trades) - winning
        avg_loss = round(sum(t["ret_pct"] for t in trades if t["ret_pct"] <= 0) / max(loser_cnt, 1), 2)
    else:
        final_eq, total_ret, mdd, bench_ret, wr, avg_win, avg_loss = initial, 0, 0, None, 0, 0, 0

    # Downsample equity curve if too long
    step = max(1, len(equity_curve) // 250)
    ec_ds = equity_curve[::step]
    if equity_curve and ec_ds[-1] != equity_curve[-1]:
        ec_ds.append(equity_curve[-1])

    return {
        "strategy": strategy,
        "params": {
            "start": start, "end": end, "capital": initial,
            "hold_days": hold_days, "n_positions": n_positions,
            "threshold": threshold, "group": group, "universe": universe,
        },
        "summary": {
            "start_date":       dates[0].strftime("%Y-%m-%d") if dates else "",
            "end_date":         dates[-1].strftime("%Y-%m-%d") if dates else "",
            "trading_days":     len(dates),
            "initial_capital":  initial,
            "final_equity":     final_eq,
            "total_return":     round(total_ret, 2),
            "benchmark_return": round(bench_ret, 2) if bench_ret is not None else None,
            "alpha":            round(total_ret - bench_ret, 2) if bench_ret is not None else None,
            "max_drawdown":     round(mdd, 2),
            "win_rate":         wr,
            "n_trades":         len(trades),
            "avg_win":          avg_win,
            "avg_loss":         avg_loss,
        },
        "equity_curve": ec_ds,
        "trades": trades[-100:],
    }


@app.get("/api/backtest-oos")
def api_backtest_oos(
    strategy:    str   = "score",
    start:       str   = "",
    end:         str   = "",
    capital:     float = 100_000.0,
    hold_days:   int   = 5,
    n_positions: int   = 5,
    threshold:   float = 65,
    group:       str   = "七巨头",
    universe:    str   = "watchlist",
    train_ratio: float = 0.7,
):
    """Walk-forward 验证:把时间切成 train_ratio (IS) / 1-train_ratio (OOS)
    在 IS 跑一遍、OOS 跑一遍,比较 alpha/胜率 衰退幅度。
    OOS 表现显著差于 IS → 策略可能过拟合。"""
    if not start:
        start = (pd.Timestamp.today() - pd.Timedelta(days=730)).strftime("%Y-%m-%d")
    if not end:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")

    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    split_dt = start_dt + (end_dt - start_dt) * train_ratio
    is_end   = split_dt.strftime("%Y-%m-%d")
    oos_start = split_dt.strftime("%Y-%m-%d")

    kwargs = dict(strategy=strategy, capital=capital, hold_days=hold_days,
                  n_positions=n_positions, threshold=threshold,
                  group=group, universe=universe)

    is_res  = api_backtest(start=start,    end=is_end,  **kwargs)
    oos_res = api_backtest(start=oos_start, end=end,    **kwargs)

    def _decay(a, b):
        if a is None or b is None: return None
        return round(b - a, 2)

    is_s = is_res["summary"]
    oos_s = oos_res["summary"]
    health = "passed"
    note = ""
    # 过拟合判断:OOS alpha << IS alpha 才算过拟合
    is_alpha = is_s.get("alpha") or 0
    oos_alpha = oos_s.get("alpha") or 0
    if is_alpha > 20 and oos_alpha < is_alpha * 0.3:
        health = "overfit"; note = f"OOS alpha 只剩 IS 的 {oos_alpha/is_alpha*100:.0f}%,疑似过拟合"
    elif is_alpha > 0 and oos_alpha < 0:
        health = "overfit"; note = f"IS 赚 OOS 赔,过拟合明显"
    elif oos_alpha >= is_alpha * 0.7:
        health = "passed"; note = "OOS 与 IS 表现一致,策略稳健"
    else:
        health = "degraded"; note = "OOS 表现略差但可接受"

    return {
        "strategy":   strategy,
        "split_date": split_dt.strftime("%Y-%m-%d"),
        "train_ratio": train_ratio,
        "in_sample":     is_res,
        "out_of_sample": oos_res,
        "decay": {
            "total_return": _decay(is_s.get("total_return"), oos_s.get("total_return")),
            "alpha":        _decay(is_s.get("alpha"),        oos_s.get("alpha")),
            "win_rate":     _decay(is_s.get("win_rate"),     oos_s.get("win_rate")),
            "max_drawdown": _decay(is_s.get("max_drawdown"), oos_s.get("max_drawdown")),
        },
        "health": health,
        "note":   note,
    }


# ============================================================================
# 主题轮动 (Theme Rotation) — 跨族群的主题标签动量
# ============================================================================
@app.get("/api/theme-rotation")
def api_theme_rotation():
    """主题轮动:依 themes 标签聚合,计算每主题 1W/1M/3M 平均报酬 + 动量。
    一档股票可属多主题,所以同一档可能出现在多个主题的成员里。"""
    cache_key = "theme_rotation:90d"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    codes = list(wl.keys())
    closes = download_closes(codes, start=start, end=end)
    closes = closes.dropna(how="all")

    # yf_codes = [wl[c]["yf"] for c in codes]
    # end = pd.Timestamp.today()
    # start = end - pd.Timedelta(days=120)
    # try:
    #     data = yf.download(yf_codes, start=start, end=end, auto_adjust=False,
    #                        progress=False, group_by="ticker", threads=True)
    # except Exception as e:
    #     raise HTTPException(503, f"yfinance batch fail: {e}")
    #
    # closes = pd.DataFrame()
    # for c, yfc in zip(codes, yf_codes):
    #     try:
    #         col = data[yfc]["Close"] if len(yf_codes) > 1 else data["Close"]
    #         closes[c] = col
    #     except Exception:
    #         continue
    # closes = closes.dropna(how="all")
    if len(closes) < 30:
        raise HTTPException(503, "资料不足")

    def n_day_ret(n):
        if len(closes) < n + 1: return {}
        return ((closes.iloc[-1] - closes.iloc[-n-1]) / closes.iloc[-n-1] * 100).to_dict()

    ret_1w = n_day_ret(5)
    ret_1m = n_day_ret(20)
    ret_3m = n_day_ret(60) if len(closes) >= 61 else {}

    # 聚合 themes → 成员
    by_theme: dict[str, list[str]] = {}
    for code in closes.columns:
        for t in wl.get(code, {}).get("themes", []) or []:
            by_theme.setdefault(t, []).append(code)

    from statistics import mean as _mean
    out = []
    for t, members in by_theme.items():
        if len(members) < 2:  # 单一成员主题没意义
            continue
        def clean(d):
            return [v for v in (d.get(c) for c in members) if v is not None and not pd.isna(v)]
        r1w_v, r1m_v, r3m_v = clean(ret_1w), clean(ret_1m), clean(ret_3m)
        r1w = _mean(r1w_v) if r1w_v else 0
        r1m = _mean(r1m_v) if r1m_v else 0
        r3m = _mean(r3m_v) if r3m_v else 0
        momentum = round(r1w - (r1m / 4), 2) if r1m_v and r1w_v else 0
        out.append({
            "theme":  t,
            "n":      len(members),
            "ret_1w": round(r1w, 2),
            "ret_1m": round(r1m, 2),
            "ret_3m": round(r3m, 2),
            "momentum": momentum,
            "members": members,
        })
    out.sort(key=lambda x: -x["momentum"])
    cache_set(cache_key, out)
    return out


# ============================================================================
# 市场宽度 (Breadth)
# ============================================================================
@app.get("/api/breadth")
def api_breadth():
    """市场宽度：watchlist 多少%在 MA50/MA200 上、A/D、SPY vs RSP 等权重比较。"""
    cache_key = "breadth"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    above_50 = above_200 = 0
    advancers = decliners = 0
    total = 0
    bull_trend = bear_trend = 0
    for code in wl:
        try:
            d = fetch_stock(code)
        except Exception:
            continue
        total += 1
        price = d["price"]
        ma20  = d.get("ma20") or 0
        ma60  = d.get("ma60") or 0
        prev  = d.get("prev") or 0
        # 用 ma20/ma60 当近似 (没有 50/200 日均线)
        if ma20 and price > ma20: above_50  += 1
        if ma60 and price > ma60: above_200 += 1
        if prev:
            if price > prev: advancers += 1
            elif price < prev: decliners += 1
        if "多头" in d.get("trend", ""): bull_trend += 1
        elif "空头" in d.get("trend", ""): bear_trend += 1

    pct_50  = round(above_50  / total * 100, 1) if total else 0
    pct_200 = round(above_200 / total * 100, 1) if total else 0
    ad_ratio = round(advancers / decliners, 2) if decliners else (advancers if advancers else 0)

    # SPY vs RSP 等权重 (宽度近似)
    spy_rsp = None
    spy_chg = rsp_chg = None
    try:
        end = pd.Timestamp.today()
        start = end - pd.Timedelta(days=10)
        sp_data = yf.download(["SPY", "RSP"], start=start, end=end, auto_adjust=False, progress=False, group_by="ticker", threads=True)
        spy_c = sp_data["SPY"]["Close"]
        rsp_c = sp_data["RSP"]["Close"]
        if len(spy_c) >= 2 and len(rsp_c) >= 2:
            spy_chg = float((spy_c.iloc[-1] - spy_c.iloc[-2]) / spy_c.iloc[-2] * 100)
            rsp_chg = float((rsp_c.iloc[-1] - rsp_c.iloc[-2]) / rsp_c.iloc[-2] * 100)
            spy_rsp = round(spy_chg - rsp_chg, 2)  # 正 = 龙头股拉,负 = 中小盘拉
    except Exception as e:
        print(f"[breadth spy/rsp] {e}")

    # 健康度结论
    status = "neutral"
    note = ""
    if pct_50 >= 70 and pct_200 >= 60:
        status = "strong"; note = "多头格局明确"
    elif pct_50 >= 50 and pct_200 >= 50:
        status = "healthy"; note = "偏多但留意过热"
    elif pct_50 < 40 and pct_200 < 40:
        status = "weak"; note = "短中线都失守,警戒"
    elif pct_200 < 50 and pct_50 > 60:
        status = "divergence"; note = "短线拉、中线弱,假反弹警惕"

    out = {
        "total":        total,
        "pct_above_50":  pct_50,
        "pct_above_200": pct_200,
        "advancers":    advancers,
        "decliners":    decliners,
        "unchanged":    total - advancers - decliners,
        "ad_ratio":     ad_ratio,
        "bull_trend":   bull_trend,
        "bear_trend":   bear_trend,
        "spy_change":   round(spy_chg, 2) if spy_chg is not None else None,
        "rsp_change":   round(rsp_chg, 2) if rsp_chg is not None else None,
        "spy_vs_rsp":   spy_rsp,
        "status":       status,
        "note":         note,
    }
    cache_set(cache_key, out)
    return out


# ============================================================================
# 52 周新高 / 新低扫描
# ============================================================================
@app.get("/api/52w-scan")
def api_52w_scan():
    """扫描 watchlist 创 52 周新高/新低的个股。"""
    cache_key = "52w_scan"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=400)
    yf_codes = [wl[c]["yf"] for c in wl]
    codes = list(wl.keys())
    new_highs = []
    new_lows = []
    near_highs = []  # 接近新高 (3% 内)
    try:
        data = download_closes(codes, start=start, end=end)
    except Exception as e:
        raise HTTPException(503, f"yfinance batch fail: {e}")

    for code, yfc in zip(codes, yf_codes):
        try:
            df = get_history(yfc, start=str(start)[:10], end=str(end)[:10])
            if df.empty:
                continue
            close = df["Close"].dropna()
            vol   = df["Volume"].dropna()
            if len(close) < 252:
                continue
            year = close.iloc[-252:]
            cur = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else cur
            year_high = float(year.max())
            year_low  = float(year.min())
            cur_vol = float(vol.iloc[-1]) if len(vol) else 0
            avg_vol_20 = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else 0
            vol_ratio = round(cur_vol / avg_vol_20, 2) if avg_vol_20 else 0
            chg_pct = round((cur - prev) / prev * 100, 2) if prev else 0
            info = wl.get(code, {})
            base = {
                "code":      code,
                "name":      info.get("name", code),
                "group":     info.get("group", "—"),
                "price":     round(cur, 2),
                "year_high": round(year_high, 2),
                "year_low":  round(year_low, 2),
                "vol_ratio": vol_ratio,
                "change_pct": chg_pct,
            }
            if cur >= year_high * 0.999:
                new_highs.append({**base, "type": "new_high"})
            elif cur <= year_low * 1.001:
                new_lows.append({**base, "type": "new_low"})
            elif cur >= year_high * 0.97:
                base["pct_from_high"] = round((cur - year_high) / year_high * 100, 2)
                near_highs.append({**base, "type": "near_high"})
        except Exception:
            continue

    new_highs.sort(key=lambda x: -x["vol_ratio"])
    near_highs.sort(key=lambda x: -(x.get("pct_from_high") or -999))
    out = {
        "new_highs":  new_highs,
        "new_lows":   new_lows,
        "near_highs": near_highs[:10],
        "as_of":      str(end.date()),
    }
    cache_set(cache_key, out)
    return out


# ============================================================================
# Insider Cluster Buying — 跨档群聚买进侦测
# ============================================================================
@app.get("/api/insider-cluster")
def api_insider_cluster(days: int = 30):
    """侦测 N 天内多个 insider 集中买进的股票。"""
    cache_key = f"insider_cluster:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    out = []
    for code in wl:
        try:
            r = fetch_insider(code, days=days)
        except Exception:
            continue
        s = r.get("summary", {})
        buys = [t for t in r.get("transactions", []) if t.get("action") == "buy"]
        # 不同 insider 的人数
        buyers = set(t.get("insider") for t in buys if t.get("insider"))
        n_buyers = len(buyers)
        buy_count = len(buys)
        buy_value = s.get("buy_value", 0) or 0
        # 评分：人数 >= 2 + 总额大 = 群聚讯号
        if n_buyers < 2 or buy_value < 100000:
            continue
        score = 0
        if n_buyers >= 5: score += 40
        elif n_buyers >= 3: score += 25
        elif n_buyers >= 2: score += 15
        if buy_value >= 5_000_000: score += 30
        elif buy_value >= 1_000_000: score += 20
        elif buy_value >= 500_000: score += 10
        # 净流入 (买 - 卖) 为正再加分
        net = s.get("net_value", 0) or 0
        if net > 0: score += 10
        info = wl.get(code, {})
        out.append({
            "code":       code,
            "name":       info.get("name", code),
            "group":      info.get("group", "—"),
            "n_buyers":   n_buyers,
            "buy_count":  buy_count,
            "buy_value":  buy_value,
            "net_value":  net,
            "top_buyers": list(buyers)[:5],
            "score":      score,
        })
    out.sort(key=lambda x: -x["score"])
    cache_set(cache_key, out)
    return out


# ============================================================================
# 投组 Drawdown 曲线 — 每档近 60d 从高点回档轨迹
# ============================================================================
@app.get("/api/portfolio/drawdown-curve")
def api_portfolio_drawdown(days: int = 60):
    """从 portfolio.json 计算每档近 N 天从滚动高点的 drawdown %。"""
    cache_key = f"pf_dd:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    p = load_portfolio()
    if not p:
        return {"holdings": [], "asOf": str(pd.Timestamp.today().date())}
    wl = load_watchlist()
    # 依 code 聚合 (避免多笔同档重复)
    seen = {}
    for h in p:
        c = h.get("code")
        if c and c not in seen and c in wl:
            seen[c] = wl[c]["yf"]
    codes = list(seen.keys())
    yf_codes = [seen[c] for c in codes]
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=days + 10)
    try:
        data = download_closes(codes, start=start, end=end)
    except Exception as e:
        raise HTTPException(503, f"yfinance fail: {e}")

    out_holdings = []
    for c, yfc in zip(codes, yf_codes):
        try:
            close = data[yfc]["Close"].dropna() if len(yf_codes) > 1 else data["Close"].dropna()
            if len(close) < 5:
                continue
            close = close.iloc[-days:]
            rolling_max = close.cummax()
            dd_pct = ((close - rolling_max) / rolling_max * 100)
            cur_dd = float(dd_pct.iloc[-1])
            max_dd = float(dd_pct.min())
            series = [
                {"date": str(idx.date()), "price": round(float(p), 2),
                 "peak": round(float(rolling_max.iloc[i]), 2),
                 "dd_pct": round(float(dd_pct.iloc[i]), 2)}
                for i, (idx, p) in enumerate(close.items())
            ]
            out_holdings.append({
                "code":   c,
                "name":   wl.get(c, {}).get("name", c),
                "group":  wl.get(c, {}).get("group", "—"),
                "cur_dd": round(cur_dd, 2),
                "max_dd": round(max_dd, 2),
                "current_price": round(float(close.iloc[-1]), 2),
                "peak_price":    round(float(rolling_max.iloc[-1]), 2),
                "series": series[-30:],  # 前端只渲染最近 30 个点
            })
        except Exception:
            continue
    # 按 cur_dd 由负到正排序 (最差先)
    out_holdings.sort(key=lambda x: x["cur_dd"])
    result = {"holdings": out_holdings, "asOf": str(end.date()), "days": days}
    cache_set(cache_key, result)
    return result


# ============================================================================
# 投组再平衡建议 — 给定目标权重,算出买卖动作
# ============================================================================
@app.get("/api/portfolio/rebalance")
def api_portfolio_rebalance(by: str = "group"):
    """依 group / theme / code 目标权重,算当前 vs 目标差异,给出再平衡建议。
    by:
      - group: 依板块配比目标 (预设按现有比例,可由 rebalance_target.json 覆盖)
      - theme: 依主题配比
      - code:  逐档给目标
    """
    target_cfg = load_json(REBALANCE_TARGET_FILE, {})
    p = load_portfolio()
    if not p:
        return {"holdings": [], "current": {}, "target": {}, "actions": [],
                "by": by, "total_value": 0, "asOf": str(pd.Timestamp.today().date()),
                "warnings": ["无持股资料"]}

    wl = load_watchlist()
    # 算每档市值
    holdings_value: dict[str, float] = {}
    holdings_info: dict[str, dict] = {}
    total_value = 0.0
    for h in p:
        c = h.get("code")
        if not c: continue
        try:
            s = fetch_summary(c)
            price = float(s["price"])
        except Exception:
            price = float(h.get("cost_price", 0))
        shares = float(h.get("shares", 0))
        v = shares * price
        holdings_value[c] = holdings_value.get(c, 0) + v
        holdings_info[c] = {
            "name":   wl.get(c, {}).get("name", c),
            "group":  wl.get(c, {}).get("group", "—"),
            "themes": wl.get(c, {}).get("themes", []),
            "price":  price,
        }
        total_value += v

    if total_value <= 0:
        return {"holdings": [], "actions": [], "by": by, "total_value": 0,
                "warnings": ["持股总市值为 0"]}

    # 算 current 分布
    current: dict[str, float] = {}  # 群 → 市值
    for c, v in holdings_value.items():
        if by == "code":
            current[c] = current.get(c, 0) + v
        elif by == "theme":
            ths = holdings_info[c].get("themes", []) or ["(无主题)"]
            # 多主题均分权重
            share = 1.0 / len(ths)
            for t in ths:
                current[t] = current.get(t, 0) + v * share
        else:  # group
            g = holdings_info[c].get("group", "—")
            current[g] = current.get(g, 0) + v

    # 取目标 (target_cfg by 对应的 key,若无则用 current 比例,并标记「无目标」)
    target_pct = target_cfg.get(by, {})
    using_default = False
    if not target_pct:
        # 预设:把当前实况作为目标,让 user 知道没设目标
        target_pct = {k: v / total_value * 100 for k, v in current.items()}
        using_default = True

    # 正规化 target_pct (确保总和 100)
    s = sum(target_pct.values())
    if s > 0 and abs(s - 100) > 0.5:
        target_pct = {k: v / s * 100 for k, v in target_pct.items()}

    # 算 actions
    actions = []
    all_keys = set(current.keys()) | set(target_pct.keys())
    for k in all_keys:
        cur_v = current.get(k, 0)
        cur_pct = cur_v / total_value * 100
        tgt_pct = target_pct.get(k, 0)
        delta_pct = tgt_pct - cur_pct
        delta_v   = total_value * delta_pct / 100
        if abs(delta_pct) < 0.5:
            continue  # 差异 < 0.5% 略过
        actions.append({
            "key":         k,
            "current_pct": round(cur_pct, 2),
            "target_pct":  round(tgt_pct, 2),
            "delta_pct":   round(delta_pct, 2),
            "delta_value": round(delta_v, 2),
            "action":      "买" if delta_v > 0 else "卖",
        })
    actions.sort(key=lambda x: -abs(x["delta_pct"]))

    return {
        "by":          by,
        "total_value": round(total_value, 2),
        "current":     {k: round(v / total_value * 100, 2) for k, v in current.items()},
        "target":      {k: round(v, 2) for k, v in target_pct.items()},
        "actions":     actions,
        "using_default_target": using_default,
        "warnings": ["未设目标,目前用「维持现状」为目标。可编辑 rebalance_target.json 设定真实目标"]
                    if using_default else [],
        "asOf":        str(pd.Timestamp.today().date()),
    }


# ============================================================================
# 停利扫描 — 侦测「拉升过快 + 筹码散 + 支撑脆弱」三条件
# ============================================================================
@app.get("/api/profit-taking-scan")
def api_profit_taking_scan():
    """扫 portfolio holdings,侦测该停利的 3 个条件:
    A. 拉升过快: RSI > 70 OR 乖离 > 12% OR ret_5d > 8%
    B. 筹码散: insider 净 < -$1M OR Top10 < 30% (仅看美股大型股)
    C. 支撑脆弱: 跌破 MA20 OR Drawdown > -5%
    回传每档的 (条件命中数, 细节, 建议动作)。
    """
    cache_key = "profit_taking_scan"
    cached = cache_get(cache_key)
    if cached:
        return cached

    p = load_portfolio()
    if not p:
        return {"scanned": 0, "warnings": [], "asOf": str(pd.Timestamp.today().date())}

    wl = load_watchlist()
    seen_codes = set()
    out = []
    for h in p:
        code = h.get("code")
        if not code or code in seen_codes or code not in wl:
            continue
        seen_codes.add(code)
        try:
            d = fetch_stock(code)
        except Exception:
            continue

        price = d["price"]; prev = d["prev"]
        rsi = d["rsi"]
        ma20 = d.get("ma20") or 0
        bias = ((price - ma20) / ma20 * 100) if ma20 else 0
        # 算 ret_5d 简单版 (close 5 天前比现价)
        klines = d.get("klines", [])
        ret_5d = None
        if len(klines) >= 6:
            p_5d_ago = klines[-6]["ohlc"][1]
            if p_5d_ago:
                ret_5d = (price - p_5d_ago) / p_5d_ago * 100

        # === Condition A: 拉升过快 ===
        cond_a_reasons = []
        if rsi > 70:           cond_a_reasons.append(f"RSI {rsi:.0f} 过热")
        if bias > 12:          cond_a_reasons.append(f"乖离 +{bias:.1f}%")
        if ret_5d and ret_5d > 8: cond_a_reasons.append(f"5日 +{ret_5d:.1f}%")
        cond_a = len(cond_a_reasons) > 0

        # === Condition B: 筹码散 ===
        cond_b_reasons = []
        try:
            ins = fetch_insider(code, days=180)
            net = ins.get("summary", {}).get("net_value", 0)
            if net <= -1_000_000:
                cond_b_reasons.append(f"内部人 6M 净卖 ${abs(net)/1e6:.1f}M")
        except Exception:
            pass
        try:
            ih = fetch_institutional_holders(code)
            isum = ih.get("summary", {})
            top10 = isum.get("top10_pct", 0)
            if top10 and top10 < 30:
                cond_b_reasons.append(f"Top10 集中度只 {top10}%")
        except Exception:
            pass
        cond_b = len(cond_b_reasons) > 0

        # === Condition C: 支撑脆弱 ===
        cond_c_reasons = []
        if ma20 and price < ma20:
            cond_c_reasons.append(f"跌破 MA20 (${ma20:.2f})")
        # drawdown from 60d high
        closes = [k["ohlc"][1] for k in klines if k.get("ohlc")]
        if len(closes) >= 20:
            peak = max(closes)
            dd = (price - peak) / peak * 100 if peak else 0
            if dd <= -5:
                cond_c_reasons.append(f"距高点 {dd:.1f}% (${peak:.2f}→${price:.2f})")
        cond_c = len(cond_c_reasons) > 0

        hits = sum([cond_a, cond_b, cond_c])
        if hits == 0:
            continue  # 完全没事的不列出

        # 建议动作
        if hits >= 3:
            action = "减码 50%"; level = "critical"
        elif hits == 2:
            action = "收紧移动停利 (8% → 5%)"; level = "warning"
        else:
            action = "警示观察,不动作"; level = "watch"

        out.append({
            "code":   code,
            "name":   wl.get(code, {}).get("name", code),
            "group":  wl.get(code, {}).get("group", "—"),
            "price":  price,
            "rsi":    round(rsi, 1),
            "bias":   round(bias, 2),
            "ret_5d": round(ret_5d, 2) if ret_5d is not None else None,
            "hits":   hits,
            "level":  level,
            "action": action,
            "conditions": {
                "拉升过快": cond_a_reasons,
                "筹码散":   cond_b_reasons,
                "支撑脆弱": cond_c_reasons,
            },
        })

    # 危险先排
    level_order = {"critical": 0, "warning": 1, "watch": 2}
    out.sort(key=lambda x: (level_order.get(x["level"], 9), -x["hits"]))

    result = {
        "scanned": len(seen_codes),
        "warnings": out,
        "asOf": str(pd.Timestamp.today().date()),
        "critical_count": sum(1 for x in out if x["level"] == "critical"),
        "warning_count":  sum(1 for x in out if x["level"] == "warning"),
        "watch_count":    sum(1 for x in out if x["level"] == "watch"),
    }
    cache_set_ttl(cache_key, result, 1800)  # 30 分钟 cache
    return result


FIRSTRADE_SHARED = Path("d:/python/shared/firstrade_holdings.json")


@app.post("/api/portfolio/import-firstrade")
def api_import_firstrade(mode: str = "replace"):
    """从 d:/python/shared/firstrade_holdings.json 汇入持仓到 portfolio.json。
    mode:
      - replace: 整个 portfolio.json 换成 firstrade 的快照 (预设)
      - merge:   保留现有 + 加上 firstrade 新增 (用 code 比对,firstrade 为准)
    """
    if not FIRSTRADE_SHARED.exists():
        raise HTTPException(404, f"找不到 {FIRSTRADE_SHARED}。请先在 Firstrade GUI 开启「持仓查询」")
    try:
        payload = json.loads(FIRSTRADE_SHARED.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(503, f"解析失败: {e}")

    lots = payload.get("lots", [])
    ts   = payload.get("ts", 0)
    age_hours = (time.time() - ts) / 3600 if ts else 999

    new_pf = []
    for i, lot in enumerate(lots):
        code = (lot.get("code") or "").upper()
        if not code: continue
        new_pf.append({
            "id":         f"ft_{int(ts)}_{i}",
            "code":       code,
            "shares":     float(lot.get("shares", 0)),
            "cost_price": float(lot.get("cost_price", 0)),
            "buy_date":   lot.get("buy_date", ""),
            "note":       "imported from Firstrade",
        })

    if mode == "merge":
        existing = load_portfolio()
        # 用 code 去重:firstrade 为准
        ft_codes = {p["code"] for p in new_pf}
        keep = [h for h in existing if h.get("code") not in ft_codes]
        new_pf = keep + new_pf

    save_json(PORTFOLIO_FILE, new_pf)
    _cache.pop("portfolio_risk", None)
    return {
        "ok": True, "imported": len(lots), "total_after": len(new_pf),
        "mode": mode, "source_age_hours": round(age_hours, 1),
    }


@app.post("/api/portfolio/rebalance-target")
def api_set_rebalance_target(target: dict):
    """设定再平衡目标权重 (写到 rebalance_target.json)。
    payload 结构: { "group": { "AI 应用层": 20, ... }, "theme": { ... }, "code": { ... } }
    """
    cur = load_json(REBALANCE_TARGET_FILE, {})
    for k, v in (target or {}).items():
        cur[k] = v
    save_json(REBALANCE_TARGET_FILE, cur)
    return {"ok": True, "target": cur}


# ============================================================================
# 投组风险：Beta、板块集中度、相关性矩阵
# ============================================================================
@app.get("/api/portfolio-risk")
def api_portfolio_risk():
    """从 portfolio.json 计算投组风险指标。"""
    cache_key = "portfolio_risk"
    cached = cache_get(cache_key)
    if cached:
        return cached

    p = load_portfolio()
    if not p:
        return {"holdings": [], "sectors": [], "warnings": [],
                "portfolio_beta": None, "corr_codes": [], "corr_matrix": [],
                "total_holdings": 0, "total_value": 0, "total_pnl": 0}

    wl = load_watchlist()
    codes = sorted(set(h["code"] for h in p if h.get("code")))
    yf_codes = [wl.get(c, {}).get("yf", c) for c in codes]

    # 1. 抓 60 日收盘 + SPY
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=90)
    closes = pd.DataFrame()
    try:
        all_codes = list(set(yf_codes + ["SPY"]))
        data = yf.download(all_codes, start=start, end=end, auto_adjust=False,
                           progress=False, group_by="ticker", threads=True)
        for yfc in all_codes:
            try:
                col = data[yfc]["Close"] if len(all_codes) > 1 else data["Close"]
                closes[yfc] = col
            except Exception:
                pass
        closes = closes.dropna(how="all")
    except Exception as e:
        print(f"[risk] yfinance fail: {e}")

    # 2. Daily returns + beta calc
    returns = closes.pct_change().dropna() if not closes.empty else pd.DataFrame()
    spy_ret = returns["SPY"] if "SPY" in returns.columns else None
    spy_var = float(spy_ret.var()) if spy_ret is not None and len(spy_ret) > 5 else None

    def _beta(yfc):
        if spy_ret is None or spy_var is None or spy_var == 0: return None
        if yfc not in returns.columns: return None
        try:
            cov = float(returns[yfc].cov(spy_ret))
            return round(cov / spy_var, 3)
        except Exception:
            return None

    # 3. Per-holding metrics — 先依 code 聚合多笔同档
    agg: dict[str, dict] = {}
    for h in p:
        code = h.get("code")
        if not code: continue
        try:
            s = fetch_summary(code)
            price = float(s["price"])
        except Exception:
            price = float(h.get("cost_price", 0))
        shares  = float(h.get("shares", 0))
        cost_p  = float(h.get("cost_price", 0))
        entry = agg.get(code)
        if entry:
            entry["shares"] += shares
            entry["cost"]   += shares * cost_p
        else:
            agg[code] = {
                "code":   code,
                "name":   wl.get(code, {}).get("name", code),
                "group":  wl.get(code, {}).get("group", "其他"),
                "yf":     wl.get(code, {}).get("yf", code),
                "shares": shares,
                "cost":   shares * cost_p,
                "price":  price,
            }

    holdings = []
    total_value = 0.0
    total_pnl   = 0.0
    by_group = {}
    for code, e in agg.items():
        value = e["shares"] * e["price"]
        pnl   = value - e["cost"]
        total_value += value
        total_pnl   += pnl
        beta = _beta(e["yf"])
        holdings.append({
            "code":   code,
            "name":   e["name"],
            "group":  e["group"],
            "shares": e["shares"],
            "price":  e["price"],
            "value":  value,
            "pnl":    pnl,
            "beta":   beta,
            "yf":     e["yf"],
        })
        by_group.setdefault(e["group"], 0)
        by_group[e["group"]] += value

    # 4. Weights + contributions
    for h in holdings:
        h["weight_pct"] = round(h["value"] / total_value * 100, 2) if total_value else 0
        h["contrib_beta"] = round(h["beta"] * h["weight_pct"] / 100, 3) if h["beta"] is not None else None

    portfolio_beta = round(sum(h["contrib_beta"] for h in holdings if h["contrib_beta"] is not None), 3)

    # 5. Sectors
    sectors = []
    for g, v in by_group.items():
        sectors.append({
            "group": g,
            "value": v,
            "weight_pct": round(v / total_value * 100, 2) if total_value else 0,
        })
    sectors.sort(key=lambda x: -x["weight_pct"])

    # 6. Warnings
    warnings = []
    if portfolio_beta is not None and portfolio_beta > 1.3:
        warnings.append(f"投组 Beta {portfolio_beta} 过高,大盘跌 10% 你会跌 ~{portfolio_beta * 10:.0f}%,考虑降杠杆")
    if sectors and sectors[0]["weight_pct"] >= 50:
        warnings.append(f"{sectors[0]['group']} 集中度 {sectors[0]['weight_pct']}%,单一板块风险过大")
    if len(holdings) < 5:
        warnings.append(f"仅 {len(holdings)} 档持股,分散不足,建议至少 5-8 档不同板块")

    # 7. Correlation matrix (60d, 取 top 8 by weight 避免太挤)
    top_holdings = sorted(holdings, key=lambda x: -x["weight_pct"])[:8]
    corr_codes = [h["code"] for h in top_holdings]
    corr_yfs = [h["yf"] for h in top_holdings]
    corr_matrix = []
    try:
        if not returns.empty and len(corr_yfs) >= 2:
            sub = returns[[c for c in corr_yfs if c in returns.columns]]
            if not sub.empty:
                m = sub.corr().round(2)
                # 对齐 corr_codes 顺序
                aligned = []
                code_to_yf = dict(zip(corr_codes, corr_yfs))
                for c1 in corr_codes:
                    row = []
                    yfc1 = code_to_yf[c1]
                    for c2 in corr_codes:
                        yfc2 = code_to_yf[c2]
                        if yfc1 in m.columns and yfc2 in m.index:
                            v = m.loc[yfc1, yfc2]
                            row.append(None if pd.isna(v) else float(v))
                        else:
                            row.append(None)
                    aligned.append(row)
                corr_matrix = aligned

                # 额外警示：高相关 pair
                for i in range(len(corr_codes)):
                    for j in range(i+1, len(corr_codes)):
                        v = corr_matrix[i][j]
                        if v is not None and v >= 0.85:
                            warnings.append(f"{corr_codes[i]} 与 {corr_codes[j]} 相关性 {v:.2f},是「假分散」")
    except Exception as e:
        print(f"[risk corr] {e}")

    out = {
        "holdings":       holdings,
        "sectors":        sectors,
        "portfolio_beta": portfolio_beta,
        "corr_codes":     corr_codes,
        "corr_matrix":    corr_matrix,
        "warnings":       warnings,
        "total_holdings": len(holdings),
        "total_value":    round(total_value, 2),
        "total_pnl":      round(total_pnl, 2),
    }
    cache_set(cache_key, out)
    return out


# ============================================================================
# 轧空候选扫描
# ============================================================================
@app.get("/api/short-squeeze")
def api_short_squeeze():
    """轧空候选：高 short ratio + 正 RS + 多头讯号。"""
    cache_key = "short_squeeze"
    cached = cache_get(cache_key)
    if cached:
        return cached

    out = []
    for code in load_watchlist():
        try:
            d = fetch_stock(code)
        except Exception:
            continue
        info = load_watchlist().get(code, {})
        yf_code = info.get("yf", code)
        try:
            t = yf.Ticker(yf_code)
            inf = t.info or {}
            short_ratio    = inf.get("shortRatio")            # Days to Cover
            short_pct      = inf.get("shortPercentOfFloat")   # 短仓占流通比
            shares_short   = inf.get("sharesShort")
            shares_prev    = inf.get("sharesShortPriorMonth")
            float_shares   = inf.get("floatShares")
            if short_ratio is None and short_pct is None:
                continue

            # 短仓变化 (+ = 卖空增加)
            short_chg_pct = None
            if shares_short and shares_prev:
                short_chg_pct = round((shares_short - shares_prev) / shares_prev * 100, 1)

            # 评分：DTC 高 + 讯号正向 + RS 强 → 轧空潜力大
            score = 0
            reasons = []
            if short_ratio is not None:
                if short_ratio >= 7:   score += 30; reasons.append(f"DTC {short_ratio:.1f} 天 (高)")
                elif short_ratio >= 5: score += 18; reasons.append(f"DTC {short_ratio:.1f} 天")
                elif short_ratio >= 3: score += 8;  reasons.append(f"DTC {short_ratio:.1f} 天")
            if short_pct is not None:
                spct = short_pct * 100 if short_pct < 1 else short_pct
                if spct >= 15:  score += 25; reasons.append(f"短仓占流通 {spct:.1f}%")
                elif spct >= 10: score += 15; reasons.append(f"短仓占流通 {spct:.1f}%")
                elif spct >= 5:  score += 6;  reasons.append(f"短仓占流通 {spct:.1f}%")
            if short_chg_pct is not None and short_chg_pct > 5:
                score += 10; reasons.append(f"短仓月增 +{short_chg_pct}%")

            # 动能加分
            sigs = d.get("signals", [])
            bull = sum(1 for s in sigs if s.get("color") == "red")
            if bull >= 2: score += 15; reasons.append(f"{bull} 多头讯号")
            elif bull == 1: score += 8

            chg_pct = (d["price"] - d["prev"]) / d["prev"] * 100 if d["prev"] else 0
            if chg_pct > 3: score += 8; reasons.append(f"今日 +{chg_pct:.1f}%")

            if "多头" in d.get("trend", ""):
                score += 5; reasons.append("多头趋势")

            out.append({
                "code":         code,
                "name":         d["name"],
                "group":        d.get("group", "—"),
                "price":        d["price"],
                "change_pct":   round(chg_pct, 2),
                "short_ratio":  round(short_ratio, 2) if short_ratio is not None else None,
                "short_pct":    round(short_pct * 100, 2) if (short_pct is not None and short_pct < 1) else short_pct,
                "short_chg_pct": short_chg_pct,
                "shares_short": int(shares_short) if shares_short else None,
                "float_shares": int(float_shares) if float_shares else None,
                "score":        score,
                "reasons":      reasons,
                "signals":      sigs,
            })
        except Exception as e:
            print(f"[short_squeeze] {code}: {e}")
            continue

    out.sort(key=lambda x: -x["score"])
    cache_set(cache_key, out)
    return out


# ============================================================================
# 盘前 / 盘后即时报价
# ============================================================================
@app.get("/api/quote/{code}")
def api_quote(code: str):
    """盘前/盘后即时价。短 cache 60 秒。"""
    cache_key = f"quote:{code}"
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, f"未追踪股票 {code}")
    yf_code = info.get("yf", code)
    try:
        t = yf.Ticker(yf_code)
        # fast_info 比 .info 快很多
        fi = t.fast_info
        regular = float(fi.get("last_price") or fi.get("lastPrice") or 0) or None
        prev_close = float(fi.get("previous_close") or fi.get("previousClose") or 0) or None

        # pre/post 用 .info (慢但较完整)
        inf = {}
        try:
            inf = t.info or {}
        except Exception:
            inf = {}

        def _get(d, *keys):
            for k in keys:
                v = d.get(k)
                if v is not None:
                    return v
            return None

        pre_price = _get(inf, "preMarketPrice")
        pre_chg   = _get(inf, "preMarketChange")
        pre_pct   = _get(inf, "preMarketChangePercent")
        pre_time  = _get(inf, "preMarketTime")

        post_price = _get(inf, "postMarketPrice")
        post_chg   = _get(inf, "postMarketChange")
        post_pct   = _get(inf, "postMarketChangePercent")
        post_time  = _get(inf, "postMarketTime")

        market_state = _get(inf, "marketState") or "UNKNOWN"

        out = {
            "code":          code,
            "yf":            yf_code,
            "regular":       regular,
            "prev_close":    prev_close,
            "market_state":  market_state,  # PRE / REGULAR / POST / POSTPOST / CLOSED
            "pre_market": {
                "price":  pre_price,
                "change": pre_chg,
                "pct":    pre_pct,
                "time":   pre_time,
            } if pre_price else None,
            "post_market": {
                "price":  post_price,
                "change": post_chg,
                "pct":    post_pct,
                "time":   post_time,
            } if post_price else None,
        }
        _cache[cache_key] = (time.time(), out)
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"quote fail: {e}")


# ============================================================================
# 财报日历 + EPS 惊奇纪录
# ============================================================================
@app.get("/api/earnings-calendar")
def api_earnings_calendar(days: int = 30):
    """未来 N 天 watchlist 财报日 + 近 4 季 EPS beat/miss。
    用 ThreadPoolExecutor 并行抓 yfinance,cache 6 小时 (财报一天变一次)。"""
    cache_key = f"earnings_cal:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    today = pd.Timestamp.today().normalize()

    def _fetch_one(code_info):
        code, info = code_info
        yf_code = info.get("yf", code)
        try:
            t = yf.Ticker(yf_code)
            next_date = None
            try:
                cal = t.calendar
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date") or cal.get("earnings_date")
                    if isinstance(ed, list) and ed:
                        next_date = pd.Timestamp(ed[0])
                    elif ed:
                        next_date = pd.Timestamp(ed)
                elif cal is not None and hasattr(cal, "loc"):
                    try:
                        ed = cal.loc["Earnings Date"]
                        next_date = pd.Timestamp(ed.iloc[0] if hasattr(ed, "iloc") else ed)
                    except Exception:
                        pass
            except Exception:
                pass

            history = []
            try:
                ed = t.earnings_dates
                if ed is not None and not ed.empty:
                    past = ed[ed.index.tz_localize(None) < pd.Timestamp.now()].head(4) if ed.index.tz is not None else ed[ed.index < pd.Timestamp.now()].head(4)
                    for idx, row in past.iterrows():
                        eps_est  = row.get("EPS Estimate")
                        eps_act  = row.get("Reported EPS")
                        surprise = row.get("Surprise(%)")
                        if pd.isna(eps_act) and pd.isna(eps_est):
                            continue
                        history.append({
                            "date":     str(idx.date()) if hasattr(idx, "date") else str(idx),
                            "estimate": float(eps_est) if not pd.isna(eps_est) else None,
                            "actual":   float(eps_act) if not pd.isna(eps_act) else None,
                            "surprise_pct": float(surprise) if not pd.isna(surprise) else None,
                        })
            except Exception:
                pass

            days_to = None
            if next_date is not None:
                try:
                    nd = next_date.tz_localize(None) if next_date.tz is not None else next_date
                    days_to = int((nd - today).days)
                except Exception:
                    days_to = None

            entry = {
                "code":         code,
                "name":         info.get("name", code),
                "yf":           yf_code,
                "earnings_date": str(next_date.date()) if next_date is not None else None,
                "days_to":      days_to,
                "history":      history,
            }
            if days_to is None or 0 <= days_to <= days or len(history) > 0:
                return entry
        except Exception as e:
            print(f"[earnings] {code}: {e}")
        return None

    from concurrent.futures import ThreadPoolExecutor
    out = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(_fetch_one, list(wl.items())):
            if r: out.append(r)

    # 排序：有日期的摆前面 by days_to，无日期的摆后面但有 history
    def _sort_key(x):
        d = x.get("days_to")
        if d is None: return (1, 9999)
        if d < 0:     return (2, abs(d))  # 已过去的排最后
        return (0, d)
    out.sort(key=_sort_key)

    cache_set_ttl(cache_key, out, 21600)  # 6 小时 — 财报日一天变一次
    return out


# ============================================================================
# 选择权情绪：P/C ratio、IV、Max Pain
# ============================================================================
@app.get("/api/options/{code}")
def api_options(code: str):
    """近月选择权情绪：总 call/put 量、P/C ratio、平均 IV、Max Pain。"""
    cache_key = f"options:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, "code not in watchlist")
    yf_code = info.get("yf", code)

    try:
        t = yf.Ticker(yf_code)
        expiries = list(t.options or [])
        if not expiries:
            raise HTTPException(503, "无选择权资料")

        # 近月（第一个到期日）
        nearest = expiries[0]
        ch = t.option_chain(nearest)
        calls = ch.calls
        puts  = ch.puts

        # 抓即时价当参考
        spot = None
        try:
            h = t.history(period="2d", auto_adjust=False)
            if not h.empty:
                spot = float(h.iloc[-1]["Close"])
        except Exception:
            pass

        call_vol = int(calls["volume"].fillna(0).sum())
        put_vol  = int(puts["volume"].fillna(0).sum())
        call_oi  = int(calls["openInterest"].fillna(0).sum())
        put_oi   = int(puts["openInterest"].fillna(0).sum())

        pc_vol = round(put_vol / call_vol, 2) if call_vol else None
        pc_oi  = round(put_oi  / call_oi, 2)  if call_oi  else None

        # 平均 IV (价量加权)
        def _wiv(df):
            if df.empty: return None
            iv = df["impliedVolatility"].fillna(0)
            w  = df["volume"].fillna(0)
            tot = w.sum()
            if tot == 0:
                return float(iv.mean()) if len(iv) > 0 else None
            return float((iv * w).sum() / tot)
        call_iv = _wiv(calls)
        put_iv  = _wiv(puts)

        # Max Pain: 找一个 strike，让所有 OI 的痛苦总和最小
        strikes = sorted(set(list(calls["strike"]) + list(puts["strike"])))
        max_pain = None
        if strikes:
            best = None
            for K in strikes:
                pain_c = ((K - calls["strike"]).clip(lower=0) * calls["openInterest"].fillna(0)).sum()
                pain_p = ((puts["strike"] - K).clip(lower=0) * puts["openInterest"].fillna(0)).sum()
                total = float(pain_c + pain_p)
                if best is None or total < best[1]:
                    best = (K, total)
            max_pain = float(best[0]) if best else None

        # 异常成交（vol > 3x OI 的合约）— 取最热前 5
        unusual = []
        for df, side in [(calls, "call"), (puts, "put")]:
            d = df.copy()
            d["volume"] = d["volume"].fillna(0)
            d["openInterest"] = d["openInterest"].fillna(0)
            d = d[(d["openInterest"] > 0) & (d["volume"] > 3 * d["openInterest"])]
            for _, row in d.nlargest(3, "volume").iterrows():
                unusual.append({
                    "side":   side,
                    "strike": float(row["strike"]),
                    "vol":    int(row["volume"]),
                    "oi":     int(row["openInterest"]),
                    "iv":     float(row["impliedVolatility"]) if not pd.isna(row["impliedVolatility"]) else None,
                })
        unusual.sort(key=lambda x: -x["vol"])
        unusual = unusual[:5]

        # 情绪结论
        sentiment = "neutral"
        if pc_vol is not None:
            if pc_vol < 0.7:    sentiment = "bullish"
            elif pc_vol > 1.3:  sentiment = "bearish"

        out = {
            "code":     code,
            "expiry":   nearest,
            "spot":     spot,
            "call_vol": call_vol, "put_vol": put_vol,
            "call_oi":  call_oi,  "put_oi":  put_oi,
            "pc_vol":   pc_vol,   "pc_oi":   pc_oi,
            "call_iv":  round(call_iv, 3) if call_iv else None,
            "put_iv":   round(put_iv,  3) if put_iv  else None,
            "max_pain": max_pain,
            "max_pain_diff_pct": round((max_pain - spot) / spot * 100, 2) if (max_pain and spot) else None,
            "unusual":  unusual,
            "sentiment": sentiment,
            "expiries": expiries[:6],
        }
        cache_set(cache_key, out)
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"options data fail: {e}")


# ============================================================================
# 总经 panel：VIX、10Y、DXY、Fed Funds
# ============================================================================
@app.get("/api/macro")
def api_macro():
    """总经背景：VIX 恐慌指数、10Y 公债、美元指数、Fed Funds proxy。"""
    cache_key = "macro:snapshot"
    cached = cache_get(cache_key)
    if cached:
        return cached

    targets = [
        ("VIX",  "^VIX",      "恐慌指数", "%",   {"lo": 15, "hi": 25}),
        ("10Y",  "^TNX",      "10年公债", "%",   {"lo": 3.5, "hi": 4.5}),
        ("DXY",  "DX-Y.NYB",  "美元指数", "",    {"lo": 100, "hi": 106}),
        ("2Y",   "^IRX",      "13周短率 (Fed Funds proxy)", "%", {"lo": 4.0, "hi": 5.0}),
    ]
    out = []
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=10)
    for code, yf_code, label, unit, band in targets:
        try:
            t = yf.Ticker(yf_code)
            h = t.history(start=start, end=end, auto_adjust=False)
            if h.empty or len(h) < 2:
                out.append({"code": code, "label": label, "value": None, "change": None,
                            "status": "—", "unit": unit})
                continue
            cur  = float(h.iloc[-1]["Close"])
            prev = float(h.iloc[-2]["Close"])
            chg  = round(cur - prev, 3)
            chg_pct = round((cur - prev) / prev * 100, 2) if prev else 0

            # 状态判断
            status = "neutral"
            if code == "VIX":
                if cur > band["hi"]: status = "danger"
                elif cur < band["lo"]: status = "calm"
            elif code in ("10Y", "2Y"):
                if cur > band["hi"]: status = "high"
                elif cur < band["lo"]: status = "low"
            elif code == "DXY":
                if cur > band["hi"]: status = "strong"
                elif cur < band["lo"]: status = "weak"

            out.append({
                "code":     code,
                "yf":       yf_code,
                "label":    label,
                "value":    round(cur, 3),
                "prev":     round(prev, 3),
                "change":   chg,
                "change_pct": chg_pct,
                "unit":     unit,
                "status":   status,
                "band":     band,
            })
        except Exception as e:
            print(f"[macro] {code}: {e}")
            out.append({"code": code, "label": label, "value": None, "status": "—", "unit": unit})

    cache_set(cache_key, out)
    return out


@app.get("/")
def root():
    return FileResponse(ROOT / "jaminindex.html")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, io, os
    if sys.stdout is None:
        # pythonw.exe 背景模式下 stdout/stderr 为 None，重导到 log 档避免 print/reconfigure 崩溃
        _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
        sys.stdout = open(_log_path, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    import uvicorn
    print("=" * 64)
    print("[US Stock Intel v1]  http://localhost:18506/")
    print("    GET  /api/stocks                    清单摘要")
    print("    GET  /api/stock/{code}?period=D|W|M  详细")
    print("    GET  /api/news/{code}                新闻")
    print("    GET  /api/groups                     族群")
    print("    GET  /api/ranking?by=change|volume|fi|rsi 热度榜")
    print("    POST /api/watchlist                  新增")
    print("    DEL  /api/watchlist/{code}           移除")
    print("    POST /api/alerts/{code}              设警示")
    print("    POST /api/telegram                  设飞书 webhook")
    print("    GET  /api/refresh                    清快取")
    print("=" * 64)
    threading.Thread(target=alert_worker, daemon=True).start()
    uvicorn.run("jaminserver:app", host="0.0.0.0", port=18506, reload=False)
