"""消费者迁移的**行为冻结**（P1b 安全网）。

做法：对同一批产物跑每个审计脚本，把**判定结果**（findings 的指纹，不是日志措辞）
与冻结基线比对。迁移到 `pack_metrics` 前后必须逐条相同 —— 否则"迁移"就成了"改判定"。

基线文件：`tests/baselines/p1b_findings.json`（`--regen` 重新生成）。
只冻结机器可判的结论，不冻结日志措辞（措辞会随注释改进变化）。
"""

import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = str(ROOT / ".venv" / "bin" / "python")
BASELINE = ROOT / "tests" / "baselines" / "p1b_findings.json"

#: 覆盖不同形态：正常 / AA 为负 / 缺小节 / 银行口径
PRODUCTS = ["00506_中国食品", "600011_华能国际", "601991_大唐发电",
            "600016_民生银行", "000157_中联重科"]


def _fingerprint(obj) -> str:
    """结构指纹：排序后的 JSON 的 sha256（前 16 位）。对键序、浮点写法不敏感。"""
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _run(script: str, args: list, out: pathlib.Path):
    r = subprocess.run([PY, str(SCRIPTS / script)] + args,
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode in (0, 1, 2), f"{script} 异常退出 {r.returncode}: {r.stderr[-500:]}"
    if not out.is_file():
        return {"__rc__": r.returncode, "__no_json__": True}
    payload = json.loads(out.read_text(encoding="utf-8"))
    out.unlink(missing_ok=True)
    if isinstance(payload, dict) and "findings" in payload:
        # 只留判定字段，去掉路径/时间戳这类易变内容
        return [{"id": f.get("id") or f.get("code") or f.get("name"),
                 "status": f.get("status"), "severity": f.get("severity")}
                for f in payload["findings"]]
    return payload


def _collect() -> dict:
    import tempfile
    out = {}
    tmp = pathlib.Path(tempfile.mkdtemp())
    for name in PRODUCTS:
        # 冻结的真实产物比合成夹具更适合"行为冻结"（覆盖负 AA、缺小节、银行口径等形态）。
        # 归档后统一从 `output/_legacy/` 读，且**显式**——不静默跳过。
        d = ROOT / "output" / "_legacy" / name
        if not (d / "data_pack_market.md").is_file():
            continue
        pack = str(d / "data_pack_market.md")
        fx = str(d / "fx.json")
        fx_args = ["--fx", fx] if (d / "fx.json").is_file() else []
        out[f"field_coverage/{name}"] = _fingerprint(
            _run("field_coverage.py", ["--pack", pack, "--json", str(tmp / "a.json"), "--quiet"],
                 tmp / "a.json"))
        out[f"invariants/{name}"] = _fingerprint(
            _run("invariants.py",
                 ["--pack", pack] + fx_args + ["--json", str(tmp / "b.json"), "--quiet"],
                 tmp / "b.json"))
        out[f"instrument_audit/{name}"] = _fingerprint(
            _run("instrument_audit.py",
                 ["--root", str(ROOT / "output" / "_legacy"), "--only", name,
                  "--output", str(tmp / "c.json"), "--quiet"],
                 tmp / "c.json"))
    return out


def _load_baseline() -> dict:
    if not BASELINE.is_file():
        return {}
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_findings_match_frozen_baseline():
    """迁移到 pack_metrics 前后，findings 指纹必须完全一致。

    基线不存在时跳过（并提示用 `--regen` 生成），存在则逐项比对。
    """
    baseline = _load_baseline()
    if not baseline:
        pytest.skip("基线未生成；先跑 `pytest tests/test_consumer_migration.py --regen`")
    current = _collect()
    diffs = []
    for key, fp in baseline.items():
        if key not in current:
            diffs.append(f"{key}: 当前跑不出来")
        elif current[key] != fp:
            diffs.append(f"{key}: {fp} → {current[key]}")
    assert not diffs, "迁移改变了判定结果：\n  " + "\n  ".join(diffs)


def _regen():
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    data = _collect()
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {BASELINE}（{len(data)} 项）")


if __name__ == "__main__" and "--regen" in sys.argv:
    _regen()
