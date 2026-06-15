import functions_framework
import requests
from datetime import datetime, timedelta
import pytz
import gspread
from google.oauth2.service_account import Credentials
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import os

# ── CONFIG ────────────────────────────────────────────────────────────────────
# (tenant_id, zone, open_bkk, close_bkk)
# open/close in minutes from midnight BKK
# peak starts at 17:00 = 1020 min

PEAK_START_MIN = 17 * 60  # 17:00 BKK

CLUBS = {
    "Baan Padel":          ("c849adf7-d4d8-467e-ae86-bb2793a4eaed", "Bangkok",    7*60,  24*60),
    "No Drama Padel":      ("29e0739b-6d56-46c6-926e-0021487bad6b", "Bangkok",    7*60,  24*60),
    "Padel Tropical":      ("57f4af99-70f1-48b0-aca5-054c8eb8660e", "Samui",      7*60,  23*60),
    "Zen Padel Phangan":   ("aad5ac7b-4633-4355-889e-ee91379a7f0e", "Phangan",    8*60,  23*60),
    "Destination Padel":   ("888fb91d-ae69-4e05-841b-06d03c1cd369", "Phuket",     7*60,  23*60),
    "PTP Club":            ("233cc474-47ca-4b9c-acf5-7aac7538e6a3", "Phuket",     8*60,  22*60+30),
    "PAT Tennis & Padel":  ("53bf5103-3d8e-457b-bc8c-6ab290cfca88", "Phuket",     8*60,  21*60),
    "Xplore Padel":        ("1ae5a517-6511-4558-870a-670042ebcb32", "Phuket",     7*60,  24*60),
    "Sensei Padel":        ("bad1c22c-fcd4-46d9-bb8c-dd94d97e31f1", "Phuket",     8*60,  23*60),
    "Prime Padel Pattaya": ("7b4c8a53-cbd0-41e3-ada6-82473cb5ac28", "Pattaya",    8*60,  23*60),
    "Chilli Padel":        ("02de9831-a5ed-4568-9b52-9fe22b2edd12", "Pattaya",   17*60,  22*60),
    "Padel CNX":           ("78b640b1-3ffc-4987-a0ca-72262168f8c4", "Chiang Mai", 8*60,  22*60),
}

ZONES = ["Bangkok", "Phuket", "Pattaya", "Samui", "Phangan", "Chiang Mai"]

SHEET_ID   = os.environ["SHEET_ID"]
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_PASS = os.environ["EMAIL_APP_PASS"]
EMAIL_TO   = os.environ["EMAIL_TO"].split(",")

# ── HELPERS ───────────────────────────────────────────────────────────────────
def club_hours(open_min, close_min):
    """Return (total_offpeak_h, total_peak_h, total_h) for a club."""
    offpeak_h = max(0, min(close_min, PEAK_START_MIN) - open_min)  / 60
    peak_h    = max(0, close_min - max(open_min, PEAK_START_MIN))  / 60
    return round(offpeak_h, 2), round(peak_h, 2), round(offpeak_h + peak_h, 2)

def split_booking(start_min, end_min):
    """Split a booking gap into off-peak and peak hours."""
    offpeak = max(0, min(end_min, PEAK_START_MIN) - start_min) / 60
    peak    = max(0, end_min - max(start_min, PEAK_START_MIN)) / 60
    return round(offpeak, 2), round(peak, 2)

def make_bar(pct):
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled)

def fmt(h):
    total_min = round(h * 60)
    hh, mm    = divmod(total_min, 60)
    return f"{hh}h{mm:02d}" if mm else f"{hh}h"

# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────
@functions_framework.http
def run(request):
    bkk         = pytz.timezone("Asia/Bangkok")
    today       = datetime.now(bkk)
    target_date = today.strftime("%Y-%m-%d")

    sheet = get_sheet()

    for club_name, (tenant_id, zone, open_min, close_min) in CLUBS.items():
        try:
            data = fetch_availability(tenant_id, target_date)
            rows = process_data(club_name, zone, target_date, data, open_min, close_min)
            for row in rows:
                sheet.append_row(list(row.values()))
        except Exception as e:
            print(f"ERROR fetching {club_name}: {e}")

    all_rows   = sheet.get_all_records()
    email_body = build_email(all_rows, today)
    send_email(email_body, target_date)

    return "OK — data written and email sent.", 200

# ── FETCH FROM PLAYTOMIC API ──────────────────────────────────────────────────
# Full browser-like headers to avoid being blocked by Playtomic's bot detection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://playtomic.com/",
    "Origin": "https://playtomic.com",
}

