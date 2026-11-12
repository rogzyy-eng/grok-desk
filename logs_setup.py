"""logs/: 1 file per agent, rotated daily (STEP 3 layout). desk.log for everything else."""
import logging
from logging.handlers import TimedRotatingFileHandler

import config


class _Only(logging.Filter):
    def __init__(self, name):
        super().__init__()
        self.n = name

    def filter(self, rec):
        return rec.name == self.n or rec.name.startswith(self.n + ".")


class _NotAgents(logging.Filter):
    def filter(self, rec):
        return rec.name.split(".")[0] not in config.AGENTS


def setup(log_dir=None, level=logging.INFO):
    d = log_dir or config.LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    for name in config.AGENTS:
        h = TimedRotatingFileHandler(d / f"{name}.log", when="midnight", backupCount=14, encoding="utf-8")
        h.setFormatter(fmt)
        h.addFilter(_Only(name))
        root.addHandler(h)
    h = TimedRotatingFileHandler(d / "desk.log", when="midnight", backupCount=14, encoding="utf-8")
    h.setFormatter(fmt)
    h.addFilter(_NotAgents())
    root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return d
