"""Hardcover "Books like this": genre parsing, ranking, caching and failures."""
import json

import pytest

from cps import hardcover_similar as hs


@pytest.mark.unit
def test_parse_genres_orders_by_count_and_accepts_shapes():
    tags = [{"tag": "Horror", "count": 5}, {"tag": "Fantasy", "count": 9}, {"tag": " "}]
    assert hs.parse_genres(tags) == ["Fantasy", "Horror"]
    assert hs.parse_genres(json.dumps(["A", "B"])) == ["A", "B"]
    assert hs.parse_genres({"Genre": [{"tag": "X", "count": 1}]}) == ["X"]
    assert hs.parse_genres(None) == []


@pytest.mark.unit
def test_rank_drops_self_and_unrelated_and_prefers_overlap():
    docs = [
        {"id": 1, "title": "self", "genres": ["Fantasy"], "rating": 5},
        {"id": 2, "title": "none", "genres": ["Cooking"], "rating": 5},
        {"id": 3, "title": "half", "genres": ["Fantasy", "Romance"], "rating": 4},
        {"id": 4, "title": "full", "genres": ["Fantasy", "Horror"], "rating": 4},
    ]
    assert [d["id"] for d in hs.rank(docs, ["Fantasy", "Horror"], 1)] == [4, 3]


@pytest.mark.unit
def test_similar_books_queries_caches_and_shapes(monkeypatch):
    hs._cache.clear()
    calls = []

    def fake_post(query, variables, tokens):
        calls.append(query)
        if "BookGenres" in query:
            return {"books": [{"cached_tags": [{"tag": "Horror", "count": 3}]}]}
        hits = [{"document": {"id": "9", "title": "Other", "slug": "other",
                              "genres": ["Horror"], "rating": 4.256,
                              "author_names": ["A"], "image": {"url": "http://c"}}}]
        return {"search": {"results": json.dumps({"hits": hits})}}

    monkeypatch.setattr(hs, "_post", fake_post)
    out = hs.similar_books(5, ["t"])
    assert out == [{"hardcover_id": 9, "title": "Other", "authors": ["A"], "cover": "http://c",
                    "rating": 4.26, "year": None, "url": "https://hardcover.app/books/other"}]
    assert hs.similar_books(5, ["t"]) == out
    assert len(calls) == 2


@pytest.mark.unit
def test_failure_is_not_cached(monkeypatch):
    hs._cache.clear()
    monkeypatch.setattr(hs, "_post", lambda *a: None)
    assert hs.similar_books(6, ["t"]) == []
    assert 6 not in hs._cache
