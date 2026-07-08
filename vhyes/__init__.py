import os

from flask import Flask

from .db import close_db, init_db
from .routes import bp


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.path.join(app.instance_path, "vhyes.sqlite3"),
        COVERS_DIR=os.path.join(app.instance_path, "covers"),
        SECRET_KEY=os.environ.get("VHYES_SECRET_KEY", "dev-vhyes"),
        TMDB_API_KEY=os.environ.get("TMDB_API_KEY", ""),
        BARCODE_LOOKUP_API_KEY=os.environ.get("BARCODE_LOOKUP_API_KEY", ""),
    )

    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["COVERS_DIR"], exist_ok=True)

    app.teardown_appcontext(close_db)
    app.register_blueprint(bp)

    with app.app_context():
        init_db()

    return app
