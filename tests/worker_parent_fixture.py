"""Disposable controller for proving idle worker EOF after abrupt parent death."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[1])))
from tests.test_transport_worker import OfflineWorker, WIRE

worker = OfflineWorker()
worker.exchange(WIRE, 5)
print(json.dumps({"worker_pid": worker._child.ready_pid}), flush=True)
sys.stdin.buffer.read(1)
worker.close()
