#!/usr/bin/env python3
"""Turtle Investment Framework - Tushare Data Collector (Phase 1A).

Facade module: re-exports all public names and defines TushareClient
which inherits from mixin classes in tushare_modules/.

Collects 5 years of financial data from Tushare Pro API and outputs
a structured data_pack_market.md file.

Usage:
    python3 scripts/tushare_collector.py --code 600887.SH
    python3 scripts/tushare_collector.py --code 600887.SH --output output/data_pack.md
    python3 scripts/tushare_collector.py --code 600887.SH --dry-run
"""

import argparse
import functools
import os
import sys
import time

import pandas as pd
import tushare as ts

try:
    import yfinance as yf
    _yf_available = True
except ImportError:
    _yf_available = False

from config import get_token, get_api_url, validate_stock_code
from format_utils import format_number, format_table, format_header

# Re-export all constants and mixin classes for backward compatibility.
# Tests and external code import these from tushare_collector directly:
#   from tushare_collector import TushareClient, WarningsCollector, rate_limit
#   from tushare_collector import _VIP_MAP, HK_INCOME_MAP, US_INCOME_MAP
from tushare_modules import (
    _VIP_MAP,
    HK_INCOME_MAP, HK_BALANCE_MAP, HK_CASHFLOW_MAP,
    US_INCOME_MAP, US_BALANCE_MAP, US_CASHFLOW_MAP,
    _YF_INCOME_MAP, _YF_BALANCE_MAP, _YF_CASHFLOW_MAP,
    field_names, field_mode,
    ii_threshold, ii_label, II_FLOOR, II_SPREAD,
    InfrastructureMixin, YFinanceMixin, FinancialsMixin,
    OtherDataMixin, DerivedMetricsMixin, AssemblyMixin,
    WarningsCollector,
)

# ============================================================
# TTL cache configuration for heavy (non-market) endpoints
# ============================================================
# Map endpoint (logical name, before VIP mapping) → TTL tier.
#   "financial": statements & indicators — refresh only a few times/year
#   "market":    price/rate series — refresh daily
# Endpoints NOT listed here are never cached (e.g. daily/daily_basic/
# hk_daily/us_daily intraday prices; *_basic use their own file cache).
_CACHE_TTL_CATEGORY = {
    # --- Financial statements & indicators (168h / 7-day TTL) ---
    "income": "financial",
    "balancesheet": "financial",
    "cashflow": "financial",
    "dividend": "financial",
    "fina_indicator": "financial",
    "fina_audit": "financial",
    "fina_mainbz": "financial",
    "top10_holders": "financial",
    "repurchase": "financial",
    "pledge_stat": "financial",
    "hk_income": "financial",
    "hk_balancesheet": "financial",
    "hk_cashflow": "financial",
    "hk_fina_indicator": "financial",
    "us_income": "financial",
    "us_balancesheet": "financial",
    "us_cashflow": "financial",
    "us_fina_indicator": "financial",
    # --- Market data (24h TTL) ---
    "weekly": "market",
    "yc_cb": "market",
}

_CACHE_TTL_HOURS = {"financial": 168, "market": 24}


