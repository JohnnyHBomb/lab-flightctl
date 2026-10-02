"""A0a: the workflow job `full` (the gate for every v2 packet) runs every suite on Linux, then portability, the
external denylist and the plan check. Read as text so the check needs no YAML library."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _jobs(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\s*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def _steps(job: str) -> list[str]:
    return [" ".join(s.split()) for s in re.split(r"^      - ", job, flags=re.M)[1:]]


def test_ci_full_job_lists_every_suite() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    assert "full" in jobs, "ci.yml has no job named full"
    for name, job in jobs.items():  # Linux only: no Windows or macOS job anywhere
        assert re.search(r"^    runs-on: ubuntu-", job, flags=re.M), name
        assert not re.search(r"windows|macos", job, flags=re.I), name
    steps = _steps(jobs["full"])

    def index(pred) -> int:
        hits = [i for i, s in enumerate(steps) if pred(s)]
        assert len(hits) == 1, hits
        return hits[0]

    suite = index(lambda s: 'pytest -o addopts="" -q tests/' in s and "test_v2_plan.py" not in s)
    portability = index(lambda s: "tools/check_portability.py ." in s)
    denylist = index(lambda s: "check_portability.py --denylist" in s and "secrets.FLIGHTCTL_DENYLIST" in s)
    plan = index(lambda s: 'pytest -o addopts="" -q tests/contracts_v2/test_v2_plan.py' in s)
    assert suite < portability < denylist < plan

    listed = set(re.findall(r"(?<=\s)tests/([\w-]+)(?=\s|$)", steps[suite]))
    suites = {p.relative_to(ROOT / "tests").parts[0] for p in (ROOT / "tests").rglob("test_*.py")}
    assert suites and not any(p.parent == ROOT / "tests" for p in (ROOT / "tests").glob("test_*.py"))
    assert listed == suites, f"missing {sorted(suites - listed)}, stale {sorted(listed - suites)}"
