# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardcover "Books like this".

Hardcover's own /similar page is not part of its public GraphQL API, so this
approximates it: read the book's genre and mood tags, ask Hardcover for its
most-read books carrying any of the genres, and rank them by how much of the
book's genre and mood profile they share, then by rating and popularity.
"""
import json
import math
import time

import requests

from . import constants, logger

log = logger.create()

GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
SITE_URL = "https://hardcover.app/books/"
CACHE_TTL = 24 * 3600
CACHE_MAX = 512
MAX_GENRES = 5
MAX_MOODS = 5
POOL_SIZE = 150
RESULT_LIMIT = 24
MIN_READERS = 20
GENRE_WEIGHT = 6.0
MOOD_WEIGHT = 3.0

BOOK_TAGS_QUERY = (
    "query BookTags($id: Int!) { "
    "books(where: {id: {_eq: $id}}) { id genres: cached_tags(path: \"Genre\") "
    "moods: cached_tags(path: \"Mood\") } }"
)

_cache = {}


class HardcoverRejected(Exception):
    """Every configured token was refused by Hardcover."""


def _post(query, variables, tokens):
    last = None
    for token in tokens:
        try:
            resp = requests.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json",
                         "User-Agent": constants.USER_AGENT,
                         "Authorization": "Bearer %s" % token},
                timeout=15,
            )
            if resp.status_code == 401:
                last = HardcoverRejected("Hardcover rejected the configured token")
                continue
            resp.raise_for_status()
            body = resp.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            log.warning("Hardcover similar-books request failed: %s", exc)
            return None
        if body.get("errors"):
            log.warning("Hardcover similar-books GraphQL errors: %s", body["errors"])
            return None
        return body.get("data") or {}
    if last:
        raise last
    return None


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def parse_tags(cached_tags):
    """``{tag name: count}`` from a cached_tags value, most-used first.

    Hardcover returns this scalar as a list of {"tag", "count"} objects, a
    list of strings, or the same JSON-encoded; accept all of them. A bare
    string counts once.
    """
    value = _decode(cached_tags)
    if isinstance(value, dict):
        value = value.get("Genre") or value.get("genre") or []
    if not isinstance(value, list):
        return {}
    scored = []
    for pos, item in enumerate(value):
        if isinstance(item, dict):
            name = item.get("tag") or item.get("name")
            count = item.get("count")
        else:
            name, count = item, 1
        if isinstance(name, str) and name.strip():
            count = count if isinstance(count, (int, float)) and count > 0 else 1
            scored.append((-count, pos, name.strip(), count))
    return {name: count for _, _, name, count in sorted(scored)}


def parse_genres(cached_tags):
    """Genre names from a cached_tags value, most-used first."""
    return list(parse_tags(cached_tags))


def _overlap(source, theirs):
    """Share of the source's tag weight that ``theirs`` also carries, 0..1."""
    total = sum(source.values())
    if not total:
        return 0.0
    mine = {name.lower() for name in theirs}
    return sum(w for name, w in source.items() if name.lower() in mine) / total


def rank(books, genres, moods, source_id):
    """Order candidate books by shared genre and mood profile, then rating and
    popularity. ``genres`` and ``moods`` are ``{tag: count}`` for the source.

    A candidate must carry at least half the source's genre weight (a third
    when the source has four or more genres, which Hardcover often lists with
    equal counts) and have a minimum readership, so one stray tag or an
    unread listing cannot qualify.
    """
    need = 1 / 3 if len(genres) >= 4 else 0.5
    out = []
    for book in books:
        try:
            book_id = int(book["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if book_id == source_id or not book.get("title"):
            continue
        readers = float(book.get("users_count") or 0)
        if readers < MIN_READERS:
            continue
        genre_share = _overlap(genres, parse_tags(book.get("genres")))
        if genre_share < need:
            continue
        mood_share = _overlap(moods, parse_tags(book.get("moods")))
        rating = float(book.get("rating") or 0)
        score = (GENRE_WEIGHT * genre_share + MOOD_WEIGHT * mood_share
                 + rating + math.log10(1 + readers))
        out.append((score, book_id, book))
    out.sort(key=lambda row: (-row[0], row[1]))
    return [book for _, _, book in out]


def _shape(book):
    slug = book.get("slug") or ""
    image = book.get("image")
    authors = []
    for contribution in book.get("contributions") or []:
        name = ((contribution or {}).get("author") or {}).get("name")
        if isinstance(name, str) and name:
            authors.append(name)
    return {
        "hardcover_id": int(book["id"]),
        "title": book["title"],
        "authors": authors,
        "cover": image.get("url") if isinstance(image, dict) else None,
        "rating": round(float(book["rating"]), 2) if book.get("rating") else None,
        "year": book.get("release_year"),
        "url": SITE_URL + slug if slug else None,
    }


def _pool_query(count):
    params = ", ".join("$g%d: jsonb!" % i for i in range(count))
    clauses = ", ".join("{cached_tags: {_contains: $g%d}}" % i for i in range(count))
    return (
        "query SimilarBooks(%s, $n: Int!, $min: Int!) { "
        "books(where: {_and: [{users_count: {_gte: $min}}, {_or: [%s]}]}, "
        "order_by: {users_count: desc}, limit: $n) { "
        "id slug title release_year rating ratings_count users_count "
        "genres: cached_tags(path: \"Genre\") moods: cached_tags(path: \"Mood\") "
        "image { url } contributions(limit: 3) { author { name } } } }" % (params, clauses)
    )


def _pool(genres, tokens):
    """Most-read books carrying any of the source's top genres. ``None`` on a
    failed request."""
    names = list(genres)[:MAX_GENRES]
    variables = {"g%d" % i: {"Genre": [{"tag": name}]} for i, name in enumerate(names)}
    variables.update(n=POOL_SIZE, min=MIN_READERS)
    data = _post(_pool_query(len(names)), variables, tokens)
    return None if data is None else list(data.get("books") or [])


def similar_books(hardcover_id, tokens):
    """Similar Hardcover books for ``hardcover_id``; ``[]`` when none or on a
    transient failure. Results are cached per Hardcover id for a day."""
    now = time.monotonic()
    cached = _cache.get(hardcover_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    data = _post(BOOK_TAGS_QUERY, {"id": hardcover_id}, tokens)
    if data is None:
        return []
    books = data.get("books") or []
    genres = parse_tags(books[0].get("genres")) if books else {}
    moods = dict(list(parse_tags(books[0].get("moods")).items())[:MAX_MOODS]) if books else {}
    result = []
    if genres:
        pool = _pool(genres, tokens)
        if pool is None:
            return []
        result = [_shape(b) for b in rank(pool, genres, moods, hardcover_id)[:RESULT_LIMIT]]

    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[hardcover_id] = (now, result)
    return result
