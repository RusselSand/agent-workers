"""Подпроцесс, который порождает внука и ждёт: убить его — не значит убить внука."""
import subprocess
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(sys.argv[1]).write_text(str(grandchild.pid), encoding="utf-8")
    time.sleep(60)
