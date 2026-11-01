from concurrent.futures import Future
from threading import Event, Thread

from shorts_api.media_thread_io import Completion


def test_claim_waits_for_completion_lock_before_ownership_transfer():
    completion = Completion(lambda value: None)
    future = Future()
    future.set_result((42, None))
    entered, returned = Event(), Event()
    result = []

    def claim():
        entered.set()
        result.append(completion.claim())
        returned.set()

    with completion.lock:
        completion.value = 42
        completion.finished = True
        thread = Thread(target=claim)
        thread.start()
        assert entered.wait(2)
        assert not returned.wait(0.05)
    thread.join(2)
    assert result == [42]


def test_abandoned_thread_completion_discards_exactly_once():
    discarded = []
    completion = Completion(discarded.append)
    completion.abandon()
    future = Future()
    future.set_result((42, None))
    completion.complete(future)
    completion.abandon()
    assert discarded == [42]


def test_claimed_completion_is_not_discarded_by_late_abandon():
    discarded = []
    completion = Completion(discarded.append)
    future = Future()
    future.set_result((42, None))
    completion.complete(future)
    assert completion.claim() == 42
    completion.abandon()
    assert discarded == []
