import asyncio
import logging
import time
from datetime import datetime, timezone


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(v):
    v = abs(v or 0)
    return f"{v / 1000:.1f}K" if v >= 1000 else f"{v:.2f}"


class Agent:
    name = "agent"

    def __init__(self, db, bus, src, notifier, clock=time.time, sleep=asyncio.sleep):
        self.db, self.bus, self.src, self.notifier = db, bus, src, notifier
        self.clock, self.sleep = clock, sleep
        self.log = logging.getLogger(self.name)
        self.stop = asyncio.Event()

    def cfg(self):
        return self.db.section(self.name)

    def paused(self):
        return self.name in (self.db.get("desk.paused_agents") or [])

    def beat(self, status="ok", note=""):
        self.db.heartbeat(self.name, status, note)

    def decide(self, mint, decision, reason, data=None):
        self.bus.decision(self.name, mint, decision, reason, data)

    async def backoff_429(self, n=0, where="DexScreener"):
        """Sleep [risk.backoff_429_s] after a 429 so a 5s poll does not hammer the source."""
        wait = float(self.db.get("risk.backoff_429_s", 30))
        extra = f", {n} polls missed" if n else ""
        self.log.warning("429 from %s: backing off %ss%s", where, wait, extra)
        await self.sleep(wait)
        return wait

    async def with_retries(self, fn, *a):
        """Source error or timeout: retry [retries] times, [retry_gap_s] apart (Auditor edge cases)."""
        from sources import FetchError
        n = int(self.db.get("auditor.retries", 3))
        gap = float(self.db.get("auditor.retry_gap_s", 2))
        last = None
        for i in range(max(1, n)):
            try:
                return await fn(*a)
            except FetchError as e:
                last = e
                if i < n - 1:
                    await self.sleep(gap)
        raise last

    async def loop(self, every_key, fn):
        while not self.stop.is_set():
            try:
                if not self.paused():
                    await fn()
                self.beat("paused" if self.paused() else "ok")
            except Exception as e:
                self.log.exception("tick failed")
                self.beat("error", type(e).__name__)
            await self.sleep(float(self.db.get(every_key, 30)))

    async def queue_loop(self, topic, fn):
        q = self.bus.subscribe(topic)
        self.beat("paused" if self.paused() else "ok", "idle")
        while not self.stop.is_set():
            try:
                ev = await asyncio.wait_for(q.get(), 60)
            except asyncio.TimeoutError:
                self.beat("paused" if self.paused() else "ok", "idle")
                continue
            try:
                await fn(ev)
                self.beat("ok")
            except Exception as e:
                self.log.exception("%s failed", self.name)
                self.beat("error", type(e).__name__)
