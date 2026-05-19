"""
缠论分析模块 - 基础笔段划分算法
实现缠论的核心概念：分型、笔、线段
"""
import pandas as pd
import numpy as np
from config import CHAN_MIN_BARS
"""
增强版 K 线图：K 线 + 成交量 + MA 叠加 + MACD/KDJ/RSI 子图
"""
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from config import COLORS


def plot_full_chart(
        df: pd.DataFrame,
        title: str = "",
        show_ma: bool = True,
        show_volume: bool = True,
        show_macd: bool = False,
        show_kdj: bool = False,
        show_rsi: bool = False,
        ma_periods: tuple = (5, 20, 60),
) -> go.Figure:
    """
    构造一张多面板 K 线图。

    Args:
        df: 已经跑过 calc_all_indicators 的 DataFrame，索引为日期
        title: 图表标题
        show_ma/volume/macd/kdj/rsi: 各指标开关
        ma_periods: 要叠加在 K 线主图上的均线周期
    """
    if df is None or df.empty:
        return go.Figure().update_layout(title=f"{title} - 无数据")

    # ===== 1. 按开关计算子图数量与高度 =====
    panels = [("K线", 0.50)]  # 主图始终存在
    if show_volume: panels.append(("成交量", 0.15))
    if show_macd:   panels.append(("MACD", 0.13))
    if show_kdj:    panels.append(("KDJ", 0.12))
    if show_rsi:    panels.append(("RSI", 0.10))

    # 高度按比例归一化
    total_h = sum(h for _, h in panels)
    row_heights = [h / total_h for _, h in panels]
    panel_titles = [name for name, _ in panels]

    fig = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=panel_titles,
    )

    # ===== 2. 主图：K 线 =====
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing=dict(line=dict(color=COLORS["rise"]), fillcolor=COLORS["rise"]),
            decreasing=dict(line=dict(color=COLORS["fall"]), fillcolor=COLORS["fall"]),
            name="K线",
            showlegend=False,
        ),
        row=1, col=1,
    )

    # 均线叠加在主图
    if show_ma:
        ma_colors = ["#f59e0b", "#8b5cf6", "#3b82f6", "#10b981", "#ef4444"]
        for i, p in enumerate(ma_periods):
            col = f"MA{p}"
            if col in df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=df.index, y=df[col],
                        name=f"MA{p}",
                        mode="lines",
                        line=dict(color=ma_colors[i % len(ma_colors)], width=1.2),
                    ),
                    row=1, col=1,
                )

    # ===== 3. 成交量子图（红涨绿跌着色）=====
    cur_row = 2
    if show_volume:
        vol_colors = [
            COLORS["rise"] if c >= o else COLORS["fall"]
            for c, o in zip(df["close"], df["open"])
        ]
        fig.add_trace(
            go.Bar(
                x=df.index, y=df["volume"],
                marker_color=vol_colors,
                name="成交量",
                showlegend=False,
            ),
            row=cur_row, col=1,
        )
        cur_row += 1

    # ===== 4. MACD 子图（DIF 线 + DEA 线 + HIST 柱） =====
    if show_macd and "MACD_DIF" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["MACD_DIF"], name="DIF",
                       line=dict(color="#3b82f6", width=1.2)),
            row=cur_row, col=1,
        )
        fig.add_trace(
            go.Scatter(x=df.index, y=df["MACD_SIGNAL"], name="DEA",
                       line=dict(color="#f59e0b", width=1.2)),
            row=cur_row, col=1,
        )
        hist_colors = [
            COLORS["rise"] if v >= 0 else COLORS["fall"]
            for v in df["MACD_HIST"].fillna(0)
        ]
        fig.add_trace(
            go.Bar(x=df.index, y=df["MACD_HIST"], name="HIST",
                   marker_color=hist_colors, showlegend=False),
            row=cur_row, col=1,
        )
        cur_row += 1

    # ===== 5. KDJ 子图 =====
    if show_kdj and "K" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["K"], name="K",
                       line=dict(color="#3b82f6", width=1.2)),
            row=cur_row, col=1,
        )
        fig.add_trace(
            go.Scatter(x=df.index, y=df["D"], name="D",
                       line=dict(color="#f59e0b", width=1.2)),
            row=cur_row, col=1,
        )
        fig.add_trace(
            go.Scatter(x=df.index, y=df["J"], name="J",
                       line=dict(color="#a855f7", width=1.0)),
            row=cur_row, col=1,
        )
        # 80 / 20 超买超卖参考线
        fig.add_hline(y=80, line=dict(color="#ef4444", width=0.6, dash="dot"),
                      row=cur_row, col=1)
        fig.add_hline(y=20, line=dict(color="#10b981", width=0.6, dash="dot"),
                      row=cur_row, col=1)
        cur_row += 1

    # ===== 6. RSI 子图 =====
    if show_rsi and "RSI" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["RSI"], name="RSI(14)",
                       line=dict(color="#8b5cf6", width=1.2)),
            row=cur_row, col=1,
        )
        fig.add_hline(y=70, line=dict(color="#ef4444", width=0.6, dash="dot"),
                      row=cur_row, col=1)
        fig.add_hline(y=30, line=dict(color="#10b981", width=0.6, dash="dot"),
                      row=cur_row, col=1)
        fig.add_hline(y=50, line=dict(color="#94a3b8", width=0.4, dash="dot"),
                      row=cur_row, col=1)
        cur_row += 1

    # ===== 7. 全局样式 =====
    fig.update_layout(
        title=title,
        template="plotly_white",
        height=240 * len(panels) + 80,
        hovermode="x unified",
        xaxis_rangeslider_visible=False,  # 隐藏 Plotly 自带的小型缩略条
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=60, b=40),
    )
    # 让所有子图共享 X 轴并显示日期格式
    fig.update_xaxes(showgrid=True, gridcolor="#e2e8f0")
    fig.update_yaxes(showgrid=True, gridcolor="#e2e8f0")
    return fig


