"""跨进程互斥锁 —— 让「选股采集」与「个股分析」两个长跑流程不互相踩。

为什么需要（2026-10-14 实测）
----------------------------
两个流程共用**同一 Tushare token** 与**同一个 `output/` 树**，存在 5 处共享：

1. **API 配额**（按 token + 接口计频，不按进程）—— 并发时一方会拿到**空 DataFrame**，
   后果不是报错而是**该小节 `no_data`，产物残缺**。实测：港股重采时 §17.3/§17.4 变
   `no_data`、C1「关键报表缺必备行」，当时误归因为"自己跑太多次"，实为与并发分析竞争。
2. **产物目录** `output/{code}_{name}/` —— 覆盖对方正在读的 pack。`pack_manifest.json`
   能让下游 `stage_manifest.py --check` **报错**，但**不阻止覆盖**（对方那次运行白跑）。
3. **TTL 磁盘缓存** `output/.collector_cache/ttl` —— `ScreenerCache.put` 直接
   `df.to_parquet(path)`，**无 `.tmp` + `os.replace`** ⇒ 并发读可能读到半写文件。
4. **sidecar 文件**（`fx.json` / `shares.json` / `dividends_declared.json` / `claims.json` …）—— 同 2。
5. **`output/rf.json`** —— 只读共享；两边 rf 不同会让 II 口径不一致（**锁不管这个**，属口径约定）。

本模块提供**文件级互斥**（`O_CREAT|O_EXCL` 原子创建），覆盖 1~4 的**同时发生**问题。

⚠️ **锁不是万能的**：
- 它把并发**串行化**，但**不预留配额** —— 我方刚消耗的频次仍可能让对方紧接着撞限频。
  真正解决配额竞争要么分开 token，要么加共享节流器（本模块不含）。
- 它**不解决口径分歧**（第 5 项）。

契约
----
锁文件是 JSON：``{"pid":…, "holder":…, "ts_code":…, "started_at":…, "host":…}``。
**陈旧锁可自愈**：持锁进程已死（`os.kill(pid,0)` 失败）或超过 ``stale_after`` 秒 ⇒ 视为陈旧，
删除后重试。这避免崩溃后锁永久占用（"锁死"比"没锁"更糟）。
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

__all__ = ["RunLock", "LockBusy", "DEFAULT_LOCK_PATH", "DEFAULT_STALE_SECONDS"]

DEFAULT_LOCK_PATH = "output/.collector.lock"

#: 超过这个秒数仍未被释放 ⇒ 视为陈旧（可自愈）。
#: 取 45 分钟：单只采集约 55 秒，但一次批量的**单次持锁**从不超过几分钟；
#: 留足余量以免把"慢但活着"的进程误判为死锁。
DEFAULT_STALE_SECONDS = 45 * 60


class LockBusy(RuntimeError):
    """锁被他人持有且未等到。"""

    def __init__(self, holder: dict):
        self.holder = holder or {}
        who = self.holder.get("holder") or "?"
        code = self.holder.get("ts_code") or "-"
        started = self.holder.get("started_at")
        age = f"{time.time() - started:.0f}s" if isinstance(started, (int, float)) else "?"
        super().__init__(
            f"采集锁被占用：holder={who} pid={self.holder.get('pid')} "
            f"ts_code={code} 已持锁 {age}。"
            f"并发采集会抢 Tushare 配额并可能覆盖对方产物 —— 请等待，或确认对方已退出后"
            f"删除锁文件 {self.holder.get('_path')}"
        )


@dataclass
class RunLock:
    """基于 ``O_CREAT|O_EXCL`` 的跨进程互斥锁。用作上下文管理器最安全。"""

    path: Union[str, Path] = DEFAULT_LOCK_PATH
    holder: str = "collector"
    ts_code: Optional[str] = None
    stale_after: float = DEFAULT_STALE_SECONDS

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._owned = False

    # -- 内部 ---------------------------------------------------------------
    def _read(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            # 文件在但读不出（可能正被写入）⇒ 当作"有人持锁但信息不明"，
            # **不得**当自由处理（fail-closed：宁可等待也不并发）
            return {} if self.path.exists() else None

    def _is_stale(self, info: Optional[dict]) -> bool:
        if not info:
            return False                      # 信息不明 ⇒ 不判陈旧（fail-closed）
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)               # 只探活，不发信号
            except ProcessLookupError:
                return True                   # 进程已死 ⇒ 陈旧
            except PermissionError:
                pass                          # 活着但不属于我 ⇒ 不判陈旧
        started = info.get("started_at")
        if isinstance(started, (int, float)) and self.stale_after > 0:
            if time.time() - started > self.stale_after:
                return True
        return False

    # -- 公共接口 -----------------------------------------------------------
    def acquire(self, wait: bool = False, timeout: float = 0.0,
                poll: float = 5.0) -> "RunLock":
        """取锁。``wait=True`` 时轮询直到 ``timeout``（0 = 无限等）。

        取不到抛 :class:`LockBusy` —— **不静默继续**。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if (wait and timeout <= 0) else (time.time() + timeout if wait else None)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self._read()
                if self._is_stale(info):
                    info = info or {}
                    info["_path"] = str(self.path)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue                  # 清掉陈旧锁后重试
                if not wait:
                    info = info or {}
                    info["_path"] = str(self.path)
                    raise LockBusy(info)
                if deadline is not None and time.time() >= deadline:
                    info = info or {}
                    info["_path"] = str(self.path)
                    raise LockBusy(info)
                time.sleep(poll)
                continue
            payload = {
                "pid": os.getpid(),
                "holder": self.holder,
                "ts_code": self.ts_code,
                "started_at": time.time(),
                "host": socket.gethostname(),
            }
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            self._owned = True
            return self

    def release(self) -> None:
        """释放。**只删自己持有的那把** —— 不误删他人的锁。"""
        if not self._owned:
            return
        info = self._read() or {}
        if info.get("pid") not in (None, os.getpid()):
            self._owned = False               # 锁已被他人接管 ⇒ 不删
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
