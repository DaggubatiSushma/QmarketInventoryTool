import csv
import hmac
import io
import os
import secrets
from datetime import timedelta
from functools import wraps

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.middleware.proxy_fix import ProxyFix

from schema import upgrade_catalog, upgrade_stores


db = SQLAlchemy()


class Store(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey("store.id"), nullable=False)
    __table_args__ = (UniqueConstraint("store_id", "name", name="uq_department_store_name"),)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    size = db.Column(db.String(80), nullable=False, default="", server_default="")
    description = db.Column(db.String(1000), nullable=False, default="", server_default="")
    __table_args__ = (UniqueConstraint("department_id", "name", "size", name="uq_product_department_name_size"),)


class StockState(db.Model):
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), primary_key=True)
    out_of_stock = db.Column(db.Boolean, nullable=False, default=False)
    order_quantity = db.Column(db.Integer, nullable=False, default=0)
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
            Department.store_id == store.id).order_by(Department.name)).all()
        products = []
        query = (select(Product, Department.name, StockState)
                 .join(Department, Product.department_id == Department.id)
                 .join(StockState, StockState.product_id == Product.id)
                 .where(Department.store_id == store.id)
                 .order_by(Department.name, Product.name, Product.size))
        for product, department_name, current in db.session.execute(query):
            products.append({
                "id": product.id, "name": product.name,
                "department_id": product.department_id, "department": department_name,
                "size": product.size,
                "out_of_stock": current.out_of_stock,
                "order_quantity": current.order_quantity, "revision": current.revision,
            })
        return {
            "store": {"id": store.id, "name": store.name},
            "departments": [{"id": item.id, "name": item.name} for item in departments],
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
        store = Store(name=name_value(payload(), 120))
        db.session.add(store)
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
        db.session.add(department)
        db.session.commit()
        return jsonify(id=department.id), 201

    @app.delete("/api/departments/<int:department_id>")
    @protected
    def delete_department(department_id):
        department = scoped_department(department_id, selected_store().id)
        if db.session.scalar(select(Product.id).where(Product.department_id == department_id).limit(1)):
            raise ValueError("Remove this department's products before deleting it.")
        db.session.delete(department)
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
        db.session.add(product)
        db.session.flush()
        db.session.add(StockState(product_id=product.id))
        db.session.commit()
        return jsonify(id=product.id), 201

    @app.put("/api/products/<int:product_id>")
    @protected
    def edit_product(product_id):
        product = scoped_product(product_id, selected_store().id)
        body = payload()
        product.name = name_value(body, 120)
        product.size = product_size(body)
        db.session.commit()
        return jsonify(ok=True)

    @app.delete("/api/products/<int:product_id>")
    @protected
    def delete_product(product_id):
        product = scoped_product(product_id, selected_store().id)
        Inventory.query.filter_by(product_id=product_id).delete()
        StockState.query.filter_by(product_id=product_id).delete()
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
            current = inventory_row(product_id, item.get("revision"))
            current.order_quantity = order
            current.out_of_stock = out_of_stock
        db.session.commit()
        return jsonify(inventory_data(store))

    @app.post("/api/departments/<int:department_id>/reset")
    @protected
    def reset_department(department_id):
        store = selected_store()
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
        for product_id in sorted(product_ids):
            current = inventory_row(product_id, expected[product_id])
            current.out_of_stock = False
            current.order_quantity = 0
            # Invalidate edits opened before reset, including already-clear rows.
            current.revision += 1
        db.session.commit()
        return jsonify(inventory_data(store))

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

        def safe(text):
            # Neutralize spreadsheet formulas in user-entered names.
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text

        for product in products:
            writer.writerow([safe(store.name), safe(product["department"]), safe(product["name"]),
                             safe(product["size"]),
                             "Yes" if product["out_of_stock"] else "No",
                             product["order_quantity"] or "Not specified"])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="shopping-list-store-{store.id}.csv"'})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
