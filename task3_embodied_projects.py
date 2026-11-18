# -*- coding: utf-8 -*-
"""
任务三：统计 HuggingFace 最火热的 10 个具身智能项目（一次性运行即可）

用法：python task3_embodied_projects.py
判定标准（纯关键词，不用 LLM）：
  一个项目（model/space/dataset 仓库）被认为属于"具身智能"，当且仅当其
  仓库名 / 标签 / README 中出现具身智能关键词，例如：
  embodied, embodiment, VLA, vision-language-action, robot, robotics,
  humanoid, manipulation, grasping, locomotion, robot-learning, lerobot 等。

热度排名：score = likes*10 + min(downloads, 5000)（下载量封顶，避免纯权重文件霸榜）

对每个项目：
  - 开源 GitHub 链接：从 README 提取候选 -> 用 GitHub API 逐个验证，只有真实
    存在的仓库才算数（README 里的链接本身可能是错的，必须过 API 验证）
  - HuggingFace Space 链接：README 链接 -> 模型卡 cardData.demo -> 作者名下
    space 列表按名字匹配 -> 全局搜同名，每一处都验证存在；确实没有就明确标注
    "无官方 Demo"，不留空

结果写入 ./hf_data.db 的 embodied_projects 表，供 build_website.py 生成网页。
"""
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hf_data.db")
# 国内网络/代理访问 huggingface.co 经常被断（ProxyError / 502），连不上时自动切镜像。
# 也可以用环境变量 HF_ENDPOINT 指定端点，指定了就不会自动切。
UPSTREAM = "https://huggingface.co"
MIRROR = "https://hf-mirror.com"
_ENDPOINT = {"base": (os.environ.get("HF_ENDPOINT") or UPSTREAM).rstrip("/")}


def api(path):
    return f"{_ENDPOINT['base']}/api{path}"


def web(path):
    return f"{_ENDPOINT['base']}{path}"


def switch_to_mirror():
    if _ENDPOINT["base"] != UPSTREAM or os.environ.get("HF_ENDPOINT"):
        return False
    _ENDPOINT["base"] = MIRROR
    return True
HEADERS = {"User-Agent": "Mozilla/5.0 (embodied-project-crawler/1.0)"}
TOP_N = 10
SEARCH_LIMIT = 100
REQUEST_SLEEP = 0.1
DOWNLOAD_CAP = 5000        # 下载量计分封顶：resnet10 这类权重文件下载 2.5 万，不封顶会霸榜
SAME_AUTHOR_MAX = 3        # 同一个 HF 作者最多入选几个，保证榜单多样性
GH_CANDIDATE_MAX = 4       # 每个项目最多验证几个 GitHub 候选（未认证 60 次/小时）

# GitHub API：用来验证链接真伪。本机 github.com 网页打不开，但 api.github.com 是通的
GH_API = "https://api.github.com"
GH_HEADERS = {"User-Agent": "embodied-project-crawler/1.0",
              "Accept": "application/vnd.github+json"}
_GH_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
if _GH_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {_GH_TOKEN}"
_GH_CACHE = {}

# 明显不是"项目"的仓库名特征（骨干网络 / 权重文件 / 配置文件）
NON_PROJECT_HINTS = ("resnet", "backbone", "tokenizer", "gguf", "onnx", "tflite", "config")

# ---------- 具身智能关键词（核心判定标准） ----------
# 说明：具身智能(Embodied AI) = 以机器人为载体、感知-决策-行动闭环的智能体，
# 涵盖 VLA/操作/抓取/移动/人形机器人/机器人学习/具身智能体等。
EMBODIED_KEYWORDS = [
    "embodied", "embodiment", "embodied-ai", "embodied-agent", "embodied-intelligence",
    "vision-language-action", "vla", "pi0", "pi05", "gr00t",
    "humanoid", "manipulation", "grasping", "locomotion", "legged", "quadruped",
    "robot", "robotics", "robot-learning", "robotic", "lerobot", "agent-robot",
    "world-model", "navigation-robot",
]
# 负向关键词：README 里出现则认为不是具身智能（排除明显误伤）
EXCLUDE_HINTS = ["chess", "trading-bot", "chatbot", "game-bot", "scraper"]

# "项目"=模型仓库。Space 是演示应用，不参与"最火热项目"排名，只作为 demo 链接来源；
# 数据集只是数据，也不算项目。
REPO_TYPES = ["models"]
MIN_LIKES = 5              # 低于这个赞数的基本是个人微调/测试仓，不进榜


