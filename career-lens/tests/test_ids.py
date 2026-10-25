from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id


def test_an_id_carries_its_prefix():
    assert new_id(REQUEST).startswith("rq-")
    assert new_id(CAPTURE).startswith("cp-")
    assert new_id(EXTRACTION).startswith("ex-")


def test_ids_are_unique_across_many_calls():
    assert len({new_id(REQUEST) for _ in range(1000)}) == 1000


def test_ids_sort_chronologically():
    # Six bytes of millisecond timestamp in the high bits, so a sorted listing
    # is a chronological one.
    #
    # Two ids and a sleep is what this used to be, and it passed four times in
    # five against an encoding that was not order-preserving at all -- a test
    # that samples a property instead of asserting it is worse than no test,
    # because it reports the bug as flakiness. Two hundred samples turn a
    # one-in-five inversion into a certainty.
    #
    # Generated in a loop rather than with a patched clock: patching stdlib
    # time.time with a fixed iterator breaks any other caller that happens to
    # run during the test.
    import time

    ids = []
    for _ in range(200):
        ids.append(new_id(REQUEST))
        time.sleep(0.001)
    assert ids == sorted(ids), "ids do not sort into the order they were made"


def test_an_id_is_lowercase_and_has_no_padding():
    value = new_id(CAPTURE)
    assert value == value.lower()
    assert "=" not in value
