#!/usr/bin/env python3
"""instrument_audit.py —— **仪器体检**（不是报告审核）。

为什么需要它（与 numeric_audit / viewpoint_audit / report_consistency 的分工）
---------------------------------------------------------------------------
那三套门禁都在问"**这份报告对不对**"。本脚本问的是另一件事：

    **这台仪器（框架+采集器）在多标的总体上可不可靠？**

起因是一次方法性错误：曾用"改动会不会让某个标的的输出变（或变更保守）"来给修复排序 ——
那是**结果锚定**。正确的判据是"能不能归约为一条对任意标的成立、且机器可检的规则"。
本脚本就是把三条这样的规则落成检查：

  A. **归属体检** entity_basis
     框架因子2 明文 `扣掉少数股东权益`（用 n_income_attr_p）。因子3 的
     OCF / capex / 现金 / 有息负债 / 防卫门槛 **全部是合并口径**，没有任何标注。
     规则：声明为"归母"的指标，其分子必须只含归母部分；且必须声明 `attribution_method`
     （按利润份额缩放 = 近似 / 子公司上缴股利 / 集团层面可得现金）。
     ⇒ 未声明 = UNKNOWN（覆盖缺口）；声明 parent 却用合并字段 = FAIL（真实冲突）。

  B. **稳定性体检** parameter_stability
     框架**有意**让 AA_2y / AA_all / AA_latest 并存并取悲观 —— 不稳定本身不是错。
     错的是"**报告了却不拦**"：现有代码给了 30% 阈值提示与 λ 异常警告，但没有一个
     变成门控，于是过半种群带着警告继续走到决策数字。
     规则：任一派生参数在**全部合法变体**上的离散度超阈值 ⇒ 降级为披露项，不得绑定决策。
     （先例：§17.13 在 λ ≤ 0 时 mark(input_inconsistent) 并拒绝出表。）

  C. **命名体检** measure_type
     名字含"回报率/收益率"的指标必须声明 `measure ∈ {distribution, generation}`，
     且 (1−Q) 税收因子只允许出现在 distribution 型。（任何标的跑出的这个数都会被
     读者与股息率对比，而它系统性高出 2–3 倍 ⇒ 命名把"生成能力"冒充成"分配收益"。）

用法::

    .venv/bin/python scripts/instrument_audit.py --root output --output output/instrument_audit.json
    .venv/bin/python scripts/instrument_audit.py --root output --only 00506

退出码：0 无 FAIL · 1 存在 FAIL · 2 输入不可读。
`--strict` 下 UNKNOWN 亦计失败（交付路径）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

PASS, FAIL, WARN, UNKNOWN = "PASS", "FAIL", "WARN", "UNKNOWN"

#: 处置（disposition）—— 决定"能不能阻断"，与 status 是两件事。
#:
#: 为什么不把稳定性直接算成 FAIL 就完事：`AA_window` 在 62/107 个产物上不达标，
#: 若它是 blocking，本工具就**永远** exit 1，于是没有任何调用方能把它当门用
#: （这正是"报告了却不拦"的反面：不是失败太软，而是失败太吵）。
#: 按框架既有模式区分（先例：§17.13 的 `input_inconsistent` **拒绝出表** =
#: blocked；`λ 异常` 只是打印 = 应当 disclose_only）：
#:
#:   blocked       —— 结构性缺陷，**必须**修（如"声明归母却用合并口径"）
#:   disclose_only —— 参数不稳定 ⇒ 该参数降级为披露项、不得绑定决策；
#:                    在报告里披露即可，不在 `--strict` 下不阻断
BLOCKED, DISCLOSE_ONLY = "blocked", "disclose_only"

#: 指标 → 处置
DISPOSITION = {
    ("entity_basis", "17.5.AA"): BLOCKED,
    ("entity_basis", "17.15.net_B"): BLOCKED,
    # 少数股东暴露本身是**已声明的口径属性**，不是实现缺陷 ⇒ 披露级
    # （它只在与"缺乏披露"组合时才成为缺陷，那条由下面的 disclosure 项负责）
    ("entity_basis", "impact"): DISCLOSE_ONLY,
    # **缺披露**是真缺陷：读者会把 GG 当归母可得 ⇒ 阻断
    ("entity_basis", "disclosure"): BLOCKED,
    ("entity_basis", "contract"): BLOCKED,
    ("entity_basis", "17.8.money_cap"): BLOCKED,     # UNKNOWN：待声明
    ("entity_basis", "17.8.ibd"): BLOCKED,
    ("stability", "AA_window"): DISCLOSE_ONLY,
    ("stability", "lambda"): DISCLOSE_ONLY,
    ("stability", "M_method"): DISCLOSE_ONLY,
    ("measure_type", "*"): BLOCKED,
}


def disposition_of(rule: str, metric: str, status: str) -> str:
    """该 finding 的处置。PASS 一律不阻断（不产生处置压力）。"""
    if status == PASS:
        return DISCLOSE_ONLY
    d = DISPOSITION.get((rule, metric))
    if d is None:
        d = DISPOSITION.get((rule, "*"))
    return d or BLOCKED       # 未登记的规则按最严处理（fail-closed）


# ---------------------------------------------------------------------------
# 契约：每个指标的实体归属与计量语义（本文件是**单点声明**）
#
# entity_basis:
#   parent       —— 语义要求"只含归母部分"
#   consolidated —— 语义就是集团整体口径（不需要拆）
#   ambiguous    —— 存量科目（现金/有息负债）：在合并报表里无法机械拆分归母/少数股东，
#                  必须显式声明方法，不得默认当作归母
#
# 字段名（store key）来自 HK_BALANCE_MAP / HK_CASHFLOW_MAP / HK_INCOME_MAP。
# ---------------------------------------------------------------------------
def _load_entity_contract() -> dict:
    """从 `strategy_rules.json` 读实体归属契约（known_issues §四十五）。

    ⚠️ **订正**：本模块初版在代码里**硬编码**了一份契约，并把 §17.5 的 AA 标成
    `entity_basis="parent"`、判它"声明归母却实现合并"⇒ 对 **107/107** 个产物报
    blocked。那是**我凭空造的口径** —— 框架文档里没有"扣掉少数股东权益"这句话；
    相反 `factor3_穿透回报率精算.md:281` 明确写着因子3 的 AA 与因子2 的 C 基准不同
    「是粗算/精算方法论差异的正常结果」。
    ⇒ 现在契约**只有一处**（`strategy_rules.json` 的 `entity_basis`），本模块只做核对。
    """
    import json as _json
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "strategy_rules.json")
    try:
        return _json.loads(open(p, encoding="utf-8").read()).get("entity_basis") or {}
    except (OSError, ValueError):
        return {}


ENTITY_CONTRACT = _load_entity_contract()
#: 契约里的量 → 本模块体检用的指标键
CONTRACT_TO_METRIC = {
    "C": "17.2.C", "B": "17.2.B", "A": "17.2.A",
    "AA": "17.5.AA", "M": "17.10.M", "GG": "17.11.GG", "R": "17.11.R",
}


def _contract_predicates() -> List[dict]:
    """把契约翻译成"该量应当满足什么"的谓词（供体检使用）。"""
    out = []
    for q, spec in (ENTITY_CONTRACT.get("quantities") or {}).items():
        out.append({
            "quantity": q,
            "metric": CONTRACT_TO_METRIC.get(q, q),
            "label": spec.get("label", q),
            "basis": spec.get("entity_basis", "?"),
            "method": spec.get("attribution_method"),
            "caveat": spec.get("caveat"),
            "spec": spec.get("spec"),
        })
    return out


# 名字里出现这些词 ⇒ 必须声明 measure 类型，且不得是裸 (1−Q) 税收口径
RETURN_WORDS = ("回报率", "收益率", "回报", "yield")
DISTRIBUTION_WORDS = ("股息", "分红", "分配", "到手", "税后")
GENERATION_WORDS = ("生成", "创造", "可支配", "结余", "现金")

# 测试/标准样品：未经标注的产物不计入 FAIL，只计 UNKNOWN
STABILITY_THRESHOLDS = {
    "AA_window_spread_pct": 30.0,   # 数据包自带阈值
    "lambda_anomaly": 3.0,          # judgment_examples_turtle.md §3
    "M_method_spread_pct": 15.0,    # 数据包自带阈值（三法偏差）
}


from pack_metrics import (Metrics, build, num,  # noqa: F401 —— num/section_text 对外暴露，兼容既有调用
                         parse_bullets, section_text)


def _metrics(pack_path: str) -> Metrics:
    """读取该产物的结构化数值件。

    迁移要点（P1b）：本脚本原先自带 `_heading_index` / `_sec` / `num` / `_row` /
    `_grab` 五套解析，其中 `_sec` 踩过两个实测坑（`### 17.3` 词边界、`### 17.10` 截断）。
    现在统一走 `pack_metrics` —— 单点。
    """
    import os
    d = os.path.dirname(pack_path)
    mj = os.path.join(d, "metrics.json")
    if os.path.isfile(mj):
        return Metrics(json.loads(open(mj, encoding="utf-8").read()), source=mj)
    # 兼容期：没有 metrics.json 的存量产物显式解析（不静默回落）
    return Metrics(build(open(pack_path, encoding="utf-8").read()),
                   source=f"{pack_path} (parsed)")


def _bullets(m: Metrics, pack_path: str, section: str) -> dict:
    """取列表项赋值；metrics.json 里没有该小节时从 pack 文本补解析。"""
    b = m.bullets(section)
    if b:
        return b
    return parse_bullets(open(pack_path, encoding="utf-8").read()).get(str(section), {})


def _bullet(m: Metrics, pack_path: str, section: str, label: str):
    b = _bullets(m, pack_path, section)
    if label in b:
        return b[label]
    for k, v in b.items():
        if label in k:
            return v
    return None


# ---------------------------------------------------------------------------
# A. 归属体检
# ---------------------------------------------------------------------------
def _coerce(x) -> Tuple[Metrics, str]:
    """把"Metrics 对象"或"pack 文本"统一成 `(Metrics, pack_text)`。

    为什么两种都收：本模块的函数原先签名为 `(pack_text, code)`，
    既有测试与外部调用都按文本传参；迁移到结构化数值件后内部需要 Metrics。
    同时接受二者，避免"迁移"顺手改掉公开签名（那是 API 变更，不是等价迁移）。
    """
    if isinstance(x, Metrics):
        p = x.payload.get("_pack_path")
        text = open(p, encoding="utf-8").read() if p and os.path.isfile(p) else ""
        return x, text
    text = str(x)
    return Metrics(build(text), source="(inline text)"), text


def check_entity_basis(pack, code: str) -> List[dict]:
    """核对产物是否符合 `strategy_rules.json` 的**实体归属契约**。

    这条检查的形态在 2026-10-15 被**订正**过一次，值得记下来：
    初版把 AA 的语义硬编码为"归母可得"，于是判它"声明归母却实现合并"⇒
    对 107/107 个产物报 blocked。**那个判定是错的** —— 框架从未要求 AA 归母，
    它由合并报表构造，且精算文档明说与因子2 的 C 基准不同是"正常结果"。
    现在契约只有一处（`strategy_rules.json`），本检查做两件**可判**的事：
      ① 该量的实体归属是否与契约一致（契约里写的是事实，不是期望）；
      ② 契约要求披露时，产物里**有没有那段披露**（缺披露才是真缺陷）。
    """
    m, text = _coerce(pack)
    out: List[dict] = []

    minority_pct = m.value("17.2", "少数股东占比")
    if minority_pct is None:
        np_tot = m.value("3", "除税后溢利")
        min_g = m.value("3", "少数股东损益")
        if np_tot and min_g is not None and np_tot > 0:
            minority_pct = min_g / np_tot * 100

    preds = _contract_predicates()
    if not preds:
        out.append({"code": code, "rule": "entity_basis", "metric": "contract",
                    "label": "实体归属契约", "status": UNKNOWN,
                    "detail": "读不到 strategy_rules.json 的 entity_basis 契约 —— "
                              "无法核对，按 fail-closed 记 UNKNOWN（不得视为通过）",
                    "minority_pct": minority_pct})
        return out

    for p_ in preds:
        basis, caveat = p_["basis"], p_.get("caveat")
        if basis == "mixed":
            # 混用口径本身是**已声明的设计**；要查的是"有没有披露"（见下）
            out.append({"code": code, "rule": "entity_basis", "metric": p_["metric"],
                        "label": p_["label"], "status": PASS,
                        "detail": f"{p_['spec']}；caveat: {caveat}",
                        "minority_pct": minority_pct})
        else:
            out.append({"code": code, "rule": "entity_basis", "metric": p_["metric"],
                        "label": p_["label"], "status": PASS,
                        "detail": f"实体归属 = {basis}（{p_['spec']}）",
                        "minority_pct": minority_pct})

    # 契约要求的**披露**是否真的出现在产物里
    rule = ENTITY_CONTRACT.get("disclosure_rule") or {}
    thresh = float(rule.get("when_minority_pct_gt") or 5.0)
    if minority_pct is not None and minority_pct > thresh:
        has_1711 = bool(section_text(text, "17.11")) if text else False
        declared_aa = "**合并口径**" in text or "合并口径" in text
        disclosed = "口径混用披露" in text
        if disclosed:
            out.append({"code": code, "rule": "entity_basis", "metric": "disclosure",
                        "label": "口径混用披露", "status": PASS,
                        "detail": f"少数股东占净利 {minority_pct:.2f}% > {thresh:g}%，"
                                  f"§17.11 已声明 GG 为归母上界", "minority_pct": minority_pct})
        elif not has_1711:
            out.append({"code": code, "rule": "entity_basis", "metric": "disclosure",
                        "label": "口径混用披露", "status": UNKNOWN,
                        "detail": f"少数股东占净利 {minority_pct:.2f}%，但 §17.11 未产出 ⇒ "
                                  f"无从检查披露（不是通过）", "minority_pct": minority_pct})
        else:
            out.append({"code": code, "rule": "entity_basis", "metric": "disclosure",
                        "label": "口径混用披露", "status": FAIL,
                        "detail": f"少数股东占净利 {minority_pct:.2f}% > {thresh:g}%，"
                                  f"契约要求披露「AA 合并 / M 归母 ⇒ GG 为归母上界」，"
                                  f"但产物里找不到该披露", "minority_pct": minority_pct})
        out.append({"code": code, "rule": "entity_basis", "metric": "impact",
                    "label": "少数股东暴露（量级）", "status": PASS,
                    "detail": f"少数股东占净利 {minority_pct:.2f}% ⇒ AA（合并）中约 "
                              f"{minority_pct:.2f}% 不归母；这是**已披露的口径属性**，"
                              f"不是实现缺陷", "minority_pct": minority_pct})
    return out


def _read_stability_sidecar(pack_path: str) -> Optional[dict]:
    """读产物自带的 `stability.json`（T4 后它是**参数稳定性的单点**）。

    为什么优先读它而不是重算：参数稳定性的**事实**应由产出的那一方（`derived_metrics`）
    写下来 —— 它用的阈值、口径、单位都在那里。体检若自己重算，就又变成"多读取器"
    （known_issues §三十三），而且阈值一改两边就会不一致。
    没有该文件的存量产物回落自算（显式，不静默）。
    """
    if not pack_path:
        return None
    p = os.path.join(os.path.dirname(pack_path), "stability.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.loads(open(p, encoding="utf-8").read())
    except (OSError, ValueError):
        return None


def check_stability(pack, code: str) -> List[dict]:
    # ⚠️ `_coerce` 返回的是 `(Metrics, **文本**)` —— 不是路径。
    # 初版把第二个元素命名成 `pack_path` 并拿它去拼 stability.json 的路径，
    # 于是 `os.path.dirname(<整份 pack 文本>)` 得到垃圾、sidecar 永远读不到（静默回落自算）。
    # 真正的路径在 `payload["_pack_path"]`（由 audit_one 注入）。
    m, _text = _coerce(pack)
    pack_path = m.payload.get("_pack_path") or ""
    out: List[dict] = []
    # 回落路径要用**文本**（`_pct_from_text` 读正文），sidecar 要用**路径** —— 两者分开。
    _body = _text
    if not _body and pack_path:
        try:
            _body = open(pack_path, encoding="utf-8").read()
        except OSError:
            _body = ""

    side = _read_stability_sidecar(pack_path)
    if side and (side.get("parameters") or {}):
        thr = STABILITY_THRESHOLDS
        for param, spec in sorted((side["parameters"] or {}).items()):
            unstable = bool(spec.get("unstable"))
            st = FAIL if unstable else PASS
            out.append({"code": code, "rule": "stability", "metric": param,
                        "label": {"AA_window": "AA 窗口离散度（AA_2y vs AA_all）",
                                  "lambda": "经营杠杆系数 λ",
                                  "M_method": "M 三法离散度"}.get(param, param),
                        "status": st, "value": spec.get("value"),
                        "sign_unstable": "异号" in str(spec.get("detail") or ""),
                        "detail": (f"（读自 stability.json）{spec.get('detail')}"
                                   f"；阈值 {spec.get('threshold')}"
                                   + ("；**降级为披露项、不得单独绑定决策**" if unstable else ""))})
        if out:
            return out

    aa2 = _bullet(m, pack_path, "17.5", "AA_2y")
    aaal = _bullet(m, pack_path, "17.5", "AA_all")
    if aa2 is not None and aaal is not None:
        # ⚠️ 分母必须 sign-safe：AA 可以为负（负的可支配现金结余），
        # 数据包用「÷ AA_all」的相对差 —— 分母跨零或为负时百分比无意义
        # （实测 600016 民生银行 AA_2y −204,060 / AA_all −8,321.6 仍输出 2352.2%）。
        denom = (abs(aa2) + abs(aaal)) / 2
        # 旧写法 `(aa2 < 0) != (aaal < 0)` 对「两者皆负」返回 False ⇒ 实测漏报。
        sign_unstable = (aa2 < 0) != (aaal < 0) or aa2 < 0 or aaal < 0
        if denom > 0:
            spread = abs(aa2 - aaal) / denom * 100
            st = FAIL if (spread > STABILITY_THRESHOLDS["AA_window_spread_pct"]
                          or sign_unstable) else PASS
            why = []
            if sign_unstable:
                if (aa2 < 0) != (aaal < 0):
                    why.append("AA_2y 与 AA_all **异号**（结余在窗口间跨零）⇒ 相对差不可解释，"
                               "且「取悲观值」在负值上失去意义")
                else:
                    why.append("窗口内 AA **为负**（可支配现金结余为负）⇒ "
                               "「取悲观值」在负值上失去意义，§17.11/§17.14 网格不可解释")
            if spread > STABILITY_THRESHOLDS["AA_window_spread_pct"]:
                why.append("窗口离散度 >30% ⇒ 该参数应降级为披露项，不得绑定决策")
            out.append({"code": code, "rule": "stability", "metric": "AA_window",
                        "label": "AA 窗口离散度（AA_2y vs AA_all）", "status": st,
                        "value": round(spread, 1), "sign_unstable": sign_unstable,
                        "detail": f"{aa2:,.2f} vs {aaal:,.2f} ⇒ 差 {spread:,.1f}%"
                                  + ("；" + "；".join(why) if why else "")})

    lam = _bullet(m, pack_path, "17.5", "经营杠杆系数 λ")
    if lam is not None:
        st = FAIL if (lam > STABILITY_THRESHOLDS["lambda_anomaly"] or lam < 0) else PASS
        out.append({"code": code, "rule": "stability", "metric": "lambda",
                    "label": "经营杠杆系数 λ", "status": st, "value": round(lam, 3),
                    "detail": f"λ = {lam:.3f}"
                              + ("（>3 或 <0 ⇒ 估计量在小分母样本上放大，"
                                 "不得用于实数结论）" if st == FAIL else "")})

    dev = _pct_from_text(_body, "17.10", "两两相对偏差最大") if _body else None
    if dev is not None:
        st = FAIL if dev > STABILITY_THRESHOLDS["M_method_spread_pct"] else PASS
        out.append({"code": code, "rule": "stability", "metric": "M_method",
                    "label": "M 三法离散度", "status": st, "value": round(dev, 1),
                    "detail": f"三法最大相对偏差 {dev:.1f}%"
                              + ("（>15% ⇒ M 口径不收敛，需明示取哪一法及理由）"
                                 if st == FAIL else "")})
    return out


def _pct_from_text(text: str, section: str, keyword: str) -> Optional[float]:
    """在某小节里找「keyword … NN.N%」的百分比。

    为什么不用 bullets：§17.10 那行是 `- **M_rec = 102.48%（法2）** — 三种方法…
    偏差最大 69.2%` —— 同一行有**两个**百分数，`=` 后那个是 M，我们要的是偏差那个。
    故按关键字就近取。
    """
    body = section_text(text, section)
    for line in body.splitlines():
        if keyword in line:
            m = re.search(re.escape(keyword) + r"\s*([0-9]+(?:\.[0-9]+)?)\s*%", line)
            if m:
                return float(m.group(1))
    return None


def check_measure_naming(pack, code: str) -> List[dict]:
    """名字含「回报率/收益率」的指标行，必须声明 measure 类型。

    作用域刻意与参考实现一致：扫**所有以 `|` 开头的行**的首列（表格行），
    而不是只扫 pack_metrics 已解析出的表格 —— 后者会漏掉「格式异常但确实是表格行」
    的实例，而那些正是本题要抓的对象（迁移时实测漏了 1 例）。
    """
    m, text = _coerce(pack)
    out: List[dict] = []
    seen = set()
    if not text:
        # 只有 Metrics 时：扫已解析的表格行（作用域略窄，仅限内部调用）
        for sec in m.section_numbers():
            for label in m.rows(sec):
                if any(w in label for w in RETURN_WORDS) and label not in seen:
                    seen.add(label)
                    declared = any(w in label for w in DISTRIBUTION_WORDS + GENERATION_WORDS)
                    out.append({"code": code, "rule": "measure_type", "metric": label,
                                "label": label, "status": PASS if declared else FAIL,
                                "detail": ("已带分配/生成限定语" if declared else
                                           "名字含「回报率/收益率」但未声明 measure 类型")})
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if not any(w in s for w in RETURN_WORDS):
            continue
        label = s.strip("|").split("|")[0].strip()
        if not any(w in label for w in RETURN_WORDS) or label in seen:
            continue
        seen.add(label)
        declared = any(w in s for w in DISTRIBUTION_WORDS + GENERATION_WORDS)
        out.append({"code": code, "rule": "measure_type", "metric": label,
                    "label": label,
                    "status": PASS if declared else FAIL,
                    "detail": ("已带分配/生成限定语" if declared else
                               "名字含「回报率/收益率」但未声明 measure ∈ {distribution, generation}"
                               " ⇒ 会被读者与股息率直接对比（系统性高出 2–3 倍）")})
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def audit_one(path: str) -> dict:
    code = os.path.basename(os.path.dirname(path))
    m = _metrics(path)
    # 给 Metrics 附上 pack 路径，供 check_measure_naming 精确扫描
    m.payload["_pack_path"] = path
    findings = (check_entity_basis(m, code)
                + check_stability(m, code)
                + check_measure_naming(m, code))
    for f in findings:
        f["disposition"] = disposition_of(f.get("rule", ""), f.get("metric", ""),
                                         f.get("status", ""))
    return {"code": code, "pack": path, "findings": findings}


def render(results: List[dict], strict: bool = False) -> str:
    lines: List[str] = []
    rules = {"entity_basis": "A 归属体检", "stability": "B 稳定性体检",
             "measure_type": "C 命名体检"}
    icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNKNOWN: "❓"}

    # 种群汇总
    lines.append("=" * 78)
    lines.append("仪器体检 · 种群汇总")
    lines.append("=" * 78)
    per_rule: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        for f in r["findings"]:
            per_rule[f["rule"]][f["status"]] += 1
    for rule, name in rules.items():
        c = per_rule.get(rule, Counter())
        lines.append(f"  {name:12s} PASS {c.get(PASS,0):4d} · FAIL {c.get(FAIL,0):4d} · "
                     f"UNKNOWN {c.get(UNKNOWN,0):4d}")
    products = len(results)
    lines.append(f"  产物总数 {products}")

    # A 的量级榜（少数股东暴露）
    exp = {}
    for r in results:
        for f in r["findings"]:
            if f["rule"] == "entity_basis" and f["metric"] == "impact":
                exp[r["code"]] = f["minority_pct"]
    if exp:
        lines.append("")
        lines.append("A · 少数股东暴露（AA 未摊薄的影响面）—— Top 12")
        lines.append(f"    {'产物':28s} {'少数股东占净利':>12s}")
        for code, v in sorted(exp.items(), key=lambda kv: -kv[1])[:12]:
            lines.append(f"    {code:28s} {v:>11.2f}%")
        lines.append(f"    >5% 的产物: {sum(1 for v in exp.values() if v > 5)} / {len(exp)}"
                     f" | >20%: {sum(1 for v in exp.values() if v > 20)}")

    # B 的不稳定榜
    inst = []
    for r in results:
        for f in r["findings"]:
            if f["rule"] == "stability" and f["status"] == FAIL:
                inst.append((f["value"], r["code"], f["metric"]))
    if inst:
        lines.append("")
        lines.append("B · 参数不稳定（应降级为披露项）—— Top 12")
        for v, code, metric in sorted(inst, reverse=True)[:12]:
            lines.append(f"    {code:28s} {metric:12s} {v:>12,.1f}")

    # C 的缺口
    miss = [r["code"] for r in results
            for f in r["findings"] if f["rule"] == "measure_type" and f["status"] == FAIL]
    if miss:
        lines.append("")
        lines.append(f"C · 命名未声明 measure 的产物: {len(set(miss))} 个")
        for code in sorted(set(miss))[:8]:
            lines.append(f"    {code}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="仪器体检（归属 / 稳定性 / 命名），只读")
    ap.add_argument("--root", default="output")
    ap.add_argument("--only", default=None, help="只体检含该子串的产物")
    ap.add_argument("--output", default=None, help="JSON 输出路径")
    ap.add_argument("--strict", action="store_true", help="UNKNOWN 亦计失败")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    packs = sorted(glob.glob(os.path.join(a.root, "*", "data_pack_market.md")))
    if a.only:
        packs = [p for p in packs if a.only in p]
    if not packs:
        print(f"Error: 在 {a.root} 下未找到 data_pack_market.md", file=sys.stderr)
        return 2

    results = []
    for p in packs:
        try:
            results.append(audit_one(p))
        except Exception as exc:  # noqa: BLE001
            results.append({"code": os.path.basename(os.path.dirname(p)), "pack": p,
                            "findings": [{"code": p, "rule": "entity_basis", "metric": "?",
                                          "label": "体检失败", "status": FAIL,
                                          "detail": f"{type(exc).__name__}: {exc}"}]})

    if not a.quiet:
        print(render(results, a.strict))

    fails = sum(1 for r in results for f in r["findings"] if f["status"] == FAIL)
    unk = sum(1 for r in results for f in r["findings"] if f["status"] == UNKNOWN)
    blocked = sum(1 for r in results for f in r["findings"]
                  if f["status"] in (FAIL, UNKNOWN)
                  and f.get("disposition", BLOCKED) == BLOCKED)
    disclose = sum(1 for r in results for f in r["findings"]
                   if f["status"] in (FAIL, UNKNOWN)
                   and f.get("disposition") == DISCLOSE_ONLY)
    print("")
    print(f"合计：FAIL {fails} · UNKNOWN {unk} · 产物 {len(results)}")
    print(f"处置：blocked {blocked}（必须修） ｜ disclose_only {disclose}"
          f"（该参数降级为披露项、不得绑定决策；--strict 下才阻断）")

    if a.output:
        with open(a.output, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "1.0", "root": a.root,
                       "summary": {"products": len(results), "fail": fails, "unknown": unk},
                       "results": results}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"→ {a.output}")

    if blocked:
        return 1
    if a.strict and (disclose or unk):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
