import os
import re
import shutil
import mysql.connector
import pandas as pd


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_password():
    env = os.getenv("INVENTORY_DB_PASSWORD")
    if env is not None:
        return env
    try:
        with open(os.path.join(BASE_DIR, "db_password.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


DB_HOST = os.getenv("INVENTORY_DB_HOST", "localhost")
DB_USER = os.getenv("INVENTORY_DB_USER", "root")
DB_PASSWORD = _read_password()
DB_NAME = os.getenv("INVENTORY_DB_NAME", "inventory_db")

CSV_FILE = os.path.join(BASE_DIR, "inventory.csv")
CSV_ORIGINAL_BACKUP = os.path.join(BASE_DIR, "inventory_original.csv")

# Remembers the CSV's modification time after the app's last write, so edits
# made while the app was closed can be detected at the next startup.
STATE_FILE = os.path.join(BASE_DIR, ".inventory_csv_state")

INVENTORY_COLUMNS = [
    "Product_ID", "Product_Name", "Category",
    "Price", "Quantity", "Reorder_Level", "SKU"
]
REQUIRED_CSV_COLUMNS = INVENTORY_COLUMNS[:6]   # SKU is optional in the CSV

# ---- sync state (used to detect changes made outside the app) ----
_last_signature = None   # fingerprint of the MySQL tables
_csv_mtime = None        # modification time of the CSV after OUR last write
_csv_dirty = False       # True if the last CSV write failed (e.g. file open in Excel)


# ==================================================
# DATABASE CONNECTION
# ==================================================

def get_connection():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )


# ==================================================
# CSV <-> MYSQL SYNCHRONIZATION
# ==================================================

def _fetch_inventory():
    """Read inventory directly from MySQL."""
    conn = get_connection()

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT Product_ID, Product_Name, Category, Price, Quantity, "
            "Reorder_Level, SKU FROM inventory ORDER BY Product_ID"
        )
        rows = cursor.fetchall()
        cursor.close()

        return pd.DataFrame(rows, columns=INVENTORY_COLUMNS)

    finally:
        conn.close()


def _get_signature():
    """Cheap fingerprint of all tables; changes whenever any data changes."""
    conn = get_connection()

    try:
        cursor = conn.cursor()
        cursor.execute("CHECKSUM TABLE inventory, sales, purchases")
        result = tuple(row[1] for row in cursor.fetchall())
        cursor.close()
        return result

    finally:
        conn.close()


def _save_state(mtime):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(repr(mtime))
    except OSError:
        pass


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_csv(filename=None):
    """Write MySQL inventory to CSV and remember the file's mtime."""
    global _csv_mtime, _csv_dirty

    target = filename or CSV_FILE

    try:
        df = _fetch_inventory()
        df.to_csv(target, index=False, encoding="utf-8-sig")

        if target == CSV_FILE:
            _csv_mtime = os.path.getmtime(CSV_FILE)
            _csv_dirty = False
            _save_state(_csv_mtime)

        return True, "CSV synchronized successfully."

    except Exception as e:
        if target == CSV_FILE:
            _csv_dirty = True   # retry on the next sync check
        return False, str(e)


def sync_inventory_to_csv():
    """Replace inventory.csv with the latest MySQL inventory."""
    return _write_csv()


def _sync_message():
    """Called after every change made through the app."""
    global _last_signature

    success, message = sync_inventory_to_csv()

    try:
        _last_signature = _get_signature()
    except mysql.connector.Error:
        pass

    if success:
        return " MySQL and CSV updated successfully."

    return " Saved in MySQL, but CSV sync failed: " + message


def _num(text, default):
    """Blank cell -> default; otherwise the text must be a number."""
    text = str(text).strip()
    return default if text == "" else float(text)


