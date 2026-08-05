from orders import order_label
from users import display_user


def test_display_user():
    assert display_user("  alice   SMITH ") == "Alice Smith"


def test_order_label():
    assert order_label(" bob  jones ", "book") == "Bob Jones: book"
