"""
Does Dhan's LIVE feed still carry ticks in the 15:15->15:30 window?

READ-ONLY. Subscribes to the Live Market Feed, records Ticker packets, and
prints when the ticks stopped. It never places an order and never mints a
token -- it reads the shared ``dhan_token`` row and opens ONE WebSocket (Dhan
allows five).

    py -3.14 scripts/dhan_feed_closing_bar_probe.py               # until 15:35 IST
    py -3.14 scripts/dhan_feed_closing_bar_probe.py --minutes 5   # smoke test
    py -3.14 scripts/dhan_feed_closing_bar_probe.py --symbols ALKEM,ASHOKLEY

WHY THIS EXISTS
---------------
Dhan's charts REST API stopped serving the closing 15:15->15:30 bar for sessions
from 2026-08-03. Verified 2026-08-30 by fetching one session at a time:

    2026-03-02..06, 06-01..05, 07-27..31   ->  25 bars/day, last stamp 15:15
    2026-08-03 onward (every weekday)      ->  24 bars/day, last stamp 15:00

It is the underlying data, not the 15m aggregation: 1m returns 360 bars ending
15:14, 5m 72 ending 15:10, 60m 6 ending 14:15. ``/v2/charts/historical`` is no
alternative -- it ignores ``interval`` and returns daily candles only.

That bar is 1h bin 6, the stub the meanrev strategy enters from at the next
open (3 of 214 backtest trades). It has been unbuildable live for ~4 weeks.

THE QUESTION THIS ANSWERS, AND ONLY THIS ONE
--------------------------------------------
Is the REST gap a *serving* bug, or is Dhan not ingesting that window at all?

  * Ticks arrive after 15:15 -> the data exists; a small recorder could rebuild
    the bar ourselves, and would also survive the token-eviction blips that
    blind the scanner (an established socket keeps working when fresh REST
    calls 401).
  * No ticks after 15:15     -> the gap is upstream. Do NOT build a recorder;
    it would record nothing. Report it to apihelp@dhan.co instead.

Run it on a TRADING DAY, started by ~15:10 IST.

ON READING THE OUTPUT
---------------------
The verdict is three-way, and the CONTROL window is what makes it honest.
Subscribing yields one stale snapshot tick per symbol even on a Sunday, so
"we received ticks" is not evidence the feed was live. Unless ticks land in
15:00-15:15, silence after 15:15 says nothing and the run reports INCONCLUSIVE.

Dhan's Last Trade Time epoch is not documented as UTC or IST, so the report
prints both readings beside our own arrival clock. A 2026-08-30 smoke test
suggests **the LTT is IST wall-clock stamped as though it were UTC** -- i.e.
do NOT convert it. Reading it that way put the three snapshot ticks at
15:29:11 / 15:50:16 / 15:57:36 on the previous Friday (a close plus two
closing-session prints); read as UTC they landed at 21:20 and later, which is
impossible. Note this is the opposite of the CHARTS API, whose epochs are true
UTC -- ``dhan._parse_candles`` converts, and its bars come out at 09:15 IST
correctly. Three samples is thin, so re-check before relying on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IST = timezone(timedelta(hours=5, minutes=30))
FEED_URL = "wss://api-feed.dhan.co"

# Feed request codes (DhanHQ v2). 15 = Ticker: LTP + last trade time, the
# lightest packet, and all this probe needs.
REQ_SUBSCRIBE_TICKER = 15
REQ_DISCONNECT = 12

# Response codes and their TOTAL packet size (8-byte header + payload). The
# header's own length field is documented as "message length of payload", which
# is ambiguous in the wild, so sizes are pinned for the codes we subscribe to
# and anything unexpected is reported rather than guessed at.
CODE_TICKER, CODE_PREV_CLOSE, CODE_DISCONNECT = 2, 6, 50
PACKET_SIZE = {CODE_TICKER: 16, CODE_PREV_CLOSE: 16, CODE_DISCONNECT: 16}

DEFAULT_SYMBOLS = ["ALKEM", "ASHOKLEY", "360ONE"]
# The bot's own universe cache, maintained by the Dhan adapter. Present
# wherever the bot runs, so this probe is not tied to a dev box.
UNIVERSE_CACHE = Path(__file__).resolve().parents[1] / "data" / "dhan_universe.json"
SCRIP_MASTER = Path(
    r"D:\Claude_TVconnect2\Backtesting Engine\data\cache\dhan_scrip_master.csv"
)


def _load_token() -> tuple[str, str]:
    """The shared token row -- read only. Never mints, never rotates."""
    import os

    from dotenv import load_dotenv

    load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))
    from sqlalchemy import create_engine, text

    url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg", "postgresql+psycopg2"
    )
    eng = create_engine(url)
    with eng.connect() as c:
        row = (
            c.execute(text("select client_id, token from dhan_token"))
            .mappings()
            .first()
        )
    if row is None:
        raise SystemExit("No dhan_token row -- is the bot running?")
    return row["token"], row["client_id"]


def _lookup_table() -> dict[str, tuple[str, str]]:
    """symbol -> (security_id, exchange_segment).

    Prefers the bot's own universe cache, which the Dhan adapter already
    maintains and which exists wherever the bot runs -- so this probe works on
    the Mumbai VM as well as a dev box. Falls back to the backtester's scrip
    master CSV, which is Windows-only.
    """
    if UNIVERSE_CACHE.exists():
        uni = json.loads(UNIVERSE_CACHE.read_text())
        return {
            k.upper(): (v["security_id"], v.get("exchange", "NSE_EQ"))
            for k, v in uni.items()
        }

    print(f"  (no {UNIVERSE_CACHE}; falling back to the scrip master CSV)")
    try:
        import pandas as pd

        df = pd.read_csv(
            SCRIP_MASTER,
            low_memory=False,
            usecols=[
                "EXCH_ID",
                "SEGMENT",
                "SECURITY_ID",
                "INSTRUMENT",
                "UNDERLYING_SYMBOL",
            ],
        )
    except Exception as exc:  # pragma: no cover - diagnostic path
        raise SystemExit(
            f"No universe cache and could not read the scrip master ({exc})."
        ) from exc

    m = df[(df.EXCH_ID == "NSE") & (df.SEGMENT == "E") & (df.INSTRUMENT == "EQUITY")]
    return {
        str(sym).upper(): (str(sid), "NSE_EQ")
        for sym, sid in zip(m.UNDERLYING_SYMBOL, m.SECURITY_ID, strict=False)
    }


def _resolve(symbols: list[str]) -> dict[str, tuple[str, str]]:
    table = _lookup_table()
    out: dict[str, tuple[str, str]] = {}
    for s in symbols:
        hit = table.get(s.upper())
        if hit is None:
            print(f"  ! {s}: not found in the universe, skipping")
            continue
        out[s.upper()] = hit
    if not out:
        raise SystemExit("No symbols resolved.")
    return out


def _parse(buf: bytes, notes: list[str]) -> list[tuple[int, int, float, int]]:
    """Walk one binary frame -> [(code, security_id, ltp, ltt), ...].

    Frames can carry several packets back to back, so this advances by pinned
    size rather than assuming one packet per frame.
    """
    out: list[tuple[int, int, float, int]] = []
    off = 0
    while off + 8 <= len(buf):
        code = buf[off]
        _, declared, _, security_id = struct.unpack_from("<BhBi", buf, off)
        size = PACKET_SIZE.get(code)
        if size is None:
            notes.append(
                f"unknown response code {code} (declared len {declared}) "
                f"-- stopped parsing this frame"
            )
            break
        if off + size > len(buf):
            notes.append(
                f"truncated packet code {code}: need {size}, "
                f"have {len(buf) - off}"
            )
            break
        if code in (CODE_TICKER, CODE_PREV_CLOSE):
            ltp, ltt = struct.unpack_from("<fi", buf, off + 8)
            out.append((code, security_id, ltp, ltt))
        elif code == CODE_DISCONNECT:
            (why,) = struct.unpack_from("<h", buf, off + 8)
            notes.append(f"server sent DISCONNECT, code {why}")
        off += size
    return out


async def _run(symbols: dict[str, str], stop_at: datetime, quiet: bool) -> dict:
    import websockets

    token, client_id = _load_token()
    url = f"{FEED_URL}?version=2&token={token}&clientId={client_id}&authType=2"
    by_sid = {int(sid): sym for sym, (sid, _) in symbols.items()}

    ticks: dict[str, list[tuple[datetime, float, int]]] = defaultdict(list)
    notes: list[str] = []

    print(f"connecting... ({len(symbols)} symbols, until {stop_at:%H:%M:%S} IST)")
    async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
        await ws.send(
            json.dumps(
                {
                    "RequestCode": REQ_SUBSCRIBE_TICKER,
                    "InstrumentCount": len(symbols),
                    "InstrumentList": [
                        {"ExchangeSegment": seg, "SecurityId": sid}
                        for sid, seg in symbols.values()
                    ],
                }
            )
        )
        print("subscribed. recording -- Ctrl+C to stop early.\n")

        while True:
            remaining = (stop_at - datetime.now(IST)).total_seconds()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30))
            except TimeoutError:
                continue
            if isinstance(frame, str):
                notes.append(f"text frame: {frame[:200]}")
                continue
            now = datetime.now(IST)
            for code, sid, ltp, ltt in _parse(frame, notes):
                if code != CODE_TICKER:
                    continue
                sym = by_sid.get(sid, f"sid:{sid}")
                ticks[sym].append((now, ltp, ltt))
                if not quiet:
                    print(f"  {now:%H:%M:%S}  {sym:10s} {ltp:10.2f}  ltt={ltt}")
        try:
            await ws.send(json.dumps({"RequestCode": REQ_DISCONNECT}))
        except Exception as exc:  # noqa: BLE001 - the socket is closing anyway
            notes.append(f"clean disconnect failed ({exc!r}) -- harmless here")
    return {"ticks": ticks, "notes": notes}


def _report(result: dict) -> str:
    ticks: dict[str, list] = result["ticks"]
    notes: list[str] = result["notes"]
    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)

    if notes:
        print("\nProtocol notes:")
        for n in dict.fromkeys(notes):
            print("  -", n)

    if not ticks:
        print(
            "\nNo ticks at all. That is NOT an answer about 15:15 -- it means the\n"
            "subscription never delivered anything. Check the token, the market\n"
            "hours, and the protocol notes above before drawing any conclusion."
        )
        return (
            "Dhan closing-bar probe: <b>NO DATA</b>.\n"
            "The subscription delivered nothing at all, which says nothing about "
            "15:15. Check the token and the market hours."
        )

    sym0 = next(iter(ticks))
    arrival, _, ltt0 = ticks[sym0][0]
    as_utc = datetime.fromtimestamp(ltt0, tz=UTC).astimezone(IST)
    as_ist = datetime.fromtimestamp(ltt0, tz=UTC).replace(tzinfo=IST)
    print(f"\nEpoch reading (first tick, {sym0}):")
    print(f"  arrival (our clock, IST) : {arrival:%Y-%m-%d %H:%M:%S}")
    print(f"  LTT read as UTC -> IST   : {as_utc:%Y-%m-%d %H:%M:%S}")
    print(f"  LTT read as already IST  : {as_ist:%Y-%m-%d %H:%M:%S}")
    print("  (trust arrival; note which LTT column agrees with it)")

    # The CONTROL window. Without ticks here the feed was not delivering at
    # all, and silence after 15:15 says nothing about Dhan's closing data --
    # it just says we were not listening to a live session. Subscribing sends
    # one stale snapshot tick per symbol even on a Sunday, so "we got ticks"
    # is NOT on its own evidence that the feed was live.
    def _in(rows: list, lo: int, hi: int) -> list:
        return [r for r in rows if r[0].hour == 15 and lo <= r[0].minute < hi]

    print(
        f"\n{'symbol':12s} {'ticks':>7s} {'first':>9s} {'last':>9s}"
        f"  {'15:00-15:15':>12s}  {'15:15-15:30':>12s}"
    )
    print("-" * 70)
    n_control = n_after = 0
    for sym in sorted(ticks):
        rows = ticks[sym]
        control, after = _in(rows, 0, 15), _in(rows, 15, 30)
        n_control += len(control)
        n_after += len(after)
        print(
            f"{sym:12s} {len(rows):7d} {rows[0][0]:%H:%M:%S} {rows[-1][0]:%H:%M:%S}"
            f"  {len(control):12d}  {len(after):12d}"
        )

    print("\nTicks per minute, 15:05 -> 15:34 (arrival clock):")
    print("  time " + "".join(f"{s[:6]:>7s}" for s in sorted(ticks)))
    for minute in range(5, 35):
        hh, mm = (15, minute) if minute < 60 else (16, minute - 60)
        row = "".join(
            f"{sum(1 for t, _, _ in ticks[s] if t.hour == hh and t.minute == mm):>7d}"
            for s in sorted(ticks)
        )
        mark = ""
        if (hh, mm) == (15, 15):
            mark = "   <- REST feed stops here"
        elif (hh, mm) == (15, 30):
            mark = "   <- market close"
        print(f"  {hh:02d}:{mm:02d}{row}{mark}")

    print("\n" + "=" * 70)
    if n_after > 0:
        print(f"VERDICT: ticks DID arrive after 15:15 ({n_after} of them).")
        print("  The data exists on the live feed and Dhan is simply not serving it")
        print("  through the charts REST API. Rebuilding bin 6 from a small recorder")
        print("  is feasible. Still report the REST regression -- it is the clean fix.")
    elif n_control > 0:
        print(f"VERDICT: NO ticks after 15:15, and the feed WAS live "
              f"({n_control} ticks in 15:00-15:15).")
        print("  The gap is upstream of the REST API, so a recorder would record")
        print("  nothing. Do not build it. Report to apihelp@dhan.co citing the")
        print("  2026-08-03 cutover, and that the live feed is affected too.")
    else:
        print("VERDICT: INCONCLUSIVE -- no ticks in the 15:00-15:15 control window")
        print("  either, so the feed was not delivering a live session and the")
        print("  silence after 15:15 proves nothing. Subscribing yields one stale")
        print("  snapshot tick per symbol even on a Sunday, which is all this run")
        print("  appears to have caught.")
        print("  Re-run on a TRADING DAY, started by ~15:10 IST.")
    print("=" * 70)

    if n_after > 0:
        return (
            f"Dhan closing-bar probe: <b>TICKS EXIST</b> after 15:15 "
            f"({n_after} ticks; control window {n_control}).\n"
            "The live feed carries the closing window — Dhan is only failing to "
            "SERVE it through the charts REST API. Rebuilding bin 6 from a "
            "recorder is feasible."
        )
    if n_control > 0:
        return (
            f"Dhan closing-bar probe: <b>NO TICKS</b> after 15:15, and the feed "
            f"WAS live ({n_control} ticks in 15:00-15:15).\n"
            "The gap is upstream of the REST API. A recorder would record "
            "nothing — do not build one. Report to apihelp@dhan.co."
        )
    return (
        "Dhan closing-bar probe: <b>INCONCLUSIVE</b>.\n"
        "No ticks in the 15:00-15:15 control window either, so the feed was not "
        "delivering a live session and the silence after 15:15 proves nothing. "
        "Re-run on a trading day, started by ~14:55 IST."
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Probe whether Dhan's live feed carries 15:15->15:30 ticks."
    )
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="run for N minutes instead of until 15:35 IST",
    )
    p.add_argument("--quiet", action="store_true", help="suppress the per-tick log")
    p.add_argument(
        "--alert",
        action="store_true",
        help="also push the verdict to Telegram (the bot's own alert channel)",
    )
    args = p.parse_args()

    now = datetime.now(IST)
    if args.minutes:
        stop_at = now + timedelta(minutes=args.minutes)
    else:
        stop_at = now.replace(hour=15, minute=35, second=0, microsecond=0)
        if stop_at <= now:
            raise SystemExit(
                f"It is {now:%H:%M} IST -- past the 15:35 stop. This probe has to\n"
                "run across the close on a TRADING DAY. Start it by ~15:10, or\n"
                "pass --minutes N to smoke-test the connection now."
            )
    if now.weekday() >= 5:
        print(
            f"! {now:%A} -- markets are shut. Expect no ticks; this only tests\n"
            "  that the socket opens and the subscription is accepted.\n"
        )

    symbols = _resolve([s.strip() for s in args.symbols.split(",") if s.strip()])
    print(
        "symbols: "
        + ", ".join(f"{k}={sid}@{seg}" for k, (sid, seg) in symbols.items())
    )

    try:
        result = asyncio.run(_run(symbols, stop_at, args.quiet))
    except KeyboardInterrupt:
        print("\ninterrupted -- no report (re-run across 15:15 for a verdict).")
        return
    verdict = _report(result)

    if args.alert:
        # The probe fires unattended from a systemd timer, so the verdict has to
        # travel to the reader rather than sit in a log file on the VM.
        from src.core.alerts import send_alert

        sent = send_alert(verdict)
        print(
            "\nTelegram: "
            + ("sent" if sent else "NOT sent (channel disabled or failed)")
        )


if __name__ == "__main__":
    main()