def import_csv_to_db():
    """
    Apply an externally edited inventory.csv to MySQL.
    - rows with an existing numeric Product_ID are updated
    - rows with a blank ID are inserted
    - rows with a NON-numeric ID (like 29-205-1132 from older files) are matched
      by SKU: updated if that SKU exists, otherwise inserted with that ID as SKU
    - products missing from the CSV are deleted
    Everything happens in one transaction: if the CSV has any bad row,
    nothing is changed.
    """
    try:
        df = pd.read_csv(
            CSV_FILE, encoding="utf-8-sig",
            dtype=str, keep_default_na=False
        )
    except Exception as e:
        return False, "Could not read inventory.csv: " + str(e)

    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in df.columns]
    has_sku = "SKU" in df.columns
    if missing:
        return False, "inventory.csv is missing columns: " + ", ".join(missing)

    rows = []
    seen_ids = set()
    seen_skus = set()

    for i, r in df.iterrows():
        line = i + 2  # +1 header, +1 for 1-based numbering

        name = r["Product_Name"].strip()
        if not name:
            return False, f"CSV row {line}: Product_Name is empty."

        pid_text = r["Product_ID"].strip()
        sku = r["SKU"].strip()[:50] if has_sku else ""
        pid = None

        try:
            if pid_text:
                try:
                    pid = int(float(pid_text))
                    if str(pid) != pid_text and not re.fullmatch(r"\d+\.0+", pid_text):
                        raise ValueError
                except ValueError:
                    # old-style ID such as 29-205-1132 -> use it as the SKU
                    pid = None
                    if not sku:
                        sku = pid_text[:50]

            price = _num(r["Price"], 0.0)
            quantity = int(_num(r["Quantity"], 0))
            reorder = int(_num(r["Reorder_Level"], 5))
        except ValueError:
            return False, f"CSV row {line}: Price/Quantity/Reorder_Level must be numbers."

        if price < 0 or quantity < 0 or reorder < 0:
            return False, f"CSV row {line}: values cannot be negative."

        if pid is not None:
            if pid in seen_ids:
                return False, f"CSV row {line}: duplicate Product_ID {pid}."
            seen_ids.add(pid)
        elif sku:
            if sku in seen_skus:
                return False, f"CSV row {line}: duplicate ID/SKU {sku}."
            seen_skus.add(sku)

        rows.append((pid, name, r["Category"].strip(), price, quantity, reorder, sku))

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT Product_ID, SKU FROM inventory")
        found = cursor.fetchall()
        existing = {row[0] for row in found}
        sku_to_id = {row[1]: row[0] for row in found if row[1]}

        # Safety net: an empty CSV (e.g. half-saved by Excel) must not wipe the DB.
        if not rows and existing:
            return False, "inventory.csv has no products; import skipped to protect your data."

        conn.start_transaction()

        # If the CSV has no SKU column, existing SKUs are left untouched.
        sku_update = ",\n                        SKU=VALUES(SKU)" if has_sku else ""

        for pid, name, category, price, quantity, reorder, sku in rows:
            if pid is None and sku and sku in sku_to_id:
                # same old-style ID/SKU already in MySQL -> update that product
                match = sku_to_id[sku]
                cursor.execute("""
                    UPDATE inventory
                    SET Product_Name=%s, Category=%s, Price=%s,
                        Quantity=%s, Reorder_Level=%s
                    WHERE Product_ID=%s
                """, (name, category, price, quantity, reorder, match))
                seen_ids.add(match)
            elif pid is None:
                cursor.execute("""
                    INSERT INTO inventory
                    (Product_Name, Category, Price, Quantity, Reorder_Level, SKU)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (name, category, price, quantity, reorder, sku))
            else:
                cursor.execute("""
                    INSERT INTO inventory
                    (Product_ID, Product_Name, Category, Price, Quantity, Reorder_Level, SKU)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        Product_Name=VALUES(Product_Name),
                        Category=VALUES(Category),
                        Price=VALUES(Price),
                        Quantity=VALUES(Quantity),
                        Reorder_Level=VALUES(Reorder_Level)""" + sku_update + """
                """, (pid, name, category, price, quantity, reorder, sku))

        for pid in existing - seen_ids:
            cursor.execute("DELETE FROM inventory WHERE Product_ID=%s", (pid,))

        conn.commit()
        return True, "inventory.csv changes were applied to MySQL."

    except mysql.connector.Error as e:
        if conn:
            conn.rollback()
        return False, "CSV import failed: " + str(e)

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def sync_check():
    """
    Call this periodically. Detects changes made OUTSIDE the app and
    propagates them everywhere.

    Returns (changed, message):
      changed -> True if the GUI should reload its data
      message -> non-empty if something needs to be shown to the user
    """
    global _last_signature, _csv_mtime

    changed = False
    message = ""

    # 1) CSV edited externally -> push into MySQL
    if os.path.exists(CSV_FILE):
        current_mtime = os.path.getmtime(CSV_FILE)

        if _csv_mtime is not None and current_mtime != _csv_mtime:
            _csv_mtime = current_mtime   # never retry the same bad file in a loop
            ok, msg = import_csv_to_db()

            if ok:
                changed = True
            else:
                message = msg
    else:
        _write_csv()   # CSV was deleted -> recreate it

    # 2) MySQL changed (from the CSV import above, or from Workbench etc.)
    signature = _get_signature()

    if signature != _last_signature:
        _last_signature = signature
        changed = True
        _write_csv()
    elif _csv_dirty:
        _write_csv()   # retry a failed CSV write

    return changed, message


# ==================================================
# CREATE DATABASE AND TABLES
# ==================================================

def initialize_database():
    global _last_signature

    # Create the database itself if it does not exist yet
    conn = mysql.connector.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD
    )
    try:
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`")
        cursor.close()
    finally:
        conn.close()

    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                Product_ID INT AUTO_INCREMENT PRIMARY KEY,
                Product_Name VARCHAR(100) NOT NULL,
                Category VARCHAR(100) DEFAULT '',
                Price DECIMAL(10,2) NOT NULL DEFAULT 0,
                Quantity INT NOT NULL DEFAULT 0,
                Reorder_Level INT NOT NULL DEFAULT 5,
                SKU VARCHAR(50) NOT NULL DEFAULT ''
            )
        """)

        # Older databases: add the SKU column if it is missing
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME='inventory' AND COLUMN_NAME='SKU'
        """, (DB_NAME,))
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "ALTER TABLE inventory ADD COLUMN SKU VARCHAR(50) NOT NULL DEFAULT ''"
            )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                Sale_ID INT AUTO_INCREMENT PRIMARY KEY,
                Product_ID INT NOT NULL,
                Product_Name VARCHAR(100) NOT NULL,
                Quantity INT NOT NULL,
                Unit_Price DECIMAL(10,2) NOT NULL,
                Total_Amount DECIMAL(12,2) NOT NULL,
                Sale_Date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                Purchase_ID INT AUTO_INCREMENT PRIMARY KEY,
                Product_ID INT NOT NULL,
                Product_Name VARCHAR(100) NOT NULL,
                Quantity INT NOT NULL,
                Unit_Price DECIMAL(10,2) NOT NULL,
                Total_Amount DECIMAL(12,2) NOT NULL,
                Purchase_Date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM inventory")
        table_is_empty = cursor.fetchone()[0] == 0

    finally:
        cursor.close()
        conn.close()

    global _csv_mtime

    # FIRST RUN: MySQL is empty but inventory.csv has your data (for example your
    # original file with IDs like 29-205-1132). Load it into MySQL once. Those
    # original IDs are kept in the SKU column; MySQL gives numeric Product_IDs.
    if table_is_empty and os.path.exists(CSV_FILE):
        rows, skipped, error = _parse_import_file(CSV_FILE)

        if not rows:
            # Could not read the CSV: keep a copy, do NOT overwrite the file.
            if not os.path.exists(CSV_ORIGINAL_BACKUP):
                shutil.copyfile(CSV_FILE, CSV_ORIGINAL_BACKUP)
            _csv_mtime = os.path.getmtime(CSV_FILE)
            _last_signature = _get_signature()
            return (
                "Your inventory.csv could not be loaded into MySQL:\n" + error +
                "\n\nThe file was left unchanged."
            )

        if rows:
            if not os.path.exists(CSV_ORIGINAL_BACKUP):
                shutil.copyfile(CSV_FILE, CSV_ORIGINAL_BACKUP)

            conn = get_connection()
            cursor = conn.cursor()
            try:
                conn.start_transaction()
                cursor.executemany("""
                    INSERT INTO inventory
                    (Product_Name, Category, Price, Quantity, Reorder_Level, SKU)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, rows)
                conn.commit()
            except mysql.connector.Error:
                conn.rollback()
                raise
            finally:
                cursor.close()
                conn.close()

            _write_csv()
            _last_signature = _get_signature()
            return (
                f"First run: {len(rows)} products from inventory.csv were loaded into MySQL.\n"
                f"Your original file is saved as {os.path.basename(CSV_ORIGINAL_BACKUP)}."
            )

    # If the CSV was edited while the app was closed, import it first.
    saved = _load_state()

    if (
        saved is not None
        and os.path.exists(CSV_FILE)
        and os.path.getmtime(CSV_FILE) != saved
    ):
        ok, msg = import_csv_to_db()

        if ok:
            _write_csv()
            _last_signature = _get_signature()
            return "inventory.csv was edited while the app was closed; its changes were applied to MySQL."

        # Bad CSV: leave the file untouched so the user can fix it
        _csv_mtime = os.path.getmtime(CSV_FILE)
        _last_signature = _get_signature()
        return "inventory.csv was not imported: " + msg + "\nThe file was left unchanged."

    # Otherwise MySQL is the master: create/refresh the CSV from it.
    _write_csv()
    _last_signature = _get_signature()
    return ""


