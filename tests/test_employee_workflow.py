import json
import unittest
from threading import Thread

from werkzeug.serving import WSGIRequestHandler, make_server

from main import Department, Product, StockState, Store, create_app, db

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "Requires Playwright and Chromium")
class EmployeeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True, "SECRET_KEY": "test-secret",
            "INVENTORY_PASSWORD": "test-password", "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "SESSION_COOKIE_SECURE": False,
        })
        with self.app.app_context():
            store = db.session.scalar(db.select(Store))
            store.name = "First store"
            other = Store(name="Second store")
            db.session.add(other)
            db.session.flush()
            departments = [
                Department(name="Drinks", store_id=store.id),
                Department(name="Food", store_id=store.id),
                Department(name="Drinks", store_id=other.id),
            ]
            db.session.add_all(departments)
            db.session.flush()
            for name, department, quantity, out in [
                ("Alpha", departments[0], 0, False),
                ("Beta", departments[0], 4, True),
                ("Gamma", departments[0], 0, True),
                ("Delta", departments[1], 2, False),
                ("Alpha", departments[2], 9, True),
            ]:
                product = Product(name=name, size="355ml", department_id=department.id)
                db.session.add(product)
                db.session.flush()
                db.session.add(StockState(
                    product_id=product.id, order_quantity=quantity, out_of_stock=out,
                ))
            db.session.commit()
            self.store_id, self.other_id = store.id, other.id
            self.department_id, self.food_id, self.other_department_id = [item.id for item in departments]
        self.server = make_server("127.0.0.1", 0, self.app, request_handler=QuietHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page_errors = []
        self.addCleanup(lambda: self.assertEqual(self.page_errors, []))
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.page = self.employee()

    def cleanup_server(self):
        self.server.shutdown()
        self.thread.join()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def employee(self, storage_blocked=False):
        context = self.browser.new_context(viewport={"width": 390, "height": 844})
        if storage_blocked:
            context.add_init_script("""Object.defineProperty(window, 'localStorage', {
                get() { throw new DOMException('Blocked', 'SecurityError'); }
            });""")
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base)
        page.locator("[name=password]").fill("test-password")
        page.locator("button[type=submit]").click()
        expect(page.locator("#store-select")).to_be_enabled()
        return page

    def department(self, page=None, department_id=None):
        page = page or self.page
        page.locator("#inventory-department").select_option(str(department_id or self.department_id))

    def state(self, page=None, store_id=None):
        response = (page or self.page).request.get(
            f"{self.base}/api/inventory?store_id={store_id or self.store_id}")
        self.assertEqual(response.status, 200)
        return response.json()["products"]

    def update(self, page, product, **changes):
        token = page.locator('meta[name="csrf-token"]').get_attribute("content")
        response = page.request.put(
            f"{self.base}/api/inventory?store_id={self.store_id}",
            headers={"X-CSRF-Token": token}, data={"products": [{**product, **changes}]},
        )
        self.assertEqual(response.status, 200)

    def test_search_shared_check_and_resume(self):
        page = self.page
        teammate = self.employee()
        self.department()
        self.department(teammate)
        page.locator("#inventory-search").fill("alpha")
        expect(page.locator("#inventory-rows tr")).to_have_count(1)
        page.get_by_role("button", name="Alpha (355ml): Checked - nothing needed", exact=True).click()
        expect(page.locator("#department-progress")).to_have_text("1 of 3 checked")
        page.locator("#inventory-search").fill("no match")
        expect(page.locator("#inventory-empty h3")).to_have_text("No matching products")
        expect(page.locator("#department-progress")).to_have_text("1 of 3 checked")
        teammate.locator("#refresh-inventory").click()
        expect(teammate.locator("#department-progress")).to_have_text("1 of 3 checked")
        page.locator("#inventory-search").fill("355ML")
        expect(page.locator("#inventory-rows tr")).to_have_count(3)
        page.locator("#flashcard-view").click()
        page.locator("#next-product").click()
        page.get_by_role("button", name="Beta (355ml): Mark checked", exact=True).click()
        expect(page.locator("#product-card h2")).to_have_text("Gamma")
        expect(page.locator("#department-progress")).to_have_text("2 of 3 checked")
        page.locator("#product-card .quantity-input").fill("3")
        page.locator("#product-card h2").click()
        expect(page.locator("#department-progress")).to_have_text("3 of 3 checked - department complete")
        teammate.locator("#refresh-inventory").click()
        expect(teammate.locator("#department-progress")).to_have_text("3 of 3 checked - department complete")
        teammate.once("dialog", lambda dialog: dialog.accept())
        teammate.locator("#start-check").click()
        expect(teammate.locator("#department-progress")).to_have_text("0 of 3 checked")
        state = {item["name"]: item for item in self.state()}
        self.assertEqual(state["Beta"]["order_quantity"], 4)
        self.assertEqual(state["Gamma"]["order_quantity"], 3)
        page.locator("#refresh-inventory").click()
        expect(page.locator("#department-progress")).to_have_text("0 of 3 checked")
        page.locator("#inventory-search").fill("")
        self.department(department_id=self.food_id)
        expect(page.locator("#product-card h2")).to_have_text("Delta")
        page.locator("#store-select").select_option(str(self.other_id))
        expect(page.locator("#store-select")).to_be_enabled()
        self.department(department_id=self.other_department_id)
        expect(page.locator("#product-card h2")).to_have_text("Alpha")
        page.goto(self.base)
        expect(page.locator("#store-select")).to_have_value(str(self.other_id))
        expect(page.locator("#inventory-department")).to_have_value(str(self.other_department_id))
        expect(page.locator("#product-card h2")).to_have_text("Alpha")
        page.locator("#store-select").select_option(str(self.store_id))
        expect(page.locator("#inventory-department")).to_have_value(str(self.food_id))
        expect(page.locator("#product-card h2")).to_have_text("Delta")
        self.department()
        expect(page.locator("#product-card h2")).to_have_text("Gamma")
        saved = page.evaluate("localStorage.getItem('qmarket-navigation-v1')")
        self.assertNotIn("Gamma", saved)
        self.assertNotIn("order_quantity", saved)
        self.assertIn("cards", json.loads(saved))

    def test_undo_reset_remove_and_conflict(self):
        page = self.page
        self.department()
        page.locator("#inventory-search").fill("Alpha")
        page.get_by_role("button", name="Alpha (355ml): Checked - nothing needed", exact=True).click()
        expect(page.locator("#department-progress")).to_have_text("1 of 3 checked")
        before = self.state()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#reset-department").click()
        expect(page.locator("#undo-banner")).to_be_visible()
        self.assertTrue(all(item["order_quantity"] == 0 for item in self.state()
                            if item["department_id"] == self.department_id))
        page.locator("#undo-action").click()
        expect(page.locator("#undo-banner")).to_be_hidden()
        after = self.state()
        for previous, current in zip(before, after):
            for key in ["id", "checked", "out_of_stock", "order_quantity"]:
                self.assertEqual(previous[key], current[key])
        self.assertEqual(self.state(store_id=self.other_id)[0]["order_quantity"], 9)
        page.locator("[data-view=orders]").click()
        remove = page.get_by_role("button", name="Remove Beta (355ml) from order", exact=True)
        remove.click()
        expect(remove).to_have_count(0)
        page.locator("#undo-action").click()
        expect(remove).to_be_visible()
        remove.click()
        expect(remove).to_have_count(0)
        product = next(item for item in self.state() if item["name"] == "Beta")
        teammate = self.employee()
        self.update(teammate, product, order_quantity=6, checked=True)
        page.locator("#undo-action").click()
        expect(page.locator("#save-state")).to_have_text("Change not saved.")
        self.assertEqual(next(item for item in self.state() if item["name"] == "Beta")["order_quantity"], 6)
        page.locator("#store-select").select_option(str(self.other_id))
        expect(page.locator("#undo-banner")).to_be_hidden()

    def test_missing_quantity_reminders(self):
        page = self.page
        page.locator("[data-view=orders]").click()
        expect(page.locator("#order-rows .quantity-warning")).to_have_count(1)
        page.locator("#view-order-text").click()
        expect(page.locator("#missing-quantities")).to_be_visible()
        expect(page.locator("#missing-quantity-items")).to_contain_text("Gamma (355ml)")
        self.assertIn("Gamma (355ml) - Qty: Not specified", page.locator("#order-text").input_value())
        page.locator("#review-quantities").click()
        expect(page.locator("#order-text-dialog")).to_be_hidden()
        gamma = page.get_by_role("textbox", name="Gamma (355ml): Qty needed", exact=True)
        expect(gamma).to_be_focused()
        gamma.fill("2")
        page.locator("#orders-panel h2").click()
        expect(page.locator("#order-rows .quantity-warning")).to_have_count(0)
        page.locator("#view-order-text").click()
        expect(page.locator("#missing-quantities")).to_be_hidden()
        page.locator("#close-order-text").click()
        product = next(item for item in self.state() if item["name"] == "Gamma")
        self.update(page, product, order_quantity=0)
        page.locator("[data-view=combined]").click()
        page.locator("#view-order-text").click()
        expect(page.locator("#missing-quantities")).to_be_visible()
        expect(page.locator("#missing-quantity-items")).to_contain_text("Gamma (355ml)")
        for width in [320, 390, 768, 1440]:
            page.set_viewport_size({"width": width, "height": 844})
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            bounds = page.locator("#order-text-dialog").bounding_box()
            self.assertGreaterEqual(bounds["y"], 0)
            self.assertLessEqual(bounds["y"] + bounds["height"], 844)

    def test_failed_check_and_unavailable_navigation_storage(self):
        page = self.page
        self.department()
        page.locator("#flashcard-view").click()
        page.route("**/api/inventory?*", lambda route: route.fulfill(
            status=409, content_type="application/json", body='{"error":"Another employee changed this product."}'))
        page.get_by_role("button", name="Alpha (355ml): Checked - nothing needed", exact=True).click()
        expect(page.locator("#notice")).to_have_text("Another employee changed this product.")
        expect(page.locator("#product-card h2")).to_have_text("Alpha")
        expect(page.locator("#department-progress")).to_have_text("0 of 3 checked")
        self.assertFalse(self.state()[0]["checked"])
        blocked = self.employee(storage_blocked=True)
        expect(blocked.locator("#navigation-storage-notice")).to_be_visible()
        self.department(blocked)
        expect(blocked.locator("#inventory-rows tr")).to_have_count(3)

    def test_navigation_url_priority_and_invalid_saved_place(self):
        page = self.page
        saved = {
            "store": self.other_id, "view": "combined", "inventoryView": "flashcards",
            "stores": {str(self.store_id): {"inventory": "999999"}},
            "cards": {},
        }
        page.evaluate("(saved) => localStorage.setItem('qmarket-navigation-v1', JSON.stringify(saved))", saved)
        page.goto(f"{self.base}/?store_id={self.store_id}&view=orders")
        expect(page.locator("#orders-panel")).to_be_visible()
        expect(page.locator("#store-select")).to_have_value(str(self.store_id))
        page.locator("[data-view=inventory]").click()
        expect(page.locator("#inventory-department")).to_have_value("")
        expect(page.locator("#inventory-empty h3")).to_have_text("Select a department")
        page.evaluate("localStorage.setItem('qmarket-navigation-v1', '{invalid')")
        page.goto(self.base)
        expect(page.locator("#navigation-storage-notice")).to_be_visible()
        expect(page.locator("#store-select")).to_be_enabled()
        self.department()
        expect(page.locator("#inventory-rows tr")).to_have_count(3)
