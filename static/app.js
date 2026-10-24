"use strict";

const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
const pages = {
  inventory: ["Inventory", "Choose a department, mark stock-outs, and enter what you need."],
  departments: ["Departments", "Manage departments and product information, all in one place."],
  orders: ["Order list", "What to buy for this store. Choose one department or view them all."],
  combined: ["All stores", "One combined shopping list, with quantities totaled across stores."],
};
let data = { departments: [], products: [] };
let view = "inventory";
let busy = false;
let loaded = false;
let stockSave = null;
let dialogKind = "product";
let editingId = null;
let stores = [];
let activeStore = null;
let inventoryView = "list";
let cardContext = "";
let cardProductId = null;
let combinedProducts = [];
let combinedDepartment = "";
let shoppingMode = false;
let arrangingShelves = false;
let inventoryRefreshedAt = null;
let combinedRefreshedAt = null;
let combinedUpdatedAt = null;
const navigationKey = "qmarket-navigation-v1";
let navigation = { stores: {}, cards: {} };
let navigationStorageAvailable = true;
const undoActions = new Map();

function storageNotice(message) {
  $("#navigation-storage-notice").textContent = message;
  $("#navigation-storage-notice").hidden = false;
}

function readNavigation() {
  try {
    const saved = JSON.parse(localStorage.getItem(navigationKey) || "null");
    if (saved !== null) {
      if (typeof saved !== "object" || Array.isArray(saved) ||
          !saved.stores || typeof saved.stores !== "object" || Array.isArray(saved.stores) ||
          !saved.cards || typeof saved.cards !== "object" || Array.isArray(saved.cards)) {
        throw new SyntaxError("Invalid saved navigation");
      }
      navigation = saved;
    }
  } catch (error) {
    if (!(error instanceof DOMException || error instanceof SyntaxError)) throw error;
    navigationStorageAvailable = !(error instanceof DOMException);
    storageNotice("Your saved place could not be loaded. Navigation still works; this device may not remember your place.");
  }
}

function rememberNavigation() {
  if (!loaded) return;
  navigation.store = activeStore;
  navigation.view = view;
  navigation.inventoryView = inventoryView;
  navigation.stores[activeStore] = {
    inventory: $("#inventory-department").value,
    catalog: $("#catalog-department").value,
    orders: $("#order-department").value,
  };
  if (cardProductId !== null) navigation.cards[cardContext] = cardProductId;
  if (!navigationStorageAvailable) return;
  try {
    // Only navigation identifiers are stored, never product information or quantities.
    localStorage.setItem(navigationKey, JSON.stringify(navigation));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    navigationStorageAvailable = false;
    storageNotice("This device cannot save your place. Navigation and inventory saving still work.");
  }
}

function restoreStoreNavigation() {
  const saved = navigation.stores[activeStore] || {};
  for (const [key, selector] of [["inventory", "#inventory-department"], ["catalog", "#catalog-department"], ["orders", "#order-department"]]) {
    const value = String(saved[key] || "");
    $(selector).value = data.departments.some((item) => String(item.id) === value) ? value : "";
  }
  $("#inventory-search").value = "";
}

function renderUndo() {
  const action = undoActions.get(activeStore);
  $("#undo-banner").hidden = !action || view === "combined";
  if (!action) return;
  const expired = Date.now() >= action.expires;
  $("#undo-message").textContent = expired ? `${action.label} Undo has expired.` : action.label;
  $("#undo-action").disabled = busy || expired;
}

function scopedPath(path, storeId = activeStore) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("store_id", storeId);
  return url.pathname + url.search;
}

function storeName() {
  return stores.find((store) => store.id === activeStore)?.name || "";
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notify(message, error = false) {
  $("#notice").textContent = message;
  $("#notice").classList.toggle("error", error);
  $("#notice").hidden = false;
}

async function checkResponse(response) {
  if (response.status === 401) throw new Error("Your session expired. Reload the page to sign in again.");
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || `Request failed (${response.status}). Please try again.`);
  }
  return response;
}

async function api(path, method = "GET", body) {
  if (!navigator.onLine) throw new Error("You're offline. Reconnect before loading or saving inventory.");
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  await checkResponse(response);
  return response.json();
}

function selectedProducts() {
  const id = Number($("#inventory-department").value);
  return data.products.filter((product) => product.department_id === id);
}

function visibleInventoryProducts() {
  const query = $("#inventory-search").value.trim().toLocaleLowerCase();
  return selectedProducts().filter((product) =>
    `${product.name} ${product.size}`.toLocaleLowerCase().includes(query));
}

function shoppingProducts(filtered = true) {
  const departmentId = filtered ? $("#order-department").value : "";
  return data.products.filter((product) => (product.out_of_stock || product.order_quantity > 0) &&
    (!departmentId || product.department_id === Number(departmentId)));
}

