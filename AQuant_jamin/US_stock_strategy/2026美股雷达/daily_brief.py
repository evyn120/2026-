"""
每日早报 — 美股情报站

执行: python daily_brief.py
排程: Task Scheduler 每天 8:00 AM 触发
功能:
  1. 总经快照 (VIX/10Y/DXY/2Y) + 灯号
  2. 今日 / 3 天内 watchlist 财报日
  3. 昨日异动大于 3% 的持股
  4. 触发中的 alerts (RSI/讯号)

需先设定 telegram.json (bot_token + chat_id),与 server.py 同目录。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parent
TELE_FILE = ROOT / "feishu.json"
SERVER_URL = os.environ.get("US_INTEL_URL", "http://localhost:18506")
TIMEOUT = 30


def ensure_server_running(max_wait: int = 60) -> bool:
    """确保 server.py 已启动。没有就在背景启动并等就绪。"""
    try:
        r = requests.get(f"{SERVER_URL}/api/stocks", timeout=5)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    server_py = ROOT / "server.py"
    if not server_py.exists():
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
        print(f"[ensure_server] 已启动 server.py, 等待 ready…")
    except Exception as e:
        print(f"[ensure_server] 启动失败: {e}")
        return False
    for i in range(max_wait):
        time.sleep(1)
        try:
            r = requests.get(f"{SERVER_URL}/api/stocks", timeout=3)
            if r.status_code == 200:
                print(f"[ensure_server] server 在 {i+1}s 后就绪")
                return True
        except Exception:
            continue
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


def section_macro() -> str:
    """总经区块"""
    try:
        macro = api("/api/macro", 20)
    except Exception as e:
        return f"🌍 *总经* — 无法取得 ({e})\n"

    icons = {"danger": "🔴", "calm": "🟢", "high": "🟠", "low": "🔵",
             "strong": "🟠", "weak": "🔵", "neutral": "⚪"}
    lines = ["🌍 *总经背景*"]
    flags = []
    for m in macro:
        if m.get("value") is None:
            continue
        ic = icons.get(m.get("status", "neutral"), "⚪")
        chg = m.get("change_pct", 0)
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "·"
        chg_str = f" {arrow}{abs(chg):.1f}%"
        lines.append(f"  {ic} *{m['code']}*: {m['value']:.2f}{m['unit']}{chg_str}")
        # 警示
        if m["code"] == "VIX" and m.get("status") == "danger":
            flags.append("⚠️ VIX 过高,风险偏好降温")
        elif m["code"] == "10Y" and m.get("status") == "high":
            flags.append("⚠️ 10Y > 4.5%,成长股压力大")
    if flags:
        lines.append("")
        lines.extend(flags)
    return "\n".join(lines) + "\n"


def section_earnings() -> str:
    """财报日历 (今日 + 7 天内)"""
    try:
        ec = api("/api/earnings-calendar?days=7", 60)
    except Exception as e:
        return f"📅 *财报* — 无法取得 ({e})\n"

    soon = [r for r in ec if r.get("days_to") is not None and 0 <= r["days_to"] <= 7]
    if not soon:
        return "📅 *未来 7 天无 watchlist 财报*\n"
    lines = ["📅 *未来 7 天财报*"]
    for r in soon[:10]:
        d = r["days_to"]
        marker = "🔔 *TODAY*" if d == 0 else (f"+{d}d" if d > 0 else f"{d}d")
        hist = ""
        if r.get("history"):
            last = r["history"][0]
            sp = last.get("surprise_pct")
            if sp is not None:
                ic = "📈" if sp >= 0 else "📉"
                hist = f" (上季 {ic}{sp:+.1f}%)"
        lines.append(f"  {marker} *{r['code']}* {r.get('name', '')} — {r.get('earnings_date', '?')}{hist}")
    return "\n".join(lines) + "\n"


def section_overnight_movers() -> str:
    """昨晚异动 >3% 的 watchlist 个股"""
    try:
        stocks = api("/api/stocks", 30)
    except Exception as e:
        return f"📊 *异动* — 无法取得 ({e})\n"

    movers = []
    for s in stocks:
        prev = s.get("prev") or 0
        if not prev:
            continue
        chg = (s["price"] - prev) / prev * 100
        if abs(chg) >= 3:
            movers.append((chg, s))
    movers.sort(key=lambda x: -abs(x[0]))
    if not movers:
        return "📊 *昨日异动* — 无 ±3% 以上个股\n"

    lines = ["📊 *昨日异动 (±3%)*"]
    for chg, s in movers[:8]:
        ic = "🚀" if chg > 0 else "⚠️"
        sigs = ""
        if s.get("signals"):
            sigs = " · " + "/".join(x.get("label", "") for x in s["signals"][:2])
        lines.append(f"  {ic} *{s['code']}* {chg:+.2f}% (${s['price']:.2f}){sigs}")
    return "\n".join(lines) + "\n"


def section_profit_taking() -> str:
    """停利警示 — 持股 ≥ 2 条件命中才推"""
    try:
        scan = api("/api/profit-taking-scan", 60)
    except Exception:
        return ""
    warns = [w for w in scan.get("warnings", []) if w.get("hits", 0) >= 2]
    if not warns:
        return ""
    lines = ["", "⚠️ *持仓停利警示*"]
    for w in warns[:5]:
        icon = "🚨" if w["level"] == "critical" else "⚠️"
        lines.append(f"  {icon} *{w['code']}* ({w['hits']}/3 条件) — {w['action']}")
        for cat, reasons in w.get("conditions", {}).items():
            if reasons:
                lines.append(f"    · {cat}: {', '.join(reasons)}")
    return "\n".join(lines) + "\n"


def section_squeeze() -> str:
    """轧空 Top 3 (高分才推)"""
    try:
        sq = api("/api/short-squeeze", 60)
    except Exception:
        return ""

    top = [s for s in sq if s.get("score", 0) >= 40][:3]
    if not top:
        return ""
    lines = ["🔥 *轧空候选 Top 3*"]
    for s in top:
        dtc = s.get("short_ratio")
        dtcStr = f"DTC {dtc:.1f}d" if dtc else "—"
        lines.append(f"  *{s['code']}* ({s['score']} 分) — {dtcStr}, 今日 {s.get('change_pct', 0):+.2f}%")
    return "\n".join(lines) + "\n"


def main():
    if not load_telegram():
        print("[daily_brief] telegram.json 没设,放弃")
        sys.exit(0)
    if not ensure_server_running():
        send_telegram("⚠️ *美股早报失败* — 无法启动 server,请检查")
        sys.exit(1)

    now = datetime.now()
    header = f"☀️ *美股早报 — {now.strftime('%m/%d %A')}*\n"
    body = "\n".join([
        header,
        section_macro(),
        section_profit_taking(),
        section_earnings(),
        section_overnight_movers(),
        section_squeeze(),
        f"_由 daily_brief.py {now.strftime('%H:%M')} 自动产生_",
    ])

    # Telegram 4096 char limit
    if len(body) > 4000:
        body = body[:3900] + "\n\n_…内容过长已截断_"

    ok = send_telegram(body)
    print(f"[daily_brief] sent={ok}, length={len(body)}")


if __name__ == "__main__":
    main()
