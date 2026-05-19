"""
首页视图 - 功能模块卡片概览
6个模块卡片，点击跳转详情页
"""
import streamlit as st
from config import COLORS


def render_home():
    """渲染首页"""
    # Hero Header - 淡蓝到淡紫渐变
    st.markdown("""
    <div class="hero-header">
        <h1 style="margin:0; font-size:2.2rem; font-weight:800; color:white;">📊 投资分析工具</h1>
        <p style="margin:8px 0 0; font-size:1.05rem; opacity:0.92; color:white;">
            多维度贵金属及期货投资分析平台 — 技术面 · 资金面 · 消息面 · 综合预测
        </p>
    </div>
    """, unsafe_allow_html=True)

    # 功能模块卡片
    st.markdown("### 🧭 功能导航")
    st.caption("点击按钮进入对应功能模块")

    # ============ 第一行：3个卡片 ============
    col1, col2, col3 = st.columns(3, gap="large")

    with col1:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">📊</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">行情总览</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                实时报价 · K线走势<br/>自定义品种
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📊 进入行情总览", key="nav_market", use_container_width=True):
            st.session_state.page = "market_overview"
            st.rerun()

    with col2:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">📈</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">技术分析</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                MACD/KDJ/RSI · 均线系统<br/>缠论 · 波浪理论 · 信号结论
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📈 进入技术分析", key="nav_tech", use_container_width=True):
            st.session_state.page = "technical"
            st.rerun()

    with col3:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">💰</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">资金面</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                量价分析 · OBV/MFI<br/>量价背离检测
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("💰 进入资金面", key="nav_fund", use_container_width=True):
            st.session_state.page = "fund_flow"
            st.rerun()

    # ============ 第二行：3个卡片 ============
    col4, col5, col6 = st.columns(3, gap="large")

    with col4:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">📰</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">消息面</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                新闻聚合 · 事件日历<br/>情感分析
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📰 进入消息面", key="nav_news", use_container_width=True):
            st.session_state.page = "news"
            st.rerun()

    with col5:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">🎯</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">单品种投资预测</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                博主观点 · 综合评分<br/>方向判断 · 置信度
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🎯 进入投资预测", key="nav_pred", use_container_width=True):
            st.session_state.page = "prediction"
            st.rerun()

    with col6:
        st.markdown("""
        <div class="card">
            <div style="font-size:2.5rem; margin-bottom:8px;">🧮</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">全品种截面预测</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                遍历所有品种 · 多维度汇总<br/>方向排序 · 一表对比
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🧮 进入截面预测", key="nav_batch", use_container_width=True):
            st.session_state.page = "batch_prediction"
            st.rerun()

    # ============ 第三行：新增 AI 期货分析助手 + 2个占位 ============
    col7, col8, col9 = st.columns(3, gap="large")

    with col7:
        st.markdown("""
        <div class="card" style="border:1px solid rgba(99,102,241,0.25); 
                                  background:linear-gradient(135deg,#eef2ff 0%,#f5f3ff 100%);">
            <div style="font-size:2.5rem; margin-bottom:8px;">🤖</div>
            <h3 style="margin:0 0 6px; color:#1e293b;">AI 期货分析助手</h3>
            <p style="color:#64748b; font-size:0.9rem; margin:0;">
                四维评分 · 否决规则<br/>布局/关注/等待 三档评级
            </p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🤖 进入 AI 期货分析助手", key="nav_ai_dash", use_container_width=True):
            st.session_state.page = "ai_dashboard"
            st.rerun()

    # col8、col9 留空，未来扩展使用
    # with col8: ...
    # with col9: ...

    # ============ 底部信息 ============
    st.markdown("---")
    footer_cols = st.columns(3)
    with footer_cols[0]:
        st.caption("📊 数据源：akshare（新浪期货主连，准实时）")
    with footer_cols[1]:
        st.caption("🔧 技术栈：Streamlit + Plotly + ta")
    with footer_cols[2]:
        st.caption("⚠️ 仅供学习参考，不构成投资建议")