function setBusy(value) {
  busy = value;
  for (const selector of ["#add-department", "#add-product", "#inventory-department", "#catalog-department", "#order-department", "#combined-department", "#create-item", "#store-select", "#add-store", "#rename-store", "#copy-source", "#arrange-shelves", "#shopping-mode", "#refresh-orders"]) {
    $(selector).disabled = value || !loaded;
  }
  $("#reset-department").disabled = value || !loaded || !selectedProducts().length;
  $("#start-check").disabled = value || !loaded || !selectedProducts().length;
  $("#refresh-inventory").disabled = value || !loaded;
  $("#inventory-search").disabled = value || !loaded || !$("#inventory-department").value;
  const sendingProducts = view === "combined" ? combinedProducts : shoppingProducts();
  $("#export").disabled = value || !loaded || !sendingProducts.length;
  $("#view-order-text").disabled = value || !loaded || !sendingProducts.length;
  $("#refresh-combined").disabled = value || !loaded;
  const departmentIndex = data.departments.findIndex((item) => String(item.id) === $("#inventory-department").value);
  $("#previous-department").disabled = value || !loaded || departmentIndex <= 0;
  $("#next-department").disabled = value || !loaded || departmentIndex >= data.departments.length - 1;
  $("#list-view").disabled = $("#flashcard-view").disabled = value || !loaded;
  const products = visibleInventoryProducts();
  const cardIndex = products.findIndex((product) => product.id === cardProductId);
  $("#previous-product").disabled = value || !loaded || cardIndex <= 0;
  $("#next-product").disabled = value || !loaded || cardIndex < 0 || cardIndex >= products.length - 1;
  document.querySelectorAll("#inventory-rows input, #inventory-rows button, #product-card input, #product-card button, #order-rows input, #order-rows button, #combined-rows input, #catalog-rows button, #department-cards button").forEach((control) => {
    control.disabled = value || control.dataset.unavailable === "true";
  });
  renderUndo();
}

function fillDepartments() {
  for (const [selector, placeholder] of [
    ["#inventory-department", "Select a department"],
    ["#catalog-department", "All departments"],
    ["#order-department", "All departments"],
    ["#item-department", "Select a department"],
  ]) {
    const select = $(selector);
    const previous = select.value;
    select.replaceChildren(new Option(placeholder, ""));
    data.departments.forEach((department) => select.add(new Option(department.name, department.id)));
    select.value = data.departments.some((department) => String(department.id) === previous) ? previous : "";
  }
}

function identity(product) {
  const cell = element("td");
  cell.append(element("div", product.name, "product-name"));
  return cell;
}

function productLabel(product) {
  return `${product.name}${product.size ? ` (${product.size})` : ""}`;
}

function labelCards(tableId) {
  const table = document.getElementById(tableId);
  const headings = Array.from(table.querySelectorAll("th"), (heading) => heading.textContent);
  table.setAttribute("role", "table");
  table.querySelectorAll("thead, tbody").forEach((group) => group.setAttribute("role", "rowgroup"));
  table.querySelectorAll("tr").forEach((row) => row.setAttribute("role", "row"));
  table.querySelectorAll("th").forEach((heading) => heading.setAttribute("role", "columnheader"));
  table.querySelectorAll("tbody tr").forEach((row) => {
    Array.from(row.children).forEach((cell, index) => {
      cell.dataset.label = headings[index];
      cell.setAttribute("role", "cell");
    });
  });
}

function quantityInput(product) {
  const input = element("input");
  input.type = "text";
  input.inputMode = "numeric";
  input.pattern = "[0-9]+";
  input.maxLength = 7;
  input.required = true;
  input.autocomplete = "off";
  input.className = "quantity-input";
  input.value = product.order_quantity;
  input.setAttribute("aria-label", `${productLabel(product)}: Qty needed`);
  input.addEventListener("change", () => {
    const value = input.value.trim();
    if (!/^[0-9]+$/.test(value) || Number(value) > 1000000) {
      input.value = product.order_quantity;
      notify("Quantity was not changed. Enter a whole number from 0 to 1,000,000.", true);
      return;
    }
    saveStock(product, { order_quantity: Number(value) });
  });
  return input;
}

function quantityCell(product) {
  const cell = element("td");
  cell.append(quantityInput(product));
  return cell;
}

function stockToggle(product) {
  const label = element("label", undefined, "stock-toggle");
  const checkbox = element("input");
  checkbox.type = "checkbox";
  checkbox.checked = product.out_of_stock;
  checkbox.setAttribute("aria-label", `${productLabel(product)}: Out of stock`);
  checkbox.addEventListener("change", () => saveStock(product, { out_of_stock: checkbox.checked }));
  label.append(checkbox, element("span", product.out_of_stock ? "Out of stock" : "Not marked", product.out_of_stock ? "status out" : "muted small"));
  return label;
}

function checkControl(product, advance = false) {
  const wrapper = element("div", undefined, "product-check");
  wrapper.append(element("span", product.checked ? "Checked" : "Not checked", product.checked ? "checked-label" : "muted small"));
  if (!product.checked) {
    const needed = product.out_of_stock || product.order_quantity > 0;
    const button = element("button", needed ? "Mark checked" : "Checked - nothing needed");
    button.setAttribute("aria-label", `${productLabel(product)}: ${needed ? "Mark checked" : "Checked - nothing needed"}`);
    button.addEventListener("click", async () => {
      if (!await ready()) return;
      const current = data.products.find((item) => item.id === product.id);
      if (!current) { notify("This product is no longer available. Refresh inventory.", true); return; }
      if (await saveStock(current, { checked: true })) {
        if (advance) await moveProduct(1);
      }
    });
    wrapper.append(button);
  }
  return wrapper;
}

