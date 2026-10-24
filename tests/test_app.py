import csv
import io
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from main import create_app, db


class InventoryTests(unittest.TestCase):
    def config(self, database="sqlite://"):
        return {
            "TESTING": True,
            "SECRET_KEY": "test-secret-only",
            "INVENTORY_PASSWORD": "test-password-only",
            "SQLALCHEMY_DATABASE_URI": database,
            "SESSION_COOKIE_SECURE": False,
        }

    def setUp(self):
        self.app = create_app(self.config())
        self.client = self.app.test_client()
        self.login()

    def tearDown(self):
        self.dispose()

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf"]
        response = self.client.post("/login", data={"csrf": token, "password": "test-password-only"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.headers = {"X-CSRF-Token": session["csrf"]}
        self.store_id = self.client.get("/api/stores").json["stores"][0]["id"]

    def url(self, path, store_id=None):
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}store_id={self.store_id if store_id is None else store_id}"

    def post(self, path, body, store_id=None):
        return self.client.post(self.url(path, store_id), json=body, headers=self.headers)

    def put(self, path, body, store_id=None):
        return self.client.put(self.url(path, store_id), json=body, headers=self.headers)

    def delete(self, path, store_id=None):
        return self.client.delete(self.url(path, store_id), headers=self.headers)

    def state(self, store_id=None):
        response = self.client.get(self.url("/api/inventory", store_id))
        self.assertEqual(response.status_code, 200)
        return response.json

    def department(self, name="Dairy"):
        response = self.post("/api/departments", {"name": name})
        self.assertEqual(response.status_code, 201)
        return response.json["id"]

    def product(self, name="Milk", size="1 ltr", department_id=None):
        if department_id is None:
            department_id = self.department()
        response = self.post("/api/products", {
            "name": name, "size": size, "department_id": department_id,
        })
        self.assertEqual(response.status_code, 201)
        return next(product for product in self.state()["products"] if product["id"] == response.json["id"])

    def save(self, rows):
        return self.put("/api/inventory", {"products": rows})

    def reset(self, department_id, products=None):
        if products is None:
            products = [product for product in self.state()["products"] if product["department_id"] == department_id]
        return self.post(f"/api/departments/{department_id}/reset", {"products": products})

    def test_authentication_csrf_logout_and_pages(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Last week", page.data)
        self.assertNotIn(b"Units sold", page.data)
        self.assertIn(b"Product size / quantity", page.data)
        self.assertNotIn(b"Description", page.data)
        self.assertNotIn(b"item-description", page.data)
        for path in ["/static/app.js", "/static/style.css"]:
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/health").json, {"status": "ok"})
        self.assertEqual(self.client.post("/api/departments", json={"name": "No CSRF"}).status_code, 400)
        self.assertEqual(self.client.post("/api/departments", json={"name": "Bad CSRF"}, headers={"X-CSRF-Token": "\u2603"}).status_code, 400)
        department_id = self.department()
        self.assertEqual(self.client.post(f"/api/departments/{department_id}/reset", json={"products": []}).status_code, 400)
        self.assertEqual(self.client.post("/logout", headers=self.headers).status_code, 302)
        self.assertEqual(self.client.get("/api/inventory").status_code, 401)
        self.assertEqual(self.client.get("/api/orders/export").status_code, 401)
        self.assertEqual(self.client.get("/api/stores").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_incorrect_password(self):
        self.client.post("/logout", headers=self.headers)
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf"]
        response = self.client.post("/login", data={"csrf": token, "password": "wrong"})
        self.assertIn(b"Incorrect password", response.data)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_public_installation_assets_and_icons(self):
        visitor = self.app.test_client()
        with visitor.get("/manifest.webmanifest") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/manifest+json")
            manifest = response.json
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["start_url"], "/?view=inventory")
        for icon in manifest["icons"]:
            with visitor.get(icon["src"]) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", response.data[16:24])
                self.assertEqual(f"{width}x{height}", icon["sizes"])
        with visitor.get("/static/icons/apple-touch-icon.png") as response:
            self.assertEqual(struct.unpack(">II", response.data[16:24]), (180, 180))
        with visitor.get("/sw.js") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
            self.assertEqual(response.mimetype, "application/javascript")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path in ["/static/offline.html", "/static/pwa.js", "/static/mobile.css"]:
            with visitor.get(path) as response:
                self.assertEqual(response.status_code, 200)
        with visitor.get("/login") as response:
            self.assertIn(b'rel="manifest"', response.data)
            self.assertIn(b'apple-touch-icon', response.data)
            self.assertIn(b'viewport-fit=cover', response.data)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(visitor.get("/api/stores").status_code, 401)

    def test_install_shortcut_context_survives_login(self):
        visitor = self.app.test_client()
        response = visitor.get("/?view=orders&store_id=1")
        self.assertEqual(response.status_code, 302)
        self.assertIn("view=orders", response.location)
        self.assertIn("store_id=1", response.location)
        visitor.get(response.location)
        with visitor.session_transaction() as session:
            token = session["csrf"]
        response = visitor.post(response.location, data={
            "password": "test-password-only", "csrf": token,
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("view=orders", response.location)
        self.assertIn("store_id=1", response.location)

    def test_product_information_sizes_and_duplicates(self):
        product = self.product(size="355ml")
        self.assertNotIn("description", product)
        self.assertEqual(product["size"], "355ml")
        self.product(size="473ml", department_id=product["department_id"])
        self.assertEqual(len(self.state()["products"]), 2)
        self.assertEqual(self.post("/api/products", {"name": "Milk", "size": "355ml", "department_id": product["department_id"]}).status_code, 409)
        self.assertFalse(product["out_of_stock"])
        self.assertEqual(product["order_quantity"], 0)
        self.assertNotIn("week", self.state())
        self.assertNotIn("closing", product)

    def test_edit_information_preserves_inventory(self):
        product = self.product()
        self.save([{**product, "out_of_stock": True, "order_quantity": 9}])
        response = self.put(f'/api/products/{product["id"]}', {
            "name": "Oat milk", "size": "473ml",
        })
        self.assertEqual(response.status_code, 200)
        updated = self.state()["products"][0]
        self.assertEqual((updated["name"], updated["size"]), ("Oat milk", "473ml"))
        self.assertTrue(updated["out_of_stock"])
        self.assertEqual(updated["order_quantity"], 9)

    def test_departments_and_deletion(self):
        product = self.product()
        self.assertEqual(self.post("/api/departments", {"name": "Dairy"}).status_code, 409)
        self.assertEqual(self.delete(f'/api/departments/{product["department_id"]}').status_code, 400)
        self.assertEqual(self.delete(f'/api/products/{product["id"]}').status_code, 200)
        self.assertEqual(self.state()["products"], [])
        self.assertEqual(self.delete(f'/api/departments/{product["department_id"]}').status_code, 200)
        self.assertEqual(self.state()["departments"], [])

    def test_reset_is_department_scoped_and_keeps_catalog(self):
        dairy = self.product()
        drinks = self.product(name="Cola", size="355ml", department_id=self.department("Drinks"))
        self.save([{**dairy, "out_of_stock": True, "order_quantity": 10},
                   {**drinks, "out_of_stock": True, "order_quantity": 5}])
        response = self.reset(dairy["department_id"])
        self.assertEqual(response.status_code, 200)
        products = {product["id"]: product for product in self.state()["products"]}
        self.assertFalse(products[dairy["id"]]["out_of_stock"])
        self.assertEqual(products[dairy["id"]]["order_quantity"], 0)
        self.assertEqual(products[dairy["id"]]["name"], "Milk")
        self.assertEqual(products[dairy["id"]]["size"], "1 ltr")
        self.assertTrue(products[drinks["id"]]["out_of_stock"])
        self.assertEqual(products[drinks["id"]]["order_quantity"], 5)
        self.assertEqual(len(self.state()["departments"]), 2)
        self.assertEqual(self.reset(dairy["department_id"], [drinks]).status_code, 409)

    def test_reset_conflicts_and_stale_edits(self):
        product = self.product()
        self.save([{**product, "out_of_stock": True}])
        self.assertEqual(self.reset(product["department_id"], [product]).status_code, 409)
        current = self.state()["products"][0]
        self.assertTrue(current["out_of_stock"])
        self.assertEqual(self.reset(product["department_id"]).status_code, 200)
        self.assertEqual(self.save([{**current, "order_quantity": 5}]).status_code, 409)
        current = self.state()["products"][0]
        self.assertEqual(self.reset(product["department_id"]).status_code, 200)
        self.assertEqual(self.save([{**current, "order_quantity": 1}]).status_code, 409)
        self.assertEqual(self.reset(99999).status_code, 404)

    def test_invalid_quantities_and_atomic_save(self):
        product = self.product()
        for bad in [-1, 1.5, True, "4", 1000001, None]:
            with self.subTest(quantity=bad):
                self.assertEqual(self.save([{**product, "order_quantity": bad}]).status_code, 400)
                self.assertEqual(self.state()["products"][0]["order_quantity"], 0)
        self.assertEqual(self.save([{**product, "order_quantity": 5}, {"id": 99999}]).status_code, 404)
        self.assertEqual(self.state()["products"][0]["order_quantity"], 0)
        self.assertEqual(self.save([product, product]).status_code, 400)
        self.assertEqual(self.save([{**product, "out_of_stock": "yes"}]).status_code, 400)

    def test_input_validation(self):
        self.assertEqual(self.post("/api/departments", {"name": "   "}).status_code, 400)
        self.assertEqual(self.post("/api/departments", {"name": "x" * 81}).status_code, 400)
        self.assertEqual(self.post("/api/products", {"name": "Milk", "size": "1 ltr", "department_id": 999}).status_code, 400)
        self.assertEqual(self.post("/api/departments", []).status_code, 400)
        department_id = self.department()
        for extra in [{"size": ""}, {"size": "x" * 81}, {"size": 9}]:
            self.assertEqual(self.post("/api/products", {"name": "Milk", "size": "1 ltr", "department_id": department_id, **extra}).status_code, 400)

    def test_shopping_list_export_includes_all_needed_items(self):
        product = self.product("=SUM(A1:A2)")
        second = self.product("Cola", "355ml", self.department("Drinks"))
        self.product("Not needed", "473ml", second["department_id"])
        self.assertEqual(self.client.get(self.url("/api/orders/export")).status_code, 400)
        self.assertEqual(self.save([{**product, "order_quantity": 7},
                                   {**second, "out_of_stock": True}]).status_code, 200)
        exported = self.client.get(self.url("/api/orders/export"))
        self.assertEqual(exported.status_code, 200)
        rows = list(csv.reader(io.StringIO(exported.text)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1], ["Main Store", "Dairy", "'=SUM(A1:A2)", "1 ltr", "No", "7"])
        self.assertEqual(rows[2], ["Main Store", "Drinks", "Cola", "355ml", "Yes", "Not specified"])
        self.assertNotIn("Description", rows[0])
        self.assertIn(f"shopping-list-store-{self.store_id}.csv", exported.headers["Content-Disposition"])
        filtered = self.client.get(self.url(f'/api/orders/export?department_id={second["department_id"]}'))
        filtered_rows = list(csv.reader(io.StringIO(filtered.text)))
        self.assertEqual(len(filtered_rows), 2)
        self.assertEqual(filtered_rows[1], rows[2])
        self.assertEqual(self.client.get(self.url("/api/orders/export?department_id=invalid")).status_code, 400)

    def test_concurrent_edits_are_rejected(self):
        product = self.product()
        self.assertEqual(self.save([{**product, "order_quantity": 10}]).status_code, 200)
        self.assertEqual(self.save([{**product, "order_quantity": 20}]).status_code, 409)
        self.assertEqual(self.state()["products"][0]["order_quantity"], 10)

    def test_stores_and_scoped_catalog_inventory_and_exports(self):
        first_store = self.store_id
        first = self.product()
        self.save([{**first, "order_quantity": 8}])
        response = self.post("/api/stores", {"name": "Second Store"})
        self.assertEqual(response.status_code, 201)
        second_store = response.json["id"]
        self.store_id = second_store
        self.assertEqual(self.state()["departments"], [])
        self.assertEqual(self.state()["products"], [])
        second = self.product()
        self.save([{**second, "out_of_stock": True, "order_quantity": 3}])
        self.assertEqual(len(self.state()["products"]), 1)
        self.assertEqual(self.state()["store"]["name"], "Second Store")
        self.assertEqual(self.state(first_store)["products"][0]["order_quantity"], 8)
        self.assertEqual(self.post("/api/products", {"name": "Wrong store", "size": "355ml",
                                                   "department_id": first["department_id"]}).status_code, 400)
        self.assertEqual(self.put(f'/api/products/{first["id"]}', {"name": "Wrong", "size": "1 ltr"}).status_code, 404)
        self.assertEqual(self.delete(f'/api/products/{first["id"]}').status_code, 404)
        self.assertEqual(self.delete(f'/api/departments/{first["department_id"]}').status_code, 404)
        self.assertEqual(self.save([{**first, "order_quantity": 99}]).status_code, 404)
        self.assertEqual(self.post(f'/api/departments/{first["department_id"]}/reset', {"products": [first]}).status_code, 404)
        self.assertEqual(self.client.get(self.url(f'/api/orders/export?department_id={first["department_id"]}')).status_code, 404)
        exported = self.client.get(self.url("/api/orders/export"))
        self.assertIn("Second Store", exported.text)
        self.assertNotIn("Main Store", exported.text)
        self.assertEqual(self.reset(second["department_id"]).status_code, 200)
        self.assertEqual(self.state()["products"][0]["order_quantity"], 0)
        self.assertEqual(self.state(first_store)["products"][0]["order_quantity"], 8)
        self.assertEqual(self.state(first_store)["products"][0]["name"], "Milk")

    def test_store_validation_and_rename(self):
        self.assertEqual(self.client.get("/api/inventory").status_code, 400)
        for value in ["", "invalid", "-1", "0", "999999999999999"]:
            self.assertEqual(self.client.get(f"/api/inventory?store_id={value}").status_code, 400)
        self.assertEqual(self.client.get("/api/inventory?store_id=99999").status_code, 404)
        self.assertEqual(self.post("/api/stores", {"name": "Main Store"}).status_code, 409)
        self.assertEqual(self.post("/api/stores", {"name": " "}).status_code, 400)
        self.assertEqual(self.post("/api/stores", {"name": "x" * 121}).status_code, 400)
        self.assertEqual(self.put(f"/api/stores/{self.store_id}", {"name": "Downtown"}).status_code, 200)
        self.assertEqual(self.state()["store"]["name"], "Downtown")

    def test_legacy_migration_and_reset_survive_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inventory.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE department (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE);
                CREATE TABLE product (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL,
                    department_id INTEGER NOT NULL REFERENCES department(id), UNIQUE(department_id, name));
                CREATE TABLE inventory (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES product(id),
                    week DATE NOT NULL, closing INTEGER, received INTEGER NOT NULL, order_quantity INTEGER NOT NULL,
                    selected BOOLEAN NOT NULL, revision INTEGER NOT NULL, UNIQUE(product_id, week));
                INSERT INTO department VALUES (1, 'Dairy');
                INSERT INTO product VALUES (1, 'Milk', 1);
                INSERT INTO inventory VALUES (1, 1, '2026-10-12', 5, 0, 3, 1, 1);
                INSERT INTO inventory VALUES (2, 1, '2026-10-19', 0, 0, 9, 1, 1);
            """)
            connection.close()
            self.dispose()
            config = self.config(f"sqlite:///{path}")
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            self.assertEqual(self.state()["store"]["name"], "Main Store")
            product = self.state()["products"][0]
            self.assertEqual(product["name"], "Milk")
            self.assertEqual(product["size"], "")
            self.assertTrue(product["out_of_stock"])
            self.assertEqual(product["order_quantity"], 9)
            self.product(size="355ml", department_id=1)
            self.product(size="473ml", department_id=1)
            self.assertEqual(self.reset(1).status_code, 200)
            self.dispose()
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            self.assertEqual(len(self.state()["products"]), 3)
            for current in self.state()["products"]:
                self.assertFalse(current["out_of_stock"])
                self.assertEqual(current["order_quantity"], 0)
            self.dispose()
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 2)
            connection.close()

    def test_current_workspace_migration_keeps_saved_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inventory.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE department (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE);
                CREATE TABLE product (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL,
                    department_id INTEGER NOT NULL REFERENCES department(id),
                    size VARCHAR(80) NOT NULL DEFAULT '', description VARCHAR(1000) NOT NULL DEFAULT '',
                    CONSTRAINT uq_product_department_name_size UNIQUE(department_id, name, size));
                CREATE TABLE stock_state (product_id INTEGER PRIMARY KEY REFERENCES product(id),
                    out_of_stock BOOLEAN NOT NULL, order_quantity INTEGER NOT NULL, revision INTEGER NOT NULL);
                INSERT INTO department VALUES (1, 'Dairy');
                INSERT INTO product VALUES (1, 'Milk', 1, '1 ltr', 'Archived detail');
                INSERT INTO stock_state VALUES (1, 1, 12, 5);
            """)
            connection.close()
            self.dispose()
            config = self.config(f"sqlite:///{path}")
            for _ in range(2):
                self.app = create_app(config)
                self.client = self.app.test_client()
                self.login()
                product = self.state()["products"][0]
                self.assertEqual(self.state()["store"]["name"], "Main Store")
                self.assertTrue(product["out_of_stock"])
                self.assertEqual(product["order_quantity"], 12)
                self.assertEqual(product["revision"], 5)
                self.assertNotIn("description", product)
                self.assertEqual(len(self.client.get("/api/stores").json["stores"]), 1)
                self.dispose()
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT description FROM product").fetchone()[0], "Archived detail")
            connection.close()


if __name__ == "__main__":
    unittest.main()
