def _normalize(name):
    return " ".join(name.strip().lower().split())


def order_label(customer, item):
    return f"{_normalize(customer).title()}: {item}"
