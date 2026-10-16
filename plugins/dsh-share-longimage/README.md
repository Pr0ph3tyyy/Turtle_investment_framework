# dsh-plugin-share-longimage

把龟龟投资框架某个个股产物目录（`output/{代码}_{公司}/`）里**已审计的成果文件**，
渲染成一张适合分享到 B 站的**中文精选长图**，并按内容块边界切好分P图片。

* 版式：Claude 文档风格 —— 暖纸底、衬线大标题、发丝分隔线、克制的陶土色强调。
* 刻度：按「手机上看长图」定标（1080 宽下正文 30px，手机上约 11–12pt），
  而不是按桌面网页定标；长句两端对齐并用 `text-justify: inter-ideograph` 处理中文。
* 内容：由 Python 从报告与 sidecar 文件**机械摘录**，剥离 `claim` 标记后原样呈现，
  **不做任何新计算、不推断、不改写数值** —— 与 `AGENTS.md` 的分工一致。

## 提供的工具

| 工具 | 作用 |
|:--|:--|
| `share_longimage` | 渲染精华长图 + 分P，并把整图登记为交付物 |

产出的文件（都在个股产物目录里）：

```
output/{code}_{company}/
├── {公司}_{代码}_精华长图.png          # 主产物：一张无缝长图
├── {公司}_{代码}_精华长图_分P/          # B 站动态最多 9 图，便于九宫格分发
│   ├── {公司}_{代码}_精华长图_01.png
│   └── …
└── _share_longimage.html               # 中间 HTML（版式可复核、可手动改）
```

## 用法

工具参数（都可省略）：

| 参数 | 说明 |
|:--|:--|
| `dir` | 个股产物目录，如 `output/06862_海底捞`；相对路径按会话工作目录解析。 |
| `code` | 股票代码，如 `06862.HK`。省略 `dir` 时用它在 `output/` 下查找唯一匹配目录。 |
| `report` | 显式指定成果 markdown；默认自动发现（分析报告 > 估值报告 > `qualitative_report.md`）。 |
| `brief` | 可选的 `brief.json`，人工精选文案覆盖自动抽取（见下）。 |
| `theme` | `paper`（默认，暖纸底）/ `ink`（深色）。 |
| `width` / `panelHeight` | 图片宽度（默认 1080）/ 分P目标高度（默认 1440）。 |
| `maxLongHeight` | 整图高度上限（默认 16000）；超过则只出分P。 |
| `split` / `longImage` | 是否输出分P / 整图（默认都输出；不能同时为 false）。 |
| `out` | 整图输出路径。 |

典型调用：

```
share_longimage(code="06862.HK")                    # 最省事
share_longimage(dir="output/600941_中国移动")
share_longimage(dir="output/06862_海底捞", theme="ink", panelHeight=1600)
share_longimage(code="06862.HK", brief="output/06862_海底捞/brief.json")
```

不等 DSH，也可以直接跑渲染脚本：

```bash
.venv/bin/python plugins/dsh-share-longimage/assets/render_longimage.py \
  --dir output/06862_海底捞            # 加 --json 输出机器可读结果
```

## 自动抽取的内容

按成果类型走两条路径，缺料就整块略去，不会留空壳：

* **分析报告（v2 / §17）** —— 结论词（`仓位建议`，取不到动作词时回退到「一句话结论」的加粗片段）、
  核心指标卡（悲观档 GG_pess / 中性档 GG / 门槛 II / 安全边际 KK / 价值陷阱 / 外推可信度）、
  最大优势与最大风险、六维速览、估值推导表、投资论点卡、风险提示、
  口径缺口、以及**可信度门禁表**。
* **定性 / 估值报告（含 v1 旧格式）** —— 按标题别名降级抽取：执行摘要 / 核心判断、
  六维速览、参数表、投资逻辑、风险与不确定性。

「可信度」区块直接读 sidecar 文件，不做推断：

| 行 | 来源 |
|:--|:--|
| 机构不变量 | `invariants.json` 的 PASS / FAIL 计数与失败 codes |
| §17 小节状态 | `section17_status.json` 的 `ok` 计数与 fail-closed 记录 |
| claim 账本 | `claims.json` 的条数 |
| 维度契约 | `dims/D*.json` 的份数 |
| 报告级门禁 | `consistency_report.md` 的硬门槛结论与跨段冲突数 |

