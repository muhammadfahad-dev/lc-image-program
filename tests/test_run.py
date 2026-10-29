"""run.py from start to finish with stand-ins (pilot, live, batch, report)."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tb", Path(__file__).with_name("test_batch.py"))
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)
tp = tb.tp

import run  # noqa: E402
from lc_images import config  # noqa: E402

# the workflow's own settings (a paused or final round) must not change what the tests expect
config.VERTEX_BATCH_NEW_ROUNDS = True
config.VERTEX_BATCH_FINAL_ROUND = ""


def call(argv, shopify, gemini, tmp):
    run.OUT = Path(tmp)
    run.Shopify = lambda dry_run=False: shopify
    run.make_generator = lambda args, s: gemini
    sys.argv = ["run.py", *argv]
    return run.main()


def test_pilot_and_live_and_batch():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, _ = tb.setup(tmp)
        assert call(["--dry-run", "--limit", "1"], s, g, tmp) == 0
        summary = json.loads((Path(tmp) / "summary.json").read_text())
        assert summary["mode"] == "pilot (dry run)" and summary["images_passed"] == 13
        assert "USED: passed the check" in (Path(tmp) / "index.html").read_text()

    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, _ = tb.setup(tmp)
        assert call(["--limit", "1"], s, g, tmp) == 0
        assert json.loads(s.products[p["id"]]["audit"]["value"])["status"] == "done"

    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, _ = tb.setup(tmp)
        run_batch_storage = storage
        import lc_images.batch as batch

        batch.Storage = lambda bucket: run_batch_storage
        for _ in range(3):
            assert call(["--vertex", "--batch", "auto"], s, g, tmp) == 0
        assert json.loads(s.products[p["id"]]["audit"]["value"])["status"] == "done"
        assert "batch auto" in json.loads((Path(tmp) / "summary.json").read_text())["mode"]


def test_shards_split_products():
    from run import in_shard

    ids = [{"id": f"gid://shopify/Product/{n}"} for n in range(100, 130)]
    shares = [[p for p in ids if in_shard(p, f"{k}/3")] for k in range(3)]
    assert sum(len(x) for x in shares) == len(ids) and all(shares)
    assert not set(map(id, shares[0])) & set(map(id, shares[1]))
    assert all(in_shard(p, "") for p in ids)


if __name__ == "__main__":
    config.REPORT_SHEET_ID = ""
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