function renderCheckProgress() {
  const products = selectedProducts();
  const count = products.filter((product) => product.checked).length;
  $("#department-progress").textContent = $("#inventory-department").value
    ? `${count} of ${products.length} checked${products.length && count === products.length ? " - department complete" : ""}`
    : "Select a department to start checking.";
  $("#department-progress-bar").hidden = !products.length;
  $("#department-progress-bar").max = products.length || 1;
  $("#department-progress-bar").value = count;
}

function renderFlashcard(products) {
  const context = `${activeStore}:${$("#inventory-department").value}`;
  if (cardContext !== context) {
    cardContext = context;
    cardProductId = navigation.cards[context] ?? null;
  }
  const product = products.find((item) => item.id === cardProductId) || products[0];
  cardProductId = product?.id ?? null;
  const card = $("#product-card");
  card.replaceChildren();
  $("#flashcards").hidden = inventoryView !== "flashcards" || !product;
  $("#list-view").setAttribute("aria-pressed", String(inventoryView === "list"));
  $("#flashcard-view").setAttribute("aria-pressed", String(inventoryView === "flashcards"));
  if (!product) {
    $("#card-progress").textContent = "";
    $("#card-end").hidden = true;
    return;
  }
  const index = products.indexOf(product);
  $("#card-progress").textContent = `Product ${index + 1} of ${products.length}`;
  $("#card-end").hidden = index !== products.length - 1;
  $("#card-end").textContent = Number($("#inventory-department").value) === data.departments.at(-1)?.id
    ? "Last product in the last department."
    : "Last product in this department. Use Next department to continue.";
  card.append(element("h2", product.name), element("p", product.size || "--", "muted"), stockToggle(product));
  const quantity = element("label", "Qty needed", "card-quantity");
  quantity.append(quantityInput(product));
  card.append(quantity, checkControl(product, true));
  rememberNavigation();
}

function renderInventory() {
  const products = visibleInventoryProducts();
  const body = $("#inventory-rows");
  body.replaceChildren();
  for (const product of products) {
    const row = element("tr");
    row.append(identity(product), element("td", product.size || "--"));
    const stock = element("td");
    stock.append(stockToggle(product));
    row.append(stock, quantityCell(product));
    const check = element("td");
    check.append(checkControl(product));
    row.append(check);
    body.append(row);
  }
  $("#inventory-table").hidden = products.length === 0 || inventoryView !== "list";
  renderFlashcard(products);
  $("#inventory-empty").hidden = products.length > 0;
  $("#inventory-empty h3").textContent = $("#inventory-department").value
    ? ($("#inventory-search").value.trim() ? "No matching products" : "No products in this department")
    : "Select a department";
  $("#inventory-empty p").textContent = $("#inventory-search").value.trim()
    ? "Try a different name or size, or clear the search to see all products."
    : "Its products will appear here. Add products on the Departments page.";
  renderCheckProgress();
  rememberNavigation();
  labelCards("inventory-table");
}

function renderCatalog() {
  $("#arrange-shelves").setAttribute("aria-pressed", String(arrangingShelves));
  $("#arrange-shelves").textContent = arrangingShelves ? "Done arranging" : "Arrange shelf order";
  $("#shelf-order-help").hidden = !arrangingShelves;
  const cards = $("#department-cards");
  cards.replaceChildren();
  if (!data.departments.length) cards.append(element("p", "Add your first department to get started.", "muted"));
  data.departments.forEach((department) => {
    const products = data.products.filter((product) => product.department_id === department.id);
    const card = element("article", undefined, "department-card");
    card.append(element("h3", department.name), element("p", `${products.length} product${products.length === 1 ? "" : "s"} - ${products.filter((product) => product.checked).length} checked`, "muted"));
    const add = element("button", "+ Add Products");
    add.addEventListener("click", () => openDialog("product", null, department.id));
    const remove = element("button", "Delete", "text-danger");
    remove.dataset.unavailable = String(products.length > 0);
    remove.title = products.length ? "Remove products before deleting this department" : "Delete empty department";
    remove.addEventListener("click", () => removeItem("department", department));
    const actions = element("div", undefined, "catalog-row-actions");
    actions.append(add, remove);
    card.append(actions);
    if (arrangingShelves) card.append(shelfControls(department, data.departments));
    cards.append(card);
  });
  const departmentId = $("#catalog-department").value;
  const products = data.products.filter((product) => !departmentId || product.department_id === Number(departmentId));
  const body = $("#catalog-rows");
  body.replaceChildren();
  products.forEach((product) => {
    const row = element("tr");
    row.append(element("td", product.name, "product-name"), element("td", product.department),
      element("td", product.size || "--"));
    const actions = element("td");
    const edit = element("button", "Edit");
    edit.setAttribute("aria-label", `Edit ${productLabel(product)}`);
    edit.addEventListener("click", () => openDialog("product", product));
    const remove = element("button", "Delete", "text-danger");
    remove.setAttribute("aria-label", `Delete ${productLabel(product)}`);
    remove.addEventListener("click", () => removeItem("product", product));
    const group = element("div", undefined, "catalog-row-actions");
    group.append(edit, remove);
    if (arrangingShelves) {
      group.append(shelfControls(product, data.products.filter((item) => item.department_id === product.department_id), product.department_id));
    }
    actions.append(group);
    row.append(actions);
    body.append(row);
  });
  $("#catalog-table").hidden = products.length === 0;
  $("#catalog-empty").hidden = products.length > 0;
  labelCards("catalog-table");
}

