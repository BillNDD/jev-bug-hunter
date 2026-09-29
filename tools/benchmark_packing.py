"""Paired identical-wire CPU comparison; no provider/network measurements."""
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

repo, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo))
from bug_hunter.core import Config, Span, multiscale
from bug_hunter.engine import Search
from bug_hunter.questions import pack_questions, screening
from bug_hunter.jev import encode_request
from tests.support import FixtureProvider, source
from tests.test_packing import reference_pack

search = Search(source(1000), FixtureProvider(), Config(), None)
region = Span(1, 48)
state = search.state_for(region, 12)
tree, _ = multiscale(region, 12, 4, 50000)
qs = screening(search.pack, region, [s for s, _ in tree], state=state)
def wires(packer):
    return [encode_request(state, batch) if batch else failed.encode()
            for batch, failed in packer(state, qs)]
assert wires(pack_questions) == wires(reference_pack)
samples = {"baseline": [], "incremental": []}
for trial in range(9):
    order = [("baseline", reference_pack), ("incremental", pack_questions)]
    if trial % 2:
        order.reverse()
    for name, packer in order:
        start = time.perf_counter()
        for _ in range(60):
            list(packer(state, qs))
        samples[name].append((time.perf_counter() - start) / 60)
result = {"kind": "local_cpu_microbenchmark_not_end_to_end", "python": platform.python_version(),
          "platform": platform.system(), "questions": len(qs), "requests": len(wires(pack_questions)),
          "wire_sha256": [hashlib.sha256(w).hexdigest() for w in wires(pack_questions)],
          "byte_identical": True, "iterations_per_sample": 60, "samples_seconds": samples,
          "median_seconds": {k: statistics.median(v) for k, v in samples.items()}, "provider_calls": 0}
result["packing_speed_ratio"] = result["median_seconds"]["baseline"] / result["median_seconds"]["incremental"]
output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