def rate_limit(func):
    """Decorator to enforce a delay between Tushare API calls.

    The delay (seconds) is read from the ``TUSHARE_RATE_DELAY`` env var at
    call time (default ``0.5``). Set to ``0`` to disable throttling entirely
    (useful when a broker/VIP endpoint has no rate limit). ``time.sleep`` is
    called through the module-level ``time`` import so tests can patch it.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            delay = float(os.environ.get("TUSHARE_RATE_DELAY", "0.5"))
        except ValueError:
            delay = 0.5
        if delay > 0:
            time.sleep(delay)
        return func(*args, **kwargs)
    return wrapper


class TushareClient(
    InfrastructureMixin,
    YFinanceMixin,
    FinancialsMixin,
    OtherDataMixin,
    DerivedMetricsMixin,
    AssemblyMixin,
):
    """Client for Tushare Pro API with rate limiting and retry logic."""

    MAX_RETRIES = 5
    RETRY_DELAY = 2.0  # seconds between retries

    BASIC_CACHE_TTL = 7 * 86400  # 7 days in seconds

    def __init__(self, token: str):
        # Deliberately NOT calling ts.set_token(): it writes ~/tk.csv into the
        # user's home directory, which raises PermissionError under the DSH
        # default sandbox (workspace-write). Passing the token to pro_api() is
        # equivalent and keeps the collector runnable without widening the
        # sandbox policy.
        self.pro = ts.pro_api(token=token, timeout=30)
        self.token = token
        self._store = {}  # {key: pd.DataFrame} for derived metrics computation
        self._yf_available = _yf_available
        self._cache_dir = os.path.join("output", ".collector_cache")
        self._cache_enabled = True  # TTL cache for heavy financial endpoints
        self._ttl_cache = None       # lazily constructed ScreenerCache
        self._fy_end_month: int = 12  # default: calendar year
        self._currency: str = "CNY"
        # Broker API support: route calls through custom URL + enable VIP endpoints
        api_url = get_api_url()
        self._vip_mode = bool(api_url)
        if api_url:
            self.pro._DataApi__token = token
            self.pro._DataApi__http_url = api_url

    @rate_limit
    def _safe_call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """Call a Tushare API endpoint with retry logic.

        Auto-upgrades to VIP endpoints when broker is active.

        Args:
            api_name: The API endpoint name (e.g., 'stock_basic').
            **kwargs: Parameters passed to the API call.

        Returns:
            DataFrame with results.

        Raises:
            RuntimeError: After MAX_RETRIES failures.
        """
        # Auto-upgrade to VIP endpoint when broker is active
        effective_name = api_name
        if self._vip_mode and api_name in _VIP_MAP:
            effective_name = _VIP_MAP[api_name]

        last_err = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                api_func = getattr(self.pro, effective_name)
                df = api_func(**kwargs)
                return df
            except Exception as e:
                last_err = e
                if attempt < self.MAX_RETRIES:
                    is_conn_err = isinstance(e, (ConnectionError, OSError)) or \
                        "RemoteDisconnected" in type(e).__name__ or \
                        "ConnectionAborted" in str(e) or \
                        "RemoteDisconnected" in str(e)
                    if is_conn_err:
                        print(f"[retry {attempt}/{self.MAX_RETRIES}] {effective_name}: connection error, re-creating API client...", file=sys.stderr)
                        # Re-create with the token explicitly: relying on the
                        # ~/tk.csv file that set_token() would have written is
                        # not possible under the DSH sandbox.
                        self.pro = ts.pro_api(token=self.token, timeout=30)
                        # Re-apply broker hacks after re-creating client
                        api_url = get_api_url()
                        if api_url:
                            self.pro._DataApi__token = self.token
                            self.pro._DataApi__http_url = api_url
                    else:
                        print(f"[retry {attempt}/{self.MAX_RETRIES}] {effective_name}: {e}", file=sys.stderr)
                    time.sleep(self.RETRY_DELAY * attempt)
        raise RuntimeError(
            f"Tushare API '{effective_name}' failed after {self.MAX_RETRIES} retries: {last_err}"
        )

    def _cached_basic_call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """Call stock_basic/hk_basic with 7-day file cache."""
        ts_code = kwargs.get("ts_code", "all")
        cache_file = os.path.join(self._cache_dir, f"{api_name}_{ts_code}.json")
        if os.path.exists(cache_file):
            mtime = os.path.getmtime(cache_file)
            if time.time() - mtime < self.BASIC_CACHE_TTL:
                return pd.read_json(cache_file)
        df = self._safe_call(api_name, **kwargs)
        if not df.empty:
            os.makedirs(self._cache_dir, exist_ok=True)
            df.to_json(cache_file, orient="records", force_ascii=False)
        return df

    def _cached_us_daily(self, ts_code: str = None) -> pd.DataFrame:
        """Fetch us_daily with same-day file cache (bulk all-stock fetch).

        First call fetches ALL US stocks (limit=6000) and caches to Parquet.
        Subsequent same-day calls read from cache and filter by ts_code.
        """
        cache_file = os.path.join(self._cache_dir, "us_daily_all.parquet")
        today = pd.Timestamp.now().strftime("%Y%m%d")

        # Check cache: file exists AND was created today
        if os.path.exists(cache_file):
            mtime = os.path.getmtime(cache_file)
            cache_date = pd.Timestamp.fromtimestamp(mtime).strftime("%Y%m%d")
            if cache_date == today:
                df = pd.read_parquet(cache_file)
                if ts_code:
                    df = df[df["ts_code"] == ts_code]
                return df

        # Bulk fetch all US stocks
        df = self._safe_call("us_daily", limit=6000,
                             fields="ts_code,trade_date,open,high,low,close,"
                                    "vol,amount,pe,pb,total_mv")
        if not df.empty:
            os.makedirs(self._cache_dir, exist_ok=True)
            df.to_parquet(cache_file, index=False)

        if ts_code and not df.empty:
            df = df[df["ts_code"] == ts_code]
        return df

    # --- Phase 2.3: TTL cache for heavy financial endpoints ---

    def _get_ttl_cache(self):
        """Lazily build a ScreenerCache rooted at ``{self._cache_dir}/ttl``.

        Re-derives from the current ``self._cache_dir`` at call time; if the
        directory changed since the last build (e.g. tests override
        ``_cache_dir`` after construction), a fresh cache object is created.
        """
        from cache_utils import ScreenerCache
        ttl_dir = os.path.join(self._cache_dir, "ttl")
        if self._ttl_cache is None or self._ttl_cache.cache_dir != ttl_dir:
            self._ttl_cache = ScreenerCache(ttl_dir)
        return self._ttl_cache

    @staticmethod
    def _ttl_cache_key(api_name: str, kwargs: dict) -> str:
        """Build a deterministic cache key for a cacheable endpoint call.

        Keyed on the LOGICAL api_name (stable across VIP/non-VIP), the ts_code
        (so per-stock invalidation via ``collector_{ts_code}_`` works), and a
        deterministic signature of the sorted kwargs (so report_type=1 vs 6 are
        cached separately).
        """
        ts_code = kwargs.get("ts_code", "global")
        sig = ";".join(f"{k}={kwargs[k]}" for k in sorted(kwargs))
        return f"collector_{ts_code}_{api_name}_{sig}"

    def _cached_call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """Call a Tushare endpoint through the TTL disk cache when eligible.

        Non-cacheable endpoints (or when ``_cache_enabled`` is False) pass
        straight through to ``_safe_call``. On a cache hit the cached DataFrame
        is returned with no ``_safe_call`` (hence no rate-limit sleep). Empty
        results are never cached.
        """
        category = _CACHE_TTL_CATEGORY.get(api_name)
        if category is None or not self._cache_enabled:
            return self._safe_call(api_name, **kwargs)

        cache = self._get_ttl_cache()
        ttl_seconds = _CACHE_TTL_HOURS[category] * 3600
        key = self._ttl_cache_key(api_name, kwargs)

        cached = cache.get(key, ttl_seconds)
        if cached is not None:
            return cached

        df = self._safe_call(api_name, **kwargs)
        if df is not None and not df.empty:
            cache.put(key, df)
        return df


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect financial data from Tushare Pro API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --code 600887.SH
  %(prog)s --code 600887 --output output/data_pack_market.md
  %(prog)s --code 00700.HK --extra-fields balancesheet.defer_tax_assets
  %(prog)s --code 600887.SH --cache-refresh   # 财报季（4/8/10月）建议刷新
  %(prog)s --code 002984.SZ --rf 1.688        # yc_cb 无权限时显式提供无风险利率

Note: 财报接口结果会缓存 7 天（行情/利率 24 小时）。财报季（4/8/10 月）
建议加 --cache-refresh 以获取最新披露的财报数据。

无风险利率（§14）无接口权限时，请用 --rf 或 TUSHARE_RF 提供，
否则依赖它的预计算网格无法生成（数据包内会明确标注）。
        """,
    )
    parser.add_argument(
        "--code",
        required=True,
        help="Stock code (e.g., 600887.SH, 000858.SZ, 00700.HK, or plain digits)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Tushare API token (defaults to TUSHARE_TOKEN env var)",
    )
    parser.add_argument(
        "--output",
        default="output/data_pack_market.md",
        help="Output file path (default: output/data_pack_market.md)",
    )
    parser.add_argument(
        "--extra-fields",
        nargs="*",
        help="Additional fields to fetch (format: endpoint.field_name)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print parsed arguments and exit without calling API",
    )
    parser.add_argument(
        "--refresh-market",
        action="store_true",
        help="Only refresh market-sensitive sections (§1/§2/§11/§14) in existing data pack",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the TTL disk cache for financial endpoints (always fetch live)",
    )
    parser.add_argument(
        "--cache-refresh",
        action="store_true",
        help="Invalidate this stock's cached financial data before collecting "
             "(recommended during earnings season: 4/8/10 月)",
    )
    parser.add_argument(
        "--rf",
        type=float,
        default=None,
        metavar="PCT",
        help="Explicit 10-year risk-free rate in percent (e.g. 1.688). Needed "
             "when the yc_cb endpoint has no permission: without a rate, the "
             "risk-free-dependent derived blocks (业绩下滑敏感性 / 穿透回报率网格 / "
             "收入敏感性) cannot be generated. Falls back to TUSHARE_RF, then to "
             "a rate already present in the output data pack's §14. A live API "
             "value, when available, still wins.",
    )
    parser.add_argument(
        "--fx-hkd-cny",
        type=float,
        default=None,
        metavar="RATE",
        help="HKD→CNY factor used to convert an HK listing's trading-currency "
             "market cap into its REPORTING currency (e.g. 0.86466). Needed "
             "because most mainland A+H / red-chip issuers report in CNY while "
             "trading in HKD; without it R/GG is understated by ~15%%. Falls back "
             "to TUSHARE_FX_HKD_CNY, then {output_dir}/fx.json, then a rate "
             "already recorded in the data pack's 协调器注. See scripts/fx_utils.py.",
    )
    parser.add_argument(
        "--shares",
        type=float,
        default=None,
        metavar="百万股",
        help="年报的**已发行股数**（百万股）。known_issues.md #25：股本必须取年报口径，"
             "不能用「市值 ÷ 现价」反推（实测 06862 差 2.79%%）。也可写 "
             "{output_dir}/shares.json 或设 TUSHARE_SHARES_MM。",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="显式接受**带缺陷交付**：存在阻断级门禁失败时仍以退出码 0 结束，并把 "
             "degraded=true 与失败清单写入 gate_status.json（必须随报告披露）。"
             "默认行为是 fail-closed —— 有阻断级失败即退出码 1。",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="⚠️ 跳过**跨进程采集锁**。仅在你已确认无人并发采集时使用 —— "
             "两个流程共用同一 Tushare token 与 output 树，并发会导致对方拿到空数据"
             "（小节变 no_data 且不报错）或覆盖对方正在读的产物。",
    )
    parser.add_argument(
        "--lock-wait",
        type=float,
        default=3600.0,
        help="等锁的最长秒数（默认 3600）。超时即拒绝并退出码 3，**不并发硬闯**。",
    )
    return parser.parse_args()


