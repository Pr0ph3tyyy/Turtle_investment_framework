"""turtle_core —— 龟龟策略的**纯计算层**（公式单点）。

为什么单独成包
--------------
本项目已经发生过"同一公式在多处重复实现"的事故三次以上：

- 门槛 II 的 ``max(5%, Rf+3%)`` 曾在三处各写一遍，其中一处还是旧档位
  （``known_issues.md`` P1-12）；
- 综合底价曾在 screener 里对 5 个锚点等权平均，而 canonical 已明确
  「①②③ 底价型」与「④⑤ 收益率型」**不得混算**（06862 事故，`derived_metrics.py` 注释记录）；
- 有息负债的组件表被内联复制（`screener_core.py:819,955`）。

设计约束
--------
1. **不取数**：本包不 import tushare、不读网络。输入由调用方（collector / screener）
   组装成结构化对象后传入。
2. **不渲染**：本包不产出任何 markdown。返回值是 dataclass，markdown 由
   ``derived_metrics.py`` 负责渲染，从而"计算"与"排版"可分别测试。
3. **不猜**：缺失输入一律 ``None`` 传播，绝不静默补 0 或用默认值兜底
   （仓库纪律：输入不全 ⇒ ``UNVERIFIABLE``，不凑数）。
4. **单位显式**：每个函数的 docstring 声明输入/输出单位。本仓库混用
   元 / 万元 / 百万元 与 % / 小数，这是历史 bug 的主要来源。
"""

from __future__ import annotations

__version__ = "1.0"

SCHEMA_VERSION = "1.0"

__all__ = ["SCHEMA_VERSION", "__version__"]