function shelfControls(item, items, departmentId = null) {
  const group = element("div", undefined, "shelf-controls");
  const index = items.findIndex((current) => current.id === item.id);
  for (const [direction, label] of [[-1, "Move up"], [1, "Move down"]]) {
    const button = element("button", label);
    button.setAttribute("aria-label", `${label} ${departmentId === null ? item.name : productLabel(item)}`);
    button.dataset.unavailable = String(index + direction < 0 || index + direction >= items.length);
    button.addEventListener("click", async () => {
      if (!await ready()) return;
      const ids = items.map((current) => current.id);
      [ids[index], ids[index + direction]] = [ids[index + direction], ids[index]];
      const body = { revision: data.store.catalog_revision };
      if (departmentId === null) body.department_ids = ids;
      else { body.department_id = departmentId; body.product_ids = ids; }
      if (await saveInventory(scopedPath("/api/catalog/order"), "PUT", body)) {
        notify("Shelf order saved for this store.");
      }
    });
    group.append(button);
  }
  return group;
}

function pickupControl(product, combined = false) {
  const label = element("label", undefined, "pickup-toggle");
  const input = element("input");
  input.type = "checkbox";
  input.checked = product.picked_up;
  input.indeterminate = combined && !product.picked_up && product.items.some((item) => item.picked_up);
  input.setAttribute("aria-label", `${productLabel(product)}: Picked up`);
  label.append(input, element("span", input.indeterminate ? "Partly picked up" : product.picked_up ? "Picked up" : "Not picked up"));
  input.addEventListener("change", async () => {
    const picked = input.checked;
    if (!await ready()) { input.checked = product.picked_up; return; }
    const path = combined ? combinedPath("/api/orders/combined/picked-up") : scopedPath("/api/orders/picked-up");
    const body = combined
      ? { name: product.name, size: product.size, picked_up: picked, products: product.items.map((item) => ({ id: item.id, revision: item.revision })) }
      : { products: [{ id: product.id, revision: product.revision, picked_up: picked }] };
    if (await saveInventory(path, "PUT", body, null, combined ? "combined" : "store")) {
      notify(picked ? "Marked picked up for the team. Order quantities are unchanged." : "Picked-up mark cleared for the team.");
    }
  });
  return label;
}

function renderShoppingTools() {
  const products = view === "combined" ? combinedProducts : shoppingProducts();
  const picked = products.filter((product) => product.picked_up).length;
  $("#shopping-mode").setAttribute("aria-pressed", String(shoppingMode));
  $("#shopping-mode").textContent = `Shopping mode: ${shoppingMode ? "on" : "off"}`;
  $("#refresh-orders").hidden = view !== "orders";
  $("#shopping-progress").hidden = !shoppingMode || !["orders", "combined"].includes(view);
  $("#shopping-progress").textContent = `${picked} of ${products.length} products picked up. Shared team ticks do not remove items or change quantities. Changing an item's quantity or stock status clears its tick.`;
  document.querySelectorAll(".shopping-column").forEach((cell) => { cell.hidden = !shoppingMode; });
}

function renderFreshness() {
  const combined = view === "combined";
  const saved = combined ? combinedUpdatedAt : data.last_updated_at;
  const refreshed = combined ? combinedRefreshedAt : inventoryRefreshedAt;
  $("#inventory-freshness").textContent = `${combined ? "All stores" : storeName()} | Last inventory save: ${saved ? new Date(saved).toLocaleString() : "not recorded"} | Refreshed: ${refreshed ? refreshed.toLocaleString() : "not yet"}`;
}

function renderOrders() {
  const products = shoppingProducts();
  const body = $("#order-rows");
  body.replaceChildren();
  products.forEach((product) => {
    const row = element("tr");
    row.append(element("td", product.department), element("td", product.name, "product-name"),
      element("td", product.size || "--"), quantityCell(product));
    const actions = element("td");
    const remove = element("button", "Remove", "text-danger");
    remove.setAttribute("aria-label", `Remove ${productLabel(product)} from order`);
    remove.title = "Clear the stock-out mark and quantity. Product information is kept.";
    remove.addEventListener("click", async () => {
      if (!await ready()) return;
      const current = data.products.find((item) => item.id === product.id);
      if (!current) {
        notify("This product is no longer available. Reload the page.", true);
        return;
      }
      if (await saveInventory(scopedPath(`/api/products/${current.id}/remove-order`), "POST",
          { revision: current.revision }, `Removed ${productLabel(current)} from the order.`)) {
        notify(`${productLabel(current)} removed from this store's order. Product information has been kept.`);
      }
    });
    actions.append(remove);
    row.append(actions);
    const pickup = element("td", undefined, "shopping-column");
    pickup.hidden = !shoppingMode;
    pickup.append(pickupControl(product));
    row.append(pickup);
    row.classList.toggle("picked-up", shoppingMode && product.picked_up);
    row.classList.toggle("needs-quantity", product.out_of_stock && product.order_quantity === 0);
    if (product.out_of_stock && product.order_quantity === 0) {
      row.children[3].append(element("span", "Quantity missing", "quantity-warning"));
    }
    body.append(row);
  });
  $("#order-badge").textContent = shoppingProducts(false).length;
  $("#shopping-count").textContent = `${products.length} product${products.length === 1 ? "" : "s"} to buy`;
  $("#orders-table").hidden = !products.length;
  $("#orders-empty").hidden = products.length > 0;
  $("#orders-empty h3").textContent = $("#order-department").value ? "Nothing to buy in this department" : "Nothing to buy right now";
  labelCards("orders-table");
  renderShoppingTools();
}

