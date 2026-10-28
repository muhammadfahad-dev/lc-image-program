"""Batch rounds with stand-ins for Vertex AI, Cloud Storage and Shopify."""
import base64
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("tp", Path(__file__).with_name("test_pipeline.py"))
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)  # reuses FakeGemini, FakeShopify, product(), jpeg()

from lc_images import batch, config
from lc_images.vertex import Spend

config.VERTEX_BUCKET = "bucket"


class FakeStorage:
    def __init__(self):
        self.files = {}

    def upload(self, name, data, content_type):
        self.files[name] = data
        return f"gs://bucket/{name}"

    def list(self, prefix):
        return [n for n in self.files if n.startswith(prefix)]

    def download(self, name):
        return self.files[name]

    def delete(self, name):
        self.files.pop(name, None)


class BatchGemini(tp.FakeGemini):
    """Answers Batch jobs by writing an output file, like Vertex AI does."""

    def __init__(self, storage, tmp, **kw):
        super().__init__(tp.SELECT_OPEN, **kw)
        self.storage, self.jobs, self.next_state = storage, {}, "JOB_STATE_SUCCEEDED"
        self.spend = Spend(Path(tmp) / "spend.json")
        self.drop = set()  # image shapes whose answers go missing
        self.error = set()  # image shapes answered with an error status
        self.echo = lambda request: request  # how the answer lines repeat the request

    def create_batch(self, src, dest, display):
        name = f"projects/p/locations/global/batchPredictionJobs/{len(self.jobs) + 1}"
        self.jobs[name] = (src, dest)
        return name

    def batch_state(self, name):
        src, dest = self.jobs[name]
        if self.next_state == "JOB_STATE_SUCCEEDED":
            lines = []
            for line in self.storage.download(src.replace("gs://bucket/", "")).decode().splitlines():
                request = json.loads(line)["request"]
                ratio = request["generationConfig"]["imageConfig"]["aspectRatio"]
                if ratio in self.drop:
                    continue
                if ratio in self.error:
                    lines.append(json.dumps({"request": request, "status": "RESOURCE_EXHAUSTED: try later"}))
                    continue
                self.calls["image"] += 1
                data = base64.b64encode(tp.jpeg((10, 20, 30))).decode()
                lines.append(json.dumps({"request": self.echo(request), "response": {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": data}}]}}]}}))
            self.storage.upload(dest.replace("gs://bucket/", "") + "/prediction-model-1/predictions.jsonl", "\n".join(lines).encode(), "application/jsonl")
        return self.next_state


class BatchShopify(tp.FakeShopify):
    def __init__(self, products):
        super().__init__()
        self.products = {p["id"]: p for p in products}
        self.shop = {}

    def iter_products(self, handles=None):
        yield from self.products.values()

    def product_by_id(self, pid):
        return self.products.get(pid)

    def save_audit(self, pid, audit):
        super().save_audit(pid, audit)
        self.products[pid]["audit"] = {"value": json.dumps(audit)}

    def get_shop_json(self, key, default=None):
        return json.loads(json.dumps(self.shop.get(key, default)))

    def set_shop_json(self, key, value):
        self.shop[key] = json.loads(json.dumps(value))


def setup(tmp, **kw):
    storage = FakeStorage()
    p = tp.product(colours=("Navy", "Emerald Green"))
    s = BatchShopify([p])
    g = BatchGemini(storage, tmp, **kw)
    g.select = dict(tp.SELECT_OPEN, colour_photos={}, input_colour="Navy")
    return storage, s, g, p, batch.BatchRunner(g, s, storage, out_dir=tmp, log=lambda *a: None)


def audit_of(s, p):
    return json.loads(s.products[p["id"]]["audit"]["value"])


def test_two_rounds_listing_then_colours():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        assert runner.auto(5) is not None  # round 1: 12 listing images
        a = audit_of(s, p)
        assert sum(v["status"] == "queued" for v in a["slots"].values()) == 12 and not a["colours"]
        assert s.shop["image_batches"]["jobs"][0]["count"] == 12
        assert runner.auto(5) is not None  # collect round 1, then round 2: the colour image
        a = audit_of(s, p)
        assert all(v["status"] == "done" for v in a["slots"].values())
        assert a["colours"]["Navy"]["status"] == "main_image" and a["colours"]["Emerald Green"]["status"] == "queued"
        assert runner.auto(5) is None  # collect round 2; nothing left
        a = audit_of(s, p)
        assert a["status"] == "done" and a["colours"]["Emerald Green"]["status"] == "done"
        assert s.shop["image_batches"]["jobs"] == [] and storage.files == {}  # read jobs and their files are cleared
        assert abs(g.spend.data["usd"] - 13 * config.VERTEX_BATCH_PRICE_PER_IMAGE) < 1e-6


def test_failed_checks_retry_then_placeholder():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp, fail_slots=["Seam and stitching"])
        for _ in range(4):
            runner.auto(5)
        a = audit_of(s, p)
        assert a["slots"]["closer-2"]["status"] == "manual" and a["slots"]["closer-2"]["placeholder"] == "placeholder-closer-2"
        closer = json.loads([m for m in s.metafields if m["key"] == "closer_images"][-1]["value"])
        assert closer[1] == "placeholder-closer-2"


def test_missing_answers_and_errors_do_not_count_as_tries():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        g.drop, g.error = {"1:1"}, {"16:9"}  # closer-2 is the only square image, closer-4 the only wide one
        runner.auto(5)
        runner.collect()
        a = audit_of(s, p)
        assert a["slots"]["closer-2"] == {"status": "pending", "tries": 0, "problems": ["no answer in the Batch results"]}
        assert a["slots"]["closer-4"]["status"] == "pending" and a["slots"]["closer-4"]["tries"] == 0
        assert "RESOURCE_EXHAUSTED" in a["slots"]["closer-4"]["problems"][0]
        assert a["slots"]["closer-3"]["status"] == "done"


def snake_case(request):
    """The request as Google may repeat it: snake_case keys and different spacing in the prompt."""
    parts = request["contents"][0]["parts"]
    return {"contents": [{"role": "user", "parts": [
        {"file_data": {"file_uri": parts[0]["fileData"]["fileUri"], "mime_type": "image/jpeg"}},
        {"text": "  " + parts[1]["text"].replace(" ", "  ") + "\n"},
    ]}]}


def test_answers_repeated_in_snake_case_are_matched():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        g.echo = snake_case
        runner.auto(5)
        runner.collect()
        assert all(v["status"] == "done" for v in audit_of(s, p)["slots"].values())


def test_unmatched_answers_keep_the_job_and_its_files():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        g.echo = lambda request: {}
        runner.auto(5)
        runner.collect()
        a = audit_of(s, p)
        assert all(v["status"] == "queued" for v in a["slots"].values())  # not put back, so not paid for twice
        assert len(s.shop["image_batches"]["jobs"]) == 1 and any("/output/" in n for n in storage.files)


def test_failed_job_puts_images_back():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.auto(5)
        g.next_state = "JOB_STATE_FAILED"
        runner.collect()
        a = audit_of(s, p)
        assert all(v["status"] == "pending" and v["tries"] == 0 for v in a["slots"].values())
        g.next_state = "JOB_STATE_RUNNING"
        runner.submit(5)
        assert runner.collect() == 1  # still running: nothing read, nothing lost


def test_failed_job_still_uses_its_finished_images_and_refunds_the_rest():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        g.drop = {"1:1"}  # closer-2 was never made
        runner.auto(5)
        spent = g.spend.data["usd"]
        g.batch_state(s.shop["image_batches"]["jobs"][0]["name"])  # Google writes what it finished
        g.next_state = "JOB_STATE_FAILED"
        runner.collect()
        a = audit_of(s, p)
        assert a["slots"]["closer-3"]["status"] == "done"
        assert a["slots"]["closer-2"]["status"] == "pending" and a["slots"]["closer-2"]["tries"] == 0
        assert abs(g.spend.data["usd"] - (spent - config.VERTEX_BATCH_PRICE_PER_IMAGE)) < 1e-6 + config.VERTEX_PRICE_PER_CHECK * 12


def test_spending_cap_limits_the_round():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        each = config.VERTEX_BATCH_PRICE_PER_IMAGE + config.VERTEX_PRICE_PER_CHECK  # an image and its check
        g.spend.data["usd"] = config.VERTEX_BUDGET_USD - 3 * each + 0.0001  # room for 2.99 images
        runner.submit(5)
        assert s.shop["image_batches"]["jobs"][0]["count"] == 2


def test_collect_stopped_by_the_cap_keeps_the_job():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.auto(5)
        real_ask, calls = g.ask_json, {"n": 0}

        def ask(images, prompt):
            calls["n"] += 1
            if calls["n"] == 4:
                raise batch.QuotaExhausted("cap")
            return real_ask(images, prompt)

        g.ask_json = ask
        try:
            runner.collect()
            raise AssertionError("expected the cap to stop the collect")
        except batch.QuotaExhausted:
            pass
        assert len(s.shop["image_batches"]["jobs"]) == 1 and storage.files, "the job must stay to be read again"
        g.ask_json = real_ask
        runner.collect()
        a = audit_of(s, p)
        assert all(v["status"] == "done" for v in a["slots"].values()), a["slots"]
        assert s.shop["image_batches"]["jobs"] == []


def test_images_of_a_lost_job_go_back_into_the_queue():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.submit(5)
        s.shop["image_batches"] = {"jobs": []}  # the job record was lost
        (Path(tmp) / "batches.json").unlink()
        runner2 = batch.BatchRunner(g, s, storage, out_dir=tmp, log=lambda *a: None)
        runner2.submit(5)
        jobs = s.shop["image_batches"]["jobs"]
        assert len(jobs) == 1 and jobs[0]["count"] == 12  # sent again in a new job


def test_answer_from_an_older_job_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.submit(5)
        run = runner._run_for(s.products[p["id"]])
        run.prepare()
        ref = {"kind": "slot", "name": "hero-1", "value": None, "base": "ref"}
        assert run.job_for(ref, "an-older-job") is None
        assert run.job_for(ref, s.shop["image_batches"]["jobs"][0]["name"]) is not None


def test_two_machines_do_not_wipe_each_others_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        other = dict(name="projects/p/locations/global/batchPredictionJobs/other", prefix="lc-batches/x", created="x", count=1, state="JOB_STATE_RUNNING")
        runner.state = {"jobs": []}
        s.shop["image_batches"] = {"jobs": [other]}  # GitHub submitted meanwhile
        runner.submit(5)
        names = {j["name"] for j in s.shop["image_batches"]["jobs"]}
        assert other["name"] in names and len(names) == 2


def test_a_product_that_always_fails_does_not_block_batch_forever():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.submit(5)
        real = s.upload_image
        s.upload_image = lambda *a: (_ for _ in ()).throw(RuntimeError("userError: always"))
        for _ in range(config.VERTEX_BATCH_MAX_READS):
            runner.collect()
        assert s.shop["image_batches"]["jobs"] == [], "the job is dropped after the last read"
        s.upload_image = real
        assert runner.submit(5) is not None  # its unfinished images are sent again
        assert audit_of(s, p)["slots"]["hero-1"]["tries"] == 1  # ... and that counted as a try


def test_an_image_that_can_never_be_finished_ends_as_manual():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        real = s.set_metafields
        for round_ in range(3):
            runner = batch.BatchRunner(g, s, storage, out_dir=tmp, log=lambda *a: None)
            runner.submit(5)
            s.upload_image = lambda *a: (_ for _ in ()).throw(RuntimeError("userError: always"))
            for _ in range(config.VERTEX_BATCH_MAX_READS):
                runner.collect()
            s.upload_image = tp.FakeShopify.upload_image.__get__(s)
        runner = batch.BatchRunner(g, s, storage, out_dir=tmp, log=lambda *a: None)
        runner.submit(5)  # the third release ends the images as "do manually": nothing more to pay for
        a = audit_of(s, p)
        assert all(v["status"] == "manual" for v in a["slots"].values()), {k: v["status"] for k, v in a["slots"].items()}
        assert a["slots"]["closer-2"].get("placeholder") == "placeholder-closer-2"
        jobs = s.shop["image_batches"]["jobs"]
        # only the colour images are still sent: with 1.1 failed, Navy cannot reuse the front image, so both colours
        # are made from the product photo
        assert len(jobs) == 1 and jobs[0]["count"] == 2
        assert a["colours"]["Emerald Green"]["status"] == "queued" and a["colours"]["Navy"]["status"] == "queued"


def test_a_job_another_machine_just_started_is_not_taken_for_lost():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        other = batch.BatchRunner(g, s, storage, out_dir=tmp + "/other", log=lambda *a: None)
        runner.state = runner._load_state()  # the PC loaded the (empty) list ...
        other.submit(5)  # ... then GitHub started a job and marked the images
        runner.submit(5)  # the PC must not send them again
        a = audit_of(s, p)
        assert len(s.shop["image_batches"]["jobs"]) == 1 and all(v["status"] == "queued" for v in a["slots"].values())


def test_storage_error_is_not_taken_for_already_read():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        runner.submit(5)
        storage.download = lambda name: (_ for _ in ()).throw(RuntimeError("Cloud Storage 500"))
        try:
            runner.collect()
        except RuntimeError:
            pass
        assert len(s.shop["image_batches"]["jobs"]) == 1  # kept, to be read when storage works


def test_final_round_waits_for_jobs_out_then_sends_one_only():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        try:
            assert runner.auto(5) is not None  # a normal round is out
            config.VERTEX_BATCH_FINAL_ROUND = "last"
            config.VERTEX_BATCH_MAX_JOBS, old_max = 4, config.VERTEX_BATCH_MAX_JOBS
            runner.collect = lambda: None  # the job out is not finished yet
            assert runner.auto(5) is None  # waits: a job is still out
            del runner.collect
            assert runner.auto(5) is not None  # collect it, then the final round (the colour image)
            assert s.shop["image_batches"]["final_round"] == "last"
            assert batch.BatchRunner(g, s, storage, out_dir=tmp, log=lambda *a: None).auto(5) is None  # collect; no new round
            assert s.shop["image_batches"]["jobs"] == [] and s.shop["image_batches"]["final_round"] == "last"
            assert audit_of(s, p)["colours"]["Emerald Green"]["status"] == "done"
        finally:
            config.VERTEX_BATCH_FINAL_ROUND = ""
            config.VERTEX_BATCH_MAX_JOBS = old_max


def test_new_rounds_off_only_reads_jobs_out():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        try:
            assert runner.auto(5) is not None  # round 1 is out
            config.VERTEX_BATCH_NEW_ROUNDS = False
            assert runner.auto(5) is None  # round 1 is read, the colour round is not sent
            assert s.shop["image_batches"]["jobs"] == []
            a = audit_of(s, p)
            assert all(v["status"] == "done" for v in a["slots"].values()) and "Emerald Green" not in a["colours"]
        finally:
            config.VERTEX_BATCH_NEW_ROUNDS = True


def test_new_rounds_off_only_reads_jobs_out():
    with tempfile.TemporaryDirectory() as tmp:
        storage, s, g, p, runner = setup(tmp)
        try:
            assert runner.auto(5) is not None  # round 1 is out
            config.VERTEX_BATCH_NEW_ROUNDS = False
            assert runner.auto(5) is None  # round 1 is read, the colour round is not sent
            assert s.shop["image_batches"]["jobs"] == []
            a = audit_of(s, p)
            assert all(v["status"] == "done" for v in a["slots"].values()) and "Emerald Green" not in a["colours"]
        finally:
            config.VERTEX_BATCH_NEW_ROUNDS = True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
