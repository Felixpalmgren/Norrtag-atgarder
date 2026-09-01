"""
fordonskontroll_server.py – VR Norrtåg Bistro-kontroll
=======================================================
Kör med:  python fordonskontroll_server.py
Kräver:   pip install flask flask-cors requests  (installeras automatiskt)

Öppna sedan webbläsaren på:  http://localhost:3100
"""

import sys, subprocess

def ensure(pkg):
    try:
        __import__(pkg.replace("-","_").split("[")[0])
    except ImportError:
        print(f"Installerar {pkg}...")
        subprocess.check_call([sys.executable,"-m","pip","install",pkg,"--quiet"])

for p in ["flask","flask-cors","requests"]:
    ensure(p)

import threading, time, smtplib, json, logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests as req

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fordonskontroll")

app = Flask(__name__, static_folder=".")
CORS(app)

# ══════════════════════════════════════════════════════════════════════════
# DELAT STATE
# ══════════════════════════════════════════════════════════════════════════
lock = threading.Lock()

import os

SEEN_TX_FILE = "seen_tx_ids.json"

def load_seen_tx():
    """Ladda tidigare behandlade transaktions-ID:n från fil."""
    try:
        if os.path.exists(SEEN_TX_FILE):
            with open(SEEN_TX_FILE, "r") as f:
                data = json.load(f)
                logging.getLogger("fordonskontroll").info("Laddade %d sparade transaktions-ID:n", len(data))
                return set(data)
    except Exception as e:
        logging.getLogger("fordonskontroll").warning("Kunde inte ladda seen_tx_ids: %s", e)
    return set()

def save_seen_tx(seen):
    """Spara behandlade transaktions-ID:n till fil."""
    try:
        with open(SEEN_TX_FILE, "w") as f:
            json.dump(list(seen), f)
    except Exception as e:
        logging.getLogger("fordonskontroll").warning("Kunde inte spara seen_tx_ids: %s", e)

# ── Läs credentials från miljövariabler (Render) eller använd defaults ──
_env_zettle_key       = os.environ.get("ZETTLE_API_KEY", "")
_env_zettle_client_id = os.environ.get("ZETTLE_CLIENT_ID", "01a0470e-ffde-78bc-b292-d9e21379c9e7")

state = {
    # Zettle-konfiguration
    "zettle_client_id":  _env_zettle_client_id,
    "zettle_api_key":    _env_zettle_key,
    "access_token":      None,
    "token_expires":     0,

    # Polling
    "polling_active":    False,
    "poll_interval_s":   60,
    "last_poll":         None,
    "seen_tx_ids":       load_seen_tx(),  # laddas från fil vid omstart

    # Positionslista
    "positions": [],

    # Rapport
    "violations":        [],
    "all_transactions":  [],
    "ok_count":          0,
    "total_count":       0,

    # E-post
    "smtp_host":  "smtp.gmail.com",
    "smtp_port":  587,
    "smtp_user":  "",
    "smtp_pass":  "",
    "smtp_from":  "",
    "notify_cc":  "",
    "test_mode":  True,
    "name_map":   {           # Zettle-namn (lowercase) → positionslistans namn (lowercase)
        "olha bozhko": "olha bergman",
    },
    "teams_log":  [],         # logg över skickade Teams-meddelanden
}

ZETTLE_AUTH_URL = "https://oauth.zettle.com/token"
ZETTLE_PURCHASE_URL = "https://purchase.izettle.com/purchases/v2"

