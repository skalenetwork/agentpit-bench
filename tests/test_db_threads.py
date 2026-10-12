"""One DB shared by the event loop and to_thread workers must not raise sqlite 'API misuse'."""
import threading

from agentpitbench.db import DB


def test_concurrent_threads_share_one_db(tmp_path):
    db = DB(tmp_path / "b.sqlite")
    errors = []

    def work(n):
        try:
            for i in range(300):
                db.put(f"k{n}-{i}", i)
                assert db.get(f"k{n}-{i}") == i
                db.q("SELECT * FROM kv LIMIT 5")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert db.one("SELECT COUNT(*) c FROM kv")["c"] == 8 * 300
