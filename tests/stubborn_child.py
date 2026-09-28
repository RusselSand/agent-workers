"""Лидер выходит по SIGTERM сразу, а внук сигнал игнорирует — его должен снять SIGKILL."""
import signal
import subprocess
import sys
import time
from pathlib import Path

GRANDCHILD = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"

if __name__ == "__main__":
    grandchild = subprocess.Popen([sys.executable, "-c", GRANDCHILD])
    Path(sys.argv[1]).write_text(str(grandchild.pid), encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    time.sleep(60)
