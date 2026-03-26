from kasane.search import rrf_score, time_decay


def test_rrf_score():
    assert rrf_score(1) == 1.0 / 61
    assert rrf_score(0) == 1.0 / 60
    assert rrf_score(10) == 1.0 / 70


def test_time_decay():
    decay_30 = time_decay(30)
    assert 0.49 < decay_30 < 0.51
    decay_60 = time_decay(60)
    assert 0.24 < decay_60 < 0.26
    decay_0 = time_decay(0)
    assert decay_0 == 1.0
