"""
datasource.py — 统一数据访问层
========================================
OHLCV 历史数据  : 本地 MySQL 数据库 us-stock-intel
其余所有数据    : iFinD (同花顺 THS_iFinDPy SDK)

MySQL 表结构（最小要求）:
    CREATE TABLE ohlcv (
        id       BIGINT AUTO_INCREMENT PRIMARY KEY,
        symbol   VARCHAR(16) NOT NULL COMMENT '股票代码, 如 AAPL',
        period   CHAR(1)     NOT NULL COMMENT 'D/W/M',
        dt       DATE        NOT NULL,
        open     DOUBLE,
        high     DOUBLE,
        low      DOUBLE,
        close    DOUBLE,
        volume   BIGINT,
        UNIQUE KEY uq_sym_prd_dt (symbol, period, dt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

iFinD 美股代码后缀规则（可在 _to_ifind_code 中扩充）:
    纳斯达克  → AAPL.O
    纽交所    → JPM.N
    大盘/波动  → SPX.GI / VIX.GI / TNX.GI / DXY.GI
    ADR       → TSM.N
"""
from __future__ import annotations

import os
import warnings
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text

# ============================================================================
# MySQL 配置（优先读环境变量）
# ============================================================================
_MYSQL_USER = os.environ.get("MYSQL_USER", "root")
_MYSQL_PWD  = os.environ.get("MYSQL_PWD", "")
_MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
_MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
_MYSQL_DB   = os.environ.get("MYSQL_DB", "us-stock-intel")

# 注意: 库名含连字符，需用 backtick 引用（在 DDL 里），connect_args 里指定 db
_engine = None

