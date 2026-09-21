"""Подпроцесс, который шлёт уведомления без конца и никогда не отвечает на запрос."""
import json
import time

if __name__ == "__main__":
    while True:
        print(json.dumps({"method": "notification", "params": {"tick": time.time()}}), flush=True)
        time.sleep(0.01)