# ==================================================
# GET INVENTORY
# ==================================================

def get_inventory():
    return _fetch_inventory()


def get_dashboard_data():
    return get_inventory()


# ==================================================
# ADD / UPDATE / STOCK / DELETE
# ==================================================

def _run_change(action):
    """Shared connection handling for single-statement changes."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        return action(conn, cursor)
    except (mysql.connector.Error, ValueError, TypeError) as e:
        if conn:
            conn.rollback()
        return False, str(e)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def add_product(name, category, price, quantity, reorder_level=5):
    try:
        name = str(name).strip()
        category = str(category).strip()
        price = float(price)
        quantity = int(quantity)
        reorder_level = int(reorder_level)
    except (ValueError, TypeError) as e:
        return False, str(e)

    if not name:
        return False, "Product name is required."

    if price < 0 or quantity < 0 or reorder_level < 0:
        return False, "Values cannot be negative."

    def action(conn, cursor):
        cursor.execute("""
            INSERT INTO inventory
            (Product_Name, Category, Price, Quantity, Reorder_Level)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, category, price, quantity, reorder_level))
        conn.commit()
        return True, "Product added successfully." + _sync_message()

    return _run_change(action)


def update_product(product_id, name, category, price, quantity, reorder_level):
    try:
        product_id = int(product_id)
        name = str(name).strip()
        category = str(category).strip()
        price = float(price)
        quantity = int(quantity)
        reorder_level = int(reorder_level)
    except (ValueError, TypeError) as e:
        return False, str(e)

    if not name:
        return False, "Product name is required."

    if price < 0 or quantity < 0 or reorder_level < 0:
        return False, "Values cannot be negative."

    def action(conn, cursor):
        cursor.execute("SELECT Product_ID FROM inventory WHERE Product_ID=%s", (product_id,))
        if cursor.fetchone() is None:
            return False, "Product not found."

        cursor.execute("""
            UPDATE inventory
            SET Product_Name=%s, Category=%s, Price=%s,
                Quantity=%s, Reorder_Level=%s
            WHERE Product_ID=%s
        """, (name, category, price, quantity, reorder_level, product_id))
        conn.commit()
        return True, "Product updated successfully." + _sync_message()

    return _run_change(action)


