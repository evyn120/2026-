"""
图表绘制工具
使用 Plotly 绘制K线图和技术指标
浅色轻盈风格 - template="plotly_white"
"""
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
from config import MA_PERIODS


# 统一使用的模板
CHART_TEMPLATE = "plotly_white"


def create_kline_chart(
    df: pd.DataFrame,
    title: str = "K线图",
    show_ma: bool = True,
    ma_periods: list = None,
    show_volume: bool = True,
) -> go.Figure:
    """创建K线图"""
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="暂无数据", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(size=24))
        fig.update_layout(template=CHART_TEMPLATE)
        return fig

    if ma_periods is None:
        ma_periods = MA_PERIODS

    rows = 2 if show_volume else 1
    row_heights = [0.75, 0.25] if show_volume else [1.0]

    fig = make_subplots(
        rows=rows, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
    )

    # K线图 - 涨红跌绿（中国惯例）
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="K线",
            increasing_line_color="#ef5350",
            decreasing_line_color="#26a69a",
            increasing_fillcolor="#ef5350",
            decreasing_fillcolor="#26a69a",
        ),
        row=1, col=1,
    )

    # 均线
    if show_ma:
        ma_colors = {
            5: "#ffd54f",
            10: "#ff9800",
            20: "#e91e63",
            60: "#2196f3",
            120: "#9c27b0",
            250: "#795548",
        }
        for p in ma_periods:
            col_name = f"MA{p}"
            if col_name in df.columns:
                fig.add_trace(
                    go.Scatter(
                        x=df.index,
                        y=df[col_name],
                        mode="lines",
                        name=f"MA{p}",
                        line=dict(color=ma_colors.get(p, "#999999"), width=1),
                    ),
                    row=1, col=1,
                )

    # 成交量
    if show_volume and "volume" in df.columns:
        colors = [
            "#ef5350" if df["close"].iloc[i] >= df["open"].iloc[i] else "#26a69a"
            for i in range(len(df))
        ]
        fig.add_trace(
            go.Bar(
                x=df.index,
                y=df["volume"],
                name="成交量",
                marker_color=colors,
                opacity=0.7,
            ),
            row=2, col=1,
        )

    fig.update_layout(
        title=dict(text=title, x=0.5, font=dict(size=16)),
        xaxis_rangeslider_visible=False,
        template=CHART_TEMPLATE,
        height=700,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=80, b=50),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )

    fig.update_xaxes(title_text="日期", row=rows, col=1)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    if show_volume:
        fig.update_yaxes(title_text="成交量", row=2, col=1)

    return fig


def create_macd_chart(df: pd.DataFrame, title: str = "MACD") -> go.Figure:
    """创建 MACD 图表"""
    fig = make_subplots(rows=1, cols=1)

    if "MACD_DIF" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["MACD_DIF"], mode="lines", name="DIF",
                       line=dict(color="#2196f3", width=1.5))
        )

    if "MACD_SIGNAL" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["MACD_SIGNAL"], mode="lines", name="DEA",
                       line=dict(color="#ff9800", width=1.5))
        )

    if "MACD_HIST" in df.columns:
        colors = ["#ef5350" if v >= 0 else "#26a69a" for v in df["MACD_HIST"].fillna(0)]
        fig.add_trace(
            go.Bar(x=df.index, y=df["MACD_HIST"], name="MACD柱",
                   marker_color=colors, opacity=0.7)
        )

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template=CHART_TEMPLATE,
        height=300,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )
    fig.update_yaxes(title_text="MACD")

    return fig


def create_kdj_chart(df: pd.DataFrame, title: str = "KDJ") -> go.Figure:
    """创建 KDJ 图表"""
    fig = go.Figure()

    if "K" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["K"], mode="lines", name="K",
                                 line=dict(color="#2196f3", width=1.5)))

    if "D" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["D"], mode="lines", name="D",
                                 line=dict(color="#ff9800", width=1.5)))

    if "J" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["J"], mode="lines", name="J",
                                 line=dict(color="#e91e63", width=1.5)))

    fig.add_hline(y=80, line_dash="dash", line_color="rgba(239,83,80,0.4)", annotation_text="超买")
    fig.add_hline(y=20, line_dash="dash", line_color="rgba(38,166,154,0.4)", annotation_text="超卖")

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template=CHART_TEMPLATE,
        height=300,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )
    fig.update_yaxes(title_text="KDJ")

    return fig


def create_rsi_chart(df: pd.DataFrame, title: str = "RSI") -> go.Figure:
    """创建 RSI 图表"""
    fig = go.Figure()

    if "RSI" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["RSI"], mode="lines", name="RSI",
                                 line=dict(color="#9c27b0", width=1.5)))

    fig.add_hline(y=70, line_dash="dash", line_color="rgba(239,83,80,0.4)", annotation_text="超买(70)")
    fig.add_hline(y=30, line_dash="dash", line_color="rgba(38,166,154,0.4)", annotation_text="超卖(30)")
    fig.add_hrect(y0=30, y1=70, fillcolor="rgba(128,128,128,0.08)", line_width=0)

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template=CHART_TEMPLATE,
        height=300,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )
    fig.update_yaxes(title_text="RSI")

    return fig


