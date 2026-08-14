## Fix immutable replay formatting and legacy inventory signs

The first post-merge production run reached Supabase and failed closed on the
existing legacy cash event. The event was financially identical, but numeric
serialization differed (`150000` versus `150000.0`), so the immutable replay
guard treated it as a conflict.

This repair:

- compares legacy replay numeric fields with `Decimal` semantics;
- keeps all source, action, symbol, currency, unit, and date fields strict;
- continues to reject true immutable payload changes;
- marks legacy BUY/SELL/deposit/withdrawal compatibility rows with explicit
  signs in the inventory adapter so historical positions are applied again.

Verification: `python -m unittest discover -s tests -q` — 164 tests passed.

The next main-branch run must verify that the private snapshot contains the
historical positions and the exact TWD 150000 correction, then confirm Telegram
and scheduled workflows.
