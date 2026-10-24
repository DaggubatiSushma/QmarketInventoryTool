from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, false, inspect, select


def upgrade_stores(engine):
    """Put the original workspace in Main Store and scope department names by store."""
    with engine.begin() as connection:
        stores = Table("store", MetaData(), autoload_with=connection)
        store_id = connection.scalar(select(stores.c.id).order_by(stores.c.id).limit(1))
        if store_id is None:
            store_id = connection.execute(stores.insert().values(name="Main Store")).inserted_primary_key[0]
        inspector = inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("department")}
        operations = Operations(MigrationContext.configure(connection))
        if "store_id" not in columns:
            operations.add_column("department", Column("store_id", Integer(), nullable=True))
        departments = Table("department", MetaData(), autoload_with=connection)
        connection.execute(departments.update().where(departments.c.store_id.is_(None)).values(store_id=store_id))
        inspector = inspect(connection)
        columns = {column["name"]: column for column in inspector.get_columns("department")}
        constraints = inspector.get_unique_constraints("department")
        old_unique = next((item for item in constraints if item["column_names"] == ["name"]), None)
        has_unique = any(set(item["column_names"]) == {"store_id", "name"} for item in constraints)
        has_foreign_key = any(item["constrained_columns"] == ["store_id"]
                              for item in inspector.get_foreign_keys("department"))
        if not columns["store_id"]["nullable"] and old_unique is None and has_unique and has_foreign_key:
            return
        with operations.batch_alter_table("department", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
            batch.alter_column("store_id", existing_type=Integer(), nullable=False)
            if old_unique is not None:
                batch.drop_constraint(old_unique["name"] or "uq_department_name", type_="unique")
            if not has_unique:
                batch.create_unique_constraint("uq_department_store_name", ["store_id", "name"])
            if not has_foreign_key:
                batch.create_foreign_key("fk_department_store", "store", ["store_id"], ["id"])


def upgrade_catalog(engine):
    """Upgrade the original catalog without deleting products or weekly history."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("product")}
        unique_constraints = inspector.get_unique_constraints("product")
        old_unique = next((constraint for constraint in unique_constraints
                           if set(constraint["column_names"]) == {"department_id", "name"}), None)
        if {"size", "description"} <= columns and old_unique is None:
            return
        operations = Operations(MigrationContext.configure(connection))
        convention = {"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"}
        with operations.batch_alter_table("product", naming_convention=convention) as batch:
            if "size" not in columns:
                batch.add_column(Column("size", String(80), nullable=False, server_default=""))
            if "description" not in columns:
                batch.add_column(Column("description", String(1000), nullable=False, server_default=""))
            if old_unique is not None:
                batch.drop_constraint(old_unique["name"] or "uq_product_department_id_name", type_="unique")
                batch.create_unique_constraint("uq_product_department_name_size", ["department_id", "name", "size"])


def upgrade_stock_checks(engine):
    """Default existing stock rows to unchecked without changing their revisions."""
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("stock_state")}
        if "checked" not in columns:
            operations = Operations(MigrationContext.configure(connection))
            operations.add_column(
                "stock_state", Column("checked", Boolean(), nullable=False, server_default=false()))


def upgrade_shopping(engine):
    """Add durable shopping metadata without inventing inventory update dates."""
    additions = {
        "store": [
            Column("catalog_revision", Integer(), nullable=False, server_default="1"),
            Column("inventory_updated_at", DateTime(), nullable=True),
            Column("departments_ordered", Boolean(), nullable=False, server_default=false()),
        ],
        "department": [
            Column("position", Integer(), nullable=False, server_default="0"),
            Column("products_ordered", Boolean(), nullable=False, server_default=false()),
        ],
        "product": [Column("position", Integer(), nullable=False, server_default="0")],
        "stock_state": [Column("picked_up", Boolean(), nullable=False, server_default=false())],
    }
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for table, columns in additions.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for column in columns:
                if column.name not in existing:
                    operations.add_column(table, column)
