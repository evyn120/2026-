"""每周周报 — 美股情报站

执行: python weekly_report.py
排程: Task Scheduler 每周日 18:00 触发

内容:
  1. 一周主题轮动 Top 3 + Bottom 3 (动量)
  2. watchlist 过去一周涨跌 Top 5 / Bottom 5
  3. 持仓周表现 (持股、加权报酬、最强/最弱)
  4. 下周 holdings 财报日
  5. 过去 7 天触发的警示统计

需先设定 telegram.json (与 daily_brief.py 同档)。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

import requests

ROOT = Path(__file__).resolve().parent
TELE_FILE = ROOT / "feishu.json"
SERVER_URL = os.environ.get("US_INTEL_URL", "http://localhost:18506")
TIMEOUT = 60


def ensure_server_running(max_wait: int = 60) -> bool:
    """确保 server.py 已启动。没有就在背景启动并等就绪。"""
    # 已在跑就直接回
    try:
        r = requests.get(f"{SERVER_URL}/api/stocks", timeout=5)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    # 拉起 server (pythonw 无视窗背景跑)
    server_py = ROOT / "server.py"
    if not server_py.exists():
        print(f"[ensure_server] {server_py} 不存在")
        return False
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    py_exe = str(pythonw) if pythonw.exists() else sys.executable
    try:
        subprocess.Popen(
            [py_exe, str(server_py)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                          getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print(f"[ensure_server] 已启动 {server_py.name}, 等待 ready…")
    except Exception as e:
        print(f"[ensure_server] 启动失败: {e}")
        return False
    # 等就绪
    for i in range(max_wait):
        time.sleep(1)
        try:
            r = requests.get(f"{SERVER_URL}/api/stocks", timeout=3)
            if r.status_code == 200:
                print(f"[ensure_server] server 在 {i+1}s 后就绪")
                return True
        except Exception:
            continue
    print(f"[ensure_server] 等了 {max_wait}s server 还没就绪")
    return False

def load_telegram():
    try:
        return json.loads(TELE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
import hashlib, hmac, base64, time as _time

def _feishu_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")

def send_telegram(text: str) -> bool:
    """名称保留以兼容 main() 调用; 实际通过飞书 Webhook 发送。"""
    cfg = load_telegram()
    webhook = cfg.get("webhook", "")
    secret = cfg.get("secret", "")
    if not webhook:
        print("[daily_brief] feishu.json 未设定 webhook")
        return False
    plain = text.replace("*", "").replace("_", "")
    payload = {"msg_type": "text", "content": {"text": plain}}
    if secret:
        ts = int(_time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _feishu_sign(secret, ts)
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[daily_brief] feishu {r.status_code}: {r.text[:200]}")
            return False
        data = r.json()
        if data.get("code", 0) != 0 and data.get("StatusCode", 0) != 0:
            print(f"[daily_brief] feishu 返回错误: {data}")
            return False
        return True
    except Exception as e:
        print(f"[daily_brief] feishu fail: {e}")
        return False

def api(path: str, timeout: int = TIMEOUT):
    r = requests.get(f"{SERVER_URL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def section_themes():
    try:
        themes = api("/api/theme-rotation", 60)
    except Exception as e:
        return f"🎨 *主题轮动* — 失败 ({e})\n"
    if not themes:
        return "🎨 *主题轮动* — 无资料\n"
    top = sorted(themes, key=lambda x: -x.get("ret_1w", 0))[:3]
    bot = sorted(themes, key=lambda x: x.get("ret_1w", 0))[:3]
    lines = ["🎨 *本周主题轮动*", "  📈 *最强*:"]
    for t in top:
        lines.append(f"    +{t['ret_1w']:.2f}% — *{t['theme']}* ({t['n']} 档)")
    lines.append("  📉 *最弱*:")
    for t in bot:
        lines.append(f"    {t['ret_1w']:+.2f}% — *{t['theme']}* ({t['n']} 档)")
    return "\n".join(lines) + "\n"


def section_movers():
    try:
        stocks = api("/api/stocks", 30)
    except Exception as e:
        return f"📊 *涨跌榜* — 失败 ({e})\n"
    movers = []
    for s in stocks:
        prev = s.get("prev") or 0
        if not prev: continue
        chg = (s["price"] - prev) / prev * 100
        movers.append((chg, s))
    movers.sort(key=lambda x: -x[0])
    top, bot = movers[:5], movers[-5:][::-1]
    lines = ["", "📊 *本周涨跌榜 (vs 昨收;近似周度)*", "  🚀 *Top 5*:"]
    for chg, s in top:
        lines.append(f"    *{s['code']}* {chg:+.2f}% — {s['name']} (${s['price']:.2f})")
    lines.append("  ⚠️ *Bottom 5*:")
    for chg, s in bot:
        lines.append(f"    *{s['code']}* {chg:+.2f}% — {s['name']} (${s['price']:.2f})")
    return "\n".join(lines) + "\n"


def section_portfolio():
    try:
        pf = api("/api/portfolio", 30)
    except Exception as e:
        return f"💼 *持仓* — 失败 ({e})\n"
    holdings = pf.get("holdings", [])
    summary  = pf.get("summary", {})
    if not holdings:
        return "💼 *持仓* — 无资料\n"
    lines = ["", "💼 *持仓周表现*"]
    pnl = summary.get("total_pnl", 0)
    pnl_pct = summary.get("total_pnl_pct", 0)
    val = summary.get("total_value", 0)
    lines.append(f"  总市值: *${val:,.0f}* · 未实现: *{pnl:+,.0f} ({pnl_pct:+.2f}%)*")
    # Top 5 by pnl_pct
    sorted_h = sorted(holdings, key=lambda x: -x.get("pnl_pct", 0))
    if len(sorted_h) >= 2:
        best, worst = sorted_h[0], sorted_h[-1]
        lines.append(f"  💎 *最强*: {best['code']} {best.get('pnl_pct', 0):+.2f}%")
        lines.append(f"  📉 *最弱*: {worst['code']} {worst.get('pnl_pct', 0):+.2f}%")
    return "\n".join(lines) + "\n"


def section_earnings():
    try:
        ec = api("/api/earnings-calendar?days=10", 60)
    except Exception as e:
        return ""
    soon = [r for r in ec if r.get("days_to") is not None and 0 <= r["days_to"] <= 10]
    if not soon:
        return ""
    lines = ["", "📅 *下周 (10 天内) 财报*"]
    for r in soon[:8]:
        d = r.get("days_to")
        lines.append(f"  +{d}d *{r['code']}* — {r.get('earnings_date', '?')}")
    return "\n".join(lines) + "\n"


def section_alerts_review():
    try:
        log = api("/api/alerts-log?days=7", 60)
    except Exception:
        return ""
    entries = log.get("entries", [])
    stats   = log.get("stats", {})
    if not entries:
        return ""
    lines = ["", f"📜 *本周警示触发 {len(entries)} 次*"]
    for k, st in stats.items():
        wr = st.get("win_rate_5d")
        avg = st.get("avg_5d")
        if wr is None: continue
        lines.append(f"  *{k}*: {st['n']} 次 · 5d 胜率 {wr}% · 平均 {avg:+.2f}%")
    return "\n".join(lines) + "\n"


def main():
    if not load_telegram():
        print("[weekly_report] telegram.json 没设,放弃")
        sys.exit(0)
    if not ensure_server_running():
        send_telegram("⚠️ *美股周报失败* — 无法启动 server,请检查")
        sys.exit(1)
    now = datetime.now()
    header = f"📈 *美股周报 — {now.strftime('%Y/%m/%d (%A)')}*\n"
    body = "\n".join([
        header,
        section_themes(),
        section_movers(),
        section_portfolio(),
        section_earnings(),
        section_alerts_review(),
        f"_由 weekly_report.py {now.strftime('%H:%M')} 自动产生_",
    ])
    if len(body) > 4000:
        body = body[:3900] + "\n\n_…内容过长已截断_"
    ok = send_telegram(body)
    print(f"[weekly_report] sent={ok}, length={len(body)}")


if __name__ == "__main__":
    main()