def find_fractals(df: pd.DataFrame) -> pd.DataFrame:
    """
    识别顶分型和底分型

    缠论定义：
    - 顶分型：第二根K线的高点最高，且三根K线的高点呈"中间高两边低"
    - 底分型：第二根K线的低点最低，且三根K线的低点呈"中间低两边高"
    """
    df = df.copy()
    df["fractal_type"] = 0

    for i in range(1, len(df) - 1):
        # 顶分型判断
        if (df["high"].iloc[i] > df["high"].iloc[i - 1] and
                df["high"].iloc[i] > df["high"].iloc[i + 1] and
                df["low"].iloc[i] > df["low"].iloc[i - 1] and
                df["low"].iloc[i] > df["low"].iloc[i + 1]):
            df.iloc[i, df.columns.get_loc("fractal_type")] = 1

        # 底分型判断
        elif (df["low"].iloc[i] < df["low"].iloc[i - 1] and
              df["low"].iloc[i] < df["low"].iloc[i + 1] and
              df["high"].iloc[i] < df["high"].iloc[i - 1] and
              df["high"].iloc[i] < df["high"].iloc[i + 1]):
            df.iloc[i, df.columns.get_loc("fractal_type")] = -1

    return df


def merge_inclusive_klines(df: pd.DataFrame) -> pd.DataFrame:
    """处理K线包含关系"""
    if len(df) < 2:
        return df

    merged = df.copy()
    to_drop = []

    i = 1
    while i < len(merged):
        prev = merged.iloc[i - 1]
        curr = merged.iloc[i]

        is_inclusive = (
            (curr["high"] <= prev["high"] and curr["low"] >= prev["low"]) or
            (prev["high"] <= curr["high"] and prev["low"] >= curr["low"])
        )

        if is_inclusive:
            if i >= 2:
                prev_prev = merged.iloc[i - 2]
                direction = 1 if prev["high"] > prev_prev["high"] else -1
            else:
                direction = 1 if curr["close"] > prev["close"] else -1

            if direction == 1:
                merged.iloc[i - 1, merged.columns.get_loc("high")] = max(prev["high"], curr["high"])
                merged.iloc[i - 1, merged.columns.get_loc("low")] = max(prev["low"], curr["low"])
            else:
                merged.iloc[i - 1, merged.columns.get_loc("high")] = min(prev["high"], curr["high"])
                merged.iloc[i - 1, merged.columns.get_loc("low")] = min(prev["low"], curr["low"])

            to_drop.append(i)
            i += 1
        else:
            i += 1

    if to_drop:
        merged = merged.drop(merged.index[to_drop]).reset_index(drop=True)

    return merged


