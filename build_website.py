# -*- coding: utf-8 -*-
"""
build_website.py —— 整合 hf_data.db 中的三份数据，生成网页并部署，得到可分享的链接

用法：
  python build_website.py            # 生成 site/index.html；若配置了 token 则自动部署
  python build_website.py --serve    # 生成后在本机起一个临时预览服务 http://127.0.0.1:8000/site
  python build_website.py --deploy   # 仅部署（有 token 时）

可分享链接的实现：把生成的静态网页部署到 HuggingFace Space（免费静态托管），
部署后的地址形如 https://<用户名>-hf-crawl-dashboard.hf.space ，任何人都可访问。

Token 配置（二选一）：
  1. 环境变量 HF_TOKEN
  2. 在本目录放一个 hf_token.txt，内容为 token（https://huggingface.co/settings/tokens 创建，
     权限选 write）
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hf_data.db")
SITE_DIR = os.path.join(BASE_DIR, "site")
HTML_PATH = os.path.join(SITE_DIR, "index.html")
TOKEN_FILE = os.path.join(BASE_DIR, "hf_token.txt")

SPACE_NAME = "hf-crawl-dashboard"   # 部署的 Space 名，可改
GRAPH_NODE_LIMIT = 180             # 网页上网络图最多显示多少个节点（VIP 全保留）
BJT = timezone(timedelta(hours=8))

TYPE_LABEL = {"models": "Models", "datasets": "Datasets", "spaces": "Spaces"}


# ---------------------------------------------------------------- 数据读取
def load_data():
    if not os.path.exists(DB_PATH):
        print(f"未找到数据库 {DB_PATH}，请先运行 task1/task2/task3")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    data = {"generated_at": datetime.now(timezone.utc).astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")}

    # 1. trending
    rows = conn.execute("SELECT * FROM trending_snapshots ORDER BY beijing_date DESC, repo_type, rank").fetchall() if "trending_snapshots" in tables else []
    days = defaultdict(lambda: defaultdict(list))
    day_time = {}
    for r in rows:
        days[r["beijing_date"]][r["repo_type"]].append(dict(r))
        day_time[r["beijing_date"]] = r["fetched_at"]
    data["trending"] = [
        {"date": d, "fetched_at": day_time[d],
         "repo_types": {t: days[d][t] for t in ("models", "datasets", "spaces") if days[d].get(t)}}
        for d in sorted(days.keys(), reverse=True)
    ]

    # 2. 网络
    # 注意：只取图里需要的字段，5.7 万个节点的 fullname/avatar 全塞进 HTML 会让文件涨到十几 MB
    data["vips"] = [dict(r) for r in conn.execute("SELECT * FROM vips ORDER BY rank")] if "vips" in tables else []
    nodes = {r["name"]: dict(r) for r in conn.execute("SELECT name, account_type FROM network_nodes")} if "network_nodes" in tables else {}
    edges = [(r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM follow_edges")] if "follow_edges" in tables else []
    data["edge_count"] = len(edges)
    data["node_count"] = len(nodes)
    stats = [dict(r) for r in conn.execute(
        "SELECT * FROM network_stats ORDER BY total_degree DESC")] if "network_stats" in tables else []
    # 网页图只展示部分节点
    data["graph"] = build_graph(data["vips"], nodes, edges, stats)
    # 三个表只用到各维度的 Top10，这里保留 Top15 即可；完整规模用计数表达，避免把 5.7 万行写进 HTML
    keep_rows = {}
    for key in ("in_degree", "out_degree", "total_degree"):
        for r in sorted(stats, key=lambda x: (-x[key], x["name"]))[:15]:
            keep_rows[r["name"]] = r
    data["stats"] = sorted(keep_rows.values(), key=lambda r: (-r["total_degree"], r["name"]))
    data["stats_total"] = len(stats)

    # 3. 项目
    data["projects"] = [dict(r) for r in conn.execute("SELECT * FROM embodied_projects ORDER BY rank")] if "embodied_projects" in tables else []
    conn.close()
    return data


def build_graph(vips, nodes, edges, stats):
    vip_names = {v["username"] for v in vips}
    node_type = {n: nodes.get(n, {}).get("account_type", "user") for n in set(
        [s["name"] for s in stats] + list(vip_names))}
    degree = {s["name"]: s["total_degree"] for s in stats}
    keep = set(vip_names)
    ranked = sorted((n for n in degree if n not in keep), key=lambda x: -degree[x])
    keep.update(ranked[:GRAPH_NODE_LIMIT - len(keep)])
    g_edges = [{"source": s, "target": d} for s, d in edges if s in keep and d in keep]
    g_nodes = []
    for n in keep:
        g_nodes.append({
            "id": n,
            "name": n,
            "symbolSize": min(10 + (degree.get(n, 0) ** 0.5) * 2, 56),
            "category": 0 if n in vip_names else (1 if node_type.get(n) == "org" else 2),
            "value": degree.get(n, 0),
        })
    return {"nodes": g_nodes, "links": g_edges,
            "counts": {"displayed_nodes": len(g_nodes), "displayed_edges": len(g_edges)}}


# ---------------------------------------------------------------- 网页模板
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HuggingFace 数据看板 · Trending / 具身智能</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#3fb950; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:"Segoe UI","Microsoft YaHei",sans-serif; }
.container { max-width:1180px; margin:0 auto; padding:24px 16px 60px; }
header h1 { margin:0; font-size:26px; }
header p { color:var(--muted); margin:6px 0 18px; }
.tabs { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }
.tab { padding:9px 16px; border:1px solid var(--border); background:var(--card); border-radius:8px; cursor:pointer; color:var(--muted); font-size:14px; }
.tab.active { color:#fff; border-color:var(--accent); background:#1f2a3a; }
.panel { display:none; }
.panel.active { display:block; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:16px; }
.card h2 { margin:0 0 12px; font-size:18px; }
.card h3 { margin:14px 0 8px; font-size:15px; color:var(--accent); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--border); }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
tr:hover td { background:#1c2129; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; border:1px solid var(--border); color:var(--muted); }
.badge.vip { color:#f0c674; border-color:#8a6d2f; background:#2d230d; }
.badge.org { color:var(--accent2); }
.badge.ok { color:#3fb950; border-color:#2f6d3f; background:#12281a; }
.days { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
.day-btn { padding:6px 12px; border:1px solid var(--border); background:var(--card); border-radius:6px; cursor:pointer; color:var(--muted); }
.day-btn.active { color:#fff; border-color:var(--accent2); }
.muted { color:var(--muted); font-size:12px; }
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
#graph { width:100%; height:640px; }
.num { font-variant-numeric:tabular-nums; }
.columns { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:800px){ .columns{ grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="container">
<header>
  <h1>🤗 HuggingFace 数据看板</h1>
  <p>Trending 每日趋势 · 具身智能大V关注网络 · 具身智能 Top10 项目　<span class="muted">生成于 %%GENERATED%%（北京时间）</span></p>
</header>

<div class="tabs">
  <div class="tab active" data-panel="p1">📈 一周 Trending</div>
  <div class="tab" data-panel="p2">🕸️ 具身智能大V关注网络</div>
  <div class="tab" data-panel="p3">🏆 具身智能 Top10 项目</div>
</div>

<!-- 面板1：Trending -->
<div class="panel active" id="p1">
  <div class="card">
    <h2>每日 Trending（Models / Datasets / Spaces 各前 10）</h2>
    <div class="days" id="dayBtns"></div>
    <div id="dayContent"></div>
  </div>
</div>

<!-- 面板2：关注网络 -->
<div class="panel" id="p2">
  <div class="card">
    <h2>具身智能领域粉丝数 Top10 大V</h2>
    <table id="vipTable"></table>
  </div>
  <div class="card stat-grid">
    <div><h3>入度最多（被最多人关注）Top10</h3><table id="inTable"></table></div>
    <div><h3>出度最多（关注最多人）Top10</h3><table id="outTable"></table></div>
    <div><h3>入度 + 出度最多 Top10</h3><table id="totalTable"></table></div>
    <div><h3>网络规模</h3><p class="muted" id="netSize"></p></div>
  </div>
  <div class="card">
    <h2>关注网络图</h2>
    <p class="muted">金色=大V，绿色=组织，蓝色=个人；节点越大连接越多。有向边：关注者 → 被关注者。</p>
    <div id="graph"></div>
  </div>
</div>

<!-- 面板3：项目 -->
<div class="panel" id="p3">
  <div class="card">
    <h2>HuggingFace 最火热的 10 个具身智能项目</h2>
    <p class="muted">判定：仓库/标签/README 含具身智能关键词（embodied、VLA、robot、humanoid、manipulation 等）；热度 = likes×10 + downloads</p>
    <table id="projTable"></table>
  </div>
</div>
</div>

<script>
const DATA = %%DATA%%;
const fmt = n => n==null ? '-' : Number(n).toLocaleString();
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* tabs */
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById(t.dataset.panel).classList.add('active');
  if (t.dataset.panel === 'p2') initGraph();
});

/* panel 1 */
const dayBtns = document.getElementById('dayBtns');
const dayContent = document.getElementById('dayContent');
let curDay = DATA.trending.length ? DATA.trending[0].date : null;
function renderDay(d) {
  curDay = d;
  document.querySelectorAll('.day-btn').forEach(b => b.classList.toggle('active', b.dataset.day === d));
  const day = DATA.trending.find(x => x.date === d);
  if (!day) { dayContent.innerHTML = '<p class="muted">暂无数据</p>'; return; }
  let html = `<p class="muted">数据采集时间：${esc(day.fetched_at)}（北京时间）</p>`;
  for (const [type, rows] of Object.entries(day.repo_types)) {
    html += `<h3>${type === 'models' ? 'Models' : type === 'datasets' ? 'Datasets' : 'Spaces'} Trending</h3>
    <table><tr><th>#</th><th>仓库</th><th>作者</th><th class="num">Likes</th><th class="num">Downloads</th></tr>`;
    rows.forEach(r => {
      html += `<tr><td>${r.rank}</td><td><a href="${esc(r.url)}" target="_blank">${esc(r.repo_id)}</a></td>
      <td>${esc(r.author)}</td><td class="num">${fmt(r.likes)}</td><td class="num">${fmt(r.downloads)}</td></tr>`;
    });
    html += '</table>';
  }
  dayContent.innerHTML = html;
}
DATA.trending.forEach(d => {
  const b = document.createElement('div');
  b.className = 'day-btn'; b.dataset.day = d.date; b.textContent = d.date;
  b.onclick = () => renderDay(d.date);
  dayBtns.appendChild(b);
});
if (curDay) renderDay(curDay);

/* panel 2 */
function statTable(elId, key) {
  const top = DATA.stats.slice().sort((a, b) => b[key] - a[key]).slice(0, 10);
  document.getElementById(elId).innerHTML =
    '<tr><th>账号</th><th>类型</th><th class="num">入度</th><th class="num">出度</th><th class="num">总度</th></tr>' +
    top.map(s => `<tr><td><a href="https://huggingface.co/${esc(s.name)}" target="_blank">${esc(s.name)}</a></td>
      <td>${s.account_type === 'org' ? '<span class="badge org">org</span>' : '<span class="badge">user</span>'}</td>
      <td class="num">${s.in_degree}${(truncatedVips.has(s.name) && s.in_degree >= 10000) ? '+' : ''}</td>
      <td class="num">${s.out_degree}</td><td class="num">${s.total_degree}</td></tr>`).join('');
}
document.getElementById('vipTable').innerHTML =
  '<tr><th>#</th><th>大V</th><th>类型</th><th class="num">粉丝数</th><th class="num">具身仓库数</th><th>入选方式</th></tr>' +
  DATA.vips.map(v => `<tr><td>${v.rank}</td>
    <td><a href="https://huggingface.co/${esc(v.username)}" target="_blank">${esc(v.username)}</a></td>
    <td>${v.account_type === 'org' ? '<span class="badge org">org</span>' : '<span class="badge">user</span>'}</td>
    <td class="num">${fmt(v.followers)}${v.followers_capped ? '+' : ''}</td>
    <td class="num">${fmt(v.embodied_repo_count)}</td>
    <td class="muted">${v.selection === 'personal_fill' ? '个人账号补齐（组织不能关注他人，为采集出度补入）' : '领域粉丝数排名'}</td></tr>`).join('');
const truncatedVips = new Set(DATA.vips.filter(v => v.edges_truncated).map(v => v.username));
statTable('inTable', 'in_degree');
statTable('outTable', 'out_degree');
statTable('totalTable', 'total_degree');
document.getElementById('netSize').innerHTML =
  `全网共 <b>${fmt(DATA.node_count)}</b> 个节点、<b>${fmt(DATA.edge_count)}</b> 条关注边（每个大V最多采集 10000 粉丝 / 2000 关注）。<br>
   带 <b>+</b> 的入度表示该账号粉丝超过采集上限（10000），显示的是已采集部分。<br>
   组织账号在 HuggingFace 上不能关注别人，出度恒为 0，所以"出度"一栏的真实样本只来自个人账号。<br>
   当前图展示 ${fmt(DATA.graph.counts.displayed_nodes)} 个节点 / ${fmt(DATA.graph.counts.displayed_edges)} 条边（大V + 度数最高的节点）。`;

let graphInited = false;
function initGraph() {
  if (graphInited) return; graphInited = true;
  const chart = echarts.init(document.getElementById('graph'));
  chart.setOption({
    tooltip: { formatter: p => p.dataType === 'node' ? `${p.name}<br>总连接数: ${p.value}` : `${p.data.source} → ${p.data.target}` },
    legend: [{ data: ['大V', '组织', '个人'], textStyle: { color: '#8b949e' } }],
    series: [{
      type: 'graph', layout: 'force', roam: true,
      categories: [{ name: '大V', itemStyle: { color: '#f0c674' } },
                   { name: '组织', itemStyle: { color: '#3fb950' } },
                   { name: '个人', itemStyle: { color: '#58a6ff' } }],
      label: { show: true, color: '#e6edf3', fontSize: 10, formatter: p => p.name.length > 18 ? p.name.slice(0, 16) + '…' : p.name },
      force: { repulsion: 120, edgeLength: [30, 90], gravity: 0.08 },
      edgeStyle: { color: '#444c56', curveness: 0.15 },
      emphasis: { focus: 'adjacency' },
      data: DATA.graph.nodes, links: DATA.graph.links
    }]
  });
  window.addEventListener('resize', () => chart.resize());
}

/* panel 3 */
const SPACE_SRC = { readme: 'README', card: '模型卡', author_list: '作者主页', search: '搜索同名', self: 'Space 本体', none: '' };
document.getElementById('projTable').innerHTML =
  '<tr><th>#</th><th>项目</th><th>类型</th><th class="num">Likes</th><th class="num">Downloads</th><th>关键词</th><th>GitHub</th><th>Space</th><th>备注</th></tr>' +
  DATA.projects.map(p => `<tr><td>${p.rank}</td>
    <td><a href="${esc(p.url)}" target="_blank">${esc(p.repo_id)}</a></td>
    <td><span class="badge">${esc(p.repo_type)}</span></td>
    <td class="num">${fmt(p.likes)}</td><td class="num">${fmt(p.downloads)}</td>
    <td class="muted">${esc(p.matched_keywords)}</td>
    <td>${p.github_url
        ? `<a href="${esc(p.github_url)}" target="_blank">GitHub</a>${p.github_verified ? ' <span class="badge ok">已校验</span>' : ''}`
        : '<span class="muted">无官方开源代码</span>'}</td>
    <td>${p.space_url
        ? `<a href="${esc(p.space_url)}" target="_blank">Space</a>${SPACE_SRC[p.space_source] ? ` <span class="muted">${SPACE_SRC[p.space_source]}</span>` : ''}`
        : '<span class="muted">无官方 Demo</span>'}</td>
    <td class="muted">${esc(p.note || '')}</td></tr>`).join('');
</script>
</body>
</html>
"""


