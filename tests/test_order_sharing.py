from csv import DictReader
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
class OrderSharingTests(unittest.TestCase):
    def test_text_sharing(self):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "INVENTORY_PASSWORD": "test-password",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "SESSION_COOKIE_SECURE": False,
        })
        with app.app_context():
            first = db.session.scalar(db.select(Store))
            first.name = "Market One"
            second = Store(name="Market Two")
            db.session.add(second)
            db.session.flush()
            dairy = Department(name="Dairy", store_id=first.id)
            drinks = Department(name="Drinks", store_id=first.id)
            private = Department(name="Private", store_id=second.id)
            empty = Department(name="Empty", store_id=first.id)
            db.session.add_all([dairy, drinks, private, empty])
            db.session.flush()
            for name, size, department, quantity, out in [
                ("Milk", "1 ltr", dairy, 3, False),
                ("Cola", "355ml", drinks, 0, True),
                ("Hidden item", "1 pack", private, 8, True),
                ("Not needed", "473ml", drinks, 0, False),
            ]:
                product = Product(name=name, size=size, department_id=department.id)
                db.session.add(product)
                db.session.flush()
                db.session.add(StockState(
                    product_id=product.id, order_quantity=quantity, out_of_stock=out,
                ))
            for index in range(12):
                product = Product(name=f"Shelf item {index:02}", size="1 pack", department_id=dairy.id)
                db.session.add(product)
                db.session.flush()
                db.session.add(StockState(product_id=product.id, order_quantity=0, out_of_stock=False))
            db.session.commit()
            dairy_id, second_id, private_id = dairy.id, second.id, private.id
        server = make_server("127.0.0.1", 0, app, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.add_init_script("""
                    window.shared = null;
                    Object.defineProperty(navigator, 'share', {
                        configurable: true, writable: true,
                        value: async data => { window.shared = data; }
                    });
                    Object.defineProperty(navigator, 'clipboard', {
                        configurable: true,
                        value: {writeText: async text => { window.copied = text; }}
                    });
                """)
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("input[name=password]").fill("test-password")
                page.locator("button[type=submit]").click()
                page.locator("#store-select:not([disabled])").wait_for()
                expect(page).to_have_title("Inventory | Qmarket Stockroom")
                expect(page.locator(".brand")).to_have_attribute("aria-label", "Qmarket Stockroom")
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({"width": width, "height": 844})
                    brand = page.locator(".brand").bounding_box()
                    logout = page.locator(".sign-out").bounding_box()
                    if width <= 800:
                        self.assertLessEqual(brand["x"] + brand["width"], logout["x"])
                self.check_navigation(page, dairy_id)
                page.locator("[data-view=orders]").click()

                def preview():
                    page.locator("#view-order-text").click()
                    page.locator("#order-text-dialog[open]").wait_for()
                    return page.locator("#order-text").input_value()

                def close():
                    page.locator("#close-order-text").click()
                    expect(page.locator("#order-text")).to_have_value("")

                text = preview()
                self.assertIn("Market One", text)
                self.assertIn("Milk (1 ltr) - Qty: 3", text)
                self.assertIn("Cola (355ml) - Qty: Not specified", text)
                self.assertNotIn("out of stock", text.lower())
                self.assertNotIn("products to buy", text)
                self.assertNotIn("Quantities are", text)
                self.assertNotIn("Prepared:", text)
                self.assertNotIn("Hidden item", text)
                self.assertNotIn("Not needed", text)
                page.locator("#copy-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("Copied.")
                self.assertEqual(page.evaluate("window.copied"), text)
                page.locator("#share-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("selected app")
                self.assertEqual(page.evaluate("window.shared.text"), text)
                page.evaluate("""() => {
                    navigator.clipboard.writeText = async () => {
                        throw new DOMException('Denied', 'NotAllowedError');
                    };
                }""")
                page.locator("#copy-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("Automatic copy")
                self.assertEqual(page.locator("#order-text").evaluate(
                    "(el) => el.selectionEnd - el.selectionStart"), len(text))
                page.evaluate("""() => {
                    navigator.share = async () => {
                        throw new DOMException('Canceled', 'AbortError');
                    };
                }""")
                page.locator("#share-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("canceled")
                close()
                self.assertEqual(page.locator("#order-text").input_value(), "")
                page.locator("#order-department").select_option(str(dairy_id))
                filtered = preview()
                self.assertIn("Milk", filtered)
                self.assertNotIn("Cola", filtered)
                with page.expect_download() as download:
                    page.locator("#export").click()
                self.assertTrue(download.value.suggested_filename.endswith(".csv"))
                with open(download.value.path(), encoding="utf-8-sig") as exported:
                    csv = exported.read()
                self.assertIn("Milk", csv)
                self.assertNotIn("Cola", csv)
                expect(page.locator("#order-text-status")).to_have_text("Shopping list exported.")
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                    bounds = page.locator("#order-text-dialog").bounding_box()
                    self.assertGreaterEqual(bounds["x"], 0)
                    self.assertLessEqual(bounds["x"] + bounds["width"], width)
                close()
                page.locator("#store-select").select_option(str(second_id))
                page.locator("#view-order-text:not([disabled])").wait_for()
                text = preview()
                self.assertIn("Market Two", text)
                self.assertIn("Hidden item", text)
                self.assertNotIn("Milk", text)
                close()
                page.locator("[data-view=inventory]").click()
                page.locator("#next-department").click()
                expect(page.locator("#product-card h2")).to_have_text("Hidden item")
                expect(page.locator("#card-progress")).to_have_text("Product 1 of 1")
                expect(page.locator("#next-product")).to_be_disabled()
                expect(page.locator("#previous-product")).to_be_disabled()
                expect(page.locator("#next-department")).to_be_disabled()
                expect(page.locator("#previous-department")).to_be_disabled()
                page.locator("[data-view=orders]").click()
                page.evaluate("() => { navigator.share = undefined; }")
                preview()
                self.assertTrue(page.locator("#share-order-text").is_hidden())
                close()
                with app.app_context():
                    for name, size, quantity in [("Milk", "1 ltr", 4), ("Milk", "473ml", 9), ("Cola", "355ml", 2)]:
                        product = Product(name=name, size=size, department_id=private_id)
                        db.session.add(product)
                        db.session.flush()
                        db.session.add(StockState(product_id=product.id, order_quantity=quantity, out_of_stock=False))
                    db.session.commit()
                page.locator("[data-view=combined]").click()
                expect(page.locator("#combined-panel")).to_be_visible()
                expect(page.locator("section.store-toolbar")).to_be_hidden()
                expect(page.locator("#all-stores-context")).to_be_visible()
                expect(page.locator("#combined-rows tr")).to_have_count(4)
                milk_row = page.locator("#combined-rows tr").filter(has_text="1 ltr")
                expect(milk_row).to_contain_text("7")
                cola_row = page.locator("#combined-rows tr").filter(has_text="Cola")
                expect(cola_row).to_contain_text("2 + unspecified")
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                combined = preview()
                self.assertIn("Shopping list - All stores", combined)
                self.assertIn("Milk (1 ltr) - Qty: 7", combined)
                self.assertIn("Milk (473ml) - Qty: 9", combined)
                self.assertIn("Cola (355ml) - Qty: 2 + unspecified", combined)
                self.assertNotIn("Prepared:", combined)
                page.evaluate("() => { navigator.clipboard.writeText = async text => {window.copied = text;}; }")
                page.locator("#copy-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("Copied.")
                self.assertEqual(page.evaluate("window.copied"), combined)
                with page.expect_download() as combined_download:
                    page.locator("#export").click()
                self.assertEqual(combined_download.value.suggested_filename, "shopping-list-all-stores.csv")
                close()
                page.locator("#combined-department").select_option("Dairy")
                expect(page.locator("#combined-rows tr")).to_have_count(1)
                expect(page.locator("#combined-rows")).to_contain_text("Market One")
                expect(page.locator("#combined-rows")).not_to_contain_text("Market Two")
                page.locator("#refresh-combined").click()
                expect(page.locator("#combined-department")).to_have_value("Dairy")
                page.evaluate("() => { navigator.share = async data => { window.shared = data; }; }")
                filtered = preview()
                self.assertIn("Department: Dairy", filtered)
                self.assertIn("Milk (1 ltr) - Qty: 3", filtered)
                self.assertNotIn("Cola", filtered)
                self.assertNotIn("473ml", filtered)
                expect(page.locator("#missing-quantities")).to_be_hidden()
                page.locator("#copy-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("Copied.")
                self.assertEqual(page.evaluate("window.copied"), filtered)
                page.locator("#share-order-text").click()
                expect(page.locator("#order-text-status")).to_contain_text("List passed")
                self.assertEqual(page.evaluate("window.shared.text"), filtered)
                with page.expect_download() as filtered_download:
                    page.locator("#export").click()
                with open(filtered_download.value.path(), encoding="utf-8", newline="") as file:
                    rows = list(DictReader(file))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["Quantity needed"], "3")
                self.assertEqual(rows[0]["Stores"], "Market One")
                close()
                page.route("**/api/orders/combined?*", lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"error":"Filter unavailable"}'))
                page.locator("#combined-department").select_option("Private")
                expect(page.locator("#notice")).to_have_text("Filter unavailable")
                expect(page.locator("#combined-department")).to_have_value("Dairy")
                expect(page.locator("#combined-rows tr")).to_have_count(1)
                page.unroute("**/api/orders/combined?*")
                page.locator("#combined-department").select_option("Private")
                expect(page.locator("#combined-rows tr")).to_have_count(4)
                expect(page.locator("#combined-rows")).not_to_contain_text("unspecified")
                page.locator("#combined-department").select_option("Empty")
                expect(page.locator("#combined-empty")).to_be_visible()
                expect(page.locator("#view-order-text")).to_be_disabled()
                page.locator("#combined-department").select_option("")
                expect(page.locator("#combined-rows tr")).to_have_count(4)
                expect(page.locator("#combined-rows").filter(has_text="Cola")).to_contain_text("2 + unspecified")
                page.reload()
                expect(page.locator("#combined-panel")).to_be_visible()
                expect(page.locator("#combined-rows tr")).to_have_count(4)
                page.locator("[data-view=orders]").click()
                page.route("**/api/inventory?*", lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"error":"Unavailable"}'))
                page.locator("#view-order-text").click()
                expect(page.locator("#notice")).to_have_text("Unavailable")
                self.assertFalse(page.locator("#order-text-dialog").is_visible())
                page.route("**/api/products/*/remove-order?*", lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"error":"Unavailable"}'))
                page.get_by_role("button", name="Remove Milk (1 ltr) from order", exact=True).click()
                expect(page.locator("#order-save-state")).to_have_text("Change not saved.")
                expect(page.get_by_role("button", name="Remove Milk (1 ltr) from order", exact=True)).to_be_visible()
                page.unroute("**/api/inventory?*")
                page.unroute("**/api/products/*/remove-order?*")
                for label in ["Milk (1 ltr)", "Hidden item (1 pack)"]:
                    button = page.get_by_role("button", name=f"Remove {label} from order", exact=True)
                    button.click()
                    expect(button).to_have_count(0)
                    expect(page.locator("#notice")).to_contain_text("Product information has been kept.")
                with app.app_context():
                    for name in ["Milk", "Hidden item"]:
                        product = db.session.scalar(db.select(Product).where(
                            Product.department_id == private_id, Product.name == name, Product.size == ("1 ltr" if name == "Milk" else "1 pack")))
                        self.assertIsNotNone(product)
                        stock = db.session.get(StockState, product.id)
                        self.assertFalse(stock.out_of_stock)
                        self.assertEqual(stock.order_quantity, 0)
                    original = db.session.scalar(db.select(Product).where(
                        Product.department_id == dairy_id, Product.name == "Milk"))
                    self.assertEqual(db.session.get(StockState, original.id).order_quantity, 3)
                page.reload()
                expect(page.locator("#order-rows tr")).to_have_count(2)
                for width in [320, 390, 768, 1440]:
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.locator("[data-view=combined]").click()
                expect(page.locator("#combined-rows tr").filter(has_text="1 ltr")).to_contain_text("3")
                expect(page.locator("#combined-rows button")).to_have_count(0)
                browser.close()
        finally:
            server.shutdown()
            thread.join()
            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    def check_navigation(self, page, dairy_id):
        expect(page.locator("#previous-department")).to_be_disabled()
        page.locator("#next-department").click()
        expect(page.locator("#inventory-department")).to_have_value(str(dairy_id))
        expect(page.locator("#previous-department")).to_be_disabled()
        for width in [320, 390, 768, 1440]:
            page.set_viewport_size({"width": width, "height": 844})
            page.evaluate("window.scrollTo(0, 800)")
            bounds = page.locator("section.store-toolbar").bounding_box()
            self.assertGreaterEqual(bounds["y"], 64 if width <= 800 else 0)
            self.assertLess(bounds["y"], 170)
            self.assertLess(bounds["y"] + bounds["height"], 844)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            page.evaluate("window.scrollTo(0, 0)")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#next-department").click()
        page.locator("#flashcard-view").click()
        expect(page.locator("#inventory-table")).to_be_hidden()
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 2")
        expect(page.locator("#product-card h2")).to_have_text("Cola")
        expect(page.locator("#previous-product")).to_be_disabled()
        page.locator("#next-product").click()
        expect(page.locator("#card-progress")).to_have_text("Product 2 of 2")
        expect(page.locator("#next-product")).to_be_disabled()
        expect(page.locator("#card-end")).to_be_visible()
        page.locator("#product-card .quantity-input").fill("7")
        page.locator("#product-card h2").click()
        expect(page.locator("#save-state")).to_have_text("All changes saved.")
        expect(page.locator("#card-progress")).to_have_text("Product 2 of 2")
        page.locator("#list-view").click()
        expect(page.locator("#inventory-rows .quantity-input").nth(1)).to_have_value("7")
        page.locator("#flashcard-view").click()
        expect(page.locator("#product-card .quantity-input")).to_have_value("7")
        page.locator("#product-card .quantity-input").fill("0")
        page.locator("#product-card h2").click()
        expect(page.locator("#save-state")).to_have_text("All changes saved.")
        page.locator("#product-card input[type=checkbox]").check()
        expect(page.locator("#save-state")).to_have_text("All changes saved.")
        page.locator("#product-card input[type=checkbox]").uncheck()
        expect(page.locator("#save-state")).to_have_text("All changes saved.")
        page.locator("#product-card").focus()
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 2")
        page.keyboard.press("ArrowRight")
        expect(page.locator("#card-progress")).to_have_text("Product 2 of 2")
        card = page.locator("#product-card").bounding_box()
        page.mouse.move(card["x"] + 40, card["y"] + 25)
        page.mouse.down()
        page.mouse.move(card["x"] + 150, card["y"] + 25, steps=8)
        page.mouse.up()
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 2")
        def fail_save(route):
            if route.request.method == "PUT":
                route.fulfill(status=409, content_type="application/json",
                              body='{"error":"Inventory changed. Reload and try again."}')
            else:
                route.continue_()
        page.route("**/api/inventory?*", fail_save)
        page.locator("#product-card .quantity-input").fill("2")
        page.evaluate("""() => {
            document.querySelector('#product-card .quantity-input').dispatchEvent(new Event('change'));
            return moveProduct(1);
        }""")
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 2")
        expect(page.locator("#save-state")).to_have_text("Change not saved.")
        expect(page.locator("#product-card .quantity-input")).to_have_value("0")
        page.unroute("**/api/inventory?*", fail_save)
        page.locator("#product-card .quantity-input").fill("2")
        page.evaluate("""() => {
            document.querySelector('#product-card .quantity-input').dispatchEvent(new Event('change'));
            return moveProduct(1);
        }""")
        expect(page.locator("#card-progress")).to_have_text("Product 2 of 2")
        page.locator("#previous-product").click()
        expect(page.locator("#product-card .quantity-input")).to_have_value("2")
        page.locator("#product-card .quantity-input").fill("0")
        page.locator("#product-card h2").click()
        expect(page.locator("#save-state")).to_have_text("All changes saved.")
        page.locator("#next-department").click()
        expect(page.locator("#flashcards")).to_be_hidden()
        expect(page.locator("#inventory-empty h3")).to_have_text("No products in this department")
        expect(page.locator("#next-department")).to_be_disabled()
        page.locator("#previous-department").click()
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 2")
        page.locator("#previous-department").click()
        expect(page.locator("#card-progress")).to_have_text("Product 1 of 13")
