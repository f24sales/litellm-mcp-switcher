from alembic import context

config = context.config
db = config.attributes["database"]
context.configure(connection=config.attributes["connection"], target_metadata=db.metadata,
                  version_table=config.get_main_option("version_table"))
with context.begin_transaction():
    context.run_migrations()
