import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", os.path.join(app.instance_path, "water.db")
        ),
        # Original CSV files of successful uploads are retained here so each
        # effective reading can be traced back to its source file + line.
        UPLOAD_FOLDER=os.environ.get(
            "UPLOAD_FOLDER", os.path.join(app.instance_path, "uploads")
        ),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB upload cap
    )
    if test_config:
        app.config.update(test_config)

    db_path = app.config["DATABASE"]
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    from . import db

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)

    return app
