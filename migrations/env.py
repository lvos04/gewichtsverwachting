from alembic import context
from app.db import Base, engine
from app import models, importer, forecast, operations, settings, crop_profiles, environment, history_import, legacy_lighting, koppeling, bridge  # register all tables

config = context.config
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
