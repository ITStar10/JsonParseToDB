V0.2.0 - Column rename (2026/11/23)

- Renamed column "useOrders" to "userOrders" (JSON key and database column)
- Breaking: databases created by V0.1.0 use the old column name and can't be appended to; create a new database from the JSON files

V0.1.0 - Initial Version (2026/11/18)

- Parse JSON files directory with fixed column names