def render_html(data):
    return (HTML.replace("%%GENERATED%%", data["generated_at"])
            .replace("%%DATA%%", json.dumps(data, ensure_ascii=False)))


# ---------------------------------------------------------------- 部署（可分享链接）
def get_token():
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not tok and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    return tok


def api(method, url, token, retries=4, **kw):
    """带重试的 API 调用。4xx 属于请求本身有问题，重试没有意义，直接把响应体抛出来。"""
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    last = None
    for i in range(retries):
        try:
            r = requests.request(method, url, headers=headers, timeout=120, **kw)
            if r.status_code < 400 or r.status_code == 409:  # 409 = 已存在，交给调用方判断
                return r
            body = (r.text or "").strip()[:600]
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}\n  服务端返回：{body}")
            last = f"HTTP {r.status_code} {body}"
        except RuntimeError:
            raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        print(f"  请求失败({i + 1}/{retries})：{last}，重试 ...")
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{method} {url} -> {last}")


def commit_files(token, repo_id, files, summary):
    """通过 NDJSON 接口一次性提交多个文件。

    格式要求（官方文档「Commit」一节）：
      第一行必须是 header：      {"key":"header","value":{"summary":...,"description":...}}
      后面每个文件一行：          {"key":"file","value":{"path":...,"encoding":"base64","content":...}}
    之前 400 的原因就是少了 header 行，而且用了不存在的 "operation" 字段。
    """
    lines = [{"key": "header",
              "value": {"summary": summary, "description": "由 build_website.py 自动部署"}}]
    for path, content in files:
        lines.append({"key": "file",
                      "value": {"path": path,
                                "encoding": "base64",
                                "content": base64.b64encode(content).decode()}})
    ndjson = "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n"
    return api("POST", f"https://huggingface.co/api/spaces/{repo_id}/commit/main", token,
               data=ndjson.encode("utf-8"),
               headers={"Content-Type": "application/x-ndjson"})


