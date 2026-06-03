"""
notify.py — 飞书自定义机器人通知
========================================
替换原项目中全部 Telegram 推送，包括：
  - server.py 中 send_telegram() 所有调用
  - daily_brief.py / weekly_report.py 中的推送
  - 价格/RSI/信号/主题/族群 alert 触发推送

配置方式（二选一）：
  1. 同目录新建 feishu.json：
     {
       "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
       "secret": ""          ← 可选，若机器人开启签名校验则填入
     }
  2. 环境变量：
     FEISHU_WEBHOOK=https://...
     FEISHU_SECRET=...       ← 可选

飞书消息类型：
  - 默认发送 text 纯文本（含 emoji），与原 Telegram 行为一致
  - 若需卡片样式，可把 send_feishu_card() 用于替代
  - 原 Markdown 的 *粗体* 标记自动清除，飞书 text 不支持 Markdown

机器人配置步骤（飞书群 → 设置 → 机器人 → 添加机器人 → 自定义机器人）：
  1. 复制 Webhook URL 填入 feishu.json
  2. 若开启「签名校验」，将密钥填入 "secret" 字段
  3. 在 server.py 工具栏「🔔 飞书」按钮可直接设定，无需手动创建 feishu.json
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

# ============================================================================
# 配置
# ============================================================================
_CONFIG_FILE = Path(__file__).resolve().parent / "feishu.json"


def load_feishu_config() -> dict:
    """读取飞书配置（feishu.json 优先，其次环境变量）。"""
    cfg: dict = {"webhook": "", "secret": ""}
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            cfg["webhook"] = data.get("webhook", "")
            cfg["secret"]  = data.get("secret", "")
        except Exception:
            pass
    # 环境变量可覆盖
    cfg["webhook"] = cfg["webhook"] or os.environ.get("FEISHU_WEBHOOK", "")
    cfg["secret"]  = cfg["secret"]  or os.environ.get("FEISHU_SECRET", "")
    return cfg


def save_feishu_config(webhook: str, secret: str = "") -> None:
    """从前端 /api/feishu POST 时调用，持久化配置。"""
    _CONFIG_FILE.write_text(
        json.dumps(
            {"webhook": webhook.strip(), "secret": secret.strip()},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def is_configured() -> bool:
    return bool(load_feishu_config().get("webhook"))


# ============================================================================
# 签名生成（机器人开启签名校验时必须）
# ============================================================================

def _gen_sign(secret: str, timestamp: int) -> str:
    """
    飞书签名算法：
      string_to_sign = timestamp + "\n" + secret
      sign = base64(hmac-sha256(string_to_sign))
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    import base64
    return base64.b64encode(hmac_code).decode("utf-8")


# ============================================================================
# 核心发送函数
# ============================================================================

def send_feishu(text: str, webhook: Optional[str] = None,
                secret: Optional[str] = None) -> bool:
    """
    发送纯文本消息到飞书群机器人。

    Parameters
    ----------
    text    : 要发送的文本（支持 emoji，自动去除 Markdown */_）
    webhook : Webhook URL，None 时从配置读取
    secret  : 签名密钥，None 时从配置读取；未开启签名校验时留空

    Returns
    -------
    bool : True = 发送成功
    """
    cfg = load_feishu_config()
    wh  = (webhook or cfg.get("webhook", "")).strip()
    sk  = (secret  or cfg.get("secret",  "")).strip()

    if not wh:
        print("[feishu] webhook 未配置，跳过推送")
        return False

    # 清理 Markdown 标记（飞书 text 类型不支持 *粗体*/_斜体_）
    plain = text.replace("*", "").replace("_", "")

    # 构造 payload
    payload: dict = {
        "msg_type": "text",
        "content":  {"text": plain},
    }

    # 签名校验（若配置了 secret）
    if sk:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"]      = _gen_sign(sk, ts)

    try:
        r = requests.post(wh, json=payload, timeout=10)
        r.raise_for_status()
        resp = r.json()
        # 飞书成功响应: {"StatusCode":0} 或 {"code":0}
        ok = (resp.get("StatusCode", resp.get("code", -1)) == 0)
        if not ok:
            print(f"[feishu] 发送失败: {resp}")
        return ok
    except Exception as e:
        print(f"[feishu] 请求异常: {e}")
        return False


def send_feishu_card(title: str, content: str,
                     color: str = "blue",
                     webhook: Optional[str] = None,
                     secret: Optional[str] = None) -> bool:
    """
    发送飞书卡片消息（富文本，视觉效果更好）。
    适用于日报/周报等内容较长的场景。

    Parameters
    ----------
    title   : 卡片标题
    content : 正文 Markdown（飞书卡片支持部分 Markdown）
    color   : 标题栏颜色 blue/green/yellow/red/orange/purple/turquoise/indigo
    """
    cfg = load_feishu_config()
    wh  = (webhook or cfg.get("webhook", "")).strip()
    sk  = (secret  or cfg.get("secret",  "")).strip()

    if not wh:
        print("[feishu] webhook 未配置，跳过卡片推送")
        return False

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title":    {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag":     "lark_md",
                        "content": content,
                    },
                }
            ],
        },
    }

    if sk:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"]      = _gen_sign(sk, ts)

    try:
        r = requests.post(wh, json=payload, timeout=10)
        r.raise_for_status()
        resp = r.json()
        ok   = (resp.get("StatusCode", resp.get("code", -1)) == 0)
        if not ok:
            print(f"[feishu card] 发送失败: {resp}")
        return ok
    except Exception as e:
        print(f"[feishu card] 请求异常: {e}")
        return False


def test_connection() -> bool:
    """发送一条测试消息，用于 /api/feishu POST 验证连通性。"""
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return send_feishu(f"✅ 美股情报站 飞书机器人连线测试成功\n时间: {now_str}")