# ══════════════════════════════════════════════════════════════════════════
# ZETTLE AUTH
# ══════════════════════════════════════════════════════════════════════════
def get_token():
    with lock:
        if state["access_token"] and time.time() < state["token_expires"] - 60:
            return state["access_token"]
        client_id = state["zettle_client_id"]
        api_key   = state["zettle_api_key"]

    if not api_key:
        raise RuntimeError("Zettle API-nyckel saknas – fyll i den i appen")

    r = req.post(ZETTLE_AUTH_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id":  client_id,
            "assertion":  api_key,
        },
        timeout=15,
    )

    if not r.ok:
        raise RuntimeError(f"Zettle auth misslyckades: {r.status_code} {r.text}")

    data = r.json()
    token      = data["access_token"]
    expires_in = data.get("expires_in", 7200)

    with lock:
        state["access_token"]  = token
        state["token_expires"] = time.time() + expires_in

    log.info("Zettle-token förnyad (gäller %d min)", expires_in // 60)
    return token


# ══════════════════════════════════════════════════════════════════════════
# ZETTLE PURCHASE API
# ══════════════════════════════════════════════════════════════════════════
def fetch_purchases(start_dt, end_dt):
    """
    Hämtar köp från Zettle Purchase API.
    start_dt / end_dt: datetime-objekt i UTC
    Returnerar lista av purchase-dict.
    """
    token     = get_token()
    purchases = []
    last_hash = None

    # Zettle Purchase API använder ISO-format utan millisekunder
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    page = 0
    while True:
        page += 1
        params = {
            "startDate": start_str,
            "endDate":   end_str,
            "limit":     200,
            "descending": "true",
        }
        if last_hash:
            params["lastPurchaseHash"] = last_hash

        r = req.get(ZETTLE_PURCHASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )

        if r.status_code == 401:
            with lock:
                state["access_token"] = None
            token = get_token()
            continue

        if not r.ok:
            log.error("Zettle Purchase API fel %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()

        data  = r.json()

        # Debug: logga rådata från första köpet
        if page == 1 and data:
            first = data[0] if isinstance(data, list) else data.get("purchases", [data])[0] if data else {}
            log.info("DEBUG – första köpet rådata:\n%s",
                     json.dumps(first, ensure_ascii=False, indent=2))

        # Zettle returnerar antingen en lista direkt eller {"purchases": [...]}
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("purchases", [])

        purchases.extend(batch)
        log.info("Sida %d: %d köp (totalt %d)", page, len(batch), len(purchases))

        # Paginering
        last_hash = data.get("lastPurchaseHash") if isinstance(data, dict) else None
        if not last_hash or len(batch) < 200 or page > 50:
            break

    return purchases


# ══════════════════════════════════════════════════════════════════════════
# FORDON-NORMALISERING
# ══════════════════════════════════════════════════════════════════════════
TRAFIKVERKET_API_KEY = "c47fec2042bf483187b480048922ba3c"
TRAFIKVERKET_URL     = "https://api.trafikinfo.trafikverket.se/v2/data.json"

# Cache för tågtider: {(tagnummer, datum): faktisk_ankomsttid_str}
train_time_cache = {}
train_time_lock  = threading.Lock()


def get_actual_arrival(train_number, date_str, till_station=""):
    """
    Hämtar faktisk ankomsttid för ett tåg vid en specifik slutstation.
    Returnerar "HH:MM" i svensk tid, eller None om okänt.
    """
    cache_key = (str(train_number), date_str, till_station)
    with train_time_lock:
        if cache_key in train_time_cache:
            return train_time_cache[cache_key]

    try:
        start = f"{date_str}T00:00:00.000+02:00"
        end   = f"{date_str}T23:59:59.000+02:00"

        # Bygg stationsfilter om slutstation är känd
        station_filter = ""
        if till_station:
            station_filter = f"<EQ name='LocationSignature' value='{till_station}'/>"

        query = f"""<REQUEST>
  <LOGIN authenticationkey='{TRAFIKVERKET_API_KEY}'/>
  <QUERY objecttype='TrainAnnouncement' schemaversion='1.8'>
    <FILTER>
      <AND>
        <EQ name='AdvertisedTrainIdent' value='{train_number}'/>
        <EQ name='ActivityType' value='Ankomst'/>
        <GT name='AdvertisedTimeAtLocation' value='{start}'/>
        <LT name='AdvertisedTimeAtLocation' value='{end}'/>
        {station_filter}
      </AND>
    </FILTER>
    <INCLUDE>AdvertisedTimeAtLocation</INCLUDE>
    <INCLUDE>TimeAtLocation</INCLUDE>
    <INCLUDE>EstimatedTimeAtLocation</INCLUDE>
    <INCLUDE>LocationSignature</INCLUDE>
  </QUERY>
</REQUEST>"""

        r = req.post(TRAFIKVERKET_URL,
            headers={"Content-Type": "application/xml"},
            data=query.encode("utf-8"),
            timeout=10)

        if not r.ok:
            log.warning("Trafikverket HTTP %s för tåg %s", r.status_code, train_number)
            return None

        data      = r.json()
        announces = data.get("RESPONSE", {}).get("RESULT", [{}])[0].get("TrainAnnouncement", [])

        if not announces:
            log.debug("Ingen ankomst vid %s för tåg %s datum %s",
                      till_station or "slutstation", train_number, date_str)
            return None

        # Ta första (och enda om station filtreras) ankomsten
        last = announces[-1]
        raw_time = (last.get("TimeAtLocation")
                 or last.get("EstimatedTimeAtLocation")
                 or last.get("AdvertisedTimeAtLocation", ""))

        if not raw_time:
            return None

        dt       = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        dt_local = dt + timedelta(hours=2)
        result   = dt_local.strftime("%H:%M")

        with train_time_lock:
            train_time_cache[cache_key] = result

        log.info("Tåg %s→%s datum %s faktisk ankomst: %s",
                 train_number, till_station or "?", date_str, result)
        return result

    except Exception as e:
        log.warning("Trafikverket-fel för tåg %s: %s – returnerar None", train_number, e)
        return None
    """Strippa X-prefix: 'X3112' → '3112', '62001' → '62001'"""
    return str(v or "").lstrip("Xx").strip()


def name_to_email(full_name):
    """Generera e-post från namn: 'Kristin Holmgren' → 'kristin.holmgren@vrsverige.com'"""
    sv_map = {'å':'a','ä':'a','ö':'o','é':'e','è':'e','ü':'u',
              'Å':'a','Ä':'a','Ö':'o','É':'e','È':'e','Ü':'u'}
    s = ''.join(sv_map.get(c, c) for c in full_name)
    s = s.lower()
    import re
    s = re.sub(r'[^a-z0-9\-]', '.', s)
    s = re.sub(r'\.+', '.', s).strip('.')
    return s + '@vrsverige.com'


# ══════════════════════════════════════════════════════════════════════════
# POSITIONSLISTA
# ══════════════════════════════════════════════════════════════════════════
def get_effective_end(p):
    """
    Returnerar effektiv sluttid via faktisk ankomsttid från Trafikverket
    vid rätt slutstation. Faller tillbaka på schemalagd tid + 30 min.
    """
    scheduled_end = p.get("end", "")
    fallback      = add_minutes(scheduled_end, 30) if scheduled_end else "23:59"

    try:
        tagnummer    = p.get("tagnummer", "").strip()
        date_str     = p.get("date", "")
        till_station = p.get("till_station", "").strip().upper()

        if not tagnummer or not date_str:
            return fallback

        actual = get_actual_arrival(tagnummer, date_str, till_station)
        if actual:
            return add_minutes(actual, 5)
    except Exception as e:
        log.warning("get_effective_end fel: %s", e)

    return fallback


def find_position(date_str, name_key, time_str):
    with lock:
        positions = list(state["positions"])
    t = time_str[:5] if time_str else "00:00"
    candidates = []
    for p in positions:
        if p["date"] != date_str:
            continue
        if p["name_key"] != name_key:
            continue
        if p["start"] and p["end"]:
            effective_end   = get_effective_end(p)
            effective_start = add_minutes(p["start"], -10)   # 10 min före avgång
            if not (effective_start <= t <= effective_end):
                continue
        candidates.append(p)
    # Välj kandidaten vars starttid är närmast (men före) köptidpunkten
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.get("start",""))


def find_position_by_email(date_str, email, time_str):
    if not email:
        return None
    with lock:
        positions = list(state["positions"])
    email_key = email.lower().strip()
    t = time_str[:5] if time_str else "00:00"
    candidates = []
    for p in positions:
        if p["date"] != date_str:
            continue
        if p.get("email", "").lower().strip() != email_key:
            continue
        if p["start"] and p["end"]:
            effective_end   = get_effective_end(p)
            effective_start = add_minutes(p["start"], -10)   # 10 min före avgång
            if not (effective_start <= t <= effective_end):
                continue
        candidates.append(p)
    # Välj kandidaten vars starttid är närmast (men före) köptidpunkten
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.get("start",""))


