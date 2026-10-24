import csv
import hmac
import io
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, false, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.middleware.proxy_fix import ProxyFix

from schema import upgrade_catalog, upgrade_shopping, upgrade_stock_checks, upgrade_stores


db = SQLAlchemy()


class Store(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    catalog_revision = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    inventory_updated_at = db.Column(db.DateTime, nullable=True)
    departments_ordered = db.Column(db.Boolean, nullable=False, default=False, server_default=false())


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey("store.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    products_ordered = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    __table_args__ = (UniqueConstraint("store_id", "name", name="uq_department_store_name"),)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    size = db.Column(db.String(80), nullable=False, default="", server_default="")
    description = db.Column(db.String(1000), nullable=False, default="", server_default="")
    position = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    __table_args__ = (UniqueConstraint("department_id", "name", "size", name="uq_product_department_name_size"),)


class StockState(db.Model):
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), primary_key=True)
    out_of_stock = db.Column(db.Boolean, nullable=False, default=False)
    order_quantity = db.Column(db.Integer, nullable=False, default=0)
    checked = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    picked_up = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    revision = db.Column(db.Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": revision}


class InventoryUndo(db.Model):
    token = db.Column(db.String(64), primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("store.id"), nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    products = db.Column(db.JSON, nullable=False)
    consumed = db.Column(db.Boolean, nullable=False, default=False)
    invalidated = db.Column(db.Boolean, nullable=False, default=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": revision}


class Inventory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    week = db.Column(db.Date, nullable=False)
    closing = db.Column(db.Integer, nullable=True)
    received = db.Column(db.Integer, nullable=False, default=0)
    order_quantity = db.Column(db.Integer, nullable=False, default=0)
    selected = db.Column(db.Boolean, nullable=False, default=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (UniqueConstraint("product_id", "week"),)
    __mapper_args__ = {"version_id_col": revision}


def create_app(test_config=None):
    app = Flask(__name__)
    if os.environ.get("RENDER") and not os.environ.get("DATABASE_URL") and not test_config:
        raise RuntimeError("Set DATABASE_URL on Render to prevent loss of inventory on redeploy.")
    database_url = os.environ.get("DATABASE_URL", "sqlite:///inventory.db")
    if database_url.startswith(("postgres://", "postgresql://")):
        database_url = "postgresql+psycopg://" + database_url.split("://", 1)[1]
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        INVENTORY_PASSWORD=os.environ.get("INVENTORY_PASSWORD"),
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] or not app.config["INVENTORY_PASSWORD"]:
        raise RuntimeError("Set SECRET_KEY and INVENTORY_PASSWORD before starting the website.")
    if os.environ.get("RENDER"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        upgrade_stores(db.engine)
        upgrade_catalog(db.engine)
        upgrade_stock_checks(db.engine)
        upgrade_shopping(db.engine)
        # Copy each product's latest legacy record once; resets retain their state row.
        for product in db.session.scalars(select(Product)):
            if db.session.get(StockState, product.id) is None:
                latest = db.session.scalar(select(Inventory).where(
                    Inventory.product_id == product.id).order_by(Inventory.week.desc()).limit(1))
                db.session.add(StockState(
                    product_id=product.id,
                    out_of_stock=latest.closing == 0 if latest else False,
                    order_quantity=latest.order_quantity if latest and latest.selected else 0,
                ))
        db.session.commit()

    def protected(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify(error="Your session expired. Please sign in again."), 401
                return redirect(url_for("login", view=request.args.get("view"), store_id=request.args.get("store_id")))
            if request.method in ("POST", "PUT", "DELETE", "PATCH"):
                # Serialize mutations before reading snapshots, including newly demanded
                # contributors/stores. A no-op UPDATE locks on both SQLite and Postgres.
                db.session.execute(update(Store).where(
                    Store.id == select(func.min(Store.id)).scalar_subquery()
                ).values(catalog_revision=Store.catalog_revision),
                    execution_options={"synchronize_session": False})
            return handler(*args, **kwargs)
        return wrapper

    @app.before_request
    def csrf_protection():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not hmac.compare_digest(token.encode(), session["csrf"].encode()):
                return jsonify(error="Security token expired. Refresh the page and try again."), 400

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; worker-src 'self'; manifest-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.path != "/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid_input(error):
        db.session.rollback()
        return jsonify(error=str(error)), 400

    @app.errorhandler(IntegrityError)
    def duplicate_record(error):
        db.session.rollback()
        return jsonify(error="That name already exists, or the inventory changed. Refresh and try again."), 409

    @app.errorhandler(StaleDataError)
    def stale_inventory(error):
        db.session.rollback()
        return jsonify(error="Another team member changed this inventory. Reload the page before editing again."), 409

    @app.get("/health")
    def health():
        db.session.execute(select(1))
        return jsonify(status="ok")

    @app.get("/manifest.webmanifest")
    def manifest():
        response = app.send_static_file("manifest.webmanifest")
        response.mimetype = "application/manifest+json"
        return response

    @app.get("/sw.js")
    def service_worker():
        response = app.send_static_file("sw.js")
        response.mimetype = "application/javascript"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            if hmac.compare_digest(password.encode(), app.config["INVENTORY_PASSWORD"].encode()):
                session.clear()
                session["authenticated"] = True
                session["csrf"] = secrets.token_hex(32)
                session.permanent = True
                return redirect(url_for("index", view=request.args.get("view"), store_id=request.args.get("store_id")))
            error = "Incorrect password. Please try again."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/")
    @protected
    def index():
        return render_template("index.html")

    def payload():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def name_value(body, maximum):
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= maximum:
            raise ValueError(f"Enter a name between 1 and {maximum} characters.")
        return name.strip()

    def optional_text(body, field, maximum):
        value = body.get(field, "")
        if not isinstance(value, str) or len(value.strip()) > maximum:
            raise ValueError(f"{field.capitalize()} must be text of at most {maximum} characters.")
        return value.strip()

    def quantity(value):
        if type(value) is not int or not 0 <= value <= 1000000:
            raise ValueError("Quantities must be whole numbers from 0 to 1,000,000.")
        return value

    def product_size(body):
        size = optional_text(body, "size", 80)
        if not size:
            raise ValueError("Enter the product size or quantity, for example 355ml.")
        return size

    def selected_store():
        value = request.args.get("store_id", "")
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2147483647:
            raise ValueError("Select a valid store.")
        return db.get_or_404(Store, int(value))

    def scoped_department(department_id, store_id):
        return db.one_or_404(select(Department).where(
            Department.id == department_id, Department.store_id == store_id))

    def scoped_product(product_id, store_id):
        return db.one_or_404(select(Product).join(Department).where(
            Product.id == product_id, Department.store_id == store_id))

    def inventory_row(product_id, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Missing inventory revision. Refresh the page and try again.")
        row = db.session.get(StockState, product_id)
        if row is None or row.revision != expected_revision:
            raise StaleDataError()
        return row

    def inventory_data(store):
        departments = db.session.scalars(select(Department).where(
            Department.store_id == store.id).order_by(Department.position, Department.name)).all()
        products = []
        query = (select(Product, Department.name, StockState)
                 .join(Department, Product.department_id == Department.id)
                 .join(StockState, StockState.product_id == Product.id)
                 .where(Department.store_id == store.id)
                 .order_by(Department.position, Department.name, Product.position, Product.name, Product.size))
        for product, department_name, current in db.session.execute(query):
            products.append({
                "id": product.id, "name": product.name,
                "department_id": product.department_id, "department": department_name,
                "size": product.size,
                "out_of_stock": current.out_of_stock,
                "checked": current.checked,
                "picked_up": current.picked_up, "position": product.position,
                "order_quantity": current.order_quantity, "revision": current.revision,
            })
        return {
            "store": {"id": store.id, "name": store.name, "catalog_revision": store.catalog_revision},
            "last_updated_at": iso_timestamp(store.inventory_updated_at),
            "departments": [{"id": item.id, "name": item.name, "position": item.position} for item in departments],
            "products": products,
        }

    @app.get("/api/inventory")
    @protected
    def get_inventory():
        return jsonify(inventory_data(selected_store()))

    @app.get("/api/stores")
    @protected
    def get_stores():
        stores = db.session.scalars(select(Store).order_by(Store.name)).all()
        return jsonify(stores=[{"id": store.id, "name": store.name} for store in stores])

    @app.post("/api/stores")
    @protected
    def add_store():
        body = payload()
        source = None
        if "source_store_id" in body:
            source_id = body["source_store_id"]
            if type(source_id) is not int or not 1 <= source_id <= 2147483647:
                raise ValueError("Select a valid source store.")
            source = db.get_or_404(Store, source_id)
        store = Store(name=name_value(body, 120),
                      departments_ordered=source.departments_ordered if source else False)
        db.session.add(store)
        db.session.flush()
        if source:
            for department in db.session.scalars(select(Department).where(Department.store_id == source.id)).all():
                copied = Department(name=department.name, store_id=store.id, position=department.position,
                                    products_ordered=department.products_ordered)
                db.session.add(copied)
                db.session.flush()
                for product in db.session.scalars(select(Product).where(Product.department_id == department.id)).all():
                    cloned = Product(name=product.name, size=product.size, department_id=copied.id,
                                     position=product.position)
                    db.session.add(cloned)
                    db.session.flush()
                    db.session.add(StockState(product_id=cloned.id))
        db.session.commit()
        return jsonify(id=store.id), 201

    @app.put("/api/stores/<int:store_id>")
    @protected
    def rename_store(store_id):
        store = db.get_or_404(Store, store_id)
        store.name = name_value(payload(), 120)
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/departments")
    @protected
    def add_department():
        store = selected_store()
        department = Department(name=name_value(payload(), 80), store_id=store.id)
        if store.departments_ordered:
            department.position = (db.session.scalar(select(func.max(Department.position)).where(
                Department.store_id == store.id)) or 0) + 1
        store.catalog_revision += 1
        db.session.add(department)
        db.session.commit()
        return jsonify(id=department.id), 201

    @app.delete("/api/departments/<int:department_id>")
    @protected
    def delete_department(department_id):
        store = selected_store()
        department = scoped_department(department_id, store.id)
        if db.session.scalar(select(Product.id).where(Product.department_id == department_id).limit(1)):
            raise ValueError("Remove this department's products before deleting it.")
        db.session.delete(department)
        store.catalog_revision += 1
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/products")
    @protected
    def add_product():
        store = selected_store()
        body = payload()
        department_id = body.get("department_id")
        if type(department_id) is not int or db.session.scalar(select(Department.id).where(
                Department.id == department_id, Department.store_id == store.id)) is None:
            raise ValueError("Select a department in the current store.")
        product = Product(name=name_value(body, 120), department_id=department_id,
                          size=product_size(body))
        department = db.session.get(Department, department_id)
        if department.products_ordered:
            product.position = (db.session.scalar(select(func.max(Product.position)).where(
                Product.department_id == department_id)) or 0) + 1
        store.catalog_revision += 1
        db.session.add(product)
        db.session.flush()
        db.session.add(StockState(product_id=product.id))
        db.session.commit()
        return jsonify(id=product.id), 201

    @app.put("/api/products/<int:product_id>")
    @protected
    def edit_product(product_id):
        store = selected_store()
        product = scoped_product(product_id, store.id)
        body = payload()
        product.name = name_value(body, 120)
        product.size = product_size(body)
        store.catalog_revision += 1
        db.session.commit()
        return jsonify(ok=True)

    @app.delete("/api/products/<int:product_id>")
    @protected
    def delete_product(product_id):
        store = selected_store()
        product = scoped_product(product_id, store.id)
        store.catalog_revision += 1
        Inventory.query.filter_by(product_id=product_id).delete()
        StockState.query.filter_by(product_id=product_id).delete()
        # SQLite can reuse deleted product IDs; an old token must not restore a replacement.
        for action in db.session.scalars(select(InventoryUndo).where(
                InventoryUndo.store_id == store.id, InventoryUndo.consumed.is_(False))):
            if any(item["id"] == product_id for item in action.products):
                action.invalidated = True
        db.session.delete(product)
        db.session.commit()
        return jsonify(ok=True)

    @app.put("/api/inventory")
    @protected
    def save_inventory():
        store = selected_store()
        body = payload()
        rows = body.get("products")
        if not isinstance(rows, list):
            raise ValueError("Expected a list of products.")
        seen = set()
        changed = False
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("Invalid inventory row.")
            product_id = item.get("id")
            if type(product_id) is not int or product_id in seen:
                raise ValueError("Invalid or duplicate product.")
            seen.add(product_id)
            scoped_product(product_id, store.id)
            order = quantity(item.get("order_quantity"))
            out_of_stock = item.get("out_of_stock")
            if type(out_of_stock) is not bool:
                raise ValueError("Select whether the product is out of stock.")
            if "checked" in item and type(item["checked"]) is not bool:
                raise ValueError("Checked must be true or false.")
            current = inventory_row(product_id, item.get("revision"))
            if current.order_quantity != order or current.out_of_stock != out_of_stock:
                current.picked_up = False
                changed = True
            if "checked" in item and current.checked != item["checked"]:
                changed = True
            current.order_quantity = order
            current.out_of_stock = out_of_stock
            if "checked" in item:
                current.checked = item["checked"]
        if changed:
            store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(inventory_data(store))

    def snapshot_ids(values):
        if not isinstance(values, list) or any(
                type(value) is not int or not 1 <= value <= 2147483647 for value in values):
            raise ValueError("Expected a list of product or department IDs.")
        if len(values) != len(set(values)):
            raise ValueError("Duplicate IDs are not allowed.")
        return values

    @app.put("/api/catalog/order")
    @protected
    def reorder_catalog():
        store = selected_store()
        body = payload()
        revision = body.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Provide the catalog revision.")
        if revision != store.catalog_revision:
            raise StaleDataError()
        if "department_ids" in body:
            if "department_id" in body or "product_ids" in body:
                raise ValueError("Reorder departments or products, not both.")
            ids = snapshot_ids(body["department_ids"])
            objects = db.session.scalars(select(Department).where(Department.store_id == store.id)).all()
            store.departments_ordered = True
        else:
            department_id = body.get("department_id")
            if type(department_id) is not int or department_id < 1:
                raise ValueError("Select a valid department.")
            department = scoped_department(department_id, store.id)
            ids = snapshot_ids(body.get("product_ids"))
            objects = db.session.scalars(select(Product).where(Product.department_id == department_id)).all()
            department.products_ordered = True
        by_id = {item.id: item for item in objects}
        if set(ids) != set(by_id):
            raise StaleDataError()
        for position, item_id in enumerate(ids, 1):
            by_id[item_id].position = position
        store.catalog_revision += 1
        db.session.commit()
        return jsonify(inventory_data(store))

    @app.put("/api/orders/picked-up")
    @protected
    def pick_up_orders():
        store = selected_store()
        items = payload().get("products")
        if not isinstance(items, list) or not items:
            raise ValueError("Expected a nonempty list of products.")
        seen = set()
        changes = []
        for item in items:
            if (not isinstance(item, dict) or type(item.get("id")) is not int
                    or not 1 <= item["id"] <= 2147483647 or item["id"] in seen):
                raise ValueError("Invalid or duplicate product.")
            seen.add(item["id"])
            if type(item.get("picked_up")) is not bool:
                raise ValueError("Picked up must be true or false.")
            scoped_product(item["id"], store.id)
            row = inventory_row(item["id"], item.get("revision"))
            if not (row.out_of_stock or row.order_quantity > 0):
                raise StaleDataError()
            changes.append((row, item["picked_up"]))
        for row, picked_up in changes:
            row.picked_up = picked_up
            row.revision += 1
        store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(inventory_data(store))

    def department_inventory(department_id, store):
        scoped_department(department_id, store.id)
        rows = payload().get("products")
        if not isinstance(rows, list):
            raise ValueError("Expected the department's current inventory.")
        expected = {}
        for item in rows:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in expected:
                raise ValueError("Invalid or duplicate product.")
            expected[item["id"]] = item.get("revision")
        product_ids = set(db.session.scalars(select(Product.id).where(Product.department_id == department_id)))
        if product_ids != set(expected):
            raise StaleDataError()
        return [inventory_row(product_id, expected[product_id]) for product_id in sorted(product_ids)]

    def before_values(rows):
        return [{
            "id": row.product_id, "out_of_stock": row.out_of_stock,
            "order_quantity": row.order_quantity, "checked": row.checked,
            "picked_up": row.picked_up,
        } for row in rows]

    def utc_now():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def iso_timestamp(value):
        return value.isoformat() + "Z" if value is not None else None

    def remember_undo(store, rows, before):
        db.session.flush()
        now = utc_now()
        InventoryUndo.query.filter(
            InventoryUndo.expires_at < now - timedelta(hours=24)
        ).delete(synchronize_session=False)
        for item, row in zip(before, rows):
            item["revision"] = row.revision
        action = InventoryUndo(
            token=secrets.token_urlsafe(32), store_id=store.id,
            expires_at=now + timedelta(minutes=10), products=before,
        )
        db.session.add(action)
        return action.token

    @app.post("/api/departments/<int:department_id>/start-check")
    @protected
    def start_check(department_id):
        store = selected_store()
        rows = department_inventory(department_id, store)
        for current in rows:
            current.checked = False
            current.revision += 1
        if rows:
            store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(inventory_data(store))

    @app.post("/api/departments/<int:department_id>/reset")
    @protected
    def reset_department(department_id):
        store = selected_store()
        rows = department_inventory(department_id, store)
        before = before_values(rows)
        for current in rows:
            current.out_of_stock = False
            current.order_quantity = 0
            current.checked = False
            current.picked_up = False
            # Invalidate edits opened before reset, including already-clear rows.
            current.revision += 1
        token = remember_undo(store, rows, before)
        if rows:
            store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(**inventory_data(store), undo_token=token)

    @app.post("/api/products/<int:product_id>/remove-order")
    @protected
    def remove_order(product_id):
        store = selected_store()
        scoped_product(product_id, store.id)
        current = inventory_row(product_id, payload().get("revision"))
        before = before_values([current])
        current.out_of_stock = False
        current.order_quantity = 0
        current.picked_up = False
        current.revision += 1
        token = remember_undo(store, [current], before)
        store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(**inventory_data(store), undo_token=token)

    @app.post("/api/inventory/undo")
    @protected
    def undo_inventory():
        store = selected_store()
        token = payload().get("token")
        if not isinstance(token, str) or not token or len(token) > 64:
            raise ValueError("Provide a valid undo token.")
        action = db.session.get(InventoryUndo, token)
        if action is None:
            return jsonify(error="Undo token was not found."), 404
        if action.store_id != store.id:
            return jsonify(error="Undo token belongs to a different store."), 400
        if action.consumed:
            return jsonify(error="This undo token has already been used."), 409
        if action.expires_at <= utc_now():
            return jsonify(error="This undo token has expired."), 410
        if action.invalidated:
            raise StaleDataError()
        rows = []
        for item in action.products:
            if db.session.scalar(select(Product.id).join(Department).where(
                    Product.id == item["id"], Department.store_id == store.id)) is None:
                raise StaleDataError()
            rows.append(inventory_row(item["id"], item["revision"]))
        for item, current in zip(action.products, rows):
            current.out_of_stock = item["out_of_stock"]
            current.order_quantity = item["order_quantity"]
            current.checked = item["checked"]
            current.picked_up = item.get("picked_up", False)
            current.revision += 1
        # Versioning the token also makes empty department resets single-use.
        action.consumed = True
        if rows:
            store.inventory_updated_at = utc_now()
        db.session.commit()
        return jsonify(inventory_data(store))

    def demanded_products():
        query = (select(Product, StockState, Store)
                 .join(StockState, StockState.product_id == Product.id)
                 .join(Department, Product.department_id == Department.id)
                 .join(Store, Department.store_id == Store.id)
                 .where((StockState.out_of_stock.is_(True)) | (StockState.order_quantity > 0))
                 .order_by(Product.name, Product.size, Store.name, Product.id))
        department_name = request.args.get("department_name", "")
        if department_name:
            query = query.where(Department.name == department_name)
        return db.session.execute(query).all()

    def combined_orders_data():
        grouped = {}
        for product, stock, store in demanded_products():
            key = (product.name, product.size)
            if key not in grouped:
                grouped[key] = {
                    "name": product.name, "size": product.size,
                    "order_quantity": 0, "unspecified_count": 0, "stores": [],
                    "store_quantities": {}, "items": [], "picked_up": True,
                }
            item = grouped[key]
            item["order_quantity"] += stock.order_quantity
            if stock.order_quantity == 0:
                item["unspecified_count"] += 1
            if store.name not in item["stores"]:
                item["stores"].append(store.name)
            breakdown = item["store_quantities"].setdefault(store.id, {
                "store_id": store.id, "store_name": store.name, "order_quantity": 0,
                "unspecified_count": 0, "picked_up": True,
            })
            breakdown["order_quantity"] += stock.order_quantity
            breakdown["unspecified_count"] += int(stock.order_quantity == 0)
            breakdown["picked_up"] = breakdown["picked_up"] and stock.picked_up
            item["items"].append({"id": product.id, "revision": stock.revision, "picked_up": stock.picked_up})
            item["picked_up"] = item["picked_up"] and stock.picked_up
        for item in grouped.values():
            item["store_quantities"] = list(item["store_quantities"].values())
        return list(grouped.values())

    def combined_data():
        departments = list(db.session.scalars(select(Department.name).distinct().order_by(Department.name)))
        latest = db.session.scalar(select(func.max(Store.inventory_updated_at)))
        return {"products": combined_orders_data(), "departments": departments,
                "last_updated_at": iso_timestamp(latest)}

    @app.put("/api/orders/combined/picked-up")
    @protected
    def pick_up_combined_orders():
        body = payload()
        name, size = body.get("name"), body.get("size")
        if not isinstance(name, str) or not 1 <= len(name) <= 120 or not isinstance(size, str) or len(size) > 80:
            raise ValueError("Provide the exact product name and size.")
        if type(body.get("picked_up")) is not bool:
            raise ValueError("Picked up must be true or false.")
        items = body.get("products")
        if not isinstance(items, list) or not items:
            raise ValueError("Expected the complete nonempty product snapshot.")
        expected = {}
        for item in items:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in expected:
                raise ValueError("Invalid or duplicate product.")
            revision = item.get("revision")
            if type(revision) is not int or revision < 1:
                raise ValueError("Provide valid inventory revisions.")
            expected[item["id"]] = revision
        contributors = [(product, stock, store) for product, stock, store in demanded_products()
                        if product.name == name and product.size == size]
        if not contributors or set(expected) != {product.id for product, _, _ in contributors}:
            raise StaleDataError()
        if any(stock.revision != expected[product.id] for product, stock, _ in contributors):
            raise StaleDataError()
        now = utc_now()
        for _, stock, store in contributors:
            stock.picked_up = body["picked_up"]
            stock.revision += 1
            store.inventory_updated_at = now
        db.session.commit()
        return jsonify(combined_data())

    def safe_csv(text):
        # Neutralize spreadsheet formulas in user-entered names.
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text

    @app.get("/api/orders/combined")
    @protected
    def combined_orders():
        return jsonify(combined_data())

    @app.get("/api/orders/combined/export")
    @protected
    def export_combined_orders():
        products = combined_orders_data()
        if not products:
            raise ValueError("Your combined shopping list is empty.")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Product", "Size", "Quantity needed", "Unspecified quantities", "Stores", "Store quantities"])
        for product in products:
            writer.writerow([
                safe_csv(product["name"]), safe_csv(product["size"]),
                product["order_quantity"] or "Not specified", product["unspecified_count"],
                safe_csv(", ".join(product["stores"])),
                safe_csv("; ".join(
                    f'{item["store_name"]}: {item["order_quantity"]}'
                    + (f' + {item["unspecified_count"]} unspecified' if item["unspecified_count"] else "")
                    for item in product["store_quantities"])),
            ])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="shopping-list-all-stores.csv"'})

    @app.get("/api/orders/export")
    @protected
    def export_orders():
        store = selected_store()
        department_filter = request.args.get("department_id", "")
        department_id = None
        if department_filter:
            if not department_filter.isascii() or not department_filter.isdecimal() or not 1 <= int(department_filter) <= 2147483647:
                raise ValueError("Select a valid department.")
            department_id = scoped_department(int(department_filter), store.id).id
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Store", "Department", "Product", "Size", "Out of stock", "Quantity needed"])
        products = [product for product in inventory_data(store)["products"]
                    if (product["out_of_stock"] or product["order_quantity"] > 0)
                    and (department_id is None or product["department_id"] == department_id)]
        if not products:
            raise ValueError("Your shopping list is empty.")

        for product in products:
            writer.writerow([safe_csv(store.name), safe_csv(product["department"]), safe_csv(product["name"]),
                             safe_csv(product["size"]),
                             "Yes" if product["out_of_stock"] else "No",
                             product["order_quantity"] or "Not specified"])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="shopping-list-store-{store.id}.csv"'})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
