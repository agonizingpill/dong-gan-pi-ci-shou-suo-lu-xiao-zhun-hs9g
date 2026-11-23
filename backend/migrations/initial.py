"""Install the laboratory schema and add columns introduced after the first release."""
from sqlalchemy import inspect, text

from src import models
from src.db import engine

ADDED_COLUMNS = {
    'chamber_runs': {
        'sealed_at': 'DATETIME NULL',
        'readings_revision': 'INT NOT NULL DEFAULT 0',
    },
    'late_readings': {
        'after_seal': 'TINYINT(1) NOT NULL DEFAULT 0',
    },
}


def install_columns():
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            present = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


if __name__ == '__main__':
    models.Base.metadata.create_all(engine)
    install_columns()