def add_minutes(time_str, minutes):
    """Lägg till minuter på ett HH:MM-värde, returnerar HH:MM."""
    try:
        h, m = map(int, time_str.split(":"))
        total = h * 60 + m + minutes
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"
    except Exception:
        return time_str


def normalize_vehicle(v):
    """Strippa X-prefix: 'X3112' → '3112', '62001' → '62001'"""
    return str(v or "").lstrip("Xx").strip()


# Cooldown: {seller_email: senaste_notis_unix_tid}
teams_cooldown = {}
teams_cooldown_lock = threading.Lock()
TEAMS_COOLDOWN_SECONDS = 3600  # 1 timme

TEAMS_WEBHOOK_URL = "https://default2913ee4980354f15ac37deb801e243.6d.environment.api.powerplatform.com:443/powerautomate/automations/direct/cu/11/workflows/4c01d34a0f0f4d9fb1edfe72db603143/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=-8SE3la6u0Z6EwpwqqKXTNNkimaT9saIYVRWL2YRz4k"


def send_teams_notification(violation):
    """Skickar Teams-meddelande via Power Automate webhook, max 1 gång per timme per person."""
    email = violation.get("seller_email", "")

    # Kolla cooldown
    with teams_cooldown_lock:
        last_sent = teams_cooldown.get(email, 0)
        now = time.time()
        if now - last_sent < TEAMS_COOLDOWN_SECONDS:
            minutes_left = int((TEAMS_COOLDOWN_SECONDS - (now - last_sent)) / 60)
            log.info("Teams cooldown för %s – %d min kvar", email, minutes_left)
            return False
        teams_cooldown[email] = now

    try:
        payload = {
            "seller":       violation.get("seller", ""),
            "seller_email": email,
            "z_vehicle":    violation.get("z_vehicle", ""),
            "a_vehicle":    violation.get("a_vehicle", ""),
            "date":         violation.get("date", ""),
            "time":         violation.get("time", ""),
            "amount":       violation.get("amount", 0),
        }
        r = req.post(
            TEAMS_WEBHOOK_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15
        )
        if r.ok:
            log.info("Teams-notis skickad till %s (HTTP %s)", email, r.status_code)
            # Spara i logg
            with lock:
                state["teams_log"].insert(0, {
                    "sent_at":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "to_email":   email,
                    "to_name":    violation.get("seller", ""),
                    "tx_date":    violation.get("date", ""),
                    "tx_time":    violation.get("time", ""),
                    "z_vehicle":  violation.get("z_vehicle", ""),
                    "a_vehicle":  violation.get("a_vehicle", ""),
                    "amount":     violation.get("amount", 0),
                    "message": (
                        f"⚠️ Fel fordon i Zettle!\n\n"
                        f"Hej {violation.get('seller','')}!\n\n"
                        f"Du sålde på fel fordon.\n\n"
                        f"Datum: {violation.get('date','')} {violation.get('time','')}\n"
                        f"Använt fordon: {violation.get('z_vehicle','')}\n"
                        f"Tilldelat fordon: {violation.get('a_vehicle','')}\n\n"
                        f"Vänligen stäng kassan, byt plats till rätt fordon och öppna kassan på nytt.\n\n"
                        f"Tack och bock önskar Felix!"
                    ),
                })
                # Max 500 i loggen
                state["teams_log"] = state["teams_log"][:500]
            return True
        else:
            log.error("Teams webhook fel %s: %s", r.status_code, r.text[:500])
            # Återställ cooldown vid fel så nästa försök kan göras
            with teams_cooldown_lock:
                teams_cooldown[email] = 0
            return False
    except Exception as e:
        log.error("Teams-notis misslyckades: %s", e)
        with teams_cooldown_lock:
            teams_cooldown[email] = 0
        return False
    """Strippa X-prefix: 'X3112' → '3112', '62001' → '62001'"""
    return str(v or "").lstrip("Xx").strip()


