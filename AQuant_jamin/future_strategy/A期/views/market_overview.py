"""
行情总览视图
"""
import streamlit as st
import pandas as pd
from config import DEFAULT_SYMBOLS, FUTURES_SYMBOLS, KLINE_PERIODS
from services.market_data import get_kline_data, get_realtime_price
from services.technical_analysis import calc_all_indicators           # 🆕 计算 MA/MACD/KDJ/RSI
from utils.chart_helper import create_kline_chart, plot_full_chart    # 🆕 新增增强版绘图函数


def render_market_overview():
    """渲染行情总览页"""
    # 返回首页按钮
    if st.button("← 返回首页", key="back_home_market"):
        st.session_state.page = "home"; st.rerun()

    st.title("📊 行情总览")
    st.markdown("---")

    # 初始化 session_state
    if "custom_symbols" not in st.session_state:
        st.session_state.custom_symbols = {}

    # 1. 期货实时报价
    st.subheader("🥇 期货实时报价")
    default_prices = _get_all_default_prices()
    _display_price_cards(default_prices)
    st.caption("💡 报价数据为最新收盘价延迟")
    st.markdown("---")

    # 2. 期货K线图
    st.subheader("📈 期货品种K线走势")
    selected_default = st.selectbox(
        "选择品种",
        options=list(DEFAULT_SYMBOLS.keys()),
        format_func=lambda x: f"{x} ({DEFAULT_SYMBOLS[x]})",
        key="default_symbol_select",
    )
    if selected_default:
        _display_kline(DEFAULT_SYMBOLS[selected_default], selected_default)

    st.markdown("---")

    # 3. 自定义品种
    st.subheader("➕ 自定义期货品种")
    _add_custom_symbol()
    _display_custom_symbols()


def _get_all_default_prices() -> dict:
    """获取所有默认品种的实时报价"""
    results = {}
    for name, symbol in DEFAULT_SYMBOLS.items():
        results[name] = get_realtime_price(symbol)
    return results


def _display_price_cards(prices: dict):
    """展示价格卡片"""
    cols = st.columns(len(prices))
    for i, (name, info) in enumerate(prices.items()):
        with cols[i]:
            price = info.get("price", 0)
            change = info.get("change", 0)
            change_pct = info.get("change_pct", 0)

            if "error" in info:
                st.metric(label=name, value="获取失败", delta="")
            else:
                delta_str = f"{change:+.2f} ({change_pct:+.2f}%)"
                st.metric(
                    label=name,
                    value=f"{price:,.2f}",
                    delta=delta_str,
                    delta_color="normal" if change >= 0 else "inverse",
                )


