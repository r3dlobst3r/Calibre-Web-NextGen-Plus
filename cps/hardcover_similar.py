# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardcover "Books like this".

Hardcover's own /similar page is not part of its public GraphQL API, so this
approximates it: read the book's genre tags, ask Hardcover for its most-read books
that carry the top genres, and rank them by genre overlap and reader rating.
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
MAX_GENRES = 3
POOL_SIZE = 50
RESULT_LIMIT = 24

BOOK_GENRES_QUERY = (
    "query BookGenres($id: Int!) { "
    "books(where: {id: {_eq: $id}}) { id cached_tags(path: \"Genre\") } }"
)
POOL_QUERY = (
    "query SimilarBooks($a: jsonb!, $b: jsonb!, $n: Int!) { "
    "books(where: {_and: [{cached_tags: {_contains: $a}}, {cached_tags: {_contains: $b}}]}, "
    "order_by: {users_count: desc}, limit: $n) { "
    "id slug title release_year rating ratings_count users_count "
    "cached_tags(path: \"Genre\") image { url } contributions(limit: 3) { author { name } } } }"
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


def parse_genres(cached_tags):
    """Genre names from a cached_tags value, most-used first.

    Hardcover returns this scalar as a list of {"tag", "count"} objects, a
    list of strings, or the same JSON-encoded; accept all of them.
    """
    value = _decode(cached_tags)
    if isinstance(value, dict):
        value = value.get("Genre") or value.get("genre") or []
    if not isinstance(value, list):
        return []
    scored = []
    for pos, item in enumerate(value):
        if isinstance(item, dict):
            name = item.get("tag") or item.get("name")
            count = item.get("count") or 0
        else:
            name, count = item, 0
        if isinstance(name, str) and name.strip():
            scored.append((-count if isinstance(count, (int, float)) else 0, pos, name.strip()))
    return [name for _, _, name in sorted(scored)]


def rank(books, genres, source_id):
    """Order candidate books by genre overlap, then rating and popularity."""
    wanted = {g.lower() for g in genres}
    out = []
    for book in books:
        try:
            book_id = int(book["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if book_id == source_id or not book.get("title"):
            continue
        theirs = {g.lower() for g in parse_genres(book.get("cached_tags"))}
        overlap = len(wanted & theirs)
        if not overlap:
            continue
        jaccard = overlap / len(wanted | theirs)
        rating = float(book.get("rating") or 0)
        popularity = math.log10(1 + float(book.get("users_count") or 0))
        out.append((jaccard * 10 + rating + popularity, book_id, book))
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


def _pool(genres, tokens):
    """Most-read books carrying the top two genres, topped up with the top genre
    alone when the pair is rare. ``None`` on a failed request."""
    top = genres[:MAX_GENRES]
    first = {"Genre": [{"tag": top[0]}]}
    second = {"Genre": [{"tag": top[1]}]} if len(top) > 1 else first
    data = _post(POOL_QUERY, {"a": first, "b": second, "n": POOL_SIZE}, tokens)
    if data is None:
        return None
    books = list(data.get("books") or [])
    if len(books) < RESULT_LIMIT and second is not first:
        more = _post(POOL_QUERY, {"a": first, "b": first, "n": POOL_SIZE}, tokens)
        if more is not None:
            seen = {b.get("id") for b in books}
            books += [b for b in more.get("books") or [] if b.get("id") not in seen]
    return books


def similar_books(hardcover_id, tokens):
    """Similar Hardcover books for ``hardcover_id``; ``[]`` when none or on a
    transient failure. Results are cached per Hardcover id for a day."""
    now = time.monotonic()
    cached = _cache.get(hardcover_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    data = _post(BOOK_GENRES_QUERY, {"id": hardcover_id}, tokens)
    if data is None:
        return []
    books = data.get("books") or []
    genres = parse_genres(books[0].get("cached_tags")) if books else []
    result = []
    if genres:
        pool = _pool(genres, tokens)
        if pool is None:
            return []
        result = [_shape(b) for b in rank(pool, genres, hardcover_id)[:RESULT_LIMIT]]

    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[hardcover_id] = (now, result)
    return result
