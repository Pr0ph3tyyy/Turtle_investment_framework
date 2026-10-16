"""Turtle Investment Framework - AssemblyMixin + WarningsCollector.

Data pack assembly, derived metrics orchestration, and warning collection.
"""

import re

import pandas as pd

from format_utils import format_number, format_table, format_header
from rf_utils import rf_text_is_failure
from claim_ledger import ClaimLedger
from section_contract import (
    REASONS,
    STATUS_MISSING,
    STATUS_OK,
    SECTION_TITLES,
    SectionRegistry,
)


class AssemblyMixin:
    """Mixin providing data pack assembly for TushareClient."""

    # sub_methods 的标签 → §17 小节号。两份清单必须一一对应：section_contract
    # 的目录是 fail-closed 的，漏登记的小节会被记为 missing 而不是静默通过。
    _LABEL_TO_SECTION = {
        "财务趋势速览": "17.1",
        "因子2输入参数": "17.2",
        "因子3·真实现金收入": "17.3",
        "因子3·经营性现金支出": "17.4",
        "因子3·基准可支配结余": "17.5",
        "因子4·股价分位": "17.6",
        "控股结构辅助": "17.7",
        "因子4·绝对估值与基准价": "17.8",
        "因子4·业绩下滑敏感性": "17.9",
        "支付率三重校验": "17.10",
        "穿透回报率网格": "17.11",
        "G 系数网格": "17.12",
        "收入敏感性": "17.13",
        "悲观基准网格": "17.14",
        "防御性净值": "17.15",
        "防御性门槛组": "17.16",
    }

    def _claim_ledger(self, ts_code: str = "", reset: bool = False) -> ClaimLedger:
        """Lazily create the claim ledger for this run (known_issues.md P2-20 改动 1/2).

        Claims are the machine-checkable substrate: each §17 value the Agent may
        quote is registered with ``(indicator, value, unit, time, basis, policy)``
        so a verifier can bind what the report says back to what Python computed.
        """
        ledger = None if reset else self._store.get("_claim_ledger")
        if ledger is None:
            ledger = ClaimLedger(ts_code)
            self._store["_claim_ledger"] = ledger
        elif ts_code and not ledger.stock_code:
            ledger.stock_code = ts_code
        return ledger

    @staticmethod
    def _aa_slug(label: str) -> str:
        """'AA_2y' → 'aa2y' (used inside claim ids)."""
        return label.lower().replace("_", "").replace(" ", "")

    @staticmethod
    def _method_slug(label: str) -> str:
        """Map an M-method label to an id-safe slug.

        The method belongs **inside** the claim id (it fully identifies the
        quantity) so the human-readable ``indicator`` can stay stable across runs.
        Putting the varying part in the indicator instead makes a legitimate
        recompute look like an id collision.
        """
        fixed = {"法1": "m1", "法2": "m2", "法3": "m3",
                 "承诺(支付率)": "commitpct", "承诺(金额折算)": "commitamt"}
        if label in fixed:
            return fixed[label]
        slug = "".join(ch for ch in str(label) if ch.isalnum())
        return slug or "mx"

    def _section_registry(self, reset: bool = False) -> SectionRegistry:
        """Return this run's §17 status registry.

        Kept in ``_store`` so individual ``_compute_*`` methods can mark their own
        section with a precise reason; assembly falls back to inference when they
        do not. ``reset=True`` starts a fresh registry — required because
        ``compute_derived_metrics`` may be called twice on one client (e.g. the
        3-B flow: run without a rate, seed ``--rf``, run again), and a stale
        ``missing`` must not survive a successful recompute.
        """
        registry = None if reset else self._store.get("_section_registry")
        if registry is None:
            registry = SectionRegistry()
            self._store["_section_registry"] = registry
        return registry

    def compute_derived_metrics(self, ts_code: str) -> str:
        """Compute §17: Derived metrics from stored DataFrames.

        Must be called after all get_* methods have populated self._store.
        """
        lines = [
            format_header(2, "17. 衍生指标（Python 预计算）"),
            "",
            f"> 以下指标基于 §1-§16 原始数据确定性计算，无 LLM 判断成分。Phase 3 可直接引用。{self._unit_label()}。",
            "",
        ]

        # (label, callable) — labels are used to report *which* blocks could not
        # be computed. They intentionally contain no section numbers so that a
        # partially-computed pack is never mistaken for one containing the
        # corresponding grids (see tests/test_derived_metrics*.py).
        sub_methods = [
            ("财务趋势速览", self._compute_financial_trends),
            ("因子2输入参数", lambda: self._compute_factor2_inputs(ts_code)),
            ("因子3·真实现金收入", self._compute_factor3_step1),
            ("因子3·经营性现金支出", self._compute_factor3_step4),
            ("因子3·基准可支配结余", lambda: self._compute_factor3_sensitivity_base(ts_code)),
            ("因子4·股价分位", lambda: self._compute_factor4_inputs(ts_code)),
            ("控股结构辅助", self._compute_sotp_inputs),
            ("因子4·绝对估值与基准价", lambda: self._compute_factor4_ev_baseline(ts_code)),
            ("因子4·业绩下滑敏感性", lambda: self._compute_factor4_sensitivity(ts_code)),
            # 17.10-17.13 grids (run after 17.5 which populates factor3_sensitivity;
            # payout cross-check must precede the penetration/revenue grids which
            # read _store["payout_crosscheck"])
            ("支付率三重校验", self._compute_payout_crosscheck),
            ("穿透回报率网格", lambda: self._compute_penetration_grid(ts_code)),
            ("G 系数网格", lambda: self._compute_g_grid(ts_code)),
            ("收入敏感性", lambda: self._compute_revenue_sensitivity(ts_code)),
            ("悲观基准网格", lambda: self._compute_pessimistic_grid(ts_code)),
            ("防御性门槛组", lambda: self._compute_defensive_gates(ts_code)),
            ("防御性净值", lambda: self._compute_defensive_net_asset(ts_code)),
        ]

        registry = self._section_registry(reset=True)
        # 账本在此建立，确保 stock_code 正确（§17.10 早于 §17.11 运行，
        # 若等到 §17.11 才建，早先登记的声明会带上空 stock_code）
        self._claim_ledger(ts_code, reset=True)
        missing: list[str] = []
        for label, method in sub_methods:
            number = self._LABEL_TO_SECTION.get(label)
            # §17.9 按文档必须与 §17.11 表B **同源 M**（§17.10 的 M_rec），但渲染顺序上
            # §17.9 在 §17.10 之前 ⇒ 不先算就会静默回落到 §17.1 的 3 年均值，并且仍在包里
            # 打印「口径与 §17.11 表B 一致」（实测 06862：M 89.70% vs M_rec 81.11%，
            # base GG 6.44% vs 5.82%，同包自相矛盾；独立审计 P0）。这里先**静默算一次**
            # （返回值丢弃，随后在其正常位置渲染）；该函数每次重建同一个 dict，可重入、不耗 API。
            if number == "17.9" and not self._store.get("payout_crosscheck"):
                try:
                    self._compute_payout_crosscheck()
                except Exception:  # noqa: BLE001 — 与主循环一致：单节失败不中断整包
                    pass
            try:
                result = method()
                if result:
                    lines.append(result)
                    lines.append("")
                    if number and not registry.is_marked(number):
                        registry.mark(number, STATUS_OK)
                else:
                    # Previously this was silently dropped, so a data-pack could
                    # lose e.g. the penetration-return grid with no trace at all.
                    missing.append(label)
                    if number and not registry.is_marked(number):
                        registry.mark(number, STATUS_MISSING,
                                      self._infer_missing_reason(label, ts_code))
            except Exception as e:
                name = getattr(method, "__name__", str(method))
                lines.append(f"*{name} 计算失败: {e}*")
                lines.append("")
                missing.append(label)
                if number:
                    # 用 _safe_mark：标记失败不得顶替原始异常、也不得中断整包
                    self._safe_mark(registry, number, STATUS_MISSING, "exception", note=str(e))

        # 始终输出状态表：缺席必须可判定，且缺失原因要指向补救动作
        lines.append(self._section_status_block(registry, ts_code))
        lines.append("")
        units = self._contract_units(ts_code)
        self._store["section17_status"] = registry.finalize(ts_code, units)
        if "_claim_ledger" in self._store:
            self._store["claims_payload"] = self._store["_claim_ledger"].snapshot(units)
        return "\n".join(lines)

    def _close_available(self) -> bool:
        """True when a usable close price is in ``_store["basic_info"]``."""
        basic = self._store.get("basic_info")
        if basic is None or getattr(basic, "empty", True):
            return False
        try:
            value = basic.iloc[0].get("close")
        except (IndexError, AttributeError):
            return False
        try:
            return value is not None and float(value) > 0
        except (TypeError, ValueError):
            return False

    def _safe_mark(self, registry, number, status, reason, note=None):
        """错误处理器专用的 `mark()`：**记录失败但绝不抛**。见 known_issues.md §三十。

        `SectionRegistry.mark()` 在「number 未知」或「reason 不在受控词表」时会
        `raise SectionMarkError`。而它被调用的位置是 `except` 分支 —— 一旦它自己抛，
        一个**受控的缺失**会升级成**整包生成失败**，且原始异常被顶掉。
        错误处理器不得比它处理的错误更脆弱。
        """
        try:
            registry.mark(number, status, reason, note=note)
        except Exception as e:  # noqa: BLE001 —— 就是要吞掉，但必须留痕
            self._store.setdefault("_mark_failures", []).append(
                f"§{number} 标记失败（status={status} reason={reason}）：{type(e).__name__}: {e}")

    def _infer_missing_reason(self, label: str, ts_code: str) -> str:
        """Best-effort reason code for a section that returned nothing.

        Only used when the ``_compute_*`` method did not mark its own section;
        ``no_data`` is the honest fallback (it asserts nothing specific).
        """
        rf_df = self._store.get("risk_free_rate")
        rf_unavailable = rf_df is None or getattr(rf_df, "empty", False)
        if rf_unavailable and label in self._RF_DEPENDENT_LABELS:
            return "no_rf"
        if label == "控股结构辅助":
            return "not_applicable"
        # 收盘价缺失优先于市场特性：它会让所有依赖价格/股数的小节一并缺席，
        # 而"港股字段缺口"只是其中一个可能原因（实测 2026-10-13：hk_daily 配额用尽）
        if not self._close_available():
            return "no_price"
        if self._is_hk(ts_code) and self._LABEL_TO_SECTION.get(label) in ("17.6", "17.8"):
            return "hk_field_gap"
        return "no_data"

    def _contract_units(self, ts_code: str) -> dict:
        """Unit/currency declarations for the §17 contract payload.

        ``reporting_currency`` is only asserted when it was *explicitly* confirmed
        (fx.json / --fx-hkd-cny); otherwise it stays ``null`` with a warning rather
        than defaulting to the ticker suffix — defaulting is how the HK CNY/HKD
        mixup survived (known_issues.md P0-2).
        """
        units = {
            "trading_currency": self._detect_trading_currency(ts_code),
            "reporting_currency": self._reporting_currency(ts_code),
            "amount_unit": "百万",
            "unit_label": self._unit_label(),
        }
        if units["reporting_currency"] is None:
            units["reporting_currency_unconfirmed"] = True
        fx = self._store.get("fx") or {}
        if fx.get("factor_to_reporting"):
            units["fx"] = {k: fx[k] for k in ("factor_to_reporting", "date", "source") if k in fx}
        return units

    def _section_status_block(self, registry: SectionRegistry, ts_code: str) -> str:
        """Render the §17 status table for the data pack.

        This replaces the previous prose-only "以下预计算小节未生成" notice. The
        critical behavioural change is the last line: a non-ok section means
        **stop**, not "hand-calculate it" — the 4 hand-calculation fallbacks in
        ``phase3_quantitative.md`` were exactly how the core invariant
        "LLM 不做算术" got violated (known_issues.md P2-20).
        """
        # 无编号：这是**关于 §17 的元数据**，不是 §17 的计算小节。
        # 若编号（如 17.17）会被 headings_in_pack 误判为数据小节，破坏
        # 「`### 17.N` 存在 ⟺ status ok」这条不变式。
        header = [format_header(3, "§17 小节状态（机器可读副本：section17_status.json）"), ""]
        rows = []
        bad = []
        for number in sorted(SECTION_TITLES, key=lambda n: int(n.split(".")[1])):
            mark = registry.finalize(ts_code, {})["sections"].get(number, {})
            status = mark.get("status", STATUS_MISSING)
            if status == STATUS_OK:
                icon, detail = "✅ ok", "—"
            elif status == "degraded":
                icon, detail = "⚠️ degraded", mark.get("note") or mark.get("reason_label", "")
            else:
                icon = "❌ missing"
                detail = mark.get("reason_label", "")
                if mark.get("remedy"):
                    detail += f" → 补救：{mark['remedy']}"
                bad.append(number)
            rows.append([f"§{number}", SECTION_TITLES[number], icon, detail or "—"])
        body = format_table(["小节", "标题", "状态", "原因 / 补救"],
                            rows, alignments=["l", "l", "l", "l"])
        note = ("> **状态语义**：`ok` = 数值可直接引用；`degraded` = 已生成但带降级说明；"
                "`missing` = **未生成**。")
        if bad:
            note += (f"\n> ⚠️ **本节缺失：{'、'.join(bad)}** —— "
                     "**不得手算、不得按 0 处理**。请停止该小节的引用，"
                     "把缺失原因与补救动作写入报告 Warnings 后继续其余小节。")
        else:
            note += "\n> ✅ 全部小节均已生成。"
        return "\n".join(header + [body, "", note])

    # Blocks that cannot be computed without a risk-free rate (§14)
    #: 缺 Rf（⇒ 缺 II）时必然算不出来的小节。**§17.14 悲观基准网格必须在内** ——
    #: 它的目标买入价 = 现价 × GG_pess / II、`vs 门槛` = GG_pess − II，与 §17.11 同依赖。
    #: 漏掉的后果是**原因码写错**：实测（00696.HK，不传 --rf，2026-10-14）
    #:   §17.9/§17.11/§17.13 → `no_rf`
    #:   §17.14            → `no_data`  ← 错
    #: `no_rf` 的补救是「传 --rf」，`no_data` 的补救是「去找上游数据」——
    #: 标错会把读者引向完全错误的修法（findings D-9）。
    _RF_DEPENDENT_LABELS = frozenset({
        "因子4·业绩下滑敏感性",
        "穿透回报率网格",
        "收入敏感性",
        "悲观基准网格",
    })

    @staticmethod
    def _parse_sections(content: str):
        """Split data_pack_market.md content into header, sections list, and footer.

        Returns:
            (header_str, sections_list, footer_str)
            - header_str: everything before the first ``## `` line
            - sections_list: list of (key, content) tuples preserving original order.
              key is the section ID string (e.g., "1", "2", "3P", "9B", "17", "13").
              content is the full text from ``## N.`` to just before the next ``## ``.
            - footer_str: trailing ``---`` line and completion summary (if any)
        """
        # Pattern: ## <digits><optional uppercase letter(s)>. <title>
        section_re = re.compile(r"^(## (\d+[A-Z]?)\. .*)$", re.MULTILINE)

        matches = list(section_re.finditer(content))
        if not matches:
            return content, [], ""

        header = content[: matches[0].start()]

        sections = []
        for i, m in enumerate(matches):
            key = m.group(2)
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections.append((key, content[start:end]))

        # Separate footer from the last section: look for trailing "---\n*共" pattern
        footer = ""
        if sections:
            last_key, last_text = sections[-1]
            footer_re = re.compile(r"\n---\n\*共 .+$", re.DOTALL)
            fm = footer_re.search(last_text)
            if fm:
                footer = last_text[fm.start():]
                sections[-1] = (last_key, last_text[: fm.start()])

        return header, sections, footer

    def _build_header(self, ts_code: str) -> str:
        """Build the data pack header block (title, timestamp, source, unit).

        Returns:
            Header string ending with a blank line (ready to be followed by sections).
        """
        timestamp = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        currency = self._detect_currency(ts_code)
        self._currency = currency
        unit_label = self._unit_label()
        lines = [
            format_header(1, f"数据包 — {ts_code}"),
            "",
            f"*生成时间: {timestamp}*",
            f"*数据来源: Tushare Pro*",
            f"*金额单位: {unit_label} (除特殊标注)*",
        ]
        # 交易币种 ≠ 报表币种（known_issues.md P0-2）。旧版把 `_detect_currency`
        # 的结果标成"报表币种"，而它其实是交易币种 —— 06862 的报表币种是 CNY。
        lines.append(f"*交易币种: {currency}（价格与市值口径）*")
        reporting = self._reporting_currency(ts_code)
        if reporting:
            lines.append(f"*报表币种: {reporting}（三张财务报表口径）*")
        else:
            lines.append("*报表币种: **未确认**（港股需 fx.json / --fx-hkd-cny 显式确认）*")
        lines.extend(["", "---", ""])
        return "\n".join(lines)

    @staticmethod
    def _check_staleness(content: str) -> int:
        """Return the number of days since the data pack was generated.

        Parses ``*生成时间: YYYY-MM-DD HH:MM:SS*`` from the content header.
        Returns 999 if the timestamp cannot be found or parsed.
        """
        m = re.search(r"\*生成时间:\s*(\d{4}-\d{2}-\d{2})", content)
        if not m:
            return 999
        try:
            gen_date = pd.Timestamp(m.group(1))
            now = pd.Timestamp.now().normalize()
            return (now - gen_date.normalize()).days
        except Exception:
            return 999

    # Map section IDs to the fetch methods and their display names
    _REFRESH_SECTIONS = {"1", "2", "11", "14"}

    def refresh_market_sections(self, ts_code: str, existing_content: str) -> str:
        """Re-fetch market-sensitive sections and merge with existing data pack.

        Only sections 1 (基本信息), 2 (市场行情), 11 (十年周线行情), and 14 (无风险利率)
        are refreshed. All other sections are preserved unchanged.

        Args:
            ts_code: Stock code (e.g., '600887.SH').
            existing_content: Full text of the existing data_pack_market.md.

        Returns:
            Complete markdown string with refreshed header and market sections.
        """
        _header, sections, footer = self._parse_sections(existing_content)

        # Build mapping from section ID to fetch callable
        fetch_map = {
            "1": ("1. 基本信息", self.get_basic_info),
            "2": ("2. 市场行情", self.get_market_data),
            "11": ("11. 十年周线行情", self.get_weekly_prices),
            "14": ("14. 无风险利率", self.get_risk_free_rate),
        }

        # Detect currency for formatting (needed by get_* methods)
        currency = self._detect_currency(ts_code)
        self._currency = currency

        # Re-fetch each refreshable section; keep old on failure
        new_sections = []
        for key, text in sections:
            if key in fetch_map:
                name, method = fetch_map[key]
                try:
                    print(f"  Refreshing {name}...")
                    fresh_md = method(ts_code)
                    # Ensure section text ends with a newline for clean joining
                    if not fresh_md.endswith("\n"):
                        fresh_md += "\n"
                    # §14 protection: a failed API fetch returns a "数据缺失"
                    # string *without raising*, so the exception handler below
                    # never fires and a manually supplemented risk-free rate
                    # (e.g. WebSearch fallback written by the coordinator) was
                    # silently overwritten. Keep the usable old text instead.
                    if key == "14" and rf_text_is_failure(fresh_md) and not rf_text_is_failure(text):
                        print("  ⚠️ §14 无风险利率刷新失败（接口无权限），保留数据包中已有数值")
                        new_sections.append((key, text))
                        continue
                    new_sections.append((key, fresh_md))
                except Exception as e:
                    print(f"  ⚠️ Failed to refresh {name}: {e}, keeping old data")
                    new_sections.append((key, text))
            else:
                new_sections.append((key, text))

        # Build new header with refresh-mode annotation
        new_header = self._build_header(ts_code)
        new_header += "*刷新模式: --refresh-market（仅更新 §1/§2/§11/§14）*\n"
        new_header += "\n"

        # Reassemble
        parts = [new_header]
        for _key, text in new_sections:
            parts.append(text)

        result = "".join(parts)

        # Re-attach footer
        if footer:
            result = result.rstrip("\n") + footer
        else:
            result = result.rstrip("\n") + "\n"

        return result

    # --- Feature #28: Full data_pack_market.md assembly ---

    def assemble_data_pack(self, ts_code: str) -> str:
        """Assemble complete data_pack_market.md combining all sections."""
        timestamp = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        currency = self._detect_currency(ts_code)
        self._currency = currency
        unit_label = self._unit_label()
        lines = [
            format_header(1, f"数据包 — {ts_code}"),
            "",
            f"*生成时间: {timestamp}*",
            f"*数据来源: Tushare Pro*",
            f"*金额单位: {unit_label} (除特殊标注)*",
        ]
        # 交易币种 ≠ 报表币种（known_issues.md P0-2）。旧版把 `_detect_currency`
        # 的结果标成"报表币种"，而它其实是交易币种 —— 06862 的报表币种是 CNY。
        lines.append(f"*交易币种: {currency}（价格与市值口径）*")
        reporting = self._reporting_currency(ts_code)
        if reporting:
            lines.append(f"*报表币种: {reporting}（三张财务报表口径）*")
        else:
            lines.append("*报表币种: **未确认**（港股需 fx.json / --fx-hkd-cny 显式确认）*")
        lines.extend(["", "---", ""])

        if self._is_us(ts_code):
            sections = [
                ("1. 基本信息", self.get_basic_info),
                ("2. 市场行情", self.get_market_data),
                ("3. 合并利润表", self.get_income),
                # No §3P for US (US GAAP: no parent/consolidated split)
                ("4. 合并资产负债表", self.get_balance_sheet),
                # No §4P for US
                ("5. 现金流量表", self.get_cashflow),
                ("6. 分红历史", self.get_dividends),
                ("7. 股东与治理", self.get_holders),
                ("9. 主营业务构成", self.get_segments),
                ("11. 十年周线行情", self.get_weekly_prices),
                ("12. 关键财务指标", self.get_fina_indicators),
                ("15. 股票回购", self.get_repurchase),
                ("16. 股权质押", self.get_pledge_stat),
            ]
        elif self._is_hk(ts_code):
            sections = [
                ("1. 基本信息", self.get_basic_info),
                ("2. 市场行情", self.get_market_data),
                ("3. 合并利润表", self.get_income),
                # No §3P for HK (HKFRS does not split parent/consolidated)
                ("4. 合并资产负债表", self.get_balance_sheet),
                # No §4P for HK
                ("5. 现金流量表", self.get_cashflow),
                ("6. 分红历史", self.get_dividends),
                ("7. 股东与治理", self.get_holders),       # placeholder
                ("9. 主营业务构成", self.get_segments),      # placeholder
                ("11. 十年周线行情", self.get_weekly_prices),
                ("12. 关键财务指标", self.get_fina_indicators),
                ("15. 股票回购", self.get_repurchase),       # placeholder
                ("16. 股权质押", self.get_pledge_stat),      # placeholder
            ]
        else:
            sections = [
                ("1. 基本信息", self.get_basic_info),
                ("2. 市场行情", self.get_market_data),
                ("3. 合并利润表", self.get_income),
                ("3P. 母公司利润表", self.get_income_parent),
                ("4. 合并资产负债表", self.get_balance_sheet),
                ("4P. 母公司资产负债表", self.get_balance_sheet_parent),
                ("5. 现金流量表", self.get_cashflow),
                ("6. 分红历史", self.get_dividends),
                ("7. 股东与治理", self.get_holders),
                ("9. 主营业务构成", self.get_segments),
                ("11. 十年周线行情", self.get_weekly_prices),
                ("12. 关键财务指标", self.get_fina_indicators),
                ("15. 股票回购", self.get_repurchase),
                ("16. 股权质押", self.get_pledge_stat),
            ]

        completed = 0
        for name, method in sections:
            try:
                print(f"  Collecting {name}...")
                section_md = method(ts_code)
                lines.append(section_md)
                lines.append("")
                completed += 1
            except Exception as e:
                # Attempt yfinance fallback for market data sections
                yf_data = self._yf_fallback_price(ts_code)
                if yf_data and name in ("1. 基本信息", "2. 市场行情"):
                    lines.append(format_header(2, name))
                    lines.append(f"\n*来源: yfinance (降级)*")
                    if yf_data.get("close"):
                        lines.append(f"- 当前价格: {yf_data['close']}")
                    if yf_data.get("market_cap"):
                        lines.append(f"- 总市值: {format_number(yf_data['market_cap'], divider=1e6)}")
                    lines.append("")
                    completed += 1
                else:
                    lines.append(format_header(2, name))
                    lines.append(f"\n数据获取失败: {e}\n")

        # Audit info (sub-section of 7)
        try:
            audit_md = self.get_audit(ts_code)
            lines.append(audit_md)
            lines.append("")
        except Exception:
            pass

        # Risk-free rate
        try:
            print("  Collecting 14. 无风险利率...")
            rf_md = self.get_risk_free_rate(ts_code)
            lines.append(rf_md)
            lines.append("")
        except Exception as e:
            lines.append(format_header(2, "14. 无风险利率"))
            lines.append(f"\n数据获取失败: {e}\n")

        # Agent-only placeholder sections (§8, §10)
        for sec_num, sec_name in [
            ("8", "行业与竞争"),
            ("10", "管理层讨论与分析 (MD&A)"),
        ]:
            lines.append(format_header(2, f"{sec_num}. {sec_name}"))
            lines.append("")
            lines.append(f"*[§{sec_num} 待Agent补充]*")
            lines.append("")

        # §17 Derived metrics (pre-computed from stored DataFrames)
        try:
            print("  Computing 17. 衍生指标...")
            derived_md = self.compute_derived_metrics(ts_code)
            lines.append(derived_md)
            lines.append("")
        except Exception as e:
            lines.append(format_header(2, "17. 衍生指标（Python 预计算）"))
            lines.append(f"\n计算失败: {e}\n")

        # §13 Warnings: auto-detect + agent placeholder
        wc = WarningsCollector()
        try:
            if self._is_us(ts_code) or self._is_hk(ts_code):
                # HK: use stored data instead of re-calling A-share-only APIs
                for label, store_key in [
                    ("合并利润表", "income"),
                    ("合并资产负债表", "balance_sheet"),
                    ("现金流量表", "cashflow"),
                ]:
                    stored = self._store.get(store_key)
                    wc.check_missing_data(label, stored if stored is not None else pd.DataFrame())
            else:
                # A-share: Check missing data + YoY anomaly for core financial
                # statements. Read from _store (populated by get_income/
                # get_balance_sheet/get_cashflow) instead of re-fetching, which
                # eliminates redundant API calls. YoY scan window is the ~5y
                # display window (consistent with the HK/US branch above).
                for label, store_key, cols in [
                    ("合并利润表", "income", ["revenue", "n_income_attr_p"]),
                    ("合并资产负债表", "balance_sheet", ["total_assets"]),
                    ("现金流量表", "cashflow", ["n_cashflow_act"]),
                ]:
                    df = self._store.get(store_key)
                    if df is None:
                        df = pd.DataFrame()
                    wc.check_missing_data(label, df)
                    if not df.empty and "end_date" in df.columns:
                        # Filter to annual reports only (end_date ending in "1231")
                        annual = df[df["end_date"].astype(str).str.endswith("1231")].copy()
                        annual = annual.sort_values("end_date", ascending=False)
                        if not annual.empty:
                            dates = annual["end_date"].astype(str).str[:4].tolist()
                            for col in cols:
                                if col in annual.columns:
                                    wc.check_yoy_change(label, col, annual[col].tolist(), dates=dates)

                # Audit risk check — read stored fina_audit (newest-first)
                audit_df = self._store.get("fina_audit")
                if audit_df is not None and not audit_df.empty and "audit_result" in audit_df.columns:
                    wc.check_audit_risk(str(audit_df.iloc[0].get("audit_result", "")))

            # Balance sheet risk checks (goodwill, debt ratio) — use stored data
            # (store rows are newest-first, so iloc[0] is the latest period).
            bs_df = self._store.get("balance_sheet")
            if bs_df is not None and not bs_df.empty:
                latest = bs_df.iloc[0]
                gw = latest.get("goodwill", 0) or 0
                ta = latest.get("total_assets", 0) or 0
                tl = latest.get("total_liab", 0) or 0
                wc.check_goodwill_ratio(float(gw), float(ta))
                wc.check_debt_ratio(float(tl), float(ta))
            # Dividend data correction warning (from _get_dividends_hk)
            div_warn = self._store.get("_dividend_warning")
            if div_warn:
                wc.warnings.append({
                    "type": "DATA_CORRECTION",
                    "severity": "中",
                    "message": div_warn,
                })
        except Exception:
            pass  # warnings are best-effort; don't block assembly

        # Build §13 with two sub-sections
        lines.append(format_header(2, "13. 风险警示"))
        lines.append("")
        lines.append("### 13.1 脚本自动检测")
        lines.append("")
        if wc.warnings:
            high = [w for w in wc.warnings if w["severity"] == "高"]
            medium = [w for w in wc.warnings if w["severity"] == "中"]
            low = [w for w in wc.warnings if w["severity"] == "低"]
            for sev_label, items in [("高风险", high), ("中风险", medium), ("低风险", low)]:
                if items:
                    lines.append(f"**{sev_label}:**")
                    for w in items:
                        lines.append(f"- [{w['type']}|{w['severity']}] {w['message']}")
                    lines.append("")
        else:
            lines.append("未检测到异常。")
            lines.append("")
        if self._is_us(ts_code):
            lines.append("")
            lines.append("> 美股数据覆盖有限：§9业务构成/§15回购 暂缺，"
                         "§16质押不适用（美股无此制度），"
                         "§3P/§4P母公司报表在US GAAP体系下不适用，c_pay_to_staff 不可用。")
            lines.append("")
        elif self._is_hk(ts_code):
            lines.append("")
            lines.append("> 港股数据覆盖有限：§9业务构成/§15回购 暂缺，"
                         "§16质押不适用（港股无此制度），"
                         "§3P/§4P母公司报表在HKFRS体系下不适用，c_pay_to_staff 不可用。")
            lines.append("")

        lines.append("### 13.2 Agent 补充")
        lines.append("")
        lines.append("*[§13.2 待Agent补充]*")
        lines.append("")

        lines.append("---")
        lines.append(f"*共 {completed}/{len(sections)} 个数据板块成功获取*")

        return "\n".join(lines)


