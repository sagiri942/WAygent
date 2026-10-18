from alembic import context
connection = context.config.attributes.get("connection")
if connection is None or connection.dialect.name != "mysql":
    raise RuntimeError("Use upgrade_mysql_schema(CoreDatabase) explicitly")
context.configure(connection=connection, transactional_ddl=False)
with context.begin_transaction():
    context.run_migrations()