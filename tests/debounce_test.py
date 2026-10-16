from unli_photos.engine.debounce import Debouncer


def test_first_event_ready():
    d = Debouncer(1.5)
    assert d.ready("/p/x.jpg", now=100.0) is True


def test_rapid_repeat_skipped_until_window():
    d = Debouncer(1.5)
    assert d.ready("/p/x.jpg", now=100.0) is True
    assert d.ready("/p/x.jpg", now=100.5) is False
    assert d.ready("/p/x.jpg", now=101.4) is False
    assert d.ready("/p/x.jpg", now=101.6) is True  # 1.6s later: window passed


def test_paths_independent():
    d = Debouncer(1.5)
    assert d.ready("/a.jpg", now=100.0) is True
    assert d.ready("/b.jpg", now=100.0) is True


def test_prune_frees_stale_entries():
    d = Debouncer(10.0, max_entries=2)
    d.ready("/old.jpg", now=1.0)
    d.ready("/b.jpg", now=200.0)
    d.ready("/c.jpg", now=201.0)
    d.prune(now=202.0)  # 3 entries > max 2: /old.jpg (age 201 > 60) dropped
    assert d.ready("/old.jpg", now=202.0) is True  # freed: processed again
    assert d.ready("/b.jpg", now=202.0) is False   # still windowed


def test_prune_noop_under_limit():
    d = Debouncer(10.0, max_entries=5)
    d.ready("/a.jpg", now=1.0)
    d.prune(now=500.0)
    assert len(d) == 1  # nothing dropped: under max_entries
