# HuggingFace 具身智能数据采集与可视化

抓取 HuggingFace 公开 API，整理进一个 SQLite 数据库，再生成一个静态看板页面回答三个问题：

1. **一周 trending**，按天展示（models / datasets / spaces 三类榜单）
2. 具身智能领域粉丝数 Top10 大V的**关注网络**，统计入度、出度、入度+出度最多的人
3. 最火的 **10 个具身智能项目**，并找到它们的 GitHub 开源地址与 HuggingFace Space

在线看板：<https://zjc994-hf-crawl-dashboard.hf.space>

---

## 数据流

```
HuggingFace API ──► task1/2/3_*.py ──► hf_data.db (SQLite) ──► build_website.py ──► site/index.html
                      采集                  存储                     展示
```

页面是**纯静态**的：数据在生成时被序列化进 HTML，打开时不发任何请求，只有图表库 ECharts 走 CDN。

---

## 目录

| 文件 | 作用 |
|---|---|
| `task1_trending.py` | 第 1 题。抓 trending 快照，当天重复跑会覆盖（一天一份） |
| `task2_follow_network.py` | 第 2 题。候选池 → 领域门槛 → 粉丝数 → 关注边 → 度数统计，约 12 分钟 |
| `task3_embodied_projects.py` | 第 3 题。项目筛选 → GitHub 链接校验 → Space 检索，约 2 分钟 |
| `build_website.py` | 从库里读数据生成 `site/index.html`，加 `--deploy` 可发布到 HF Space |
| `data/*.csv` | 关键结果的小体积快照（完整数据在 db 里） |
| `backup/trending_YYYY-MM-DD.json` | task1 每次运行自动写的原始归档 |

数据库文件 `hf_data.db`（13MB）不进版本库，跑一遍脚本就能重建。

---

## 快速开始

```bash
pip install -r requirements.txt

# 第 1 题：每天跑一次（约 10 秒）
python task1_trending.py

# 第 2 题：需要重建关注网络时跑（约 12 分钟）
python task2_follow_network.py

# 第 3 题：需要重建项目榜时跑（约 2 分钟）
python task3_embodied_projects.py

# 生成看板
python build_website.py
```

国内网络访问 `huggingface.co` 经常被断（ProxyError / 502）。三个脚本都支持通过环境变量指定端点，连不上时会自动切到镜像：

```bash
HF_ENDPOINT=https://hf-mirror.com python task1_trending.py
```

部署到 HF Space 需要在目录下放一个 `hf_token.txt`（write 权限的 token），然后：

```bash
python build_website.py --deploy
```

注意：**只要 `hf_token.txt` 存在，直接 `python build_website.py` 也会自动部署**。只想本地看页面的话，把 token 文件移走，或者用 `--serve`。

`hf_token.txt` 已在 `.gitignore` 中，不会被提交。

---

## 数据库结构

`hf_data.db`，6 张业务表。

### trending_snapshots — 第 1 题

| 字段 | 说明 |
|---|---|
| `beijing_date` | 北京时间日期，一天一份快照 |
| `repo_type` | models / datasets / spaces |
| `rank` | 该类榜单的第几名（取前 10） |
| `repo_id` / `author` / `url` | 仓库标识与地址 |
| `likes` / `downloads` / `trending_score` | 热度指标，排序用的是 trendingScore |

约束 `UNIQUE(beijing_date, repo_type, rank)`：同一天重复运行只保留最后一次。HF 不提供历史 trending 接口，所以**漏一天就永久缺一天**。

### vips — 第 2 题，Top10 大V

| 字段 | 说明 |
|---|---|
| `username` / `account_type` | 账号名与类型（org / user） |
| `followers` / `followers_capped` | 真实粉丝数与是否触达采集上限 |
| `rank` | 排名 |
| `embodied_repo_count` | 名下真实具身仓库数（按仓库名/标签判定） |
| `selection` | 入选原因：domain（领域门槛通过）/ seed（种子账号） |
| `edges_count` / `edges_truncated` | 实际采集的边数、是否被 `EDGES_MAX_PER_VIP` 截断 |

### network_nodes / follow_edges — 第 2 题，网络本体

`follow_edges(src, dst)` 一条边表示 `src` 关注了 `dst`。`network_nodes` 存节点属性。

### network_stats — 第 2 题，度数统计

`in_degree` / `out_degree` / `total_degree`，三个排行榜直接查这张表。

### embodied_projects — 第 3 题

| 字段 | 说明 |
|---|---|
| `repo_id` / `repo_type` | 仓库标识 |
| `likes` / `downloads` / `score` | `score = likes*10 + min(downloads, 5000)` |
| `github_url` / `github_verified` | GitHub 地址，以及是否经 API 校验存在（1 = 已确认 200） |
| `space_url` / `space_source` | Space 地址与来源（readme / card / author_list / search / self） |
| `matched_keywords` / `note` | 命中的关键词、人工备注（如"无官方开源代码"） |

---

## 几个关键口径

这些不是实现细节，是答辩时会被追问的地方，写清楚避免误解。

**trending 的"一周"是累积出来的。** HF 的 `sort=trendingScore` 只返回此刻的榜，没有历史接口，所以必须连续 7 个北京日期各跑一次。当前库里有 6 天。

**大V 不是按总粉丝数选的。** HF 只有账号总粉丝，没有"领域粉丝"。若直接按粉丝排，`microsoft`（22,979 粉）会靠 Phi/Azure 的热度挤进第一，但它只有 5 个具身仓库。所以改成按**名下真实具身仓库数与占比**设门槛，种子账号豁免。

**组织账号不能关注别人。** HF 上 org 类型的账号没有 following 列表，出度天然为 0。为了让"这些人关注了谁"这半边有意义，榜单里补入个人账号撑起出度。

**入度不等于真实粉丝数。** 每个大V 最多采 `EDGES_MAX_PER_VIP = 10000` 条粉丝边，超过就截断。`tencent` 真实粉丝 13,070，图内入度显示 10,000，网页上会标 `+`。

**网络是单层星型，不是社群网络。** 只采了大V的粉丝，没做二层扩散，所以大V之间几乎没有互相关注的边，图上呈十个独立星。这是采集口径决定的，不是 bug。

**GitHub 链接必须校验。** 早期版本只从 README 正则抓链接，结果抓出过一个不存在的 `Lerobot/lerobot`。现在每个候选都打 `api.github.com/repos/{owner}/{repo}`，200 才采用，并写回规范化地址。搜不到就标注"无官方开源代码"，不留空。

**Space 找不到就明确写"无"。** 检索顺序：README 链接 → 模型卡 `cardData.demo` → 作者名下 Space 列表 → 全局搜索，每级都验证存在且名字相关（否则 `MiMo-Embodied` 会被配成编码模型 `MiMo-Code` 的 Space）。很多项目确实没有官方 Demo。

---

## 数据现状

| 项 | 现状 |
|---|---|
| trending | 6 天（2026-11-13 ~ 11-18），每天 3 类各前 10 = 30 条 |
| 关注网络 | 10 个大V、26,777 节点、28,982 条边 |
| 具身项目 | 10 个，GitHub 9/10 已校验，Space 2/10（其余确认无官方 Demo） |

## 已知局限

- trending 未满 7 天，页面标题的"一周"目前是 6 天
- 入度受 10,000 条上限截断，与真实粉丝数不等
- 网络只做一层扩散，大V之间无边
- 第 3 题的领域判定用关键词，未接 LLM；`is_weight_only` 之类的规则能挡掉大部分骨干网络权重，但边界情况仍需人工核对
