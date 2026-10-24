# Qmarket Stockroom

A mobile-first, installable inventory web app (PWA) built with Flask. Manage separate stores, organize their products by department, flag stock-outs, type quantities needed, and use a shopping list. Your team signs in with one shared password.

## Use it like a phone app

Open your deployed **HTTPS Render URL** on your phone and tap **Install app**. If the browser does not offer an installation prompt:

- **iPhone/iPad:** Open in Safari, tap **Share > Add to Home Screen**, and leave **Open as Web App** enabled if offered.
- **Android:** Open in Chrome and select **Install app** or **Add to Home screen** from the browser menu.

Qmarket Stockroom opens from its own home-screen icon in standalone mode. A soft mint, cream, and lavender theme keeps the workspace light, with dark text and teal actions. The mobile interface has bottom navigation, full-width product cards without sideways table scrolling, large touch controls, numeric keyboards for quantities, and bottom-sheet forms. Desktop browsers still work. The selected store and page are retained in the URL.

**Connection required:** Inventory, authentication, and exports are always fetched from the server. The service worker caches only public styling, icons, and a generic offline screen, never login pages, inventory, or shopping data. There is no offline editing or queued background sync. A failed save is reported and must be retried after reconnecting. This is a web app, not an App Store/Play Store native application.

## Deploy to Render

1. Push this folder to a private GitHub repository (do not include passwords or local database files).
2. In Render, select **New > Blueprint**, connect the repository, and use `render.yaml`.
3. Set `INVENTORY_PASSWORD` to a long, unique team password when prompted, then deploy.
4. Open the web service URL and sign in.

The blueprint provisions a **paid Starter web service and paid Basic PostgreSQL database**. Review Render's current prices before approving. Render generates `SECRET_KEY` and connects `DATABASE_URL` automatically. PostgreSQL preserves inventory across web service restarts and redeploys; do not replace it with SQLite on Render's ephemeral filesystem. Configure database backups appropriate to your needs.

For manual setup, use `pip install -r requirements.txt` as the build command and `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT 'main:create_app()'` as the start command. Set `DATABASE_URL` to your Render PostgreSQL internal URL, `SECRET_KEY` to a securely generated random value, and `INVENTORY_PASSWORD` to your team password. The health-check path is `/health`.

## Run locally (PowerShell)

```powershell
python -m pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
$env:INVENTORY_PASSWORD = Read-Host "Choose a local team password"
python main.py
```

Open `http://127.0.0.1:5000`. Local data is stored in `instance\inventory.db` unless `DATABASE_URL` is set. Gunicorn is used on Render's Linux host, not on Windows. Keep your production secret key stable across restarts; changing it signs everyone out.

Localhost supports PWA development on this computer. `127.0.0.1` on your phone refers to the phone itself, not this computer; use the deployed HTTPS URL for phone access and installation.

## Stores and pages

**Stores:** Use the store selector above the pages. Open the **Manage stores (...)** menu beside it for **Add Store** and **Rename store**. A new store starts empty by default. Optionally choose **Copy catalog from** while adding it to copy another store's departments, product names, sizes, and shelf order. Quantities, stock-out flags, checked/picked-up marks, history, and inventory-save timestamps are never copied. The new catalog is independent: later edits to either store do not affect the other. Renaming changes its name without affecting its data. The same department/product names can be used in different stores. The selected store is retained in the page URL when you reload.

**Departments:** Add departments and products for the selected store. Each product has a name, department, and size/quantity such as `355ml`, `473ml`, or `1 ltr`. There is no description field. The same product name can have different sizes in a department. Edit product information here; this does not change its inventory. Deleting a product permanently removes its inventory and legacy history. A department must be empty before it can be deleted.