class WarningsCollector:
    """Auto-detect anomalies during data collection (Feature #30)."""

    def __init__(self):
        self.warnings = []

    def check_missing_data(self, section_name: str, df: pd.DataFrame):
        """Warn if a data section returned empty."""
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            self.warnings.append({
                "type": "DATA_MISSING",
                "severity": "中",
                "message": f"{section_name} 数据缺失",
            })

    def check_yoy_change(self, section_name: str, field_name: str,
                         values: list, threshold: float = 3.0,
                         dates: list = None):
        """Warn if year-over-year change exceeds threshold (e.g., 300%)."""
        for i in range(len(values) - 1):
            curr, prev = values[i], values[i + 1]
            if prev is not None and curr is not None and float(prev) != 0:
                try:
                    change = abs(float(curr) / float(prev) - 1)
                    if change > threshold:
                        period = ""
                        if dates and i + 1 < len(dates):
                            period = f"{dates[i+1]}→{dates[i]} "
                        self.warnings.append({
                            "type": "YOY_ANOMALY",
                            "severity": "高",
                            "message": f"{section_name}/{field_name}: "
                                       f"{period}同比变化 {change*100:.0f}% 超过 {threshold*100:.0f}% 阈值",
                        })
                except (ValueError, ZeroDivisionError):
                    pass

    def check_audit_risk(self, audit_opinion: str):
        """Warn if audit opinion is not clean."""
        if audit_opinion and audit_opinion not in ("标准无保留意见", "—", ""):
            self.warnings.append({
                "type": "AUDIT_RISK",
                "severity": "高",
                "message": f"审计意见非标准: {audit_opinion}",
            })

    def check_goodwill_ratio(self, goodwill: float, total_assets: float):
        """Warn if goodwill/total_assets > 20%."""
        if goodwill and total_assets and total_assets > 0:
            ratio = float(goodwill) / float(total_assets)
            if ratio > 0.20:
                self.warnings.append({
                    "type": "GOODWILL_RISK",
                    "severity": "高",
                    "message": f"商誉占总资产比例 {ratio*100:.1f}% 超过 20%",
                })

    def check_debt_ratio(self, total_liab: float, total_assets: float):
        """Warn if debt ratio > 70%."""
        if total_liab and total_assets and total_assets > 0:
            ratio = float(total_liab) / float(total_assets)
            if ratio > 0.70:
                self.warnings.append({
                    "type": "LEVERAGE_RISK",
                    "severity": "中",
                    "message": f"资产负债率 {ratio*100:.1f}% 超过 70%",
                })

    def format_warnings(self) -> str:
        """Format all collected warnings as section 13 markdown."""
        lines = [format_header(2, "13. 风险警示 (脚本自动生成)"), ""]

        if not self.warnings:
            lines.append("未检测到异常。")
            return "\n".join(lines)

        # Group by severity
        high = [w for w in self.warnings if w["severity"] == "高"]
        medium = [w for w in self.warnings if w["severity"] == "中"]
        low = [w for w in self.warnings if w["severity"] == "低"]

        if high:
            lines.append("**高风险:**")
            for w in high:
                lines.append(f"- [{w['type']}] {w['message']}")
            lines.append("")
        if medium:
            lines.append("**中风险:**")
            for w in medium:
                lines.append(f"- [{w['type']}] {w['message']}")
            lines.append("")
        if low:
            lines.append("**低风险:**")
            for w in low:
                lines.append(f"- [{w['type']}] {w['message']}")
            lines.append("")

        lines.append(f"*共 {len(self.warnings)} 条自动警示*")
        return "\n".join(lines)
