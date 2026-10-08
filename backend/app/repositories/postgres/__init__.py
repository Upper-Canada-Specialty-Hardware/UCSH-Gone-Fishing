"""Postgres-backed repository implementations.

Each one rebuilds the **SharePoint response shape** (``{"id", "fields": {<SP
column name>: ...}}``) from a model row, so the services reading through the
seam behave identically whichever backend their domain flag selects. See
app/repositories/base.py for why that shape is preserved.
"""
