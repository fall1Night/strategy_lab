# -*- coding: utf-8 -*-
"""容灾切换事件日志：内存环形缓冲 + 追加写 JSONL 文件。

- ``SwitchEvent``：单次切换事件数据类。
- ``SwitchLog``：记录/查询切换事件（内存环形缓冲最多 N 条 + 磁盘 JSONL 追加）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SwitchEvent:
    """单次数据源切换事件。

    Attributes:
        timestamp: Unix 时间戳。
        symbol: 标的代码。
        period: 周期标识（daily/weekly）。
        from_source: 切换前数据源。
        to_source: 切换后数据源。
        reason: 切换原因（异常信息摘要）。
    """

    timestamp: float
    symbol: str
    period: str
    from_source: str
    to_source: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "period": self.period,
            "from_source": self.from_source,
            "to_source": self.to_source,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(d: dict[str, object]) -> "SwitchEvent":
        return SwitchEvent(
            timestamp=float(d.get("timestamp", 0)),  # type: ignore[arg-type]
            symbol=str(d.get("symbol", "")),
            period=str(d.get("period", "")),
            from_source=str(d.get("from_source", "")),
            to_source=str(d.get("to_source", "")),
            reason=str(d.get("reason", "")),
        )


class SwitchLog:
    """切换事件日志：内存环形缓冲（最近 N 条）+ 磁盘 JSONL 追加。

    用法：
        slog = SwitchLog(file_path=Path("./data/datasource_switch_log.jsonl"))
        slog.record(SwitchEvent(...))
        recent = slog.recent(10)
    """

    def __init__(self, file_path: Path | None = None, ring_size: int = 50) -> None:
        """初始化切换日志。

        Args:
            file_path: JSONL 文件路径，若为 None 则仅内存记录。
            ring_size: 内存环形缓冲区最大条数（默认 50）。
        """
        self._file: Path | None = file_path
        self._ring_size: int = max(1, int(ring_size))
        self._ring: list[SwitchEvent] = []
        self._pos: int = 0
        self._count: int = 0  # 总写入次数

    def record(self, event: SwitchEvent) -> None:
        """记录一次切换事件（内存 + 磁盘 JSONL）。"""
        # 内存环形缓冲
        if len(self._ring) < self._ring_size:
            self._ring.append(event)
        else:
            self._ring[self._pos % self._ring_size] = event
        self._pos += 1
        self._count += 1

        # 追加写 JSONL
        if self._file is not None:
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                iso_ts = time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(event.timestamp)
                )
                line = json.dumps(
                    {
                        **event.to_dict(),
                        "timestamp_iso": iso_ts,
                    },
                    ensure_ascii=False,
                )
                with open(self._file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 磁盘写失败不阻塞主流程

    def recent(self, n: int = 10) -> list[SwitchEvent]:
        """返回最近 N 条切换事件（按时间降序）。

        Args:
            n: 返回条数。

        Returns:
            最近 N 条 ``SwitchEvent``（时间降序）。
        """
        n = max(1, int(n))
        # ring 按写入顺序排列，取最后 N 条并反转
        if len(self._ring) <= n:
            return list(reversed(self._ring))
        # 环形缓冲：从当前位置往回取
        result: list[SwitchEvent] = []
        size = len(self._ring)
        for i in range(size):
            idx = (self._pos - 1 - i) % size
            result.append(self._ring[idx])
            if len(result) >= n:
                break
        return result
