"""The cost contract's doors that read a product idiom (tests/_cost_meter.py, tests/_cost_driver.py).

Each counts work only as long as the code it watches keeps one idiom: a lock file is created by an
exclusive ``os.open`` (``platform_layer``), a stored dialogue row is decoded by ``dialogue_store.decode``,
a host name is resolved through ``socket.getaddrinfo``. These checks live apart from
tests/test_cost_contract.py so that a change to those modules re-runs them in seconds (the commit
gate selects a test module by its direct imports) instead of booting the contract's two installs.
"""

from __future__ import annotations

import json

import pytest

from tests._cost_meter import CostMeter, tagged


@pytest.mark.parametrize("contended", [False, True])
def test_the_meter_counts_one_lock_acquisition_however_contended(tmp_path, contended):
    """A lock taken after its holder let go is one acquisition: the waiter's refused creates and its
    probes of the holder are contention (more opens of the lock file), never more acquisitions."""
    import threading

    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    lock = tmp_path / "state" / "chronicle.lock"
    releaser = threading.Timer(0.3, release_exclusive_file_lock,
                               (lock, acquire_exclusive_file_lock(lock, timeout_sec=1.0) if contended else None))

    def take():
        fd = acquire_exclusive_file_lock(lock, timeout_sec=30.0)
        assert fd is not None
        release_exclusive_file_lock(lock, fd)

    with CostMeter(tmp_path) as meter:
        if contended:
            releaser.start()
        tagged("take", take)
    if contended:
        releaser.join()
    counts = meter.snapshot("take")["counts"]
    assert counts.get("lock_acquired") == 1 and (counts.get("os_open:lock", 0) > 1) is contended, counts


def test_the_meter_counts_stored_dialogue_rows_decoded_and_no_other_json(tmp_path):
    """``rows_decoded`` is the stored dialogue row bodies a boundary decodes; any other JSON is not a row."""
    from ouroboros import dialogue_store

    with CostMeter(tmp_path) as meter:
        tagged("read", lambda: (dialogue_store.decode('{"text": "a stored row"}'), json.loads('{"other": 1}')))
    counts = meter.snapshot("read")["counts"]
    assert (counts.get("json_loads"), counts.get("rows_decoded")) == (2, 1), counts


def test_the_driver_refuses_and_counts_every_lookup_beyond_this_machine(tmp_path):
    """The driver's resolver: this machine's names resolve, any other host is refused and counted under
    the boundary that asked (a turn's capability fetch, for one), so a measurement never waits on a network."""
    from tests._cost_driver import loopback_only

    with CostMeter(tmp_path) as meter:
        lookup = loopback_only(meter, lambda host, *_args, **_kwargs: [host])
        assert tagged("turn", lambda: (lookup("127.0.0.1", 0), lookup("localhost", 0))) == (["127.0.0.1"], ["localhost"])
        with pytest.raises(OSError, match="no network"):
            tagged("turn", lambda: lookup("openrouter.ai", 443))
    snap = meter.snapshot("turn")
    assert snap["counts"].get("network_refused") == 1 and snap["names"] == {"network_refused openrouter.ai": 1}
