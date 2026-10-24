import unittest

import test_employee_workflow as workflow


@unittest.skipIf(workflow.sync_playwright is None, "Requires Playwright and Chromium")
class ShoppingWorkflowTests(unittest.TestCase):
    setUp = workflow.EmployeeWorkflowTests.setUp
    cleanup_server = workflow.EmployeeWorkflowTests.cleanup_server
    employee = workflow.EmployeeWorkflowTests.employee
    state = workflow.EmployeeWorkflowTests.state
    update = workflow.EmployeeWorkflowTests.update

    def orders(self, page=None):
        page = page or self.page
        page.locator("[data-view=orders]").click()
        workflow.expect(page.locator("#orders-panel")).to_be_visible()
        if page.locator("#shopping-mode").get_attribute("aria-pressed") == "false":
            page.locator("#shopping-mode").click()

    def pickup(self, name, page=None, combined=False):
        return (page or self.page).locator(
            "#combined-rows" if combined else "#order-rows"
        ).get_by_role("checkbox", name=f"{name} (355ml): Picked up", exact=True)

    def test_shared_pickups_breakdowns_and_changed_demand(self):
        page = self.page
        alpha = next(item for item in self.state() if item["name"] == "Alpha")
        self.update(page, alpha, order_quantity=3)
        self.orders()
        before = next(item for item in self.state() if item["name"] == "Alpha")
        self.pickup("Alpha").check()
        workflow.expect(self.pickup("Alpha")).to_be_checked()
        after = next(item for item in self.state() if item["name"] == "Alpha")
        self.assertEqual(after["order_quantity"], before["order_quantity"])
        self.assertEqual(after["checked"], before["checked"])
        self.assertEqual(after["out_of_stock"], before["out_of_stock"])
        teammate = self.employee()
        self.orders(teammate)
        workflow.expect(self.pickup("Alpha", teammate)).to_be_checked()
        page.locator("[data-view=combined]").click()
        row = page.locator("#combined-rows tr").filter(has_text="Alpha")
        workflow.expect(row).to_contain_text("First store: 3")
        workflow.expect(row).to_contain_text("Second store: 9")
        workflow.expect(row).to_contain_text("Partly picked up")
        self.assertTrue(self.pickup("Alpha", combined=True).evaluate("(input) => input.indeterminate"))
        self.pickup("Alpha", combined=True).check()
        workflow.expect(self.pickup("Alpha", combined=True)).to_be_checked()
        self.assertTrue(self.state(store_id=self.other_id)[0]["picked_up"])
        teammate.locator("#store-select").select_option(str(self.other_id))
        workflow.expect(self.pickup("Alpha", teammate)).to_be_checked()
        teammate.locator("#order-rows input.quantity-input").fill("10")
        teammate.locator("#order-rows input.quantity-input").press("Tab")
        workflow.expect(self.pickup("Alpha", teammate)).not_to_be_checked()
        page.locator("#refresh-combined").click()
        workflow.expect(row).to_contain_text("Second store: 10")
        workflow.expect(row).to_contain_text("Partly picked up")
        page.locator("#combined-department").select_option("Food")
        workflow.expect(page.locator("#combined-rows tr")).to_have_count(1)
        workflow.expect(self.pickup("Delta", combined=True)).not_to_be_checked()
        for width in [320, 390, 768, 1440]:
            page.set_viewport_size({"width": width, "height": 844})
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        page.locator("#view-order-text").click()
        workflow.expect(page.locator("#order-text-dialog")).to_be_visible()
        self.assertIn("First store: 2", page.locator("#order-text").input_value())
        self.assertNotIn("Second store", page.locator("#order-text").input_value())

    def test_failed_and_stale_pickups_do_not_change_inventory_or_freshness(self):
        page = self.page
        self.orders()
        freshness = page.locator("#inventory-freshness").inner_text()
        before = self.state()
        page.route("**/api/orders/picked-up?*", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"error":"Pickup unavailable"}'))
        self.pickup("Beta").click()
        workflow.expect(page.locator("#notice")).to_have_text("Pickup unavailable")
        workflow.expect(self.pickup("Beta")).not_to_be_checked()
        self.assertEqual(self.state(), before)
        workflow.expect(page.locator("#inventory-freshness")).to_have_text(freshness)
        page.unroute("**/api/orders/picked-up?*")
        teammate = self.employee()
        beta = next(item for item in self.state(teammate) if item["name"] == "Beta")
        self.update(teammate, beta, order_quantity=8)
        self.pickup("Beta").click()
        workflow.expect(page.locator("#notice")).to_contain_text("changed")
        workflow.expect(self.pickup("Beta")).not_to_be_checked()
        page.locator("#refresh-orders").click()
        workflow.expect(page.locator("#order-rows tr").filter(has_text="Beta").locator(".quantity-input")).to_have_value("8")
        workflow.expect(page.locator("#inventory-freshness")).not_to_contain_text("not recorded")

    def test_shelf_order_navigation_and_stale_reorder(self):
        page = self.page
        teammate = self.employee()
        for current in [page, teammate]:
            current.locator("[data-view=departments]").click()
            current.locator("#arrange-shelves").click()
        page.get_by_role("button", name="Move up Food", exact=True).click()
        workflow.expect(page.locator("#department-cards h3")).to_have_text(["Food", "Drinks"])
        teammate.get_by_role("button", name="Move up Food", exact=True).click()
        workflow.expect(teammate.locator("#notice")).to_contain_text("changed")
        workflow.expect(teammate.locator("#department-cards h3")).to_have_text(["Drinks", "Food"])
        page.locator("#catalog-department").select_option(str(self.department_id))
        page.get_by_role("button", name="Move up Gamma (355ml)", exact=True).click()
        workflow.expect(page.locator("#catalog-rows .product-name")).to_have_text(["Alpha", "Gamma", "Beta"])
        page.get_by_role("button", name="Move up Gamma (355ml)", exact=True).click()
        workflow.expect(page.locator("#catalog-rows .product-name")).to_have_text(["Gamma", "Alpha", "Beta"])
        page.locator("#arrange-shelves").click()
        page.locator("[data-view=inventory]").click()
        page.locator("#next-department").click()
        workflow.expect(page.locator("#inventory-department")).to_have_value(str(self.food_id))
        page.locator("#next-department").click()
        workflow.expect(page.locator("#inventory-department")).to_have_value(str(self.department_id))
        workflow.expect(page.locator("#inventory-rows .product-name")).to_have_text(["Gamma", "Alpha", "Beta"])
        page.locator("#flashcard-view").click()
        workflow.expect(page.locator("#product-card h2")).to_have_text("Gamma")
        self.orders()
        workflow.expect(page.locator("#order-rows .product-name")).to_have_text(["Delta", "Gamma", "Beta"])
        page.reload()
        workflow.expect(page.locator("#order-rows .product-name")).to_have_text(["Delta", "Gamma", "Beta"])
        self.assertEqual([item["order_quantity"] for item in self.state()], [2, 0, 0, 4])

    def test_copy_catalog_starts_independent_and_timestamps_persist(self):
        page = self.page
        workflow.expect(page.locator("#inventory-freshness")).to_contain_text("not recorded")
        self.orders()
        self.pickup("Beta").check()
        workflow.expect(self.pickup("Beta")).to_be_checked()
        workflow.expect(page.locator("#inventory-freshness")).not_to_contain_text("not recorded")
        page.locator("[data-view=departments]").click()
        page.locator("#arrange-shelves").click()
        page.get_by_role("button", name="Move up Food", exact=True).click()
        workflow.expect(page.locator("#department-cards h3")).to_have_text(["Food", "Drinks"])
        source = self.state()
        saved_time = page.request.get(f"{self.base}/api/inventory?store_id={self.store_id}").json()["last_updated_at"]
        page.locator("#store-management summary").click()
        page.locator("#add-store").click()
        workflow.expect(page.locator("#copy-source")).to_have_value("")
        page.locator("#item-name").fill("Copied store")
        page.locator("#copy-source").select_option(str(self.store_id))
        page.locator("#create-item").click()
        workflow.expect(page.locator("#add-dialog")).to_be_hidden()
        copied_id = int(page.locator("#store-select").input_value())
        self.assertNotEqual(copied_id, self.store_id)
        copied = self.state(store_id=copied_id)
        self.assertEqual([item["name"] for item in copied], [item["name"] for item in source])
        self.assertTrue(all(item["order_quantity"] == 0 and not item["out_of_stock"]
                            and not item["checked"] and not item["picked_up"] for item in copied))
        self.assertTrue({item["id"] for item in copied}.isdisjoint(item["id"] for item in source))
        workflow.expect(page.locator("#department-cards h3")).to_have_text(["Food", "Drinks"])
        workflow.expect(page.locator("#inventory-freshness")).to_contain_text("not recorded")
        token = page.locator('meta[name="csrf-token"]').get_attribute("content")
        response = page.request.put(
            f"{self.base}/api/inventory?store_id={copied_id}",
            headers={"X-CSRF-Token": token}, data={"products": [{**copied[0], "order_quantity": 12}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.state(), source)
        page.locator("#store-select").select_option(str(self.store_id))
        page.reload()
        workflow.expect(page.locator("#inventory-freshness")).not_to_contain_text("not recorded")
        self.assertEqual(page.request.get(f"{self.base}/api/inventory?store_id={self.store_id}").json()["last_updated_at"], saved_time)
