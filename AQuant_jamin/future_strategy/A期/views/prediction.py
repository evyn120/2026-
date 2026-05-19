"""
投资预测视图
整合技术面、资金面、消息面、博主观点的加权评分
"""
import streamlit as st
import pandas as pd
from config import DEFAULT_SYMBOLS, FUTURES_SYMBOLS, KLINE_PERIODS, PREDICTION_WEIGHTS
from services.market_data import get_kline_data
from services.technical_analysis import (
    calc_all_indicators,
    get_all_conclusions,
    get_overall_technical_verdict,
)
from services.chan_theory import analyze_chan, get_chan_conclusion
from services.elliott_wave import analyze_elliott, get_elliott_conclusion
from services.fund_flow import calc_fund_flow_all, get_fund_flow_conclusions
from services.news_scraper import get_news, get_news_sentiment_score
from services.blogger_scraper import get_all_blogger_views, calculate_blogger_score
from services.sentiment import get_batch_sentiment_score



def render_prediction():
    """渲染投资预测页"""
    # 返回首页按钮
    if st.button("← 返回首页", key="back_home_pred"):
        st.session_state.page = "home"; st.rerun()

    st.title("🎯 投资预测")
    st.markdown("---")

    # 品种选择
    all_symbols = dict(DEFAULT_SYMBOLS)
    all_symbols.update(FUTURES_SYMBOLS)
    if "custom_symbols" in st.session_state and st.session_state.custom_symbols:
        all_symbols.update(st.session_state.custom_symbols)

    selected = st.selectbox(
        "选择品种",
        options=list(all_symbols.keys()),
        format_func=lambda x: f"{x} ({all_symbols[x]})",
        key="pred_symbol_select",
    )

    if not selected:
        return

    symbol = all_symbols[selected]

    # ===== 1. 获取各维度数据 =====
    progress = st.progress(0, text="正在分析各维度数据...")

    # 技术面
    progress.progress(20, text="正在计算技术指标...")
    df = get_kline_data(symbol, "日K")
    tech_score = 50
    tech_direction = "中性"
    tech_reason = "数据不足"

    if not df.empty:
        df = calc_all_indicators(df)
        conclusions = get_all_conclusions(df)
        chan_result = analyze_chan(df)
        chan_conclusion = get_chan_conclusion(df, chan_result)
        elliott_result = analyze_elliott(df)
        elliott_conclusion = get_elliott_conclusion(elliott_result)

        all_conclusions = dict(conclusions)
        all_conclusions["缠论"] = chan_conclusion
        all_conclusions["波浪理论"] = elliott_conclusion

        verdict = get_overall_technical_verdict(all_conclusions)
        tech_score = verdict["score"]
        tech_direction = verdict["direction"]
        tech_reason = verdict["reason"]

    # 资金面
    progress.progress(45, text="正在计算资金面指标...")
    fund_score = 50
    fund_direction = "中性"
    fund_reason = "数据不足"

    if not df.empty:
        df = calc_fund_flow_all(df)
        fund_conclusions = get_fund_flow_conclusions(df)
        overall_fund = fund_conclusions.get("overall", {})
        fund_score = overall_fund.get("score", 50)
        fund_direction = overall_fund.get("direction", "中性")
        fund_reason = overall_fund.get("reason", "")

    # 消息面（商品期货专用情感分析：传入 selected 中文名 → 自动叠加行业差异化词库）
    progress.progress(65, text="正在获取新闻数据...")
    news_score = 50
    news_direction = "中性"
    news_confidence = "低"

    try:
        news_df = get_news()
        if not news_df.empty:
            # 优先用 title 列；若没有，则回退到 content/正文列
            if "title" in news_df.columns:
                news_titles = news_df["title"].dropna().astype(str).tolist()
            elif "content" in news_df.columns:
                news_titles = news_df["content"].dropna().astype(str).tolist()
            else:
                # 兜底：把第一列当文本
                news_titles = news_df.iloc[:, 0].dropna().astype(str).tolist()

            if news_titles:
                news_result = get_batch_sentiment_score(news_titles, symbol_name=selected)
                news_score = news_result.get("score", 50)
                news_direction = news_result.get("direction", "中性")
                news_confidence = news_result.get("confidence", "低")
    except Exception as e:
        # 调试期间可以打印异常；生产环境保持静默
        # st.warning(f"消息面分析异常：{e}")
        pass


    # 博主观点
    progress.progress(85, text="正在获取博主观点...")
    blogger_score = 50
    blogger_direction = "中性"
    blogger_confidence = "低"
    blogger_df = pd.DataFrame()

    try:
        blogger_df = get_all_blogger_views()
        if not blogger_df.empty:
            blogger_result = calculate_blogger_score(blogger_df)
            blogger_score = blogger_result.get("score", 50)
            blogger_direction = blogger_result.get("direction", "中性")
            blogger_confidence = blogger_result.get("confidence", "低")
    except Exception:
        pass

    progress.progress(100, text="分析完成！")

    # ===== 2. 加权评分 =====
    weights = PREDICTION_WEIGHTS
    weighted_score = (
        tech_score * weights["技术面"] +
        fund_score * weights["资金面"] +
        news_score * weights["消息面"] +
        blogger_score * weights["博主观点"]
    )

    # 方向判断
    if weighted_score >= 58:
        final_direction = "偏多"
        direction_desc = "建议关注做多机会"
    elif weighted_score <= 42:
        final_direction = "偏空"
        direction_desc = "建议关注做空机会或减仓"
    else:
        final_direction = "震荡"
        direction_desc = "方向不明确，建议观望"

    # 置信度
    min_conf = min(tech_direction != "中性", fund_direction != "中性",
                   news_direction != "中性", blogger_direction != "中性")
    direction_count = sum(1 for d in [tech_direction, fund_direction, news_direction, blogger_direction]
                          if d == final_direction or (d == "中性" and final_direction == "震荡"))
    if direction_count >= 3:
        confidence = "高"
    elif direction_count >= 2:
        confidence = "中"
    else:
        confidence = "低"

    # ===== 3. 展示预测结果 =====
    st.markdown("---")

    # 最终预测
    st.markdown("### 🔥 综合投资预测")

    if final_direction == "偏多":
        circle_color = "#dcfce7"
        circle_text_color = "#16a34a"
        tag_class = "tag-bullish"
    elif final_direction == "偏空":
        circle_color = "#fee2e2"
        circle_text_color = "#dc2626"
        tag_class = "tag-bearish"
    else:
        circle_color = "#f1f5f9"
        circle_text_color = "#64748b"
        tag_class = "tag-neutral"

    st.markdown(f"""
    <div style="text-align:center; padding:24px;">
        <div class="score-circle" style="background:{circle_color}; color:{circle_text_color};">
            {weighted_score:.0f}
        </div>
        <h2 style="margin:0;">
            <span class="conclusion-tag {tag_class}" style="font-size:1.4em; padding:8px 24px;">
                {final_direction}
            </span>
        </h2>
        <p style="color:#64748b; margin-top:8px; font-size:1em;">{direction_desc}</p>
        <p style="color:#94a3b8; font-size:0.9em;">置信度：<strong>{confidence}</strong></p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # ===== 4. 各维度评分详情 =====
    st.markdown("### 📊 各维度评分详情")

    dimensions = [
        {
            "name": "技术面",
            "weight": weights["技术面"],
            "score": tech_score,
            "direction": tech_direction,
            "reason": tech_reason,
        },
        {
            "name": "资金面",
            "weight": weights["资金面"],
            "score": fund_score,
            "direction": fund_direction,
            "reason": fund_reason,
        },
        {
            "name": "消息面",
            "weight": weights["消息面"],
            "score": news_score,
            "direction": news_direction,
            "reason": f"置信度：{news_confidence}",
        },
        {
            "name": "博主观点",
            "weight": weights["博主观点"],
            "score": blogger_score,
            "direction": blogger_direction,
            "reason": f"置信度：{blogger_confidence}",
        },
    ]

    for dim in dimensions:
        direction = dim["direction"]
        if direction == "偏多":
            tag_class = "tag-bullish"
            tag_text = "🟢 偏多"
        elif direction == "偏空":
            tag_class = "tag-bearish"
            tag_text = "🔴 偏空"
        else:
            tag_class = "tag-neutral"
            tag_text = "⚪ 中性"

        weighted = dim["score"] * dim["weight"]

        st.markdown(f"""
        <div class="indicator-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <div>
                    <strong style="font-size:1.1em;">{dim['name']}</strong>
                    <span style="color:#94a3b8; font-size:0.85em; margin-left:8px;">
                        权重 {dim['weight']*100:.0f}%
                    </span>
                </div>
                <span class="conclusion-tag {tag_class}">{tag_text}</span>
            </div>
            <div style="display:flex; align-items:center; gap:12px;">
                <div style="flex:1;">
                    <div style="background:#eef0f4; border-radius:6px; height:8px; overflow:hidden;">
                        <div style="background:linear-gradient(90deg, #4facfe, #667eea);
                                    width:{dim['score']}%; height:100%; border-radius:6px;"></div>
                    </div>
                </div>
                <span style="font-weight:700; min-width:80px; text-align:right;">
                    {dim['score']:.0f}/100
                </span>
                <span style="color:#94a3b8; font-size:0.85em; min-width:80px; text-align:right;">
                    加权 {weighted:.1f}
                </span>
            </div>
            <p style="color:#64748b; font-size:0.85em; margin-top:4px;">{dim['reason']}</p>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")

    # ===== 5. 博主观点详情 =====
    st.subheader("📋 博主/分析师观点")

    try:
        if blogger_df is not None and not blogger_df.empty:
            display_cols = ["博主", "平台", "观点", "时间", "方向"]
            available_cols = [c for c in display_cols if c in blogger_df.columns]
            st.dataframe(
                blogger_df[available_cols],
                use_container_width=True,
                hide_index=True,
                height=300,
            )
        else:
            st.info("暂无博主观点数据")
    except Exception:
        st.info("博主观点数据获取中，请稍后重试")

    # 底部提示
    st.markdown("---")
    st.caption("⚠️ 以上预测基于技术指标、资金面、消息面和博主观点的加权评分，仅供参考，不构成投资建议。")
