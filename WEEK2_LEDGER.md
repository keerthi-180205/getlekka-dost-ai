# Week 2 – Your first core file: `app/ledger.py`

This is the heart of DukaanDost: the credit book itself.
**You write it.** I wrote the database tables and the tests.

---

## 1. The idea in simple words

A shop's notebook has lines like:

| # | Customer | What happened | Amount |
|---|---|---|---|
| 1 | Suresh | took goods on credit | 650 |
| 2 | Suresh | took goods on credit | 450 |
| 3 | Suresh | paid back | 200 |
| 4 | – | cash sale | 1500 |

Our database stores exactly this, in the `entries` table. Each line is one row.

**Suresh's balance** is never written down anywhere. We *calculate* it every time:

```
credits  = 650 + 450 = 1100
payments = 200
balance  = 1100 - 200 = 900   → Suresh owes ₹900
```

Why calculate instead of storing? A stored number can get out of sync with the
entries. A calculated one is always right.

**Undo** doesn't erase a line. It marks it `undone` (like striking it out in the
notebook). Undone lines are ignored in every calculation, but we keep them as history.

---

## 2. The four tables (already created in `db.py`)

```
shops       id | owner_phone | shop_name | upi_id | ...
customers   id | shop_id | name ("Suresh") | name_key ("suresh") | ...
entries     id | shop_id | customer_id | kind | amount | due_date | status | ...
                                       credit / payment / sale      active / undone
messages    (Week 1 – don't touch)
```

Every customer and every entry belongs to a shop (`shop_id`). Always filter by
`shop_id`, so one shop can never see another shop's money.

---

## 3. Setup (one time)

```
source venv/bin/activate
pip install -r requirements.txt      # adds pytest
pytest -v                            # run the tests
```

Right now almost everything fails or shows ERROR – that's expected.
Most tests need `get_or_create_shop()` first, so **many turn green as soon as you
write that one function.**

---

## 4. Write the functions in this order

| Order | Function | Tests that turn green |
|---|---|---|
| 1 | `get_or_create_shop` | the `shop` tests, and unlocks the rest |
| 2 | `get_or_create_customer` | customer names, empty name |
| 3 | `add_credit` | bad amounts, due date |
| 4 | `get_balance` | credit / payment / balance tests |
| 5 | `add_payment` | overpayment, credit and payment |
| 6 | `add_cash_sale` | cash sale |
| 7 | `get_all_dues` | dues list |
| 8 | `undo_last` | all undo tests |

After each function: save → `pytest -v` → see what turned green.
Goal: **27 passed**.

---

## 5. SQL cheat sheet (the patterns you'll need)

Open the database:
```python
with db.connect() as conn:
    ...   # changes are saved automatically when this block ends
```

Read one row:
```python
row = conn.execute("SELECT id FROM shops WHERE owner_phone = ?", (phone,)).fetchone()
if row:            # None when nothing found
    print(row["id"])
```

Insert and get the new id:
```python
cursor = conn.execute(
    "INSERT INTO shops (owner_phone, created_at) VALUES (?, ?)",
    (phone, db._now()),
)
new_id = cursor.lastrowid
```

Read many rows:
```python
for row in conn.execute("SELECT name FROM customers WHERE shop_id = ?", (shop_id,)):
    print(row["name"])
```

Update:
```python
conn.execute("UPDATE entries SET status = 'undone' WHERE id = ?", (entry_id,))
```

**Always use `?` placeholders**, never f-strings, for values in SQL.
(`f"... WHERE name = '{name}'"` breaks on names like `D'Souza` and is a security hole called *SQL injection*.)

### Hint for `get_balance` – one query can do the whole sum

```sql
SELECT SUM(CASE WHEN kind = 'credit'  THEN amount
                WHEN kind = 'payment' THEN -amount
                ELSE 0 END)
FROM entries
WHERE customer_id = ? AND status = 'active'
```
`CASE` works like if/else inside SQL: credits count as plus, payments as minus.
If there are no rows, `SUM` returns `None` → turn that into `0`.

### Hint for `undo_last` – getting the customer's name too

```sql
SELECT e.id, e.kind, e.amount, c.name
FROM entries e
LEFT JOIN customers c ON c.id = e.customer_id
WHERE e.shop_id = ? AND e.status = 'active'
ORDER BY e.id DESC
LIMIT 1
```
`LEFT JOIN` keeps cash sales (no customer) – their `name` comes back as `None`.

---

## 6. When a test fails

Read the red part from the bottom up. It tells you:
- which test failed
- what it expected (`assert ... == 650`)
- what your code returned instead

Common ones:
| You see | Usually means |
|---|---|
| `NotImplementedError` | You haven't written that function yet (or forgot to delete the `raise` line) |
| `assert None == 650` | Your function forgot to `return` |
| `assert 1300 == 650` | You counted something twice, or didn't skip `undone` entries |
| `ValueError not raised` | Missing `_check_amount(amount)` at the start |
| `sqlite3.IntegrityError` | Inserting something the table rules forbid (check the `CHECK` rules in `db.py`) |

---

## 7. When all 27 pass

Send me your `ledger.py`. I'll review it like a senior developer would
(correctness, edge cases, readability), and then we connect it to WhatsApp messages
so `Suresh 650 credit` in the simulator really updates the book.

**Rule for this week:** you can ask me to *explain* anything, give a *hint*, or
*explain an error* – but try writing each function yourself first.
