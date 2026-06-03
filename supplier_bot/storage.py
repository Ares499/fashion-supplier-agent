import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import Product, ProductStatus, Supplier


SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
  supplier_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  contact_name TEXT NOT NULL,
  external_user_id TEXT NOT NULL,
  main_categories TEXT NOT NULL,
  sample_address TEXT NOT NULL,
  send_frequency TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
  product_id TEXT PRIMARY KEY,
  supplier_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  primary_image TEXT NOT NULL,
  related_images TEXT NOT NULL,
  category_lv1 TEXT NOT NULL,
  category_lv2 TEXT NOT NULL,
  phash TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  report_box TEXT,
  FOREIGN KEY(supplier_id) REFERENCES suppliers(supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_products_date ON products(received_at);
CREATE INDEX IF NOT EXISTS idx_products_supplier ON products(supplier_id);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA)

    def upsert_supplier(self, supplier: Supplier) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO suppliers VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(supplier_id) DO UPDATE SET
                  name=excluded.name,
                  contact_name=excluded.contact_name,
                  external_user_id=excluded.external_user_id,
                  main_categories=excluded.main_categories,
                  sample_address=excluded.sample_address,
                  send_frequency=excluded.send_frequency,
                  paused=excluded.paused
                """,
                (
                    supplier.supplier_id,
                    supplier.name,
                    supplier.contact_name,
                    supplier.external_user_id,
                    json.dumps(supplier.main_categories, ensure_ascii=False),
                    supplier.sample_address,
                    supplier.send_frequency,
                    1 if supplier.paused else 0,
                ),
            )

    def list_suppliers(self, include_paused: bool = False) -> List[Supplier]:
        sql = "SELECT * FROM suppliers"
        if not include_paused:
            sql += " WHERE paused = 0"
        sql += " ORDER BY supplier_id"
        with self.connect() as con:
            return [self._supplier_from_row(row) for row in con.execute(sql)]

    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM suppliers WHERE supplier_id = ?", (supplier_id,)).fetchone()
            return self._supplier_from_row(row) if row else None

    def upsert_product(self, product: Product) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                  supplier_id=excluded.supplier_id,
                  received_at=excluded.received_at,
                  primary_image=excluded.primary_image,
                  related_images=excluded.related_images,
                  category_lv1=excluded.category_lv1,
                  category_lv2=excluded.category_lv2,
                  phash=excluded.phash,
                  status=excluded.status,
                  confidence=excluded.confidence,
                  report_box=excluded.report_box
                """,
                (
                    product.product_id,
                    product.supplier_id,
                    product.received_at.isoformat(),
                    product.primary_image,
                    json.dumps(product.related_images, ensure_ascii=False),
                    product.category_lv1,
                    product.category_lv2,
                    product.phash,
                    product.status.value,
                    product.confidence,
                    json.dumps(product.report_box) if product.report_box else None,
                ),
            )

    def list_products_for_date(self, date: str) -> List[Product]:
        start = f"{date}T00:00:00"
        end = f"{date}T23:59:59"
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM products WHERE received_at BETWEEN ? AND ? ORDER BY category_lv1, category_lv2, supplier_id, product_id",
                (start, end),
            )
            return [self._product_from_row(row) for row in rows]

    def list_products(self) -> List[Product]:
        with self.connect() as con:
            return [self._product_from_row(row) for row in con.execute("SELECT * FROM products ORDER BY received_at")]

    def get_product(self, product_id: str) -> Optional[Product]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
            return self._product_from_row(row) if row else None

    def update_status(self, product_ids: Iterable[str], status: ProductStatus) -> None:
        with self.connect() as con:
            con.executemany(
                "UPDATE products SET status = ? WHERE product_id = ?",
                [(status.value, product_id) for product_id in product_ids],
            )

    def export_suppliers_json(self, path: Path) -> None:
        suppliers = [supplier.__dict__ for supplier in self.list_suppliers(include_paused=True)]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(suppliers, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _supplier_from_row(row: sqlite3.Row) -> Supplier:
        return Supplier(
            supplier_id=row["supplier_id"],
            name=row["name"],
            contact_name=row["contact_name"],
            external_user_id=row["external_user_id"],
            main_categories=json.loads(row["main_categories"]),
            sample_address=row["sample_address"],
            send_frequency=row["send_frequency"],
            paused=bool(row["paused"]),
        )

    @staticmethod
    def _product_from_row(row: sqlite3.Row) -> Product:
        return Product(
            product_id=row["product_id"],
            supplier_id=row["supplier_id"],
            received_at=datetime.fromisoformat(row["received_at"]),
            primary_image=row["primary_image"],
            related_images=json.loads(row["related_images"]),
            category_lv1=row["category_lv1"],
            category_lv2=row["category_lv2"],
            phash=row["phash"],
            status=ProductStatus(row["status"]),
            confidence=float(row["confidence"]),
            report_box=json.loads(row["report_box"]) if row["report_box"] else None,
        )