def update_stock(product_id, quantity):
    try:
        product_id = int(product_id)
        quantity = int(quantity)
    except (ValueError, TypeError) as e:
        return False, str(e)

    if quantity < 0:
        return False, "Quantity cannot be negative."

    def action(conn, cursor):
        cursor.execute("SELECT Product_ID FROM inventory WHERE Product_ID=%s", (product_id,))
        if cursor.fetchone() is None:
            return False, "Product not found."

        cursor.execute(
            "UPDATE inventory SET Quantity=%s WHERE Product_ID=%s",
            (quantity, product_id)
        )
        conn.commit()
        return True, "Stock updated successfully." + _sync_message()

    return _run_change(action)


def delete_product(product_id):
    try:
        product_id = int(product_id)
    except (ValueError, TypeError) as e:
        return False, str(e)

    def action(conn, cursor):
        cursor.execute("DELETE FROM inventory WHERE Product_ID=%s", (product_id,))
        if cursor.rowcount == 0:
            conn.rollback()
            return False, "Product not found."
        conn.commit()
        return True, "Product deleted successfully." + _sync_message()

    return _run_change(action)


# ==================================================
# LOW STOCK
# ==================================================

def get_low_stock():
    conn = get_connection()

    try:
        return pd.read_sql("""
            SELECT *
            FROM inventory
            WHERE Quantity <= Reorder_Level
            ORDER BY (Reorder_Level - Quantity) DESC, Product_ID
        """, conn)

    finally:
        conn.close()


# ==================================================
# RECORD SALE / PURCHASE
# ==================================================

