import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine

from job_finder.config import ROOT
from job_finder.schema import metadata

url = context.config.get_main_option("sqlalchemy.url")
if not url:
    load_dotenv(ROOT / ".env")
    url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