def fetch_availability(tenant_id, date):
    url = (
        f"https://playtomic.com/api/clubs/availability"
        f"?tenant_id={tenant_id}&date={date}&sport_id=PADEL"
    )
    r = requests.get(url, headers=HEADERS, timeout=15)
    if not r.ok:
        print(f"HTTP {r.status_code} from Playtomic — response body: {r.text[:500]}")
    r.raise_for_status()
    return r.json()

# ── PROCESS RAW DATA → ROWS ───────────────────────────────────────────────────
def process_data(club_name, zone, date, api_data, open_min, close_min):
    rows = []
    bkk = pytz.timezone("Asia/Bangkok")
    utc = pytz.utc
    ref = datetime(2000, 1, 1, tzinfo=utc)

    total_offpeak_h, total_peak_h, total_h = club_hours(open_min, close_min)

    for court in api_data:
        court_id = court["resource_id"][:8]
        slots    = court.get("slots", [])

        # Convert all slots to BKK minutes and keep max duration per start time
        bkk_slots = {}
        for s in slots:
            utc_h   = int(s["start_time"][:2])
            utc_m   = int(s["start_time"][3:5])
            bkk_dt  = ref.replace(hour=utc_h, minute=utc_m).astimezone(bkk)
            bkk_min = bkk_dt.hour * 60 + bkk_dt.minute
            dur     = s["duration"]
            if bkk_min not in bkk_slots:
                bkk_slots[bkk_min] = 0
            bkk_slots[bkk_min] = max(bkk_slots[bkk_min], dur)

        # Only keep slots within this club's operating hours
        sorted_slots = sorted(
            [(t, d) for t, d in bkk_slots.items()
             if open_min <= t < close_min],
            key=lambda x: x[0]
        )

        booked_peak_h    = 0
        booked_offpeak_h = 0

        # Gap at start of day
        if sorted_slots:
            first_start = sorted_slots[0][0]
            if first_start > open_min:
                offpeak_h, peak_h = split_booking(open_min, first_start)
                booked_offpeak_h += offpeak_h
                booked_peak_h    += peak_h
        else:
            # No slots = fully booked
            offpeak_h, peak_h = split_booking(open_min, close_min)
            booked_offpeak_h += offpeak_h
            booked_peak_h    += peak_h

        # Walk through slots and find gaps
        for idx, (slot_start, max_dur) in enumerate(sorted_slots):
            court_free_until = slot_start + max_dur
            next_slot_start  = sorted_slots[idx + 1][0] if idx + 1 < len(sorted_slots) else close_min

            if next_slot_start > court_free_until:
                offpeak_h, peak_h = split_booking(court_free_until, next_slot_start)
                booked_offpeak_h += offpeak_h
                booked_peak_h    += peak_h

        rows.append({
            "date":             date,
            "zone":             zone,
            "club":             club_name,
            "court_id":         court_id,
            "booked_peak_h":    round(booked_peak_h, 2),
            "booked_offpeak_h": round(booked_offpeak_h, 2),
            "booked_total_h":   round(booked_peak_h + booked_offpeak_h, 2),
            "total_peak_h":     total_peak_h,
            "total_offpeak_h":  total_offpeak_h,
            "total_h":          total_h,
            "day_of_week":      datetime.strptime(date, "%Y-%m-%d").strftime("%A"),
        })
    return rows

# ── SHEET HELPERS ─────────────────────────────────────────────────────────────
def get_sheet():
    creds_json = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    scopes     = ["https://www.googleapis.com/auth/spreadsheets"]
    creds      = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc         = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).sheet1

# ── AGGREGATE ─────────────────────────────────────────────────────────────────
def aggregate(rows, filter_key, filter_val):
    r = [x for x in rows if x[filter_key] == filter_val]
    if not r:
        return None

    booked_peak    = sum(x["booked_peak_h"]    for x in r)
    booked_offpeak = sum(x["booked_offpeak_h"] for x in r)
    booked_total   = sum(x["booked_total_h"]   for x in r)
    avail_peak     = sum(x["total_peak_h"]     for x in r)
    avail_offpeak  = sum(x["total_offpeak_h"]  for x in r)
    avail_total    = sum(x["total_h"]          for x in r)

    def pct(b, t): return round(b / t * 100, 1) if t else 0

    return {
        "booked_total":   booked_total,   "avail_total":   avail_total,
        "booked_peak":    booked_peak,    "avail_peak":    avail_peak,
        "booked_offpeak": booked_offpeak, "avail_offpeak": avail_offpeak,
        "pct_total":   pct(booked_total,   avail_total),
        "pct_peak":    pct(booked_peak,    avail_peak),
        "pct_offpeak": pct(booked_offpeak, avail_offpeak),
    }

