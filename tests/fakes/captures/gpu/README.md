# Golden GPU captures (packet A3i input)

One capture per lab card model, recorded read-only on 4 Oct 2026 with `flightctl.commands.RecordingCommandRunner`
around the merged A3 `NvidiaOccupancyProbe` plus the three inventory commands of `x-real-commands`
(`inventory`, `numa`, `device_minor`). Each lane was held through the lab's lane controller while recording.

| File | Card model | Recorded state |
|---|---|---|
| `titan-rtx.jsonl` | NVIDIA TITAN RTX (2 cards, NVLink) | healthy, idle |
| `titan-rtx-fault.jsonl` | NVIDIA TITAN RTX | one card after Xid 62 (internal micro-controller halt): every field reads `[GPU requires reset]` / `[N/A]`, and the compute-apps row is `[N/A]` |
| `quadro-rtx-8000.jsonl` | Quadro RTX 8000 | healthy, idle |
| `tesla-t4.jsonl` | Tesla T4 (2 cards) | healthy, idle |

Sanitised before commit: card UUIDs are replaced by stable synthetic ones (prefix `GPU-5a5a`; the same real card always
maps to the same synthetic UUID), and no host, user or path of the recording site remains. Everything else (bus ids,
numbers, `[N/A]`, the nvidia-smi layout, driver 610.57.04) is verbatim.

`<model>.expected.json` holds the observation the production parser returns when the capture is replayed through
`tests.fakes.replay.ReplayCommandRunner` with a fixed clock (`2026-10-04T00:00:00Z`) and the parameters stored in the
file. `tests/gpu/test_golden_capture_files.py` pins that, and that the frozen oracle agrees on the status.
