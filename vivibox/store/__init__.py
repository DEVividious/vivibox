"""vivibox's own database, vivibox.db: the tasks and their events, on the host, in one SQLite file
(ADR-0045). db is the one module that knows the engine; events keeps the tasks' events (K2).
The modules that use it are listed in tests/test_structure.py (STORE_USERS): task.py alone,
which writes every event there before its events.jsonl, the log every reader still reads."""
