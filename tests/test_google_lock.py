import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HOLD = """
import sys, time
sys.path.insert(0, {root!r})
import collect_all
with collect_all.google_lock("test-holder"):
    print("held", flush=True)
    time.sleep(2)
"""


def test_second_scrape_waits_for_the_first():
    holder = subprocess.Popen([sys.executable, "-c", HOLD.format(root=str(ROOT))], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    sys.path.insert(0, str(ROOT))
    import collect_all
    started = time.monotonic()
    with collect_all.google_lock("test-waiter"):
        waited = time.monotonic() - started
    holder.wait(timeout=10)
    assert waited >= 1.0
