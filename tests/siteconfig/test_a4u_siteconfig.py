"""A4u part 1 named acceptance tests: the hash-verified site loader, its verified local copy and the lane binding."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from flightctl.commands import LocalCommandRunner
from flightctl.gpu import GPU_QUERY, NvidiaOccupancyProbe
from tests.fakes.clock import FakeClock

INVENTORY = (Path(__file__).resolve().parents[2] / "config" / "inventory-v2.json.example").read_text(encoding="utf-8")
CARD, OTHER = "GPU-00000000-0000-0000-0000-000000000002", "GPU-00000000-0000-0000-0000-0000000000ff"
SSH_DOWN = {"code": "transport_failed", "message": "ssh: connect: no route to host", "layer": "transport", "cause": None}
ROW = "{}, 10, 16384, 0, 40, 30.00, 200.00, Not Active, Not Active, 0\n"
TEXTS = {"adapters.json": '{"profile": "sim"}\n', "inventory.json": INVENTORY}
FILES = {name: text.encode() for name, text in TEXTS.items()}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir()}


class Host:
    """A scripted host: answer(argv) -> (returncode, stdout, error); every argv, host_id and time given is recorded."""

    def __init__(self, answer, delay=0.0):
        self.answer, self.delay, self.calls, self.hosts, self.timeouts = answer, delay, [], [], []

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        self.calls.append(argv)
        self.hosts.append(host_id)
        self.timeouts.append(timeout_s)
        time.sleep(self.delay)
        returncode, stdout, error = self.answer(argv)
        return {"argv": argv, "host_id": host_id, "returncode": returncode, "stdout": stdout, "stderr": "", "timed_out": False,
                "duration_s": 0.0, "error": error}


class Store(Host):
    """The store host: cat of a deploy file ({path: text}) is answered from `files`; asleep, every call fails as ssh does."""

    def __init__(self, files, delay=0.0):
        super().__init__(self.cat, delay)
        self.files, self.asleep = files, False

    def cat(self, argv):
        if self.asleep:
            return 255, "", SSH_DOWN
        return (0, self.files[argv[1]], None) if argv[0] == "cat" and argv[1] in self.files else (1, "", None)


def publish(files):
    """A deploy directory ({path: text}) holding `files` ({name: text}) and the SHA256SUMS that names them."""
    sums = "".join(f"{sha(text)}  {name}\n" for name, text in files.items())
    return {f"/deploy/{name}": text for name, text in {**files, "SHA256SUMS": sums}.items()}


BROKEN = [  # each breaks one binding rule of the inventory; its hash is still right
    lambda inv: inv["lanes"].append(inv["lanes"][0]),  # two lanes named lane-gpu1
    lambda inv: inv["hosts"].append(inv["hosts"][1]),  # two hosts named host-1
    lambda inv: inv["hosts"].pop(1),  # no host of the lane
    lambda inv: inv["lanes"][0].update(device_ids=["gpu-a"]),  # a card the host does not have
    lambda inv: inv["lanes"][0].update(device_ids=[]),  # no card
    lambda inv: inv["hosts"][1]["devices"][0].update(uuid="GPU-1"),  # not a GPU UUID
    lambda inv: inv["hosts"][1]["devices"][0].update(uuid=None),
    lambda inv: [inv["hosts"][1]["devices"].append({"device_id": "gpu-c", "uuid": CARD}), inv["lanes"][0]["device_ids"].append("gpu-c")],
    lambda inv: inv["lanes"][0]["rules"]["external_tenant"].pop("noise_cap_mib"),
    lambda inv: inv["lanes"][0]["rules"]["external_tenant"].update(lane_noise_mib="1024"),
    lambda inv: inv["lanes"][0]["rules"]["external_tenant"].update(noise_allowlist=[{"argv0": "browser"}]),
]


@pytest.mark.realtime
def test_config_loader_rejects_hash_mismatch(tmp_path):
    from flightctl.siteconfig import SiteConfigRefused, load_site
    deploy, local = tmp_path / "deploy", tmp_path / "local"
    deploy.mkdir()
    local.mkdir()
    for name, data in FILES.items():
        (deploy / name).write_bytes(data)
    published = subprocess.run(["sha256sum", *FILES], cwd=deploy, capture_output=True, check=True)
    (deploy / "SHA256SUMS").write_bytes(published.stdout)
    site = load_site(LocalCommandRunner(), str(deploy), str(local), timeout_s=10)
    assert site["source"] == "deploy" and site["files"] == FILES
    kept = snapshot(local)
    assert kept.keys() == {"SHA256SUMS", *FILES} and kept["SHA256SUMS"] == published.stdout
    (deploy / "adapters.json").write_bytes(b'{"profile": "live"}\n')  # changed, not republished
    with pytest.raises(SiteConfigRefused) as refused:
        load_site(LocalCommandRunner(), str(deploy), str(local), timeout_s=10)
    assert "adapters.json" in str(refused.value) and "inventory.json" not in str(refused.value)
    assert snapshot(local) == kept


def test_local_copy_used_when_store_host_asleep(tmp_path):
    from flightctl.siteconfig import SiteConfigRefused, load_site
    store, local = Store(publish(TEXTS)), tmp_path / "local"
    local.mkdir()

    def load(host=store, into=local, timeout_s=5):
        return load_site(host, "/deploy", str(into), host_id="store-1", timeout_s=timeout_s)

    def refused(host, into=local):
        with pytest.raises(SiteConfigRefused) as raised:
            load(host, into)
        return str(raised.value)

    store.asleep = True
    refused(store)  # no verified local copy, no start
    assert snapshot(local) == {}
    store.asleep = False
    site = load()
    assert site["source"] == "deploy" and site["files"] == FILES and set(store.hosts) == {"store-1"}
    assert site["sha256"] == {name: sha(text) for name, text in TEXTS.items()}
    kept = snapshot(local)
    assert kept.keys() == {"SHA256SUMS", *FILES}
    absent = Store({path: text for path, text in store.files.items() if not path.endswith("inventory.json")})
    assert "inventory.json" in refused(absent)  # a failed read never falls back to the local copy
    page = "x\n"  # nor does a malformed manifest, although every file it could name is there with the right hash
    h, pages = sha(page), {"/deploy/adapters.json": page, "/deploy/d/adapters.json": page}
    for bad in ("", "\n", f"{h}  adapters.json\n\n", f"{h.upper()}  adapters.json\n", f"{h} *adapters.json\n", f"{h}\tadapters.json\n",
                f"{h}  d/adapters.json\n", f"{h}  adapters.json\n" * 2, f"{h}  SHA256SUMS\n"):
        refused(Store({"/deploy/SHA256SUMS": bad, **pages}))
    assert snapshot(local) == kept
    other = tmp_path / "other"
    (other / "adapters.json").mkdir(parents=True)
    (other / "SHA256SUMS").write_bytes(b"old")
    refused(store, other)  # a write that fails is refused; SHA256SUMS is replaced last, so the old one stays (and no temporary file)
    assert (other / "SHA256SUMS").read_bytes() == b"old" and sorted(p.name for p in other.iterdir()) == ["SHA256SUMS", "adapters.json"]
    store.asleep, store.calls = True, []
    assert load() == {**site, "source": "local"} and store.calls == [["cat", "/deploy/SHA256SUMS"]]
    assert snapshot(local) == kept
    slow = Store(publish(TEXTS), delay=0.3)  # one budget for all reads: the third read finds no time left
    assert load(slow, timeout_s=0.5) == {**site, "source": "local"}
    assert len(slow.calls) < 3 and slow.timeouts == sorted(slow.timeouts, reverse=True) and slow.timeouts[0] <= 0.5
    (local / "adapters.json").write_bytes(b"{}")  # a local file changed afterwards
    assert "adapters.json" in refused(store)


def test_expected_uuids_come_from_confirmed_inventory(tmp_path):
    from flightctl.siteconfig import SiteConfigRefused, lane_occupancy, load_site
    cards = [CARD, OTHER]  # what nvidia-smi lists
    smi = Host(lambda argv: (0, "".join(ROW.format(uuid) for uuid in cards) if argv[1] == GPU_QUERY else "", None))
    probe = NvidiaOccupancyProbe(smi, clock=FakeClock())
    occupancy = lambda site, lane="lane-gpu1": lane_occupancy(site, probe, lane, timeout_s=10)  # noqa: E731

    def load(text):  # published on a scripted store host, loaded and hash-verified
        return load_site(Store(publish({"inventory.json": text})), "/deploy", str(tmp_path), host_id="store-1", timeout_s=5)

    def site_of(inventory):  # a site whose inventory.json carries the right hash
        raw = json.dumps(inventory)
        return {"source": "deploy", "files": {"inventory.json": raw.encode()}, "sha256": {"inventory.json": sha(raw)}}

    site = load(INVENTORY)
    obs = occupancy(site)
    assert obs["status"] == "ok" and obs["expected_uuids"] == [CARD] and [g["uuid"] for g in obs["gpus"]] == [CARD]
    assert set(smi.hosts) == {"host-1"}
    assert [argv[-1] for argv in smi.calls if argv[1:5] == ["-q", "-d", "PIDS", "-i"]] == [CARD]
    assert obs["thresholds"] == {"lane_noise_mib": 1024, "noise_cap_mib": 64, "noise_allowlist": [{"argv0": "browser", "uid": 1000}]}
    assert occupancy(site_of(json.loads(INVENTORY)))["status"] == "ok"
    cards[:] = [OTHER]
    assert (unknown := occupancy(site))["status"] == "unknown" and unknown["expected_uuids"] == [CARD]

    smi.calls.clear()  # from here on no command may reach the probe's runner
    lying = {**publish({"inventory.json": INVENTORY}), "/deploy/SHA256SUMS": f"{sha('another inventory')}  inventory.json\n"}
    with pytest.raises(SiteConfigRefused, match="inventory.json"):
        load_site(Store(lying), "/deploy", str(tmp_path), timeout_s=5)
    replaced = {**site, "files": {"inventory.json": INVENTORY.replace(CARD, OTHER).encode()}}  # replaced after loading
    draft = load(INVENTORY.replace('"stage": "confirmed"', '"stage": "draft"'))  # verified, but not confirmed
    for refused in (replaced, draft, {}, {"files": {}, "sha256": {}}):
        with pytest.raises(SiteConfigRefused):
            occupancy(refused)
    with pytest.raises(SiteConfigRefused):
        occupancy(site, "lane-none")
    for break_it in BROKEN:
        inventory = json.loads(INVENTORY)
        break_it(inventory)
        with pytest.raises(SiteConfigRefused):
            occupancy(site_of(inventory))
    assert smi.calls == []
