# Stockroom

A mobile-first, installable inventory web app (PWA) built with Flask. Manage separate stores, organize their products by department, flag stock-outs, type quantities needed, and use a shopping list. Your team signs in with one shared password.

## Use it like a phone app

Open your deployed **HTTPS Render URL** on your phone and tap **Install app**. If the browser does not offer an installation prompt:

- **iPhone/iPad:** Open in Safari, tap **Share > Add to Home Screen**, and leave **Open as Web App** enabled if offered.
- **Android:** Open in Chrome and select **Install app** or **Add to Home screen** from the browser menu.

Stockroom opens from its own home-screen icon in standalone mode. The mobile interface has bottom navigation, full-width product cards without sideways table scrolling, large touch controls, numeric keyboards for quantities, and bottom-sheet forms. Desktop browsers still work. The selected store and page are retained in the URL.

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

**Stores:** Use the store selector above the pages. Open the **Manage stores (...)** menu beside it for **Add Store** and **Rename store**. Adding a store creates an empty workspace with its own departments, products, stock-out flags, and quantities; nothing is shared or copied from another store. Renaming changes its name without affecting its data. The same department/product names can be used in different stores. The selected store is retained in the page URL when you reload.

**Departments:** Add departments and products for the selected store. Each product has a name, department, and size/quantity such as `355ml`, `473ml`, or `1 ltr`. There is no description field. The same product name can have different sizes in a department. Edit product information here; this does not change its inventory. Deleting a product permanently removes its inventory and legacy history. A department must be empty before it can be deleted.

**Inventory:** Select a department to see its products. The only inventory fields are **Out of stock** and **Qty needed** (number of packs/items to buy, not the product's volume). Type a whole number from 0 to 1,000,000; scrolling or arrow keys cannot increment it. Changes save automatically when a checkbox changes or you finish editing a quantity. There are no weekly counts, deliveries, or sales calculations.

**Reset department** asks for confirmation, then clears all stock-out flags and quantities needed in the selected department of the selected store. It never deletes product information or changes other departments or stores. There is no automatic weekly reset.

**Order list:** Shows products in the selected store that are marked out of stock **or** have a quantity needed greater than zero. Use its department filter to choose one department or **All departments**. You can type quantities directly here; changes also appear in Inventory. An out-of-stock product with quantity 0 remains listed (quantity not specified). Setting quantity to 0 removes a product only if it is not marked out of stock. CSV export follows the selected store and department filter and includes the store name and product size, without descriptions. The Inventory page's department selection is independent of this filter.

**Share an order:** On Order list, select a department or All departments, then tap **View / share text**. The app refreshes saved inventory and opens a text snapshot with the store, preparation time, departments, product sizes, and quantities. **Copy text** lets you paste it into messages or email; **Share** opens your device's share menu when supported. If clipboard access is blocked, select and copy the preview manually. Recipients receive plain text, not access to your inventory or a live link. Later inventory changes do not update previously sent lists. CSV export remains available.

Team members can work on different products. Conflicting edits or resets are rejected rather than silently overwriting another person's work. Reload to get the current inventory if you see a conflict.

## Access and maintenance

All inventory pages, data, and exports require a signed-in session. Writes require a CSRF token. Sessions expire after 12 hours and use secure cookies on Render. All signed-in team members can access all stores; there are no store-specific permissions, individual accounts, roles, or per-person audit logs. API catalog, inventory, reset, and export requests require an explicit `store_id` and reject products/departments belonging to another store. Share the password only with trusted staff; use a strong random password. Rotate `SECRET_KEY` as well as the password when you need to revoke existing sessions.

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
