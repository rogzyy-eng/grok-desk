"""bus.py — agents talk only through this bus and the db (STEP 5)."""
import asyncio
import json
import time


class Bus:
    def __init__(self, db=None):
        self.db = db
        self.subs = {}
        self.all = []

    def subscribe(self, topic):
        q = asyncio.Queue(maxsize=1000)
        self.subs.setdefault(topic, []).append(q)
        return q

    def subscribe_all(self):
        q = asyncio.Queue(maxsize=300)
        self.all.append(q)
        return q

    def unsubscribe_all(self, q):
        if q in self.all:
            self.all.remove(q)

    def publish(self, topic, payload):
        ev = {"topic": topic, "ts": time.time(), **payload}
        for q in list(self.subs.get(topic, [])) + list(self.all):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def activity(self, kind, mint, agent, title, subtitle, value=None):
        """Activity row (STEP 4): kind bought|sold|stopped|rejected, title / subtitle / value."""
        row = {"ts": time.time(), "kind": kind, "mint": mint, "agent": agent, "title": title,
               "subtitle": subtitle, "value": value}
        if self.db is not None:
            self.db.insert("activity", row)
        self.publish("activity", row)

    def decision(self, agent, mint, decision, reason, data=None):
        if self.db is not None:
            self.db.insert("decisions", {"ts": time.time(), "agent": agent, "mint": mint, "decision": decision,
                                         "reason": reason, "data": json.dumps(data, default=str) if data else None})
        self.publish("decision", {"agent": agent, "mint": mint, "decision": decision, "reason": reason})