def _resolve_rf(args) -> tuple[float | None, str | None]:
    """Resolve the risk-free rate from --rf, TUSHARE_RF, or the existing pack.

    Returns ``(value_pct, source_label)``. Explicit CLI/env input wins over the
    data pack so a manual value is never lost to a stale file.
    """
    if args.rf is not None:
        value = args.rf
        if not 0 < value < 15:
            print(f"Error: --rf {value} out of plausible range (0, 15)", file=sys.stderr)
            sys.exit(1)
        return value, "--rf"

    env_rf = os.environ.get("TUSHARE_RF")
    if env_rf:
        try:
            value = float(env_rf)
        except ValueError:
            print(f"Error: TUSHARE_RF='{env_rf}' is not a number", file=sys.stderr)
            sys.exit(1)
        if not 0 < value < 15:
            print(f"Error: TUSHARE_RF={value} out of plausible range (0, 15)", file=sys.stderr)
            sys.exit(1)
        return value, "TUSHARE_RF env"

    # 3-B: read back a rate already present in the data pack (§14), e.g. one the
    # coordinator supplied via WebSearch. Without this the manual value could
    # never reach the Python pre-computation.
    try:
        from pathlib import Path
        path = Path(args.output)
        if path.exists():
            from rf_utils import parse_rf_from_data_pack
            value, date_str = parse_rf_from_data_pack(path.read_text(encoding="utf-8"))
            if value is not None:
                label = f"数据包 §14 ({date_str})" if date_str else "数据包 §14"
                return value, label
    except OSError as e:
        print(f"  ⚠️ 无法读取数据包以复用无风险利率: {e}", file=sys.stderr)

    return None, None


def _seed_risk_free_rate(client, args) -> None:
    """Seed ``client._store["risk_free_rate"]`` before collection.

    A live API value overwrites this later (fresher data wins), but when the
    endpoint has no permission the seeded value is what makes the
    risk-free-dependent derived blocks computable.
    """
    value, source = _resolve_rf(args)
    if value is None:
        return
    from rf_utils import make_rf_dataframe
    client._store["risk_free_rate"] = make_rf_dataframe(value)
    # §14 的失败分支要用它渲染来源（否则包里会出现「§14 数据缺失」与
    # 「§17.2 Rf 来自 §14」自相矛盾，见 other_data._render_seeded_rf）
    client._store["risk_free_rate_source"] = source
    print(f"  ✓ 无风险利率 Rf = {value}% (来源: {source})")


def _seed_fx(client, args) -> None:
    """Seed ``client._store["fx"]`` (trading → reporting currency factor).

    Without it, an HK listing whose financials are reported in CNY but traded in
    HKD would compute ``R = 净利润(CNY) / 市值(HKD)``, understating the return by
    ~15.65% (known_issues.md P0-2). Non-HK codes are unaffected.
    """
    code = getattr(args, "code", "") or ""
    if ".HK" not in code.upper():
        return
    from fx_utils import make_fx_store_entry, resolve_fx_hkd_cny
    factor, source, reporting = resolve_fx_hkd_cny(
        getattr(args, "fx_hkd_cny", None), args.output)
    if factor is None:
        print("  ⚠️ 港股未提供 HKD/CNY 汇率：R/GG 将按未换算计算（若报表币种非港元会低估约 15%）")
        print("     请用 --fx-hkd-cny <rate>、TUSHARE_FX_HKD_CNY，或写入 {output_dir}/fx.json")
        return
    # reporting_currency 必须一并带上，否则 _unit_label() 会退回"币种未确认"
    # （实测 2026-10-13：fx.json 里有 CNY，但 seed 时丢掉了）
    client._store["fx"] = make_fx_store_entry(
        factor, source=source, reporting_currency=reporting)
    _ccy = f"，报表币种 {reporting}" if reporting else "，**报表币种未确认**"
    print(f"  ✓ 汇率 HKD/CNY = {factor} (来源: {source}{_ccy})")


