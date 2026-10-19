"""Hardcover "Books like this": genre parsing, ranking, caching and failures."""
import json

import pytest

from cps import hardcover_similar as hs


def book(book_id, genres, rating=4.0, users=100, title=None):
    return {"id": book_id, "title": title or "Book %d" % book_id, "slug": "b%d" % book_id,
            "rating": rating, "users_count": users, "release_year": 2001,
            "cached_tags": [{"tag": g, "count": 1} for g in genres],
            "image": {"url": "http://c/%d" % book_id},
            "contributions": [{"author": {"name": "A"}}]}


@pytest.mark.unit
def test_parse_genres_orders_by_count_and_accepts_shapes():
    tags = [{"tag": "Horror", "count": 5}, {"tag": "Fantasy", "count": 9}, {"tag": " "}]
    assert hs.parse_genres(tags) == ["Fantasy", "Horror"]
    assert hs.parse_genres(json.dumps(["A", "B"])) == ["A", "B"]
    assert hs.parse_genres({"Genre": [{"tag": "X", "count": 1}]}) == ["X"]
    assert hs.parse_genres(None) == []


@pytest.mark.unit
def test_rank_drops_self_and_unrelated_and_prefers_overlap():
    books = [book(1, ["Fantasy"], 5), book(2, ["Cooking"], 5),
             book(3, ["Fantasy", "Romance"]), book(4, ["Fantasy", "Horror"])]
    assert [b["id"] for b in hs.rank(books, ["Fantasy", "Horror"], 1)] == [4, 3]


@pytest.mark.unit
def test_similar_books_queries_caches_and_shapes(monkeypatch):
    hs._cache.clear()
    calls = []

    def fake_post(query, variables, tokens):
        calls.append((query, variables))
        if "BookGenres" in query:
            return {"books": [{"cached_tags": [{"tag": "Horror", "count": 3},
                                               {"tag": "Fantasy", "count": 2}]}]}
        return {"books": [book(9, ["Horror", "Fantasy"], rating=4.256)]}

    monkeypatch.setattr(hs, "_post", fake_post)
    out = hs.similar_books(5, ["t"])
    assert out == [{"hardcover_id": 9, "title": "Book 9", "authors": ["A"], "cover": "http://c/9",
                    "rating": 4.26, "year": 2001, "url": "https://hardcover.app/books/b9"}]
    assert calls[1][1]["a"] == {"Genre": [{"tag": "Horror"}]}
    assert calls[1][1]["b"] == {"Genre": [{"tag": "Fantasy"}]}
    assert hs.similar_books(5, ["t"]) == out
    # a thin pair triggers one top-up query on the first genre alone, then no more
    assert len(calls) == 3


@pytest.mark.unit
def test_single_genre_book_is_one_pool_query(monkeypatch):
    hs._cache.clear()
    calls = []

    def fake_post(query, variables, tokens):
        calls.append(query)
        if "BookGenres" in query:
            return {"books": [{"cached_tags": [{"tag": "Horror", "count": 3}]}]}
        return {"books": [book(9, ["Horror"])]}

    monkeypatch.setattr(hs, "_post", fake_post)
    assert [b["hardcover_id"] for b in hs.similar_books(7, ["t"])] == [9]
    assert len(calls) == 2


@pytest.mark.unit
def test_failure_is_not_cached(monkeypatch):
    hs._cache.clear()
    monkeypatch.setattr(hs, "_post", lambda *a: None)
    assert hs.similar_books(6, ["t"]) == []
    assert 6 not in hs._cache
