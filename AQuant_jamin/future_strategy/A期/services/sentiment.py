"""
情感分析服务（商品期货专用增强版）
特性：
1. 通用关键词词库（POSITIVE_KEYWORDS / NEGATIVE_KEYWORDS）
2. 按品种分类的差异化高权重词库
3. 否定词检测 → 反转情感方向
4. 语义反转词组（美元、加/降息等）
5. 关键词加权计分（默认权重 1，分行业词库可指定权重 2-3）
"""
import re
from typing import Optional
from config import (
    POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS,
    NEGATION_WORDS, NEGATION_WINDOW, REVERSAL_PHRASES,
    KEYWORDS_BY_SECTOR, SYMBOL_TO_SECTOR,
)


# ============================================================
# 1. 底层：带否定词检测的加权匹配
# ============================================================
def _is_negated(text: str, match_start: int) -> bool:
    """检查关键词前 NEGATION_WINDOW 个字符内是否有否定词"""
    window_start = max(0, match_start - NEGATION_WINDOW)
    window_text = text[window_start:match_start].lower()
    return any(neg in window_text for neg in NEGATION_WORDS)


def _count_weighted(text: str, keyword_map: dict) -> tuple[float, list[str]]:
    """
    在 text 中匹配 keyword_map 中的关键词，返回 (加权得分, 命中词列表)
    keyword_map: {关键词: 权重}
    """
    text_lower = text.lower()
    score = 0.0
    hits = []
    for kw, weight in keyword_map.items():
        kw_lower = kw.lower()
        for match in re.finditer(re.escape(kw_lower), text_lower):
            if _is_negated(text_lower, match.start()):
                # 否定 → 反向计分（按一半权重，避免过度反转）
                score -= weight * 0.5
                hits.append(f"!{kw}")
            else:
                score += weight
                hits.append(kw)
    return score, hits


# ============================================================
# 2. 顶层：综合情感打分
# ============================================================
def get_sentiment_score(
    text: str,
    symbol_name: Optional[str] = None,
) -> dict:
    """
    对单条文本（新闻标题/正文）做情感打分。

    Args:
        text: 待分析文本
        symbol_name: 可选，品种中文名（如"螺纹"/"豆粕"），用于叠加行业词库

    Returns:
        {
            "score":      0-100 之间的分值（>55 偏多，<45 偏空，否则中性）,
            "direction":  "偏多" / "偏空" / "中性",
            "raw_score":  原始净得分（正负累加，不归一化）,
            "pos_hits":   命中的利多词列表,
            "neg_hits":   命中的利空词列表,
            "reversal":   触发的语义反转词列表,
        }
    """
    if not text or not isinstance(text, str):
        return {
            "score": 50, "direction": "中性", "raw_score": 0,
            "pos_hits": [], "neg_hits": [], "reversal": [],
        }

    # 通用词库统一权重 1
    pos_map = {kw: 1 for kw in POSITIVE_KEYWORDS}
    neg_map = {kw: 1 for kw in NEGATIVE_KEYWORDS}

    # 叠加行业差异化词库（高权重 2-3）
    if symbol_name and symbol_name in SYMBOL_TO_SECTOR:
        sector = SYMBOL_TO_SECTOR[symbol_name]
        sector_pos = KEYWORDS_BY_SECTOR.get(sector, {}).get("positive", {})
        sector_neg = KEYWORDS_BY_SECTOR.get(sector, {}).get("negative", {})
        # 行业词覆盖/合并通用词
        pos_map.update(sector_pos)
        neg_map.update(sector_neg)

    pos_score, pos_hits = _count_weighted(text, pos_map)
    neg_score, neg_hits = _count_weighted(text, neg_map)

    # 语义反转词组（如"美元上涨"是利空，"降息"是利多）
    reversal_score = 0.0
    reversal_hits = []
    text_lower = text.lower()
    for phrase, direction in REVERSAL_PHRASES.items():
        if phrase.lower() in text_lower:
            reversal_score += direction * 2   # 反转词权重 = 2
            reversal_hits.append(f"{phrase}({'+' if direction>0 else '-'})")

    raw_score = pos_score - neg_score + reversal_score

    # 映射到 0-100 区间：tanh 平滑，避免极端值
    # 经验：raw_score 每 +1 → 约 +3 分，±10 接近饱和
    import math
    normalized = 50 + 25 * math.tanh(raw_score / 5.0)
    normalized = max(0, min(100, normalized))

    if normalized >= 55:
        direction = "偏多"
    elif normalized <= 45:
        direction = "偏空"
    else:
        direction = "中性"

    return {
        "score": round(normalized, 1),
        "direction": direction,
        "raw_score": round(raw_score, 2),
        "pos_hits": pos_hits,
        "neg_hits": neg_hits,
        "reversal": reversal_hits,
    }


# ============================================================
# 3. 批量打分：对 DataFrame 的标题/正文列做整体情感汇总
# ============================================================
def get_batch_sentiment_score(
    texts: list[str],
    symbol_name: Optional[str] = None,
) -> dict:
    """
    对多条新闻做批量打分，输出平均分与方向。
    """
    if not texts:
        return {"score": 50, "direction": "中性", "count": 0, "confidence": "低"}

    results = [get_sentiment_score(t, symbol_name) for t in texts if t]
    if not results:
        return {"score": 50, "direction": "中性", "count": 0, "confidence": "低"}

    avg_score = sum(r["score"] for r in results) / len(results)
    bull_cnt = sum(1 for r in results if r["direction"] == "偏多")
    bear_cnt = sum(1 for r in results if r["direction"] == "偏空")

    if avg_score >= 55:
        direction = "偏多"
    elif avg_score <= 45:
        direction = "偏空"
    else:
        direction = "中性"

    # 置信度：偏向同一方向的新闻占比
    same_dir = bull_cnt if direction == "偏多" else bear_cnt if direction == "偏空" else (len(results) - bull_cnt - bear_cnt)
    ratio = same_dir / len(results)
    if ratio >= 0.7:
        confidence = "高"
    elif ratio >= 0.5:
        confidence = "中"
    else:
        confidence = "低"

    return {
        "score": round(avg_score, 1),
        "direction": direction,
        "count": len(results),
        "bullish_count": bull_cnt,
        "bearish_count": bear_cnt,
        "confidence": confidence,
    }


# ============================================================
# 4. 向后兼容：保留旧版 analyze_sentiment 接口
# 旧调用方（services/news_scraper.py）仍在用这个名字
# ============================================================
def analyze_sentiment(text: str, symbol_name: Optional[str] = None) -> dict:
    """
    【向后兼容包装】等价于 get_sentiment_score()。

    旧版本返回结构可能是 {"sentiment": "positive"/"negative"/"neutral", "score": float}，
    这里同时返回新旧两套字段，确保 news_scraper.py 不管按哪种方式取值都不会 KeyError。
    """
    result = get_sentiment_score(text, symbol_name=symbol_name)

    # 旧字段名映射（按常见旧版约定补全）
    direction = result["direction"]
    if direction == "偏多":
        sentiment_label = "positive"
    elif direction == "偏空":
        sentiment_label = "negative"
    else:
        sentiment_label = "neutral"

    # 同时挂上新旧字段，最大兼容
    result["sentiment"] = sentiment_label  # 旧字段 1：英文标签
    result["label"] = direction  # 旧字段 2：中文标签
    return result
