"""Serve the API + PWA over a DEMO DB with NO agents running (screenshots)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn  # noqa
from db import DB  # noqa
from main import Desk  # noqa
from api import make_app  # noqa
from sources import ReplaySources  # noqa

desk = Desk(db=DB(sys.argv[1]), src=ReplaySources())
uvicorn.run(make_app(desk), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning", access_log=False)
