"""Declared caller-transactional — but look at line 8."""


def persist(conn, rows):
    cur = conn.cursor()
    for row in rows:
        cur.execute("INSERT INTO audit (row) VALUES (%s)", (row,))
    conn.commit()   # the caller thought THEY owned this transaction
