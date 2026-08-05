from calc import average, rolling_total


def test_rolling_total():
    assert rolling_total([1, 2, 3]) == [1, 3, 6]


def test_rolling_total_empty():
    assert rolling_total([]) == []


def test_average():
    assert average([2, 4, 6]) == 4


def test_average_single_value():
    assert average([5]) == 5
