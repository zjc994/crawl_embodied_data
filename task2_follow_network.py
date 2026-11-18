# -*- coding: utf-8 -*-
"""
任务二：具身智能领域粉丝数 Top10 大V 及其关注网络（一次性运行即可）

用法：python task2_follow_network.py
流程：
1. 用具身智能关键词搜索 HF 的 models/spaces，收集候选作者（组织/个人），
   并补充一份领域内知名账号种子列表
2. 逐个统计候选账号的粉丝数（分页统计，上限 50000，超大账号标记 capped）
3. 取粉丝数 Top10 为"大V"
4. 采集每个大V的粉丝（关注了他们的人，上限 10000）和关注列表（他们关注了谁，
   仅个人账号，组织无法关注别人，上限 2000）
5. 入库 ./hf_data.db（vips / network_nodes / follow_edges / network_stats），
   统计入度/出度/入度+出度最多的人并打印

说明：有向边 (src, dst) 表示 src 关注 dst；入度=被多少人关注，出度=关注了多少人。
"""
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hf_data.db")
LOG_PATH = os.path.join(BASE_DIR, "task2_progress.log")
# 国内网络/代理访问 huggingface.co 经常被断（ProxyError / 502），连不上时自动切镜像。
# 也可以用环境变量 HF_ENDPOINT 指定自己的端点，指定了就不会自动切。
UPSTREAM = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"
_ENDPOINT = {"base": (os.environ.get("HF_ENDPOINT") or UPSTREAM).rstrip("/")}


def api(path):
    """拼 API 地址（跟随当前端点，可能在镜像和官方之间切换）"""
    return f"{_ENDPOINT['base']}/api{path}"


def switch_to_mirror():
    """把端点切到镜像，返回是否发生了切换"""
    if _ENDPOINT["base"] != UPSTREAM or os.environ.get("HF_ENDPOINT"):
        return False
    _ENDPOINT["base"] = MIRROR
    return True
HEADERS = {"User-Agent": "Mozilla/5.0 (embodied-network-crawler/1.0)"}

# ---------------- 可调参数 ----------------
TOP_VIP_N = 10               # 取粉丝数前10的大V
COUNT_MAX_PAGES = 50         # 统计粉丝数时最多翻 50 页(每页1000)=50000
EDGES_MAX_PER_VIP = 10000    # 每个大V最多采集多少条粉丝关系
FOLLOWING_MAX = 2000         # 大V(个人账号)最多采集多少条关注关系
PAGE_LIMIT = 1000
REQUEST_SLEEP = 0.3   # 镜像对高频请求会 429，调太快反而更慢

# 领域门槛（用"具身仓库占该账号仓库的比例"判定，单看绝对数会把量化转载号也算进来）：
# - 组织账号：具身仓库 ≥3 且占比 ≥2%（nvidia 74/1123、allenai 30/1003 这类大厂能过）
# - 个人账号：占比 ≥30%（个人账号仓库少而专，mradermacher 12/10000 这种过不了）
# - 种子账号：只要名下有具身仓库就放行（physical-intelligence 只有 1 个仓库但是领域核心）
MIN_EMBODIED_REPOS = 3
MIN_EMBODIED_RATIO = 0.02
MIN_PERSONAL_RATIO = 0.30
# 保证 Top10 里至少有几个个人账号：组织账号在 HF 上不能关注别人，出度恒为 0，
# 个人账号太少时"出度最多"这一项就没有样本，所以从领域个人账号里按粉丝补齐
PERSONAL_FILL_MIN = 3

# 具身智能领域关键词（用于发现候选作者，与任务三一致的判定标准）
EMBODIED_KEYWORDS = [
    "embodied", "embodiment", "embodied-ai", "embodied-agent",
    "vision-language-action", "vla", "humanoid", "manipulation",
    "grasping", "locomotion", "robot", "robotics", "robot-learning",
    "lerobot", "quadruped", "legged",
]

# 领域内知名账号种子（存在才用，不存在自动跳过）
SEED_ACCOUNTS = [
    "physical-intelligence", "lerobot", "unitreerobotics", "cyberorigin",
    "AgiBotTech", "OpenDriveLab", "galbot", "OpenGVLab",
]

# 平台官方账号不算领域大V
EXCLUDE_ACCOUNTS = {"huggingface"}

SEED_SET = {s.lower() for s in SEED_ACCOUNTS}

BJT = timezone(timedelta(hours=8))


