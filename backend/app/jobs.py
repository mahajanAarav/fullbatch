"""
Run the deadline job from the command line.

    python -m app.jobs            run once and exit (what a scheduler or Render Workflow calls)
    python -m app.jobs --loop     run every 30 seconds (handy for local development)
"""

import sys
import time

from app import drops
from app.db import get_session_factory
from app.paypal import from_env


def run_once() -> dict:
    return drops.run_deadline_job(get_session_factory(), from_env())


if __name__ == "__main__":
    while True:
        print(run_once(), flush=True)
        if "--loop" not in sys.argv:
            break
        time.sleep(30)
