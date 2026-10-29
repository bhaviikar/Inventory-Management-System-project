# Inventory Management System

A Python desktop application for managing products, stock, sales and purchases. It uses a Tkinter interface and a MySQL database, and includes a dashboard with charts, low-stock alerts and CSV import/export.

## Overview
The application replaces manual inventory records with a central database. Users can manage product details, record sales and purchases that update stock automatically, and review inventory through a visual dashboard.

## Key Features
- **Product management**: add, view, update, delete and search products
- **Sales and purchases**: record transactions by product ID and quantity, with stock updated automatically
- **Dashboard**: KPI cards (total products, units in stock, inventory value, low-stock items, today's sales and purchases) and 3 Matplotlib charts
- **Low-stock alerts**: products at or below their reorder level are flagged
- **CSV import/export**: validates required columns and numeric values, detects duplicate IDs and applies changes in a database transaction
- **Auto-sync**: checks for changes in MySQL and the CSV file every 3 seconds and refreshes the interface

## Tech Stack
- Python
- Tkinter (GUI)
- MySQL with mysql-connector-python
- Pandas
- Matplotlib

## Architecture
The project has two layers:
- `main.py`: the Tkinter frontend that handles user interaction and displays data
- `backend.py`: database operations, inventory rules, transaction processing and CSV synchronization

## Database Design
| Table | Purpose |
|-------|---------|
| `inventory` | Product details and current stock (ID, name, category, price, quantity, reorder level, SKU) |
| `sales` | Sales transactions |
| `purchases` | Purchase transactions |

The database and tables are created automatically on first run.

## CSV Format
Required columns: `Product_ID`, `Product_Name`, `Category`, `Price`, `Quantity`, `Reorder_Level`. The `SKU` column is optional.

## Low-Stock Logic
A product is flagged when `Quantity <= Reorder_Level`.


## Limitations
- Designed for small stores or labs; no multi-branch support or user roles
- Runs as a local desktop app

## Author
Bhavika Rane | [LinkedIn](https://www.linkedin.com/in/bhavika-rane-ba1b4232b/)
