import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import matplotlib
matplotlib.use("TkAgg")

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import backend


# How often (milliseconds) the app checks for changes made in MySQL or inventory.csv
AUTO_SYNC_MS = 3000


# ---------- APPLICATION ----------

class InventoryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Inventory Management System")
        self.root.geometry("1150x720")
        self.root.minsize(950, 600)

        self.selected_id = None
        self.last_sync_error = ""

        self.setup_style()
        self.setup_ui()
        self.refresh_all()

        # Start watching for changes made outside the app
        self.root.after(AUTO_SYNC_MS, self.auto_sync)

    def setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=27)
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"))
        style.configure("TButton", padding=6)

    def setup_ui(self):
        header = tk.Frame(self.root, bg="#183153", height=65)
        header.pack(fill="x")

        tk.Label(
            header,
            text="INVENTORY MANAGEMENT SYSTEM",
            bg="#183153",
            fg="white",
            font=("Arial", 19, "bold")
        ).pack(pady=17)

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=12)

        self.dashboard_tab = ttk.Frame(self.tabs, padding=10)
        self.inventory_tab = ttk.Frame(self.tabs, padding=10)
        self.transactions_tab = ttk.Frame(self.tabs, padding=10)

        self.tabs.add(self.dashboard_tab, text="Dashboard")
        self.tabs.add(self.inventory_tab, text="Inventory")
        self.tabs.add(self.transactions_tab, text="Sales & Purchases")

        self.setup_dashboard()
        self.setup_inventory()
        self.setup_transactions()

        footer = ttk.Frame(self.root, padding=5)
        footer.pack(fill="x", side="bottom", before=self.tabs)

        ttk.Button(
            footer, text="Refresh All", command=self.manual_refresh
        ).pack(side="left", padx=5)

        ttk.Button(
            footer, text="Import CSV...", command=self.import_csv
        ).pack(side="left", padx=5)

        ttk.Button(
            footer, text="Export Inventory CSV", command=self.export_csv
        ).pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left", padx=15)

        ttk.Button(
            footer, text="Exit", command=self.root.destroy
        ).pack(side="right", padx=5)

    # ---------- AUTO SYNC ----------

    def auto_sync(self):
        """Runs every few seconds: picks up changes from MySQL or inventory.csv."""
        try:
            changed, message = backend.sync_check()

            if changed:
                self.refresh_inventory()
                self.refresh_dashboard()
                self.status_var.set("Data updated from MySQL / CSV")

            # Show a CSV problem once, not every 3 seconds
            if message and message != self.last_sync_error:
                self.last_sync_error = message
                messagebox.showwarning("Sync", message)
            elif not message:
                self.last_sync_error = ""

        except Exception as e:
            self.status_var.set("Sync paused: " + str(e)[:80])

        finally:
            self.root.after(AUTO_SYNC_MS, self.auto_sync)

    def manual_refresh(self):
        try:
            backend.sync_check()
        except Exception:
            pass
        self.refresh_all()

    # ---------- DASHBOARD ----------

    def setup_dashboard(self):
        ttk.Label(
            self.dashboard_tab,
            text="Dashboard Overview",
            font=("Arial", 16, "bold")
        ).pack(anchor="w", pady=(0, 10))

        self.kpi_frame = ttk.Frame(self.dashboard_tab)
        self.kpi_frame.pack(fill="x")

        self.kpi_labels = {}

        cards = [
            ("total_products", "Total Products"),
            ("total_stock", "Units in Stock"),
            ("inventory_value", "Inventory Value"),
            ("low_stock", "Low-Stock Products"),
            ("today_sales", "Today's Sales"),
            ("today_purchases", "Today's Purchases")
        ]

        for index, (key, title) in enumerate(cards):
            card = ttk.LabelFrame(self.kpi_frame, text=title, padding=12)
            card.grid(
                row=0, column=index,
                sticky="nsew", padx=4, pady=4
            )

            label = ttk.Label(card, text="0", font=("Arial", 13, "bold"))
            label.pack()

            self.kpi_labels[key] = label

        for col in range(len(cards)):
            self.kpi_frame.columnconfigure(col, weight=1)

        self.chart_frame = ttk.Frame(self.dashboard_tab)
        self.chart_frame.pack(fill="both", expand=True, pady=(8, 4))

        self.low_frame = ttk.LabelFrame(
            self.dashboard_tab,
            text="Products to Reorder (Quantity <= Reorder Level)",
            padding=5
        )
        self.low_frame.pack(fill="x")

        low_cols = (
            "Product_ID", "SKU", "Product_Name",
            "Category", "Quantity", "Reorder_Level"
        )
        low_heads = ("ID", "SKU", "Product Name", "Category", "Quantity", "Reorder Level")
        low_widths = (60, 130, 240, 160, 100, 120)

        self.low_tree = ttk.Treeview(
            self.low_frame, columns=low_cols, show="headings", height=4
        )

        for col, head, width in zip(low_cols, low_heads, low_widths):
            self.low_tree.heading(col, text=head)
            self.low_tree.column(col, width=width, anchor="center")

        low_scroll = ttk.Scrollbar(
            self.low_frame, orient="vertical", command=self.low_tree.yview
        )
        self.low_tree.configure(yscrollcommand=low_scroll.set)

        self.low_tree.pack(side="left", fill="x", expand=True)
        low_scroll.pack(side="right", fill="y")

    def refresh_dashboard(self):
        data = backend.get_dashboard_kpis()

        for key, label in self.kpi_labels.items():
            value = data[key]

            if key in ("inventory_value", "today_sales", "today_purchases"):
                text = f"₹{value:,.2f}"
            else:
                text = f"{value:,}"

            label.config(text=text)

        for widget in self.chart_frame.winfo_children():
            widget.destroy()

        fig = Figure(figsize=(11, 2.7), dpi=100)

        ax1 = fig.add_subplot(131)
        ax2 = fig.add_subplot(132)
        ax3 = fig.add_subplot(133)

        inventory = backend.get_inventory()

        if not inventory.empty:
            inv = inventory.copy()
            inv["Category"] = (
                inv["Category"].fillna("").replace("", "Uncategorized")
            )

            stock = (
                inv.groupby("Category")["Quantity"].sum()
                .sort_values(ascending=False).head(8)
            )

            if stock.sum() > 0:
                stock.plot(kind="bar", ax=ax1)
                ax1.set_title("Stock by Category", fontsize=10)
                ax1.set_ylabel("Quantity")
            else:
                inv["Category"].value_counts().head(8).plot(kind="bar", ax=ax1)
                ax1.set_title("Products by Category", fontsize=10)
                ax1.set_ylabel("Products")

            ax1.set_xlabel("")
            ax1.tick_params(axis="x", labelrotation=35, labelsize=8)

            top = inv["Product_Name"].value_counts().head(10).sort_values()
            top.plot(kind="barh", ax=ax2)
            ax2.set_title("Top 10 Products (by SKU count)", fontsize=10)
            ax2.set_xlabel("SKUs")
            ax2.set_ylabel("")
            ax2.tick_params(axis="y", labelsize=8)
        else:
            ax1.set_title("No inventory data", fontsize=10)
            ax2.set_title("No inventory data", fontsize=10)

        sales = backend.get_sales_data()

        if not sales.empty:
            ax3.plot(
                sales["Date"].astype(str),
                sales["Total_Amount"],
                marker="o"
            )
            ax3.set_title("Daily Sales", fontsize=10)
            ax3.set_ylabel("Amount (₹)")
            ax3.tick_params(axis="x", labelrotation=35, labelsize=8)
        else:
            ax3.set_title("No sales recorded", fontsize=10)

        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

        # Products that need reordering
        for item in self.low_tree.get_children():
            self.low_tree.delete(item)

        low = backend.get_low_stock()
        shown = low.head(200)

        self.low_frame.config(
            text=(
                f"Products to Reorder (Quantity <= Reorder Level): "
                f"{len(low)}"
                + (" - showing first 200" if len(low) > 200 else "")
            )
        )

        for _, r in shown.iterrows():
            self.low_tree.insert(
                "", "end",
                values=(
                    r["Product_ID"], r["SKU"], r["Product_Name"],
                    r["Category"], r["Quantity"], r["Reorder_Level"]
                )
            )

    # ---------- INVENTORY TAB ----------

    def setup_inventory(self):
        form = ttk.LabelFrame(
            self.inventory_tab, text="Product Details", padding=10
        )
        form.pack(fill="x", pady=(0, 10))

        self.name_var = tk.StringVar()
        self.category_var = tk.StringVar()
        self.price_var = tk.StringVar()
        self.quantity_var = tk.StringVar()
        self.reorder_var = tk.StringVar(value="5")

        fields = [
            ("Product Name", self.name_var),
            ("Category", self.category_var),
            ("Price", self.price_var),
            ("Quantity", self.quantity_var),
            ("Reorder Level", self.reorder_var)
        ]

        for index, (label, variable) in enumerate(fields):
            row = index // 3
            col = (index % 3) * 2

            ttk.Label(form, text=label).grid(
                row=row, column=col, sticky="w", padx=5, pady=5
            )

            ttk.Entry(form, textvariable=variable, width=23).grid(
                row=row, column=col + 1, sticky="ew", padx=5, pady=5
            )

        buttons = ttk.Frame(form)
        buttons.grid(row=2, column=0, columnspan=6, pady=8)

        ttk.Button(
            buttons, text="Add Product", command=self.add_product
        ).pack(side="left", padx=4)

        ttk.Button(
            buttons, text="Update Selected", command=self.update_product
        ).pack(side="left", padx=4)

        ttk.Button(
            buttons, text="Delete Selected", command=self.delete_product
        ).pack(side="left", padx=4)

        ttk.Button(
            buttons, text="Clear Form", command=self.clear_form
        ).pack(side="left", padx=4)

        search_frame = ttk.Frame(self.inventory_tab)
        search_frame.pack(fill="x", pady=5)

        ttk.Label(search_frame, text="Search:").pack(side="left")

        self.search_var = tk.StringVar()

        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=8)
        search_entry.bind("<KeyRelease>", self.search_inventory)

        columns = (
            "Product_ID", "Product_Name", "Category",
            "Price", "Quantity", "Reorder_Level", "SKU"
        )

        self.inventory_tree = ttk.Treeview(
            self.inventory_tab, columns=columns,
            show="headings", height=13
        )

        headings = {
            "Product_ID": "ID",
            "Product_Name": "Product Name",
            "Category": "Category",
            "Price": "Price (₹)",
            "Quantity": "Quantity",
            "Reorder_Level": "Reorder Level",
            "SKU": "SKU"
        }

        widths = {
            "Product_ID": 60,
            "Product_Name": 200,
            "Category": 150,
            "Price": 110,
            "Quantity": 100,
            "Reorder_Level": 110,
            "SKU": 120
        }

        for col in columns:
            self.inventory_tree.heading(col, text=headings[col])
            self.inventory_tree.column(col, width=widths[col], anchor="center")

        self.inventory_tree.pack(fill="both", expand=True)
        self.inventory_tree.bind("<<TreeviewSelect>>", self.select_product)

    def refresh_inventory(self):
        """Reload the table from MySQL, keeping the current search filter."""
        df = backend.get_inventory()
        search_text = self.search_var.get().strip().lower()

        if search_text and not df.empty:
            mask = df.apply(
                lambda row: search_text in " ".join(
                    str(value).lower() for value in row.values
                ),
                axis=1
            )
            df = df[mask]

        for item in self.inventory_tree.get_children():
            self.inventory_tree.delete(item)

        for _, row in df.iterrows():
            self.inventory_tree.insert(
                "", "end",
                values=(
                    row["Product_ID"],
                    row["Product_Name"],
                    row["Category"],
                    f'{float(row["Price"]):.2f}',
                    row["Quantity"],
                    row["Reorder_Level"],
                    row["SKU"]
                )
            )

    def search_inventory(self, event=None):
        self.refresh_inventory()

    def select_product(self, event=None):
        selected = self.inventory_tree.selection()

        if not selected:
            return

        values = self.inventory_tree.item(selected[0], "values")

        self.selected_id = values[0]
        self.name_var.set(values[1])
        self.category_var.set(values[2])
        self.price_var.set(values[3])
        self.quantity_var.set(values[4])
        self.reorder_var.set(values[5])

        # Pre-fill the Sales & Purchases tab
        self.transaction_id.set(values[0])

    def clear_form(self):
        self.selected_id = None
        self.name_var.set("")
        self.category_var.set("")
        self.price_var.set("")
        self.quantity_var.set("")
        self.reorder_var.set("5")

        for item in self.inventory_tree.selection():
            self.inventory_tree.selection_remove(item)

    def get_form_values(self):
        name = self.name_var.get().strip()
        category = self.category_var.get().strip()
        price = float(self.price_var.get())
        quantity = int(self.quantity_var.get())
        reorder = int(self.reorder_var.get())

        if not name:
            raise ValueError("Product name is required.")

        if price < 0 or quantity < 0 or reorder < 0:
            raise ValueError("Values cannot be negative.")

        return name, category, price, quantity, reorder

    def add_product(self):
        try:
            values = self.get_form_values()

            success, message = backend.add_product(*values)
            messagebox.showinfo("Inventory", message)

            if success:
                self.clear_form()
                self.refresh_all()

        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def update_product(self):
        if self.selected_id is None:
            messagebox.showwarning(
                "Select Product", "Select a product from the table first."
            )
            return

        try:
            values = self.get_form_values()

            success, message = backend.update_product(
                self.selected_id, *values
            )

            messagebox.showinfo("Inventory", message)

            if success:
                self.clear_form()
                self.refresh_all()

        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def delete_product(self):
        if self.selected_id is None:
            messagebox.showwarning(
                "Select Product", "Select a product from the table first."
            )
            return

        if not messagebox.askyesno(
            "Confirm Delete", "Delete the selected product?"
        ):
            return

        try:
            success, message = backend.delete_product(self.selected_id)
            messagebox.showinfo("Inventory", message)

            if success:
                self.clear_form()
                self.refresh_all()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---------- SALES AND PURCHASES ----------

    def setup_transactions(self):
        ttk.Label(
            self.transactions_tab,
            text="Record a Sale or Purchase",
            font=("Arial", 16, "bold")
        ).pack(anchor="w", pady=(0, 15))

        form = ttk.LabelFrame(
            self.transactions_tab, text="Transaction Details", padding=15
        )
        form.pack(fill="x")

        self.transaction_id = tk.StringVar()
        self.transaction_quantity = tk.StringVar()

        ttk.Label(form, text="Product ID").grid(
            row=0, column=0, padx=5, pady=8, sticky="w"
        )
        ttk.Entry(form, textvariable=self.transaction_id, width=25).grid(
            row=0, column=1, padx=5, pady=8
        )

        ttk.Label(form, text="Quantity").grid(
            row=1, column=0, padx=5, pady=8, sticky="w"
        )
        ttk.Entry(form, textvariable=self.transaction_quantity, width=25).grid(
            row=1, column=1, padx=5, pady=8
        )

        ttk.Button(
            form, text="Record Sale", command=self.make_sale
        ).grid(row=2, column=0, padx=5, pady=12, sticky="ew")

        ttk.Button(
            form, text="Record Purchase", command=self.make_purchase
        ).grid(row=2, column=1, padx=5, pady=12, sticky="ew")

        ttk.Label(
            self.transactions_tab,
            text=(
                "Select a product in the Inventory tab and its ID is filled in "
                "here. Sales decrease stock; purchases increase stock."
            ),
            wraplength=800
        ).pack(anchor="w", pady=15)

    def make_sale(self):
        try:
            success, message = backend.record_sale(
                self.transaction_id.get(),
                self.transaction_quantity.get()
            )

            messagebox.showinfo("Sale", message)

            if success:
                self.refresh_all()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def make_purchase(self):
        try:
            success, message = backend.record_purchase(
                self.transaction_id.get(),
                self.transaction_quantity.get()
            )

            messagebox.showinfo("Purchase", message)

            if success:
                self.refresh_all()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---------- EXPORT AND REFRESH ----------

    def import_csv(self):
        filename = filedialog.askopenfilename(
            title="Choose a CSV file with product data",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )

        if not filename:
            return

        if not messagebox.askyesno(
            "Import CSV",
            "Add all products from this file to the inventory?\n\n" + filename
        ):
            return

        try:
            success, message = backend.import_csv_file(filename)

            if success:
                messagebox.showinfo("Import CSV", message)
                self.refresh_all()
            else:
                messagebox.showerror("Import CSV", message)

        except Exception as e:
            messagebox.showerror("Import Error", str(e))

    def export_csv(self):
        filename = filedialog.asksaveasfilename(
            title="Export Inventory",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="inventory.csv"
        )

        if not filename:
            return

        try:
            success, message = backend.export_inventory_csv(filename)

            if success:
                messagebox.showinfo("CSV Export", message)
            else:
                messagebox.showerror("CSV Export", message)

        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def refresh_all(self):
        try:
            self.refresh_inventory()
            self.refresh_dashboard()
        except Exception as e:
            messagebox.showerror(
                "Database Error",
                "Could not load data from MySQL.\n\n" + str(e)
            )


# ---------- START APPLICATION ----------

if __name__ == "__main__":
    startup_message = ""
    try:
        startup_message = backend.initialize_database()
    except Exception as e:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror(
            "Startup Error",
            "Could not connect to MySQL.\n\n" + str(e)
        )
        raise SystemExit(1)

    root = tk.Tk()
    app = InventoryApp(root)
    if startup_message:
        root.after(300, lambda: messagebox.showinfo("Sync", startup_message))
    root.mainloop()