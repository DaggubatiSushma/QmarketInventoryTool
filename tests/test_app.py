import csv
import io
import sqlite3
import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import event

from main import InventoryUndo, Product, StockState, create_app, db


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

    def undo(self, token, store_id=None):
        return self.post("/api/inventory/undo", {"token": token}, store_id)

    def test_checked_validation_preservation_and_shared_state(self):
        product = self.product()
        self.assertIs(product["checked"], False)
        for value in [None, 0, 1, "true", [], {}]:
            with self.subTest(checked=value):
                response = self.save([{**product, "checked": value, "order_quantity": 3}])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.state()["products"][0], product)
        response = self.save([{**product, "checked": True}])
        self.assertEqual(response.status_code, 200)
        current = response.json["products"][0]
        self.assertIs(current["checked"], True)
        self.assertEqual(current["revision"], product["revision"] + 1)
        other = self.app.test_client()
        with other.session_transaction() as session:
            session["authenticated"] = True
        self.assertTrue(other.get(self.url("/api/inventory")).json["products"][0]["checked"])
        omitted = {key: value for key, value in current.items() if key != "checked"}
        response = self.save([{**omitted, "order_quantity": 4}])
        self.assertTrue(response.json["products"][0]["checked"])
        self.assertEqual(self.save([{**product, "checked": True}]).status_code, 409)
        current = response.json["products"][0]
        self.assertFalse(self.save([{**current, "checked": False}]).json["products"][0]["checked"])

    def test_checked_batch_validation_rolls_back(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        before = self.state()
        response = self.save([{**first, "checked": True}, {**second, "checked": "yes"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state(), before)

    def test_start_check_preserves_orders_and_scopes_department_and_store(self):
        first_store = self.store_id
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        other_department = self.product("Cola", department_id=self.department("Drinks"))
        self.save([{**first, "checked": True, "out_of_stock": True, "order_quantity": 7},
                   {**other_department, "checked": True, "order_quantity": 3}])
        self.store_id = self.post("/api/stores", {"name": "Second"}).json["id"]
        other_store = self.product()
        self.save([{**other_store, "checked": True, "order_quantity": 2}])
        other_before = self.state()
        self.assertEqual(self.post(f'/api/departments/{first["department_id"]}/start-check',
                                   {"products": [first, second]}).status_code, 404)
        self.store_id = first_store
        before = {item["id"]: item for item in self.state()["products"]}
        response = self.post(f'/api/departments/{first["department_id"]}/start-check',
                             {"products": [{"id": item["id"], "revision": item["revision"]}
                                           for item in before.values()
                                           if item["department_id"] == first["department_id"]]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("undo_token", response.json)
        after = {item["id"]: item for item in response.json["products"]}
        for product in [first, second]:
            self.assertEqual(after[product["id"]], {
                **before[product["id"]], "checked": False,
                "revision": before[product["id"]]["revision"] + 1,
            })
        self.assertEqual(after[other_department["id"]], before[other_department["id"]])
        self.assertEqual(self.state(other_before["store"]["id"]), other_before)
        self.assertEqual(self.save([{**before[first["id"]], "checked": True}]).status_code, 409)

    def test_start_check_snapshot_validation_and_atomic_conflicts(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        path = f'/api/departments/{first["department_id"]}/start-check'
        self.save([{**first, "checked": True}, {**second, "checked": True}])
        before = self.state()
        current = {item["id"]: item for item in before["products"]}
        for body, status in [
            ({}, 400), ({"products": None}, 400), ({"products": [None]}, 400),
            ({"products": [first, first]}, 400), ({"products": []}, 409),
            ({"products": [current[first["id"]], second]}, 409),
            ({"products": [current[first["id"]], {**current[second["id"]], "revision": True}]}, 400),
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.post(path, body).status_code, status)
                self.assertEqual(self.state(), before)
        empty = self.department("Empty")
        self.assertEqual(self.post(f"/api/departments/{empty}/start-check", {"products": []}).status_code, 200)

    def test_remove_order_preserves_catalog_checked_and_other_stores(self):
        first_store = self.store_id
        product = self.product()
        self.save([{**product, "checked": True, "out_of_stock": True, "order_quantity": 6}])
        current = self.state()["products"][0]
        self.store_id = self.post("/api/stores", {"name": "Second"}).json["id"]
        other = self.product()
        self.save([{**other, "order_quantity": 9, "checked": True}])
        other_before = self.state()
        path = f'/api/products/{product["id"]}/remove-order'
        self.assertEqual(self.post(path, {"revision": current["revision"]}).status_code, 404)
        self.store_id = first_store
        response = self.post(path, {"revision": current["revision"], "order_quantity": 999, "checked": False})
        self.assertEqual(response.status_code, 200)
        removed = response.json["products"][0]
        self.assertEqual(removed, {**current, "out_of_stock": False, "order_quantity": 0,
                                   "revision": current["revision"] + 1})
        self.assertEqual(self.state(other_before["store"]["id"]), other_before)
        self.assertEqual(self.post(path, {"revision": current["revision"]}).status_code, 409)
        self.assertEqual(self.undo(response.json["undo_token"]).status_code, 200)
        self.assertEqual(self.state()["products"][0], {**current, "revision": current["revision"] + 2})
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Product, product["id"]))

    def test_remove_order_and_undo_version_noop_rows(self):
        product = self.product()
        path = f'/api/products/{product["id"]}/remove-order'
        for revision in [None, True, "1", 0, -1]:
            self.assertEqual(self.post(path, {"revision": revision}).status_code, 400)
        response = self.post(path, {"revision": product["revision"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["products"][0]["revision"], product["revision"] + 1)
        restored = self.undo(response.json["undo_token"])
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json["products"][0]["revision"], product["revision"] + 2)
        self.assertEqual(self.save([{**product, "order_quantity": 1}]).status_code, 409)

    def test_reset_undo_restores_server_values_and_checked(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        self.save([{**first, "checked": True, "out_of_stock": True, "order_quantity": 8},
                   {**second, "order_quantity": 3}])
        before = self.state()
        forged = [{**item, "checked": False, "out_of_stock": False, "order_quantity": 999}
                  for item in before["products"]]
        response = self.reset(first["department_id"], forged)
        self.assertEqual(response.status_code, 200)
        for current in response.json["products"]:
            self.assertFalse(current["checked"])
            self.assertFalse(current["out_of_stock"])
            self.assertEqual(current["order_quantity"], 0)
        restored = self.undo(response.json["undo_token"])
        self.assertEqual(restored.status_code, 200)
        self.assertNotIn("undo_token", restored.json)
        self.assertEqual(restored.json["products"],
                         [{**item, "revision": item["revision"] + 2} for item in before["products"]])

    def test_undo_rejects_missing_wrong_store_expired_and_reused_tokens(self):
        product = self.product()
        token = self.reset(product["department_id"]).json["undo_token"]
        before = self.state()
        for invalid in [None, "", 7, {}, "x" * 65]:
            self.assertEqual(self.undo(invalid).status_code, 400)
        self.assertEqual(self.undo("missing-token").status_code, 404)
        second_store = self.post("/api/stores", {"name": "Second"}).json["id"]
        wrong_store = self.undo(token, second_store)
        self.assertEqual(wrong_store.status_code, 400)
        self.assertIn("different store", wrong_store.json["error"])
        self.assertEqual(self.state(), before)
        with self.app.app_context():
            action = db.session.get(InventoryUndo, token)
            self.assertGreater(action.expires_at, datetime.now(timezone.utc).replace(tzinfo=None))
            self.assertLessEqual(action.expires_at, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10))
            action.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.session.commit()
        expired = self.undo(token)
        self.assertEqual(expired.status_code, 410)
        self.assertIn("expired", expired.json["error"])
        self.assertEqual(self.state(), before)
        token = self.reset(product["department_id"]).json["undo_token"]
        self.assertEqual(self.undo(token).status_code, 200)
        before = self.state()
        reused = self.undo(token)
        self.assertEqual(reused.status_code, 409)
        self.assertIn("already been used", reused.json["error"])
        self.assertEqual(self.state(), before)

    def test_creating_undo_purges_only_tokens_expired_over_24_hours(self):
        department = self.department()
        for operation in ["reset", "remove-order"]:
            with self.subTest(operation=operation):
                product = self.product(operation, department_id=department)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                expirations = {
                    "active": now + timedelta(minutes=5),
                    "recent": now - timedelta(hours=23),
                    "old": now - timedelta(hours=25),
                }
                with self.app.app_context():
                    for age, expires_at in expirations.items():
                        for consumed in [False, True]:
                            db.session.add(InventoryUndo(
                                token=f"{operation}-{age}-{consumed}", store_id=self.store_id,
                                expires_at=expires_at, consumed=consumed, products=[],
                            ))
                    db.session.commit()
                if operation == "reset":
                    response = self.reset(department)
                else:
                    response = self.post(f'/api/products/{product["id"]}/remove-order',
                                         {"revision": product["revision"]})
                self.assertEqual(response.status_code, 200)
                with self.app.app_context():
                    self.assertIsNotNone(db.session.get(InventoryUndo, response.json["undo_token"]))
                    for age in expirations:
                        for consumed in [False, True]:
                            action = db.session.get(InventoryUndo, f"{operation}-{age}-{consumed}")
                            if age == "old":
                                self.assertIsNone(action)
                            else:
                                self.assertIsNotNone(action)
                                self.assertEqual(action.consumed, consumed)
                                self.assertEqual(action.expires_at, expirations[age])
                self.assertEqual(self.undo(f"{operation}-recent-False").status_code, 410)
                self.assertEqual(self.undo(f"{operation}-active-False").status_code, 200)
                self.assertEqual(self.undo(f"{operation}-old-False").status_code, 404)

    def test_undo_conflict_never_partially_restores_or_consumes_token(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        self.save([{**first, "checked": True, "order_quantity": 4},
                   {**second, "out_of_stock": True, "order_quantity": 8}])
        response = self.reset(first["department_id"])
        token = response.json["undo_token"]
        current = next(item for item in response.json["products"] if item["id"] == second["id"])
        self.save([{**current, "checked": True, "order_quantity": 12}])
        before = self.state()
        self.assertEqual(self.undo(token).status_code, 409)
        self.assertEqual(self.state(), before)
        with self.app.app_context():
            self.assertFalse(db.session.get(InventoryUndo, token).consumed)

    def test_undo_late_revision_conflict_rolls_back_rows_and_token(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        self.save([{**first, "checked": True, "order_quantity": 4},
                   {**second, "out_of_stock": True, "order_quantity": 8}])
        token = self.reset(first["department_id"]).json["undo_token"]
        before = self.state()

        def conflict_during_flush(mapper, connection, target):
            if target.product_id == second["id"]:
                table = StockState.__table__
                connection.execute(table.update().where(table.c.product_id == target.product_id)
                                   .values(revision=table.c.revision + 1))

        event.listen(StockState, "before_update", conflict_during_flush)
        try:
            self.assertEqual(self.undo(token).status_code, 409)
        finally:
            event.remove(StockState, "before_update", conflict_during_flush)
        self.assertEqual(self.state(), before)
        with self.app.app_context():
            self.assertFalse(db.session.get(InventoryUndo, token).consumed)
        self.assertEqual(self.undo(token).status_code, 200)

    def test_undo_rejects_deleted_products_and_reused_catalog_ids(self):
        product = self.product()
        self.save([{**product, "order_quantity": 4}])
        token = self.reset(product["department_id"]).json["undo_token"]
        self.assertEqual(self.delete(f'/api/products/{product["id"]}').status_code, 200)
        self.assertEqual(self.undo(token).status_code, 409)
        replacement = self.product("Replacement", department_id=product["department_id"])
        self.save([{**replacement, "checked": True}])
        before = self.state()
        self.assertEqual(self.undo(token).status_code, 409)
        self.assertEqual(self.state(), before)

    def test_undo_rejects_missing_state_or_changed_store(self):
        original_store = self.store_id
        product = self.product()
        token = self.reset(product["department_id"]).json["undo_token"]
        second_store = self.post("/api/stores", {"name": "Second"}).json["id"]
        self.store_id = second_store
        department = self.department()
        self.store_id = original_store
        with self.app.app_context():
            db.session.get(Product, product["id"]).department_id = department
            db.session.commit()
        self.assertEqual(self.undo(token, original_store).status_code, 409)
        with self.app.app_context():
            db.session.get(Product, product["id"]).department_id = product["department_id"]
            db.session.delete(db.session.get(StockState, product["id"]))
            db.session.commit()
        self.assertEqual(self.undo(token, original_store).status_code, 409)

    def test_empty_reset_undo_is_single_use(self):
        department = self.department()
        response = self.reset(department, [])
        self.assertEqual(response.status_code, 200)
        token = response.json["undo_token"]
        self.assertEqual(self.undo(token).status_code, 200)
        self.assertEqual(self.undo(token).status_code, 409)

    def test_checked_and_undo_survive_restart(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            config = self.config(f"sqlite:///{Path(folder) / 'inventory.db'}")
            self.dispose()
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            product = self.product()
            self.save([{**product, "checked": True, "out_of_stock": True, "order_quantity": 6}])
            current = self.state()["products"][0]
            self.dispose()
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            self.assertEqual(self.state()["products"][0], current)
            token = self.reset(product["department_id"]).json["undo_token"]
            self.dispose()
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            self.assertEqual(self.undo(token).json["products"][0],
                             {**current, "revision": current["revision"] + 2})
            self.dispose()

    def test_combined_orders_and_export(self):
        anonymous = self.app.test_client()
        for path in ["/api/orders/combined", "/api/orders/combined/export"]:
            self.assertEqual(anonymous.get(path).status_code, 401)
        self.assertEqual(self.client.get("/api/orders/combined").json,
                         {"products": [], "departments": [], "last_updated_at": None})
        self.assertEqual(self.client.get("/api/orders/combined/export").status_code, 400)
        milk = self.product()
        self.save([{**milk, "order_quantity": 3}])
        original_store = self.store_id
        second = self.post("/api/stores", {"name": "Second Store"}).json["id"]
        self.store_id = second
        department = self.department("Different department")
        milk2 = self.product(department_id=department)
        variant = self.product(size="473ml", department_id=department)
        unknown = self.product("Unknown", department_id=department)
        formula = self.product("=Formula", department_id=department)
        self.product("Not needed", department_id=department)
        self.save([
            {**milk2, "order_quantity": 4},
            {**variant, "order_quantity": 9},
            {**unknown, "out_of_stock": True},
            {**formula, "order_quantity": 1},
        ])
        self.store_id = original_store
        unknown2 = self.product("Unknown", department_id=milk["department_id"])
        self.save([{**unknown2, "order_quantity": 2}])
        before = self.state()
        response = self.client.get("/api/orders/combined?store_id=999&department_id=999")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        items = {(item["name"], item["size"]): item for item in response.json["products"]}
        self.assertEqual(len(items), 4)
        self.assertEqual(items[("Milk", "1 ltr")]["order_quantity"], 7)
        self.assertEqual(len(items[("Milk", "1 ltr")]["stores"]), 2)
        self.assertEqual(items[("Milk", "473ml")]["order_quantity"], 9)
        self.assertEqual(items[("Unknown", "1 ltr")]["order_quantity"], 2)
        self.assertEqual(items[("Unknown", "1 ltr")]["unspecified_count"], 1)
        exported = self.client.get("/api/orders/combined/export")
        self.assertEqual(exported.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        self.assertTrue(any(row["Product"] == "'=Formula" for row in rows))
        self.assertTrue(any(row["Product"] == "Milk" and row["Quantity needed"] == "7" for row in rows))
        self.assertEqual(self.state(), before)

    def test_combined_department_filter_applies_before_merging_and_export(self):
        department_name = "Dairy & chilled"
        original_store = self.store_id
        department = self.department(department_name)
        self.department("Empty")
        milk = self.product(department_id=department)
        unknown = self.product("Unknown", department_id=department)
        self.save([{**milk, "order_quantity": 3}, {**unknown, "out_of_stock": True}])
        first_before = self.state()
        self.store_id = self.post("/api/stores", {"name": "Second Store"}).json["id"]
        matching = self.department(department_name)
        milk2 = self.product(department_id=matching)
        unknown2 = self.product("Unknown", department_id=matching)
        other = self.department("Other")
        milk3 = self.product(department_id=other)
        unknown3 = self.product("Unknown", department_id=other)
        self.save([
            {**milk2, "order_quantity": 4}, {**unknown2, "order_quantity": 2},
            {**milk3, "order_quantity": 9}, {**unknown3, "out_of_stock": True},
        ])
        second_before = self.state()
        query = {"department_name": department_name, "store_id": 999}
        result = self.client.get("/api/orders/combined", query_string=query).json
        self.assertEqual(result["departments"], [department_name, "Empty", "Other"])
        products = {item["name"]: item for item in result["products"]}
        self.assertEqual(products["Milk"]["order_quantity"], 7)
        self.assertEqual(products["Milk"]["unspecified_count"], 0)
        self.assertEqual(len(products["Milk"]["stores"]), 2)
        self.assertEqual(products["Unknown"]["order_quantity"], 2)
        self.assertEqual(products["Unknown"]["unspecified_count"], 1)
        exported = self.client.get("/api/orders/combined/export", query_string=query)
        self.assertEqual(exported.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        self.assertEqual([row["Quantity needed"] for row in rows], ["7", "2"])
        self.assertEqual([row["Unspecified quantities"] for row in rows], ["0", "1"])
        other_result = self.client.get("/api/orders/combined", query_string={"department_name": "Other"}).json
        self.assertEqual(other_result["products"][0]["stores"], ["Second Store"])
        for name in ["Empty", "Deleted department", department_name.lower()]:
            query = {"department_name": name}
            self.assertEqual(self.client.get("/api/orders/combined", query_string=query).json["products"], [])
            self.assertEqual(self.client.get("/api/orders/combined/export", query_string=query).status_code, 400)
        all_products = self.client.get("/api/orders/combined", query_string={"department_name": ""}).json["products"]
        self.assertEqual(all_products[0]["order_quantity"], 16)
        self.assertEqual(all_products[1]["unspecified_count"], 2)
        self.assertEqual(self.state(original_store), first_before)
        self.assertEqual(self.state(), second_before)

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
        self.assertEqual(manifest["name"], "Qmarket Stockroom")
        self.assertEqual(manifest["short_name"], "Qmarket Stockroom")
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
        for path in ["/static/offline.html", "/static/pwa.js", "/static/mobile.css", "/static/theme.css"]:
            with visitor.get(path) as response:
                self.assertEqual(response.status_code, 200)
        with visitor.get("/login") as response:
            self.assertIn(b"Qmarket Stockroom", response.data)
            self.assertIn(b"theme.css", response.data)
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
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
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
            self.assertFalse(product["checked"])
            self.assertFalse(product["picked_up"])
            self.assertEqual(product["position"], 0)
            self.assertIsNone(self.state()["last_updated_at"])
            self.assertEqual(self.state()["store"]["catalog_revision"], 1)
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
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
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
                self.assertFalse(product["checked"])
                self.assertNotIn("description", product)
                self.assertEqual(len(self.client.get("/api/stores").json["stores"]), 1)
                self.dispose()
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT description FROM product").fetchone()[0], "Archived detail")
            self.assertEqual(connection.execute("SELECT checked FROM stock_state").fetchone()[0], 0)
            connection.execute("INSERT INTO product (id, name, department_id) VALUES (2, 'Cream', 1)")
            connection.execute("INSERT INTO stock_state (product_id, out_of_stock, order_quantity, revision) VALUES (2, 0, 0, 1)")
            self.assertEqual(connection.execute("SELECT checked FROM stock_state WHERE product_id = 2").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT picked_up FROM stock_state WHERE product_id = 2").fetchone()[0], 0)
            connection.close()

    def pickup(self, products):
        return self.put("/api/orders/picked-up", {"products": products})

    def combined(self, department_name=""):
        response = self.client.get("/api/orders/combined", query_string={"department_name": department_name})
        self.assertEqual(response.status_code, 200)
        return response.json

    def combined_pickup(self, item, picked_up=True, department_name=""):
        return self.client.put("/api/orders/combined/picked-up",
                               query_string={"department_name": department_name},
                               json={"name": item["name"], "size": item["size"],
                                     "products": item["items"], "picked_up": picked_up},
                               headers=self.headers)

    def test_pickup_preserves_demand_and_checked_and_resets_on_demand_edits(self):
        product = self.product()
        self.assertFalse(product["picked_up"])
        current = self.save([{**product, "order_quantity": 5, "checked": True}]).json["products"][0]
        result = self.pickup([{**current, "picked_up": True}])
        self.assertEqual(result.status_code, 200)
        picked = result.json["products"][0]
        self.assertEqual(picked, {**current, "picked_up": True, "revision": current["revision"] + 1})
        self.assertTrue(self.combined()["products"][0]["picked_up"])
        checked = self.save([{**picked, "checked": False}]).json["products"][0]
        self.assertTrue(checked["picked_up"])
        started = self.post(f'/api/departments/{product["department_id"]}/start-check',
                            {"products": [checked]}).json["products"][0]
        self.assertTrue(started["picked_up"])
        changed = self.save([{**started, "order_quantity": 6}]).json["products"][0]
        self.assertFalse(changed["picked_up"])
        picked = self.pickup([{**changed, "picked_up": True}]).json["products"][0]
        changed = self.save([{**picked, "out_of_stock": True}]).json["products"][0]
        self.assertFalse(changed["picked_up"])
        self.assertEqual(changed["order_quantity"], 6)

    def test_pickup_validation_scoping_and_atomic_staleness(self):
        first = self.product()
        second = self.product("Cream", department_id=first["department_id"])
        self.save([{**first, "order_quantity": 2}])
        first = next(item for item in self.state()["products"] if item["id"] == first["id"])
        before = self.state()
        invalid = [
            ({}, 400), ({"products": []}, 400), ({"products": [None]}, 400),
            ({"products": [{**first, "picked_up": True}] * 2}, 400),
            ({"products": [{**first, "picked_up": True},
                           {**second, "picked_up": True}]}, 409),
            ({"products": [{**first, "picked_up": True, "revision": 1}]}, 409),
        ]
        for value in [None, 1, 0, "true", [], {}]:
            invalid.append(({"products": [{**first, "picked_up": value}]}, 400))
        for body, status in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.put("/api/orders/picked-up", body).status_code, status)
                self.assertEqual(self.state(), before)
        other = self.post("/api/stores", {"name": "Other"}).json["id"]
        self.assertEqual(self.put("/api/orders/picked-up", {"products": [{**first, "picked_up": True}]},
                                  other).status_code, 404)
        self.assertEqual(self.state(), before)

    def test_pickup_reset_remove_undo_and_old_tokens(self):
        product = self.product()
        current = self.save([{**product, "order_quantity": 3, "checked": True}]).json["products"][0]
        current = self.pickup([{**current, "picked_up": True}]).json["products"][0]
        for path, body in [
            (f'/api/departments/{product["department_id"]}/reset', lambda row: {"products": [row]}),
            (f'/api/products/{product["id"]}/remove-order', lambda row: {"revision": row["revision"]}),
        ]:
            response = self.post(path, body(current))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["products"][0]["picked_up"])
            restored = self.undo(response.json["undo_token"]).json["products"][0]
            self.assertEqual(restored, {**current, "revision": current["revision"] + 2})
            current = restored
        token = self.reset(product["department_id"]).json["undo_token"]
        with self.app.app_context():
            action = db.session.get(InventoryUndo, token)
            action.products = [{key: value for key, value in item.items() if key != "picked_up"}
                               for item in action.products]
            db.session.commit()
        restored = self.undo(token).json["products"][0]
        self.assertFalse(restored["picked_up"])
        self.assertTrue(restored["checked"])
        self.assertEqual(restored["order_quantity"], 3)

    def test_combined_breakdown_filter_and_formula_safe_csv(self):
        main_store = self.store_id
        dairy = self.product()
        other = self.product(department_id=self.department("Other"))
        self.save([{**dairy, "order_quantity": 3}, {**other, "out_of_stock": True}])
        dairy = next(item for item in self.state()["products"] if item["id"] == dairy["id"])
        self.pickup([{**dairy, "picked_up": True}])
        self.store_id = self.post("/api/stores", {"name": "=Second"}).json["id"]
        second = self.product()
        second = self.save([{**second, "order_quantity": 4}]).json["products"][0]
        item = self.combined()["products"][0]
        self.assertEqual(item["store_quantities"], [
            {"store_id": self.store_id, "store_name": "=Second", "order_quantity": 4,
             "unspecified_count": 0, "picked_up": False},
            {"store_id": main_store, "store_name": "Main Store", "order_quantity": 3,
             "unspecified_count": 1, "picked_up": False},
        ])
        self.assertEqual(len(item["items"]), 3)
        self.assertEqual(item["order_quantity"], 7)
        self.assertFalse(item["picked_up"])
        filtered = self.combined("Dairy")["products"][0]
        self.assertEqual(len(filtered["items"]), 2)
        self.assertTrue(filtered["store_quantities"][1]["picked_up"])
        response = self.combined_pickup(filtered, department_name="Dairy")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["products"][0]["picked_up"])
        self.assertEqual(response.json, self.combined("Dairy"))
        self.assertFalse(self.combined()["products"][0]["picked_up"])
        response = self.client.get("/api/orders/combined/export")
        row = next(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(row["Store quantities"], "'=Second: 4; Main Store: 3 + 1 unspecified")
        row = next(csv.DictReader(io.StringIO(self.client.get(
            "/api/orders/combined/export", query_string={"department_name": "Dairy"}).text)))
        self.assertEqual(row["Store quantities"], "'=Second: 4; Main Store: 3")

    def test_combined_pickup_rejects_changed_contributor_set_and_stale_revisions(self):
        first_store = self.store_id
        first = self.product()
        self.save([{**first, "order_quantity": 2}])
        snapshot = self.combined()["products"][0]
        self.store_id = self.post("/api/stores", {"name": "Second"}).json["id"]
        second = self.product()
        self.save([{**second, "out_of_stock": True}])
        before = self.combined()
        self.assertEqual(self.combined_pickup(snapshot).status_code, 409)
        self.assertEqual(self.combined(), before)
        snapshot = before["products"][0]
        current = self.state()["products"][0]
        self.save([{**current, "checked": True}])
        before = self.combined()
        self.assertEqual(self.combined_pickup(snapshot).status_code, 409)
        self.assertEqual(self.combined(), before)
        snapshot = before["products"][0]
        self.put(f'/api/products/{second["id"]}', {"name": "Changed", "size": second["size"]})
        before = self.combined()
        self.assertEqual(self.combined_pickup(snapshot).status_code, 409)
        self.assertEqual(self.combined(), before)
        self.assertFalse(self.state(first_store)["products"][0]["picked_up"])
        item = next(item for item in before["products"] if item["name"] == "Milk")
        response = self.combined_pickup(item)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.state(first_store)["products"][0]["picked_up"])
        self.assertFalse(self.state()["products"][0]["picked_up"])

    def test_combined_pickup_validation_and_unpick_retains_totals(self):
        product = self.product()
        self.save([{**product, "order_quantity": 8}])
        item = self.combined()["products"][0]
        base = {"name": item["name"], "size": item["size"], "products": item["items"], "picked_up": True}
        bodies = [
            {**base, "name": 1}, {**base, "size": None}, {**base, "products": []},
            {**base, "products": item["items"] * 2},
            {**base, "products": [{"id": product["id"], "revision": True}]},
            {**base, "products": [{"id": True, "revision": 1}]},
            {**base, "products": [None]},
        ] + [{**base, "picked_up": value} for value in [None, 0, 1, "true", []]]
        before = self.combined()
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/api/orders/combined/picked-up",
                                                json=body, headers=self.headers).status_code, 400)
                self.assertEqual(self.combined(), before)
        self.assertEqual(self.combined_pickup(item, department_name="Missing").status_code, 409)
        self.assertEqual(self.combined_pickup({**item, "size": "wrong"}).status_code, 409)
        picked = self.combined_pickup(item).json["products"][0]
        self.assertTrue(picked["picked_up"])
        self.assertEqual(picked["order_quantity"], 8)
        unpicked = self.combined_pickup(picked, False).json["products"][0]
        self.assertFalse(unpicked["picked_up"])
        self.assertEqual(unpicked["order_quantity"], 8)

    def test_catalog_shelf_order_appending_and_inventory_preservation(self):
        dairy = self.department("Dairy")
        drinks = self.department("Drinks")
        milk = self.product("Milk", department_id=dairy)
        cream = self.product("Cream", department_id=dairy)
        cola = self.product("Cola", department_id=drinks)
        self.save([{**milk, "order_quantity": 3, "checked": True}, {**cream, "order_quantity": 2},
                   {**cola, "out_of_stock": True}])
        before = self.state()
        self.assertEqual([item["name"] for item in before["products"]], ["Cream", "Milk", "Cola"])
        response = self.put("/api/catalog/order", {"revision": before["store"]["catalog_revision"],
                                                  "department_ids": [drinks, dairy]})
        self.assertEqual(response.status_code, 200)
        result = response.json
        self.assertEqual([item["name"] for item in result["products"]], ["Cola", "Cream", "Milk"])
        self.assertEqual(result["last_updated_at"], before["last_updated_at"])
        self.assertEqual(result["store"]["catalog_revision"], before["store"]["catalog_revision"] + 1)
        result = self.put("/api/catalog/order", {"revision": result["store"]["catalog_revision"],
                                                "department_id": dairy,
                                                "product_ids": [milk["id"], cream["id"]]}).json
        self.assertEqual([item["name"] for item in result["products"]], ["Cola", "Milk", "Cream"])
        by_id = {item["id"]: item for item in before["products"]}
        for item in result["products"]:
            self.assertEqual({key: value for key, value in item.items() if key != "position"},
                             {key: value for key, value in by_id[item["id"]].items() if key != "position"})
        exported = self.client.get(self.url("/api/orders/export"))
        self.assertEqual([row["Product"] for row in csv.DictReader(io.StringIO(exported.text))],
                         ["Cola", "Milk", "Cream"])
        self.product("A first alphabetically", department_id=dairy)
        self.department("A new department")
        self.assertEqual([item["name"] for item in self.state()["departments"]],
                         ["Drinks", "Dairy", "A new department"])
        self.assertEqual([item["name"] for item in self.state()["products"]],
                         ["Cola", "Milk", "Cream", "A first alphabetically"])
        self.assertEqual([item["name"] for item in self.combined()["products"]], ["Cola", "Cream", "Milk"])

    def test_catalog_order_validation_staleness_and_scoping(self):
        milk = self.product()
        other = self.department("Other")
        before = self.state()
        revision = before["store"]["catalog_revision"]
        base = {"revision": revision, "department_ids": [milk["department_id"], other]}
        invalid = [
            ({**base, "revision": True}, 400), ({**base, "department_ids": [other, other]}, 400),
            ({**base, "department_ids": [True]}, 400), ({**base, "department_ids": None}, 400),
            ({**base, "department_ids": []}, 409),
            ({**base, "department_ids": [other, 999]}, 409),
            ({**base, "department_id": other}, 400),
            ({"revision": revision, "department_id": milk["department_id"], "product_ids": []}, 409),
            ({"revision": revision, "department_id": other, "product_ids": [milk["id"]]}, 409),
        ]
        for body, status in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.put("/api/catalog/order", body).status_code, status)
                self.assertEqual(self.state(), before)
        other_store = self.post("/api/stores", {"name": "Other"}).json["id"]
        self.assertEqual(self.put("/api/catalog/order",
                                  {"revision": 1, "department_id": other, "product_ids": []},
                                  other_store).status_code, 404)
        self.assertEqual(self.put("/api/catalog/order", base).status_code, 200)
        after = self.state()
        self.assertEqual(self.put("/api/catalog/order", base).status_code, 409)
        self.assertEqual(self.state(), after)
        revision = after["store"]["catalog_revision"]
        self.put(f'/api/products/{milk["id"]}', {"name": "Whole milk", "size": "1 ltr"})
        self.assertEqual(self.put("/api/catalog/order", {**base, "revision": revision}).status_code, 409)
        self.assertEqual(self.state()["store"]["catalog_revision"], revision + 1)
        self.delete(f'/api/products/{milk["id"]}')
        self.assertEqual(self.state()["store"]["catalog_revision"], revision + 2)
        self.delete(f'/api/departments/{milk["department_id"]}')
        self.assertEqual(self.state()["store"]["catalog_revision"], revision + 3)

    def test_catalog_copy_is_independent_and_does_not_copy_stock_or_undo(self):
        source = self.store_id
        dairy = self.department()
        drinks = self.department("Drinks")
        milk = self.product(department_id=dairy)
        cream = self.product("Cream", department_id=dairy)
        current = self.save([{**milk, "order_quantity": 7, "checked": True}]).json["products"]
        milk = next(item for item in current if item["id"] == milk["id"])
        self.pickup([{**milk, "picked_up": True}])
        state = self.state()
        state = self.put("/api/catalog/order", {"revision": state["store"]["catalog_revision"],
                                               "department_ids": [drinks, dairy]}).json
        self.put("/api/catalog/order", {"revision": state["store"]["catalog_revision"],
                                        "department_id": dairy, "product_ids": [milk["id"], cream["id"]]})
        before = self.state()
        result = self.post("/api/stores", {"name": "Copied", "source_store_id": source})
        self.assertEqual(result.status_code, 201)
        self.store_id = result.json["id"]
        copied = self.state()
        self.assertIsNone(copied["last_updated_at"])
        self.assertEqual(copied["store"]["catalog_revision"], 1)
        self.assertEqual([item["name"] for item in copied["departments"]], ["Drinks", "Dairy"])
        self.assertEqual([item["name"] for item in copied["products"]], ["Milk", "Cream"])
        for item in copied["products"]:
            self.assertNotIn(item["id"], {milk["id"], cream["id"]})
            self.assertEqual((item["order_quantity"], item["out_of_stock"], item["checked"],
                              item["picked_up"], item["revision"]), (0, False, False, False, 1))
        copied_dairy = copied["products"][0]["department_id"]
        self.product("A new", department_id=copied_dairy)
        self.department("A new")
        self.assertEqual([item["name"] for item in self.state()["products"]], ["Milk", "Cream", "A new"])
        self.assertEqual([item["name"] for item in self.state()["departments"]], ["Drinks", "Dairy", "A new"])
        self.save([{**copied["products"][0], "order_quantity": 99}])
        self.assertEqual(self.state(source), before)
        with self.app.app_context():
            self.assertEqual(db.session.query(InventoryUndo).filter_by(store_id=self.store_id).count(), 0)

    def test_catalog_copy_invalid_and_empty_sources_are_atomic(self):
        before = self.client.get("/api/stores").json
        for value in [None, True, 0, -1, "1", [], {}, 999]:
            with self.subTest(value=value):
                result = self.post("/api/stores", {"name": "New", "source_store_id": value})
                self.assertEqual(result.status_code, 404 if value == 999 else 400)
                self.assertEqual(self.client.get("/api/stores").json, before)
        self.assertEqual(self.post("/api/stores", {"name": "Main Store",
                                                   "source_store_id": self.store_id}).status_code, 409)
        copied = self.post("/api/stores", {"name": "Empty copy", "source_store_id": self.store_id})
        self.assertEqual(copied.status_code, 201)
        empty = self.state(copied.json["id"])
        self.assertEqual(empty["products"], [])
        self.assertEqual(empty["departments"], [])
        self.assertIsNone(empty["last_updated_at"])

    def test_inventory_timestamps_are_scoped_transactional_and_durable_on_get(self):
        first_store = self.store_id
        product = self.product()
        self.assertIsNone(self.state()["last_updated_at"])
        self.assertIsNone(self.combined()["last_updated_at"])
        self.assertIsNone(self.save([product]).json["last_updated_at"])
        first = self.save([{**product, "order_quantity": 3}]).json
        stamp = first["last_updated_at"]
        self.assertIsNotNone(stamp)
        self.assertTrue(stamp.endswith("Z"))
        self.assertIsNotNone(datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo)
        self.assertEqual(self.state(), first)
        self.assertEqual(self.combined()["last_updated_at"], stamp)
        self.store_id = self.post("/api/stores", {"name": "Second"}).json["id"]
        self.assertIsNone(self.state()["last_updated_at"])
        second = self.product()
        second = self.save([{**second, "out_of_stock": True}]).json
        self.assertGreater(second["last_updated_at"], stamp)
        before = self.combined()
        self.assertEqual(before["last_updated_at"], second["last_updated_at"])
        self.assertEqual(self.save([{**second["products"][0], "order_quantity": 9},
                                    {"id": 99999}]).status_code, 404)
        self.assertEqual(self.state(), second)
        self.assertEqual(self.combined(), before)
        self.combined_pickup(before["products"][0])
        self.assertGreater(self.state()["last_updated_at"], second["last_updated_at"])
        self.assertEqual(self.state()["last_updated_at"], self.state(first_store)["last_updated_at"])

    def test_new_metadata_migration_and_restart_retention(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "current.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript("""
                    CREATE TABLE store (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE);
                    CREATE TABLE department (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL,
                        store_id INTEGER NOT NULL REFERENCES store(id),
                        CONSTRAINT uq_department_store_name UNIQUE(store_id,name));
                    CREATE TABLE product (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL,
                        department_id INTEGER NOT NULL REFERENCES department(id),
                        size VARCHAR(80) NOT NULL DEFAULT '', description VARCHAR(1000) NOT NULL DEFAULT '',
                        CONSTRAINT uq_product_department_name_size UNIQUE(department_id,name,size));
                    CREATE TABLE stock_state (product_id INTEGER PRIMARY KEY REFERENCES product(id),
                        out_of_stock BOOLEAN NOT NULL, order_quantity INTEGER NOT NULL,
                        checked BOOLEAN NOT NULL DEFAULT 0, revision INTEGER NOT NULL);
                    INSERT INTO store VALUES (1,'Main Store');
                    INSERT INTO department VALUES (1,'Dairy',1);
                    INSERT INTO product VALUES (1,'Milk',1,'1 ltr','');
                    INSERT INTO stock_state VALUES (1,1,9,1,6);
                """)
            self.dispose()
            config = self.config(f"sqlite:///{path}")
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            state = self.state()
            self.assertIsNone(state["last_updated_at"])
            self.assertEqual(state["store"]["catalog_revision"], 1)
            product = state["products"][0]
            self.assertEqual((product["checked"], product["picked_up"], product["revision"]), (True, False, 6))
            self.pickup([{**product, "picked_up": True}])
            self.put("/api/catalog/order", {"revision": 1, "department_id": 1, "product_ids": [1]})
            before = self.state()
            for _ in range(2):
                self.dispose()
                self.app = create_app(config)
                self.client = self.app.test_client()
                self.login()
                self.assertEqual(self.state(), before)
            token = self.reset(1).json["undo_token"]
            self.dispose()
            self.app = create_app(config)
            self.client = self.app.test_client()
            self.login()
            restored = self.undo(token).json
            self.assertTrue(restored["products"][0]["picked_up"])
            self.assertGreater(restored["last_updated_at"], before["last_updated_at"])
            self.dispose()

    def test_combined_pickup_late_conflict_rolls_back_all_stores_and_timestamps(self):
        first_store = self.store_id
        milk = self.product()
        self.save([{**milk, "order_quantity": 5}])
        self.store_id = self.post("/api/stores", {"name": "Second"}).json["id"]
        second = self.product()
        self.save([{**second, "order_quantity": 2}])
        combined_before = self.combined()
        first_before, second_before = self.state(first_store), self.state()

        def conflict_during_flush(mapper, connection, target):
            if target.product_id == second["id"]:
                table = StockState.__table__
                connection.execute(table.update().where(table.c.product_id == target.product_id)
                                   .values(revision=table.c.revision + 1))

        event.listen(StockState, "before_update", conflict_during_flush)
        try:
            self.assertEqual(self.combined_pickup(combined_before["products"][0]).status_code, 409)
        finally:
            event.remove(StockState, "before_update", conflict_during_flush)
        self.assertEqual(self.state(first_store), first_before)
        self.assertEqual(self.state(), second_before)
        self.assertEqual(self.combined(), combined_before)

    def test_simultaneous_reorders_and_combined_pickups_have_one_winner(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            self.dispose()
            self.app = create_app(self.config(f"sqlite:///{Path(folder) / 'race.db'}"))
            self.client = self.app.test_client()
            self.login()
            milk = self.product()
            self.save([{**milk, "order_quantity": 5}])
            other = self.department("Other")

            def simultaneous(path, bodies):
                clients = [self.app.test_client(), self.app.test_client()]
                for client in clients:
                    with client.session_transaction() as session:
                        session["authenticated"] = True
                        session["csrf"] = "race-token"
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(client.put, path, json=body,
                                               headers={"X-CSRF-Token": "race-token"})
                               for client, body in zip(clients, bodies)]
                    return sorted(future.result().status_code for future in futures)

            revision = self.state()["store"]["catalog_revision"]
            self.assertEqual(simultaneous(self.url("/api/catalog/order"), [
                {"revision": revision, "department_ids": [other, milk["department_id"]]},
                {"revision": revision, "department_ids": [milk["department_id"], other]},
            ]), [200, 409])
            self.assertEqual(self.state()["store"]["catalog_revision"], revision + 1)
            item = self.combined()["products"][0]
            body = {"name": item["name"], "size": item["size"], "products": item["items"], "picked_up": True}
            self.assertEqual(simultaneous("/api/orders/combined/picked-up", [body, body]), [200, 409])
            self.assertEqual(self.state()["products"][0]["revision"], item["items"][0]["revision"] + 1)
            self.dispose()


if __name__ == "__main__":
    unittest.main()