# ══════════════════════════════════════════════════════════════════════════
# E-POST
# ══════════════════════════════════════════════════════════════════════════
def send_alert(violation):
    with lock:
        notify_cc  = state["notify_cc"]
        test_mode  = state["test_mode"]

    to_email = violation.get("seller_email", "")
    if not to_email:
        log.warning("Ingen e-post för %s – hoppar", violation["seller"])
        return False

    subject = f"⚠️ Varning: Fel fordon i Zettle – {violation['date']}"
    body    = f"""Hej {violation['seller']},

Vi har noterat att du registrerade en försäljning på fel fordon i Zettle.

  Datum & tid:       {violation['date']} {violation['time']}
  Använt fordon:     {violation['z_vehicle']}
  Tilldelat fordon:  {violation['a_vehicle']}
  Kvitto-ID:         {violation['tx_id']}
  Belopp:            {violation['amount']:.2f} kr

Vänligen kontrollera att du alltid väljer rätt försäljningsplats i Zettle-appen.

/VR Norrtåg – Automatisk fordonskontroll
"""

    to_list = [{"email": to_email, "name": violation["seller"]}]
    if notify_cc:
        to_list.append({"email": notify_cc})

    payload = {
        "sender":      {"name": "VR Norrtåg Fordonskontroll",
                        "email": "Norrtag.bistro@outlook.com"},
        "to":          to_list,
        "subject":     subject,
        "textContent": body,
    }

    try:
        r = req.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key":      "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",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if r.ok:
            log.info("Brevo: e-post skickad till %s", to_email)
            return True
        else:
            log.error("Brevo-fel %s: %s", r.status_code, r.text)
            raise RuntimeError(f"Brevo {r.status_code}: {r.text}")
    except Exception as e:
        log.error("E-post misslyckades: %s", e)
        raise