def record_sale(product_id, quantity):
    try:
        product_id = int(product_id)
        quantity = int(quantity)
    except (ValueError, TypeError) as e:
        return False, str(e)

    if quantity <= 0:
        return False, "Sale quantity must be greater than zero."

    def action(conn, cursor):
        conn.start_transaction()

        cursor.execute("""
            SELECT Product_Name, Price, Quantity
            FROM inventory WHERE Product_ID=%s FOR UPDATE
        """, (product_id,))
        product = cursor.fetchone()

        if product is None:
            conn.rollback()
            return False, "Product not found."

        name, price, stock = product

        if quantity > stock:
            conn.rollback()
            return False, "Not enough stock available."

        total = float(price) * quantity

        cursor.execute("""
            INSERT INTO sales
            (Product_ID, Product_Name, Quantity, Unit_Price, Total_Amount)
            VALUES (%s, %s, %s, %s, %s)
        """, (product_id, name, quantity, price, total))

        cursor.execute(
            "UPDATE inventory SET Quantity=Quantity-%s WHERE Product_ID=%s",
            (quantity, product_id)
        )
        conn.commit()
        return True, f"Sale recorded. Total: {total:.2f}." + _sync_message()

    return _run_change(action)


def record_purchase(product_id, quantity):
    try:
        product_id = int(product_id)
        quantity = int(quantity)
    except (ValueError, TypeError) as e:
        return False, str(e)

    if quantity <= 0:
        return False, "Purchase quantity must be greater than zero."

    def action(conn, cursor):
        conn.start_transaction()

        cursor.execute("""
            SELECT Product_Name, Price
            FROM inventory WHERE Product_ID=%s FOR UPDATE
        """, (product_id,))
        product = cursor.fetchone()

        if product is None:
            conn.rollback()
            return False, "Product not found."

        name, price = product
        total = float(price) * quantity

        cursor.execute("""
            INSERT INTO purchases
            (Product_ID, Product_Name, Quantity, Unit_Price, Total_Amount)
            VALUES (%s, %s, %s, %s, %s)
        """, (product_id, name, quantity, price, total))

        cursor.execute(
            "UPDATE inventory SET Quantity=Quantity+%s WHERE Product_ID=%s",
            (quantity, product_id)
        )
        conn.commit()
        return True, f"Purchase recorded. Total: {total:.2f}." + _sync_message()

    return _run_change(action)


# ==================================================
# CATEGORIES
# ==================================================

