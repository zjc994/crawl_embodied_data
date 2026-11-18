# -*- coding: utf-8 -*-
"""
任务一：爬取 HuggingFace 每日 Trending（models / datasets / spaces 各前 10）

用法：每天运行一次  python task1_trending.py
- 按"北京时间"判断同一天：同一天多次运行会覆盖上一次的备份和数据库记录
- 备份保存在 ./backup/trending_YYYY-MM-DD.json（北京时间日期）
- 数据写入 ./hf_data.db 的 trending_snapshots 表，供 build_website.py 生成网页

注意：本机时区不是北京时间没关系，统一用 UTC+8 计算"当天"。
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hf_data.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")

# 国内网络/代理访问 huggingface.co 经常被断（ProxyError / 502），连不上时自动切镜像。
# 也可以用环境变量 HF_ENDPOINT 指定端点，指定了就不会自动切。
UPSTREAM = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"
_ENDPOINT = {"base": (os.environ.get("HF_ENDPOINT") or UPSTREAM).rstrip("/")}


def api(path):
    return f"{_ENDPOINT['base']}/api{path}"


def switch_to_mirror():
    if _ENDPOINT["base"] != UPSTREAM or os.environ.get("HF_ENDPOINT"):
        return False
    _ENDPOINT["base"] = MIRROR
    return True


REPO_TYPES = ["models", "datasets", "spaces"]  # 网页对应 ?sort=trending
TOP_N = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (trending-crawler/1.0)"}

# 北京时间 = UTC+8，与电脑本地时区无关
BJT = timezone(timedelta(hours=8))


def beijing_now():
    return datetime.now(timezone.utc).astimezone(BJT)


def beijing_today_str():
    return beijing_now().strftime("%Y-%m-%d")


def hf_get(url, params=None, retries=5):
    """带重试的 GET（本机代理偶尔会重置连接）"""
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if url.startswith(UPSTREAM) and switch_to_mirror():
                print(f"  {UPSTREAM} 连不上（{type(e).__name__}），切换到镜像 {MIRROR}")
                url = MIRROR + url[len(UPSTREAM):]
                continue
            wait = 2 * (i + 1)
            print(f"  请求失败({i + 1}/{retries})：{e}，{wait}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"请求最终失败: {url} -> {last_err}")


def fetch_trending(repo_type):
    """抓取某类仓库的 trending 前 TOP_N（对应网页 ?sort=trending）"""
    params = {"sort": "trendingScore", "direction": "-1", "limit": TOP_N}
    items = hf_get(api(f"/{repo_type}"), params)
    rows = []
    for rank, it in enumerate(items[:TOP_N], start=1):
        repo_id = it.get("id") or it.get("modelId")
        rows.append({
            "rank": rank,
            "repo_id": repo_id,
            "author": repo_id.split("/")[0] if "/" in repo_id else repo_id,
            "likes": it.get("likes", 0) or 0,
            "downloads": it.get("downloads", 0) or 0,
            "trending_score": it.get("trendingScore"),
            "url": f"https://huggingface.co/{'spaces/' if repo_type == 'spaces' else ''}{repo_id}",
        })
    return rows


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trending_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            beijing_date TEXT NOT NULL,
            repo_type TEXT NOT NULL,
            rank INTEGER NOT NULL,
            repo_id TEXT NOT NULL,
            author TEXT,
            likes INTEGER DEFAULT 0,
            downloads INTEGER DEFAULT 0,
            trending_score REAL,
            url TEXT,
            fetched_at TEXT,
            UNIQUE(beijing_date, repo_type, rank)
        )
    """)
    conn.commit()


def save_backup(day, data):
    """按天备份：同一天多次运行覆盖，新的一天生成新文件"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"trending_{day}.json")
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    action = "覆盖" if existed else "新建"
    print(f"[备份] {action} {path}")


def save_db(conn, day, fetched_at, data):
    """入库：同一天的数据先删后插（覆盖上一次）"""
    cur = conn.cursor()
    for repo_type, rows in data["repo_types"].items():
        cur.execute("DELETE FROM trending_snapshots WHERE beijing_date=? AND repo_type=?",
                    (day, repo_type))
        for r in rows:
            cur.execute(
                "INSERT INTO trending_snapshots"
                "(beijing_date, repo_type, rank, repo_id, author, likes, downloads,"
                " trending_score, url, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (day, repo_type, r["rank"], r["repo_id"], r["author"], r["likes"],
                 r["downloads"], r["trending_score"], r["url"], fetched_at))
    conn.commit()


def main():
    day = beijing_today_str()
    fetched_at = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[任务一] 抓取 HF Trending（北京时间 {day} {fetched_at}）")

    data = {
        "beijing_date": day,
        "fetched_at": fetched_at,
        "repo_types": {},
    }
    for repo_type in REPO_TYPES:
        print(f"  抓取 {repo_type} trending 前 {TOP_N} ...")
        rows = fetch_trending(repo_type)
        data["repo_types"][repo_type] = rows
        for r in rows:
            print(f"    #{r['rank']:>2} {r['repo_id']}  likes={r['likes']} downloads={r['downloads']}")

    save_backup(day, data)

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        save_db(conn, day, fetched_at, data)
        n = sum(len(v) for v in data["repo_types"].values())
        print(f"[数据库] 已写入/覆盖 {day} 的 {n} 条记录 -> {DB_PATH}")
    finally:
        conn.close()

    print("完成。运行 build_website.py 可生成/更新网页。")


if __name__ == "__main__":
    main()
