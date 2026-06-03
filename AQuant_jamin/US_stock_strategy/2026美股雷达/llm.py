"""
llm.py — DeepSeek 客户端
========================================
替换原项目中全部 Gemini 调用，包括：
  - 新闻标题批次翻译（原 _gemini_translate_batch）
  - 新闻情感批次分析（原 _gemini_sentiment_batch）
  - AI 个股评论（原 api_ai_comment 中的 _gemini_call）

配置：
  1. 在同目录新建 deepseek.json：{"api_key": "sk-..."}
  2. 或设置环境变量 DEEPSEEK_API_KEY=sk-...
  前端工具栏设置后会自动写入 deepseek.json，无需手动操作。

DeepSeek API 兼容 OpenAI chat/completions 格式，此模块用 requests 直接调用，
无需额外 SDK 依赖。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

# ============================================================================
# 配置
# ============================================================================
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
# deepseek-chat    : DeepSeek-V3，通用对话，性价比高
# deepseek-reasoner: DeepSeek-R1，强推理，适合复杂分析（价格更高）

_CONFIG_FILE = Path(__file__).resolve().parent / "deepseek.json"

# ============================================================================
# Key 管理
# ============================================================================

def get_deepseek_key() -> str:
    """优先读 deepseek.json，其次读环境变量 DEEPSEEK_API_KEY。"""
    key = ""
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            key = data.get("api_key", "")
        except Exception:
            pass
    return (key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()


def save_deepseek_key(api_key: str) -> None:
    """从前端 /api/deepseek POST 时调用，持久化 key。"""
    _CONFIG_FILE.write_text(
        json.dumps({"api_key": api_key.strip()}, ensure_ascii=False),
        encoding="utf-8",
    )


def is_configured() -> bool:
    return bool(get_deepseek_key())


# ============================================================================
# 核心调用
# ============================================================================

def deepseek_call(
    prompt: str,
    key: Optional[str] = None,
    system: str = "你是专业的美股技术分析助理，用繁体中文回答。",
    temperature: float = 0.7,
    max_tokens: int = 2048,
    retries: int = 2,
) -> str:
    """
    向 DeepSeek API 发送单次请求，返回模型回复文本。

    Parameters
    ----------
    prompt      : 用户输入 prompt
    key         : API Key，为 None 时自动读取配置
    system      : 系统角色提示
    temperature : 0~1，越低越确定
    max_tokens  : 最大输出 token 数
    retries     : 网络/限速错误时的自动重试次数

    Raises
    ------
    RuntimeError : 未配置 key 或所有重试失败
    """
    api_key = key or get_deepseek_key()
    if not api_key:
        raise RuntimeError(
            "尚未设定 DeepSeek API key。"
            "请在工具栏「🤖 AI」按钮设定，或在 deepseek.json 中填写 api_key。"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens":  max_tokens,
        "stream":      False,
    }

    last_err: Exception = RuntimeError("DeepSeek call failed")
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_API_URL,
                headers=headers,
                json=payload,
                timeout=90,
            )
            if resp.status_code == 429:
                # 限速：等待后重试
                wait = 2 ** attempt
                print(f"[DeepSeek] 429 限速，{wait}s 后重试（attempt {attempt+1}）")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
            return text
        except requests.HTTPError as e:
            last_err = e
            if resp.status_code in (400, 401, 403):
                # 不可重试的错误
                break
            time.sleep(2 ** attempt)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)

    raise last_err


# ============================================================================
# 翻译（替代 _gemini_translate_batch）
# ============================================================================

def translate_batch(titles: list[str], key: Optional[str] = None) -> Optional[list[str]]:
    """
    批次将英文新闻标题翻译为繁体中文（台湾用语）。
    一次 API call 翻全部，失败返回 None（上层会 fallback 到免费 Google Translate）。

    Parameters
    ----------
    titles : 英文标题列表
    key    : DeepSeek API Key，None 时自动读取

    Returns
    -------
    list[str] 对应翻译结果，或 None（翻译失败）
    """
    if not titles:
        return []
    api_key = key or get_deepseek_key()
    if not api_key:
        return None

    prompt = (
        "把以下英文新闻标题翻译成繁体中文（台湾用语），保持简洁自然，"
        "每行一条翻译对应一条原文，顺序对齐，不要加编号或解释：\n\n"
        + "\n".join(titles)
    )
    try:
        text = deepseek_call(
            prompt, key=api_key,
            system="你是专业的英中翻译助理，只输出翻译结果，不添加任何解释。",
            temperature=0.3,
            max_tokens=1024,
        )
        lines = [
            re.sub(r"^\s*\d+[\.\)]\s*", "", line).strip()
            for line in text.split("\n") if line.strip()
        ]
        if len(lines) >= len(titles):
            return lines[:len(titles)]
        # 行数不对齐，降级到 None 让上层 fallback
        print(f"[DeepSeek translate] 行数不对齐: expected={len(titles)}, got={len(lines)}")
        return None
    except Exception as e:
        print(f"[DeepSeek translate] {e}")
        return None


# ============================================================================
# 情感分析（替代 _gemini_sentiment_batch）
# ============================================================================

def sentiment_batch(titles: list[str], key: Optional[str] = None) -> list[str]:
    """
    批次判断新闻标题对股票的情感影响。
    返回与 titles 等长的 list，每项为 'positive'/'negative'/'neutral'。

    Parameters
    ----------
    titles : 新闻标题列表（中文或英文均可）
    key    : DeepSeek API Key，None 时自动读取
    """
    if not titles:
        return []
    api_key = key or get_deepseek_key()
    if not api_key:
        return ["neutral"] * len(titles)

    prompt = (
        "判断以下新闻标题对相关股票的情感影响，"
        "每行只回 positive / negative / neutral 之一，"
        "不要加编号或解释，顺序对齐：\n\n"
        + "\n".join(titles)
    )
    try:
        text = deepseek_call(
            prompt, key=api_key,
            system="你是专业的金融新闻情感分析助理，只输出情感标签。",
            temperature=0.1,
            max_tokens=256,
        )
        lines = [line.strip().lower() for line in text.split("\n") if line.strip()]
        out = []
        for i in range(len(titles)):
            line = lines[i] if i < len(lines) else ""
            if "pos" in line:
                out.append("positive")
            elif "neg" in line:
                out.append("negative")
            else:
                out.append("neutral")
        return out
    except Exception as e:
        print(f"[DeepSeek sentiment] {e}")
        return ["neutral"] * len(titles)


# ============================================================================
# AI 个股评论（替代 api_ai_comment 中的 Gemini 调用）
# ============================================================================

def ai_comment(stock_data: dict, key: Optional[str] = None) -> dict:
    """
    用 DeepSeek 对单只股票生成结构化投资论点。
    返回 {ok, comment, thesis, risks, triggers, asOf}。

    Parameters
    ----------
    stock_data : fetch_stock() 的返回值（含价格/指标/信号/持仓等字段）
    key        : DeepSeek API Key，None 时自动读取
    """
    api_key = key or get_deepseek_key()
    if not api_key:
        return {"ok": False, "msg": "尚未设定 DeepSeek API key（从工具栏「🤖 AI」按钮设定）"}

    d = stock_data
    sigs = "、".join(s["label"] for s in d.get("signals", [])) or "无强烈信号"

    prompt = f"""你是美股技术分析助理。针对以下个股，用繁体中文写**结构化投资论点**，严格依照下列格式回复:

【论点】
2-3 句说明为什么这档值得买/持有，结合技术面 + 筹码面（内部人 + 13F）。

【风险】
2-3 句具体写出什么状况下要警惕或重新评估（例如：跌破 $X、RSI 过热、机构出货、财报不如预期）。

【触发】
明确的可执行信号，2-3 条，每条一行，格式如「📈 突破 $X → 加码 1/3」「⚠️ RSI > 80 → 减码 1/2」「🛑 跌破 $X 停利出场」。

不要免责声明，不要写「以上分析仅供参考」之类的话。三段都用上述【】标题开头。

【{d.get('name','')} ({d.get('code','')}）{d.get('tag','')}】
收盘 {d.get('price',0)} （前日 {d.get('prev',0)}，{(d.get('price',0)-d.get('prev',1))/d.get('prev',1)*100:+.2f}%）
趋势：{d.get('trend','')}，均线：{d.get('maStatus','')}（5/20/60 = {d.get('ma5',0)}/{d.get('ma20',0)}/{d.get('ma60',0)}）
RSI(14) = {d.get('rsi',0)}，KD(9,3) K/D = {d.get('kd_k',0)}/{d.get('kd_d',0)}，MACD {d.get('macd',0)}
量能变化 {d.get('volChange',0):+.1f}%（5 日均量 {d.get('avgVol',0):,} 股）
分析师评等：Strong Buy 累计 {d.get('chip',{}).get('fi_10',0)} 家、Buy {d.get('chip',{}).get('it_10',0)} 家
近期信号：{sigs}
压力 {d.get('resist',[])} / 支撑 {d.get('support',[])}
"""

    try:
        text = deepseek_call(
            prompt, key=api_key,
            system="你是专业的美股技术分析助理，用繁体中文严格按格式输出，不添加免责声明。",
            temperature=0.7,
            max_tokens=1024,
        )
    except requests.HTTPError as e:
        status = e.response.status_code if hasattr(e, "response") and e.response else 0
        if status == 429:
            return {
                "ok": False,
                "msg": (
                    "DeepSeek API 达到速率限制（429）。"
                    "请稍后再试，或升级 DeepSeek 账号额度。"
                    "提示：评论已 cache 12 小时，正常使用不会频繁触发此限制。"
                ),
            }
        return {"ok": False, "msg": f"DeepSeek HTTP 错误 {status}: {e}"}
    except Exception as e:
        return {"ok": False, "msg": f"DeepSeek 调用失败: {e}"}

    # 解析三段结构
    def _extract_prose(full: str, label: str) -> str:
        m = re.search(rf"【{label}】\s*([\s\S]*?)(?=【|$)", full)
        if not m:
            return ""
        return re.sub(r"\s+", " ", m.group(1)).strip()

    def _extract_lines(full: str, label: str) -> str:
        m = re.search(rf"【{label}】\s*([\s\S]*?)(?=【|$)", full)
        return m.group(1).strip() if m else ""

    thesis   = _extract_prose(text, "论点")
    risks    = _extract_prose(text, "风险")
    triggers_raw = _extract_lines(text, "触发")
    triggers_list = [
        ln.strip().lstrip("-•·*").strip()
        for ln in triggers_raw.split("\n")
        if ln.strip() and len(ln.strip()) > 5
    ]

    return {
        "ok":      True,
        "code":    d.get("code", ""),
        "comment": text,        # 原始全文，前端可降级显示
        "thesis":  thesis,
        "risks":   risks,
        "triggers": triggers_list,
        "asOf":    d.get("asOf", ""),
    }
