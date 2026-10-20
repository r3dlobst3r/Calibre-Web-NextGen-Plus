"""Hardcover "Books like this": tag parsing, ranking, caching and failures."""
import json

import pytest

from cps import hardcover_similar as hs


def tags(*names):
    return [{"tag": n, "count": 1} for n in names]


def book(book_id, genres, moods=(), rating=4.0, users=100):
    return {"id": book_id, "title": "Book %d" % book_id, "slug": "b%d" % book_id,
            "rating": rating, "users_count": users, "release_year": 2001,
            "genres": tags(*genres), "moods": tags(*moods),
            "image": {"url": "http://c/%d" % book_id},
            "contributions": [{"author": {"name": "A"}}]}


@pytest.mark.unit
def test_parse_tags_orders_by_count_and_accepts_shapes():
    value = [{"tag": "Horror", "count": 5}, {"tag": "Fantasy", "count": 9}, {"tag": " "}]
    assert hs.parse_tags(value) == {"Fantasy": 9, "Horror": 5}
    assert hs.parse_tags(json.dumps(["A", "B"])) == {"A": 1, "B": 1}
    assert hs.parse_tags({"Genre": [{"tag": "X", "count": 1}]}) == {"X": 1}
    assert hs.parse_genres(value) == ["Fantasy", "Horror"]
    assert hs.parse_tags(None) == {}


@pytest.mark.unit
def test_rank_drops_self_unread_and_unrelated_and_prefers_shared_profile():
    genres = {"Fantasy": 1, "Horror": 1}
    books = [book(1, ["Fantasy", "Horror"]),                  # the source itself
             book(2, ["Cooking"]),                            # no shared genre
             book(3, ["Fantasy", "Horror"], users=5),         # too few readers
             book(4, ["Fantasy"], rating=4.5),                # half the genres
             book(5, ["Fantasy", "Horror"])]                  # all of them
    assert [b["id"] for b in hs.rank(books, genres, {}, 1)] == [5, 4]


@pytest.mark.unit
def test_rank_requires_half_the_genre_weight_not_a_single_stray_tag():
    genres = {"Forgiveness": 1, "Fantasy": 1}
    assert hs.rank([book(2, ["Forgiveness", "Romance"])], genres, {}, 1)
    wide = {"A": 1, "B": 1, "C": 1, "D": 1, "E": 1}
    # five-way ties need only a third of the profile, but not a single tag of five
    assert not hs.rank([book(2, ["A"])], wide, {}, 1)
    assert hs.rank([book(2, ["A", "B"])], wide, {}, 1)


@pytest.mark.unit
def test_moods_break_ties_between_equal_genre_matches():
    genres = {"Fantasy": 1}
    moods = {"dark": 3, "reflective": 2}
    books = [book(2, ["Fantasy"], moods=["funny"]), book(3, ["Fantasy"], moods=["dark", "reflective"])]
    assert [b["id"] for b in hs.rank(books, genres, moods, 1)] == [3, 2]


@pytest.mark.unit
def test_similar_books_queries_caches_and_shapes(monkeypatch):
    hs._cache.clear()
    calls = []

    def fake_post(query, variables, tokens):
        calls.append((query, variables))
        if "BookTags" in query:
            return {"books": [{"genres": [{"tag": "Horror", "count": 3}, {"tag": "Fantasy", "count": 2}],
                               "moods": tags("dark")}]}
        return {"books": [book(9, ["Horror", "Fantasy"], moods=["dark"], rating=4.256)]}

    monkeypatch.setattr(hs, "_post", fake_post)
    out = hs.similar_books(5, ["t"])
    assert out == [{"hardcover_id": 9, "title": "Book 9", "authors": ["A"], "cover": "http://c/9",
                    "rating": 4.26, "year": 2001, "url": "https://hardcover.app/books/b9"}]
    pool_query, variables = calls[1]
    assert variables["g0"] == {"Genre": [{"tag": "Horror"}]}
    assert variables["g1"] == {"Genre": [{"tag": "Fantasy"}]}
    assert "$g0: jsonb!" in pool_query and "$g1: jsonb!" in pool_query
    assert hs.similar_books(5, ["t"]) == out
    assert len(calls) == 2


@pytest.mark.unit
def test_book_with_no_genres_makes_no_pool_query(monkeypatch):
    hs._cache.clear()
    calls = []

    def fake_post(query, variables, tokens):
        calls.append(query)
        return {"books": [{"genres": [], "moods": []}]}

    monkeypatch.setattr(hs, "_post", fake_post)
    assert hs.similar_books(7, ["t"]) == []
    assert len(calls) == 1


@pytest.mark.unit
def test_failure_is_not_cached(monkeypatch):
    hs._cache.clear()
    monkeypatch.setattr(hs, "_post", lambda *a: None)
    assert hs.similar_books(6, ["t"]) == []
    assert 6 not in hs._cache