def upload_single(token, repo_id, path, content):
    """备用通道：单文件直传（老接口，返回 404/405 就说明已经下线）"""
    return api("POST", f"https://huggingface.co/api/spaces/{repo_id}/upload/main/{path}", token,
               data=content, headers={"Content-Type": "application/octet-stream"})


def push_files(token, repo_id, files, summary):
    """写入 Space，三级兜底：官方库 → NDJSON 批量提交 → 单文件直传"""
    try:
        from huggingface_hub import HfApi  # 官方库，格式最不容易出错
    except ImportError:
        HfApi = None
    if HfApi:
        print("[部署] 检测到 huggingface_hub，使用官方库上传")
        hub = HfApi(token=token)
        hub.create_repo(repo_id, repo_type="space", space_sdk="static", exist_ok=True)
        hub.upload_folder(folder_path=SITE_DIR, repo_id=repo_id, repo_type="space",
                          commit_message=summary, ignore_patterns=["*.db", "*_test.html"])
        return "hub"
    try:
        commit_files(token, repo_id, files, summary)
        return "commit"
    except RuntimeError as e:
        print(f"[部署] 批量提交失败，改用单文件上传：{e}")
    for path, content in files:
        upload_single(token, repo_id, path, content)
        print(f"  已上传 {path}")
    return "single"