def _seed_shares(client, args) -> None:
    """Seed 股本（known_issues.md #25，2026-10-14 用户拍板：以年报已发行股数为准）。

    没有申报股本时解析器会降级到「期末市值÷期末价」，再降级到「当前市值÷当前价」，
    并把降级标记写进 §1 与 `section17_status.json` —— 一个不知道自己是降级值的降级值，
    正是 #25 反复复发的原因。
    """
    from pathlib import Path
    client._store["_output_path"] = args.output        # 让解析器能找到 shares.json
    if getattr(args, "shares", None):
        client._store["shares_mm"] = float(args.shares)
        print(f"  ✓ 股本: {args.shares:,.2f} 百万股（--shares 显式提供）")
        return
    p = Path(args.output).parent / "shares.json"
    if p.is_file():
        try:
            import json
            d = json.loads(p.read_text(encoding="utf-8"))
            print(f"  ✓ 股本: {d.get('shares_mm')} 百万股"
                  f"（来源: {d.get('source') or 'shares.json'}"
                  + (f"，日期 {d.get('date')}" if d.get("date") else "") + "）")
        except (OSError, ValueError) as e:
            print(f"  ⚠️ shares.json 读取失败，将按降级口径反推: {e}", file=sys.stderr)
    else:
        print("  ⚠️ 未提供年报股本（--shares / TUSHARE_SHARES_MM / shares.json）："
              "将按「期末市值÷期末价」反推并在 §1 标注为降级值")


def _seed_dividend_commitment(client, args) -> None:
    """Seed ``client._store["dividend_commitment"]`` from ``{output_dir}/dividend_commitment.json``.

    Documented factor-2 rule (``known_issues.md`` §3.21 21b): a minimum payout
    ratio / amount **written in a public filing** anchors M outright
    (``[1:09:42]``), ahead of any historical extrapolation. The file is produced
    by the annual-report extraction step (P14); absence just means §17.10 falls
    back to its three-method cross-check.
    """
    try:
        from pathlib import Path
        path = Path(args.output).parent / "dividend_commitment.json"
        if not path.exists():
            return
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            return
        client._store["dividend_commitment"] = data
        kind = data.get("kind") or "无"
        detail = (f"最低支付率 {data['min_payout_pct']}%"
                  if data.get("min_payout_pct") is not None
                  else (f"最低金额 {data['min_amount_mm']} 百万"
                        if data.get("min_amount_mm") is not None else "无具体数值"))
        print(f"  ✓ 分红承诺（{kind}）: {detail}（来源: {data.get('source') or '未标注'}）")
    except (OSError, ValueError) as e:
        print(f"  ⚠️ dividend_commitment.json 读取失败，忽略: {e}", file=sys.stderr)