def log(msg):
    """进度同时打到控制台和日志文件（后台运行时可监控）"""
    print(msg, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def hf_get(url, params=None, retries=6):
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code in (401, 403, 404):
                return None
            if r.status_code == 429:
                # 镜像/官方都会限流，优先听 Retry-After
                wait = float(r.headers.get("Retry-After") or 5)
                print(f"  触发限流(429)，等待 {wait:.0f}s 后重试")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            # 连不上官方站点时切镜像，并把本条请求的 host 换掉再试
            if url.startswith(UPSTREAM) and switch_to_mirror():
                print(f"  {UPSTREAM} 连不上（{type(e).__name__}），切换到镜像 {MIRROR}")
                log(f"[网络] huggingface.co 不可达，已切换到镜像 {MIRROR}")
                url = MIRROR + url[len(UPSTREAM):]
                continue
            wait = 3 * (i + 1)
            print(f"  请求失败({i + 1}/{retries})：{e}，{wait}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"请求最终失败: {url} -> {last_err}")


def next_cursor(resp):
    """解析 Link header 里的下一页 cursor"""
    link = resp.headers.get("Link", "")
    if 'rel="next"' not in link:
        return None
    # <https://.../followers?limit=1000&cursor=xxxx>; rel="next"
    for part in link.split(","):
        if 'rel="next"' in part:
            seg = part.split(";")[0].strip().strip("<>")
            if "cursor=" in seg:
                return seg.split("cursor=")[1]
    return None


def followers_endpoint(name, is_org):
    kind = "organizations" if is_org else "users"
    return api(f"/{kind}/{name}/followers")


def probe_account(name):
    """探测账号类型：org / user / 不存在"""
    for is_org in (True, False):
        r = hf_get(followers_endpoint(name, is_org), params={"limit": PAGE_LIMIT})
        if r is not None:
            return is_org, r
    return None, None


def kw_match(text):
    """文本是否命中具身智能关键词（与任务三同一套判定规则）"""
    if not text:
        return None
    low = text.lower()
    for kw in EMBODIED_KEYWORDS:
        if kw in ("robot", "robotic", "vla"):
            if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low):
                return kw
        elif kw in low:
            return kw
    return None


def list_author_repos(name, repo_type):
    """分页列出某账号名下的全部仓库（models / spaces）"""
    items, cursor = [], None
    for _ in range(10):  # 最多 10 页 × 1000
        params = {"author": name, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = hf_get(api(f"/{repo_type}"), params=params)
        if r is None:
            break
        items.extend(r.json())
        cursor = next_cursor(r)
        if not cursor:
            break
        time.sleep(REQUEST_SLEEP)
    return items


def real_embodied_repo_count(name, early_exit=800):
    """账号名下真实的具身仓库数（按仓库名/标签判定），返回 (具身数, 仓库总数)

    这跟"关键词搜索命中次数"是两回事：后者受 search 的 limit 和排序影响，
    microsoft 这类通用账号也能靠 Phi/Azure 相关结果混进候选池。

    early_exit：仓库极多但具身占比极低（量化转载号动辄上万仓库）时提前收工，
    反正过不了门槛，没必要把 1 万个仓库全翻完。
    """
    hit = total = 0
    for repo_type in ("models", "spaces"):
        for it in list_author_repos(name, repo_type):
            total += 1
            rid = it.get("id") or ""
            short = rid.split("/")[-1]
            tags = " ".join(it.get("tags") or [])
            if kw_match(short) or kw_match(tags):
                hit += 1
            if total >= early_exit and hit / total < MIN_EMBODIED_RATIO / 2:
                return hit, total
    return hit, total


def iter_followers(name, is_org, max_items, max_pages=COUNT_MAX_PAGES):
    """分页拉取粉丝列表，返回 (items, total_count, capped)"""
    items, url, params, pages = [], followers_endpoint(name, is_org), {"limit": PAGE_LIMIT}, 0
    cursor = None
    while pages < max_pages:
        if cursor:
            params["cursor"] = cursor
        r = hf_get(url, params=params)
        if r is None:
            break
        pages += 1
        batch = r.json()
        items.extend(batch)
        if len(items) >= max_items:
            items = items[:max_items]
            return items, None, True
        cursor = next_cursor(r)
        if cursor is None:
            return items, len(items), False
        time.sleep(REQUEST_SLEEP)
    return items, None, True  # 超过页数上限


def iter_following(name, max_items):
    """个人账号的关注列表（组织无此能力）"""
    items, params, pages = [], {"limit": PAGE_LIMIT if PAGE_LIMIT >= 10 else 10}, 0
    # following 端点 limit 最小 10
    params["limit"] = min(PAGE_LIMIT, 1000)
    cursor = None
    while pages < 50:
        if cursor:
            params["cursor"] = cursor
        r = hf_get(api(f"/users/{name}/following"), params=params)
        if r is None:
            break
        pages += 1
        items.extend(r.json())
        if len(items) >= max_items:
            return items[:max_items]
        cursor = next_cursor(r)
        if cursor is None:
            break
        time.sleep(REQUEST_SLEEP)
    return items


def discover_candidates():
    """关键词搜索 models/spaces，收集作者作为候选大V"""
    authors = {}
    for kw in EMBODIED_KEYWORDS:
        for repo_type in ("models", "spaces"):
            r = hf_get(api(f"/{repo_type}"),
                       params={"search": kw, "limit": 100, "sort": "likes", "direction": "-1"})
            if r is None:
                continue
            for it in r.json():
                rid = it.get("id") or it.get("modelId") or ""
                author = rid.split("/")[0] if "/" in rid else ""
                if not author:
                    continue
                key = author.lower()
                if key not in authors:
                    authors[key] = {"name": author, "repo_count": 0}
                authors[key]["repo_count"] += 1
            time.sleep(REQUEST_SLEEP)
    for seed in SEED_ACCOUNTS:
        authors.setdefault(seed.lower(), {"name": seed, "repo_count": 0})
    for ex in EXCLUDE_ACCOUNTS:
        authors.pop(ex, None)
    # 粗筛：关键词搜索命中 ≥3 次的作者（+种子），只为缩小后面要核对的候选池。
    # 真正的领域判定放在 real_embodied_repo_count()，这里的 repo_count 不能当门槛用。
    # 门槛从 2 提到 3 是因为每个候选要翻 2~4 页仓库，镜像限流，238 个跑不完。
    filtered = [a for a in authors.values() if a["repo_count"] >= 3 or a["name"].lower() in SEED_SET]
    print(f"[候选] 关键词+种子共发现 {len(authors)} 个账号，粗筛后 {len(filtered)} 个进入领域核对")
    return filtered


def init_db(conn):
    cur = conn.cursor()
    # 这几张都是每次重算的派生表，直接重建，避免旧库字段对不上
    for t in ("vips", "network_nodes", "follow_edges", "network_stats"):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.execute("""CREATE TABLE vips (
        username TEXT PRIMARY KEY, account_type TEXT, fullname TEXT,
        followers INTEGER, followers_capped INTEGER, rank INTEGER,
        embodied_repo_count INTEGER, avatar_url TEXT,
        selection TEXT, edges_count INTEGER, edges_truncated INTEGER)""")
    cur.execute("""CREATE TABLE network_nodes (
        name TEXT PRIMARY KEY, account_type TEXT, fullname TEXT, avatar_url TEXT)""")
    cur.execute("""CREATE TABLE follow_edges (
        src TEXT NOT NULL, dst TEXT NOT NULL,
        PRIMARY KEY(src, dst))""")
    cur.execute("""CREATE TABLE network_stats (
        name TEXT PRIMARY KEY, account_type TEXT, in_degree INTEGER,
        out_degree INTEGER, total_degree INTEGER)""")
    conn.commit()


def is_domain_account(name, is_org, hit, total):
    """按"具身仓库占比 + 账号类型"判定是不是具身领域账号"""
    if total <= 0 or hit <= 0:
        # 种子账号即使名下没有仓库解析到（比如仓库数量为 0）也先放行，后面 probe 会筛掉不存在的
        return name.lower() in SEED_SET and total == 0
    if name.lower() in SEED_SET:
        return True
    ratio = hit / total
    if is_org:
        return hit >= MIN_EMBODIED_REPOS and ratio >= MIN_EMBODIED_RATIO
    return ratio >= MIN_PERSONAL_RATIO


def main():
    fetched_at = datetime.now(timezone.utc).astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[任务二] 具身智能大V关注网络采集，北京时间 {fetched_at}")

    candidates = discover_candidates()

    # ---- 第一步：领域门槛（账号类型 + 具身仓库占比）----
    log("[步骤1] 核对账号类型与具身仓库占比 ...")
    gated = []
    for i, cand in enumerate(candidates, 1):
        name = cand["name"]
        try:
            is_org, first_page = probe_account(name)
            if is_org is None:
                continue  # 账号不存在
            hit, total_repos = real_embodied_repo_count(name)
            ok = is_domain_account(name, is_org, hit, total_repos)
        except Exception as e:
            log(f"  ({i}/{len(candidates)}) {name:<35} 核对失败，跳过：{e}")
            continue
        if ok:
            gated.append({"name": name, "is_org": is_org, "first_page": first_page,
                          "embodied": hit, "total_repos": total_repos})
        if i % 10 == 0:
            log(f"  ... 已核对 {i}/{len(candidates)}，通过 {len(gated)} 个")
    log(f"  领域门槛通过 {len(gated)} / {len(candidates)} 个账号"
        f"（个人账号 {sum(1 for g in gated if not g['is_org'])} 个）")
    if not gated:
        log("[错误] 没有账号通过领域门槛，检查关键词或网络")
        return

    # ---- 第二步：统计领域账号粉丝数 ----
    log("[步骤2] 统计领域账号粉丝数 ...")
    counted = []
    for i, cand in enumerate(gated, 1):
        name = cand["name"]
        try:
            is_org = cand["is_org"]
            first_page = cand["first_page"]
            batch = first_page.json()
            cursor = next_cursor(first_page)
            if cursor is None and len(batch) < PAGE_LIMIT:
                total, capped = len(batch), 0
            else:
                # 需要翻页，继续
                items, pages = batch[:], 1
                while cursor is not None and pages < COUNT_MAX_PAGES:
                    params = {"limit": PAGE_LIMIT, "cursor": cursor}
                    r = hf_get(followers_endpoint(name, is_org), params=params)
                    if r is None:
                        break
                    items.extend(r.json())
                    pages += 1
                    cursor = next_cursor(r)
                    time.sleep(REQUEST_SLEEP)
                total, capped = len(items), 1 if cursor is not None else 0
            counted.append({
                "name": name, "is_org": is_org, "followers": total,
                "capped": capped, "repo_count": cand["embodied"],
                "total_repos": cand["total_repos"],
                "fullname": "", "avatar": "",
            })
            log(f"  ({i}/{len(gated)}) {name:<35} 粉丝 {total}{'+' if capped else ''}"
                f"  具身仓库 {cand['embodied']}")
        except Exception as e:
            log(f"  ({i}/{len(gated)}) {name:<35} 统计失败，跳过：{e}")
            continue
        time.sleep(REQUEST_SLEEP)

    if not counted:
        log("[错误] 没有任何账号统计成功（网络/代理问题？），稍后重试")
        return

    counted.sort(key=lambda x: (-x["followers"], x["name"]))
    vips = [dict(v, selection="followers") for v in counted[:TOP_VIP_N]]

    # 个人账号补齐：HF 上组织账号不能关注别人，出度恒为 0。
    # 个人账号太少时"出度最多"就只剩粉丝之间的边（上限只有大V个数），榜单失去意义。
    personal_pool = [c for c in counted
                     if not c["is_org"] and c["name"] not in {v["name"] for v in vips}]
    need = PERSONAL_FILL_MIN - sum(1 for v in vips if not v["is_org"])
    if need > 0:
        for p in personal_pool[:need]:
            idx = next((i for i in range(len(vips) - 1, -1, -1) if vips[i]["is_org"]), None)
            if idx is None:
                break
            dropped = vips[idx]
            vips[idx] = dict(p, selection="personal_fill")
            log(f"  [补齐] 个人账号 {p['name']}（粉丝 {p['followers']}）替换组织账号 "
                f"{dropped['name']}（粉丝 {dropped['followers']}），使「出度」有样本可采")
    vips.sort(key=lambda x: (-x["followers"], x["name"]))

    print(f"[步骤3] 具身智能领域粉丝数 Top{TOP_VIP_N} 大V：")
    for rank, v in enumerate(vips, 1):
        v["rank"] = rank
        tag = "" if v["selection"] == "followers" else "  [个人账号补齐]"
        print(f"  #{rank:<2} {v['name']:<35} {v['is_org'] and 'org ' or 'user'} "
              f"粉丝 {v['followers']}{'+' if v['capped'] else ''}  "
              f"具身仓库 {v['repo_count']}{tag}")

    # ---- 第二步：采集大V的粉丝与关注 ----
    print("[步骤3] 采集大V关注网络（粉丝+关注）...")
    nodes = {}
    edges = set()

    def add_node(entry, default_type="user"):
        if not entry:
            return
        uname = entry.get("user") or entry.get("name")
        if not uname:
            return
        if uname not in nodes:
            nodes[uname] = {
                "name": uname,
                "type": entry.get("type", default_type),
                "fullname": entry.get("fullname") or "",
                "avatar": entry.get("avatarUrl") or "",
            }

    for rank, v in enumerate(vips, 1):
        name, is_org = v["name"], v["is_org"]
        v["rank"] = rank
        log(f"  大V #{rank} {name}:")
        try:
            # 粉丝：follower -> name
            items, _, capped = iter_followers(name, is_org, max_items=EDGES_MAX_PER_VIP)
            for it in items:
                add_node(it)
                edges.add((it.get("user"), name))
            v["edges_count"] = len(items)
            v["edges_truncated"] = 1 if capped else 0
            if not capped:
                # 粉丝数与入边用同一次快照，避免"真实粉丝 5931 / 图内入度 5932"这种自相矛盾
                v["followers"] = len(items)
            log(f"    粉丝(被关注, 入边) {len(items)} 条{'（已截断，仅保留前 %d）' % EDGES_MAX_PER_VIP if capped else ''}")
        except Exception as e:
            v["edges_count"], v["edges_truncated"] = 0, 1
            log(f"    [警告] 粉丝采集失败，该大V的入边不完整：{e}")
            items = []
        # 关注：name -> followee（仅个人账号）
        if not is_org:
            try:
                fitems = iter_following(name, max_items=FOLLOWING_MAX)
                for it in fitems:
                    add_node(it)
                    edges.add((name, it.get("user")))
                log(f"    关注(出边) {len(fitems)} 条")
            except Exception as e:
                log(f"    关注采集失败：{e}")
        else:
            log(f"    关注(出边) 0 条（组织账号在 HF 上不能关注别人）")
        # 大V节点自身信息
        nodes[name] = {"name": name, "type": "org" if is_org else "user",
                       "fullname": v.get("fullname", ""), "avatar": v.get("avatar", "")}

    # ---- 第三步：度数统计 ----
    indeg, outdeg = defaultdict(int), defaultdict(int)
    for src, dst in edges:
        outdeg[src] += 1
        indeg[dst] += 1
    stats = {}
    for n in set(list(indeg) + list(outdeg) + [v["name"] for v in vips]):
        i_d, o_d = indeg.get(n, 0), outdeg.get(n, 0)
        stats[n] = (i_d, o_d, i_d + o_d)  # (入度, 出度, 总度)
    print(f"[统计] 网络共 {len(nodes)} 个节点、{len(edges)} 条关注边")

    def top_by(key_idx, label):
        ranked = sorted(stats.items(), key=lambda kv: (-kv[1][key_idx], kv[0]))[:5]
        print(f"  {label}:")
        for n, d in ranked:
            print(f"    {n:<35} 入度={d[0]} 出度={d[1]} 总度={d[2]}")

    print("[步骤4] 度数排行 Top5：")
    top_by(0, "入度最多（被最多人关注）")
    top_by(1, "出度最多（关注最多人）")
    top_by(2, "入度+出度最多")

    # ---- 入库 ----
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        cur = conn.cursor()
        for v in vips:
            cur.execute("INSERT OR REPLACE INTO vips VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (v["name"], "org" if v["is_org"] else "user", v.get("fullname", ""),
                         v["followers"], v["capped"], v["rank"], v.get("repo_count", 0),
                         v.get("avatar", ""), v.get("selection", "followers"),
                         v.get("edges_count", 0), v.get("edges_truncated", 0)))
        for n, info in nodes.items():
            cur.execute("INSERT OR REPLACE INTO network_nodes VALUES (?,?,?,?)",
                        (n, info["type"], info["fullname"], info["avatar"]))
        cur.executemany("INSERT OR IGNORE INTO follow_edges VALUES (?,?)", sorted(edges))
        for n, (ind, outd, tot) in stats.items():
            cur.execute("INSERT OR REPLACE INTO network_stats VALUES (?,?,?,?,?)",
                        (n, nodes.get(n, {}).get("type", "user"), ind, outd, tot))
        conn.commit()
        print(f"[数据库] 已写入 {DB_PATH}（vips={len(vips)}, nodes={len(nodes)}, edges={len(edges)}）")
    finally:
        conn.close()
    print("完成。运行 build_website.py 生成网页。")


if __name__ == "__main__":
    main()
