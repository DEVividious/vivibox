"""vivibox's own database, vivibox.db: the tasks and their events, on the host, in one SQLite file
(ADR-0045). db is the one module that knows the engine. Nothing in the product uses it yet
(tests/test_structure.py, STORE_USERS)."""