## `brief.json`（可选文案覆盖）

只覆盖你写了的字段，其余仍走自动抽取。空模板见
[`assets/brief.example.json`](assets/brief.example.json)。

```json
{
  "framework": "龟龟投资策略",
  "title": "海底捞",
  "code": "06862.HK",
  "subtitle_kind": "投资分析",
  "meta": "数据截止 FY2025 年报 ｜ 最新股价 9.30 港元",
  "verdict_word": "不建仓",
  "verdict_tone": "negative",
  "verdict_line": "一句话结论……",
  "verdict_kv": [{ "label": "目标买入价", "value": "8.67 港元", "note": "悲观档唯一买点" }],
  "kpis": [{ "label": "穿透回报率", "value": "4.66%", "note": "低于门槛", "tone": "negative" }],
  "notes": [{ "label": "最大风险", "text": "……", "tone": "clay" }],
  "dimensions": [{ "id": "D1", "name": "商业模式", "headline": "……", "confidence": "中", "status": "充分" }],
  "table": { "columns": ["参数", "取值"], "rows": [["Owner Earnings", "4,049.82 百万元"]] },
  "thesis": { "core": "……", "bullets": ["……"] },
  "risks": ["……"],
  "limits": ["……"],
  "gates": [{ "label": "机构不变量", "value": "4 / 7 PASS", "note": "未通过：I4、I3、I5", "tone": "amber" }],
  "footer": { "tags": ["Tushare Pro"], "lines": ["……"] }
}
```

`tone` 取值：`negative`（陶土色）/ `positive`（绿）/ `neutral`（墨色）/ `amber`（琥珀）。

## 配置

`cordis.patch.yml` 里的 `config`（缺省即用默认值）：

| 字段 | 默认 | 说明 |
|:--|:--|:--|
| `pythonPath` | `null` | 留空自动探测：工作目录向上找 `.venv/bin/python`，否则 `python3`。 |
| `chromePath` | `null` | 留空自动探测 Chrome/Chromium（macOS 常见路径 + `PATH`）。 |
| `width` | `1080` | 图片宽度。 |
| `panelHeight` | `1440` | 分P目标高度。 |
| `maxLongHeight` | `16000` | 整图高度上限。 |
| `theme` | `paper` | 默认配色。 |
| `timeoutMs` | `600000` | 渲染子进程超时。 |
| `presentDeliverables` | `true` | 是否把整图登记为交付物卡片。 |

## 实现说明

* **零第三方 import**：`lib/host.js`（宿主半边）只用 `node:` 内建模块，通过注入的 `tools` 服务
  直接注册原始 `ToolDefinition`，因此 profile 里不需要安装任何 `@deepseek-ai/*` 包
  （profile 的 `pnpm-workspace.yaml` 设了 `autoInstallPeers: false`，peer 不会被自动装上）。
  `Config` 用**手写 Standard Schema** 校验，同样不依赖 `schemastery`。
* **零图像库**：渲染脚本只用标准库 + 本机 Chrome 无头模式。
  先用 `--dump-dom` 让页面里的测量脚本回报每个区块的 `offsetTop/height` 与总高，
  再用 `--screenshot` 按精确高度出整图；分P则在同一个 HTML 上注入
  `#clip{height:H;overflow:hidden} + #stage{margin-top:-offset}` 后逐片重渲染，
  并在底部叠一条 `n/N` 页脚。**不依赖 Pillow / Playwright**。
* **切分不切字**：分P边界只落在区块边界（贪心装箱），因此不会把一行字切两半。
* **缺 Chrome 不伪造**：找不到 Chrome 时仍写出 HTML 并如实报错（`ok: false` + warning），
  不假装成功。

## 已知边界

* 分P按区块装箱，所以各张高度不齐（目标高度是上限，不是固定值）——这样比留大片空白好。
* 整图过高（默认 > 16000px）时只出分P，因为超长图在手机上会被压缩到不可读。
* 中文正文依赖系统字体：`Hiragino Sans GB` / `Songti SC`（标题衬线）在 macOS 上通常可用；
  CSS 里已给出多种回退。