def wait_space(token, repo_id, timeout=150):
    """轮询 Space 的构建状态，直到 RUNNING / ERROR / 超时。返回最后一次的 stage。"""
    final = ("RUNNING", "BUILD_ERROR", "CONFIG_ERROR", "RUNTIME_ERROR", "PAUSED", "STOPPED")
    stage, t0 = None, time.time()
    while time.time() - t0 < timeout:
        try:
            info = api("GET", f"https://huggingface.co/api/spaces/{repo_id}", token).json()
            s = (info.get("runtime") or {}).get("stage")
            if s != stage:
                print(f"    构建状态：{s or '未知'}（已等 {int(time.time() - t0)} 秒）")
                stage = s
            if stage in final:
                return stage
        except RuntimeError as e:
            print(f"    （查询状态失败：{e}）")
        time.sleep(6)
    return stage


def deploy(token):
    """把 site/index.html 部署到 HF Space（静态托管），返回公开链接"""
    print("[部署] 验证 token ...")
    who = api("GET", "https://huggingface.co/api/whoami-v2", token).json()
    user = who.get("name") or who.get("user", {}).get("name")
    role = ((who.get("auth") or {}).get("accessToken") or {}).get("role")
    print(f"[部署] 已登录：{user}（token 角色：{role or '未知'}）")
    if role == "read":
        raise RuntimeError("这个 token 只有 read 权限，无法上传。请到 https://huggingface.co/settings/tokens "
                           "重新创建一个 Write 权限的 token 写进 hf_token.txt")
    repo_id = f"{user}/{SPACE_NAME}"

    # 创建 static space（已存在则忽略）
    r = api("POST", "https://huggingface.co/api/repos/create", token,
            json={"type": "space", "name": SPACE_NAME, "sdk": "static", "private": False})
    print(f"[部署] Space 创建/已存在：{repo_id} ({r.status_code})")

    # 如果这个 Space 之前被建成了 gradio/docker，static 网页放进去也不会被当静态站托管
    try:
        info = api("GET", f"https://huggingface.co/api/spaces/{repo_id}", token).json()
        sdk = info.get("sdk")
        if sdk and sdk != "static":
            print(f"  [注意] 该 Space 当前 SDK 是 {sdk}，不是 static。")
            print(f"    请到 https://huggingface.co/spaces/{repo_id}/settings 把 SDK 改成 Static，或删掉这个 Space 重跑。")
    except RuntimeError as e:
        print(f"  （读取 Space 信息失败，跳过 SDK 检查：{e}）")

    # README.md 决定 Space 的 sdk 元信息，必须和 index.html 一起上传
    readme = ("---\ntitle: HF Crawl Dashboard\nemoji: 🤗\nsdk: static\napp_file: index.html\n"
              "---\n\n# HuggingFace 数据看板\n\n由 build_website.py 自动部署，请勿手改。\n")
    with open(os.path.join(SITE_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    with open(HTML_PATH, "rb") as f:
        html_bytes = f.read()
    print(f"[部署] 上传 index.html（{len(html_bytes) / 1024:.0f} KB）+ README.md ...")
    push_files(token, repo_id, [("index.html", html_bytes), ("README.md", readme.encode("utf-8"))],
               f"deploy {datetime.now(BJT):%Y-%m-%d %H:%M}")

    # 回读一次文件列表，确认真的传上去了（而不是只有 200 响应）
    try:
        tree = api("GET", f"https://huggingface.co/api/spaces/{repo_id}/tree/main", token).json()
        names = [t.get("path") for t in tree]
        print(f"[部署] 仓库文件：{', '.join(names) if names else '(空)'}")
        if "index.html" not in names:
            print("  [注意] 仓库里没有 index.html，部署可能没生效。")
    except RuntimeError as e:
        print(f"  （回读文件列表失败：{e}）")

    print("[部署] 文件已上传，等待 Space 构建（静态站一般 30 秒~2 分钟）...")
    stage = wait_space(token, repo_id)

    url = f"https://{user.replace('_', '-')}-{SPACE_NAME.replace('_', '-')}.hf.space"
    space_page = f"https://huggingface.co/spaces/{repo_id}"
    print(f"    Space 页面：{space_page}")
    print(f"    直连域名： {url}")
    if stage == "RUNNING":
        print("    状态：RUNNING —— 已经可以打开了")
    elif stage and stage.endswith("ERROR"):
        print(f"    状态：{stage} —— 构建没成功，这时候访问直连域名一定是 404。")
        print(f"    看构建日志：{space_page}?logs=build")
    elif stage in ("PAUSED", "STOPPED"):
        print(f"    状态：{stage} —— 免费 Space 长时间没人访问会被暂停，页面点一下 Restart 即可")
    else:
        print(f"    状态：{stage or '未知'} —— 可能还在构建，等一两分钟再刷新")
    return url


def serve():
    import http.server
    import socketserver
    os.chdir(BASE_DIR)
    port = 8000
    with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
        addr = f"http://127.0.0.1:{port}/site/index.html"
        print(f"本地预览：{addr}（Ctrl+C 停止）")
        try:
            webbrowser.open(addr)
        except Exception:
            pass
        httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="生成后开启本地预览")
    ap.add_argument("--deploy", action="store_true", help="仅部署")
    args = ap.parse_args()

    data = load_data()
    print(f"[数据] trending {len(data['trending'])} 天 / "
          f"网络 {data['node_count']} 节点 {data['edge_count']} 边 / "
          f"项目 {len(data['projects'])} 个")

    os.makedirs(SITE_DIR, exist_ok=True)
    html = render_html(data)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[网页] 已生成 {HTML_PATH}")

    token = get_token()
    if token:
        try:
            url = deploy(token)
            print(f"\n[成功] 分享链接（任何人可访问）：{url}")
            print("   首次构建约需 1-2 分钟，之后可重复运行本脚本更新。")
        except Exception as e:
            print(f"\n[失败] 部署失败：{e}")
            print(f"   网页本身没问题，已保存在本地：{HTML_PATH}")
            print("   排查顺序：token 是否有 write 权限 → Space 的 SDK 是否是 static → 网络能否直连 huggingface.co")
            if not args.serve:
                sys.exit(2)
    else:
        print("\n[注意] 未配置 HF_TOKEN，暂未部署。获取可分享链接：")
        print("   1. 到 https://huggingface.co/settings/tokens 创建一个 write 权限 token")
        print(f"   2. 把 token 存到 {TOKEN_FILE}（或设置环境变量 HF_TOKEN），再运行：python build_website.py --deploy")
        print("   网页已保存在本地 site/index.html，可直接双击打开。")

    if args.serve:
        serve()


if __name__ == "__main__":
    main()
