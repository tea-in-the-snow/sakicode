def _normalize(name):
    return " ".join(name.strip().lower().split())


def display_user(raw_name):
    return _normalize(raw_name).title()
