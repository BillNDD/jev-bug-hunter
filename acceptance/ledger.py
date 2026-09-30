"""Durable, serialized campaign accounting; no provider calls or automatic retries."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".pending")
    with temp.open("wb") as stream:
        stream.write((json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def loads(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def read(path):
    return loads(Path(path).read_text(encoding="utf-8"))


def validate_budget(budget):
    if (type(budget) is not dict or type(budget.get("limit")) is not int
            or budget["limit"] < 1 or type(budget.get("charged")) is not int
            or budget["charged"] < 0 or type(budget.get("attempts")) is not list
            or type(budget.get("basis")) is not str or not budget["basis"].strip()
            or ("ledger_id" in budget and (type(budget["ledger_id"]) is not str or not budget["ledger_id"]))
            or type(budget.get("paused_by_user", False)) is not bool
            or (budget.get("reservation") is not None and type(budget["reservation"]) is not dict)):
        raise ValueError("invalid_campaign_budget")
    if budget.get("reservation") is not None:
        maximum = budget["reservation"].get("maximum_calls")
        if type(maximum) is not int or maximum < 1:
            raise ValueError("invalid_campaign_reservation")
    for attempt in budget["attempts"]:
        if (type(attempt) is not dict or type(attempt.get("charged_calls")) is not int
                or attempt["charged_calls"] < 0):
            raise ValueError("invalid_campaign_attempt")
    return budget


def new_budget(limit):
    return validate_budget({"schema": "jev-outcome-budget-v2", "ledger_id": uuid.uuid4().hex,
                            "limit": limit, "charged": 0, "reservation": None,
                            "attempts": [], "paused_by_user": False,
                            "basis": "additional-calls-for-this-acceptance-campaign"})


@contextmanager
def transaction(path):
    """Serialize reserve/settle/pause edits without locking during provider work.

    The lock file is persistent: deleting it would let two processes lock
    different file identities. Lock contention fails closed before dispatch.
    Legacy ledger fields and historical attempts are retained verbatim.
    """
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("campaign_budget_busy") from None
        try:
            budget = validate_budget(read(path))
            before = deepcopy(budget)
            yield budget
            validate_budget(budget)
            if budget != before:
                save(path, budget)
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def set_paused(path, paused):
    if type(paused) is not bool:
        raise ValueError("invalid_pause_state")
    with transaction(path) as budget:
        budget["paused_by_user"] = paused
    return {"paused_by_user": paused, "charged": budget["charged"],
            "limit": budget["limit"], "reservation_pending": budget.get("reservation") is not None}