function combinedQuantity(product) {
  if (!product.order_quantity) return "Not specified";
  return `${product.order_quantity}${product.unspecified_count ? " + unspecified" : ""}`;
}

function renderCombined() {
  const body = $("#combined-rows");
  body.replaceChildren();
  combinedProducts.forEach((product) => {
    const row = element("tr");
    const breakdown = element("td");
    const storeList = element("div", undefined, "store-quantities");
    for (const store of product.store_quantities) {
      storeList.append(element("div", `${store.store_name}: ${combinedQuantity(store)}`));
    }
    breakdown.append(storeList);
    row.append(element("td", product.name, "product-name"), element("td", product.size || "--"),
      element("td", combinedQuantity(product)), breakdown);
    const pickup = element("td", undefined, "shopping-column");
    pickup.hidden = !shoppingMode;
    pickup.append(pickupControl(product, true));
    row.append(pickup);
    row.classList.toggle("picked-up", shoppingMode && product.picked_up);
    body.append(row);
  });
  $("#combined-table").hidden = !combinedProducts.length;
  $("#combined-empty").hidden = combinedProducts.length > 0;
  $("#combined-empty h3").textContent = combinedDepartment
    ? "Nothing to buy in this department across all stores" : "Nothing to buy across all stores";
  labelCards("combined-table");
  renderShoppingTools();
}

function combinedPath(path, department = combinedDepartment) {
  return `${path}?department_name=${encodeURIComponent(department)}`;
}

async function reloadCombined(department = combinedDepartment) {
  const result = await api(combinedPath("/api/orders/combined", department));
  applyCombined(result, department);
}

function applyCombined(result, department = combinedDepartment) {
  const names = result.departments;
  // Keep a deleted department selected rather than silently broadening the shopping list.
  if (department && !names.includes(department)) names.push(department);
  $("#combined-department").replaceChildren(new Option("All departments", ""),
    ...names.map((name) => new Option(name, name)));
  $("#combined-department").value = department;
  combinedDepartment = department;
  combinedProducts = result.products;
  combinedUpdatedAt = result.last_updated_at;
  combinedRefreshedAt = new Date();
  renderCombined();
  renderFreshness();
}

function render() {
  $("#store-select").replaceChildren(...stores.map((store) => new Option(store.name, store.id)));
  $("#store-select").value = activeStore;
  fillDepartments();
  renderInventory();
  renderCatalog();
  renderOrders();
  renderCombined();
  renderFreshness();
  setBusy(busy);
}

async function reloadData(storeId = activeStore) {
  const result = await api(scopedPath("/api/inventory", storeId));
  const storeChanged = activeStore !== storeId || !loaded;
  data = result;
  inventoryRefreshedAt = new Date();
  activeStore = storeId;
  if (storeChanged) {
    fillDepartments();
    restoreStoreNavigation();
  }
  loaded = true;
  const url = new URL(window.location.href);
  url.searchParams.set("store_id", activeStore);
  window.history.replaceState(null, "", url);
  render();
}

async function reloadStores() {
  stores = (await api("/api/stores")).stores;
}

function saveMessage(message) {
  $("#save-state").textContent = message;
  $("#order-save-state").textContent = message;
}

function saveStock(product, changes) {
  return saveInventory(scopedPath("/api/inventory"), "PUT",
    { products: [{ ...product, ...changes, checked: true }] });
}

function saveInventory(path, method, body, undoLabel = null, scope = "store") {
  if (busy) return;
  setBusy(true);
  saveMessage("Saving...");
  stockSave = (async () => {
    try {
      const result = await api(path, method, body);
      if (scope === "combined") applyCombined(result);
      else { data = result; inventoryRefreshedAt = new Date(); }
      if (undoLabel) {
        undoActions.set(activeStore, { token: data.undo_token, label: undoLabel, expires: Date.now() + 10 * 60 * 1000 });
      }
      saveMessage("All changes saved.");
      $("#notice").hidden = true;
      render();
      return true;
    } catch (error) {
      saveMessage("Change not saved.");
      notify(error.message, true);
      render();
      return false;
    } finally {
      setBusy(false);
      stockSave = null;
    }
  })();
  return stockSave;
}

async function ready() {
  if (stockSave && !await stockSave) return false;
  return loaded && !busy;
}

async function changeView(nextView) {
  if (!await ready()) return;
  if (nextView === "combined") {
    setBusy(true);
    try {
      await reloadCombined();
    } catch (error) {
      notify(error.message, true);
      return;
    } finally { setBusy(false); }
  } else if (["inventory", "departments", "orders"].includes(nextView)) {
    setBusy(true);
    try {
      await reloadData();
    } catch (error) {
      notify(error.message, true);
      return;
    } finally { setBusy(false); }
  }
  view = nextView;
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false");
  });
  for (const name of Object.keys(pages)) $(`#${name}-panel`).hidden = name !== view;
  $("#page-title").textContent = pages[view][0];
  $("#breadcrumb").textContent = pages[view][0];
  $("#page-subtitle").textContent = pages[view][1];
  document.title = `${pages[view][0]} | Qmarket Stockroom`;
  $("#catalog-actions").hidden = view !== "departments";
  $("#order-actions").hidden = !["orders", "combined"].includes(view);
  $("section.store-toolbar").hidden = view === "combined";
  $("#all-stores-context").hidden = view !== "combined";
  const url = new URL(window.location.href);
  url.searchParams.set("view", view);
  window.history.replaceState(null, "", url);
  render();
  rememberNavigation();
  window.scrollTo({ top: 0, behavior: "instant" });
}

