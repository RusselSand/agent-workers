"""Лидер запускает внука в фоне и сразу выходит сам: ход кончился, а внук ещё жив."""
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(sys.argv[1]).write_text(str(grandchild.pid), encoding="utf-8")