def _get_engine():
    global _engine
    if _engine is None:
        # pymysql 对含特殊字符库名的处理: 在 url 中用 %60 不可靠, 改用 connect_args
        _engine = create_engine(
            f"mysql+pymysql://{_MYSQL_USER}:{_MYSQL_PWD}@{_MYSQL_HOST}:{_MYSQL_PORT}/",
            connect_args={"db": _MYSQL_DB, "charset": "utf8mb4"},
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine

# ============================================================================
# 1. OHLCV — 从 MySQL 获取历史 K 线
# ============================================================================

def get_ohlcv(
    symbol: str,
    period: str = "D",
    start: Optional[str] = None,
    end:   Optional[str] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """
    从 MySQL us-stock-intel 读取 OHLCV 历史数据。
    返回 DataFrame，index = DatetimeIndex（列名对齐 yfinance: Open/High/Low/Close/Volume），
    方便上层技术指标代码无需改动。

    Parameters
    ----------
    symbol : 股票代码（不含后缀，如 'AAPL'）
    period : 'D'=日线  'W'=周线  'M'=月线
    start  : '2024-01-01' 开始日期（含）
    end    : '2024-12-31' 结束日期（含）
    limit  : 若指定，只取最近 limit 根 K 线
    """
    period = period.upper()
    engine = _get_engine()

    where = "WHERE symbol = :sym AND period = :prd"
    params: dict[str, Any] = {"sym": symbol.upper(), "prd": period}

    if start:
        where += " AND dt >= :start"
        params["start"] = str(start)
    if end:
        where += " AND dt <= :end"
        params["end"] = str(end)

    if limit:
        # 取最近 limit 根：先倒序 LIMIT，再正序返回
        sql = text(f"""
            SELECT * FROM (
                SELECT dt,
                       open  AS Open,
                       high  AS High,
                       low   AS Low,
                       close AS Close,
                       volume AS Volume
                FROM ohlcv
                {where}
                ORDER BY dt DESC
                LIMIT :lim
            ) t
            ORDER BY t.dt ASC
        """)
        params["lim"] = int(limit)
    else:
        sql = text(f"""
            SELECT dt,
                   open  AS Open,
                   high  AS High,
                   low   AS Low,
                   close AS Close,
                   volume AS Volume
            FROM ohlcv
            {where}
            ORDER BY dt ASC
        """)

    try:
        df = pd.read_sql(sql, engine, params=params, parse_dates=["dt"])
    except Exception as e:
        warnings.warn(f"[datasource.get_ohlcv] MySQL query failed for {symbol}/{period}: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    df = df.set_index("dt")
    df.index.name = "Date"
    df.index = pd.DatetimeIndex(df.index)
    # 确保数值类型
    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype(int)
    return df


def download_ohlcv(
    symbols: list[str],
    period: str = "D",
    start: Optional[str] = None,
    end:   Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, pd.DataFrame]:
    """
    批量获取多只股票 OHLCV（替代 yf.download）。
    返回 {symbol: DataFrame}，缺失的股票不出现在字典里。
    """
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = get_ohlcv(sym, period=period, start=start, end=end, limit=limit)
        if not df.empty:
            out[sym] = df
    return out


def download_ohlcv_panel(
    symbols: list[str],
    period: str = "D",
    start: Optional[str] = None,
    end:   Optional[str] = None,
) -> pd.DataFrame:
    """
    返回 Panel 形式的收盘价矩阵：DataFrame，列=symbol，index=日期。
    （替代 yf.download 后提取 ['Close']）
    """
    data = download_ohlcv(symbols, period=period, start=start, end=end)
    if not data:
        return pd.DataFrame()
    closes = {sym: df["Close"] for sym, df in data.items()}
    df_panel = pd.DataFrame(closes)
    df_panel = df_panel.dropna(how="all")
    return df_panel


# ============================================================================
# 2. iFinD — 其余所有数据
# ============================================================================

_IFIND_LOGGED = False


def _ensure_ifind() -> None:
    """确保 iFinD 已登录（单次登录，进程生命周期内复用）。"""
    global _IFIND_LOGGED
    if _IFIND_LOGGED:
        return
    uid = os.environ.get("IFIND_USER", "")
    pwd = os.environ.get("IFIND_PWD", "")
    if not uid or not pwd:
        raise RuntimeError(
            "iFinD 账号未配置。请设置环境变量 IFIND_USER 和 IFIND_PWD，"
            "或在启动脚本中 os.environ 赋值。"
        )
    try:
        from iFinDPy import THS_iFinDLogin
        ret = THS_iFinDLogin(uid, pwd)
        if str(ret) not in ("0", ""):
            raise RuntimeError(f"iFinD 登录返回: {ret}")
        _IFIND_LOGGED = True
        print(f"[iFinD] 登录成功 uid={uid}")
    except ImportError:
        raise ImportError(
            "未安装 iFinDPy。请按同花顺官方文档安装 iFinD Python SDK。"
        )


def _to_ifind_code(symbol: str) -> str:
    """
    把 yfinance 格式代码转换为 iFinD 格式代码。
    规则：
      - 指数/宏观指标映射表优先
      - 已带后缀（含 '.'）直接返回
      - 默认纳斯达克 .O（纽交所股票请在映射表中补充）
    """
    _MAP: dict[str, str] = {
        "^GSPC":    "SPX.GI",
        "^VIX":     "VIX.GI",
        "^TNX":     "TNX.GI",
        "^IRX":     "IRX.GI",
        "DX-Y.NYB": "DXY.GI",
        "SPY":      "SPY.P",
        "RSP":      "RSP.P",
        "QQQ":      "QQQ.O",
        "IVV":      "IVV.P",
        "VTI":      "VTI.P",
        # NYSE 主要股票
        "JPM":  "JPM.N",
        "TSM":  "TSM.N",
        "AVGO": "AVGO.O",
        "ORCL": "ORCL.N",
        "CEG":  "CEG.N",
        "ETN":  "ETN.N",
        "VST":  "VST.N",
        "ANET": "ANET.N",
        "SMCI": "SMCI.O",
        "ARM":  "ARM.O",
    }
    if symbol in _MAP:
        return _MAP[symbol]
    if "." in symbol:
        return symbol
    return f"{symbol}.O"  # 默认纳斯达克


def _ifind_result_to_df(r) -> pd.DataFrame:
    """
    把 iFinD SDK 返回对象统一转为 DataFrame。
    SDK 不同接口返回结构略有差异，此函数做兼容处理。
    """
    if r is None:
        return pd.DataFrame()
    # 部分接口返回 dict，部分返回自定义对象
    if hasattr(r, "to_frame"):
        return r.to_frame()
    if isinstance(r, dict):
        ec = r.get("errorcode", r.get("errorCode", -1))
        if str(ec) != "0":
            return pd.DataFrame()
        tables = r.get("tables", r.get("data", []))
        if isinstance(tables, list) and tables:
            return pd.DataFrame(tables)
        if isinstance(tables, dict):
            return pd.DataFrame([tables])
    if isinstance(r, pd.DataFrame):
        return r
    return pd.DataFrame()


def ifind_realtime(symbol: str) -> dict:
    """
    获取实时行情快照（替代 yfinance fast_info / .info 盘前后报价）。
    返回含 latest/preClose/open/high/low/volume 的 dict。
    """
    _ensure_ifind()
    from iFinDPy import THS_RealtimeQuotes
    code = _to_ifind_code(symbol)
    indicators = "latest,preClose,open,high,low,volume,preMarketPrice,postMarketPrice,marketState"
    try:
        r = THS_RealtimeQuotes(code, indicators, "")
        df = _ifind_result_to_df(r)
        return df.iloc[0].to_dict() if not df.empty else {}
    except Exception as e:
        print(f"[iFinD realtime] {symbol}: {e}")
        return {}


def ifind_basic(
    symbol: str,
    indicators: str,
    params: str = "",
    report_type: str = "",
) -> pd.DataFrame:
    """
    基础/衍生数据接口（替代 yfinance .info 估值/财务字段）。
    indicators: iFinD 指标字符串，多个用逗号分隔。
    常用指标示例:
        估值: ths_pe_ttm_stock,ths_pb_stock,ths_ps_stock,ths_peg_stock
        成长: ths_revenue_growth_stock,ths_eps_growth_stock
        规模: ths_market_cap_stock
        评等: ths_analyst_rating_stock（参考）
    """
    _ensure_ifind()
    from iFinDPy import THS_BasicData
    code = _to_ifind_code(symbol)
    try:
        r = THS_BasicData(code, indicators, params)
        return _ifind_result_to_df(r)
    except Exception as e:
        print(f"[iFinD basic] {symbol} / {indicators}: {e}")
        return pd.DataFrame()


def ifind_date_serial(
    symbol: str,
    indicators: str,
    start_date: str,
    end_date: str,
    params: str = "",
) -> pd.DataFrame:
    """
    时序数据接口（替代 yfinance quarterly_income_stmt 等）。
    适用于季度财报、营收、EPS 等时间序列。
    """
    _ensure_ifind()
    from iFinDPy import THS_DateSerial
    code = _to_ifind_code(symbol)
    try:
        r = THS_DateSerial(code, indicators, params, "Days:Tradedays", start_date, end_date)
        return _ifind_result_to_df(r)
    except Exception as e:
        print(f"[iFinD date_serial] {symbol} / {indicators}: {e}")
        return pd.DataFrame()


def ifind_news(symbol: str, count: int = 12) -> list[dict]:
    """
    获取个股新闻（替代 yf.Ticker().news）。
    返回 list[dict]，每条含 title/link/time/publisher。
    """
    _ensure_ifind()
    try:
        from iFinDPy import THS_DataPool
        code = _to_ifind_code(symbol)
        r = THS_DataPool("news", f"ths_code={code};ths_news_num={count}", "")
        df = _ifind_result_to_df(r)
        if df.empty:
            return []
        out = []
        for _, row in df.iterrows():
            out.append({
                "title":     str(row.get("title", row.get("ths_news_title", ""))),
                "publisher": str(row.get("source", row.get("ths_news_source", ""))),
                "link":      str(row.get("url", row.get("ths_news_url", ""))),
                "time":      int(pd.Timestamp(
                                 row.get("publishTime", row.get("ths_publish_time", 0))
                             ).timestamp()) if row.get("publishTime") or row.get("ths_publish_time") else 0,
            })
        return out[:count]
    except Exception as e:
        print(f"[iFinD news] {symbol}: {e}")
        return []


def ifind_analyst_rating(symbol: str, months: int = 10) -> dict:
    """
    分析师评级（替代 yf.Ticker().recommendations）。
    返回对齐原 institutional 格式的 dict:
      { dates, fi(strongBuy), it(buy), dealer(hold), neg(sell) }
    """
    _ensure_ifind()
    try:
        code = _to_ifind_code(symbol)
        # ths_analyst_suggest_stock 返回近期评级变化
        df = ifind_basic(
            symbol,
            "ths_analyst_buy_stock,ths_analyst_overweight_stock,"
            "ths_analyst_neutral_stock,ths_analyst_underweight_stock,ths_analyst_sell_stock",
        )
        if df.empty:
            return {"dates": [], "fi": [], "it": [], "dealer": [], "neg": [],
                    "_legend": {"fi": "Strong Buy", "it": "Buy", "dealer": "Hold", "neg": "Sell"}}
        # 取最近 months 行
        df = df.tail(months)
        labels = [str(i) for i in range(len(df))]
        sb  = [int(v) if not pd.isna(v) else 0 for v in df.get("ths_analyst_buy_stock", [0]*len(df))]
        b   = [int(v) if not pd.isna(v) else 0 for v in df.get("ths_analyst_overweight_stock", [0]*len(df))]
        h   = [int(v) if not pd.isna(v) else 0 for v in df.get("ths_analyst_neutral_stock", [0]*len(df))]
        sl  = [int(v) if not pd.isna(v) else 0 for v in df.get("ths_analyst_underweight_stock", [0]*len(df))]
        ssl = [int(v) if not pd.isna(v) else 0 for v in df.get("ths_analyst_sell_stock", [0]*len(df))]
        neg = [-(s + ss) for s, ss in zip(sl, ssl)]
        return {
            "dates":   labels,
            "fi":      sb,
            "it":      b,
            "dealer":  h,
            "neg":     neg,
            "_legend": {"fi": "Strong Buy", "it": "Buy", "dealer": "Hold", "neg": "Sell"},
        }
    except Exception as e:
        print(f"[iFinD analyst_rating] {symbol}: {e}")
        return {"dates": [], "fi": [], "it": [], "dealer": [], "neg": [],
                "_legend": {"fi": "Strong Buy", "it": "Buy", "dealer": "Hold", "neg": "Sell"}}


def ifind_insider_transactions(symbol: str, days: int = 180) -> pd.DataFrame:
    """
    内部人交易（替代 yf.Ticker().insider_transactions）。
    返回含 Start Date/Insider/Position/Text/Value/Shares 列的 DataFrame。
    """
    _ensure_ifind()
    try:
        from iFinDPy import THS_DataPool
        code = _to_ifind_code(symbol)
        r = THS_DataPool("insiderTrade",
                         f"ths_code={code};ths_days={days}", "")
        df = _ifind_result_to_df(r)
        # 统一列名到原 server.py 期望的格式
        rename_map = {
            "transDate": "Start Date",
            "insiderName": "Insider",
            "position": "Position",
            "transType": "Text",
            "transValue": "Value",
            "transShares": "Shares",
        }
        df.rename(columns=rename_map, inplace=True)
        return df
    except Exception as e:
        print(f"[iFinD insider] {symbol}: {e}")
        return pd.DataFrame()


def ifind_institutional_holders(symbol: str) -> dict:
    """
    机构持股（替代 yf.Ticker().institutional_holders / major_holders）。
    返回 {institutional: list, mutualfund: list, summary: dict}。
    """
    _ensure_ifind()
    try:
        code = _to_ifind_code(symbol)
        # 机构持股明细
        df_inst = ifind_basic(
            symbol,
            "ths_top_holder_name_stock,ths_top_holder_shares_stock,"
            "ths_top_holder_ratio_stock,ths_top_holder_market_value_stock",
        )
        # 整体持股比例
        df_major = ifind_basic(
            symbol,
            "ths_institutional_holdings_ratio_stock,"
            "ths_insider_holdings_ratio_stock,"
            "ths_institutional_holder_num_stock",
        )

        inst_list = []
        if not df_inst.empty:
            for _, row in df_inst.head(10).iterrows():
                inst_list.append({
                    "holder":      str(row.get("ths_top_holder_name_stock", "")),
                    "shares":      int(float(row.get("ths_top_holder_shares_stock", 0) or 0)),
                    "value":       int(float(row.get("ths_top_holder_market_value_stock", 0) or 0)),
                    "pct_held":    round(float(row.get("ths_top_holder_ratio_stock", 0) or 0), 2),
                    "pct_change":  0.0,
                    "date":        "",
                })

        pct_inst    = 0.0
        pct_insider = 0.0
        n_inst      = 0
        if not df_major.empty:
            row = df_major.iloc[0]
            pct_inst    = float(row.get("ths_institutional_holdings_ratio_stock", 0) or 0)
            pct_insider = float(row.get("ths_insider_holdings_ratio_stock", 0) or 0)
            n_inst      = int(float(row.get("ths_institutional_holder_num_stock", 0) or 0))

        top10_pct = sum(h["pct_held"] for h in inst_list)
        return {
            "institutional": inst_list,
            "mutualfund":    [],
            "summary": {
                "pct_insider":       round(pct_insider, 2),
                "pct_institutions":  round(pct_inst, 2),
                "top10_pct":         round(top10_pct, 2),
                "n_institutions":    n_inst,
                "n_top_holders":     len(inst_list),
            }
        }
    except Exception as e:
        print(f"[iFinD inst_holders] {symbol}: {e}")
        return {
            "institutional": [], "mutualfund": [],
            "summary": {"pct_insider": 0, "pct_institutions": 0,
                        "top10_pct": 0, "n_institutions": 0, "n_top_holders": 0}
        }


def ifind_valuation(symbol: str) -> dict:
    """
    估值指标（替代 yf.Ticker().info P/E / PEG / P/B 等）。
    """
    _ensure_ifind()
    indicators = (
        "ths_pe_ttm_stock,ths_pe_lyr_stock,ths_pb_stock,"
        "ths_ps_ttm_stock,ths_peg_stock,ths_ev_ebitda_stock,"
        "ths_dividend_yield_stock,ths_profit_margin_stock,"
        "ths_roe_stock,ths_revenue_growth_stock,ths_eps_growth_stock,"
        "ths_beta_stock,ths_market_cap_stock"
    )
    df = ifind_basic(symbol, indicators)
    if df.empty:
        return {}
    row = df.iloc[0]

    def num(k):
        v = row.get(k)
        if v is None:
            return None
        try:
            f = float(v)
            return None if (f != f) else f  # NaN check
        except (TypeError, ValueError):
            return None

    return {
        "trailing_pe":   num("ths_pe_ttm_stock"),
        "forward_pe":    num("ths_pe_lyr_stock"),
        "price_book":    num("ths_pb_stock"),
        "price_sales":   num("ths_ps_ttm_stock"),
        "peg":           num("ths_peg_stock"),
        "ev_ebitda":     num("ths_ev_ebitda_stock"),
        "div_yield":     num("ths_dividend_yield_stock"),
        "profit_margin": num("ths_profit_margin_stock"),
        "roe":           num("ths_roe_stock"),
        "revenue_growth":num("ths_revenue_growth_stock"),
        "earnings_growth":num("ths_eps_growth_stock"),
        "beta":          num("ths_beta_stock"),
        "market_cap":    num("ths_market_cap_stock"),
    }


def ifind_quarterly_financials(symbol: str) -> tuple[list[dict], list[dict]]:
    """
    季度营收 + EPS（替代 yf.Ticker().quarterly_income_stmt）。
    返回 (revenue_list, eps_list)，格式对齐原 api_fundamentals。
    """
    _ensure_ifind()
    import datetime
    end_date   = datetime.date.today().strftime("%Y-%m-%d")
    start_date = (datetime.date.today().replace(year=datetime.date.today().year - 3)).strftime("%Y-%m-%d")

    df_rev = ifind_date_serial(
        symbol,
        "ths_total_operating_revenue_q_stock",  # 单季营收
        start_date, end_date, "period:Q"
    )
    df_eps = ifind_date_serial(
        symbol,
        "ths_eps_q_stock",  # 单季EPS
        start_date, end_date, "period:Q"
    )

    revenue: list[dict] = []
    if not df_rev.empty:
        vals = df_rev["ths_total_operating_revenue_q_stock"].tolist() if "ths_total_operating_revenue_q_stock" in df_rev.columns else []
        prev_vals: list[float] = []
        for i, (idx, row) in enumerate(df_rev.iterrows()):
            v = row.get("ths_total_operating_revenue_q_stock")
            if v is None or (isinstance(v, float) and v != v):
                continue
            v = float(v)
            yoy = None
            if i >= 4 and len(prev_vals) >= 4 and prev_vals[i - 4] > 0:
                yoy = round((v - prev_vals[i - 4]) / prev_vals[i - 4] * 100, 1)
            prev_vals.append(v)
            try:
                dt = pd.Timestamp(str(idx))
                q = (dt.month - 1) // 3 + 1
                ym = f"{dt.year}/Q{q}"
            except Exception:
                ym = str(idx)
            revenue.append({"ym": ym, "revenue": int(v), "yoy": yoy})
        revenue = revenue[-12:]

    eps: list[dict] = []
    if not df_eps.empty:
        for idx, row in df_eps.iterrows():
            v = row.get("ths_eps_q_stock")
            if v is None or (isinstance(v, float) and v != v):
                continue
            try:
                dt = pd.Timestamp(str(idx))
                date_str = dt.strftime("%Y-%m-%d")
            except Exception:
                date_str = str(idx)
            eps.append({"date": date_str, "value": round(float(v), 2)})
        eps = eps[-12:]

    return revenue, eps


def ifind_earnings_calendar(symbol: str) -> dict:
    """
    财报日历（替代 yf.Ticker().calendar / .earnings_dates）。
    返回 {"earnings_date": "2025-01-30", "history": [...]}。
    """
    _ensure_ifind()
    try:
        df_next = ifind_basic(symbol, "ths_next_report_date_stock")
        df_hist = ifind_date_serial(
            symbol,
            "ths_eps_q_stock,ths_eps_estimate_stock,ths_eps_surprise_ratio_stock",
            "2022-01-01",
            pd.Timestamp.today().strftime("%Y-%m-%d"),
            "period:Q",
        )

        next_date = None
        if not df_next.empty:
            v = df_next.iloc[0].get("ths_next_report_date_stock")
            if v:
                try:
                    next_date = str(pd.Timestamp(str(v)).date())
                except Exception:
                    pass

        history: list[dict] = []
        if not df_hist.empty:
            for idx, row in df_hist.tail(4).iterrows():
                est  = row.get("ths_eps_estimate_stock")
                act  = row.get("ths_eps_q_stock")
                surp = row.get("ths_eps_surprise_ratio_stock")
                if (est is None or (isinstance(est, float) and est != est)) and \
                   (act  is None or (isinstance(act, float) and act != act)):
                    continue
                history.append({
                    "date":         str(pd.Timestamp(str(idx)).date()),
                    "estimate":     float(est) if est and not (isinstance(est, float) and est != est) else None,
                    "actual":       float(act) if act and not (isinstance(act, float) and act != act) else None,
                    "surprise_pct": float(surp) if surp and not (isinstance(surp, float) and surp != surp) else None,
                })

        return {"earnings_date": next_date, "history": history}
    except Exception as e:
        print(f"[iFinD earnings_calendar] {symbol}: {e}")
        return {"earnings_date": None, "history": []}


def ifind_short_data(symbol: str) -> dict:
    """
    做空数据（替代 yf.Ticker().info shortRatio / shortPercentOfFloat 等）。
    """
    _ensure_ifind()
    df = ifind_basic(
        symbol,
        "ths_short_interest_ratio_stock,ths_short_percent_float_stock,"
        "ths_shares_short_stock,ths_shares_short_prior_month_stock,"
        "ths_float_shares_stock",
    )
    if df.empty:
        return {}
    row = df.iloc[0]
    def num(k):
        v = row.get(k)
        try: return float(v) if v is not None else None
        except: return None
    return {
        "shortRatio":           num("ths_short_interest_ratio_stock"),
        "shortPercentOfFloat":  num("ths_short_percent_float_stock"),
        "sharesShort":          num("ths_shares_short_stock"),
        "sharesShortPriorMonth":num("ths_shares_short_prior_month_stock"),
        "floatShares":          num("ths_float_shares_stock"),
    }


def ifind_options(symbol: str, expiry: str = "") -> dict:
    """
    期权链（替代 yf.Ticker().option_chain）。
    返回 {calls: DataFrame, puts: DataFrame, expiries: list}。
    如 iFinD 账号不含期权权限，返回 {"error": "..."}。
    """
    _ensure_ifind()
    try:
        from iFinDPy import THS_DataPool
        code = _to_ifind_code(symbol)
        params_str = f"ths_code={code}"
        if expiry:
            params_str += f";ths_expiry_date={expiry}"
        r_call = THS_DataPool("optionChainCall", params_str, "")
        r_put  = THS_DataPool("optionChainPut",  params_str, "")
        calls = _ifind_result_to_df(r_call)
        puts  = _ifind_result_to_df(r_put)
        # 列名对齐 yfinance option_chain 格式
        col_map = {
            "strike":           "strike",
            "ths_strike":       "strike",
            "volume":           "volume",
            "ths_volume":       "volume",
            "openInterest":     "openInterest",
            "ths_open_interest":"openInterest",
            "impliedVolatility":"impliedVolatility",
            "ths_iv":           "impliedVolatility",
        }
        calls.rename(columns={k: v for k, v in col_map.items() if k in calls.columns}, inplace=True)
        puts.rename( columns={k: v for k, v in col_map.items() if k in puts.columns},  inplace=True)
        return {"calls": calls, "puts": puts, "expiries": [expiry] if expiry else []}
    except Exception as e:
        print(f"[iFinD options] {symbol}: {e}")
        return {"error": str(e), "calls": pd.DataFrame(), "puts": pd.DataFrame(), "expiries": []}


def ifind_macro_snapshot() -> list[dict]:
    """
    宏观快照（替代 yf.Ticker('^VIX'/'^TNX'/...).history）。
    返回 [{code, label, value, prev, change, change_pct, unit, status, band}]。
    """
    _ensure_ifind()
    targets = [
        ("VIX",  "^VIX",     "恐慌指数",              "%",  {"lo": 15, "hi": 25}),
        ("10Y",  "^TNX",     "10年公债",              "%",  {"lo": 3.5, "hi": 4.5}),
        ("DXY",  "DX-Y.NYB", "美元指数",              "",   {"lo": 100, "hi": 106}),
        ("2Y",   "^IRX",     "13周短率(Fed Funds proxy)","%",{"lo": 4.0, "hi": 5.0}),
    ]
    out = []
    for code, sym, label, unit, band in targets:
        try:
            snap = ifind_realtime(sym)
            cur  = float(snap.get("latest", snap.get("close", 0)) or 0)
            prev = float(snap.get("preClose", 0) or 0)
            chg      = round(cur - prev, 3)
            chg_pct  = round((cur - prev) / prev * 100, 2) if prev else 0
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
                "code": code, "yf": sym, "label": label,
                "value": round(cur, 3), "prev": round(prev, 3),
                "change": chg, "change_pct": chg_pct,
                "unit": unit, "status": status, "band": band,
            })
        except Exception as e:
            print(f"[iFinD macro] {code}: {e}")
            out.append({"code": code, "label": label, "value": None,
                        "status": "—", "unit": unit})
    return out
