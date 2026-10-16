"""选股器（独立模块）—— 与 analysis 零文件耦合。

定位：用**宽松指标**从全市场筛出 ~100 只候选，供人挑选后再跑 analysis。
刻意**不依赖** §17 数据包 / claims.json / 常备池（那是 analysis 侧的东西）。

模块：
    universe.py  A 股取数（Tushare）+ 通用卫生过滤
    hk.py        港股：hk_basic / hk_fina_indicator / hk_tradecal
    quotes.py    报价多源（Yahoo 主源 + 东财备选 + **来源报告**）
    rules.py     A/HK 两套宽松规则（阈值外置）
    report.py    CSV + Markdown + 来源报告
"""

__all__ = ["universe", "hk", "quotes", "rules", "report"]
