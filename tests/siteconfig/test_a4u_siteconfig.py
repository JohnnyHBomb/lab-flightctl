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


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir()}


class Store:
    """A scripted store host: cat of a deploy file is answered from `files` ({path: text}); every argv, host_id and time
    given is recorded; asleep, every call fails as ssh to a host that does not answer."""

    def __init__(self, files, delay=0.0):
        self.files, self.delay, self.asleep, self.calls, self.hosts, self.timeouts = files, delay, False, [], [], []

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        self.calls.append(argv)
        self.hosts.append(host_id)
        self.timeouts.append(timeout_s)
        time.sleep(self.delay)
        result = {"argv": argv, "host_id": host_id, "returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                  "duration_s": 0.0, "error": None}
        if self.asleep:
            return result | {"returncode": 255, "error": SSH_DOWN}
        if argv[0] != "cat" or argv[1] not in self.files:
            return result | {"returncode": 1, "stderr": f"cat: {argv[-1]}: No such file or directory"}
        return result | {"stdout": self.files[argv[1]]}


def publish(files):
    """A deploy directory ({path: text}) holding `files` ({name: text}) and the SHA256SUMS that names them."""
    sums = "".join(f"{sha(text)}  {name}\n" for name, text in files.items())
    return {f"/deploy/{name}": text for name, text in {**files, "SHA256SUMS": sums}.items()}


class Smi:
    """A scripted GPU host: nvidia-smi lists `cards`, none of them in use; every (host_id, argv) is recorded."""

    def __init__(self, cards):
        self.cards, self.calls = cards, []

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        self.calls.append((host_id, argv))
        rows = "".join(f"{uuid}, 10, 16384, 0, 40, 30.00, 200.00, Not Active, Not Active, 0\n" for uuid in self.cards)
        return {"argv": argv, "host_id": host_id, "returncode": 0, "stdout": rows if argv[1] == GPU_QUERY else "", "stderr": "",
                "timed_out": False, "duration_s": 0.0, "error": None}


def _shared_uuid(inventory):
    inventory["hosts"][1]["devices"].append({"device_id": "gpu-c", "uuid": CARD})
    inventory["lanes"][0]["device_ids"].append("gpu-c")


BROKEN = [  # each breaks one binding rule of the inventory; its hash is still right
    lambda inv: inv["lanes"].append(inv["lanes"][0]),  # two lanes named lane-gpu1
    lambda inv: inv["hosts"].append(inv["hosts"][1]),  # two hosts named host-1
    lambda inv: inv["hosts"].pop(1),  # no host of the lane
    lambda inv: inv["lanes"][0].update(device_ids=["gpu-a"]),  # a card the host does not have
    lambda inv: inv["lanes"][0].update(device_ids=[]),  # no card
    lambda inv: inv["hosts"][1]["devices"][0].update(uuid="GPU-1"),  # not a GPU UUID
    lambda inv: inv["hosts"][1]["devices"][0].update(uuid=None),
    _shared_uuid,
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
    texts = {"adapters.json": '{"profile": "sim"}\n', "inventory.json": INVENTORY}
    for name, text in texts.items():
        (deploy / name).write_bytes(text.encode())
    published = subprocess.run(["sha256sum", *texts], cwd=deploy, capture_output=True, check=True)
    (deploy / "SHA256SUMS").write_bytes(published.stdout)

    site = load_site(LocalCommandRunner(), str(deploy), str(local), timeout_s=10)
    assert site["source"] == "deploy" and site["files"] == {name: text.encode() for name, text in texts.items()}
    kept = snapshot(local)
    assert kept.keys() == {"SHA256SUMS", *texts} and kept["SHA256SUMS"] == published.stdout
    (deploy / "adapters.json").write_bytes(b'{"profile": "live"}\n')  # changed, not republished
    with pytest.raises(SiteConfigRefused) as refused:
        load_site(LocalCommandRunner(), str(deploy), str(local), timeout_s=10)
    assert "adapters.json" in str(refused.value) and "inventory.json" not in str(refused.value)
    assert snapshot(local) == kept


def test_local_copy_used_when_store_host_asleep(tmp_path):
    from flightctl.siteconfig import SiteConfigRefused, load_site
    texts = {"adapters.json": '{"profile": "sim"}\n', "inventory.json": INVENTORY}
    store, local = Store(publish(texts)), tmp_path / "local"
    local.mkdir()

    def load(host=store, timeout_s=5):
        return load_site(host, "/deploy", str(local), host_id="store-1", timeout_s=timeout_s)

    store.asleep = True
    with pytest.raises(SiteConfigRefused):  # no verified local copy, no start
        load()
    assert snapshot(local) == {}
    store.asleep = False
    site = load()
    assert site["source"] == "deploy" and site["files"] == {name: text.encode() for name, text in texts.items()}
    assert site["sha256"] == {name: sha(text) for name, text in texts.items()} and set(store.hosts) == {"store-1"}
    kept = snapshot(local)
    assert kept.keys() == {"SHA256SUMS", *texts}

    absent = Store({path: text for path, text in store.files.items() if not path.endswith("inventory.json")})
    with pytest.raises(SiteConfigRefused, match="inventory.json"):  # a failed read never falls back to the local copy
        load(absent)
    ab = "ab" * 32
    for bad in ("", "\n", f"{ab}  adapters.json\n\n", f"{ab.upper()}  adapters.json\n", f"{ab} *adapters.json\n", f"{ab}\tadapters.json\n",
                f"{ab}  d/adapters.json\n", f"{ab}  a.json\n{ab}  a.json\n", f"{ab}  SHA256SUMS\n"):
        with pytest.raises(SiteConfigRefused):  # nor does a malformed manifest
            load(Store({"/deploy/SHA256SUMS": bad}))
    assert snapshot(local) == kept

    store.asleep, store.calls = True, []
    assert load() == {**site, "source": "local"} and store.calls == [["cat", "/deploy/SHA256SUMS"]]
    assert snapshot(local) == kept
    slow = Store(publish(texts), delay=0.3)  # one budget for all reads: the third read finds no time left
    late = load(slow, timeout_s=0.5)
    assert late == {**site, "source": "local"} and len(slow.calls) < 3 and slow.timeouts == sorted(slow.timeouts, reverse=True)
    assert slow.timeouts[0] <= 0.5

    other = tmp_path / "other"  # a write that fails is refused; SHA256SUMS is replaced last, so the old one stays
    other.mkdir()
    (other / "SHA256SUMS").write_bytes(b"old")
    (other / "inventory.json").mkdir()
    store.asleep = False
    with pytest.raises(SiteConfigRefused):
        load_site(store, "/deploy", str(other), timeout_s=5)
    assert (other / "SHA256SUMS").read_bytes() == b"old" and sorted(p.name for p in other.iterdir()) == ["SHA256SUMS", "adapters.json", "inventory.json"]

    store.asleep = True
    (local / "adapters.json").write_bytes(b"{}")  # a local file changed afterwards
    with pytest.raises(SiteConfigRefused, match="adapters.json"):
        load()


def test_expected_uuids_come_from_confirmed_inventory(tmp_path):
    from flightctl.siteconfig import SiteConfigRefused, lane_occupancy, load_site

    def load(text):  # published on a scripted store host, loaded and hash-verified
        return load_site(Store(publish({"inventory.json": text})), "/deploy", str(tmp_path), host_id="store-1", timeout_s=5)

    def occupancy(site, lane="lane-gpu1"):
        return lane_occupancy(site, probe, lane, timeout_s=10)

    def site_of(inventory):  # a site whose inventory.json carries the right hash
        raw = json.dumps(inventory)
        return {"source": "deploy", "files": {"inventory.json": raw.encode()}, "sha256": {"inventory.json": sha(raw)}}

    smi = Smi([CARD, OTHER])
    probe = NvidiaOccupancyProbe(smi, clock=FakeClock())
    site = load(INVENTORY)
    obs = occupancy(site)
    assert obs["status"] == "ok" and obs["expected_uuids"] == [CARD] and [g["uuid"] for g in obs["gpus"]] == [CARD]
    assert {host for host, _ in smi.calls} == {"host-1"}
    assert [argv[-1] for _, argv in smi.calls if argv[1:5] == ["-q", "-d", "PIDS", "-i"]] == [CARD]
    assert obs["thresholds"] == {"lane_noise_mib": 1024, "noise_cap_mib": 64, "noise_allowlist": [{"argv0": "browser", "uid": 1000}]}
    assert occupancy(site_of(json.loads(INVENTORY)))["status"] == "ok"
    smi.cards = [OTHER]
    unknown = occupancy(site)
    assert unknown["status"] == "unknown" and unknown["expected_uuids"] == [CARD]

    smi.calls.clear()  # from here on no command may reach the probe's runner
    lying = publish({"inventory.json": INVENTORY})
    lying["/deploy/SHA256SUMS"] = f"{sha('another inventory')}  inventory.json\n"
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
