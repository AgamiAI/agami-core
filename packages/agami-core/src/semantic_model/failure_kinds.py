"""Which of execute_sql's failure kinds are about the connection, and which about the statement.

Shared by everything that runs SQL through execute_sql and has to tell "the database could not be
used" (every statement fails the same way) from "this statement is wrong". Its own module so a
command can import it without importing the introspection engine.
"""

from __future__ import annotations

# The database could not be reached as configured: credentials, driver, sign-in, the network.
CONNECTION_KINDS = frozenset({"dsn", "driver_missing", "auth", "network", "sign_in_required"})
# The statement itself is wrong: a fault of the SQL, never of the connection.
STATEMENT_KINDS = frozenset({"syntax", "column_not_found", "table_not_found", "permission"})
