# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardcover "Books like this".

Hardcover's own /similar page is not part of its public GraphQL API, so this
approximates it: read the book's genre tags, search Hardcover for popular books
carrying those genres, and rank them by genre overlap and reader rating.
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
GENRE_SEARCH_QUERY = (
    "query SimilarBooks($query: String!, $per_page: Int!) { "
    "search(query: $query, query_type: \"Book\", fields: \"genres\", "
    "sort: \"users_count:desc\", per_page: $per_page) { results } }"
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


def parse_hits(results):
    parsed = _decode(results)
    hits = parsed.get("hits") if isinstance(parsed, dict) else None
    docs = []
    for hit in hits or []:
        doc = hit.get("document") if isinstance(hit, dict) else None
        if isinstance(doc, dict) and doc.get("id") and doc.get("title"):
            docs.append(doc)
    return docs


def rank(docs, genres, source_id):
    """Order candidate documents by genre overlap, then rating and popularity."""
    wanted = {g.lower() for g in genres}
    out = []
    for doc in docs:
        try:
            doc_id = int(doc["id"])
        except (TypeError, ValueError):
            continue
        if doc_id == source_id:
            continue
        theirs = {str(g).lower() for g in (doc.get("genres") or [])}
        overlap = len(wanted & theirs)
        if not overlap:
            continue
        jaccard = overlap / len(wanted | theirs)
        rating = float(doc.get("rating") or 0)
        popularity = math.log10(1 + float(doc.get("ratings_count") or doc.get("users_count") or 0))
        out.append((jaccard * 10 + rating + popularity, doc_id, doc))
    out.sort(key=lambda row: (-row[0], row[1]))
    return [doc for _, _, doc in out]


def _shape(doc):
    slug = doc.get("slug") or ""
    image = doc.get("image")
    return {
        "hardcover_id": int(doc["id"]),
        "title": doc["title"],
        "authors": [a for a in (doc.get("author_names") or []) if isinstance(a, str)],
        "cover": image.get("url") if isinstance(image, dict) else None,
        "rating": round(float(doc["rating"]), 2) if doc.get("rating") else None,
        "year": doc.get("release_year"),
        "url": SITE_URL + slug if slug else None,
    }


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
    if not genres:
        result = []
    else:
        found = _post(GENRE_SEARCH_QUERY,
                      {"query": " ".join(genres[:MAX_GENRES]), "per_page": POOL_SIZE}, tokens)
        if found is None:
            return []
        docs = parse_hits(((found.get("search") or {}).get("results")))
        result = [_shape(d) for d in rank(docs, genres[:MAX_GENRES], hardcover_id)[:RESULT_LIMIT]]

    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[hardcover_id] = (now, result)
    return result