function openDialog(kind, product = null, departmentId = null) {
  if (busy || !loaded) return;
  $("#store-management").open = false;
  if (kind === "product" && !data.departments.length) {
    notify("Add a department first.");
    kind = "department";
  }
  dialogKind = kind;
  editingId = product?.id ?? null;
  $("#add-form").reset();
  $("#dialog-title").textContent = `${product ? (kind === "store" ? "Rename" : "Edit") : "Add"} ${kind}`;
  $("#create-item").textContent = product ? `Save ${kind}` : `Add ${kind}`;
  $("#item-name").maxLength = kind === "department" ? 80 : 120;
  $("#product-fields").hidden = kind !== "product";
  $("#store-fields").hidden = kind !== "store" || product !== null;
  $("#copy-source").replaceChildren(new Option("Start with an empty catalog", ""),
    ...stores.map((store) => new Option(store.name, store.id)));
  $("#item-department").required = kind === "product";
  $("#item-department").disabled = product !== null;
  $("#item-size").required = kind === "product";
  $("#dialog-error").hidden = true;
  $("#item-name").value = product?.name ?? "";
  $("#item-size").value = product?.size ?? "";
  $("#item-department").value = product?.department_id ?? departmentId ?? $("#catalog-department").value;
  $("#add-dialog").showModal();
  $("#item-name").focus();
}

async function removeItem(kind, item) {
  if (!await ready()) return;
  const message = kind === "product" ? `Delete "${productLabel(item)}" and its inventory and history? This cannot be undone.` : `Delete department "${item.name}"?`;
  if (!confirm(message)) return;
  setBusy(true);
  try {
    await api(scopedPath(`/api/${kind === "product" ? "products" : "departments"}/${item.id}`), "DELETE");
    await reloadData();
    notify(`${kind === "product" ? "Product" : "Department"} deleted.`);
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
}

$("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const body = { name: $("#item-name").value.trim() };
  if (!body.name) {
    $("#dialog-error").textContent = "Enter a name.";
    $("#dialog-error").hidden = false;
    return;
  }
  if (dialogKind === "product") {
    body.department_id = Number($("#item-department").value);
    body.size = $("#item-size").value.trim();
    if (!body.size) {
      $("#dialog-error").textContent = "Enter the product size or quantity, for example 355ml.";
      $("#dialog-error").hidden = false;
      return;
    }
  }
  if (dialogKind === "store" && !editingId && $("#copy-source").value) {
    body.source_store_id = Number($("#copy-source").value);
  }
  setBusy(true);
  try {
    const collection = { store: "stores", product: "products", department: "departments" }[dialogKind];
    const path = editingId ? `/api/${collection}/${editingId}` : `/api/${collection}`;
    const result = await api(dialogKind === "store" ? path : scopedPath(path), editingId ? "PUT" : "POST", body);
    if (dialogKind === "store") {
      await reloadStores();
      await reloadData(editingId || result.id);
    } else {
      await reloadData();
    }
    $("#add-dialog").close();
    notify(body.source_store_id ? "Store created with an independent catalog. Inventory and shopping progress start empty."
      : `${{ product: "Product information", department: "Department", store: "Store" }[dialogKind]} saved.`);
  } catch (error) {
    $("#dialog-error").textContent = error.message;
    $("#dialog-error").hidden = false;
  } finally { setBusy(false); }
});

$("#reset-department").addEventListener("click", async () => {
  if (!await ready()) return;
  const id = Number($("#inventory-department").value);
  const department = data.departments.find((item) => item.id === id);
  if (!department) return;
  if (!confirm(`Reset "${department.name}" in "${storeName()}"? This clears this department's stock-out marks, quantities, check progress, and picked-up ticks. Products and other stores will not be changed.`)) return;
  if (await saveInventory(scopedPath(`/api/departments/${id}/reset`), "POST",
      { products: selectedProducts().map((product) => ({ id: product.id, revision: product.revision })) },
      `Reset ${department.name}.`)) {
    notify(`${department.name} inventory reset. Products have been kept.`);
  }
});

$("#start-check").addEventListener("click", async () => {
  if (!await ready()) return;
  const id = Number($("#inventory-department").value);
  if (!confirm("Start a new team check for this department? Checked marks will be cleared for everyone. Stock-out marks and order quantities stay unchanged.")) return;
  if (await saveInventory(scopedPath(`/api/departments/${id}/start-check`), "POST",
      { products: selectedProducts().map((product) => ({ id: product.id, revision: product.revision })) })) {
    cardProductId = null;
    delete navigation.cards[cardContext];
    $("#inventory-search").value = "";
    renderInventory();
    setBusy(false);
    notify("New department check started for the team. Order quantities have been kept.");
  }
});

