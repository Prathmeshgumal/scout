from job_finder.guard import START_LIMIT_S, SubagentGuard


def make():
    now = [0.0]
    names: list[str] = []
    return SubagentGuard(lambda: names, clock=lambda: now[0]), names, now


def test_one_run_of_each_type():
    guard, names, _ = make()
    assert guard.try_start("research", "t1")
    assert not guard.try_start("research", "t2")  # the same turn: the first call marked the type
    assert guard.try_start("job-gen", "t3")      # another type is free
    names.append("research")
    guard.started("research")
    assert guard.busy("research")                 # it runs now
    names.remove("research")
    assert not guard.busy("research")


def test_a_failed_call_and_an_old_mark_free_the_type():
    guard, _, now = make()
    guard.try_start("research", "t1")
    guard.failed("t1")
    assert not guard.busy("research")
    guard.try_start("fit-check", "t2")
    now[0] = START_LIMIT_S + 1
    assert not guard.busy("fit-check")