def hf_get(url, params=None, retries=5, raw=False):
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code in (401, 403, 404):
                return None  # 私有/受限/不存在，当作拿不到
            r.raise_for_status()
            return r.text if raw else r.json()
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


def gh_check(repo_path):
    """验证 GitHub 仓库是否存在；存在返回 API 给的规范地址，不存在返回 None

    README 里的链接不能直接信。lerobot/resnet10 的模型卡就写着
    github.com/Lerobot/lerobot（真实仓库是 huggingface/lerobot），只做正则
    提取会把这个 404 当成官方链接。
    """
    repo_path = (repo_path or "").strip("/")
    if not repo_path or repo_path in _GH_CACHE:
        return _GH_CACHE.get(repo_path)
    result = None
    for i in range(3):
        try:
            r = requests.get(f"{GH_API}/repos/{repo_path}", headers=GH_HEADERS, timeout=20)
            if r.status_code == 200:
                result = r.json().get("html_url")   # 仓库改过名也会被重定向到新地址
                break
            if r.status_code == 403:                 # 触发限流，等一会儿再试
                print(f"    GitHub API 限流，等待 30s")
                time.sleep(30)
                continue
            break                                    # 404/451 等：确实不存在
        except Exception:
            time.sleep(2 * (i + 1))
    _GH_CACHE[repo_path] = result
    return result



def kw_match(text):
    """文本是否命中具身智能关键词（不区分大小写）"""
    if not text:
        return None
    low = text.lower()
    for kw in EMBODIED_KEYWORDS:
        # robot 需作为独立词/连字符片段出现，避免误伤；其余子串匹配即可
        if kw in ("robot", "robotic", "vla"):
            if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low):
                return kw
        elif kw in low:
            return kw
    return None


def search_candidates():
    """关键词搜索三类仓库，聚合去重，返回按热度排序的候选列表"""
    repo_map = {}  # repo_id -> info
    for kw in EMBODIED_KEYWORDS:
        for repo_type in REPO_TYPES:
            r = hf_get(api(f"/{repo_type}"),
                       params={"search": kw, "limit": SEARCH_LIMIT,
                               "sort": "likes", "direction": "-1"})
            if r is None:
                continue
            for it in r:
                rid = it.get("id") or it.get("modelId")
                if not rid:
                    continue
                likes = it.get("likes", 0) or 0
                downloads = it.get("downloads", 0) or 0
                if rid not in repo_map:
                    repo_map[rid] = {
                        "repo_id": rid,
                        "repo_type": repo_type,
                        "likes": likes,
                        "downloads": downloads,
                        "tags": [t for t in (it.get("tags") or [])][:15],
                        "score": likes * 10 + min(downloads, DOWNLOAD_CAP),
                        "matched_kw": {kw},
                    }
                else:
                    repo_map[rid]["matched_kw"].add(kw)
            time.sleep(REQUEST_SLEEP)

    # 初筛：仓库名或标签必须命中关键词
    candidates = []
    for info in repo_map.values():
        text = info["repo_id"].lower() + " " + " ".join(t.lower() for t in info["tags"])
        if kw_match(text):
            candidates.append(info)
    candidates.sort(key=lambda x: -x["score"])
    print(f"[候选] 搜索到 {len(repo_map)} 个仓库，关键词初筛后 {len(candidates)} 个")
    return candidates


def repo_url(info):
    prefix = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}[info["repo_type"]]
    return f"https://huggingface.co/{prefix}{info['repo_id']}"


def fetch_readme(info):
    prefix = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}[info["repo_type"]]
    for branch in ("main", "master"):
        text = hf_get(web(f"/{prefix}{info['repo_id']}/raw/{branch}/README.md"), raw=True)
        if text:
            return text
    return ""


def extract_github_candidates(readme, repo_id):
    """从 README 抓出所有 GitHub 仓库候选，按"像官方仓库的程度"排序（不做验证）"""
    if not readme:
        return []
    links = re.findall(r'https?://(?:www\.)?github\.com/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)?', readme)
    author = repo_id.split("/")[0].lower()
    name = repo_id.split("/")[-1].lower()
    skip = {"blob", "tree", "issues", "releases", "pull", "wiki", "discussions",
            "raw", "assets", "sponsors", "orgs", "marketplace", "settings"}
    repos = []
    for l in links:
        l = l.rstrip(".")
        parts = l.split("github.com/")[1].split("/")
        if len(parts) < 2:
            continue  # 只是用户主页
        owner, rname = parts[0], parts[1]
        if rname.lower() in skip:
            continue  # 指向仓库内部页，跳过
        repos.append(f"https://github.com/{owner}/{rname}")
    if not repos:
        return []
    scored = {}
    for r in repos:
        owner, rname = r.split("github.com/")[1].split("/")
        s = repos.count(r)                 # 出现频率
        if owner.lower() == author:
            s += 10                        # 与 HF 作者同名，可能相关
        rl = rname.lower()
        if name and (name in rl or rl in name):
            s += 8                         # 仓库名与项目名一致
        scored[r] = s
    return [r for r, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]