$("#undo-action").addEventListener("click", async () => {
  if (!await ready()) return;
  const action = undoActions.get(activeStore);
  if (!action || Date.now() >= action.expires) {
    notify("Undo has expired.", true);
    renderUndo();
    return;
  }
  if (await saveInventory(scopedPath("/api/inventory/undo"), "POST", { token: action.token })) {
    undoActions.delete(activeStore);
    renderUndo();
    notify("Previous inventory and check progress restored.");
  }
});

function shoppingText() {
  if (view === "combined") {
    return ["Shopping list - All stores", `Department: ${combinedDepartment || "All departments"}`, "", ...combinedProducts.map((product) =>
      `- ${productLabel(product)} - Qty: ${combinedQuantity(product)}\n  ${product.store_quantities.map((store) => `${store.store_name}: ${combinedQuantity(store)}`).join("; ")}`)].join("\n");
  }
  const department = $("#order-department").selectedOptions[0].textContent;
  const lines = [
    `Shopping list - ${storeName()}`,
    `Department: ${department}`,
  ];
  for (const item of data.departments) {
    const products = shoppingProducts().filter((product) => product.department_id === item.id);
    if (!products.length) continue;
    lines.push("", item.name);
    for (const product of products) {
      const quantity = product.order_quantity > 0 ? product.order_quantity : "Not specified";
      lines.push(`- ${productLabel(product)} - Qty: ${quantity}`);
    }
  }
  return lines.join("\n");
}

$("#view-order-text").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    if (view === "combined") await reloadCombined();
    else await reloadData();
    if (!(view === "combined" ? combinedProducts : shoppingProducts()).length) {
      notify(view === "combined" ? "Nothing to share for this department across all stores."
        : "Nothing to share for this store and department.");
      return;
    }
    $("#order-text").value = shoppingText();
    const missing = (view === "combined" ? combinedProducts : shoppingProducts()).filter((product) =>
      view === "combined" ? product.unspecified_count > 0 : product.out_of_stock && product.order_quantity === 0);
    $("#missing-quantity-items").replaceChildren(...missing.map((product) =>
      element("li", `${productLabel(product)}${view === "combined" ? ` - check stores: ${product.stores.join(", ")}` : ""}`)));
    $("#missing-quantities").hidden = !missing.length;
    $("#order-text-help").textContent = view === "combined"
      ? "A combined snapshot of all stores for the selected department filter. Copy, share, or export it for your shopper."
      : "A snapshot of this store and department filter. Copy, share, or export it for your shopper.";
    $("#order-text-status").textContent = "";
    $("#share-order-text").hidden = typeof navigator.share !== "function";
    $("#order-text-dialog").showModal();
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$("#copy-order-text").addEventListener("click", async () => {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText($("#order-text").value);
    $("#order-text-status").textContent = "Copied. Paste into a message or email.";
  } catch (error) {
    $("#order-text").focus();
    $("#order-text").select();
    $("#order-text-status").textContent = "Automatic copy is unavailable. The text is selected; use your device's Copy command.";
  }
});

$("#share-order-text").addEventListener("click", async () => {
  const button = $("#share-order-text");
  button.disabled = true;
  try {
    await navigator.share({ title: "Shopping list", text: $("#order-text").value });
    $("#order-text-status").textContent = "List passed to your selected app.";
  } catch (error) {
    $("#order-text-status").textContent = error.name === "AbortError"
      ? "Sharing canceled. You can still copy the text."
      : "Sharing is unavailable. Use Copy text instead.";
  } finally { button.disabled = false; }
});

$("#close-order-text").addEventListener("click", () => $("#order-text-dialog").close());
$("#review-quantities").addEventListener("click", () => {
  $("#order-text-dialog").close();
  if (view === "orders") {
    const input = $("#order-rows .needs-quantity input");
    input?.focus();
  } else {
    notify("Open the individual store's Order list to enter the missing quantities.");
  }
});
$("#order-text-dialog").addEventListener("close", () => {
  $("#order-text").value = "";
  $("#order-text-status").textContent = "";
  $("#missing-quantity-items").replaceChildren();
  $("#missing-quantities").hidden = true;
});

