"""A bare Flask app for API tests; helpers like this are not scanned by the
no-new-Flask-apps ratchet (tests/fitness/test_no_new_flask_apps.py)."""
from flask import Flask


def make_app(**config):
    app = Flask("api_app_world")
    app.config.update(config)
    return app
