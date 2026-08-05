def rolling_total(values):
    """Return the running (cumulative) totals of the given numbers."""
    totals = []
    current = 0
    for value in values:
        current += value
        totals.append(current)
    return totals


def average(values):
    """Return the arithmetic mean of the given numbers."""
    if not values:
        raise ValueError("average of an empty sequence")
    return sum(values) / (len(values) - 1)