$("#export").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    const path = view === "combined" ? combinedPath("/api/orders/combined/export")
      : scopedPath(`/api/orders/export?department_id=${encodeURIComponent($("#order-department").value)}`);
    const response = await checkResponse(await fetch(path));
    const url = URL.createObjectURL(await response.blob());
    const link = element("a");
    link.href = url;
    link.download = view === "combined" ? "shopping-list-all-stores.csv" : `shopping-list-store-${activeStore}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $("#order-text-status").textContent = "Shopping list exported.";
  } catch (error) { $("#order-text-status").textContent = error.message; }
  finally { setBusy(false); }
});

$("#refresh-combined").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    await reloadCombined();
    notify("Combined list refreshed.");
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$("#combined-department").addEventListener("change", async (event) => {
  const department = event.target.value;
  if (!await ready()) { event.target.value = combinedDepartment; return; }
  setBusy(true);
  try {
    await reloadCombined(department);
  } catch (error) {
    $("#combined-department").value = combinedDepartment;
    notify(error.message, true);
  } finally { setBusy(false); }
});

$("#refresh-inventory").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    await reloadData();
    notify("Team inventory and check progress refreshed.");
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$("#refresh-orders").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    await reloadData();
    notify("Shopping list and team pickup progress refreshed.");
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$("#shopping-mode").addEventListener("click", async () => {
  if (!await ready()) return;
  shoppingMode = !shoppingMode;
  render();
});

$("#arrange-shelves").addEventListener("click", async () => {
  if (!await ready()) return;
  arrangingShelves = !arrangingShelves;
  renderCatalog();
  setBusy(false);
});

async function moveDepartment(direction) {
  if (!await ready()) return;
  const index = data.departments.findIndex((item) => String(item.id) === $("#inventory-department").value);
  const department = data.departments[index + direction];
  if (!department) return;
  $("#inventory-department").value = department.id;
  $("#inventory-search").value = "";
  renderInventory();
  setBusy(false);
}

async function moveProduct(direction) {
  // A swipe or keyboard action must commit the current field before changing cards.
  const keyboardFocus = document.activeElement === $("#product-card");
  if ($("#product-card").contains(document.activeElement)) document.activeElement.blur();
  if (!await ready()) return;
  const products = visibleInventoryProducts();
  const index = products.findIndex((product) => product.id === cardProductId);
  const product = products[index + direction];
  if (!product) return;
  cardProductId = product.id;
  renderFlashcard(products);
  setBusy(false);
  if (keyboardFocus) $("#product-card").focus({ preventScroll: true });
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    $("#product-card").animate([
      { transform: `translateX(${direction * 30}px)`, opacity: 0 },
      { transform: "translateX(0)", opacity: 1 },
    ], { duration: 180 });
  }
}

$("#previous-department").addEventListener("click", () => moveDepartment(-1));
$("#next-department").addEventListener("click", () => moveDepartment(1));
$("#previous-product").addEventListener("click", () => moveProduct(-1));
$("#next-product").addEventListener("click", () => moveProduct(1));
for (const mode of ["list", "flashcards"]) {
  $(mode === "list" ? "#list-view" : "#flashcard-view").addEventListener("click", async () => {
    if (!await ready()) return;
    inventoryView = mode;
    renderInventory();
    setBusy(false);
  });
}
let swipeStart = null;
$("#product-card").addEventListener("pointerdown", (event) => {
  swipeStart = null;
  if (busy || !event.isPrimary || event.target.closest("input, label, button")) return;
  swipeStart = { x: event.clientX, y: event.clientY, id: event.pointerId };
  $("#product-card").setPointerCapture(event.pointerId);
});
$("#product-card").addEventListener("pointercancel", () => { swipeStart = null; });
$("#product-card").addEventListener("pointerup", (event) => {
  const start = swipeStart;
  swipeStart = null;
  if (!start || start.id !== event.pointerId) return;
  const dx = event.clientX - start.x;
  const dy = event.clientY - start.y;
  if (Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy) * 1.5) moveProduct(dx < 0 ? 1 : -1);
});
$("#product-card").addEventListener("keydown", (event) => {
  if (event.target !== $("#product-card") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  moveProduct(event.key === "ArrowRight" ? 1 : -1);
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => changeView(button.dataset.view)));
$("#inventory-department").addEventListener("change", () => { $("#inventory-search").value = ""; renderInventory(); setBusy(busy); });
$("#inventory-search").addEventListener("input", () => { renderInventory(); setBusy(busy); });
$("#catalog-department").addEventListener("change", () => { renderCatalog(); setBusy(busy); rememberNavigation(); });
$("#order-department").addEventListener("change", () => { renderOrders(); setBusy(busy); rememberNavigation(); });
$("#add-department").addEventListener("click", () => openDialog("department"));
$("#add-product").addEventListener("click", () => openDialog("product"));
$("#add-store").addEventListener("click", () => openDialog("store"));
$("#rename-store").addEventListener("click", () => openDialog("store", stores.find((store) => store.id === activeStore)));
$("#store-select").addEventListener("change", async (event) => {
  const storeId = Number(event.target.value);
  if (!await ready()) { $("#store-select").value = activeStore; return; }
  setBusy(true);
  try {
    await reloadData(storeId);
    saveMessage("Changes save automatically.");
    $("#notice").hidden = true;
  } catch (error) {
    $("#store-select").value = activeStore;
    notify(error.message, true);
  } finally { setBusy(false); }
});
$("#close-dialog").addEventListener("click", () => { if (!busy) $("#add-dialog").close(); });
$("#add-dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
$("#logout-form").addEventListener("submit", async (event) => {
  if (stockSave) {
    event.preventDefault();
    if (await ready()) $("#logout-form").submit();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (stockSave) { event.preventDefault(); event.returnValue = ""; }
});

readNavigation();
inventoryView = navigation.inventoryView === "flashcards" ? "flashcards" : "list";
const initialView = new URL(window.location.href).searchParams.get("view") || navigation.view;
setBusy(true);
(async () => {
  await reloadStores();
  const requested = Number(new URL(window.location.href).searchParams.get("store_id"));
  const storeId = stores.some((store) => store.id === requested) ? requested
    : stores.some((store) => store.id === navigation.store) ? navigation.store : stores[0]?.id;
  if (!storeId) throw new Error("No stores are available. Reload the page to try again.");
  await reloadData(storeId);
})().catch((error) => notify(error.message, true)).finally(() => {
  setBusy(false);
  if (loaded && Object.hasOwn(pages, initialView)) changeView(initialView);
});