# ── BUILD EMAIL ───────────────────────────────────────────────────────────────
def build_email(all_rows, today):
    target_date = today.strftime("%Y-%m-%d")
    week_start  = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    month_start = today.strftime("%Y-%m-01")

    today_rows = [r for r in all_rows if r["date"] == target_date]
    wtd_rows   = [r for r in all_rows if r["date"] >= week_start]
    mtd_rows   = [r for r in all_rows if r["date"] >= month_start]

    lines = []
    lines.append("=" * 58)
    lines.append("  THAILAND PADEL — Daily Occupancy Report")
    lines.append(f"  {today.strftime('%A, %d %b %Y')}")
    lines.append("=" * 58)

    for zone in ZONES:
        zone_clubs = [name for name, (_, z, _, _) in CLUBS.items() if z == zone]
        if not zone_clubs:
            continue

        lines.append(f"\n{'═' * 58}")
        lines.append(f"  {zone.upper()}")
        lines.append(f"{'═' * 58}")

        for club in zone_clubs:
            lines.append(f"\n  {club}")
            lines.append(f"  {'─' * 50}")
            for label, rows in [
                (f"Today ({target_date})",            today_rows),
                (f"WTD   (Mon {week_start})",          wtd_rows),
                (f"MTD   ({today.strftime('%b %Y')})", mtd_rows),
            ]:
                agg = aggregate(rows, "club", club)
                if not agg:
                    lines.append(f"    {label}: no data")
                    continue
                lines.append(f"    {label}")
                lines.append(f"      Overall  {make_bar(agg['pct_total'])}  {agg['pct_total']}%  ({fmt(agg['booked_total'])} / {fmt(agg['avail_total'])})")
                lines.append(f"      Peak     {make_bar(agg['pct_peak'])}  {agg['pct_peak']}%  ({fmt(agg['booked_peak'])} / {fmt(agg['avail_peak'])})  [17-24h]")
                lines.append(f"      Off-peak {make_bar(agg['pct_offpeak'])}  {agg['pct_offpeak']}%  ({fmt(agg['booked_offpeak'])} / {fmt(agg['avail_offpeak'])})  [07-17h]")

        # Zone summary
        lines.append(f"\n  {zone.upper()} SUMMARY")
        lines.append(f"  {'─' * 50}")
        for label, rows in [
            ("Today", today_rows),
            ("WTD",   wtd_rows),
            ("MTD",   mtd_rows),
        ]:
            agg = aggregate(rows, "zone", zone)
            if not agg:
                continue
            lines.append(
                f"    {label:<6} {make_bar(agg['pct_total'])}  {agg['pct_total']}%  "
                f"|  Peak {agg['pct_peak']}%  "
                f"|  Off-peak {agg['pct_offpeak']}%"
            )

    # Grand total
    lines.append(f"\n{'=' * 58}")
    lines.append("  ALL THAILAND")
    lines.append(f"{'─' * 58}")
    for label, rows in [
        (f"Today ({target_date})",            today_rows),
        (f"WTD   (Mon {week_start})",          wtd_rows),
        (f"MTD   ({today.strftime('%b %Y')})", mtd_rows),
    ]:
        if not rows:
            continue
        bt = sum(x["booked_total_h"]   for x in rows)
        bp = sum(x["booked_peak_h"]    for x in rows)
        bo = sum(x["booked_offpeak_h"] for x in rows)
        at = sum(x["total_h"]          for x in rows)
        ap = sum(x["total_peak_h"]     for x in rows)
        ao = sum(x["total_offpeak_h"]  for x in rows)

        def pct(b, t): return round(b / t * 100, 1) if t else 0
        lines.append(f"    {label}")
        lines.append(f"      Overall  {make_bar(pct(bt,at))}  {pct(bt,at)}%  ({fmt(bt)} / {fmt(at)})")
        lines.append(f"      Peak     {make_bar(pct(bp,ap))}  {pct(bp,ap)}%  ({fmt(bp)} / {fmt(ap)})  [17-24h]")
        lines.append(f"      Off-peak {make_bar(pct(bo,ao))}  {pct(bo,ao)}%  ({fmt(bo)} / {fmt(ao)})  [07-17h]")

    lines.append(f"\n{'=' * 58}")
    lines.append(f"  Generated: {today.strftime('%Y-%m-%d %H:%M')} BKK  |  Source: Playtomic")
    lines.append("=" * 58)

    return "\n".join(lines)

# ── SEND EMAIL ────────────────────────────────────────────────────────────────
def send_email(body, date):
    msg            = MIMEMultipart()
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(EMAIL_TO)
    msg["Subject"] = f"Thailand Padel — Occupancy Report | {date}"
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
