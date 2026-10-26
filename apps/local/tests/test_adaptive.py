"""Offline resource pressure, admission limits, and cancellation checks."""

import asyncio

import pytest
from app.adaptive import GIB, Governor, Load, adaptive_map

HEALTHY = Load(20, 12 * GIB, 30)


def test_growth_pressure_and_missing_metrics(monkeypatch):
    monkeypatch.setattr("app.adaptive.os.cpu_count", lambda: 8)
    governor = Governor(4)
    for _ in range(8):
        governor.update(HEALTHY)
    assert governor.limit == 4
    governor.update(HEALTHY, congested=True)
    assert governor.limit == 2
    governor.update(Load(20, GIB // 2, 95))
    assert governor.limit == 1
    governor.update(HEALTHY)
    assert governor.limit == 1
    governor.update(HEALTHY)
    assert governor.limit == 2
    governor.update(HEALTHY, lag=0.5)
    assert governor.limit == 1
    for _ in range(6):
        governor.update(HEALTHY)
    governor.update(None)
    assert governor.limit == 2
    assert governor.reason == "metrics_unavailable"


@pytest.mark.parametrize("maximum", [0, 17, 1.5])
def test_invalid_ceiling(maximum):
    with pytest.raises(ValueError):
        Governor(maximum)


async def test_scheduler_grows_and_keeps_all_work(monkeypatch):
    monkeypatch.setattr("app.adaptive.os.cpu_count", lambda: 8)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0
    completed = []
    reports = []

    async def work(item):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            entered.set()
        try:
            await release.wait()
            completed.append(item)
        finally:
            active -= 1

    task = asyncio.create_task(
        adaptive_map(
            range(12),
            work,
            governor=Governor(4),
            report=lambda *args: reports.append(args),
            sampler=lambda: HEALTHY,
            interval=0.01,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert peak == 4
        release.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sorted(completed) == list(range(12))
    assert reports[-1][1] == 0


async def test_pressure_drains_without_cancelling_and_shutdown_cleans_up(monkeypatch):
    monkeypatch.setattr("app.adaptive.os.cpu_count", lambda: 8)
    started = []
    two_started = asyncio.Event()
    cancelled = []
    release = [asyncio.Event() for _ in range(4)]
    pressure = asyncio.Event()
    reduced = asyncio.Event()

    async def work(item):
        started.append(item)
        if len(started) == 2:
            two_started.set()
        try:
            await release[item].wait()
        except asyncio.CancelledError:
            cancelled.append(item)
            raise

    def report(limit, active, reason):
        if limit == 1:
            reduced.set()

    task = asyncio.create_task(
        adaptive_map(
            range(4),
            work,
            governor=Governor(4),
            report=report,
            sampler=lambda: Load(95, 12 * GIB, 30) if pressure.is_set() else HEALTHY,
            interval=0.01,
        )
    )
    try:
        await asyncio.wait_for(two_started.wait(), 2)
        pressure.set()
        await asyncio.wait_for(reduced.wait(), 2)
        assert started == [0, 1]
        assert not cancelled
        release[0].set()
        await asyncio.sleep(0.03)
        assert started == [0, 1]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert cancelled == [1]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
