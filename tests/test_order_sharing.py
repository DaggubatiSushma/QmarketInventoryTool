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
            db.session.add_all([dairy, drinks, private])
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
            db.session.commit()
            dairy_id, second_id = dairy.id, second.id
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
                page.locator("input[name=password]").fill("test-password")
                page.locator("button[type=submit]").click()
                page.locator("#store-select:not([disabled])").wait_for()
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
                self.assertIn("Cola (355ml) - Qty: Not specified (out of stock)", text)
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
                page.evaluate("() => { navigator.share = undefined; }")
                preview()
                self.assertTrue(page.locator("#share-order-text").is_hidden())
                close()
                page.route("**/api/inventory?*", lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"error":"Unavailable"}'))
                page.locator("#view-order-text").click()
                expect(page.locator("#notice")).to_have_text("Unavailable")
                self.assertFalse(page.locator("#order-text-dialog").is_visible())
                browser.close()
        finally:
            server.shutdown()
            thread.join()
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
