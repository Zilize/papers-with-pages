"""Bounded local scheduling with gradual growth and resource-pressure backoff."""

import asyncio
import os
from dataclasses import dataclass

import psutil

GIB = 1024**3


@dataclass(frozen=True)
class Load:
    cpu: float
    available: int
    memory_percent: float


def sample_load():
    try:
        memory = psutil.virtual_memory()
        return Load(psutil.cpu_percent(interval=None), memory.available, memory.percent)
    except (OSError, psutil.Error):
        return None


class Governor:
    def __init__(self, maximum=8):
        if not isinstance(maximum, int) or not 1 <= maximum <= 16:
            raise ValueError("Maximum workers must be between 1 and 16")
        self.maximum = min(maximum, max(1, os.cpu_count() or 2))
        self.limit = min(2, self.maximum)
        self.healthy = 0
        self.reason = "warming_up"

    def update(self, load, *, lag=0, congested=False):
        if load is None:
            self.limit = min(self.limit, 2)
            self.healthy = 0
            self.reason = "metrics_unavailable"
            return
        ceiling = min(self.maximum, max(1, int((load.available - GIB) // (GIB // 2))))
        if congested or load.cpu >= 85 or load.memory_percent >= 90 or lag >= 0.25:
            self.limit = max(1, min(ceiling, self.limit // 2))
            self.healthy = 0
            self.reason = "upstream_backoff" if congested else "resource_pressure"
        elif self.limit > ceiling:
            self.limit = ceiling
            self.healthy = 0
            self.reason = "memory_headroom"
        elif load.cpu < 65 and load.available >= 2 * GIB and lag < 0.1:
            self.healthy += 1
            if self.healthy >= 2:
                self.limit = min(ceiling, self.limit + 1)
                self.healthy = 0
            self.reason = "healthy"
        else:
            self.healthy = 0
            self.reason = "holding"


async def adaptive_map(items, process, *, governor, report, sampler=None, interval=2):
    """Admit new work up to the target; never cancel active work when shrinking."""
    sampler = sampler or sample_load
    iterator = iter(items)
    running = set()
    exhausted = False
    congested = False
    loop = asyncio.get_running_loop()
    # Prime the nonblocking CPU counter; its first reading is not a growth signal.
    governor.update(sampler())
    governor.healthy = 0
    next_sample = loop.time() + interval
    async with asyncio.TaskGroup() as group:
        while running or not exhausted:
            now = loop.time()
            if now >= next_sample:
                governor.update(sampler(), lag=max(0, now - next_sample), congested=congested)
                congested = False
                next_sample = loop.time() + interval
            while not exhausted and len(running) < governor.limit:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                running.add(group.create_task(process(item)))
            report(governor.limit, len(running), governor.reason)
            if not running:
                break
            done, running = await asyncio.wait(
                running,
                timeout=max(0, next_sample - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                congested = bool(task.result()) or congested

    report(governor.limit, 0, governor.reason)