**Inventory:** Select a department to see its products. The only inventory fields are **Out of stock** and **Qty needed** (number of packs/items to buy, not the product's volume). Type a whole number from 0 to 1,000,000; scrolling or arrow keys cannot increment it. Changes save automatically when a checkbox changes or you finish editing a quantity. There are no weekly counts, deliveries, or sales calculations.

**Reset department** asks for confirmation, then clears all stock-out flags, quantities needed, checked marks, and picked-up ticks in the selected department of the selected store. It never deletes product information or changes other departments or stores. There is no automatic weekly reset.

**Order list:** Shows products in the selected store that are marked out of stock **or** have a quantity needed greater than zero. Use its department filter to choose one department or **All departments**. You can type quantities directly here; changes also appear in Inventory. An out-of-stock product with quantity 0 remains listed (quantity not specified). Setting quantity to 0 removes a product only if it is not marked out of stock. CSV export follows the selected store and department filter and includes the store name and product size, without descriptions. The Inventory page's department selection is independent of this filter.

**Inventory navigation:** The store selector stays visible while scrolling. Use **Previous department / Next department** to browse departments. Switch between **List** and **Flashcards** to see all products or one product at a time. Flashcards use the same automatically saved stock-out and quantity fields; swipe horizontally, use Previous/Next product, or focus the card and use arrow keys. A counter shows your position, and navigation stops at the last product rather than looping. Browsing cards does not mark products or reset inventory.

**Find and resume:** Search the selected department by product name or size in either inventory view. Search only hides nonmatching products; resets and team progress always apply to the entire department. This device remembers your last store, page, view, department filters, and flashcard per store/department. Explicit store/page URL parameters take priority. Only navigation identifiers are kept in browser local storage, not product names, quantities, or inventory; blocked storage shows a notice and does not prevent normal use.

**Team checking:** Progress is shared in the database. Saving a quantity or stock status marks that product checked. For a product with no order needed, **Checked - nothing needed** confirms the check without changing stock or quantities and advances the flashcard. **Mark checked** confirms existing order needs without clearing them. Simply browsing never marks a product checked. The department counter and bar include all products, even during search. **Start new check** clears only checked marks for everyone in that department, keeping existing order needs. **Refresh** loads other employees' changes; opening Inventory or Departments also refreshes. There are no employee accounts or per-person attribution.

**Undo:** After Remove or Reset, an Undo banner offers the latest action for that store for up to 10 minutes while this page remains open. Undo restores the affected quantities, stock-out marks, checked state, and picked-up ticks together. It refuses to overwrite any newer edits, checks, pickups, or resets to those products, and cannot restore a deleted product. Tokens are single-use and checked on the server. A new Remove/Reset replaces the available Undo for that store; reloading closes the local Undo banner.

**Before sending:** Products with missing quantities are highlighted on Order list and called out in the Send list preview, including combined orders. Go back to correct them, or deliberately send the list with unspecified quantities. Reminders do not silently exclude products or assume zero demand.

**Share an order:** On Order list, select a department or All departments, then tap **Send list**. The app refreshes saved inventory and opens a text preview with **Share**, **Copy text**, and **Export CSV** together. Text includes the store, departments, product sizes, and quantities, without a prepared timestamp, per-product stock-out labels, or a summary footer. **Copy text** lets you paste it into messages or email; **Share** opens your device's share menu when supported. If clipboard access is blocked, select and copy the preview manually. Recipients receive plain text, not access to your inventory or a live link. Later inventory changes do not update previously sent lists.

**Remove from order:** Each product on an individual store's Order list has a **Remove** button. It clears that product's stock-out and picked-up marks and sets Qty needed to 0 for that store, removing it from shopping while keeping its catalog information. Other stores are unchanged. Failed saves leave the item listed with an error. Combined quantities reflect removals when All stores is reopened or refreshed.

**All stores:** A combined shopping list merges products with exactly matching names and sizes and sums their quantities. Each row includes a per-store quantity breakdown, also included in shared/copied text and the CSV's Store quantities column. Choose All departments or filter by department name across every store; matching department names share one option. Only demand in the selected department contributes to totals, unspecified quantities, and store breakdowns. Different product sizes or spellings remain separate. Products without a specified quantity remain visible; a partial total displays as, for example, `7 + unspecified`. The list refreshes when opened, with a Refresh button for newer changes. Send list sharing, copying, and CSV honor this page's department filter; the individual store selector and its department filter do not limit this page. Quantities remain read-only here; change them on the original per-store pages.

**Shopping mode:** Turn it on in Order list or All stores to tick products **Picked up**. Ticks are saved in the database and shared with the team; opening either shopping page or using Refresh loads current progress. They never remove items, clear order quantities, or hide products from sharing/CSV. Combined ticks apply to all contributing products in the selected department filter; a partly collected group shows **Partly picked up**. Stale or incomplete snapshots are rejected atomically. Changing a product's quantity or stock-out status clears its tick so changed demand is not mistaken for collected goods. Use the existing per-store Remove action when you want to clear an order, or untick a product to correct shopping progress. Start new check leaves picked-up ticks unchanged.

**Shelf order:** On Departments, turn on **Arrange shelf order**, then use **Move up / Move down** for departments or products within a department. Turn it off with **Done arranging**. The saved order is shared per store and used in department navigation, both inventory views, the catalog, and per-store shopping text/CSV. Existing catalogs remain alphabetical until arranged; products added to an arranged department go at the end. Reordering does not alter inventory. Conflicting catalog changes require a refresh rather than overwriting newer work. Combined orders stay alphabetical because each store has its own shelf layout.

**Freshness:** Each page shows the last recorded inventory-save time for its store (or the latest across all stores), plus when this device last successfully loaded that data. Save timestamps persist across restarts; older inventory without a known timestamp is labeled **not recorded**, not given an invented date. Refreshing alone does not change the saved time. Times are displayed in the device's local timezone.

Team members can work on different products. Conflicting edits or resets are rejected rather than silently overwriting another person's work. Reload to get the current inventory if you see a conflict.

## Access and maintenance

All inventory pages, data, and exports require a signed-in session. Writes require a CSRF token. Sessions expire after 12 hours and use secure cookies on Render. All signed-in team members can access all stores; there are no store-specific permissions, individual accounts, roles, or per-person audit logs. Per-store catalog, inventory, pickup, reset, and export requests require an explicit `store_id` and reject products/departments belonging to another store. Combined-order endpoints intentionally span all stores and honor their own `department_name` filter. Share the password only with trusted staff; use a strong random password. Rotate `SECRET_KEY` as well as the password when you need to revoke existing sessions.

Tables are created on first startup. `schema.py` upgrades existing workspaces into **Main Store**, preserving departments, products, and saved inventory. Rename Main Store to your actual store name if needed. Older product descriptions are retained only in database storage, not displayed, editable, returned by the API, or exported. The original weekly history is also kept but not shown. For the oldest version, each product's latest legacy inventory record initializes stock once; already-saved stock/reset state is never overwritten on restart. Back up an existing database before deploying a schema upgrade.

## Tests

```powershell
python -m unittest discover -s tests -v
node --check static\app.js
node --check static\pwa.js
node --check static\sw.js
```

The sharing browser regression in `tests\test_order_sharing.py` runs when Playwright is available (with Chromium installed); otherwise it is skipped. It uses an isolated in-memory database, never your configured production database.

When changing offline assets, bump the `stockroom-public-v...` cache name in `static\sw.js` so existing installations replace their public asset cache. The service worker is served at `/sw.js` with root scope; do not move registration to a subdirectory.
