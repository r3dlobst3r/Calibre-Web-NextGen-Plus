# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardcover "Books like this" for the book detail page."""
from flask import jsonify

from . import api_v1
from .. import calibre_db, config, db, hardcover_similar, logger
from ..cw_login import current_user
from ..usermanagement import login_required_if_no_ano

log = logger.create()


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Cookie, Authorization'
    return response


def _tokens():
    tokens = []
    for raw in (getattr(current_user, 'hardcover_token', None),
                config.resolved_hardcover_token()):
        cleaned = (raw or '').replace('Bearer ', '').strip()
        if cleaned and cleaned not in tokens:
            tokens.append(cleaned)
    return tokens


def _visible_book(book_id):
    return calibre_db.get_book_read_archived(
        book_id, config.config_read_column,
        allow_show_archived=True, allow_show_hidden=True,
        allow_show_global=bool(current_user.role_browse_global()),
        allow_public_shelf_books=True,
    )


def _library_matches(hardcover_ids):
    """Map Hardcover id -> local book id for visible books already owned."""
    rows = calibre_db.session.query(db.Identifiers.val, db.Identifiers.book).filter(
        db.Identifiers.type == 'hardcover-id',
        db.Identifiers.val.in_([str(i) for i in hardcover_ids])).all()
    owned = {}
    for val, book_id in rows:
        if val.isdigit() and int(val) not in owned and _visible_book(book_id):
            owned[int(val)] = book_id
    return owned


@api_v1.route('/books/<int:book_id>/similar')
@login_required_if_no_ano
def book_similar(book_id):
    """``available`` is false when there is nothing to ask Hardcover (no token
    or no hardcover-id on the book), so the page can hide the section."""
    book = _visible_book(book_id)
    if not book:
        return _response({'error': {'code': 'not_found', 'message': 'Book not found'}}, 404)
    hc_id = next((i.val for i in (book.identifiers or [])
                  if i.type == 'hardcover-id' and str(i.val).isdigit()), None)
    tokens = _tokens()
    if not hc_id or not tokens:
        return _response({'available': False, 'items': []})
    try:
        items = hardcover_similar.similar_books(int(hc_id), tokens)
    except hardcover_similar.HardcoverRejected:
        return _response({'available': False, 'items': []})
    owned = _library_matches([i['hardcover_id'] for i in items])
    for item in items:
        item['library_book_id'] = owned.get(item['hardcover_id'])
    return _response({'available': True, 'items': items})
