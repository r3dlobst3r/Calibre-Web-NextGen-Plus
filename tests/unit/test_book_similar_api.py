"""The "Books like this" endpoint reads the Hardcover id off a visibility row."""
from collections import namedtuple
from types import SimpleNamespace

import pytest

Row = namedtuple("Row", "Books read_status is_archived")


def ident(kind, val):
    return SimpleNamespace(type=kind, val=val)


@pytest.mark.unit
def test_hardcover_id_is_read_from_the_books_entity_of_the_row():
    from cps.api.book_similar import hardcover_id

    book = SimpleNamespace(identifiers=[ident("isbn", "123"), ident("hardcover-id", "436502")])
    assert hardcover_id(Row(book, None, None)) == "436502"


@pytest.mark.unit
def test_hardcover_id_ignores_missing_and_non_numeric_values():
    from cps.api.book_similar import hardcover_id

    assert hardcover_id(Row(SimpleNamespace(identifiers=[]), None, None)) is None
    assert hardcover_id(Row(SimpleNamespace(identifiers=None), None, None)) is None
    junk = SimpleNamespace(identifiers=[ident("hardcover-id", "a-slug")])
    assert hardcover_id(Row(junk, None, None)) is None