def find_bi(df: pd.DataFrame, min_bars: int = CHAN_MIN_BARS) -> list:
    """识别笔"""
    fractals = df[df["fractal_type"] != 0].copy()

    if len(fractals) < 2:
        return []

    bi_list = []
    last_fractal_idx = fractals.index[0]
    last_fractal_type = fractals.iloc[0]["fractal_type"]

    for i in range(1, len(fractals)):
        curr_idx = fractals.index[i]
        curr_type = fractals.iloc[i]["fractal_type"]

        if (curr_type != last_fractal_type and
                curr_idx - last_fractal_idx >= min_bars):
            direction = 1 if last_fractal_type == -1 else -1
            bi_list.append((last_fractal_idx, curr_idx, direction))
            last_fractal_idx = curr_idx
            last_fractal_type = curr_type
        else:
            if last_fractal_type == 1 and curr_type == 1:
                if df["high"].iloc[curr_idx] > df["high"].iloc[last_fractal_idx]:
                    last_fractal_idx = curr_idx
            elif last_fractal_type == -1 and curr_type == -1:
                if df["low"].iloc[curr_idx] < df["low"].iloc[last_fractal_idx]:
                    last_fractal_idx = curr_idx

    return bi_list


def find_duan(df: pd.DataFrame, bi_list: list) -> list:
    """识别线段"""
    if len(bi_list) < 3:
        return []

    duan_list = []
    start_idx = bi_list[0][0]

    for i in range(2, len(bi_list)):
        bi1 = bi_list[i - 2]
        bi2 = bi_list[i - 1]
        bi3 = bi_list[i]

        if bi1[2] == 1:
            if df["low"].iloc[bi3[1]] < df["low"].iloc[bi1[1]]:
                duan_list.append((start_idx, bi2[1]))
                start_idx = bi2[1]
        else:
            if df["high"].iloc[bi3[1]] > df["high"].iloc[bi1[1]]:
                duan_list.append((start_idx, bi2[1]))
                start_idx = bi2[1]

    if start_idx != bi_list[-1][1]:
        duan_list.append((start_idx, bi_list[-1][1]))

    return duan_list


def analyze_chan(df: pd.DataFrame) -> dict:
    """执行完整的缠论分析"""
    if df.empty or len(df) < 5:
        return {"fractals": pd.DataFrame(), "bi": [], "duan": []}

    merged = merge_inclusive_klines(df)
    merged = find_fractals(merged)
    bi_list = find_bi(merged)
    duan_list = find_duan(merged, bi_list)
    df_marked = find_fractals(df)

    return {
        "fractals": df_marked[df_marked["fractal_type"] != 0],
        "bi": bi_list,
        "duan": duan_list,
        "merged_df": merged,
    }


def get_chan_conclusion(df: pd.DataFrame, chan_result: dict) -> dict:
    """
    生成缠论方向性判断
    """
    bi_list = chan_result.get("bi", [])
    duan_list = chan_result.get("duan", [])

    if not bi_list:
        return {"signal": "未知", "conclusion": "缠论数据不足，无法识别笔结构", "score": 50}

    # 看最近一笔的方向
    last_bi = bi_list[-1]
    direction = last_bi[2]  # 1=向上笔, -1=向下笔

    # 统计近期笔的方向
    recent_bi = bi_list[-min(5, len(bi_list)):]
    up_count = sum(1 for b in recent_bi if b[2] == 1)
    down_count = sum(1 for b in recent_bi if b[2] == -1)

    score = 50
    if direction == 1:  # 当前向上笔
        score += 10
        trend_word = "向上笔运行中"
    else:
        score -= 10
        trend_word = "向下笔运行中"

    if up_count > down_count:
        score += 5
        recent_trend = "近期偏多"
    elif down_count > up_count:
        score -= 5
        recent_trend = "近期偏空"
    else:
        recent_trend = "近期震荡"

    # 线段方向
    if duan_list:
        last_duan = duan_list[-1]
        duan_start = df["low"].iloc[last_duan[0]] if last_duan[0] < len(df) else 0
        duan_end = df["low"].iloc[last_duan[1]] if last_duan[1] < len(df) else 0
        if duan_end > duan_start:
            score += 5
            duan_trend = "线段向上"
        else:
            score -= 5
            duan_trend = "线段向下"
    else:
        duan_trend = "线段不明"

    score = max(0, min(100, score))
    signal = "偏多" if score >= 58 else "偏空" if score <= 42 else "震荡"

    conclusion = f"当前{trend_word}，{recent_trend}，{duan_trend}，共识别{len(bi_list)}笔、{len(duan_list)}线段"

    return {"signal": signal, "conclusion": conclusion, "score": score}