def get_categories():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT DISTINCT Category
            FROM inventory
            WHERE Category IS NOT NULL AND Category <> ''
            ORDER BY Category
        """)
        return [row[0] for row in cursor.fetchall()]

    finally:
        cursor.close()
        conn.close()


# ==================================================
# DASHBOARD STATISTICS
# ==================================================

def get_dashboard_kpis():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(Quantity), 0),
                COALESCE(SUM(Price * Quantity), 0),
                COALESCE(SUM(CASE WHEN Quantity <= Reorder_Level THEN 1 ELSE 0 END), 0)
            FROM inventory
        """)
        products, stock, value, low_stock = cursor.fetchone()

        cursor.execute("""
            SELECT COALESCE(SUM(Total_Amount), 0)
            FROM sales WHERE Sale_Date >= CURRENT_DATE()
        """)
        today_sales = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COALESCE(SUM(Total_Amount), 0)
            FROM purchases WHERE Purchase_Date >= CURRENT_DATE()
        """)
        today_purchases = cursor.fetchone()[0]

        return {
            "total_products": int(products),
            "total_stock": int(stock),
            "inventory_value": float(value),
            "low_stock": int(low_stock),
            "today_sales": float(today_sales),
            "today_purchases": float(today_purchases)
        }

    finally:
        cursor.close()
        conn.close()


def get_statistics():
    return get_dashboard_kpis()


# ==================================================
# SALES AND PURCHASE GRAPH DATA
# ==================================================

def get_sales_data():
    conn = get_connection()

    try:
        return pd.read_sql("""
            SELECT DATE(Sale_Date) AS Date, SUM(Total_Amount) AS Total_Amount
            FROM sales GROUP BY DATE(Sale_Date) ORDER BY Date
        """, conn)

    finally:
        conn.close()


def get_purchase_data():
    conn = get_connection()

    try:
        return pd.read_sql("""
            SELECT DATE(Purchase_Date) AS Date, SUM(Total_Amount) AS Total_Amount
            FROM purchases GROUP BY DATE(Purchase_Date) ORDER BY Date
        """, conn)

    finally:
        conn.close()


# ==================================================
# IMPORT A DATA CSV (any file, flexible column names)
# ==================================================

# "sku" is checked first so a file that already has a SKU column keeps it.
_SKU_ALIASES = ["sku", "product_id", "id", "item_id", "code", "item_code"]

_ALIASES = {
    "Product_Name": ["product_name", "name", "product", "item", "item_name", "productname"],
    "Category": ["category", "type", "group", "department"],
    "Price": ["price", "unit_price", "rate", "mrp", "cost"],
    "Quantity": ["quantity", "qty", "stock", "units", "in_stock"],
    "Reorder_Level": ["reorder_level", "reorder", "reorder_point", "min_stock", "minimum"],
}


def _norm_col(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _to_number(text, default, as_int=False):
    text = str(text).strip()

    if text == "" or text.lower() in ("nan", "none", "null", "n/a"):
        return default

    cleaned = re.sub(r"[^0-9.\-]", "", text)   # drops currency signs, commas

    try:
        value = float(cleaned)
    except ValueError:
        return default

    value = max(value, 0)
    return int(value) if as_int else value


def _parse_import_file(path):
    """Returns (rows, skipped, error). Rows are (name, category, price, qty, reorder, sku)."""
    try:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False,
                             sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, dtype=str, keep_default_na=False,
                             sep=None, engine="python", encoding="latin-1")
    except Exception as e:
        return None, 0, "Could not read the file: " + str(e)

    lookup = {_norm_col(c): c for c in df.columns}
    column = {}

    for target, names in _ALIASES.items():
        for n in names:
            if n in lookup:
                column[target] = lookup[n]
                break

    if "Product_Name" not in column:
        return None, 0, (
            "No product-name column found. Columns in the file: "
            + ", ".join(map(str, df.columns))
        )

    sku_col = next((lookup[n] for n in _SKU_ALIASES if n in lookup), None)

    rows, skipped = [], 0

    for _, r in df.iterrows():
        name = str(r[column["Product_Name"]]).strip()

        if not name or name.lower() == "nan":
            skipped += 1
            continue

        category = str(r[column["Category"]]).strip() if "Category" in column else ""
        if category.lower() == "nan":
            category = ""

        price = _to_number(r[column["Price"]], 0.0) if "Price" in column else 0.0
        qty = _to_number(r[column["Quantity"]], 0, True) if "Quantity" in column else 0
        reorder = _to_number(r[column["Reorder_Level"]], 5, True) if "Reorder_Level" in column else 5

        sku = str(r[sku_col]).strip()[:50] if sku_col else ""
        if sku.lower() == "nan":
            sku = ""

        rows.append((name[:100], category[:100], price, qty, reorder, sku))

    if not rows:
        return None, skipped, "The file has no usable rows."

    return rows, skipped, ""


def import_csv_file(path):
    """
    Add the products in a data CSV to MySQL. MySQL assigns numeric Product_IDs;
    the file's own ID is kept in the SKU column.
    """
    if os.path.abspath(path) == os.path.abspath(CSV_FILE):
        return False, ("That is the live inventory.csv, which is already synced. "
                       "Edit it directly, or pick a different data file.")

    rows, skipped, error = _parse_import_file(path)

    if error:
        return False, error

    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor()
        conn.start_transaction()

        # Skip rows whose SKU is already in the database (safe to re-import)
        cursor.execute("SELECT SKU FROM inventory WHERE SKU <> ''")
        known = {row[0] for row in cursor.fetchall()}

        new_rows, duplicates = [], 0
        for row in rows:
            if row[5] and row[5] in known:
                duplicates += 1
            else:
                if row[5]:
                    known.add(row[5])
                new_rows.append(row)

        if new_rows:
            cursor.executemany("""
                INSERT INTO inventory
                (Product_Name, Category, Price, Quantity, Reorder_Level, SKU)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, new_rows)

        conn.commit()

        msg = f"Imported {len(new_rows)} products."
        if duplicates:
            msg += f" Skipped {duplicates} already in the inventory (same SKU)."
        if skipped:
            msg += f" Skipped {skipped} rows without a name."

        return True, msg + _sync_message()

    except mysql.connector.Error as e:
        if conn:
            conn.rollback()
        return False, "Import failed, nothing was changed: " + str(e)

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ==================================================
# EXPORT CSV
# ==================================================

def export_inventory_csv(filename=None):
    ok, msg = _write_csv(filename or CSV_FILE)

    if ok:
        return True, f"Inventory exported to {filename or CSV_FILE}"

    return False, msg


# ==================================================
# TEST CONNECTION
# ==================================================

if __name__ == "__main__":
    try:
        initialize_database()
        print("MySQL connected successfully.")
        print("CSV file:", CSV_FILE)
        print(get_inventory())

    except mysql.connector.Error as e:
        print("Database connection failed:", e)