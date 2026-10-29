"""``agenteval run``: ``plan`` turns a run file into a matrix, ``execute`` runs it.

Both work without click so they can be tested directly. Heavy modules (Inspect tasks,
sandbox providers, the report builder) are imported lazily so that ``run --dry-run``
stays fast and works even when an optional provider is unavailable.
"""
