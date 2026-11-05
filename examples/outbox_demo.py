"""Exercise the real Worker/outbox with FakeAdapter: no account or provider CLI needed."""

from pathlib import Path
from tempfile import TemporaryDirectory

from agent_workers.base import Profile, Worker
from agent_workers.testing import QUIET, FakeAdapter


def main() -> None:
    with TemporaryDirectory(prefix="agent-workers-demo-") as folder:
        root = Path(folder)
        adapter = FakeAdapter(root)
        worker = Worker(adapter, Profile("demo", root / "account"), root / "runs", QUIET)
        request = {"user": "Ready to implement."}

        print("OFFLINE DEMO | FakeAdapter | no model calls or subscription usage\n")
        first = worker.run(request, key="task-0042", ensure_login=True)
        print(f"1. Run task-0042       state={first['state']}")
        print(f"   Reply              {first['reply'].text}")
        print(f"   Simulated window   {first['before'].worst}% -> {first['after'].worst}%")
        print(f"   Simulated API cost ${first['cost'].amount} {first['cost'].currency}")
        print(f"   Pending results    {len(worker.pending())}\n")

        repeated = worker.run(request, key="task-0042", ensure_login=True)
        print(f"2. Retry task-0042     state={repeated['state']}")
        print(f"   Same reply         {repeated['reply'].text == first['reply'].text}")
        print(f"   Adapter invocations {len(adapter.asked)} (still one)\n")

        # A real coordinator must persist/accept the result before this call.
        worker.collect(repeated["entry"])
        print("3. Accept and collect entry removed")
        print(f"   Pending results    {len(worker.pending())}")
        assert first["state"] == "answered"
        assert repeated["state"] == "resumed"
        assert len(adapter.asked) == 1
        assert not worker.pending()


if __name__ == "__main__":
    main()