def _write_field_absence(client, output_path: str) -> None:
    """Persist the *reason* a debt line is blank → ``{output_dir}/field_absence.json``.

    为什么需要：C2（债务构成可核验）只能看到"声明了却全年度为空"这个**结果**，
    分不清「这家发行人真的没有银行借款」与「我们的映射标签没命中上游」（06862 的 P0）。
    判据在**上游科目清单**里，所以由采集器在拿到 raw 行项目时记录，随包交付给 C2。

    fail-closed：没有这个 sidecar（或其中没有某项的 ``verified_absent`` 证据），
    C2 保持原来的 FAIL 语义 —— 于是"删掉 sidecar"不是一条洗白路径。
    """
    try:
        from pathlib import Path
        import json

        journal = client._store.get("field_absence") or {}
        out = Path(output_path).parent / "field_absence.json"
        if not journal:
            # 显式写出空清单（而不是不写）：让"本次确实查过、结论是没有缺失"与
            # "sidecar 被删/未生成"在文件层面可区分。后者 C2 按 FAIL 处理。
            journal = {}
        payload = {
            "schema_version": "1.0",
            "stock_code": getattr(client, "stock_code", "") or "",
            "method": ("upstream_line_item_absence (hk_balancesheet/us_balancesheet 长表 ind_name 清单)"
                       if not str(getattr(client, "stock_code", "") or "").upper().endswith(
                           (".SH", ".SZ", ".BJ"))
                       else "upstream_field_all_nan (A 股 balancesheet 宽表字段，仅能证明『上游没给数』)"),
            "note": ("verified_absent = 上游行项目清单里没有该科目名，**且**上游所有债类名字都被"
                     "映射认领（可核验；映射写错时 unclaimed 非空 ⇒ 证据被拒）；"
                     "reported_zero = 上游显式给 0；mapping_incomplete = 上游存在无人认领的"
                     "债类科目 ⇒ 疑似映射错位（不得放行）；unreported = 字段全 NaN，"
                     "只说明上游没给数，**不构成**科目不存在的证据 ⇒ C2 仍判 FAIL。"),
            "fields": journal,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        n = sum(1 for e in journal.values() if e.get("status") == "verified_absent")
        if n:
            print(f"  科目缺失证据: {n} 项 verified_absent → {out.name}")
    except Exception as e:  # never fail the collection over a sidecar
        print(f"  ⚠️ field_absence.json 写入失败: {e}", file=sys.stderr)


def _stamp_rules_version(output_path: str) -> None:
    """给产物的 JSON 文件加盖 `rules_version`（陈旧检测的前提，known_issues §三十五）。

    为什么每个都盖而不是只在 manifest 上盖：审计是**分布式读取**的 ——
    `viewpoint_audit` 只读 claims、选股器只读 claims+section17_status，
    它们不会去看 manifest。锚必须在各自读到的文件里。
    """
    import json as _json          # 模块级没有 json，必须局部引入（否则 NameError）
    from pathlib import Path as _Path
    try:
        import rules_version as _rv
        rv = _rv.compute()
    except Exception as e:  # noqa: BLE001
        # 锚是"可判陈旧"的前提，不是数值本身；缺它不阻断，但必须可见。
        print(f"  ⚠️ 规则版本锚计算失败（产物将标记为 unanchored）：{type(e).__name__}: {e}",
              file=sys.stderr)
        return
    out_dir = str(_Path(output_path).parent)
    for name in ("claims.json", "section17_status.json", "invariants.json",
                 "field_coverage.json", "pack_manifest.json", "gate_status.json"):
        p = os.path.join(out_dir, name)
        if not os.path.isfile(p):
            continue
        # 只吞"文件不可读/不是合法 JSON"这类**预期内**失败；编程错误必须炸出来
        # （本函数初版把整个循环体包在 try 里，结果 NameError 被悄悄吞掉）。
        try:
            raw = open(p, encoding="utf-8").read()
            d = _json.loads(raw)
        except (OSError, ValueError) as e:
            print(f"  ⚠️ {name} 未加版本锚：{type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not isinstance(d, dict):
            continue
        d["rules_version"] = rv
        with open(p, "w", encoding="utf-8") as fh:
            _json.dump(d, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    print(f"  规则版本锚: {rv}（{len([n for n in ('claims.json','section17_status.json','invariants.json','field_coverage.json','pack_manifest.json','gate_status.json') if os.path.isfile(os.path.join(out_dir, n))])} 个文件）")


def _write_stability_sidecar(client, output_path: str) -> None:
    """写 `{output_dir}/stability.json` —— 参数稳定性的**机器可读声明**。

    为什么必须落成产物（known_issues §四十七，T4）：
    参数不稳定原先只是 `instrument_audit` 一次性扫描出来的结论 —— **报告侧不知道**，
    于是不稳定的标的照样出买点。"报告了不拦等于没拦"（§四十二 的教训）。
    现在这份事实随产物走，体检/选股/报告都读**同一份**，不再各自重算。

    语义：`disposition = "disclose_only"` **不是通过**，而是
    「该参数降级为披露项、不得单独绑定决策」。
    """
    try:
        from pathlib import Path
        import json as _json
        params = (getattr(client, "_store", {}) or {}).get("stability") or {}
        rv = ""
        try:
            import rules_version as _rv
            rv = _rv.compute()
        except Exception:  # noqa: BLE001
            pass
        payload = {
            "schema_version": "1.0",
            "rules_version": rv,
            "note": ("参数稳定性声明。disclose_only = 该参数不稳定 ⇒ 降级为披露项，"
                     "不得单独绑定决策（不是'通过'）。阈值来自 strategy_rules.stability_gates。"),
            "parameters": params,
            "unstable_count": sum(1 for v in params.values() if v.get("unstable")),
        }
        out = Path(output_path).parent / "stability.json"
        out.write_text(_json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        n = payload["unstable_count"]
        print(f"  参数稳定性: {len(params)} 项已声明"
              + (f"（其中 **{n} 项 unstable ⇒ 披露项**）" if n else "（全部稳定）"))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ stability.json 写入失败: {type(e).__name__}: {e}", file=sys.stderr)


def _write_metrics_sidecar(data_pack: str, output_path: str) -> None:
    """写 `{output_dir}/metrics.json` —— 数据包的**结构化数值件**（P1）。

    为什么：审计层原先用正则把 markdown 读回来，18 个脚本里有 5–11 份重复解析器
    （known_issues.md §三十三）。本文件把"取值"收敛成一个实现：计算层产出它，
    审计/下游只经 `pack_metrics` 读取；markdown 保留给人看。

    写完立即跑**双跑比对**（metrics.json vs 重新解析 pack）——不等下游发现，
    在本阶段就把"生产者写错"暴露出来。
    """
    try:
        from pathlib import Path
        import pack_metrics as pm

        out_dir = Path(output_path).parent
        rules_version = ""
        try:
            import rules_version as _rv
            rules_version = _rv.compute()
        except Exception:  # noqa: BLE001
            pass
        path = pm.write(str(out_dir), data_pack, rules_version=rules_version)
        cmp = pm.compare(str(out_dir), data_pack)   # 显式传文本：落盘名不一定是 data_pack_market.md
        if cmp["ok"]:
            print(f"  结构化数值件: {Path(path).name}（双跑比对 {cmp['checked']} 行一致）")
        else:
            print(f"  ⛔ metrics.json 双跑比对不一致（{len(cmp['mismatches'])} 处）：", file=sys.stderr)
            for m in cmp["mismatches"][:5]:
                print(f"     · {m}", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"  ⛔ metrics.json 生成失败：{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def _write_section_status(client, data_pack: str, output_path: str,
                          allow_degraded: bool = False) -> dict:
    """Write ``section17_status.json`` next to the data pack.

    Also verifies the ``heading present iff status ok`` invariant, so a mismatch
    between the pack and the contract is reported at generation time rather than
    discovered later by a human.

    Returns the **gate verdict** (see :func:`_gate_verdict`), which ``main`` turns
    into an exit code.
    """
    payload = client._store.get("section17_status")
    if not payload:
        # --refresh-market 路径：只刷 §1/§2/§11/§14，**不重建 §17**（AGENTS.md）。
        # 旧实现在此直接 return ⇒ 刷新后连"价格与网格是否同源"都没人查。
        # 现在仍然跑不变式与字段覆盖（二者只读 pack 文本与 fx.json，不需要 API），
        # 并把"§17 未重算"作为一条披露级信号。
        print("  ⚠️ 未生成 section17_status.json（本次运行未计算 §17 衍生指标）")
        try:
            _write_field_absence(client, output_path)
            inv = _run_invariants(client, output_path)
            cov = _run_field_coverage(output_path)
            # ⚠️ 顺序关键：版本锚**必须先盖**，清单**最后**打 ——
            # 清单钉住的正是这些文件（claims/section17_status/invariants/…）。
            # 先打清单再盖章 = 清单当场失效（实测：stage_manifest --check 报 4 处已变更，
            # 每个文件恰好大 35 字节 = 一行 `"rules_version": "…"`）。
            _stamp_rules_version(output_path)
            _write_stage_manifest(output_path)
            verdict = _gate_verdict(client, inv, cov, [], allow_degraded)
            verdict["stale_section17"] = True
            verdict["disclosure"].append({
                "gate": "collector", "code": "STALE17",
                "title": "§17 未随本次刷新重建",
                "detail": "本次为 --refresh-market：§1/§2/§11/§14 已刷新，§17 网格仍是"
                          "上一次采集的结果。若 §2 的价格已变，§17 的目标买入价/溢价与"
                          "§2 展示价不同源（由 I7 检查）。"})
            verdict["disclosure_count"] = len(verdict["disclosure"])
            from pathlib import Path as _P
            import json as _json
            gp = _P(output_path).parent / "gate_status.json"
            gp.write_text(_json.dumps(verdict, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
            _print_verdict(verdict, gp)
            client._store["gate_verdict"] = verdict
            return verdict
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 刷新模式下门禁判定失败: {e}", file=sys.stderr)
            return {"deliverable": True, "degraded": True, "blocking": [], "disclosure": [],
                    "note": f"刷新模式下门禁未完成：{type(e).__name__}: {e}"}
    try:
        from pathlib import Path
        import json
        from section_contract import check_pack_consistency, missing_summary

        out = Path(output_path).parent / "section17_status.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        _write_claims(client, output_path)
        ok = sum(1 for e in payload["sections"].values() if e["status"] == "ok")
        total = len(payload["sections"])
        print(f"  §17 状态: {ok}/{total} 小节 ok → {out.name}")

        problems = check_pack_consistency(data_pack, payload)
        if problems:
            print("  ⚠️ 状态契约与数据包不一致:", file=sys.stderr)
            for prob in problems:
                print(f"     - {prob}", file=sys.stderr)
        missing = missing_summary(payload)
        if missing:
            print(f"  ⚠️ {len(missing)} 个小节缺失（缺失 ≠ 零值；调用方不得手算）:")
            for m in missing:
                print(f"     - §{m['section']} {m['title']}: {m['reason']}")
        _write_field_absence(client, output_path)
        inv = _run_invariants(client, output_path)
        cov = _run_field_coverage(output_path)
        # 同上：锚先盖、清单最后打（清单钉住 claims/section17_status/invariants/…）
        _stamp_rules_version(output_path)
        _write_stage_manifest(output_path)

        verdict = _gate_verdict(client, inv, cov, problems, allow_degraded)
        gp = Path(output_path).parent / "gate_status.json"
        gp.write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
        _print_verdict(verdict, gp)
        client._store["gate_verdict"] = verdict
        return verdict
    except Exception as e:  # never fail the collection over a status sidecar
        print(f"  ⚠️ section17_status.json 写入失败: {e}", file=sys.stderr)
        return {"deliverable": True, "degraded": True, "blocking": [], "disclosure": [],
                "note": f"门禁自身故障，未完成判定：{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# FAIL 分级（交付语义）—— 单点实现在 `scripts/gate_policy.py`
#
# 2026-10-14：分级从本文件**抽出**为共享模块。原因：数值审核与观点审核
# （scripts/numeric_audit.py / scripts/viewpoint_audit.py）需要**同一套**交付语义，
# 而本仓库已有"同一阈值三处重复实现"的事故（P1-12）。分级是政策，必须只有一份。
# ---------------------------------------------------------------------------
def _gate_verdict(client, inv_findings, cov_findings, contract_problems,
                  allow_degraded: bool = False) -> dict:
    """把各路门禁结果折成一个**可交付/不可交付**的裁决（委托 gate_policy）。"""
    from gate_policy import verdict as _policy_verdict
    return _policy_verdict(
        ("invariants", inv_findings),
        ("field_coverage", cov_findings),
        ("section_contract", [{"code": "SC", "status": "FAIL", "title": "§17 状态契约",
                               "detail": str(p)} for p in (contract_problems or [])]),
        allow_degraded=allow_degraded,
        stock_code=getattr(client, "stock_code", "") or "",
    )


def _print_verdict(verdict: dict, path) -> None:
    if verdict.get("blocking"):
        print(f"  ⛔ 阻断级门禁失败 {len(verdict['blocking'])} 项"
              f"（{path.name}）—— 这些 FAIL 会改变结论数字，按契约**不得交付**：")
        for b in verdict["blocking"]:
            print(f"     - [{b['gate']}/{b['code']}] {b.get('why') or b['title']}")
        if verdict.get("degraded"):
            print("  ⚠️ 已用 --allow-degraded 显式降级：本次产物**带缺陷交付**，"
                  "gate_status.json 的 degraded=true 必须随报告披露。")
        else:
            print("  → 修数据后重跑；或确知影响面时用 --allow-degraded 显式降级重跑。")
    if verdict.get("disclosure"):
        print(f"  ⚠️ 披露级信号 {len(verdict['disclosure'])} 项"
              "（不阻断，但相关数字不得被引用）：")
        for d in verdict["disclosure"][:6]:
            print(f"     - [{d['gate']}/{d['code']}] {d['title']}")
    if verdict.get("deliverable") and not verdict.get("blocking"):
        print("  ✅ 门禁裁决：可交付（无阻断级失败）")



def _run_invariants(client, output_path: str) -> None:
    """Run the pack-level invariant engine and persist ``invariants.json``.

    Invariants are **cross-section** relations (period labelling, per-share currency,
    cash tie-out, dividend integrity, sensitivity direction) that neither the Python
    arithmetic nor the report-side numeric consistency check can see. They are
    reported, not swallowed: a FAIL means a downstream narrative built on those
    numbers cannot be trusted (see scripts/invariants.py).
    """
    try:
        from pathlib import Path
        import json
        import invariants as inv

        pack = Path(output_path).read_text(encoding="utf-8")
        fx_path = Path(output_path).parent / "fx.json"
        fx = json.loads(fx_path.read_text(encoding="utf-8")) if fx_path.exists() else None
        findings = inv.run_all(pack, fx)
        out = Path(output_path).parent / "invariants.json"
        out.write_text(json.dumps(
            {"pack": str(output_path), "findings": [f.to_dict() for f in findings]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fails = [f for f in findings if f.status == "FAIL"]
        unk = [f for f in findings if f.status == "UNEVALUATED"]
        print(f"  不变式: FAIL {len(fails)} · UNEVALUATED {len(unk)} · "
              f"PASS {sum(1 for f in findings if f.status == 'PASS')} → {out.name}")
        for f in fails:
            print(f"     ❌ [{f.code}] {f.title}: {f.detail}")
            for e in f.evidence[:3]:
                print(f"        · {e}")
        if fails:
            print("     ⚠️ 上述 FAIL 项所在的数字**不得**被报告直接引用；"
                  "先修数据/口径或按 section17_status.json 的 reason 停手报告。")
        return findings
    except Exception as e:  # never fail the collection over a sidecar
        print(f"  ⚠️ invariants.json 写入失败: {e}", file=sys.stderr)
        return None


def _run_field_coverage(output_path: str) -> None:
    """Run the **field-coverage** gate (完整性) and persist ``field_coverage.json``.

    与 ``invariants.py`` 互补：不变式查**包内自洽**，本门禁查**是否少了一行**。
    06862 实测的 P0 缺口（上游有应付债券 2,028,881 千元、映射标签写错，导致 §4 的
    「应付债券」行声明了却全年度为空、有息负债漏计 20.29 亿元）就是被它机械抓住的。
    """
    try:
        from pathlib import Path
        import json
        import field_coverage as fc

        pack = Path(output_path).read_text(encoding="utf-8")
        findings = fc.run_all(pack, pack_path=str(output_path))
        out = Path(output_path).parent / "field_coverage.json"
        out.write_text(json.dumps(
            {"pack": str(output_path), "findings": [f.to_dict() for f in findings]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fails = [f for f in findings if f.status == "FAIL"]
        unk = [f for f in findings if f.status == "UNEVALUATED"]
        print(f"  字段覆盖: FAIL {len(fails)} · UNEVALUATED {len(unk)} · "
              f"PASS {sum(1 for f in findings if f.status == 'PASS')} → {out.name}")
        for f in fails:
            print(f"     ❌ [{f.code}] {f.title}: {f.detail}")
            for e in f.evidence[:3]:
                print(f"        · {e}")
        if fails:
            print("     ⚠️ 缺行对不变式不可见；先确认是『真的没有』还是『被漏渲染/映射错位』。")
        return findings
    except Exception as e:  # never fail the collection over a sidecar
        print(f"  ⚠️ field_coverage.json 写入失败: {e}", file=sys.stderr)
        return None


def _write_stage_manifest(output_path: str) -> None:
    """Pin this stage's outputs with md5 → ``{output_dir}/pack_manifest.json``.

    下游阶段开工前用 `stage_manifest.py --check` 校验，**变更即报错**：
    本轮三轮回溯里"运行期改了上游状态文件导致报告自相矛盾""读到半写产物""审计读了陈旧基线"
    都是"下游读了非预期版本"，纸面规则挡不住。
    """
    try:
        from pathlib import Path
        import stage_manifest as sm

        out_dir = Path(output_path).parent
        # 决策 sidecar 必须一并钉住：`shares.json` 决定每股锚点与目标买入价、
        # `dividends_declared.json` 决定派息率（00506 实测：法3 的逐年 DPS 就来自它，
        # 而 M_pess 直接进 §17.14 买点）。漏钉等于上游冻结门禁看不见这两个输入
        # （数值审核 00506 复核轮 §五 建议 ①）。
        cand = ["data_pack_market.md", "claims.json", "section17_status.json",
                "invariants.json", "field_coverage.json", "field_absence.json",
                "fx.json", "shares.json", "dividends_declared.json",
                "balance_sheet_overrides.json", "metrics.json", "stability.json"]
        inputs = [out_dir / c for c in cand if (out_dir / c).is_file()]
        payload = sm.build("pack", inputs)
        (out_dir / "pack_manifest.json").write_text(
            __import__("json").dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"  阶段清单: {len(payload['inputs'])} 项已钉住 → pack_manifest.json")
    except Exception as e:  # never fail the collection over a sidecar
        print(f"  ⚠️ pack_manifest.json 写入失败: {e}", file=sys.stderr)


def _write_claims(client, output_path: str) -> None:
    """Write ``claims.json`` — the machine-checkable claim set (P2-20 改动 2).

    Every §17 value the agent may quote is registered with
    ``(indicator, value, unit, time, basis, policy)``. The report-side verifier
    (``report_consistency.py --claims``) mechanical-checks each claim-bound token
    against this file and is **fail-closed**: a token that cannot be bound, or
    whose value fails its policy, is reported as a gate failure.
    """
    payload = client._store.get("claims_payload")
    if not payload:
        print("  ⚠️ 未生成 claims.json（本次运行未登记任何 §17 声明）")
        return
    try:
        from pathlib import Path
        import json
        out = Path(output_path).parent / "claims.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"  §17 声明: {len(payload['claims'])} 条 → {out.name}")
    except Exception as e:  # never fail the collection over a sidecar
        print(f"  ⚠️ claims.json 写入失败: {e}", file=sys.stderr)


def main():
    args = parse_args()

    # ── 跨进程互斥锁（2026-10-14）────────────────────────────────────────
    # 两个长跑流程共用**同一 Tushare token** 与 **同一 output 树**：
    #   · 并发时一方会拿到**空 DataFrame** ⇒ 该小节 `no_data`、产物残缺且**不报错**
    #     （实测：港股重采 §17.3/§17.4 变 no_data，当时误归因为"自己跑太多次"）
    #   · 覆盖对方正在读的产物目录；`output/.collector_cache/ttl` 的写入**非原子**
    # ⇒ 采集入口一律先取锁。**锁只在这一处取**：批量子进程若也持锁会与自身死锁。
    # `--no-lock` 仅用于确认无人并发时的紧急绕过。
    _lock = None
    # ⚠️ **测试环境必须跳过锁**：多个测试在**进程内**调用 `main()`，若取锁，
    # 锁的 pid 就是 pytest 自己 ⇒ 后续采集类测试全部阻塞在等锁上（实测：套件挂住，
    # 需等 `--lock-wait` 3600 秒）。锁保护的是**跨进程**的并发，
    # 同进程内的反复调用不需要它。
    _in_tests = bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules
    _lock_off = (getattr(args, "no_lock", False) or _in_tests
                 or os.environ.get("TUSHARE_LOCK_DISABLE") == "1")
    if not _lock_off:
        try:
            from run_lock import RunLock
            _lock = RunLock(holder="tushare_collector", ts_code=args.code).acquire(
                wait=True, timeout=getattr(args, "lock_wait", 3600), poll=5.0)
        except Exception as _le:                     # noqa: BLE001
            print(f"\n⛔ 采集锁不可用：{_le}", file=sys.stderr)
            print("   并发采集会抢配额并可能覆盖对方产物；确认无人并发时可用 --no-lock 绕过。",
                  file=sys.stderr)
            # ⚠️ 必须 `sys.exit` 而不是 `return`：脚本底部是裸 `main()`（不是
            # `sys.exit(main())`），返回值会被丢弃 ⇒ 退出码恒为 0，锁形同虚设。
            sys.exit(3)
        import atexit as _atexit
        _atexit.register(_lock.release)

    # Validate and normalize stock code
    try:
        ts_code = validate_stock_code(args.code)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("=== Dry Run ===")
        print(f"  Stock code: {args.code} -> {ts_code}")
        print(f"  Token: {'provided via --token' if args.token else 'from TUSHARE_TOKEN env'}")
        print(f"  Output: {args.output}")
        print(f"  Extra fields: {args.extra_fields or 'none'}")
        rf_value, rf_source = _resolve_rf(args)
        print(f"  Risk-free rate: {rf_value}% ({rf_source})" if rf_value is not None
              else "  Risk-free rate: not provided (set --rf / TUSHARE_RF if §14 has no permission)")
        if ".HK" in ts_code.upper():
            from fx_utils import resolve_fx_hkd_cny
            fx_value, fx_source, fx_ccy = resolve_fx_hkd_cny(
                getattr(args, "fx_hkd_cny", None), args.output)
            print(f"  HKD/CNY factor: {fx_value} ({fx_source}; reporting={fx_ccy})" if fx_value is not None
                  else "  HKD/CNY factor: not provided (HK R/GG will be ~15% low "
                       "if the issuer reports in CNY)")
        return

    # Get token
    token = args.token or get_token()
    client = TushareClient(token)

    # 让 client 自己带上标的代码：`_gate_verdict` 读 `client.stock_code` 写进
    # `gate_status.json`，而该属性此前**从未被赋值** ⇒ 每份产物都写 `"stock_code": ""`，
    # 事后核对某份 gate_status 属于哪只股票只能靠目录名（实测 2026-10-16 06078）。
    client.stock_code = ts_code

    # Seed the risk-free rate before any collection (3-B/3-C)
    _seed_risk_free_rate(client, args)

    # Seed the HK trading→reporting FX factor (known_issues.md P0-2)
    _seed_fx(client, args)

    # Seed 股本（known_issues.md #25）
    _seed_shares(client, args)

    # Seed the documented dividend commitment (known_issues.md §3.21 21b)
    _seed_dividend_commitment(client, args)

    # ── 决策 sidecar 三道关（schema / 量级 / 来源），fail-closed ──────────────
    # 这 4 个 sidecar 是决策变量的直接来源（股本决定全部买点、fx 决定分母、
    # 已宣告 DPS 决定 M_pess、补录决定 §17.15），却由人/LLM 撰写。
    # `pack_manifest` 只钉 md5（证明"没被改过"），不证明"当初写对了" ——
    # 实测两起事故都出在这一层（overrides 单位写成百万元 ⇒ 渲染 0.00；
    # 股数反推 3,279.35 vs 真值 2,797.22 并循环喂进当日市值）。
    try:
        from pathlib import Path as _Path
        import sidecar_contract as _sc
        _rep = _sc.validate_dir(str(_Path(args.output).parent))
        for _f in _rep.warns:
            print(f"  ⚠️ sidecar[{_f.gate}] {_f.file}: {_f.detail}")
        if not _rep.ok:
            print("  ⛔ 决策 sidecar 校验失败（fail-closed，不得作为分析输入）：", file=sys.stderr)
            for _f in _rep.fails:
                print(f"     ❌ sidecar[{_f.gate}] {_f.file}: {_f.detail}", file=sys.stderr)
            print("  → 修正 {output_dir} 下的 sidecar 后重跑；这些值直接决定买点与净值。",
                  file=sys.stderr)
            sys.exit(1)
        print(f"  ✓ 决策 sidecar 三道关：FAIL 0 · WARN {len(_rep.warns)}")
    except SystemExit:
        raise
    except Exception as _e:  # noqa: BLE001 —— 校验器自身故障不得静默放过
        print(f"  ⛔ sidecar 校验器不可用：{type(_e).__name__}: {_e}", file=sys.stderr)
        sys.exit(1)

    # TTL cache controls
    if args.no_cache:
        client._cache_enabled = False
    if args.cache_refresh:
        client._get_ttl_cache().invalidate_prefix(f"collector_{ts_code}_")
        print(f"Cache invalidated for {ts_code}")

    if args.refresh_market:
        from pathlib import Path
        output_path = Path(args.output)
        if not output_path.exists():
            print(f"⚠️ {output_path} does not exist, falling back to full collection")
            print(f"Collecting data for {ts_code}...")
            data_pack = client.assemble_data_pack(ts_code)
        else:
            existing = output_path.read_text(encoding="utf-8")
            age_days = client._check_staleness(existing)
            if age_days > 7:
                print(f"⚠️ Data pack is {age_days} days old, falling back to full collection")
                print(f"Collecting data for {ts_code}...")
                data_pack = client.assemble_data_pack(ts_code)
            else:
                print(f"Refreshing market data for {ts_code} (data pack is {age_days} day(s) old)...")
                data_pack = client.refresh_market_sections(ts_code, existing)
    else:
        print(f"Collecting data for {ts_code}...")
        data_pack = client.assemble_data_pack(ts_code)

    # Handle extra fields
    if args.extra_fields:
        extra_lines = ["\n", format_header(2, "附加字段"), ""]
        for field_spec in args.extra_fields:
            parts = field_spec.split(".", 1)
            if len(parts) != 2:
                extra_lines.append(f"- 无效字段格式: {field_spec} (应为 endpoint.field_name)")
                continue
            endpoint, field_name = parts
            try:
                df = client._safe_call(endpoint, ts_code=ts_code, fields=f"ts_code,end_date,{field_name}")
                if not df.empty:
                    extra_lines.append(f"**{endpoint}.{field_name}**:")
                    extra_lines.append(df.to_markdown(index=False))
                    extra_lines.append("")
                else:
                    extra_lines.append(f"- {endpoint}.{field_name}: 无数据")
            except Exception as e:
                extra_lines.append(f"- {endpoint}.{field_name}: 获取失败 ({e})")
        data_pack += "\n".join(extra_lines)

    # Write output （模块顶部已 import os；此处再 import 会让 `os` 在整个 main()
    # 变成局部名，从而让上面的取锁代码抛 UnboundLocalError —— 已实测）
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(data_pack)
    print(f"Output written to {args.output}")
    print(f"File size: {os.path.getsize(args.output):,} bytes")

    # §17 状态契约（known_issues.md P2-20 改动 4）
    # 缺席必须机器可判定：调用方据此**停止**，而不是退回手算。没有这份文件时，
    # 读者只能靠"小节标题在不在"来推断，而"没算"与"算出来是 0"无法区分。
    _write_metrics_sidecar(data_pack, args.output)
    _write_stability_sidecar(client, args.output)

    # 规则单点不可用 ⇒ **阻断**（known_issues §四十九）：此时数值可能来自内嵌默认值，
    # 而我们无法再声称"产物与策略规则一致"。这是 fail-closed，不是"多一条警告"。
    try:
        import tushare_modules.derived_metrics as _dm
        if getattr(_dm, "STRATEGY_RULES_ERROR", None):
            print("Error: 策略规则单点（strategy_rules.json）不可用 ⇒ 产物不得作为分析输入。"
                  f"\n  原因：{_dm.STRATEGY_RULES_ERROR}"
                  "\n  修好规则文件后重跑；不得用 --allow-degraded 绕过（它影响全部数值口径）。",
                  file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as _e:  # noqa: BLE001
        print(f"  ⚠️ 规则单点状态无法确认：{type(_e).__name__}: {_e}", file=sys.stderr)

    verdict = _write_section_status(client, data_pack, args.output,
                                    allow_degraded=args.allow_degraded)

    # 退出码即门禁结论：print 出来的警告没有任何调用方会读（实测：invariants FAIL 3 /
    # field_coverage FAIL 1 的产物照样交付）。改由退出码 + gate_status.json 承担。
    if not verdict.get("deliverable", True):
        print("Error: 阻断级门禁失败，data pack 已写出但**不得作为分析输入**；"
              "修数据后重跑，或用 --allow-degraded 显式降级。", file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    main()
