"""
Request and memory monitoring utilities.
"""
import gc
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("rateio_sync.stats")


@dataclass
class RequestStats:
    clickup_requests: int = 0
    clickup_tasks_fetched: int = 0
    powerrev_requests: int = 0
    sheets_read_requests: int = 0
    sheets_write_requests: int = 0
    sheets_cells_written: int = 0
    _start_time: float = field(default_factory=time.time)

    def reset(self) -> None:
        self.clickup_requests = 0
        self.clickup_tasks_fetched = 0
        self.powerrev_requests = 0
        self.sheets_read_requests = 0
        self.sheets_write_requests = 0
        self.sheets_cells_written = 0
        self._start_time = time.time()

    @property
    def total_requests(self) -> int:
        return (
            self.clickup_requests
            + self.powerrev_requests
            + self.sheets_read_requests
            + self.sheets_write_requests
        )

    @staticmethod
    def get_memory_mb_safe() -> float:
        return get_memory_mb()


stats = RequestStats()

_lifetime_clickup = 0
_lifetime_powerrev = 0
_lifetime_sheets = 0


def get_memory_mb() -> float:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        pass

    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (FileNotFoundError, ValueError, IndexError):
        pass

    return -1.0


def log_memory(label: str = "") -> None:
    mb = get_memory_mb()
    prefix = f"[{label}] " if label else ""
    logger.info("%sMemoria RSS: %.1f MB", prefix, mb)


def force_free_memory() -> None:
    gc.collect()
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass


def log_sync_stats(sync_type: str) -> None:
    global _lifetime_clickup, _lifetime_powerrev, _lifetime_sheets

    _lifetime_clickup += stats.clickup_requests
    _lifetime_powerrev += stats.powerrev_requests
    _lifetime_sheets += stats.sheets_read_requests + stats.sheets_write_requests

    logger.info(
        "--- STATS %s ---\n"
        "  ClickUp:  %d requests, %d tasks fetched\n"
        "  PowerRev: %d requests\n"
        "  Sheets:   %d reads, %d writes, %d cells written\n"
        "  Total:    %d requests neste ciclo\n"
        "  Memoria:  %.1f MB\n"
        "  Lifetime: %d ClickUp, %d PowerRev, %d Sheets (desde boot)",
        sync_type,
        stats.clickup_requests,
        stats.clickup_tasks_fetched,
        stats.powerrev_requests,
        stats.sheets_read_requests,
        stats.sheets_write_requests,
        stats.sheets_cells_written,
        stats.total_requests,
        get_memory_mb(),
        _lifetime_clickup,
        _lifetime_powerrev,
        _lifetime_sheets,
    )
