"""CLI entry point for the SharePoint -> Postgres backfill.

  python -m app.backfill                          # VERIFY all domains (dry run, no writes)
  python -m app.backfill --domain holidays        # verify a single domain
  python -m app.backfill --domain leave_requests --domain overtime_requests
  python -m app.backfill --apply                  # WRITE: upsert SP -> Postgres

Verify mode prints the diff report and exits non-zero if any domain is out of
parity, so it can gate a cutover. Apply mode performs the idempotent upsert and
is safe to re-run. Like uvicorn/alembic, this imports ``app`` and therefore needs
the same secrets present (a backend/.env or the Railway env), and it writes to
whatever DATABASE_URL points at (empty -> local SQLite) — never point it at
production without intent.
"""
import argparse
import asyncio
import json
import sys

from app.backfill.core import DOMAINS, run


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill",
        description="Backfill/verify SharePoint list data into the Postgres business tables.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write SP -> Postgres (idempotent upsert). Default is verify-only (no writes).",
    )
    parser.add_argument(
        "--domain", action="append", dest="domains", choices=list(DOMAINS), metavar="DOMAIN",
        help="limit to this domain (repeatable). Default: all. One of: " + ", ".join(DOMAINS),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    report = asyncio.run(run(domain_names=args.domains, apply=args.apply))
    print(json.dumps(report, indent=2, default=str))
    if not args.apply:
        # Verify mode is a gate: fail if any domain is out of parity.
        all_in_parity = all(d.get("in_parity") for d in report["domains"].values())
        return 0 if all_in_parity else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
