"""Monthly export. Added in good faith by someone who never
found the declared writer."""

FIXUP_SQL = "UPDATE orders SET exported = true WHERE id = %s"


def mark_exported(cur, order_id):
    cur.execute(FIXUP_SQL, (order_id,))
