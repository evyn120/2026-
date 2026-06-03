"""
项目繁体→简体批量转换工具
用法: python convert_to_simplified.py
会就地修改 .py / .md / .html / .json / .txt 文件, 转换前自动备份到 _backup_traditional/
"""
import shutil
from pathlib import Path
from opencc import OpenCC

ROOT = Path(__file__).resolve().parent
BACKUP = ROOT / "_backup_traditional"
# t2s = 繁体转简体 (含台湾用语→大陆用语可改 tw2sp, 这里用纯字形 t2s 更安全)
cc = OpenCC("t2s")

# 要处理的文件后缀
EXTS = {".py", ".md", ".html", ".htm", ".json", ".txt", ".css", ".js"}
# 跳过的目录
SKIP_DIRS = {"_backup_traditional", ".git", "__pycache__", "node_modules", ".venv", "venv"}

def main():
    BACKUP.mkdir(exist_ok=True)
    changed = 0
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in EXTS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        converted = cc.convert(text)
        if converted != text:
            # 备份原文件 (保持相对路径结构)
            rel = p.relative_to(ROOT)
            bak = BACKUP / rel
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, bak)
            # 就地写回简体
            p.write_text(converted, encoding="utf-8")
            changed += 1
            print(f"[已转换] {rel}")
    print(f"\n完成, 共转换 {changed} 个文件。原文件已备份到 {BACKUP.name}/")

if __name__ == "__main__":
    main()