def _display_kline(symbol: str, symbol_name: str):
    """展示K线图（增强版：支持 MA / 成交量 / MACD / KDJ / RSI 多面板叠加）"""

    # ---------- 周期选择 ----------
    col_p, col_mode = st.columns([1, 2])
    with col_p:
        period = st.selectbox(
            "K线周期",
            options=list(KLINE_PERIODS.keys()),
            index=0,
            key=f"kline_period_{symbol}",
        )
    with col_mode:
        # 🆕 视图模式：简洁版（沿用原 create_kline_chart）/ 进阶版（多指标面板）
        view_mode = st.radio(
            "图表模式",
            options=["简洁版", "进阶版（多指标）"],
            index=1,
            horizontal=True,
            key=f"chart_mode_{symbol}",
        )

    # ---------- 🆕 技术指标勾选 UI ----------
    st.markdown("##### 📐 技术指标显示")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        show_ma = st.checkbox("均线 MA", value=True, key=f"show_ma_{symbol}")
    with c2:
        show_vol = st.checkbox("成交量", value=True, key=f"show_vol_{symbol}")
    with c3:
        show_macd = st.checkbox("MACD",
                                value=(view_mode == "进阶版（多指标）"),
                                key=f"show_macd_{symbol}")
    with c4:
        show_kdj = st.checkbox("KDJ", value=False, key=f"show_kdj_{symbol}")
    with c5:
        show_rsi = st.checkbox("RSI", value=False, key=f"show_rsi_{symbol}")

    # ---------- 拉数据 ----------
    df = get_kline_data(symbol, period)
    if df.empty:
        st.warning(f"暂无 {symbol_name} 的{period}数据")
        return

    # 🆕 一次性算齐所有技术指标（MA / MACD / KDJ / RSI），列会附加到 df 上
    df = calc_all_indicators(df)

    # ---------- 绘图：按模式分流 ----------
    title = f"{symbol_name} ({symbol}) - {period}"
    if view_mode == "简洁版":
        # 旧版图（仅 K 线 + MA + 成交量），保持原行为
        fig = create_kline_chart(df, title=title,
                                 show_ma=show_ma, show_volume=show_vol)
    else:
        # 🆕 进阶版：K 线 + MA 叠加 + 成交量 + MACD/KDJ/RSI 子图
        fig = plot_full_chart(
            df,
            title=title,
            show_ma=show_ma,
            show_volume=show_vol,
            show_macd=show_macd,
            show_kdj=show_kdj,
            show_rsi=show_rsi,
            ma_periods=(5, 20, 60),
        )
    st.plotly_chart(fig, use_container_width=True)

    # ---------- 最新数据详情（保持原样，🆕 额外加技术指标当日值）----------
    with st.expander("📋 最新数据详情"):
        latest = df.iloc[-1]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("开盘", f"{latest['open']:,.2f}")
        c2.metric("最高", f"{latest['high']:,.2f}")
        c3.metric("最低", f"{latest['low']:,.2f}")
        c4.metric("收盘", f"{latest['close']:,.2f}")
        c5.metric("成交量", f"{latest['volume']:,.0f}")

        # 🆕 技术指标当日值（计算结果展示在数据详情里）
        st.markdown("---")
        st.markdown("**​📐 技术指标当日值**")
        t1, t2, t3, t4, t5 = st.columns(5)
        t1.metric("MA5",   f"{latest.get('MA5', float('nan')):,.2f}"   if pd.notna(latest.get('MA5'))   else "—")
        t2.metric("MA20",  f"{latest.get('MA20', float('nan')):,.2f}"  if pd.notna(latest.get('MA20'))  else "—")
        t3.metric("MACD",  f"{latest.get('MACD_HIST', float('nan')):.3f}" if pd.notna(latest.get('MACD_HIST')) else "—",
                  delta=f"DIF {latest.get('MACD_DIF', 0):.3f}" if pd.notna(latest.get('MACD_DIF')) else None)
        t4.metric("RSI",   f"{latest.get('RSI', float('nan')):.1f}"   if pd.notna(latest.get('RSI'))   else "—")
        t5.metric("KDJ-J", f"{latest.get('J', float('nan')):.1f}"     if pd.notna(latest.get('J'))     else "—",
                  delta=f"K {latest.get('K', 0):.1f} / D {latest.get('D', 0):.1f}" if pd.notna(latest.get('K')) else None)


def _add_custom_symbol():
    """添加自定义期货品种"""
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        new_name = st.text_input("品种名称", placeholder="如：原油", key="new_symbol_name")
    with col2:
        new_code = st.text_input("品种代码", placeholder="如：CL=F", key="new_symbol_code")
    with col3:
        st.write("")
        st.write("")
        add_btn = st.button("添加", type="primary", use_container_width=True)

    st.caption("💡 常用期货代码参考：")
    quick_cols = st.columns(4)
    quick_items = list(FUTURES_SYMBOLS.items())
    for i, (name, code) in enumerate(quick_items):
        with quick_cols[i % 4]:
            if st.button(f"{name} ({code})", key=f"quick_{code}"):
                st.session_state.new_symbol_name = name
                st.session_state.new_symbol_code = code
                st.rerun()

    if add_btn:
        if new_name and new_code:
            test_price = get_realtime_price(new_code)
            if test_price.get("price", 0) > 0:
                st.session_state.custom_symbols[new_name] = new_code
                st.success(f"✅ 已添加 {new_name} ({new_code})")
                st.rerun()
            else:
                st.error(f"❌ 无法获取 {new_code} 的数据，请检查代码是否正确")
        else:
            st.warning("请输入品种名称和代码")


def _display_custom_symbols():
    """展示自定义期货品种"""
    if not st.session_state.custom_symbols:
        st.info("暂未添加自定义品种，请在上方添加")
        return

    custom_prices = {}
    for name, code in st.session_state.custom_symbols.items():
        custom_prices[name] = get_realtime_price(code)
    _display_price_cards(custom_prices)

    with st.expander("⚙️ 管理自定义品种"):
        for name, code in list(st.session_state.custom_symbols.items()):
            c1, c2, c3 = st.columns([2, 2, 1])
            with c1:
                st.text(name)
            with c2:
                st.code(code)
            with c3:
                if st.button("🗑️ 删除", key=f"del_{name}"):
                    del st.session_state.custom_symbols[name]
                    st.rerun()

    st.subheader("📈 自定义品种K线")
    selected_custom = st.selectbox(
        "选择品种",
        options=list(st.session_state.custom_symbols.keys()),
        format_func=lambda x: f"{x} ({st.session_state.custom_symbols[x]})",
        key="custom_symbol_select",
    )
    if selected_custom:
        _display_kline(st.session_state.custom_symbols[selected_custom], selected_custom)