# ══════════════════════════════════════════════════════════════════════════
# POLLING-TRÅD
# ══════════════════════════════════════════════════════════════════════════
def polling_loop():
    log.info("Polling-tråd startad")
    while True:
        with lock:
            active   = state["polling_active"]
            interval = state["poll_interval_s"]
            positions = list(state["positions"])

        if not active or not positions:
            time.sleep(5)
            continue

        try:
            run_check()
        except Exception as e:
            log.error("Fel i polling: %s", e)

        with lock:
            state["last_poll"] = datetime.now(timezone.utc).isoformat()

        time.sleep(interval)


def run_check():
    """
    Hämtar köp från Zettle för alla datum i positionslistan
    och jämför med tilldelat fordon.
    """
    with lock:
        positions = list(state["positions"])
        seen      = set(state["seen_tx_ids"])

    if not positions:
        return

    # Datumintervall från positionslistan
    dates = sorted({p["date"] for p in positions})
    start = datetime.fromisoformat(dates[0] + "T00:00:00").replace(tzinfo=timezone.utc)
    end   = datetime.fromisoformat(dates[-1] + "T23:59:59").replace(tzinfo=timezone.utc)

    log.info("Kontrollerar %s → %s", dates[0], dates[-1])
    purchases = fetch_purchases(start, end)
    log.info("%d köp hämtade", len(purchases))

    new_violations = []
    new_all        = []
    ok_delta       = 0
    total_delta    = 0

    for p in purchases:
        # Unikt ID
        tx_id = p.get("purchaseUUID") or p.get("purchaseUUID1") or p.get("purchaseNumber", "")
        if not tx_id or tx_id in seen:
            continue
        seen.add(tx_id)
        total_delta += 1

        # Tidsstämpel – Zettle skickar UTC, konvertera till svensk tid (UTC+2)
        ts       = p.get("timestamp", "")
        try:
            dt_utc   = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt_local = dt_utc + timedelta(hours=2)   # Sverige UTC+2
            date_str = dt_local.strftime("%Y-%m-%d")
            time_str = dt_local.strftime("%H:%M:%S")
        except Exception:
            date_str = ts[:10]
            time_str = ts[11:19] if len(ts) > 10 else ""

        # Säljare – Zettle-fält
        seller_name = (p.get("userDisplayName")
                    or p.get("staffName")
                    or p.get("cashierName")
                    or str(p.get("userId", ""))
                    or "(okänd)")

        # Tillämpa namnmappning INNAN e-post genereras
        with lock:
            name_map = state["name_map"]
        mapped_name = name_map.get(seller_name.lower().strip(), seller_name)
        if mapped_name != seller_name:
            log.info("Namnmappning: '%s' → '%s'", seller_name, mapped_name)
        else:
            log.debug("Ingen mappning för '%s' (map har %d poster: %s)",
                      seller_name.lower().strip(), len(name_map), list(name_map.keys())[:5])

        # Generera e-post från mappat namn
        seller_email = name_to_email(mapped_name) if mapped_name != "(okänd)" else ""

        # Fordon – "site" är den plats personalen väljer i Zettle
        site      = p.get("site", {})
        cash_reg  = p.get("cashRegister", {})
        z_vehicle = normalize_vehicle(
            site.get("displayName")
            or cash_reg.get("displayName")
            or "(okänd)"
        )

        # Belopp (Zettle anger i ören)
        amount = p.get("amount", 0) / 100.0

        # Matcha mot positionslista (med mappat namn)
        pos = (find_position_by_email(date_str, seller_email, time_str)
               or find_position(date_str, mapped_name.lower().strip(), time_str))

        if pos is None:
            status    = "SAKNAS"
            comment   = "Saknas i positionslistan"
            a_vehicle = "–"
        elif pos["vehicle"] == z_vehicle:
            status    = "OK"
            comment   = ""
            a_vehicle = pos["vehicle"]
            ok_delta += 1
        else:
            status    = "FEL"
            a_vehicle = pos["vehicle"]
            comment   = f"Borde ha använt {a_vehicle}"

        row = {
            "date":         date_str,
            "time":         time_str,
            "seller":       seller_name,
            "seller_email": pos["email"] if pos else seller_email,
            "paypal_email": seller_email,
            "z_vehicle":    z_vehicle,
            "a_vehicle":    a_vehicle,
            "status":       status,
            "comment":      comment,
            "amount":       amount,
            "tx_id":        tx_id,
            "mail_sent":    False,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

        new_all.append(row)

        # Kolla om transaktionen är för gammal för Teams-notis (max 2 timmar)
        try:
            tx_dt    = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - tx_dt).total_seconds() / 3600
            too_old  = age_hours > 2
        except Exception:
            too_old = False

        if status in ("FEL", "SAKNAS"):
            new_violations.append(row)
        if status == "FEL":  # Skicka Teams KUN vid FEL fordon, inte SAKNAS
            if too_old:
                log.info("Hoppar Teams – transaktion %.1f tim gammal (%s)", age_hours, tx_id)
                row["mail_note"] = f"För gammal ({age_hours:.0f}h) – ej skickat"
            log.warning("AVVIKELSE: %s använt %s (ska: %s) tx=%s",
                        seller_name, z_vehicle, a_vehicle, tx_id)

    # E-post
    with lock:
        test_mode = state["test_mode"]

    for v in new_violations:
        if test_mode:
            log.info("TESTLÄGE – mejl hålls inne för %s", v["seller"])
            v["mail_sent"] = False
            v["mail_note"] = v.get("mail_note") or "Testläge – ej skickat"
        elif v.get("mail_note","").startswith("För gammal"):
            # Redan markerad som för gammal – hoppa över
            v["mail_sent"] = False
        else:
            teams_ok = send_teams_notification(v)
            v["mail_sent"] = teams_ok
            if teams_ok:
                v["mail_note"] = f"Teams ✓ → {v.get('seller_email','')}"
            else:
                with teams_cooldown_lock:
                    last = teams_cooldown.get(v.get("seller_email",""), 0)
                    mins_left = max(0, int((TEAMS_COOLDOWN_SECONDS - (time.time() - last)) / 60))
                v["mail_note"] = f"Cooldown – {mins_left} min kvar" if mins_left > 0 else "Misslyckades"

    with lock:
        state["seen_tx_ids"]      = seen
        state["violations"]       = new_violations + state["violations"]
        state["all_transactions"] = new_all + state["all_transactions"]
        state["ok_count"]        += ok_delta
        state["total_count"]     += total_delta

    # Spara seen_tx_ids till fil så de överlever omstarter
    save_seen_tx(seen)

    log.info("Kontroll klar – %d nya avvikelser, %d OK", len(new_violations), ok_delta)


