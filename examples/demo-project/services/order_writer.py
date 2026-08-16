"""The declared writer for the orders table."""

INSERT_SQL = "INSERT INTO orders (id, total) VALUES (%s, %s)"


def write_order(cur, order):
    cur.execute(INSERT_SQL, (order.id, order.total))
