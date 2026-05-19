"""
投资分析工具 - Streamlit 主入口
单页面路由架构，通过 session_state 管理页面切换
"""
import streamlit as st
from config import APP_TITLE, APP_ICON, PAGE_CONFIG, COLORS
from views.ai_dashboard import render_ai_dashboard

# 页面配置（必须在最前面）
st.set_page_config(**PAGE_CONFIG)

# 注入全局CSS样式 - 浅色轻盈风格
st.markdown("""
<style>
/* 隐藏默认sidebar导航 */
section[data-testid="stSidebarNav"] { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
button[kind="header"] { display: none !important; }

/* 全局样式 */
.main .block-container {
    padding-top: 2rem;
    padding-bottom: 2rem;
    max-width: 1400px;
    background: #f8f9fa;
}

/* 卡片样式 */
.card {
    background: #ffffff;
    border-radius: 12px;
    padding: 28px 24px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    border: 1px solid #e8ecf0;
    cursor: pointer;
    transition: all 0.3s ease;
    text-align: center;
    height: 100%;
}
.card:hover {
    box-shadow: 0 6px 24px rgba(79,172,254,0.18);
    transform: translateY(-4px);
    border-color: #4facfe;
}

/* 首页头部渐变 */
.hero-header {
    background: linear-gradient(135deg, #4facfe 0%, #667eea 100%);
    border-radius: 20px;
    padding: 48px 40px;
    margin-bottom: 32px;
    color: white;
}

/* 指标卡片 */
.indicator-card {
    background: #ffffff;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 6px rgba(0,0,0,0.05);
    border: 1px solid #eef0f4;
    margin-bottom: 12px;
}

/* 结论标签 */
.conclusion-tag {
    display: inline-block;
    padding: 4px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
}
.tag-bullish { background: #dcfce7; color: #16a34a; }
.tag-bearish { background: #fee2e2; color: #dc2626; }
.tag-neutral { background: #f0f0f0; color: #6b7280; }

/* 新闻条目 */
.news-item {
    background: #ffffff;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 10px;
    border: 1px solid #eef0f4;
    box-shadow: 0 1px 4px rgba(0,0,0,0.03);
}

/* 预测评分 */
.score-circle {
    width: 120px;
    height: 120px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 32px;
    font-weight: 800;
    margin: 0 auto 12px;
}

/* 分隔线 */
hr { border: none; border-top: 1px solid #eef0f4; margin: 24px 0; }

/* stMetric 自定义 */
[data-testid="stMetricValue"] { font-size: 1.5rem !important; }

/* 字体层次 */
h1 { font-size: 24px !important; font-weight: 800 !important; color: #1e293b !important; }
h2 { font-size: 18px !important; font-weight: 700 !important; color: #1e293b !important; }
h3 { font-size: 16px !important; font-weight: 600 !important; color: #1e293b !important; }
</style>
""", unsafe_allow_html=True)

# 初始化 session_state（只存字符串，不存函数）
if "page" not in st.session_state:
    st.session_state.page = "home"

# 导入视图（必须在 PAGES 字典之前完成全部导入）
from views.home import render_home
from views.market_overview import render_market_overview
from views.technical import render_technical
from views.fund_flow import render_fund_flow
from views.news import render_news
from views.prediction import render_prediction
from views.batch_prediction import render_batch_prediction

# 页面路由映射
PAGES = {
    "home": render_home,
    "market_overview": render_market_overview,
    "technical": render_technical,
    "fund_flow": render_fund_flow,
    "news": render_news,
    "prediction": render_prediction,
    "batch_prediction": render_batch_prediction,
    "ai_dashboard": render_ai_dashboard,
}

# 渲染当前页面
current_page = st.session_state.page
if current_page in PAGES:
    PAGES[current_page]()
else:
    st.session_state.page = "home"
    st.rerun()
