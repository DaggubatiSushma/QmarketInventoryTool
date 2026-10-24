"use strict";

const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
const pages = {
  inventory: ["Inventory", "Choose a department, mark stock-outs, and enter what you need."],
  departments: ["Departments", "Manage departments and product information, all in one place."],
  orders: ["Order list", "What to buy for this store. Choose one department or view them all."],
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

function shoppingProducts(filtered = true) {
  const departmentId = filtered ? $("#order-department").value : "";
  return data.products.filter((product) => (product.out_of_stock || product.order_quantity > 0) &&
    (!departmentId || product.department_id === Number(departmentId)));
}

function setBusy(value) {
  busy = value;
  for (const selector of ["#add-department", "#add-product", "#inventory-department", "#catalog-department", "#order-department", "#create-item", "#store-select", "#add-store", "#rename-store"]) {
    $(selector).disabled = value || !loaded;
  }
  $("#reset-department").disabled = value || !loaded || !selectedProducts().length;
  $("#export").disabled = value || !loaded || !shoppingProducts().length;
  document.querySelectorAll("#inventory-rows input, #order-rows input, #catalog-rows button, #department-cards button").forEach((control) => {
    control.disabled = value || control.dataset.unavailable === "true";
  });
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

function quantityCell(product) {
  const cell = element("td");
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
  cell.append(input);
  return cell;
}

function renderInventory() {
  const products = selectedProducts();
  const body = $("#inventory-rows");
  body.replaceChildren();
  for (const product of products) {
    const row = element("tr");
    row.append(identity(product), element("td", product.size || "--"));
    const stock = element("td");
    const label = element("label", undefined, "stock-toggle");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = product.out_of_stock;
    checkbox.setAttribute("aria-label", `${productLabel(product)}: Out of stock`);
    checkbox.addEventListener("change", () => saveStock(product, { out_of_stock: checkbox.checked }));
    label.append(checkbox, element("span", product.out_of_stock ? "Out of stock" : "Not marked", product.out_of_stock ? "status out" : "muted small"));
    stock.append(label);
    row.append(stock, quantityCell(product));
    body.append(row);
  }
  $("#inventory-table").hidden = products.length === 0;
  $("#inventory-empty").hidden = products.length > 0;
  $("#inventory-empty h3").textContent = $("#inventory-department").value ? "No products in this department" : "Select a department";
  labelCards("inventory-table");
}

function renderCatalog() {
  const cards = $("#department-cards");
  cards.replaceChildren();
  if (!data.departments.length) cards.append(element("p", "Add your first department to get started.", "muted"));
  data.departments.forEach((department) => {
    const products = data.products.filter((product) => product.department_id === department.id);
    const card = element("article", undefined, "department-card");
    card.append(element("h3", department.name), element("p", `${products.length} product${products.length === 1 ? "" : "s"}`, "muted"));
    const add = element("button", "+ Add Products");
    add.addEventListener("click", () => openDialog("product", null, department.id));
    const remove = element("button", "Delete", "text-danger");
    remove.dataset.unavailable = String(products.length > 0);
    remove.title = products.length ? "Remove products before deleting this department" : "Delete empty department";
    remove.addEventListener("click", () => removeItem("department", department));
    const actions = element("div", undefined, "catalog-row-actions");
    actions.append(add, remove);
    card.append(actions);
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
    actions.append(group);
    row.append(actions);
    body.append(row);
  });
  $("#catalog-table").hidden = products.length === 0;
  $("#catalog-empty").hidden = products.length > 0;
  labelCards("catalog-table");
}

function renderOrders() {
  const products = shoppingProducts();
  const body = $("#order-rows");
  body.replaceChildren();
  products.forEach((product) => {
    const row = element("tr");
    row.append(element("td", product.department), element("td", product.name, "product-name"),
      element("td", product.size || "--"), quantityCell(product));
    body.append(row);
  });
  $("#order-badge").textContent = shoppingProducts(false).length;
  $("#shopping-count").textContent = `${products.length} product${products.length === 1 ? "" : "s"} to buy`;
  $("#orders-table").hidden = !products.length;
  $("#orders-empty").hidden = products.length > 0;
  $("#orders-empty h3").textContent = $("#order-department").value ? "Nothing to buy in this department" : "Nothing to buy right now";
  labelCards("orders-table");
}

function render() {
  $("#store-select").replaceChildren(...stores.map((store) => new Option(store.name, store.id)));
  $("#store-select").value = activeStore;
  fillDepartments();
  renderInventory();
  renderCatalog();
  renderOrders();
  setBusy(busy);
}

async function reloadData(storeId = activeStore) {
  const result = await api(scopedPath("/api/inventory", storeId));
  data = result;
  activeStore = storeId;
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
  if (busy) return;
  setBusy(true);
  saveMessage("Saving...");
  stockSave = (async () => {
    try {
      data = await api(scopedPath("/api/inventory"), "PUT", { products: [{ ...product, ...changes }] });
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
  view = nextView;
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false");
  });
  for (const name of Object.keys(pages)) $(`#${name}-panel`).hidden = name !== view;
  $("#page-title").textContent = pages[view][0];
  $("#breadcrumb").textContent = pages[view][0];
  $("#page-subtitle").textContent = pages[view][1];
  document.title = `${pages[view][0]} | Stockroom`;
  $("#catalog-actions").hidden = view !== "departments";
  $("#export").hidden = view !== "orders";
  const url = new URL(window.location.href);
  url.searchParams.set("view", view);
  window.history.replaceState(null, "", url);
  render();
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
    notify(`${{ product: "Product information", department: "Department", store: "Store" }[dialogKind]} saved.`);
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
  if (!confirm(`Reset "${department.name}" in "${storeName()}"? This clears this department's stock-out marks and quantities. Products and other stores will not be changed.`)) return;
  setBusy(true);
  try {
    data = await api(scopedPath(`/api/departments/${id}/reset`), "POST", { products: selectedProducts().map((product) => ({ id: product.id, revision: product.revision })) });
    render();
    saveMessage("All changes saved.");
    notify(`${department.name} inventory reset. Products have been kept.`);
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$("#export").addEventListener("click", async () => {
  if (!await ready()) return;
  setBusy(true);
  try {
    const path = scopedPath(`/api/orders/export?department_id=${encodeURIComponent($("#order-department").value)}`);
    const response = await checkResponse(await fetch(path));
    const url = URL.createObjectURL(await response.blob());
    const link = element("a");
    link.href = url;
    link.download = `shopping-list-store-${activeStore}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("Shopping list exported.");
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => changeView(button.dataset.view)));
$("#inventory-department").addEventListener("change", () => { renderInventory(); setBusy(busy); });
$("#catalog-department").addEventListener("change", () => { renderCatalog(); setBusy(busy); });
$("#order-department").addEventListener("change", () => { renderOrders(); setBusy(busy); });
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

setBusy(true);
(async () => {
  await reloadStores();
  const requested = Number(new URL(window.location.href).searchParams.get("store_id"));
  const storeId = stores.some((store) => store.id === requested) ? requested : stores[0]?.id;
  if (!storeId) throw new Error("No stores are available. Reload the page to try again.");
  await reloadData(storeId);
})().catch((error) => notify(error.message, true)).finally(() => {
  setBusy(false);
  const requestedView = new URL(window.location.href).searchParams.get("view");
  if (loaded && Object.hasOwn(pages, requestedView)) changeView(requestedView);
});
