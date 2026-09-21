"""Отдельный процесс для проверки замка: берёт его или сообщает, что он занят."""
import sys

from agent_workers.base.entry import take_lock

if __name__ == "__main__":
    handle = open(sys.argv[1], "a+b")
    try:
        take_lock(handle)
        print("free")
    except OSError:
        print("held")