def _repo_name_related(model_name, repo_name):
    """GitHub 仓库名是否真的和这个 HF 项目相关

    只靠系列名会配错：XiaomiMiMo 名下既有 MiMo-Code（编码模型）也有
    MiMo-Embodied，按 "mimo" 搜第一个命中的是 Code，跟具身无关。
    """
    m = re.sub(r"[^a-z0-9]", "", model_name.lower())
    g = re.sub(r"[^a-z0-9]", "", repo_name.lower())
    if not g:
        return False
    if m == g:
        return True
    if (g in m or m in g) and len(g) >= 6:
        return True          # 名字足够长才允许"包含"关系，避免 mimo -> MiMo 这种泛化匹配
    shared = _name_tokens(model_name) & _name_tokens(repo_name)
    return len(shared) >= 2 or any(len(t) >= 6 for t in shared)


def gh_search_repo(name, prefer_owner=None, model_name=None):
    """GitHub 搜索兜底：README 里没给链接时按名字搜，只认 owner 相同的官方仓库"""
    try:
        r = requests.get(f"{GH_API}/search/repositories",
                         params={"q": f"{name} in:name", "sort": "stars",
                                 "order": "desc", "per_page": 10},
                         headers=GH_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        for it in r.json().get("items") or []:
            owner = ((it.get("owner") or {}).get("login") or "").lower()
            rname = it.get("name") or ""
            if prefer_owner and owner != prefer_owner.lower():
                continue
            if model_name and not _repo_name_related(model_name, rname):
                continue
            return it.get("html_url")
    except Exception:
        pass
    return None


def resolve_github(readme, repo_id, limit=GH_CANDIDATE_MAX):
    """按分数顺序验证候选，返回第一个真实存在的仓库地址

    README 里的链接一律要过 GitHub API 才算数；全都不存在时再按仓库名搜一次，
    只接受 owner 与 HF 作者一致的结果（宁缺勿错）。
    """
    for url in extract_github_candidates(readme, repo_id)[:limit]:
        real = gh_check(url.split("github.com/")[1])
        if real:
            return real
        print(f"        候选无效(404): {url}")
    owner = repo_id.split("/")[0]
    name = repo_id.split("/")[-1]
    for term in (name, family_token(repo_id)):
        if not term:
            continue
        found = gh_search_repo(term, prefer_owner=owner, model_name=name)
        if found:
            print(f"        搜索兜底命中（按 {term}）: {found}")
            return found
    return None


def extract_space_links(readme):
    if not readme:
        return []
    links = re.findall(r'https?://(?:www\.)?huggingface\.co/spaces/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+', readme)
    return links


def safe_get(url):
    """探测类请求：失败就返回 None，不要让一个 429 把整个任务打断"""
    try:
        return hf_get(url, retries=2)
    except Exception:
        return None


def list_author_spaces(author):
    """一次请求列出作者名下所有 space，比猜 5 个名字覆盖率高得多"""
    r = safe_get(api("/spaces") + f"?author={author}&limit=1000")
    return [it.get("id") or "" for it in (r or [])]


def card_demo(repo_id):
    """模型卡 cardData 里的 demo 字段经常直接给出可用 Demo"""
    info = safe_get(api(f"/models/{repo_id}")) or {}
    cd = info.get("cardData") or {}
    for key in ("demo", "space", "app_file"):
        v = cd.get(key)
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            if isinstance(x, str) and x.strip():
                if "/spaces/" in x:
                    return (x if x.startswith("http")
                            else f"https://huggingface.co/spaces/{x.split('/spaces/')[-1]}")
                if "/" in x and " " not in x and key != "app_file":
                    return f"https://huggingface.co/spaces/{x}"
    return None


SPACE_NAME_STOP = {"base", "model", "demo", "vla", "the", "and", "with", "for"}


def _name_tokens(text):
    return {t for t in re.split(r"[-_.]", text.lower()) if len(t) >= 4 and t not in SPACE_NAME_STOP}


def _space_matches(model_name, space_name, fam):
    """判断某个 space 是不是这个项目的 Demo

    光靠系列名容易配错：MiMo-Embodied-7B 会匹配到同作者改名带 mimo 的
    mimo_audio_chat（音频对话），完全不是具身 Demo。所以除了系列名，还要求
    系列名足够具体(>=6)，或两者共享一个更具体的词（embodied / robot 等）。
    """
    sname = space_name.lower()
    mname = model_name.lower()
    if sname in (mname, mname.replace("_base", "").replace("_", "-")):
        return True
    if not fam or fam not in sname.replace("-", "").replace("_", ""):
        return False
    if len(fam) >= 6:
        return True
    shared = (_name_tokens(model_name) & _name_tokens(space_name)) - {fam}
    return bool(shared)


def find_space_for_repo(info):
    """找演示 Space，返回 (url, 来源)。每一处都验证存在，找不到返回 (None, 'none')"""
    readme = info.get("readme", "") or ""
    if info["repo_type"] == "spaces":
        return repo_url(info), "self"
    # 1) README 里直接写的 space 链接（验证后才用）
    for link in extract_space_links(readme)[:3]:
        sid = link.split("/spaces/")[1].strip("/")
        if safe_get(api(f"/spaces/{sid}")) is not None:
            return f"https://huggingface.co/spaces/{sid}", "readme"
    # 2) 模型卡 cardData 的 demo 字段
    demo = card_demo(info["repo_id"])
    if demo:
        sid = demo.split("/spaces/")[1].strip("/") if "/spaces/" in demo else None
        if sid is None or safe_get(api(f"/spaces/{sid}")) is not None:
            return demo, "card"
    # 3) 作者名下全部 space 按名字匹配
    author = info["repo_id"].split("/")[0]
    name = info["repo_id"].split("/")[-1].lower()
    fam = family_token(info["repo_id"])
    for sid in list_author_spaces(author):
        if _space_matches(name, sid.split("/")[-1], fam):
            return f"https://huggingface.co/spaces/{sid}", "author_list"
    # 4) 全局搜索：名字完全一致，或作者一致且符合上面的匹配规则
    for term in {name, name.replace("_base", "").replace("_", "-"), fam}:
        if not term:
            continue
        r = safe_get(api("/spaces") + f"?search={term}&limit=30")
        for it in (r or []):
            rid = it.get("id") or ""
            sp_author, sp_name = (rid.split("/") + [""])[:2]
            if sp_name.lower() == name or (sp_author == author and _space_matches(name, sp_name, fam)):
                return f"https://huggingface.co/spaces/{rid}", "search"
    return None, "none"


def family_token(repo_id):
    """取仓库名的系列标识，用于"同系列只留最热的一个"
    GR00T-N1.7-3B -> gr00t；smolvla_libero -> smolvla；pi0_base -> pi0；
    X-VLA-Pt -> xvla（首段太短时并上第二段；比较时忽略连字符和点）
    """
    parts = [p for p in re.split(r"[-_.]", repo_id.split("/")[-1].lower()) if p]
    if not parts:
        return ""
    head = parts[0]
    if len(head) < 4 and not re.search(r"\d", head) and len(parts) > 1:
        head = f"{head}-{parts[1]}"
    return re.sub(r"[^a-z0-9]", "", head)


def is_junk_name(repo_id):
    """弃用/测试仓不进榜（lerobot/pi0_old 这类 likes 还挺高，会污染排名）"""
    name = repo_id.split("/")[-1].lower()
    return re.search(r"(^|[-_])(old|deprecated|dummy|test|tmp|backup|copy)([-_]|$)", name) is not None


def is_weight_only(info):
    """排除骨干网络/权重文件这类"不是一个项目"的仓库（如 lerobot/resnet10）"""
    name = info["repo_id"].split("/")[-1].lower()
    if any(h in name for h in NON_PROJECT_HINTS):
        return True
    readme = (info.get("readme") or "").lower()
    if ("backbone" in readme or "component of" in readme):
        if "demo" not in readme and "example" not in readme and "inference" not in readme:
            return True
    return False



def confirm_embodied(info):
    """用 README 复核关键词，排除误伤"""
    readme = fetch_readme(info)
    info["readme"] = readme
    if readme:
        for hint in EXCLUDE_HINTS:
            if hint in readme.lower() and kw_match(readme[:5000]) is None:
                return False
        if kw_match(info["repo_id"]) or kw_match(" ".join(info["tags"])):
            return True
        return kw_match(readme[:5000]) is not None
    # 无 README，靠初筛结果
    return True


def init_db(conn):
    cur = conn.cursor()
    # 重建表：新增验证状态字段，旧表字段对不上直接重建（本表是爬取结果的派生表）
    cur.execute("DROP TABLE IF EXISTS embodied_projects")
    cur.execute("""CREATE TABLE embodied_projects (
        rank INTEGER PRIMARY KEY,
        repo_id TEXT, repo_type TEXT,
        likes INTEGER, downloads INTEGER, score INTEGER,
        url TEXT, github_url TEXT, github_verified INTEGER,
        space_url TEXT, space_source TEXT,
        matched_keywords TEXT, description TEXT, note TEXT,
        fetched_at TEXT)""")
    conn.commit()


def main():
    fetched_at = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[任务三] 统计 HF 最火热的 {TOP_N} 个具身智能项目，北京时间 {fetched_at}")

    candidates = search_candidates()

    print("[步骤2] 同系列去重（保留每系列最热的一个）后复核关键词 ...")

    reps = {}
    for info in candidates:
        if info["likes"] < MIN_LIKES or is_junk_name(info["repo_id"]):
            continue                      # 个人微调/弃用/测试仓，不进榜
        fam = family_token(info["repo_id"])
        if fam not in reps or info["score"] > reps[fam]["score"]:
            reps[fam] = info
    rep_list = sorted(reps.values(), key=lambda x: -x["score"])
    print(f"  {len(candidates)} 个候选 -> {len(rep_list)} 个项目系列")

    print("[步骤3] 复核关键词 + 验证 GitHub / Space 链接 ...")
    projects = []
    checked = 0
    author_count = Counter()
    for info in rep_list:
        if len(projects) >= TOP_N:
            break
        checked += 1
        if not confirm_embodied(info):
            print(f"  排除(关键词复核不通过): {info['repo_id']}")
            continue
        if is_weight_only(info):
            print(f"  排除(骨干网络/权重文件，不是项目): {info['repo_id']}")
            continue
        author = info["repo_id"].split("/")[0]
        if author_count[author] >= SAME_AUTHOR_MAX:
            print(f"  跳过(同作者已入选 {SAME_AUTHOR_MAX} 个): {info['repo_id']}")
            continue
        github = resolve_github(info.get("readme", ""), info["repo_id"])
        space, space_src = find_space_for_repo(info)
        notes = []
        if not github:
            notes.append("无官方开源代码")
        if not space:
            notes.append("无官方 Demo")
        info["github"], info["space"] = github, space
        info["space_source"] = space_src
        info["note"] = "；".join(notes)
        projects.append(info)
        author_count[author] += 1
        print(f"  入选 #{len(projects)}: {info['repo_id']} (likes={info['likes']}, "
              f"downloads={info['downloads']}, kw={','.join(sorted(info['matched_kw'] & set(EMBODIED_KEYWORDS)))})")
        print(f"           github={github or '(无)'}  space={space or '(无)'} [{space_src}]")
        time.sleep(REQUEST_SLEEP)
    print(f"  共复核 {checked} 个候选")

    # 简介取 README 第一段文本
    for p in projects:
        desc = ""
        m = re.search(r'^#\s*(.+)$', p.get("readme", ""), re.M)
        if m:
            desc = m.group(1).strip()[:120]
        p["desc"] = desc

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        cur = conn.cursor()
        for rank, p in enumerate(projects, 1):
            cur.execute(
                """INSERT INTO embodied_projects
                   (rank, repo_id, repo_type, likes, downloads, score, url,
                    github_url, github_verified, space_url, space_source,
                    matched_keywords, description, note, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rank, p["repo_id"], p["repo_type"], p["likes"], p["downloads"],
                 p["score"], repo_url(p),
                 p.get("github"), 1 if p.get("github") else 0,
                 p.get("space"), p.get("space_source", "none"),
                 ",".join(sorted(p["matched_kw"])), p.get("desc", ""),
                 p.get("note", ""), fetched_at))
        conn.commit()
        print(f"[数据库] 已写入 {len(projects)} 个项目 -> {DB_PATH}")
    finally:
        conn.close()
    print("完成。运行 build_website.py 生成网页。")


if __name__ == "__main__":
    main()