def create_obv_chart(df: pd.DataFrame, title: str = "OBV") -> go.Figure:
    """创建 OBV 图表"""
    fig = make_subplots(rows=1, cols=1)

    if "OBV" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["OBV"], mode="lines", name="OBV",
                       line=dict(color="#4facfe", width=1.5))
        )

    if "OBV_MA5" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["OBV_MA5"], mode="lines", name="OBV_MA5",
                       line=dict(color="#ff9800", width=1, dash="dash"))
        )

    if "OBV_MA20" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["OBV_MA20"], mode="lines", name="OBV_MA20",
                       line=dict(color="#e91e63", width=1, dash="dash"))
        )

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template=CHART_TEMPLATE,
        height=300,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )
    fig.update_yaxes(title_text="OBV")

    return fig


def create_mfi_chart(df: pd.DataFrame, title: str = "MFI") -> go.Figure:
    """创建 MFI 图表"""
    fig = go.Figure()

    if "MFI" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["MFI"], mode="lines", name="MFI",
                                 line=dict(color="#667eea", width=1.5)))

    fig.add_hline(y=80, line_dash="dash", line_color="rgba(239,83,80,0.4)", annotation_text="超买(80)")
    fig.add_hline(y=20, line_dash="dash", line_color="rgba(38,166,154,0.4)", annotation_text="超卖(20)")
    fig.add_hrect(y0=20, y1=80, fillcolor="rgba(128,128,128,0.08)", line_width=0)

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template=CHART_TEMPLATE,
        height=300,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=30),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8f9fa",
    )
    fig.update_yaxes(title_text="MFI")

    return fig


def create_kline_with_chan(
    df: pd.DataFrame,
    chan_result: dict,
    title: str = "缠论分析",
) -> go.Figure:
    """创建带缠论标注的K线图"""
    fig = create_kline_chart(df, title=title, show_ma=False, show_volume=False)

    fractals = chan_result.get("fractals", pd.DataFrame())
    if not fractals.empty:
        top_fractals = fractals[fractals["fractal_type"] == 1]
        if not top_fractals.empty:
            fig.add_trace(
                go.Scatter(
                    x=top_fractals.index,
                    y=top_fractals["high"] * 1.005,
                    mode="markers", name="顶分型",
                    marker=dict(symbol="triangle-down", size=10, color="#ff5722"),
                )
            )

        bottom_fractals = fractals[fractals["fractal_type"] == -1]
        if not bottom_fractals.empty:
            fig.add_trace(
                go.Scatter(
                    x=bottom_fractals.index,
                    y=bottom_fractals["low"] * 0.995,
                    mode="markers", name="底分型",
                    marker=dict(symbol="triangle-up", size=10, color="#4caf50"),
                )
            )

    bi_list = chan_result.get("bi", [])
    for bi in bi_list:
        start_idx, end_idx, direction = bi
        if start_idx < len(df) and end_idx < len(df):
            fig.add_shape(
                type="line",
                x0=df.index[start_idx], x1=df.index[end_idx],
                y0=df["low"].iloc[start_idx] if direction == 1 else df["high"].iloc[start_idx],
                y1=df["high"].iloc[end_idx] if direction == 1 else df["low"].iloc[end_idx],
                line=dict(color="#ffeb3b", width=2, dash="dot"),
            )

    return fig


def create_kline_with_elliott(
    df: pd.DataFrame,
    elliott_result: dict,
    title: str = "波浪理论分析",
) -> go.Figure:
    """创建带波浪标注的K线图"""
    fig = create_kline_chart(df, title=title, show_ma=False, show_volume=False)

    impulse_waves = elliott_result.get("impulse_waves", [])
    wave_colors_impulse = ["#4caf50", "#ff9800", "#2196f3", "#9c27b0", "#f44336"]

    for wave in impulse_waves[-3:]:
        points = wave["points"]
        for i in range(len(points) - 1):
            p1 = points[i]
            p2 = points[i + 1]
            fig.add_shape(
                type="line",
                x0=p1[0], x1=p2[0],
                y0=p1[1], y1=p2[1],
                line=dict(color=wave_colors_impulse[i % 5], width=2.5),
            )
            fig.add_annotation(
                x=p1[0], y=p1[1],
                text=f"W{i + 1}", showarrow=False,
                font=dict(size=12, color=wave_colors_impulse[i % 5]),
            )

    corrective_waves = elliott_result.get("corrective_waves", [])
    wave_colors_corrective = ["#e91e63", "#00bcd4", "#e91e63"]

    for wave in corrective_waves[-2:]:
        points = wave["points"]
        labels = ["A", "B", "C"]
        for i in range(len(points) - 1):
            p1 = points[i]
            p2 = points[i + 1]
            fig.add_shape(
                type="line",
                x0=p1[0], x1=p2[0],
                y0=p1[1], y1=p2[1],
                line=dict(color=wave_colors_corrective[i % 3], width=2, dash="dash"),
            )
            fig.add_annotation(
                x=p1[0], y=p1[1],
                text=labels[i], showarrow=False,
                font=dict(size=12, color=wave_colors_corrective[i % 3]),
            )

    return fig

# ============================================================
# 增强版多面板 K 线图（K线 + MA + 成交量 + MACD/KDJ/RSI 子图）
# ============================================================
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
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
    panels = [("K线", 0.50)]                       # 主图始终存在
    if show_volume: panels.append(("成交量", 0.15))
    if show_macd:   panels.append(("MACD",   0.13))
    if show_kdj:    panels.append(("KDJ",    0.12))
    if show_rsi:    panels.append(("RSI",    0.10))

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

    # ===== 4. MACD 子图（DIF 线 + DEA 线 + HIST 柱）=====
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
        xaxis_rangeslider_visible=False,   # 隐藏 Plotly 自带的小型缩略条
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=60, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e2e8f0")
    fig.update_yaxes(showgrid=True, gridcolor="#e2e8f0")
    return fig