# ══════════════════════════════════════════════════════════════════════════
# FLASK API
# ══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "fordonskontroll_ui.html")


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    with lock:
        if "zettle_api_key" in data:
            state["zettle_api_key"]   = data["zettle_api_key"]
        if "zettle_client_id" in data:
            state["zettle_client_id"] = data["zettle_client_id"]
        state["access_token"] = None
    try:
        get_token()
        return jsonify({"ok": True, "message": "Ansluten till Zettle ✓"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.json or {}
        with lock:
            for k in ["smtp_host","smtp_port","smtp_user","smtp_pass",
                      "smtp_from","notify_cc","poll_interval_s","test_mode"]:
                if k in data:
                    state[k] = data[k]
        return jsonify({"ok": True})
    else:
        with lock:
            return jsonify({
                "zettle_client_id": state["zettle_client_id"],
                "smtp_host":        state["smtp_host"],
                "smtp_port":        state["smtp_port"],
                "smtp_user":        state["smtp_user"],
                "smtp_from":        state["smtp_from"],
                "notify_cc":        state["notify_cc"],
                "poll_interval_s":  state["poll_interval_s"],
                "test_mode":        state["test_mode"],
            })


@app.route("/api/positions", methods=["POST"])
def api_positions():
    data = request.json or {}
    rows = data.get("positions", [])
    parsed = []
    for r in rows:
        parsed.append({
            "date":         r.get("date",""),
            "name":         r.get("name",""),
            "name_key":     r.get("name","").lower().strip(),
            "vehicle":      normalize_vehicle(r.get("vehicle","")),
            "start":        r.get("start",""),
            "end":          r.get("end",""),
            "email":        r.get("email",""),
            "tagnummer":    str(r.get("tagnummer","")).strip(),
            "till_station": r.get("till_station","").strip().upper(),
        })
    with lock:
        state["positions"]        = parsed
        state["seen_tx_ids"]      = set()
        state["violations"]       = []
        state["all_transactions"] = []
        state["ok_count"]         = 0
        state["total_count"]      = 0
    log.info("%d positioner inlästa", len(parsed))
    return jsonify({"ok": True, "count": len(parsed)})


@app.route("/api/namemap", methods=["POST"])
def api_namemap():
    data = request.json or {}
    raw_map = data.get("map", {})
    clean = {k.lower().strip(): v.lower().strip() for k, v in raw_map.items() if k and v}
    with lock:
        state["name_map"] = clean
    log.info("Namnmappning mottagen: %s", clean)
    return jsonify({"ok": True, "count": len(clean)})


@app.route("/api/polling", methods=["POST"])
def api_polling():
    data = request.json or {}
    with lock:
        state["polling_active"]  = data.get("active", False)
        state["poll_interval_s"] = int(data.get("interval_s", 60))
    return jsonify({"ok": True})


@app.route("/api/run_now", methods=["POST"])
def api_run_now():
    threading.Thread(target=run_check, daemon=True).start()
    return jsonify({"ok": True, "message": "Kontroll startad"})


@app.route("/api/status", methods=["GET"])
def api_status():
    with lock:
        return jsonify({
            "polling_active":   state["polling_active"],
            "poll_interval_s":  state["poll_interval_s"],
            "last_poll":        state["last_poll"],
            "positions_count":  len(state["positions"]),
            "total_count":      state["total_count"],
            "ok_count":         state["ok_count"],
            "violation_count":  len(state["violations"]),
            "violations":       state["violations"][:200],
            "all_transactions": state["all_transactions"][:500],
            "test_mode":        state["test_mode"],
        })


@app.route("/api/clear_violations", methods=["POST"])
def api_clear():
    with lock:
        state["violations"]       = []
        state["all_transactions"] = []
        state["ok_count"]         = 0
        state["total_count"]      = 0
        state["seen_tx_ids"]      = set()
    save_seen_tx(set())  # rensa filen också
    return jsonify({"ok": True})


@app.route("/api/teams_log", methods=["GET"])
def api_teams_log():
    with lock:
        return jsonify(state["teams_log"])


@app.route("/api/test_teams", methods=["POST"])
def api_test_teams():
    data = request.json or {}
    to   = data.get("to", "")
    if not to:
        return jsonify({"ok": False, "message": "Ingen e-postadress"}), 400
    # Återställ cooldown för testadress så testet alltid går igenom
    with teams_cooldown_lock:
        teams_cooldown[to] = 0
    fake_v = {
        "seller":       "Testperson",
        "seller_email": to,
        "date":         "2026-08-28",
        "time":         "09:37:00",
        "z_vehicle":    "9033",
        "a_vehicle":    "9042",
        "amount":       29.0,
    }
    ok = send_teams_notification(fake_v)
    return jsonify({"ok": ok, "message": "Teams-meddelande skickat!" if ok else "Misslyckades"})
def api_test_smtp():
    data = request.json or {}
    to   = data.get("to","")
    if not to:
        return jsonify({"ok": False, "message": "Ingen mottagaradress"}), 400
    fake_v = {
        "seller": "Testperson", "seller_email": to,
        "date": "2026-01-01", "time": "10:00:00",
        "z_vehicle": "62002", "a_vehicle": "62001",
        "tx_id": "TEST-001", "amount": 39.0,
    }
    try:
        ok = send_alert(fake_v)
        return jsonify({"ok": ok, "message": "E-post skickad!" if ok else "Misslyckades"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


# ══════════════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    t = threading.Thread(target=polling_loop, daemon=True)
    t.start()

    import os
    port = int(os.environ.get("PORT", 3100))

    print("\n" + "="*60)
    print("  VR Norrtåg – Fordonskontroll (Zettle Purchase API)")
    print(f"  Port: {port}")
    print("  Stoppa med Ctrl+C")
    print("="*60 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
