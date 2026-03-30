from flask import Flask, jsonify, render_template, request, session, redirect, url_for, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from avwx import Metar, Taf, Station
from concurrent.futures import ThreadPoolExecutor, as_completed
import math, re, io, os, time, requests, gc, json, threading, base64
import pandas as pd
from datetime import datetime, timedelta, timezone

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, PageBreak, HRFlowable)
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

app = Flask(__name__)

# --- DATABASE & SECURITY SETTINGS ---
app.secret_key = os.environ.get("SECRET_KEY", "occ-super-secret-key-change-me")
db_url = os.environ.get("DATABASE_URL", "sqlite:///occ.db")
if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI']  = db_url
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping':   True,   # test connection before use — handles dropped idle connections
    'pool_recycle':    300,    # recycle connections after 5 min — before Postgres drops them
    'pool_timeout':    20,
    'connect_args':    {'connect_timeout': 10},
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 300} 

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class AppData(db.Model):
    id = db.Column(db.String(50), primary_key=True)
    data = db.Column(db.Text, nullable=False)

class DisruptionLog(db.Model):
    id = db.Column(db.String(100), primary_key=True)
    flight = db.Column(db.String(20))
    date = db.Column(db.String(20))
    event_type = db.Column(db.String(20)) 
    origin = db.Column(db.String(10))
    sched_dest = db.Column(db.String(10))
    actual_dest = db.Column(db.String(10))
    weather_snap = db.Column(db.Text)
    taf_snap = db.Column(db.Text)
    notam_snap = db.Column(db.Text)
    acars_snap = db.Column(db.Text)       # ACARS messages for this flight
    ba_code = db.Column(db.String(10))
    logged_by = db.Column(db.String(50))  # username who logged/triggered
    notes = db.Column(db.Text)            # controller freetext notes
    case_ref = db.Column(db.String(20))   # e.g. CF-001
    tail_snap = db.Column(db.String(20))  # tail registration at event time
    xw_snap = db.Column(db.String(20))    # crosswind at decision time
    xw_limit        = db.Column(db.String(20))   # aircraft xwind limit
    timestamp       = db.Column(db.DateTime, default=datetime.utcnow)
    metar_history   = db.Column(db.Text)          # last 12hrs of raw METARs at capture time
    hidden_sections = db.Column(db.Text)          # JSON list of section names suppressed from PDF
    section_audit   = db.Column(db.Text)          # JSON: [{section,action,user,timestamp}]

    # ── SI Classification fields ──
    si_cause          = db.Column(db.String(30))   # WEATHER_WIND, ATC, TECHNICAL etc.
    si_cause_label    = db.Column(db.String(80))   # Human readable cause label
    si_problem_airport = db.Column(db.String(10))  # IATA code of problem airport
    si_airport_focus  = db.Column(db.String(15))   # ARRIVAL, DEPARTURE, NEUTRAL
    si_section_priority = db.Column(db.Text)       # JSON {expand:[], suppress:[]}

    # ── Living Dossier Lifecycle fields ──
    dossier_status    = db.Column(db.String(15), default='ACTIVE')  # ACTIVE, CLOSED
    close_time        = db.Column(db.DateTime)      # when to auto-close
    closed_at         = db.Column(db.DateTime)      # actual close time
    metar_evolution   = db.Column(db.Text)          # JSON: [{ts, icao, raw_metar}]
    taf_vs_actual     = db.Column(db.Text)          # JSON: [{hour, taf_pred, actual_metar, deviation}]
    station_picture   = db.Column(db.Text)          # JSON: operational context snapshot
    auto_summary      = db.Column(db.Text)          # Generated narrative summary

class AcarsLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    flight = db.Column(db.String(20))
    reg = db.Column(db.String(20))
    message = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class SlotLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    flight = db.Column(db.String(20))
    date = db.Column(db.String(20)) 
    station = db.Column(db.String(10))
    scr_text = db.Column(db.Text)
    status = db.Column(db.String(20), default="PENDING") 
    coordinator_reply = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    sent_count  = db.Column(db.Integer, default=1)
    resolved_by = db.Column(db.String(50), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

class CaseEvidence(db.Model):
    """Supporting evidence attached to a disruption case."""
    __tablename__ = 'case_evidence'
    id           = db.Column(db.Integer, primary_key=True)
    log_id       = db.Column(db.String(100), db.ForeignKey('disruption_log.id'), nullable=False, index=True)
    filename     = db.Column(db.String(255))
    content_type = db.Column(db.String(100), default='text/plain')
    content_text = db.Column(db.Text)
    file_data    = db.Column(db.Text)   # base64
    added_by     = db.Column(db.String(50))
    timestamp    = db.Column(db.DateTime, default=datetime.utcnow)

# ── SI CLASSIFICATION ENGINE ───────────────────────────────────────────────
# Parses the SI (Supplementary Information) line from ASMs to derive:
#   cause category, problem airport, and which evidence sections matter most.

SI_CAUSE_RULES = [
    # (keywords_list, cause_key, cause_label, default_focus)
    (['xwind', 'x-wind', 'crosswind', 'cross wind', 'tailwind', 'tail wind',
      'windshear', 'wind shear', 'gusts', 'gust'],
     'WEATHER_WIND', 'Weather — Wind', 'ARRIVAL'),
    (['vis ', 'visibility', 'fog', 'low vis', 'rvr ', 'ceiling', 'cloud',
      'bkn00', 'ovc00', 'vv00', 'mist', 'br '],
     'WEATHER_VIS', 'Weather — Visibility', 'ARRIVAL'),
    ([' wx ', 'weather', ' met ', 'snow', ' ice ', 'icing', 'storm',
      'thunderstorm', 'ts ', 'cb ', 'volcanic', 'ash '],
     'WEATHER_GENERAL', 'Weather — General', 'ARRIVAL'),
    (['atc', 'ctot', 'flow', 'gdp', 'restriction', 'atfm', 'slot ',
      'eurocontrol', 'regulation'],
     'ATC', 'ATC / Flow Control', 'NEUTRAL'),
    (['strike', 'industrial action', 'action industrielle', 'greve'],
     'INDUSTRIAL_ACTION', 'Industrial Action / Strike', 'NEUTRAL'),
    (['deice', 'de-ice', 'de ice', 'stand ', 'gate ', 'fuel ', 'handling',
      'pushback', 'tow ', 'ground', 'ramp'],
     'GROUND_OPS', 'Ground Operations', 'DEPARTURE'),
    (['tech', 'maint', 'defect', 'snag', 'aog', 'u/s', 'unserviceable',
      'mmel', 'mel '],
     'TECHNICAL', 'Technical / Maintenance', 'NEUTRAL'),
    (['crew', 'rest', 'ftl', 'augment', 'rostering', 'sickness',
      'discretion', 'captain decision'],
     'CREW', 'Crew', 'NEUTRAL'),
]

# Which PDF sections to prioritise per cause category
SI_SECTION_PRIORITY = {
    'WEATHER_WIND':      {'expand': ['metar_taf','metar_history','crosswind'],
                          'suppress': []},
    'WEATHER_VIS':       {'expand': ['metar_taf','metar_history','notams'],
                          'suppress': []},
    'WEATHER_GENERAL':   {'expand': ['metar_taf','metar_history','crosswind','notams'],
                          'suppress': []},
    'ATC':               {'expand': ['notams','controller_log'],
                          'suppress': ['crosswind']},
    'INDUSTRIAL_ACTION': {'expand': ['notams','controller_log'],
                          'suppress': ['crosswind','metar_history']},
    'GROUND_OPS':        {'expand': ['notams','controller_log'],
                          'suppress': ['crosswind','metar_history']},
    'TECHNICAL':         {'expand': ['acars','controller_log'],
                          'suppress': ['crosswind','metar_history','metar_taf']},
    'CREW':              {'expand': ['controller_log'],
                          'suppress': ['crosswind','metar_history','metar_taf','notams']},
}


def classify_si_line(si_text, origin='', sched_dest='', event_type=''):
    """Parse SI text to derive cause category, problem airport, and section focus.
    Returns dict with: cause, cause_label, problem_airport, airport_focus,
                       section_priority {expand:[], suppress:[]}"""
    result = {
        'cause': 'UNKNOWN', 'cause_label': 'Unknown / Not classified',
        'problem_airport': '', 'airport_focus': 'NEUTRAL',
        'section_priority': {'expand': [], 'suppress': []},
    }
    if not si_text:
        return result

    si_upper = ' ' + si_text.upper().strip() + ' '

    # 1. Match cause category
    for keywords, cause_key, cause_label, default_focus in SI_CAUSE_RULES:
        for kw in keywords:
            if kw.upper() in si_upper:
                result['cause'] = cause_key
                result['cause_label'] = cause_label
                result['airport_focus'] = default_focus
                result['section_priority'] = SI_SECTION_PRIORITY.get(cause_key,
                    {'expand': [], 'suppress': []})
                break
        if result['cause'] != 'UNKNOWN':
            break

    # 2. Resolve problem airport
    #    a) If an IATA code appears in SI text, use it
    si_airports = re.findall(r'\b([A-Z]{3})\b', si_text.upper())
    # Filter to known airports — skip common false positives (DUE, OUT, THE, etc.)
    _noise = {'DUE', 'OUT', 'THE', 'FOR', 'AND', 'NOT', 'ALL', 'VIS', 'LOW',
              'DIV', 'CNL', 'RRT', 'NEW', 'WAS', 'HAS', 'ARE', 'CAN', 'MAY',
              'BKN', 'OVC', 'FEW', 'SCT', 'VFR', 'IFR', 'VOR', 'ILS', 'RVR',
              'CAT', 'OAT', 'PAX', 'POB', 'ETA', 'ETD', 'ATD', 'ATA', 'STA',
              'STD', 'OFF', 'BAD', 'MET', 'TAF', 'FOG', 'ICE'}
    si_airports = [a for a in si_airports if a not in _noise]
    if si_airports:
        result['problem_airport'] = si_airports[0]
        # Determine if problem airport is dep or arr
        if result['problem_airport'] == origin:
            result['airport_focus'] = 'DEPARTURE'
        elif result['problem_airport'] == sched_dest:
            result['airport_focus'] = 'ARRIVAL'
        # else keep default_focus from the cause rule

    #    b) If no explicit airport, infer from event type + cause
    if not result['problem_airport']:
        if event_type in ('DIVERT',) and result['airport_focus'] == 'ARRIVAL':
            result['problem_airport'] = sched_dest
        elif result['airport_focus'] == 'DEPARTURE':
            result['problem_airport'] = origin
        elif result['airport_focus'] == 'ARRIVAL':
            result['problem_airport'] = sched_dest
        else:
            result['problem_airport'] = sched_dest or origin

    return result


# Accumulation window defaults (hours) by event type
ACCUMULATION_DEFAULTS = {'DIVERT': 4, 'CANCEL': 8, 'DELAY': 6}


@login_manager.user_loader
def load_user(user_id): return User.query.get(int(user_id))

with app.app_context():
    # Wrap everything so a DB hiccup never prevents Flask from starting
    try:
        db.create_all()
    except Exception as _dce:
        print(f"db.create_all warning: {_dce}")

    try:
        if not User.query.filter_by(username='admin').first():
            hashed_pw = generate_password_hash('occ123', method='pbkdf2:sha256')
            db.session.add(User(username='admin', password_hash=hashed_pw, is_admin=True))
            db.session.commit()
    except Exception as _ue:
        print(f"Admin user setup warning: {_ue}")
        try: db.session.rollback()
        except: pass

    # Safe migration — each column in its own transaction with IF NOT EXISTS
    # engine.begin() auto-commits on clean exit, rolls back on exception
    _new_cols = [
        ("acars_snap",       "TEXT"),
        ("logged_by",        "VARCHAR(50)"),
        ("notes",            "TEXT"),
        ("case_ref",         "VARCHAR(20)"),
        ("tail_snap",        "VARCHAR(20)"),
        ("xw_snap",          "VARCHAR(20)"),
        ("xw_limit",         "VARCHAR(20)"),
        ("metar_history",    "TEXT"),
        ("hidden_sections",  "TEXT"),
        ("section_audit",    "TEXT"),
        # SI Classification
        ("si_cause",          "VARCHAR(30)"),
        ("si_cause_label",    "VARCHAR(80)"),
        ("si_problem_airport","VARCHAR(10)"),
        ("si_airport_focus",  "VARCHAR(15)"),
        ("si_section_priority","TEXT"),
        # Living Dossier Lifecycle
        ("dossier_status",    "VARCHAR(15) DEFAULT 'ACTIVE'"),
        ("close_time",        "TIMESTAMP WITHOUT TIME ZONE"),
        ("closed_at",         "TIMESTAMP WITHOUT TIME ZONE"),
        ("metar_evolution",   "TEXT"),
        ("taf_vs_actual",     "TEXT"),
        ("station_picture",   "TEXT"),
        ("auto_summary",      "TEXT"),
    ]
    for _col, _type in _new_cols:
        try:
            with db.engine.begin() as _conn:
                _conn.execute(db.text(
                    f"ALTER TABLE disruption_log ADD COLUMN IF NOT EXISTS {_col} {_type}"
                ))
            print(f"Migration OK: {_col}")
        except Exception as _me:
            print(f"Migration note — {_col}: {_me}")

    # slot_log migrations
    _slot_cols = [
        ("sent_count",  "INTEGER DEFAULT 1"),
        ("resolved_by", "VARCHAR(50)"),
        ("resolved_at", "TIMESTAMP WITHOUT TIME ZONE"),
    ]
    for _col, _type in _slot_cols:
        try:
            with db.engine.begin() as _conn:
                _conn.execute(db.text(
                    f"ALTER TABLE slot_log ADD COLUMN IF NOT EXISTS {_col} {_type}"
                ))
            print(f"Migration OK: slot_log.{_col}")
        except Exception as _me:
            print(f"Migration note — slot_log.{_col}: {_me}")

# ── TIMETABLE BACKGROUND SCHEDULER ─────────────────────────────────────
def _start_opensky_scheduler():
    """Polls OpenSky every 44s in a daemon thread.
    Completely decoupled from request handling — never blocks wx route."""
    def _loop():
        while True:
            try:
                _refresh_opensky_positions()
            except Exception: pass
            time.sleep(44)
    threading.Thread(target=_loop, daemon=True).start()

def _start_timetable_scheduler():
    """Refreshes AE timetable every 15 minutes in a daemon thread.
    Completely decoupled from request handling."""
    def _loop():
        while True:
            try:
                if AVIATION_EDGE_KEY:
                    global ae_timetable_cache, ae_timetable_cache_time, ae_timetable_fetching
                    if not ae_timetable_fetching:
                        ae_timetable_fetching = True
                        try:
                            _iatas = list(set(base_airports.keys()))[:15]
                            with ThreadPoolExecutor(max_workers=4) as _ex:
                                _fs = {_ex.submit(_fetch_ae_timetable, i): i for i in _iatas}
                                for _f in as_completed(_fs, timeout=25):
                                    _i = _fs[_f]
                                    try:
                                        _r = _f.result(timeout=8)
                                        if _r: ae_timetable_cache[_i] = _r
                                    except Exception: pass
                            ae_timetable_cache_time = time.time()
                            _total = sum(len(v.get("arr",[]))+len(v.get("dep",[])) for v in ae_timetable_cache.values())
                            print(f"Timetable scheduler: {_total} entries across {len(ae_timetable_cache)} airports")
                        except Exception as _e:
                            print(f"Timetable scheduler error: {_e}")
                        finally:
                            ae_timetable_fetching = False
            except Exception: pass
            time.sleep(900)  # 15 minutes
    threading.Thread(target=_loop, daemon=True).start()

# ── DOSSIER ACCUMULATION SCHEDULER ─────────────────────────────────────
def _start_dossier_accumulation_scheduler():
    """Every 15 minutes, check for ACTIVE dossiers and accumulate:
    - METAR observations at problem airport
    - TAF vs actual comparison
    Auto-close when close_time is reached, generating summary."""
    def _loop():
        time.sleep(120)  # initial delay — let app warm up
        while True:
            try:
                with app.app_context():
                    _accumulate_active_dossiers()
            except Exception as _e:
                print(f"Dossier accumulation error: {_e}")
            time.sleep(900)  # 15 minutes
    threading.Thread(target=_loop, daemon=True).start()


def _accumulate_active_dossiers():
    """Process all ACTIVE dossiers — append METAR, check for auto-close."""
    try:
        active = DisruptionLog.query.filter(
            DisruptionLog.dossier_status == 'ACTIVE'
        ).all()
        if not active:
            return
        now_utc = datetime.now(timezone.utc)
        for dossier in active:
            try:
                _accumulate_single_dossier(dossier, now_utc)
            except Exception as _de:
                print(f"Accumulation error for {dossier.id}: {_de}")
        db.session.commit()
    except Exception as _e:
        print(f"_accumulate_active_dossiers error: {_e}")
        try: db.session.rollback()
        except: pass


def _accumulate_single_dossier(dossier, now_utc):
    """Accumulate data for a single ACTIVE dossier."""
    prob_apt = getattr(dossier, 'si_problem_airport', '') or dossier.sched_dest or dossier.origin
    if not prob_apt:
        return

    # 1. Append current METAR to evolution log
    try:
        _icao = ops.get(prob_apt, {}).get('icao', prob_apt)
        if _icao and prob_apt in raw_weather_cache:
            _m = raw_weather_cache[prob_apt].get('m')
            if _m:
                evo = json.loads(dossier.metar_evolution or '[]')
                evo.append({
                    'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'),
                    'icao': prob_apt,
                    'raw_metar': getattr(_m, 'raw', '') or ''
                })
                dossier.metar_evolution = json.dumps(evo)
    except Exception as _me:
        print(f"METAR evo append error {dossier.id}: {_me}")

    # 2. TAF vs Actual comparison — build from the TAF at creation time vs actual METARs
    try:
        _build_taf_vs_actual(dossier, prob_apt)
    except Exception as _te:
        print(f"TAF vs actual error {dossier.id}: {_te}")

    # 3. Check auto-close
    close_time = getattr(dossier, 'close_time', None)
    if close_time and now_utc >= close_time:
        _close_dossier(dossier, now_utc)


def _build_taf_vs_actual(dossier, prob_apt):
    """Compare the TAF snapshot (at creation time) with actual METAR evolution."""
    taf_raw = getattr(dossier, 'taf_snap', '') or ''
    evo_raw = getattr(dossier, 'metar_evolution', '[]') or '[]'
    try:
        evo = json.loads(evo_raw)
    except Exception:
        return
    if not evo or not taf_raw or taf_raw == 'N/A':
        return

    # Extract TAF TEMPO/BECMG groups for the problem airport
    # Look for the airport's TAF section
    _apt_taf = ''
    for section in taf_raw.split('['):
        if prob_apt in section:
            _apt_taf = section
            break
    if not _apt_taf:
        _apt_taf = taf_raw

    comparisons = []
    for entry in evo:
        ts_str = entry.get('ts', '')
        raw_metar = entry.get('raw_metar', '')
        if not raw_metar:
            continue

        # Extract key values from METAR: wind, vis, cloud
        _wind_match = re.search(r'(\d{3})(\d{2,3})(G\d{2,3})?KT', raw_metar)
        _vis_match = re.search(r'\b(\d{4})\b', raw_metar)  # 4-digit vis in metres
        _sm_vis = re.search(r'(\d+)\s?SM', raw_metar)  # SM visibility
        _cloud_match = re.findall(r'(FEW|SCT|BKN|OVC|VV)(\d{3})', raw_metar)

        actual_wind = f"{_wind_match.group(0)}" if _wind_match else 'N/A'
        if _vis_match:
            actual_vis = f"{_vis_match.group(1)}m"
        elif _sm_vis:
            actual_vis = f"{_sm_vis.group(1)}SM"
        else:
            actual_vis = 'N/A'
        actual_cloud = '/'.join(f"{c[0]}{c[1]}" for c in _cloud_match) if _cloud_match else 'N/A'

        # Simple deviation detection: check if METAR shows worse conditions than typical
        # Vis below 1500m, cloud below 500ft, wind gusting
        deviations = []
        try:
            if _vis_match and int(_vis_match.group(1)) < 1500:
                deviations.append('LOW VIS')
            if _cloud_match:
                lowest = min(int(c[1]) for c in _cloud_match) * 100  # in feet
                if lowest < 500:
                    deviations.append(f'LOW CLD {lowest}ft')
            if _wind_match and _wind_match.group(3):  # gusting
                deviations.append('GUSTING')
        except Exception:
            pass

        comparisons.append({
            'ts': ts_str,
            'actual_wind': actual_wind,
            'actual_vis': actual_vis,
            'actual_cloud': actual_cloud,
            'deviation': ', '.join(deviations) if deviations else 'WITHIN LIMITS',
            'raw': raw_metar[:80]
        })

    if comparisons:
        dossier.taf_vs_actual = json.dumps(comparisons)


def _close_dossier(dossier, now_utc):
    """Auto-close an ACTIVE dossier and generate summary narrative."""
    dossier.dossier_status = 'CLOSED'
    dossier.closed_at = now_utc

    # Generate narrative summary from accumulated data
    summary_parts = []
    flt = dossier.flight or 'Unknown'
    evt = dossier.event_type or 'disruption'
    create_ts = dossier.timestamp.strftime('%H:%MZ') if dossier.timestamp else 'N/A'
    close_ts = now_utc.strftime('%H:%MZ')
    origin = dossier.origin or '?'
    dest = dossier.sched_dest or '?'
    actual = dossier.actual_dest or dest

    summary_parts.append(
        f"{flt} {evt.lower()} {origin}-{dest}"
        + (f" (diverted to {actual})" if actual != dest else "")
        + f" at {create_ts} on {dossier.date or 'N/A'}."
    )

    # Cause from SI classification
    cause_lbl = getattr(dossier, 'si_cause_label', '') or 'Not classified'
    prob_apt = getattr(dossier, 'si_problem_airport', '') or ''
    focus = getattr(dossier, 'si_airport_focus', '') or ''
    summary_parts.append(f"Cause: {cause_lbl}. Problem airport: {prob_apt} ({focus}).")

    # METAR evolution summary
    try:
        evo = json.loads(dossier.metar_evolution or '[]')
        if evo:
            summary_parts.append(
                f"METAR observations recorded: {len(evo)} between "
                f"{evo[0].get('ts','')} and {evo[-1].get('ts','')}."
            )
    except Exception:
        pass

    # TAF vs actual deviations
    try:
        tva = json.loads(dossier.taf_vs_actual or '[]')
        deviations = [t for t in tva if t.get('deviation', '') != 'WITHIN LIMITS']
        if deviations:
            summary_parts.append(
                f"TAF vs actual: {len(deviations)} of {len(tva)} observations showed "
                f"conditions worse than forecast ({', '.join(set(d['deviation'] for d in deviations))})."
            )
        elif tva:
            summary_parts.append("TAF vs actual: conditions matched or were better than forecast.")
    except Exception:
        pass

    summary_parts.append(f"Case auto-closed {close_ts}.")
    dossier.auto_summary = ' '.join(summary_parts)
    print(f"Dossier auto-closed: {dossier.id} — {dossier.auto_summary[:100]}")


# Start background schedulers after app context is ready
with app.app_context():
    threading.Thread(target=_start_timetable_scheduler, daemon=True).start()
    threading.Thread(target=_start_opensky_scheduler, daemon=True).start()
    threading.Thread(target=_start_dossier_accumulation_scheduler, daemon=True).start()
# ─────────────────────────────────────────────────────────────────────────


# ── AIRFIELD OPERATIONAL LIMITS DATABASE ─────────────────────────────────────
AIRFIELD_LIMITS = {
    "LCY": {
        "name": "London City Airport", "cat": "CAT C", "runway": "09/27",
        "notes": "Steep 5.5° ILS approach on 27. Short runway (1508m LDA). Windshear from Docklands towers on short finals. E190/A318 steep approach approved only.",
        "xw_limit": "25 kt", "tailwind": "0 kt (nil tailwind ops)", "vis_min": "RVR 350m (CAT I), 200m (CAT III)",
        "special": ["5.5° glideslope — non-standard, aircraft type approval required",
                    "No standard A320/B737 ops", "E190 xwind limit 25kt for line ops",
                    "Thames barrier area — ATC holds differ from standard"],
        "ops_hours": "0630–2130 local (Mon–Sat), 0800–2100 (Sun)", "ppr_required": False,
    },
    "FLR": {
        "name": "Florence Peretola", "cat": "CAT B/C", "runway": "05/23",
        "notes": "Single runway. Terrain to north (Apennines). Rwy 05 displaced threshold. Limited approach paths.",
        "xw_limit": "20 kt (Rwy 05), 25 kt (Rwy 23)", "tailwind": "5 kt max",
        "vis_min": "RVR 550m (non-precision), 200m (ILS Rwy 23)",
        "special": ["Night curfew 2300–0600", "Wind from NE funnels down valley — gusts underreported vs actual short final",
                    "Significant terrain avoidance in go-around"],
        "ops_hours": "0500–2300 local", "ppr_required": False,
    },
    "INN": {
        "name": "Innsbruck Airport", "cat": "CAT C", "runway": "08/26",
        "notes": "Mountain airport. Surrounded on three sides by Alps. Approach requires visual segment. Mandatory crew training.",
        "xw_limit": "20 kt", "tailwind": "5 kt max (Rwy 26 only, exceptional conditions)",
        "vis_min": "Near-VMC required. Minimum 5km vis for circling.",
        "special": ["MANDATORY crew qualification — Innsbruck approval required",
                    "Visual approach mandatory Rwy 08 — no precision approach",
                    "Foehn wind — extreme turbulence and rapid vis change",
                    "Snow/ice on rwy common Oct–Apr — SNOWTAM checking essential",
                    "No CAT II/III operations", "Go-around Rwy 08 requires immediate left turn"],
        "ops_hours": "0600–2200 local", "ppr_required": True,
    },
    "FNC": {
        "name": "Funchal Madeira Airport", "cat": "CAT C", "runway": "05/23",
        "notes": "Cliffside runway over the Atlantic built on pillars. Strong variable winds from surrounding mountains.",
        "xw_limit": "15 kt demonstrated, 20 kt with special approval", "tailwind": "0 kt",
        "vis_min": "RVR 800m (non-precision)",
        "special": ["MANDATORY crew qualification — Funchal approval required",
                    "Windshear common on final — automatic windshear escape manoeuvre briefed",
                    "Rwy surface wet almost always — reduced braking factored in performance",
                    "NW wind creates strong rotors behind Pico do Facho (575m, 1nm from threshold)"],
        "ops_hours": "0700–2300 local", "ppr_required": False,
    },
    "CMF": {
        "name": "Chambéry-Savoie Airport", "cat": "CAT C", "runway": "18/36",
        "notes": "Alpine ski resort airport. High terrain all sides. Visual segment approaches. Strong windshear common.",
        "xw_limit": "20 kt", "tailwind": "5 kt max",
        "vis_min": "Minimum 1500m RVR, circling approaches need 5km",
        "special": ["Visual segment mandatory both runway ends",
                    "Snow/ice ops common Nov–Apr", "Mountain wave and rotor turbulence in strong winds",
                    "Performance calculations must use airfield elevation (779m / 2556ft AMSL)"],
        "ops_hours": "0700–2100 local (seasonal)", "ppr_required": True,
    },
    "GNB": {
        "name": "Grenoble-Isère Airport", "cat": "CAT B/C", "runway": "09/27",
        "notes": "Alpine airport at 383m AMSL. Terrain constraints to east. Challenging in winter.",
        "xw_limit": "25 kt", "tailwind": "7 kt max", "vis_min": "RVR 600m (ILS), NPA 1500m",
        "special": ["Rwy 27 ILS — terrain to right on short final",
                    "Strong bise (NE wind) and foehn events common in spring",
                    "Snow ops common Dec–Mar — de-icing hold time critical"],
        "ops_hours": "0600–2200 local (seasonal)", "ppr_required": False,
    },
    "GVA": {
        "name": "Geneva Airport", "cat": "CAT B", "runway": "04/22",
        "notes": "Mont Blanc massif to SE. Strong bise winds. Single ILS direction.",
        "xw_limit": "30 kt", "tailwind": "10 kt (Rwy 22 tailwind ops approved)",
        "vis_min": "RVR 200m (CAT III)",
        "special": ["Bise (NE wind) common — 30-40kt sustained with gusts to 50kt",
                    "Tailwind ops on Rwy 22 with strong bise — approved up to 10kt"],
        "ops_hours": "0600–2300 local", "ppr_required": False,
    },
    "BHD": {
        "name": "Belfast George Best City Airport", "cat": "CAT B", "runway": "04/22",
        "notes": "City centre airport. Short runway (1829m). Cave Hill terrain to NW.",
        "xw_limit": "25 kt", "tailwind": "5 kt max", "vis_min": "RVR 400m (ILS), NPA 800m",
        "special": ["Short runway — landing performance essential, wet runway common",
                    "Cave Hill terrain restricts departure paths in low cloud"],
        "ops_hours": "0630–2200 local", "ppr_required": False,
    },
    "LGW": {
        "name": "London Gatwick", "cat": "CAT A", "runway": "08R/26L, 08L/26R",
        "notes": "Major London airport. Parallel runways (one normally in use). High traffic density.",
        "xw_limit": "33 kt", "tailwind": "10 kt", "vis_min": "RVR 75m (CAT IIIb)",
        "special": ["CAT III operations available both runways",
                    "High ATFM slot pressure — CTOT compliance critical"],
        "ops_hours": "24hr", "ppr_required": False,
    },
    "AMS": {
        "name": "Amsterdam Schiphol", "cat": "CAT A", "runway": "Multiple",
        "notes": "Major hub. Complex runway system. High ATFM slot pressure.",
        "xw_limit": "35 kt (most runways)", "tailwind": "10 kt (selected runways)",
        "vis_min": "RVR 75m (CAT IIIb/c)",
        "special": ["Multiple intersecting runways — complex LVP coordination",
                    "ATFM frequently impacted — CTOT compliance critical",
                    "Night restrictions on Rwy 09/27 and 18R/36L"],
        "ops_hours": "23hr (curfew restrictions per runway)", "ppr_required": False,
    },
}

# ── AIRCRAFT CROSSWIND LIMITS BY RUNWAY CONDITION ───────────────────────
# E190 limits from BA CityFlyer OMB Part B s1.5.14
# A320/A321 limits from Airbus FCOM / BA EFW ops
AC_XW_TABLE = {
    "E190": {
        # (condition_key): (xw_limit_kt, label)
        "dry":        (38, "Dry runway"),
        "wet":        (31, "Wet runway"),
        "steep":      (25, "Steep approach"),
        "snow_comp":  (20, "Compacted snow"),
        "snow_wet":   (18, "Slush / wet & dry snow"),
        "ice":        (12, "Ice / standing water"),
    },
    "A320": {
        "dry":        (29, "Dry/damp/wet <3mm"),
        "wet":        (29, "Wet runway"),
        "snow_comp":  (25, "Slush / dry snow"),
        "snow_wet":   (15, "Standing water / wet snow"),
        "ice":        (5,  "Ice / hydroplaning risk"),
    },
    "A321": {
        "dry":        (29, "Dry/damp/wet <3mm"),
        "wet":        (29, "Wet runway"),
        "snow_comp":  (25, "Slush / dry snow"),
        "snow_wet":   (15, "Standing water / wet snow"),
        "ice":        (5,  "Ice / hydroplaning risk"),
    },
}

def get_rwy_condition(wx_codes_list, temp_c=None):
    """Derive runway condition key from METAR wx codes.
    Returns (condition_key, description)."""
    codes = " ".join(wx_codes_list).upper()
    # Ice / standing water (worst)
    if any(k in codes for k in ["FZRA", "FZDZ", "FZFG", "IC", "PL", "GR"]):
        return "ice", "Freezing precipitation / ice"
    if "BLSN" in codes:
        return "ice", "Blowing snow / icy surface"
    # Heavy snow / wet snow
    if any(k in codes for k in ["+SN", "RASN", "+RASN", "SHSN"]):
        return "snow_wet", "Heavy snow / wet snow"
    # Light/moderate snow — dry if cold enough
    if "SN" in codes or "-SN" in codes:
        if temp_c is not None and temp_c <= -2:
            return "snow_comp", "Dry/compacted snow (T<=-2°C)"
        return "snow_wet", "Snow (temp near 0°C — wet snow risk)"
    # Rain / shower — wet runway
    if any(k in codes for k in ["RA", "DZ", "SHRA", "TSRA", "-RA", "-DZ"]):
        return "wet", "Rain — wet runway"
    # Thunderstorm without reported precip code
    if "TS" in codes:
        return "wet", "Thunderstorm — wet runway assumed"
    return "dry", "Dry runway"

def get_operative_xw_limit(ac_type_str, wx_codes_list, airport_base_limit, temp_c=None):
    """Return (operative_limit_kt, ac_limit_kt, condition_key, condition_label,
               binding_factor) where binding_factor is 'airport' or 'aircraft'."""
    # Map fleet string to AC type key
    if "E19" in ac_type_str or "E90" in ac_type_str or "EMB" in ac_type_str or "190" in ac_type_str:
        ac_key = "E190"
    elif "321" in ac_type_str or "31N" in ac_type_str or "31E" in ac_type_str:
        ac_key = "A321"
    else:
        ac_key = "A320"
    cond_key, cond_label = get_rwy_condition(wx_codes_list, temp_c)
    table = AC_XW_TABLE.get(ac_key, AC_XW_TABLE["A320"])
    ac_limit, _ = table.get(cond_key, table["dry"])
    base = airport_base_limit if airport_base_limit else 999
    operative = min(ac_limit, base)
    binding = "airport" if base <= ac_limit else "aircraft"
    return operative, ac_limit, cond_key, cond_label, binding, ac_key

def get_airfield_limits(iata):
    """Return operational limits for an IATA code, or None."""
    return AIRFIELD_LIMITS.get((iata or '').upper())

@app.route('/favicon.svg')
def favicon():
    return app.send_static_file('favicon.svg')

@app.route('/favicon.ico')
def favicon_ico():
    return app.send_static_file('favicon.svg'), 200, {'Content-Type': 'image/svg+xml'}

@app.route('/api/airfield_limits')
@login_required
def api_airfield_limits():
    iata = request.args.get('iata', '').upper().strip()
    data = get_airfield_limits(iata)
    if not data:
        return jsonify({"found": False, "iata": iata})
    return jsonify({"found": True, "iata": iata, **data})

# ─────────────────────────────────────────────────────────────────────────────
# --- SAAS CONFIGURATION ENGINE ---
AVIATION_EDGE_KEY     = os.environ.get("AVIATION_EDGE_KEY")
OPENSKY_CLIENT_ID     = os.environ.get("OPENSKY_CLIENT_ID", "")    # OpenSky username
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "") # OpenSky password

# Fleet registry — pre-defined so NameError never occurs if import fails
ICAO24_TO_REG = {}
FLEET         = {}
try:
    import fleet_registry as _fr

    # Support current registry format (ALL_FLEET / HEX_TO_REG / "hex" key)
    # as well as any future format (FLEET / ICAO24_TO_REG / "icao24" key)

    # 1. Build FLEET from ALL_FLEET or FLEET
    if hasattr(_fr, "ALL_FLEET"):
        # Convert registry format: {"hex": "406a42"} → {"icao24": "406a42"}
        for _reg, _info in _fr.ALL_FLEET.items():
            _h = (_info.get("hex") or _info.get("icao24") or "").lower().strip()
            if _h and _h != "todo":
                FLEET[_reg] = {
                    "icao24":   _h,
                    "type":     _info.get("type", ""),
                    "operator": _info.get("operator", ""),
                }
    elif hasattr(_fr, "FLEET"):
        FLEET = _fr.FLEET

    # 2. Build ICAO24_TO_REG from HEX_TO_REG or reverse of FLEET
    if hasattr(_fr, "HEX_TO_REG"):
        ICAO24_TO_REG = {k.lower(): v for k, v in _fr.HEX_TO_REG.items()
                         if k.lower() != "todo"}
    elif hasattr(_fr, "ICAO24_TO_REG"):
        ICAO24_TO_REG = _fr.ICAO24_TO_REG
    elif FLEET:
        for _reg, _info in FLEET.items():
            _h = (_info.get("icao24") or "").lower().strip()
            if _h: ICAO24_TO_REG[_h] = _reg

    print(f"fleet_registry loaded: {len(FLEET)} aircraft, {len(ICAO24_TO_REG)} hex codes")
except Exception as _e:
    print(f"fleet_registry not loaded ({_e}) — OpenSky overlay disabled")

# Ensure case_evidence table exists
with app.app_context():
    try:
        db.create_all()
    except Exception as _ce:
        print(f"db.create_all: {_ce}")

CLIENT_ENV = os.environ.get("CLIENT_CONFIG", "BACF") 

def group_bacf(f, icao, arr, dep, ac_type):
    if icao.startswith('CFE') or f.get('airline', {}).get('icaoCode', '').upper() == 'CFE': return "CFE"
    elif icao.startswith('BAW') or f.get('airline', {}).get('icaoCode', '').upper() == 'BAW':
        if 'E19' in ac_type or 'E90' in ac_type or 'EMB' in ac_type or 'E75' in ac_type: return "CFE"
        if (arr == 'LGW' or dep == 'LGW') and ('A32' in ac_type or '320' in ac_type or '321' in ac_type): return "EFW"
        else: return "BAW"
    return "UNK"

def group_loganair(f, icao, arr, dep, ac_type):
    return "LOG" if icao.startswith('LOG') or f.get('airline', {}).get('icaoCode', '').upper() == 'LOG' else "UNK"

bacf_airports = {
    "LCY": {"icao": "EGLC", "name": "London City", "lat": 51.505, "lon": 0.055, "rwy": 270, "rwys": "09/27", "fleet": "Cityflyer", "spec": True, "xw_lim": 25, "tw_lim": 5, "one_way": False, "curfew": "22:30"},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "lat": 52.313, "lon": 4.764, "rwy": 180, "rwys": "18R/36L, 18C/36C, 18L/36R", "fleet": "Cityflyer", "spec": False, "one_way": False, "curfew": "22:00"},
    "EDI": {"icao": "EGPH", "name": "Edinburgh", "lat": 55.950, "lon": -3.363, "rwy": 240, "rwys": "06/24", "fleet": "Cityflyer", "spec": False, "one_way": False},
    "GLA": {"icao": "EGPF", "name": "Glasgow", "lat": 55.871, "lon": -4.433, "rwy": 230, "rwys": "05/23", "fleet": "Cityflyer", "spec": False, "one_way": False},
    "BHD": {"icao": "EGAC", "name": "Belfast City", "lat": 54.628, "lon": -5.872, "rwy": 220, "rwys": "04/22", "fleet": "Cityflyer", "spec": False, "one_way": False},
    "DUB": {"icao": "EIDW", "name": "Dublin", "lat": 53.421, "lon": -6.249, "rwy": 280, "rwys": "10R/28L, 10L/28R, 16/34", "fleet": "Cityflyer", "spec": False, "one_way": False},
    "FLR": {"icao": "LIRQ", "name": "Florence", "lat": 43.810, "lon": 11.205, "rwy": 50,  "rwys": "05/23", "fleet": "Cityflyer", "spec": True, "xw_lim": 20, "tw_lim": 10, "one_way": True},
    "LGW": {"icao": "EGKK", "name": "London Gatwick", "lat": 51.148, "lon": -0.190, "rwy": 260, "rwys": "08R/26L, 08L/26R", "fleet": "Both", "spec": False, "one_way": False},
    "JER": {"icao": "EGJJ", "name": "Jersey", "lat": 49.207, "lon": -2.195, "rwy": 260, "rwys": "08/26", "fleet": "Both", "spec": False, "one_way": False},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "lat": 47.260, "lon": 11.344, "rwy": 260, "rwys": "08/26", "fleet": "Both", "spec": True, "xw_lim": 25, "tw_lim": 10, "one_way": True},
    "FNC": {"icao": "LPMA", "name": "Funchal", "lat": 32.694, "lon": -16.774, "rwy": 50,  "rwys": "05/23", "fleet": "Euroflyer", "spec": True, "xw_lim": 20, "tw_lim": 10, "one_way": True},
    "CMF": {"icao": "LFLB", "name": "Chambery", "lat": 45.637, "lon": 5.880, "rwy": 180, "rwys": "18/36", "fleet": "Cityflyer", "spec": True, "xw_lim": 25, "tw_lim": 10, "one_way": True},
    "GNB": {"icao": "LFLS", "name": "Grenoble", "lat": 45.362, "lon": 5.329, "rwy": 90, "rwys": "09/27", "fleet": "Both", "spec": True, "xw_lim": 25, "tw_lim": 10, "one_way": True},
    "FAO": {"icao": "LPFR", "name": "Faro", "lat": 37.014, "lon": -7.965, "rwy": 280, "rwys": "10/28", "fleet": "Both", "spec": False, "one_way": False}
}
loganair_airports = {
    "GLA": {"icao": "EGPF", "name": "Glasgow", "lat": 55.871, "lon": -4.433, "rwy": 230, "rwys": "05/23", "fleet": "Main", "spec": False, "one_way": False},
    "EDI": {"icao": "EGPH", "name": "Edinburgh", "lat": 55.950, "lon": -3.363, "rwy": 240, "rwys": "06/24", "fleet": "Main", "spec": False, "one_way": False}
}

CONFIGS = {"BACF": {"tracked_icaos": ["CFE", "BAW"], "base_airports": bacf_airports, "grouper": group_bacf}, "LOGANAIR": {"tracked_icaos": ["LOG"], "base_airports": loganair_airports, "grouper": group_loganair}}
ACTIVE_CONFIG = CONFIGS.get(CLIENT_ENV, CONFIGS["BACF"])
base_airports = ACTIVE_CONFIG["base_airports"]

# --- DIVERSION PLANNING ---
COMMON_ALT_AIRPORTS = {
    "LHR": {"name": "London Heathrow", "lat": 51.477, "lon": -0.461, "icao": "EGLL"},
    "STN": {"name": "Stansted",        "lat": 51.885, "lon":  0.235, "icao": "EGSS"},
    "LTN": {"name": "Luton",           "lat": 51.874, "lon": -0.368, "icao": "EGGW"},
    "SEN": {"name": "Southend",        "lat": 51.571, "lon":  0.696, "icao": "EGMC"},
    "BRS": {"name": "Bristol",         "lat": 51.382, "lon": -2.719, "icao": "EGGD"},
    "SOU": {"name": "Southampton",     "lat": 50.950, "lon": -1.357, "icao": "EGHI"},
    "EXT": {"name": "Exeter",          "lat": 50.734, "lon": -3.414, "icao": "EGTE"},
    "MAN": {"name": "Manchester",      "lat": 53.354, "lon": -2.275, "icao": "EGCC"},
    "BHX": {"name": "Birmingham",      "lat": 52.453, "lon": -1.748, "icao": "EGBB"},
    "NCL": {"name": "Newcastle",       "lat": 55.037, "lon": -1.692, "icao": "EGNT"},
    "BFS": {"name": "Belfast Intl",    "lat": 54.657, "lon": -6.216, "icao": "EGAA"},
    "ABZ": {"name": "Aberdeen",        "lat": 57.202, "lon": -2.198, "icao": "EGPD"},
    "PIK": {"name": "Prestwick",       "lat": 55.509, "lon": -4.587, "icao": "EGPK"},
    "GCI": {"name": "Guernsey",        "lat": 49.435, "lon": -2.602, "icao": "EGJB"},
    "IOM": {"name": "Isle of Man",     "lat": 54.083, "lon": -4.624, "icao": "EGNS"},
    "RTM": {"name": "Rotterdam",       "lat": 51.956, "lon":  4.438, "icao": "EHRD"},
    "EIN": {"name": "Eindhoven",       "lat": 51.450, "lon":  5.375, "icao": "EHEH"},
    "BRU": {"name": "Brussels",        "lat": 50.901, "lon":  4.484, "icao": "EBBR"},
    "CDG": {"name": "Paris CDG",       "lat": 49.013, "lon":  2.550, "icao": "LFPG"},
    "DUS": {"name": "Dusseldorf",      "lat": 51.280, "lon":  6.757, "icao": "EDDL"},
    "MUC": {"name": "Munich",          "lat": 48.354, "lon": 11.786, "icao": "EDDM"},
    "ZRH": {"name": "Zurich",          "lat": 47.464, "lon":  8.549, "icao": "LSZH"},
    "GVA": {"name": "Geneva",          "lat": 46.238, "lon":  6.109, "icao": "LSGG"},
    "LYS": {"name": "Lyon",            "lat": 45.726, "lon":  5.091, "icao": "LFLL"},
    "PSA": {"name": "Pisa",            "lat": 43.683, "lon": 10.393, "icao": "LIRP"},
    "BLQ": {"name": "Bologna",         "lat": 44.535, "lon": 11.289, "icao": "LIPE"},
    "FCO": {"name": "Rome Fiumicino",  "lat": 41.800, "lon": 12.238, "icao": "LIRF"},
    "SZG": {"name": "Salzburg",        "lat": 47.793, "lon": 13.004, "icao": "LOWS"},
    "VIE": {"name": "Vienna",          "lat": 48.110, "lon": 16.570, "icao": "LOWW"},
    "LIS": {"name": "Lisbon",          "lat": 38.774, "lon": -9.135, "icao": "LPPT"},
    "OPO": {"name": "Porto",           "lat": 41.248, "lon": -8.681, "icao": "LPPR"},
    "SNN": {"name": "Shannon",         "lat": 52.702, "lon": -8.925, "icao": "EINN"},
    "PXO": {"name": "Porto Santo",     "lat": 33.070, "lon":-16.345, "icao": "LPPS"},
    "TFS": {"name": "Tenerife South",  "lat": 28.044, "lon":-16.572, "icao": "GCTS"},
    "ORK": {"name": "Cork",            "lat": 51.841, "lon": -8.491, "icao": "EICK"},
}
# Key diversion alternate airports — fetched for wx alongside main ops airports
DIVERT_ALT_WX = {
    "PXO": {"icao": "LPPS", "lat": 33.070, "lon": -16.345},  # FNC alternate
    "MUC": {"icao": "EDDM", "lat": 48.354, "lon": 11.786},  # INN alternate
    "PSA": {"icao": "LIRP", "lat": 43.683, "lon": 10.393},  # FLR alternate
    "BLQ": {"icao": "LIPE", "lat": 44.535, "lon": 11.289},  # FLR alternate
}

DIVERSION_CANDIDATES = {
    "LCY": ["SEN", "LGW", "STN", "LTN", "BRS", "SOU"],
    "LGW": ["STN", "LHR", "SEN", "LTN", "BRS", "SOU"],
    "AMS": ["RTM", "LCY", "EIN", "BRU", "DUS", "CDG"],
    "EDI": ["GLA", "PIK", "LCY", "NCL", "ABZ", "IOM"],
    "GLA": ["EDI", "PIK", "BHD", "LCY", "NCL", "IOM"],
    "BHD": ["BFS", "DUB", "GLA", "PIK", "IOM"],
    "DUB": ["BHD", "LCY", "SNN", "ORK", "BFS", "IOM"],
    "FLR": ["PSA", "BLQ", "FCO"],
    "INN": ["MUC", "SZG", "ZRH", "VIE"],
    "FNC": ["PXO", "FAO", "LIS", "TFS", "OPO"],
    "CMF": ["LYS", "GVA", "GNB"],
    "GNB": ["GVA", "LYS", "CMF"],
    "FAO": ["LIS", "OPO"],
    "JER": ["LGW", "LCY", "GCI", "SOU", "BRS", "EXT"],
}


# --- PUBLISHED OPS HOURS & NIGHT RESTRICTIONS DATABASE ---
# Sources: AIP AD 2.3, DfT/CAA. ops_hrs = [open_utc, close_utc]. All times UTC.
OPS_HRS_DB = {
    "LCY": {"ops_hrs": ["06:30","22:30"], "night_jet": "No movements 22:30-06:30Z. Strict night ban enforced by LBHA."},
    "LGW": {"ops_hrs": None,              "night_jet": "QC noise quota 23:00-07:00Z. Chapter 2 banned."},
    "LHR": {"ops_hrs": None,              "night_jet": "Night quota scheme 23:30-06:00Z. QC2 limit per season. Chapter 2 banned."},
    "STN": {"ops_hrs": None,              "night_jet": "Noise preferential routes. No formal curfew but restrictions 23:00-07:00Z."},
    "LTN": {"ops_hrs": None,              "night_jet": "Quota system 23:00-07:00Z. Night movement restrictions apply."},
    "BHX": {"ops_hrs": None,              "night_jet": "Night noise restrictions. Preferential runways active 23:00-07:00Z."},
    "MAN": {"ops_hrs": None,              "night_jet": "Night quota 23:00-07:00Z. Chapter 2 banned. Preferential runway 23R/05L nights."},
    "EDI": {"ops_hrs": None,              "night_jet": "Noise preferential routes. Restrictions approx 23:30-06:00Z."},
    "GLA": {"ops_hrs": None,              "night_jet": "Noise abatement procedures. Preferential runway use 23:00-06:00Z."},
    "BHD": {"ops_hrs": ["06:30","21:30"], "night_jet": "No scheduled ops outside hours. Night ban 21:30-06:30Z per Belfast City planning conditions."},
    "BFS": {"ops_hrs": None,              "night_jet": "No formal curfew. Noise abatement procedures. Check AIP EGAA AD 2.3."},
    "DUB": {"ops_hrs": None,              "night_jet": "Night noise restrictions 23:00-07:00Z. Chapter 3 preferential runways."},
    "SNN": {"ops_hrs": None,              "night_jet": "No formal curfew. Noise abatement departure procedures."},
    "ORK": {"ops_hrs": None,              "night_jet": "No formal curfew. Preferential runway and noise procedures."},
    "JER": {"ops_hrs": ["06:30","22:00"], "night_jet": "No scheduled movements outside published ops hours. Check AIP EGJJ AD 2.3."},
    "GCI": {"ops_hrs": ["06:30","21:30"], "night_jet": "Restricted hours. Night ops by PPR only."},
    "IOM": {"ops_hrs": None,              "night_jet": "No formal curfew. Noise abatement procedures."},
    "NCL": {"ops_hrs": None,              "night_jet": "Night quota restrictions. Noise preferential routes 23:00-07:00Z."},
    "BRS": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise preferential routes 23:00-07:00Z."},
    "SOU": {"ops_hrs": ["07:00","21:00"], "night_jet": "Restricted hours. Night ops by PPR only. Check AIP EGHI AD 2.3."},
    "EXT": {"ops_hrs": None,              "night_jet": "Noise abatement. No formal curfew."},
    "ABZ": {"ops_hrs": None,              "night_jet": "No formal curfew. Noise abatement procedures."},
    "INV": {"ops_hrs": ["07:30","21:30"], "night_jet": "Restricted hours operation."},
    "NQY": {"ops_hrs": ["08:00","20:00"], "night_jet": "Seasonal airport. Restricted hours. Check AIP EGDG AD 2.3."},
    "CWL": {"ops_hrs": None,              "night_jet": "Noise abatement. Restricted movements after 23:00Z."},
    "LBA": {"ops_hrs": None,              "night_jet": "Night restrictions. Preferential runway 14/32 for noise abatement."},
    "AMS": {"ops_hrs": None,              "night_jet": "Full night quota (QC). Restrictions 23:00-06:00Z. Chapter 3 only nights."},
    "RTM": {"ops_hrs": ["06:00","23:00"], "night_jet": "Restricted hours. Night ops limited. Check AIP EHRD AD 2.3."},
    "EIN": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise abatement 23:00-07:00Z."},
    "BRU": {"ops_hrs": None,              "night_jet": "Night quota 23:00-06:00Z. Chapter 2 banned."},
    "FRA": {"ops_hrs": ["05:00","23:00"], "night_jet": "Night ban 23:00-05:00Z. No scheduled commercial flights. Strict curfew."},
    "MUC": {"ops_hrs": None,              "night_jet": "Night restrictions 22:00-06:00Z. Preferential runways. Significant noise measures."},
    "DUS": {"ops_hrs": None,              "night_jet": "Night quota restrictions. Noise procedures 22:00-06:00Z."},
    "ZRH": {"ops_hrs": ["06:00","23:00"], "night_jet": "Night ban 23:00-06:00Z. Strict enforcement."},
    "GVA": {"ops_hrs": ["06:00","23:00"], "night_jet": "Night ban 23:00-06:00Z. Arrivals must land by 00:00Z with PPR."},
    "INN": {"ops_hrs": ["05:30","21:00"], "night_jet": "AD closed outside ops hours (~21:00-05:30Z winter). Check AIP LOWI AD 2.3."},
    "SZG": {"ops_hrs": ["06:00","21:00"], "night_jet": "Seasonal/restricted hours. AD closed outside published ops hours."},
    "VIE": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise quota 22:00-06:00Z."},
    "CDG": {"ops_hrs": None,              "night_jet": "Night measures 22:00-06:00Z. Strong noise restrictions and preferential runways."},
    "LYS": {"ops_hrs": None,              "night_jet": "Noise abatement. Night restrictions 22:00-06:00Z."},
    "GNB": {"ops_hrs": ["06:00","22:00"], "night_jet": "Night restrictions 22:00-06:00Z. Seasonal ski airport."},
    "CMF": {"ops_hrs": ["06:00","21:00"], "night_jet": "Seasonal ski airport. AD closed outside ops hours. Check AIP LFLB AD 2.3."},
    "FCO": {"ops_hrs": None,              "night_jet": "Night restrictions 23:00-06:00Z. Noise abatement. Preferential runway."},
    "LIN": {"ops_hrs": ["06:00","23:00"], "night_jet": "Night ban 23:00-06:00Z. City airport strict noise measures."},
    "FLR": {"ops_hrs": ["05:00","21:00"], "night_jet": "AD closed outside ops hours (~21:00-05:00Z). One-way op. Check AIP LIRQ AD 2.3."},
    "PSA": {"ops_hrs": None,              "night_jet": "Night restrictions. Check AIP LIRP AD 2.3."},
    "BLQ": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise abatement 22:00-07:00Z."},
    "VRN": {"ops_hrs": ["06:00","23:00"], "night_jet": "Night ban 23:00-06:00Z. Noise restrictions enforced."},
    "LIS": {"ops_hrs": None,              "night_jet": "Night restrictions 00:00-06:00Z. Noise abatement and preferential runway."},
    "OPO": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise abatement 23:00-07:00Z."},
    "FAO": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise abatement procedures."},
    "FNC": {"ops_hrs": ["07:00","23:00"], "night_jet": "Reduced ops outside published hours. Night movements by PPR only."},
    "OSL": {"ops_hrs": None,              "night_jet": "Night restrictions 23:00-06:00Z. Noise preferential procedures."},
    "CPH": {"ops_hrs": None,              "night_jet": "Night restrictions. Quota scheme 23:00-06:00Z."},
    "LJU": {"ops_hrs": ["06:00","22:00"], "night_jet": "Night ban 22:00-06:00Z. AD closed outside published ops hours."},
    "PRG": {"ops_hrs": None,              "night_jet": "Night restrictions. Noise abatement 22:00-06:00Z."},
    "ATH": {"ops_hrs": None,              "night_jet": "Night restrictions 00:00-06:00Z. Noise abatement procedures."},
    "IST": {"ops_hrs": None,              "night_jet": "No formal curfew. 24hr operations. Noise abatement procedures."},
}


def get_ops_restrictions(iata, now_utc):
    """
    Build restriction banner entries from OPS_HRS_DB for the station summary.
    - ops_hrs defined: CLOSURE banner if currently outside hours, else informational OPS HRS.
    - night_jet only: always shows a NIGHT_JET info banner.
    Deduplication against live NOTAM entries is done at call site.
    """
    entry = OPS_HRS_DB.get(iata)
    if not entry:
        return []

    results   = []
    ops_hrs   = entry.get("ops_hrs")
    night_jet = entry.get("night_jet", "")

    if ops_hrs:
        open_h,  open_m  = map(int, ops_hrs[0].split(":"))
        close_h, close_m = map(int, ops_hrs[1].split(":"))
        now_mins   = now_utc.hour * 60 + now_utc.minute
        open_mins  = open_h  * 60 + open_m
        close_mins = close_h * 60 + close_m

        # Normal daytime window (open < close, e.g. 06:30-22:30)
        if close_mins > open_mins:
            is_open = open_mins <= now_mins < close_mins
        else:
            # Overnight window
            is_open = now_mins >= open_mins or now_mins < close_mins

        is_outside = not is_open
        label = (
            f"\U0001f512 AD CLOSED  {ops_hrs[1]}Z \u2013 {ops_hrs[0]}Z"
            if is_outside
            else f"\U0001f319 OPS HRS: {ops_hrs[0]}-{ops_hrs[1]}Z"
        )
        results.append({
            "type":   "CLOSURE" if is_outside else "NIGHT_JET",
            "label":  label,
            "detail": night_jet,
            "active": is_outside,
            "end_dt": None,
            "source": "AIP",
        })
    elif night_jet:
        results.append({
            "type":   "NIGHT_JET",
            "label":  "\U0001f507 NIGHT RESTRICTIONS",
            "detail": night_jet,
            "active": False,
            "end_dt": None,
            "source": "AIP",
        })

    return results

flight_schedule_df = pd.DataFrame()
contacts_df = pd.DataFrame()
acars_cache = {}
flight_trails = {} 
live_flights_memory = {}
flight_tactical_state = {}
departure_times = {} 
arrival_times = {} 
aviation_edge_cache_time = 0
ae_timetable_cache      = {}   # iata → {arrivals: [...], departures: [...]}
ae_timetable_cache_time  = 0      # last successful timetable fetch
ae_timetable_fetching    = False  # guard against concurrent fetches
AE_TIMETABLE_TTL         = 900   # refresh every 15 minutes

opensky_cache_time       = 0      # last successful OpenSky poll
opensky_token_cache      = {"token": None, "expires": 0}  # OAuth2 bearer token
opensky_fail_count       = 0      # consecutive auth failures
opensky_backoff_until    = 0      # epoch time — skip until this time if circuit open
opensky_pos_cache     = {}         # icao24 → {lat, lon, alt_ft, spd_kts, hdg, last_seen, squawk}
squawk_alert_log      = {}         # flt → {squawk, first_seen, last_seen, reg, arr}
divert_memory         = {}         # local cache — also persisted to DB for multi-worker

def _divert_memory_set(flt, orig_dest):
    """Store divert memory in both local dict and DB (shared across workers)."""
    flt = flt.upper().strip()
    divert_memory[flt] = orig_dest
    bare = flt.replace("BA","").replace("CJ","")
    divert_memory[bare] = orig_dest
    try:
        _key = f"divert:{flt}"
        rec = db.session.get(AppData, _key)
        if rec: rec.data = orig_dest
        else: db.session.add(AppData(id=_key, data=orig_dest))
        db.session.commit()
        print(f"Divert memory SET (DB): {flt} → {orig_dest}")
    except Exception as _e:
        print(f"Divert memory DB write failed: {_e}")
        try: db.session.rollback()
        except: pass

def _divert_memory_get(flt):
    """Get original dest — checks local dict first, then DB (for other worker's writes)."""
    flt = flt.upper().strip()
    bare = flt.replace("BA","").replace("CJ","")
    # Local cache first
    orig = divert_memory.get(flt) or divert_memory.get(bare)
    if orig: return orig
    # DB fallback — other worker may have set it
    try:
        rec = db.session.get(AppData, f"divert:{flt}")
        if rec and rec.data:
            divert_memory[flt] = rec.data  # warm local cache
            return rec.data
    except Exception:
        try: db.session.rollback()
        except: pass
    return None

# Emergency squawk codes and their meanings
SQUAWK_EMERGENCY = {
    '7700': ('GENERAL EMERGENCY',      'RED',    '🆘'),
    '7600': ('RADIO FAILURE — NORDO',  'RED',    '📻'),
    '7500': ('UNLAWFUL INTERFERENCE',  'RED',    '🚨'),
    '7000': ('VFR CONSPICUITY',        'AMBER',  '⚠️'),
    '2000': ('UNASSIGNED IFR',         'AMBER',  '📡'),
}
SQUAWK_EMERGENCY_CODES = set(SQUAWK_EMERGENCY.keys())
OPENSKY_REFRESH_SECS  = 44         # poll OpenSky every 44 seconds (free tier: 100 req/day)
pax_figures = {}   # {flt_key: {"m": int, "c": int, "dep": str, "date": str}}

# AIMS SFTP config — set these in Render environment variables
AIMS_HOST       = os.environ.get("AIMS_HOST",       "splhrl22")          # Set to IP in Render — splhrl22 is internal BA DNS only
AIMS_PORT       = int(os.environ.get("AIMS_PORT",   "22"))
AIMS_USER       = os.environ.get("AIMS_USER",       "cyxtfrpr")           # Live: cyxtfrpr / Test: cyxtfrts
AIMS_PASSWORD   = os.environ.get("AIMS_PASSWORD",   "")
AIMS_REMOTE_DIR = os.environ.get("AIMS_REMOTE_DIR", "/xt/live/cyxtfrpr/from-ba")  # Full path confirmed from FileZilla

def get_safe_num(val, default=0):
    try: return float(val) if val is not None else default
    except: return default

def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

def calculate_winds(wind_dir, wind_spd, rwy_hdg, is_one_way=False):
    """Returns (crosswind, arrival_tailwind, departure_tailwind).
    For one-way airports, departures use the reciprocal runway."""
    if wind_dir is None or wind_spd is None or rwy_hdg is None: return 0, 0, 0
    angle_rad = math.radians(wind_dir - rwy_hdg)
    xw = round(abs(wind_spd * math.sin(angle_rad)))
    hw = wind_spd * math.cos(angle_rad)
    tw = round(abs(hw)) if hw < 0 else 0

    dep_rwy = (rwy_hdg + 180) % 360
    dep_angle = math.radians(wind_dir - dep_rwy)
    dep_hw = wind_spd * math.cos(dep_angle)
    dep_tw = round(abs(dep_hw)) if dep_hw < 0 else 0

    if not is_one_way:
        tw = min(tw, dep_tw)
        dep_tw = tw
    return xw, tw, dep_tw

def load_schedule_robust(file_bytes):
    try:
        content = file_bytes.decode('utf-8', errors='ignore').splitlines()
        skip_r = 0
        for i, line in enumerate(content):
            line_up = line.upper()
            if 'FLT' in line_up and 'DEP' in line_up and 'ARR' in line_up: skip_r = i; break
        df = pd.read_csv(io.StringIO(file_bytes.decode('utf-8', errors='ignore')), skiprows=skip_r, on_bad_lines='skip')
        df.columns = df.columns.str.strip().str.upper()
        df = df.dropna(subset=['FLT'])
        if 'DATE' in df.columns: df['DATE_OBJ'] = pd.to_datetime(df['DATE'], format='mixed', dayfirst=True, errors='coerce').dt.date
        
        # FIX: Catch AIMS 'REG' column and map it to 'AC_REG'
        if 'REG' in df.columns and 'AC_REG' not in df.columns:
            df['AC_REG'] = df['REG']
            
        if 'AC_REG' not in df.columns: df['AC_REG'] = 'UNK'
        df['AC_REG'] = df['AC_REG'].fillna('UNK').astype(str)
        # Normalise AIMS station codes: strip terminal suffix (LGWS→LGW, ACE1→ACE etc)
        for _sc in ['DEP', 'ARR']:
            if _sc in df.columns:
                df[_sc] = df[_sc].astype(str).str.strip().str.upper()
                df[_sc] = df[_sc].apply(lambda x: x[:3] if (len(x)==4 and x[:3].isalpha() and (x[3:].isalpha() or x[3:].isdigit())) else x)
        
        # Clean up REG formats missing the dash, but catch AIMS dummy tails
        def clean_reg(x):
            x = str(x).strip().upper()
            if x.startswith('GLGW') or x.startswith('GLHR') or x.startswith('GLCY'): return 'TBC'
            if len(x) == 5 and x.startswith('G') and '-' not in x: return f"G-{x[1:]}"
            return x
            
        df['AC_REG'] = df['AC_REG'].apply(clean_reg)
        
        return df
    except Exception as e: return pd.DataFrame()

def refresh_schedule_cache():
    global flight_schedule_df
    try:
        record = db.session.get(AppData, 'schedule')
        if record: flight_schedule_df = load_schedule_robust(record.data.encode('utf-8'))
    except: pass

def refresh_contacts_cache():
    global contacts_df
    try:
        record = db.session.get(AppData, 'contacts')
        if record:
            df = pd.read_csv(io.StringIO(record.data), on_bad_lines='skip', encoding='utf-8')
            df.columns = df.columns.str.strip()
            if not df.empty: df.iloc[:, 0] = df.iloc[:, 0].ffill()
            contacts_df = df
    except: contacts_df = pd.DataFrame()

def refresh_pax_cache():
    global pax_figures
    try:
        record = db.session.get(AppData, 'pax_figures')
        if record:
            pax_figures = json.loads(record.data)
    except: pass

with app.app_context():
    refresh_schedule_cache()
    refresh_contacts_cache()
    refresh_pax_cache()

def get_station_contact(iata):
    if contacts_df.empty: return "N/A", "N/A", ""
    try:
        cols = contacts_df.columns.tolist()
        st_col = next((c for c in cols if 'station' in str(c).lower() or 'iata' in str(c).lower()), cols[0])
        co_col = next((c for c in cols if 'company' in str(c).lower() or 'name' in str(c).lower() or 'agent' in str(c).lower()), cols[1] if len(cols) > 1 else None)
        ph_col = next((c for c in cols if 'tel' in str(c).lower() or 'phone' in str(c).lower()), cols[2] if len(cols) > 2 else None)
        em_col = next((c for c in cols if 'email' in str(c).lower()), cols[3] if len(cols) > 3 else None)
        match = contacts_df[contacts_df[st_col].astype(str).str.strip().str.upper() == iata.upper()]
        if not match.empty:
            row = match.iloc[0]
            co = str(row[co_col]).strip() if co_col and pd.notna(row[co_col]) else "N/A"
            ph = str(row[ph_col]).strip() if ph_col and pd.notna(row[ph_col]) else "N/A"
            em = str(row[em_col]).strip() if em_col and pd.notna(row[em_col]) else ""
            if co.lower() == 'nan': co = "N/A"
            if ph.lower() == 'nan': ph = "N/A"
            if em.lower() == 'nan': em = ""
            return co, ph, em
    except: pass
    return "N/A", "N/A", ""

raw_weather_cache, wx_cache_time = {}, 0
raw_notam_cache, notam_cache_time = {}, 0


# Parallel wx/NOTAM fetch helper
def _fetch_ae_timetable(iata):
    """Fetch AE timetable (arrivals + departures) for one airport.
    Returns dict with 'arr' and 'dep' lists, or empty dict on failure."""
    if not AVIATION_EDGE_KEY:
        return {}
    result = {'arr': [], 'dep': []}
    try:
        for t_type, key in [('arrival', 'arr'), ('departure', 'dep')]:
            url = (f'https://aviation-edge.com/v2/public/timetable'
                   f'?key={AVIATION_EDGE_KEY}&iataCode={iata}&type={t_type}')
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200 and isinstance(resp.json(), list):
                result[key] = resp.json()
    except Exception as e:
        print(f'AE timetable fetch failed for {iata}: {e}')
    return result

def _tt_time(t):
    """Parse AE time string to HH:MMZ display. Returns '--:--' if empty."""
    if not t: return '--:--'
    try:
        s = t.split('T')[1][:5] if 'T' in str(t) else str(t)[:5]
        return s + 'Z'
    except:
        return '--:--'

def _tt_lookup(flt):
    """Look up a flight number in the timetable cache.
    Returns dict with gate, delay, atd, ata, status etc. Empty dict if not found."""
    flt = flt.upper().strip()
    for iata, data in ae_timetable_cache.items():
        for direction in ('arr', 'dep'):
            for entry in data.get(direction, []):
                if str(entry.get('flight', {}).get('iataNumber', '')).upper() == flt:
                    arr_d  = entry.get('arrival', {})
                    dep_d  = entry.get('departure', {})
                    status = entry.get('status', '').upper()
                    # Delay in minutes — AE provides as integer
                    arr_delay = int(arr_d.get('delay') or 0)
                    dep_delay = int(dep_d.get('delay') or 0)
                    return {
                        'gate_dep':      dep_d.get('gate') or '',
                        'gate_arr':      arr_d.get('gate') or '',
                        'terminal_dep':  dep_d.get('terminal') or '',
                        'terminal_arr':  arr_d.get('terminal') or '',
                        'dep_delay_min': dep_delay,
                        'arr_delay_min': arr_delay,
                        'std':           _tt_time(dep_d.get('scheduledTime')),
                        'sta':           _tt_time(arr_d.get('scheduledTime')),
                        'etd':           _tt_time(dep_d.get('estimatedTime')),
                        'eta_live':      _tt_time(arr_d.get('estimatedTime')),
                        'atd':           _tt_time(dep_d.get('actualTime')),
                        'ata':           _tt_time(arr_d.get('actualTime')),
                        'status':        status,
                        'aircraft_reg':  entry.get('aircraft', {}).get('regNumber') or '',
                    }
    return {}

def _fetch_airport_wx(iata, icao):
    result = {}
    m_obj, t_obj = None, None
    try: m_obj = Metar(icao); m_obj.update()
    except: pass
    try: t_obj = Taf(icao); t_obj.update()
    except: pass
    result['wx'] = {"m": m_obj, "t": t_obj}
    result['notam'] = fetch_faa_notams(icao)
    return iata, result



def _notam_affects_flight(windows, eta_dt):
    """Return True only if the flight ETA falls within a NOTAM's validity AND daily time window."""
    if not windows or eta_dt is None:
        return bool(windows)   # no ETA → conservative
    for entry in windows:
        b_dt, c_dt = entry[0], entry[1]
        d_start = entry[2] if len(entry) > 2 else None  # HHMM int e.g. 2300
        d_end   = entry[3] if len(entry) > 3 else None  # HHMM int e.g. 600

        # 1. Check overall B/C validity window
        starts = b_dt if b_dt else eta_dt
        ends   = c_dt if c_dt else (eta_dt + timedelta(hours=24))
        if not (starts <= eta_dt <= ends):
            continue  # outside overall validity

        # 2. If a daily schedule exists, check time-of-day
        if d_start is not None and d_end is not None:
            eta_hhmm = eta_dt.hour * 100 + eta_dt.minute
            if d_end < d_start:  # overnight e.g. 2300-0600
                if not (eta_hhmm >= d_start or eta_hhmm <= d_end):
                    continue  # not in nightly window
            else:
                if not (d_start <= eta_hhmm <= d_end):
                    continue  # not in daytime window

        return True
    return False

def fetch_faa_notams(icao_code):
    try:
        url = "https://notams.aim.faa.gov/notamSearch/search"
        payload = {"searchType": 0, "designatorsForLocation": icao_code}
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if 'notamList' in data:
                valid_notams = []
                for n in data['notamList']:
                    msg = n.get('icaoMessage') or n.get('traditionalMessage') or n.get('message') or ""
                    msg = str(msg).replace('<br>', '\n').strip()
                    if msg and msg.lower() not in ["none", "null"]: valid_notams.append(msg)
                if valid_notams: return valid_notams
        return ["NO ACTIVE NOTAMS REPORTED BY FAA."]
    except: return ["⚠️ SYSTEM ERROR FETCHING NOTAMS"]

def parse_notam_restrictions(notam_list, now_utc):
    """
    Scan NOTAMs for curfews, AD/RWY closures, night jet bans.
    Extracts operational time windows from E) free text first (e.g. "2200-0600"),
    then falls back to D) daily schedule, then B)/C) validity dates.
    Never shows the NOTAM issuance date as the restriction time.
    """
    restrictions = []
    seen = set()

    for raw in notam_list:
        text = str(raw).upper()
        if not text or "NO ACTIVE NOTAMS" in text or text.startswith("⚠️"): continue

        # --- B) start / C) end validity ---
        start_dt = end_dt = None
        try:
            b = re.search(r'B\)\s*(\d{10})', text)
            c = re.search(r'C\)\s*(\d{10})', text)
            if b: start_dt = datetime.strptime(b.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
            if c: end_dt   = datetime.strptime(c.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
        except: pass

        if end_dt and end_dt < now_utc: continue
        if start_dt and start_dt > now_utc + timedelta(hours=72): continue

        # --- Extract the E) free-text body ---
        e_match = re.search(r'E\)\s*(.+?)(?=\nF\)|\nG\)|\Z)', text, re.DOTALL)
        e_text = e_match.group(1).strip() if e_match else text

        # --- Find a HHMM-HHMM time window — priority order: ---
        # 1. D) daily schedule line
        # 2. Explicit time range in E) text  e.g. "AD CLSD 2200-0600" or "OPS 0600-2200"
        # 3. "BETWEEN HHMM AND HHMM" or "FROM HHMM TO HHMM"
        time_window = None

        dm = re.search(r'D\)\s*(\d{4})\s*[-–]\s*(\d{4})', text)
        if dm:
            t1, t2 = dm.group(1), dm.group(2)
            time_window = f"{t1[:2]}:{t1[2:]}Z – {t2[:2]}:{t2[2:]}Z"

        if not time_window:
            # Pattern: any HHMM-HHMM in E) text (not a date like 2503091800)
            tw = re.search(r'\b(\d{4})\s*[-–]\s*(\d{4})\b', e_text)
            if tw:
                h1, h2 = int(tw.group(1)[:2]), int(tw.group(2)[:2])
                m1, m2 = int(tw.group(1)[2:]), int(tw.group(2)[2:])
                # Sanity: valid hours 00-23, valid mins 00-59, not a date
                if h1 <= 23 and h2 <= 23 and m1 <= 59 and m2 <= 59:
                    time_window = f"{tw.group(1)[:2]}:{tw.group(1)[2:]}Z – {tw.group(2)[:2]}:{tw.group(2)[2:]}Z"

        if not time_window:
            bw = re.search(r'(?:BETWEEN|FROM)\s+(\d{4})\s+(?:AND|TO|-)\s+(\d{4})', e_text)
            if bw:
                time_window = f"{bw.group(1)[:2]}:{bw.group(1)[2:]}Z – {bw.group(2)[:2]}:{bw.group(2)[2:]}Z"

        # Only use B/C dates as a date-range fallback (not as "times")
        if not time_window and start_dt and end_dt:
            # If validity spans more than 1 day — show date range, not a time
            if (end_dt - start_dt).days >= 1:
                time_window = f"{start_dt.strftime('%d%b')} – {end_dt.strftime('%d%b %H:%M')}Z"
            else:
                time_window = f"{start_dt.strftime('%H:%M')}Z – {end_dt.strftime('%H:%M')}Z"

        if not time_window: time_window = "See NOTAM"

        end_label = end_dt.strftime("%d%b %H:%MZ") if end_dt else None

        # --- Classify restriction type ---
        rtype = label = None

        is_ppr_notam = (
            bool(re.search(r'\bPPR\b|PRIOR PERMISSION REQUIRED', text)) and
            bool(re.search(r'\bRWY\b|\bRUNWAY\b', text)) and
            not bool(re.search(r'\bPARK\b|\bSTAND\b|\bAPRON\b|\bGATE\b|\bHANGAR\b|\bWINGSPAN\b|\bWINGS\b|\bACFT\s+SIZE\b', text))
        )

        if re.search(r'\bAD CLSD\b|\bAERODROME CLSD\b|\bAIRPORT CLSD\b|\bAD CLOSED\b', text):
            rtype, label = "CLOSURE", "🔒 AD CLOSED"

        elif re.search(r'\bRWY\s+\d{2}[LRC]?\s+(?:CLSD|CLOSED)\b|\bRUNWAY\s+(?:CLSD|CLOSED)\b', text):
            rm = re.search(r'RWY\s+(\d{2}[LRC]?(?:/\d{2}[LRC]?)?)', text)
            rtype = "RWY_CLOSURE"
            label = f"🚧 RWY {rm.group(1) if rm else ''} CLOSED"

        elif re.search(
            r'NIGHT\s+(?:JET|FLIGHT|MOVEMENT|OPERATION)\s+(?:BAN|RESTRICT|PROHIBIT|QUOTA)|'
            r'QC\s*(?:LIMIT|QUOTA|SCHEME|RESTRICT)|'
            r'NOISE\s+(?:ABATEMENT|CURFEW|RESTRICT)|'
            r'CHAPTER\s+\d+\s+(?:RESTRICT|BAN|PROHIBIT)|'
            r'JET\s+NOISE\s+RESTRICT', text):
            rtype, label = "NIGHT_JET", "🔇 NIGHT JET BAN"

        elif re.search(
            r'\bCURFEW\b|'
            r'NO (?:DEPARTURE|ARRIVAL|MOVEMENT|FLIGHT)S?\s+(?:BETWEEN|FROM|AFTER|BEFORE)', text):
            rtype = "CURFEW"
            label = f"🌙 CURFEW {time_window}" if time_window != "See NOTAM" else "🌙 CURFEW"

        # PPR with no other rtype — add as CURFEW banner
        if not rtype:
            if is_ppr_notam:
                is_active = (start_dt is None) or (start_dt <= now_utc)
                key = f"PPR|{time_window}"
                if key not in seen:
                    seen.add(key)
                    restrictions.append({
                        "type":   "CURFEW",
                        "label":  f"⛔ PPR REQUIRED {time_window}",
                        "detail": e_text[:120].strip(),
                        "active": is_active,
                        "end_dt": end_label,
                    })
            continue

        key = f"{rtype}|{time_window}"
        if key in seen: continue
        seen.add(key)

        is_active = (start_dt is None) or (start_dt <= now_utc)
        restrictions.append({
            "type":    rtype,
            "label":   label,
            "detail":  time_window,
            "active":  is_active,
            "end_dt":  end_label,
        })

    order = {"CLOSURE": 0, "RWY_CLOSURE": 1, "CURFEW": 2, "NIGHT_JET": 3}
    restrictions.sort(key=lambda x: (0 if x['active'] else 1, order.get(x['type'], 9)))
    return restrictions

def validate_password(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return "Password must contain at least one number."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return "Password must contain at least one special character."
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username').lower()).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('home'))
        error = 'Invalid Username or Password.'
    return render_template('login.html', error=error, client=CLIENT_ENV)

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    old_pass = data.get('old_password')
    new_pass = data.get('new_password')

    if not check_password_hash(current_user.password_hash, old_pass):
        return jsonify({"error": "Incorrect current password."})

    val_error = validate_password(new_pass)
    if val_error:
        return jsonify({"error": val_error})

    current_user.password_hash = generate_password_hash(new_pass, method='pbkdf2:sha256')
    db.session.commit()
    return jsonify({"message": "Password updated successfully!"})

@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin_panel():
    if not current_user.is_admin: return "Access Denied", 403
    if request.method == 'POST':
        if 'create_user' in request.form:
            u_name = request.form.get('new_username').lower()
            u_pass = request.form.get('new_password')
            
            val_error = validate_password(u_pass)
            if val_error:
                flash(f"Error creating user: {val_error}")
            elif not User.query.filter_by(username=u_name).first():
                new_user = User(username=u_name, password_hash=generate_password_hash(u_pass, method='pbkdf2:sha256'), is_admin=request.form.get('is_admin') == 'on')
                db.session.add(new_user)
                db.session.commit()
                flash(f"User {u_name} created successfully.")
            else:
                flash("Username already exists!")
                
        elif 'delete_user' in request.form:
            u_id = request.form.get('user_id')
            user_to_delete = User.query.get(u_id)
            if user_to_delete and user_to_delete.username != 'admin':
                db.session.delete(user_to_delete)
                db.session.commit()
                flash("User deleted.")
    return render_template('admin.html', users=User.query.all(), client=CLIENT_ENV)

@app.route('/admin/reset_dossiers')
@login_required
def reset_dossiers():
    if not current_user.is_admin: return "Access Denied", 403
    DisruptionLog.__table__.drop(db.engine)
    DisruptionLog.__table__.create(db.engine)
    return "Success! The Disruption table has been updated with the new EU261 columns."

@app.route('/')
@login_required
def home(): return render_template('index.html', client=CLIENT_ENV, current_user=current_user, tv_mode=False, owm_key=os.environ.get("OWM_KEY",""))

@app.route('/tv')
@login_required
def tv_mode(): return render_template('index.html', client=CLIENT_ENV, current_user=current_user, tv_mode=True, owm_key=os.environ.get("OWM_KEY",""))

def _parse_aar_to_schedule(aar_text):
    """Parse an AAR (Aircraft Allocation Report) text into a schedule DataFrame.
    AAR format: DATE  FLIGHT   DEP ARR ETD  ETA  REG          AC   PAX
    e.g.        30.03 BA8701   EDI LCY 0520 0700 G-LCAB       E90  56
    Returns a DataFrame with columns matching the schedule CSV format."""
    rows = []
    year = datetime.utcnow().strftime('%Y')
    for line in aar_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Match AAR flight lines: DD.MM BAnnnn DEP ARRx HHMM HHMM G-XXXX ACtype PAX
        m = re.match(
            r'(\d{2}\.\d{2})\s+'          # date DD.MM
            r'((?:BA|CJ|A0)\d{3,4}[A-Z]?)\s+'  # flight number
            r'([A-Z]{3}\d?)\s+'            # dep (may have terminal digit)
            r'([A-Z]{3}\d?)\s+'            # arr (may have terminal digit)
            r'(\d{4})\s+'                  # ETD
            r'(\d{4})\s+'                  # ETA
            r'(G-[A-Z]{4})\s+'            # registration
            r'([A-Z0-9]{2,4})\s*'         # ac type
            r'(\d*)',                       # pax (optional)
            line
        )
        if not m:
            continue
        date_str, flt, dep, arr, etd, eta, reg, ac, pax = m.groups()

        # Strip terminal suffix from airports (NCE1→NCE, BER1→BER, DUB2→DUB)
        dep_clean = dep[:3] if len(dep) == 4 else dep
        arr_clean = arr[:3] if len(arr) == 4 else arr

        # Map AC type codes: E90→E190, A20N→A320, A21N→A321
        ac_map = {'E90': 'E190', 'E95': 'E195', 'A20N': 'A320', 'A21N': 'A321',
                  'A320': 'A320', 'A321': 'A321', 'E190': 'E190'}
        ac_type = ac_map.get(ac.upper(), ac.upper())

        # Format STD/STA with colon for consistency with CSV format
        std = f"{etd[:2]}:{etd[2:]}"
        sta = f"{eta[:2]}:{eta[2:]}"

        # Build date for DATE column: dd.mm → DD/MM/YYYY
        dd, mm = date_str.split('.')
        date_full = f"{dd}/{mm}/{year}"

        rows.append({
            'FLT': flt,
            'DEP': dep_clean,
            'ARR': arr_clean,
            'STD': std,
            'STA': sta,
            'AC_TYPE': ac_type,
            'AC_REG': reg,
            'PAX': pax or '',
            'DATE': date_full,
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Create DATE_OBJ for date-based filtering (same as load_schedule_robust)
    try:
        df['DATE_OBJ'] = pd.to_datetime(df['DATE'], format='mixed', dayfirst=True, errors='coerce').dt.date
    except Exception:
        pass
    return df


@app.route('/api/aar_webhook', methods=['POST'])
def aar_webhook():
    global flight_schedule_df
    raw_text = ""
    if request.is_json: raw_text = request.json.get('aar_text', '')
    elif request.form: raw_text = request.form.get('aar_text', '')
    if not raw_text and request.data: raw_text = request.data.decode('utf-8', errors='ignore')
    
    raw_text = raw_text.strip()
    if not raw_text:
        return jsonify({"error": "No AAR text provided"}), 400

    # ── DETECT IF AAR CONTAINS FULL FLIGHT DATA ──────────────────────
    # If it has the "COMPLETE FLIGHT LISTING" header or enough flight lines,
    # parse it as a full schedule source — not just a tail update
    is_full_aar = ('COMPLETE FLIGHT LISTING' in raw_text.upper()
                   or len(re.findall(r'(?:BA|CJ)\d{3,4}', raw_text)) >= 5)

    # ── PATH 1: FULL AAR → BUILD SCHEDULE ────────────────────────────
    # Use AAR as schedule source when: (a) schedule is empty/stale, or
    # (b) this is clearly a full AAR report
    if is_full_aar:
        try:
            aar_df = _parse_aar_to_schedule(raw_text)
            if not aar_df.empty:
                flight_count = len(aar_df)

                # If existing schedule is empty or stale, replace entirely
                if flight_schedule_df.empty:
                    flight_schedule_df = aar_df
                    mode = 'CREATED'
                else:
                    # Merge: update matching flights, add new ones
                    # Key on FLT to update regs, times, etc.
                    existing_flts = set(flight_schedule_df['FLT'].astype(str).str.upper())
                    new_flts = set(aar_df['FLT'].astype(str).str.upper())

                    # Update regs for flights that exist in both
                    for _, row in aar_df.iterrows():
                        flt = str(row['FLT']).upper()
                        mask = flight_schedule_df['FLT'].astype(str).str.upper() == flt
                        if mask.any():
                            flight_schedule_df.loc[mask, 'AC_REG'] = row['AC_REG']
                            flight_schedule_df.loc[mask, 'STD'] = row['STD']
                            flight_schedule_df.loc[mask, 'STA'] = row['STA']
                            if 'PAX' in flight_schedule_df.columns:
                                flight_schedule_df.loc[mask, 'PAX'] = row.get('PAX', '')

                    # Add flights that don't exist in current schedule
                    new_only = aar_df[~aar_df['FLT'].astype(str).str.upper().isin(existing_flts)]
                    if not new_only.empty:
                        flight_schedule_df = pd.concat([flight_schedule_df, new_only], ignore_index=True)

                    mode = 'MERGED'

                # Persist to DB
                try:
                    record = db.session.get(AppData, 'schedule')
                    csv_data = flight_schedule_df.to_csv(index=False)
                    if not record:
                        db.session.add(AppData(id='schedule', data=csv_data))
                    else:
                        record.data = csv_data
                    db.session.commit()
                except Exception as _pe:
                    print(f"AAR schedule persist error: {_pe}")
                    try: db.session.rollback()
                    except: pass

                print(f"AAR → schedule {mode}: {flight_count} flights parsed")
                return jsonify({
                    "message": f"AAR processed: {flight_count} flights {mode.lower()}",
                    "mode": mode,
                    "flights": flight_count,
                    "total_schedule": len(flight_schedule_df),
                })
        except Exception as _ae:
            print(f"AAR full parse error: {_ae}")
            import traceback; traceback.print_exc()

    # ── PATH 2: FALLBACK — TAIL-UPDATE-ONLY (original logic) ────────
    if raw_text and not flight_schedule_df.empty:
        try:
            swaps = []
            for line in raw_text.split('\n'):
                fMatch = re.search(r'\b(?:BA|CJ|A0)?\s*(\d{3,4}[a-zA-Z]?)\b', line, re.IGNORECASE)
                rMatch = re.search(r'\bG-?([A-Za-z]{4})\b', line, re.IGNORECASE)
                
                if fMatch and rMatch:
                    num = fMatch.group(1).upper()
                    flt_ba = "BA" + num
                    flt_cj = "CJ" + num
                    reg = "G-" + rMatch.group(1).upper()
                    
                    if len(reg) == 6 and reg[2:].isalpha():
                        swaps.append((flt_ba, flt_cj, num, reg))
                    
            if swaps:
                for flt_ba, flt_cj, num, reg in swaps:
                    mask = flight_schedule_df['FLT'].astype(str).str.upper().str.replace(' ', '').isin([flt_ba, flt_cj, num])
                    flight_schedule_df.loc[mask, 'AC_REG'] = str(reg)
                
                record = db.session.get(AppData, 'schedule')
                if record:
                    record.data = flight_schedule_df.to_csv(index=False)
                    db.session.commit()
                
                return jsonify({"message": f"Successfully updated {len(swaps)} tails."})
        except Exception as e: pass
    return jsonify({"message": "AAR received, no valid tails found or schedule empty."})

@app.route('/api/acars_webhook', methods=['POST'])
def acars_webhook():
    raw_text = ""
    if request.is_json: raw_text = request.json.get('acars_text', '')
    elif request.form: raw_text = request.form.get('acars_text', '')
    if not raw_text and request.data: raw_text = request.data.decode('utf-8', errors='ignore')
    
    raw_text = raw_text.strip()
    if not raw_text: return jsonify({"error": "No text provided"}), 400

    try:
        flt, reg, msg = "UNK", "UNK", raw_text
        clean_text = raw_text.replace('\xa0', ' ').replace('\r', '')
        
        airbus_match = re.search(r'(BA\d{3,4}|CJ\d{3,4})\s+(G-[A-Z]{4})', clean_text)
        embraer_match = re.search(r'FI\s+(BA\d{3,4}|CJ\d{3,4})/AN\s+(G-[A-Z]{4})', clean_text)
        
        if embraer_match:
            flt = embraer_match.group(1).upper()
            reg = embraer_match.group(2).upper()
            # Split immediately after the "- FTX..." dispatch line to grab the pure message body
            parts = re.split(r'\n\s*-\s*FTX[^\n]*\n', clean_text)
            if len(parts) > 1:
                msg_lines = parts[1].split('\n')
                msg = '\n'.join([l.strip() for l in msg_lines if l.strip() and not l.startswith('\x03') and not l.startswith('[ ACK ]')])
            else:
                msg = clean_text
                
        elif airbus_match:
            flt = airbus_match.group(1).upper()
            reg = airbus_match.group(2).upper()
            parts = clean_text.split(airbus_match.group(0))
            if len(parts) > 1:
                raw_msg = parts[1]
                msg_lines = raw_msg.split('\n')[1:] 
                msg = '\n'.join([l.strip() for l in msg_lines if l.strip() and not l.startswith('MSG FROM') and not l.startswith('*') and not l.startswith('FTX')])
        else:
            fm = re.search(r'(BA\d{3,4}|CJ\d{3,4})', clean_text)
            rm = re.search(r'(G-[A-Z]{4})', clean_text)
            if fm: flt = fm.group(1).upper()
            if rm: reg = rm.group(1).upper()

        if flt.startswith('CJ'): flt = 'BA' + flt[2:]
            
        acars_cache[flt] = {"text": msg.strip(), "time": datetime.now(timezone.utc).strftime("%H:%M") + "Z", "ack": False, "reg": reg}
        db.session.add(AcarsLog(flight=flt, reg=reg, message=msg.strip()))
        db.session.commit()
        return jsonify({"message": f"ACARS Saved for {flt} / {reg}"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/ack_acars', methods=['POST'])
@login_required
def ack_acars():
    flt = request.json.get('flt')
    if flt in acars_cache: acars_cache[flt]['ack'] = True; return jsonify({"message": "Acknowledged"})
    return jsonify({"error": "Flight not found"}), 400
def parse_asm_to_scr_list(asm):
    fallback_acft = '098E90'
    lines = [l.strip() for l in asm.split('\n') if l.strip()]
    
    action_line = next((l for l in lines if any(l.startswith(x) for x in ['CNL', 'NEW', 'RPL', 'REV', 'TIM', 'RRT'])), None)
    if not action_line: raise ValueError("Could not detect CNL, NEW, RPL, REV, TIM, or RRT action in the text.")
        
    action = action_line.split()[0]
    flt_line_idx = lines.index(action_line) + 1
    
    if flt_line_idx >= len(lines): raise ValueError("Email is missing the flight details line.")
        
    flt_raw = lines[flt_line_idx].split()[0] 
    flt_match = re.search(r'[A-Z]{2}(\d+[A-Z]?)', flt_raw)
    flt_num = flt_match.group(1) if flt_match else flt_raw[2:]
    
    is_positioning = flt_num.endswith('P') or flt_num.endswith('E') or ' P ' in asm or '\nP ' in asm
    is_training = flt_num.endswith('T')
    
    if is_training: service_type = 'K'
    elif is_positioning: service_type = 'P'
    else: service_type = 'J'
    
    zero_seats = is_positioning or is_training
    
    date_match = re.search(r'/(\d{2}[A-Z]{3})(\d{2})', flt_raw)
    if not date_match:
        date_match = re.search(r'/(\d{2}[A-Z]{3})', flt_raw)
        if not date_match: raise ValueError("Could not extract the date from the flight line.")
        day_str = date_match.group(1)
        year_str = str(datetime.now().year)[2:]
    else:
        day_str = date_match.group(1)
        year_str = date_match.group(2)
        
    dt_obj = datetime.strptime(f"{day_str}{year_str}", "%d%b%y")
    dow = dt_obj.weekday() + 1 
    dow_str = ["0"] * 7
    dow_str[dow-1] = str(dow)
    dow_str = "".join(dow_str)
    
    season_year = dt_obj.strftime("%y")
    if 4 <= dt_obj.month <= 10: season = f"S{season_year}"
    elif dt_obj.month <= 3: season = f"W{int(season_year)-1}" 
    else: season = f"W{season_year}"
    
    origin, dest = "XXX", "XXX"
    dep_time, arr_time = "XXXX", "XXXX"
    acft_str = fallback_acft
    
    if action == "CNL":
        parts = lines[flt_line_idx].split()
        if len(parts) > 1 and '/' in parts[1]: origin, dest = parts[1].split('/')
        
        if not flight_schedule_df.empty:
            match = flight_schedule_df[flight_schedule_df['FLT'].astype(str).str.contains(flt_num, na=False)]
            if not match.empty:
                dt = str(match.iloc[0].get('STD', 'XXXX')).replace(':', '').zfill(4)
                at = str(match.iloc[0].get('STA', 'XXXX')).replace(':', '').zfill(4)
                if dt != 'XXXX': dep_time = dt
                if at != 'XXXX': arr_time = at
        
        if zero_seats:
            base_acft = acft_str[-3:] if len(acft_str) > 3 else "E90"
            acft_str = f"000{base_acft}"
            
    elif action in ["NEW", "RPL", "REV", "TIM", "RRT"]:
        route_line_idx = -1
        for idx in range(flt_line_idx + 1, min(flt_line_idx + 4, len(lines))):
            if re.match(r'^[A-Z]{3}\d{4,6}\s+[A-Z]{3}\d{4,6}', lines[idx]):
                route_line_idx = idx
                break
                
        if route_line_idx != -1:
            route_parts = lines[route_line_idx].split()
            origin = route_parts[0][:3]
            dep_time = route_parts[0][-4:]
            dest = route_parts[1][:3]
            arr_time = route_parts[1][-4:]
        
        if route_line_idx > flt_line_idx + 1:
            equip_line = lines[flt_line_idx+1].split()
            svc = equip_line[0] 
            if svc in ['J', 'P', 'C', 'F', 'K', 'T', 'X']:
                service_type = svc
                if svc in ['P', 'K', 'T', 'X']: zero_seats = True
            
            ac_type = equip_line[1] if len(equip_line) > 1 else fallback_acft[-3:]
            
            if zero_seats:
                acft_str = f"000{ac_type}"
            else:
                if len(equip_line) > 2:
                    primary_config = equip_line[2].split('V')[0]
                    configs = re.findall(r'\d+', primary_config)
                    if configs:
                        seats = str(sum(int(x) for x in configs)).zfill(3)
                        acft_str = f"{seats}{ac_type}"
                    else:
                        acft_str = fallback_acft
                else: acft_str = fallback_acft
        else:
            if zero_seats: acft_str = f"000{fallback_acft[-3:]}"
    
    airline = "EFW" 
    if any(x in acft_str for x in ["E90", "E75", "E19", "EMB"]): airline = "CFE"
    elif any(x in acft_str for x in ["320", "321", "31E", "32E", "31", "32"]): airline = "EFW"
    else:
        if flt_num.startswith('8') or flt_num.startswith('4') or flt_num.startswith('3') or flt_num.startswith('7') or flt_num.startswith('9'): 
            airline = "CFE"
    
    uk_ba_stations = ["LCY", "LGW", "LHR", "STN", "LTN", "SEN", "EDI", "GLA", "ABZ", "INV", "PIK", "KOI", "LSI", "SYY", "DND", "BHD", "BFS", "LDY", "MAN", "BHX", "BRS", "NCL", "NQY", "SOU", "EXT", "LBA", "MME", "HUY", "CWL", "DUB", "JER", "GCI", "IOM", "AMS", "LUX", "RTM"]
    
    results = []
    
    for stn in [origin, dest]:
        if stn == "XXX": continue
        
        if airline == "CFE":
            designator = "BA" if stn in uk_ba_stations else "CJ"
            email = "/bacityflyer.ops@ba.com"
            signoff = "GI BRGDS BACF OPS"
        else:
            designator = "BA" if stn in uk_ba_stations else "A0"
            email = "/baeuroflyer.ops@ba.com"
            signoff = "GI BRGDS BAEF OPS"
            
        scr_lines = ["SCR", email, season, dt_obj.strftime("%d%b").upper(), stn]
        
        is_dep = (stn == origin)
        act_spc = " " if is_dep else ""
        
        if action == "CNL":
            if is_dep: scr_action = f"D{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {dep_time}{dest} {service_type}"
            else: scr_action = f"D{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {origin}{arr_time} {service_type}"
            scr_lines.append(scr_action)
            scr_lines.append("SI CANX FLIGHT DUE DISRUPTION")
            
        elif action in ["NEW", "RPL", "REV", "TIM", "RRT"]:
            new_time = dep_time if is_dep else arr_time
            other_stn = dest if is_dep else origin
            
            orig_time = None
            if not flight_schedule_df.empty and action in ["RPL", "REV", "TIM"]:
                match = flight_schedule_df[flight_schedule_df['FLT'].astype(str).str.contains(flt_num, na=False)]
                if not match.empty:
                    if is_dep and str(match.iloc[0].get('DEP', '')).strip().upper() == stn:
                        orig_time = str(match.iloc[0].get('STD', '')).replace(':', '').zfill(4)
                    elif not is_dep and str(match.iloc[0].get('ARR', '')).strip().upper() == stn:
                        orig_time = str(match.iloc[0].get('STA', '')).replace(':', '').zfill(4)
                        
            if action in ["RPL", "REV", "TIM"] and orig_time and orig_time != "0000":
                if is_dep:
                    scr_lines.append(f"C{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {orig_time}{other_stn} {service_type}")
                    scr_lines.append(f"R{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {new_time}{other_stn} {service_type}")
                else:
                    scr_lines.append(f"C{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {other_stn}{orig_time} {service_type}")
                    scr_lines.append(f"R{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {other_stn}{new_time} {service_type}")
                si_line = next((l for l in lines if l.startswith('SI ')), "SI SCHEDULE REVISION")
                scr_lines.append(si_line)
            else:
                if is_dep: scr_action = f"N{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {new_time}{other_stn} {service_type}"
                else: scr_action = f"N{act_spc}{designator}{flt_num} {day_str}{day_str} {dow_str} {acft_str} {other_stn}{new_time} {service_type}"
                scr_lines.append(scr_action)
                
                if action == "NEW": default_si = "SI NEW FLIGHT ADDED"
                elif action == "RRT": default_si = "SI A/C REROUTE"
                else: default_si = "SI AD HOC REVISION"
                si_line = next((l for l in lines if l.startswith('SI ')), default_si)
                scr_lines.append(si_line)
            
        scr_lines.append(signoff)
        
        results.append({
            "flight": f"{designator}{flt_num}",
            "station": stn,
            "date": dt_obj.strftime("%d%b").upper(),
            "scr_text": "\n".join(scr_lines)
        })
        
    return results

@app.route('/api/generate_scr', methods=['POST'])
@login_required
def generate_scr():
    try:
        data = request.json or {}
        asm = data.get('asm_text', '').strip()
        if not asm: return jsonify({"error": "Missing ASM text. Paste the email first."})
        
        scr_list = parse_asm_to_scr_list(asm)
        outputs = [item['scr_text'] for item in scr_list]
        return jsonify({"scr": "\n\n==========================\n\n".join(outputs)})
    except Exception as e:
        return jsonify({"error": f"Python Processing Error: {str(e)}"})

@app.route('/api/generate_itr', methods=['POST'])
@login_required
def generate_itr():
    try:
        data = request.json or {}
        aar_text = data.get('aar_text', '').strip()
        if not aar_text: return jsonify({"error": "No AAR text provided."})

        # Set up date references
        now_utc = datetime.now(timezone.utc)
        today = now_utc.date()
        tomorrow = today + timedelta(days=1)
        
        routings = {} # Format: { date_string: { reg_short: [flt1, flt2] } }

        # Scan the AAR text line by line
        for line in aar_text.split('\n'):
            # Look for lines starting with "DD.MM" followed by flights and regs
            match = re.search(r'^(\d{2})\.(\d{2})\s+(?:BA|CJ|A0)?\s*(\d{3,4}[a-zA-Z]?)\s+.*?\bG-?([A-Za-z]{4})\b', line.strip(), re.IGNORECASE)
            if match:
                day, month, flt_num, reg = match.groups()
                
                try:
                    flight_date = datetime(year=today.year, month=int(month), day=int(day)).date()
                    # Handle year wrap-around if looking at Jan flights in Dec
                    if flight_date < today - timedelta(days=15): flight_date = flight_date.replace(year=today.year + 1)
                except: continue

                reg_short = reg[-3:].upper() # e.g., CAB
                flt_clean = flt_num.upper()
                
                if flight_date not in routings: routings[flight_date] = {}
                if reg_short not in routings[flight_date]: routings[flight_date][reg_short] = []
                routings[flight_date][reg_short].append(flt_clean)

        if not routings: return jsonify({"error": "Could not extract valid dates and flights from the AAR."})

        # Build the FICO strings
        output_lines = []
        for f_date, tails in routings.items():
            if f_date == today: prefix = "I TR"
            elif f_date == tomorrow: prefix = "I TR T"
            else: prefix = f"I TR {f_date.strftime('%d%b').upper()}"
            
            for reg, flights in tails.items():
                flt_str = " ".join(flights)
                output_lines.append(f"{prefix} {reg} {flt_str}")

        return jsonify({"itr": "\n".join(output_lines)})
        
    except Exception as e:
        return jsonify({"error": f"Python Processing Error: {str(e)}"})

# ── SHARED AUTO-DOSSIER CREATION ────────────────────────────────────────
def _auto_create_dossier(flt, event_type, origin, sched_dest, actual_dest,
                          logged_by="AUTO-ASM", ba_code=None, notes=None):
    """Create a DisruptionLog entry from ASM data using live wx/notam caches.
    Called by asm_webhook on CNL and RRT-with-DIV. Returns (log_id, case_ref) or None."""
    try:
        now_utc = datetime.now(timezone.utc)
        log_id  = f"{flt}_{now_utc.strftime('%Y-%m-%d')}_{event_type}"
        if db.session.get(DisruptionLog, log_id):
            print(f"AUTO-ASM: dossier {log_id} already exists — skipping")
            return log_id, None

        # ── SI CLASSIFICATION ──────────────────────────────────────────
        si_class = classify_si_line(notes or '', origin, sched_dest, event_type)
        # Use classified problem airport as primary target
        target = si_class['problem_airport'] or actual_dest or sched_dest or origin

        # Weather snapshot — both airports
        _apts = list(dict.fromkeys([a for a in [sched_dest, actual_dest, origin] if a and a != "N/A"]))
        _wx_snaps, _t_snaps, _n_snaps = [], [], []
        for _apt in _apts:
            try:
                if _apt in raw_weather_cache:
                    _m = raw_weather_cache[_apt].get("m")
                    _t = raw_weather_cache[_apt].get("t")
                    if _m: _wx_snaps.append(f"[{_apt}] " + (getattr(_m, "raw", "") or ""))
                    if _t:  _t_snaps.append(f"[{_apt}] " + (getattr(_t, "raw", "") or ""))
                if _apt in raw_notam_cache and raw_notam_cache[_apt]:
                    _n_snaps.append(f"=== {_apt} NOTAMs ===\n" + "\n\n".join(raw_notam_cache[_apt]))
            except Exception: pass
        wx_snap = "\n".join(_wx_snaps) if _wx_snaps else "N/A"
        t_snap  = "\n".join(_t_snaps)  if _t_snaps  else "N/A"
        n_snap  = "\n\n".join(_n_snaps) if _n_snaps  else "N/A"

        # ACARS last 6hrs
        cutoff = now_utc - timedelta(hours=6)
        try:
            acars_msgs = AcarsLog.query.filter(
                AcarsLog.flight == flt, AcarsLog.timestamp >= cutoff
            ).order_by(AcarsLog.timestamp.asc()).all()
            a_snap = "\n".join([
                f"{m.timestamp.strftime('%H:%MZ')} {m.reg}: {m.message}"
                for m in acars_msgs]) if acars_msgs else "N/A"
        except Exception: a_snap = "N/A"

        # Case ref
        try:
            last = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).first()
            last_num = int(last.case_ref.split("-")[-1]) if last and last.case_ref else 0
        except Exception: last_num = 0
        c_ref = f"CF-{last_num+1:03d}"

        # Crosswind
        xw_val = xw_lim_str = "N/A"
        try:
            if target in network_data:
                xw_val = str(network_data[target].get("cur_xw", "N/A"))
            # Fallback: calculate from METAR if network_data doesn't have it
            if (xw_val == "N/A" or xw_val == "0") and target in raw_weather_cache:
                _mm = raw_weather_cache[target].get("m")
                if _mm and hasattr(_mm, "data") and _mm.data:
                    try:
                        _w_dir = get_safe_num(getattr(_mm.data.wind_direction, 'value', None))
                        _w_spd = get_safe_num(getattr(_mm.data.wind_speed, 'value', None))
                        _w_gst = get_safe_num(getattr(_mm.data.wind_gust, 'value', None)) or 0
                        _rwy_hdg = ops.get(target, {}).get('rwy', None)
                        _is_ow = ops.get(target, {}).get('one_way', False)
                        if _w_dir is not None and _w_spd is not None and _rwy_hdg:
                            _calc_xw, _, _ = calculate_winds(_w_dir, max(_w_spd, _w_gst), _rwy_hdg, _is_ow)
                            xw_val = str(_calc_xw)
                    except Exception: pass
            _apt_bl = ops.get(target, {}).get("xw_lim", None)
            _wxc, _tc = [], None
            if target in raw_weather_cache:
                _mm = raw_weather_cache[target].get("m")
                if _mm and hasattr(_mm, "data") and _mm.data:
                    try: _wxc = [w.repr for w in (_mm.data.wx_codes or [])]
                    except: pass
                    try: _tc = get_safe_num(_mm.data.temperature.value)
                    except: pass
            _ol, _al, _ck, _cl, _bf, _ak = get_operative_xw_limit("A320", _wxc, _apt_bl, _tc)
            xw_lim_str = f"{_ol}kt ({_ak} / {_cl} / binding:{_bf})" if _apt_bl else f"{_ol}kt ({_ak} / {_cl})"
        except Exception: pass

        # ── LIVING DOSSIER LIFECYCLE ────────────────────────────────────
        accum_hours = ACCUMULATION_DEFAULTS.get(event_type, 6)
        _close_time = now_utc + timedelta(hours=accum_hours)

        # Initial METAR evolution entry
        _init_metar_evo = []
        try:
            _prob_apt = si_class['problem_airport'] or sched_dest or origin
            if _prob_apt in raw_weather_cache:
                _me = raw_weather_cache[_prob_apt].get("m")
                if _me:
                    _init_metar_evo.append({
                        'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'),
                        'icao': _prob_apt,
                        'raw_metar': getattr(_me, 'raw', '') or ''
                    })
        except Exception: pass

        db.session.add(DisruptionLog(
            id=log_id, flight=flt, date=now_utc.strftime("%Y-%m-%d"),
            event_type=event_type, origin=origin,
            sched_dest=sched_dest, actual_dest=actual_dest or sched_dest,
            weather_snap=wx_snap, taf_snap=t_snap, notam_snap=n_snap,
            acars_snap=a_snap, tail_snap="N/A",
            xw_snap=xw_val, xw_limit=xw_lim_str,
            ba_code=ba_code, notes=notes,
            case_ref=c_ref, logged_by=logged_by,
            metar_history=None,
            # SI classification
            si_cause=si_class['cause'],
            si_cause_label=si_class['cause_label'],
            si_problem_airport=si_class['problem_airport'],
            si_airport_focus=si_class['airport_focus'],
            si_section_priority=json.dumps(si_class['section_priority']),
            # Living dossier lifecycle
            dossier_status='ACTIVE',
            close_time=_close_time,
            metar_evolution=json.dumps(_init_metar_evo) if _init_metar_evo else '[]',
        ))
        db.session.commit()

        # METAR history async
        def _bg(lid, apt):
            try:
                _icao = ops.get(apt, {}).get("icao", apt)
                if not _icao: return
                _h = fetch_metar_history(_icao, hours=12)
                if _h:
                    with app.app_context():
                        _l = db.session.get(DisruptionLog, lid)
                        if _l: _l.metar_history = _h; db.session.commit()
            except Exception as _e: print(f"METAR hist bg error: {_e}")
        threading.Thread(target=_bg, args=(log_id, sched_dest or origin), daemon=True).start()

        print(f"AUTO-ASM dossier created: {log_id} ({c_ref}) event={event_type} "
              f"origin={origin} sched={sched_dest} actual={actual_dest}")
        return log_id, c_ref
    except Exception as _e:
        print(f"_auto_create_dossier error: {_e}")
        try: db.session.rollback()
        except: pass
        return None, None

@app.route('/api/div_webhook', methods=['POST'])
def div_webhook():
    """Ingest a DIV message (SITA MVTS format) and record original dest.
    Format: DIV\nBA2620/26.GEUUV.INN\nEA1300 MUC\n..."""
    raw = ''
    if request.is_json: raw = request.json.get('div_text', '')
    elif request.form:  raw = request.form.get('div_text', '')
    if not raw and request.data: raw = request.data.decode('utf-8', errors='ignore')
    raw = raw.strip()
    if not raw: return jsonify({'error': 'No DIV text provided'}), 400
    try:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        # Line 1: DIV or header  Line 2: BA2620/26.GEUUV.INN
        flt = orig_apt = div_apt = None
        for line in lines:
            # BA2620/26.GEUUV.INN — flight/date.reg.original_dest
            _m = re.match(r'(BA|CJ)?(\d{3,4}[A-Z]?)/\d+\.([A-Z-]+)\.([A-Z]{3})', line.upper())
            if _m:
                flt = 'BA' + _m.group(2)
                orig_apt = _m.group(4)
            # EA1300 MUC — estimated arrival + divert airport
            _ea = re.match(r'EA\d{4}\s+([A-Z]{3})', line.upper())
            if _ea: div_apt = _ea.group(1)
        if flt and orig_apt:
            _divert_memory_set(flt, orig_apt)
            print(f'DIV message: {flt} diverted from {orig_apt} to {div_apt or "?"}')
        # Auto-create dossier if not already exists
        if flt and orig_apt and div_apt:
            _log_id = f"{flt}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_DIVERT"
            if not db.session.get(DisruptionLog, _log_id):
                _wx = _ta = _no = 'N/A'
                if orig_apt in raw_weather_cache:
                    _wx = getattr(raw_weather_cache[orig_apt].get('m'), 'raw', 'N/A') or 'N/A'
                    _ta = getattr(raw_weather_cache[orig_apt].get('t'), 'raw', 'N/A') or 'N/A'
                if orig_apt in raw_notam_cache:
                    _no = '\n\n'.join(raw_notam_cache[orig_apt]) or 'N/A'
                _last = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).first()
                _ln = 0
                if _last and _last.case_ref:
                    try: _ln = int(_last.case_ref.split('-')[-1])
                    except: pass
                _cr = f'CF-{_ln+1:03d}'
                db.session.add(DisruptionLog(
                    id=_log_id, flight=flt,
                    date=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                    event_type='DIVERT',
                    origin='', sched_dest=orig_apt, actual_dest=div_apt,
                    weather_snap=_wx, taf_snap=_ta, notam_snap=_no,
                    case_ref=_cr, logged_by='AUTO-DIV', metar_history=None
                ))
                try:
                    db.session.commit()
                    # Fetch METAR history async
                    def _bg_div_hist(lid, apt):
                        try:
                            _icao = ops.get(apt, {}).get('icao', apt)
                            _h = fetch_metar_history(_icao, hours=12)
                            if _h:
                                with app.app_context():
                                    _l = db.session.get(DisruptionLog, lid)
                                    if _l: _l.metar_history = _h; db.session.commit()
                        except: pass
                    threading.Thread(target=_bg_div_hist, args=(_log_id, orig_apt), daemon=True).start()
                    return jsonify({'ok': True, 'case_ref': _cr, 'dossier': _log_id})
                except:
                    db.session.rollback()
            else:
                return jsonify({'ok': True, 'existing': True, 'id': _log_id})
        return jsonify({'ok': True, 'flt': flt, 'orig': orig_apt, 'div': div_apt})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/asm_webhook', methods=['POST'])
def asm_webhook():
    try:
        asm = ""
        if request.is_json: asm = request.json.get('asm_text', '')
        elif request.form: asm = request.form.get('asm_text', '')
        if not asm and request.data: asm = request.data.decode('utf-8', errors='ignore')
        
        asm = asm.strip()
        if not asm: return jsonify({"error": "No ASM text provided"}), 400

        # Check if this is a DIV format message — route to div_webhook logic
        _first_line = asm.split('\n')[0].strip().upper()
        if _first_line == 'DIV' or _first_line.startswith('DIV '):
            # Re-use the div_webhook parser inline
            lines = [l.strip() for l in asm.splitlines() if l.strip()]
            _flt = _orig_apt = _div_apt = None
            for _ln in lines:
                _m = re.match(r'(BA|CJ)?(\d{3,4}[A-Z]?)/\d+\.([A-Z-]+)\.([A-Z]{3})', _ln.upper())
                if _m: _flt = 'BA' + _m.group(2); _orig_apt = _m.group(4)
                _ea = re.match(r'EA\d{4}\s+([A-Z]{3})', _ln.upper())
                if _ea: _div_apt = _ea.group(1)
            if _flt and _orig_apt:
                _divert_memory_set(_flt, _orig_apt)
                print(f'DIV via asm_webhook: {_flt} orig={_orig_apt} div={_div_apt}')
            return jsonify({"message": "DIV message received", "flt": _flt, "orig": _orig_apt, "div": _div_apt})

        scr_list = parse_asm_to_scr_list(asm)
        for item in scr_list:
            slot = SlotLog(flight=item['flight'], date=item['date'], station=item['station'], scr_text=item['scr_text'])
            db.session.add(slot)
        db.session.commit()

        # Divert memory: capture original ARR before RRT overwrites schedule
        # This lets auto-capture detect is_diverted correctly after RRT
        try:
            asm_upper = asm.upper()
            if any(act in asm_upper for act in ['RRT ', '\nRRT']):
                # Extract flight number from ASM
                _fm = re.search(r'\b(BA|CJ)?(\d{3,4})\b', asm_upper)
                if _fm and not flight_schedule_df.empty:
                    _flt_num = 'BA' + _fm.group(2)
                    _match = flight_schedule_df[
                        flight_schedule_df['FLT'].astype(str).str.upper().str.contains(_fm.group(2), na=False)
                    ]
                    if not _match.empty:
                        _orig_arr = str(_match.iloc[0].get('ARR', '')).strip().upper()
                        if _orig_arr and _orig_arr not in ('NAN', ''):
                            _divert_memory_set(_flt_num, _orig_arr)
                            print(f'Divert memory SET called for: {_flt_num} orig={_orig_arr}')
        except Exception:
            pass

        # ── PRE-FETCH WX FOR DOSSIER AIRPORTS IF NOT CACHED ───────────
        # If the wx cache is cold for the airports in this ASM, fetch now
        # so the dossier captures real data rather than N/A
        try:
            _asm_apts_to_fetch = {}
            for _ln in asm.splitlines():
                # Match airport codes in route lines and FLR/LCY style lines
                for _ap in re.findall(r'\b([A-Z]{3})\b', _ln.upper()):
                    if _ap in ops and _ap not in raw_weather_cache:
                        _asm_apts_to_fetch[_ap] = ops[_ap]
                    elif _ap in DIVERT_ALT_WX and _ap not in raw_weather_cache:
                        _asm_apts_to_fetch[_ap] = DIVERT_ALT_WX[_ap]
            if _asm_apts_to_fetch:
                print(f"ASM pre-fetch wx for: {list(_asm_apts_to_fetch.keys())}")
                with ThreadPoolExecutor(max_workers=4) as _wx_ex:
                    _wx_futs = {_wx_ex.submit(_fetch_airport_wx, _ia, _iv['icao']): _ia
                                for _ia, _iv in _asm_apts_to_fetch.items()}
                    for _wf in as_completed(_wx_futs, timeout=12):
                        _wia = _wx_futs[_wf]
                        try:
                            _wia2, _wres = _wf.result(timeout=8)
                            if 'wx' in _wres: raw_weather_cache[_wia2] = _wres['wx']
                            if 'notam' in _wres: raw_notam_cache[_wia2] = _wres['notam']
                        except Exception: pass
        except Exception as _wfe:
            print(f"ASM pre-fetch wx error: {_wfe}")

        # ── AUTO-DOSSIER FROM CNL ──────────────────────────────────────
        # Parse CNL ASM and create EU261 dossier immediately
        try:
            asm_upper = asm.upper()
            if any(act in asm_upper for act in ["\nCNL ", "CNL "]):
                # Extract from scr_list (already parsed above)
                for _item in scr_list:
                    _cnl_flt = _item.get("flight", "").upper()
                    if not _cnl_flt: continue
                    # Parse origin/dest from ASM flight line
                    _cnl_origin = ""; _cnl_dest = ""
                    for _ln in asm.splitlines():
                        # e.g. BA8472/27MAR26 FLR/LCY
                        _rd = re.search(r'([A-Z]{3})/([A-Z]{3})', _ln.upper())
                        if _rd:
                            _cnl_origin = _rd.group(1)
                            _cnl_dest   = _rd.group(2)
                            break
                    # Extract BA code from action header line: "CNL WEAN"
                    _ba_code = ""
                    for _ln in asm.splitlines():
                        _lnu = _ln.strip().upper()
                        if _lnu.startswith("CNL "):
                            _pts = _lnu.split()
                            if len(_pts) >= 2 and re.match(r'^[A-Z]{4}$', _pts[1]):
                                _ba_code = _pts[1]
                                break
                    # Extract notes from SI line
                    _notes = ""
                    for _ln in asm.splitlines():
                        if _ln.strip().upper().startswith("SI "):
                            _notes = _ln.strip()[3:]
                            break
                    if _cnl_flt:
                        _log_id, _cref = _auto_create_dossier(
                            flt=_cnl_flt, event_type="CANCEL",
                            origin=_cnl_origin, sched_dest=_cnl_dest,
                            actual_dest=_cnl_dest,
                            logged_by="AUTO-CNL",
                            ba_code=_ba_code or None,
                            notes=_notes or None
                        )
                        if _log_id:
                            print(f"AUTO-CNL dossier: {_log_id}")
                    break  # only process first flight in CNL
        except Exception as _e:
            print(f"AUTO-CNL dossier error: {_e}")

        # ── AUTO-DOSSIER FROM RRT WITH DIV IN SI ──────────────────────
        try:
            asm_upper = asm.upper()
            _si_line = next((l for l in asm.splitlines() if l.strip().upper().startswith("SI ")), "")
            _si_upper = _si_line.upper()
            _is_rrt = any(act in asm_upper for act in ["\nRRT ", "RRT "])
            _is_div_si = any(k in _si_upper for k in ["DIV", "DIVERT", "DIVERSION"])
            if _is_rrt and _is_div_si and scr_list:
                _item = scr_list[0]
                _rrt_flt = _item.get("flight","").upper()

                # Parse RRT route line: e.g. LCY270805 PSA271010
                # origin = departure airport, dest = NEW (divert) airport
                _rrt_origin = ""; _rrt_dest = ""
                for _ln in asm.splitlines():
                    _rm = re.match(r'([A-Z]{3})\d{4,6}\s+([A-Z]{3})\d{4,6}', _ln.strip().upper())
                    if _rm:
                        _rrt_origin = _rm.group(1)
                        _rrt_dest   = _rm.group(2)
                        break

                # Original scheduled destination — 3-tier lookup:
                # 1. divert_memory (set earlier in this same webhook call)
                # 2. Schedule CSV direct lookup
                # 3. Extract from SI text (e.g. "DIV PSA DUE FLR..." → FLR)
                _orig_dest = _divert_memory_get(_rrt_flt) or ""

                if not _orig_dest and not flight_schedule_df.empty:
                    try:
                        _flt_num = re.sub(r'[^0-9]', '', _rrt_flt)
                        _smatch = flight_schedule_df[
                            flight_schedule_df['FLT'].astype(str).str.contains(_flt_num, na=False)
                        ]
                        if not _smatch.empty:
                            _orig_dest = str(_smatch.iloc[0].get('ARR', '')).strip().upper()
                            if _orig_dest and _orig_dest not in ('NAN', ''):
                                # Also persist so other worker knows
                                _divert_memory_set(_rrt_flt, _orig_dest)
                            else:
                                _orig_dest = ""
                    except Exception: pass

                if not _orig_dest:
                    # Last resort: parse from SI text
                    # e.g. "SI DIV PSA DUE FLR TAILWINDS..." → airport before "TAILWIND/DUE"
                    _si_apts = re.findall(r'\b([A-Z]{3})\b', _si_upper)
                    # Filter to known airports — skip DIV/DUE/FLT/PSA (actual_dest)
                    _known_iata = set(ops.keys()) | set(COMMON_ALT_AIRPORTS.keys())
                    for _sa in _si_apts:
                        if _sa in _known_iata and _sa != _rrt_dest and _sa != _rrt_origin:
                            _orig_dest = _sa
                            break

                # BA delay code: look for 4-letter code on the action header line
                # e.g. "RRT WEAN" → WEAN
                _ba_code = ""
                for _ln in asm.splitlines():
                    _lnu = _ln.strip().upper()
                    if _lnu.startswith("RRT "):
                        _parts = _lnu.split()
                        # RRT <BA_CODE> — code is the token after RRT
                        if len(_parts) >= 2 and re.match(r'^[A-Z]{4}$', _parts[1]):
                            _ba_code = _parts[1]
                            break

                _notes = _si_line.strip()[3:] if _si_line else None

                if _rrt_flt:
                    _log_id, _cref = _auto_create_dossier(
                        flt=_rrt_flt, event_type="DIVERT",
                        origin=_rrt_origin,
                        sched_dest=_orig_dest or _rrt_origin,
                        actual_dest=_rrt_dest,
                        logged_by="AUTO-RRT",
                        ba_code=_ba_code or None,
                        notes=_notes
                    )
                    if _log_id:
                        print(f"AUTO-RRT dossier: {_log_id} "
                              f"origin={_rrt_origin} sched={_orig_dest} actual={_rrt_dest} "
                              f"ba_code={_ba_code}")
        except Exception as _e:
            print(f"AUTO-RRT dossier error: {_e}")

        return jsonify({"message": "ASM ingested and slots created."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/slots', methods=['GET'])
@login_required
def get_slots():
    slots = SlotLog.query.order_by(SlotLog.timestamp.desc()).limit(50).all()
    result = []
    for s in slots:
        result.append({
            "id": s.id, "flight": s.flight, "date": s.date, "station": s.station,
            "scr_text": s.scr_text, "status": s.status, "reply": s.coordinator_reply,
            "time": s.timestamp.strftime("%Y-%m-%d %H:%M")
        })
    return jsonify(result)

@app.route('/api/slots/history', methods=['GET'])
@login_required
def get_slots_history():
    """Full SCR history — raw SQL so missing columns never crash the endpoint."""
    status_f = request.args.get('status', '').strip()
    flight_f = request.args.get('flight', '').upper().strip()
    try:
        with db.engine.connect() as conn:
            # Probe for new columns — graceful fallback if not migrated yet
            try:
                conn.execute(db.text("SELECT sent_count FROM slot_log LIMIT 1"))
                has_new = True
            except Exception:
                has_new = False

            if has_new:
                q = ("SELECT id,flight,date,station,scr_text,status,"
                     "coordinator_reply,sent_count,resolved_by,resolved_at,timestamp "
                     "FROM slot_log")
            else:
                q = ("SELECT id,flight,date,station,scr_text,status,"
                     "coordinator_reply,NULL,NULL,NULL,timestamp "
                     "FROM slot_log")

            filters, params = [], {}
            if status_f:
                filters.append("status = :status"); params['status'] = status_f
            if flight_f:
                filters.append("flight LIKE :flight"); params['flight'] = f'%{flight_f}%'
            if filters:
                q += " WHERE " + " AND ".join(filters)
            q += " ORDER BY timestamp DESC LIMIT 200"

            rows = conn.execute(db.text(q), params).fetchall()

        result = []
        for r in rows:
            try:
                ts = r[10]
                ts_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts,'strftime') else str(ts or '')[:16]
                ra = r[9]
                ra_str = ra.strftime('%Y-%m-%d %H:%M') if hasattr(ra,'strftime') else (str(ra)[:16] if ra else None)
                result.append({
                    "id": r[0], "flight": r[1] or '', "date": r[2] or '',
                    "station": r[3] or '', "scr_text": r[4] or '',
                    "status": r[5] or 'PENDING', "reply": r[6],
                    "sent_count": int(r[7] or 1),
                    "resolved_by": r[8],
                    "resolved_at": ra_str,
                    "time": ts_str,
                })
            except Exception:
                pass
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/slots/<int:slot_id>', methods=['PUT'])
@login_required
def update_slot(slot_id):
    slot = SlotLog.query.get_or_404(slot_id)
    data = request.json
    if 'action' in data:
        if data['action'] == 'SEND':
            slot.status = 'SENT'
            slot.scr_text = data.get('scr_text', slot.scr_text)
            slot.sent_count = (getattr(slot, 'sent_count', 0) or 0) + 1
            webhook_url = os.environ.get("MAKE_SCR_WEBHOOK", "")
            if webhook_url:
                try:
                    requests.post(webhook_url, json={
                        "flight": slot.flight,
                        "station": slot.station,
                        "scr_text": slot.scr_text,
                        "subject": f"URGENT: SCR REQUIRED - {slot.flight} {slot.station}"
                    }, timeout=5)
                except: pass
        elif data['action'] in ('CLEAR', 'RESOLVED'):
            slot.status = 'HANDLED'
            slot.resolved_by = current_user.username
            slot.resolved_at = datetime.utcnow()
        elif data['action'] == 'SAVE':
            slot.scr_text = data.get('scr_text', slot.scr_text)

    db.session.commit()
    return jsonify({"message": "Success"})

@app.route('/api/coordinator_webhook', methods=['POST'])
def coordinator_webhook():
    data = request.json
    flight = data.get('flight')
    station = data.get('station')
    status_code = data.get('status_code') 
    reply_text = data.get('reply_text', '')
    
    slot = SlotLog.query.filter_by(flight=flight, station=station).order_by(SlotLog.timestamp.desc()).first()
    if slot:
        if status_code in ['K', 'X']: slot.status = 'CLEARED'
        else: slot.status = 'REJECTED'
        
        slot.coordinator_reply = reply_text
        db.session.commit()
        return jsonify({"message": "Updated"})
    return jsonify({"error": "Slot not found"}), 404

# ── EU261 HELPERS ────────────────────────────────────────────────────────

# Canonical section names used across UI, DB, and PDF
EU261_SECTIONS = [
    ('metar_taf',          '1. Weather (METAR/TAF)'),
    ('metar_history',      '2. 12hr METAR History'),
    ('crosswind',          '3. Crosswind Assessment'),
    ('notams',             '4. NOTAM Data'),
    ('acars',              '5. ACARS Messages'),
    ('conditions_evo',     '6. Conditions Evolution (TAF vs Actual)'),
    ('ops_context',        '7. Operational Context (Station Picture)'),
    ('controller_log',     '8. Controller Decision Log'),
    ('supporting_evidence','9. Supporting Evidence'),
]

EVIDENCE_SOURCES = {
    'metar_taf':           'AVWX (avwx.rest) — sourced from NOAA Aviation Weather Center',
    'metar_history':       'NOAA Aviation Weather Center API (aviationweather.gov) — 12hr observation history',
    'crosswind':           'Calculated from live METAR wind data at time of event',
    'notams':              'FAA NOTAM API (notams.aim.faa.gov) — real-time NOTAM feed',
    'acars':               'BA CityFlyer OCC Router — direct airline ACARS feed (last 6hrs)',
    'conditions_evo':      'NOAA AWC (live METAR feed) vs TAF at decision time — automated accumulation during active dossier window',
    'ops_context':         'OCC Intelligence Platform — station-level disruption tracking (diversions, delays, cancellations)',
    'controller_log':      'OCC Intelligence Platform — entered directly by operations controller',
    'supporting_evidence': 'OCC Intelligence Platform — manually attached by operations team',
}

def fetch_metar_history(icao, hours=12):
    """Fetch last N hours of raw METARs from NOAA AWC API. Returns newline-joined string."""
    try:
        url = f'https://aviationweather.gov/api/data/metar?ids={icao}&hours={hours}&format=raw'
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200 and resp.text.strip():
            lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
            return '\n'.join(lines)
    except Exception as e:
        print(f'METAR history fetch failed for {icao}: {e}')
    return None

def get_hidden(log):
    """Return set of hidden section names for a dossier."""
    raw = getattr(log, 'hidden_sections', None)
    if not raw: return set()
    try: return set(json.loads(raw))
    except: return set()

def get_audit(log):
    """Return list of audit entries for a dossier."""
    raw = getattr(log, 'section_audit', None)
    if not raw: return []
    try: return json.loads(raw)
    except: return []

# ─────────────────────────────────────────────────────────────────────────

@app.route('/api/dossiers')
@login_required
def get_dossiers():
    try:
        logs = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).all()
    except Exception as e:
        print(f"get_dossiers DB error: {e}")
        return jsonify({"error": f"Database error: {e}"}), 500
    res = {}
    for log in logs:
        try:
            d = log.date or 'unknown'
            if d not in res: res[d] = []
            ev_fields = [
                log.weather_snap,
                getattr(log,'taf_snap',None),
                getattr(log,'notam_snap',None),
                getattr(log,'acars_snap',None),
                getattr(log,'ba_code',None),
                getattr(log,'logged_by',None),
                getattr(log,'notes',None),
                getattr(log,'xw_snap',None),
                getattr(log,'metar_history',None),
            ]
            ev_score = sum(1 for f in ev_fields if f and str(f).strip() not in ('','N/A','None'))
            ts_time  = log.timestamp.strftime("%H:%MZ") if log.timestamp else 'N/A'
            ts_full  = log.timestamp.strftime("%d %b %Y %H:%MZ") if log.timestamp else 'N/A'
            res[d].append({
                "id":             log.id,
                "case_ref":       getattr(log,'case_ref',None) or log.id,
                "flight":         log.flight or '',
                "event":          log.event_type or '',
                "origin":         log.origin or '',
                "dest":           log.sched_dest or '',
                "actual_dest":    log.actual_dest or '',
                "tail":           getattr(log,'tail_snap','N/A'),
                "metar":          log.weather_snap,
                "taf":            getattr(log,'taf_snap','N/A'),
                "notams":         getattr(log,'notam_snap','N/A'),
                "acars":          getattr(log,'acars_snap','N/A'),
                "ba_code":        getattr(log,'ba_code','N/A'),
                "logged_by":      getattr(log,'logged_by','N/A'),
                "notes":          getattr(log,'notes',''),
                "xw_snap":        getattr(log,'xw_snap','N/A'),
                "xw_limit":       getattr(log,'xw_limit','N/A'),
                "metar_history":  getattr(log,'metar_history',None),
                "hidden_sections": sorted(get_hidden(log)),
                "ev_score":       ev_score,
                "ev_max":         9,
                "time":           ts_time,
                "timestamp_full": ts_full,
                # SI Classification
                "si_cause":          getattr(log,'si_cause',None) or '',
                "si_cause_label":    getattr(log,'si_cause_label',None) or '',
                "si_problem_airport":getattr(log,'si_problem_airport',None) or '',
                "si_airport_focus":  getattr(log,'si_airport_focus',None) or '',
                "si_section_priority": json.loads(getattr(log,'si_section_priority',None) or '{}'),
                # Living Dossier Lifecycle
                "dossier_status":    getattr(log,'dossier_status',None) or 'CLOSED',
                "close_time":        getattr(log,'close_time',None).strftime('%H:%MZ') if getattr(log,'close_time',None) else None,
                "closed_at":         getattr(log,'closed_at',None).strftime('%H:%MZ') if getattr(log,'closed_at',None) else None,
                "metar_evolution":   json.loads(getattr(log,'metar_evolution',None) or '[]'),
                "taf_vs_actual":     json.loads(getattr(log,'taf_vs_actual',None) or '[]'),
                "station_picture":   json.loads(getattr(log,'station_picture',None) or '{}') if getattr(log,'station_picture',None) else None,
                "auto_summary":      getattr(log,'auto_summary',None) or '',
            })
        except Exception as e:
            print(f"get_dossiers: skipping log {getattr(log,'id','?')}: {e}")
            continue
    return jsonify(res)

@app.route('/api/run_migration')
@login_required
def run_migration():
    """Admin: force-add any missing DisruptionLog columns. Safe to run multiple times."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin only'}), 403
    results = {}
    cols = [
        ('acars_snap',       'TEXT'),
        ('logged_by',        'VARCHAR(50)'),
        ('notes',            'TEXT'),
        ('case_ref',         'VARCHAR(20)'),
        ('tail_snap',        'VARCHAR(20)'),
        ('xw_snap',          'VARCHAR(20)'),
        ('xw_limit',         'VARCHAR(20)'),
        ('metar_history',    'TEXT'),
        ('hidden_sections',  'TEXT'),
        ('section_audit',    'TEXT'),
        ('si_cause',          'VARCHAR(30)'),
        ('si_cause_label',    'VARCHAR(80)'),
        ('si_problem_airport','VARCHAR(10)'),
        ('si_airport_focus',  'VARCHAR(15)'),
        ('si_section_priority','TEXT'),
        ('dossier_status',    "VARCHAR(15) DEFAULT 'ACTIVE'"),
        ('close_time',        'TIMESTAMP WITHOUT TIME ZONE'),
        ('closed_at',         'TIMESTAMP WITHOUT TIME ZONE'),
        ('metar_evolution',   'TEXT'),
        ('taf_vs_actual',     'TEXT'),
        ('station_picture',   'TEXT'),
        ('auto_summary',      'TEXT'),
    ]
    for col, typ in cols:
        try:
            with db.engine.begin() as conn:
                conn.execute(db.text(
                    f'ALTER TABLE disruption_log ADD COLUMN IF NOT EXISTS {col} {typ}'
                ))
            results[f'disruption_log.{col}'] = 'OK'
        except Exception as e:
            results[f'disruption_log.{col}'] = str(e)

    # slot_log columns
    slot_cols = [
        ('sent_count',  'INTEGER DEFAULT 1'),
        ('resolved_by', 'VARCHAR(50)'),
        ('resolved_at', 'TIMESTAMP WITHOUT TIME ZONE'),
    ]
    for col, typ in slot_cols:
        try:
            with db.engine.begin() as conn:
                conn.execute(db.text(
                    f'ALTER TABLE slot_log ADD COLUMN IF NOT EXISTS {col} {typ}'
                ))
            results[f'slot_log.{col}'] = 'OK'
        except Exception as e:
            results[f'slot_log.{col}'] = str(e)

    return jsonify({'migration': results})

@app.route('/api/debug_dossiers')
@login_required
def debug_dossiers():
    """Admin debug — shows raw dossier count and any column errors."""
    try:
        logs = DisruptionLog.query.all()
        # Try accessing each new column to surface migration errors
        cols_ok = {}
        for col in ['metar_history','hidden_sections','section_audit',
                    'taf_snap','acars_snap','logged_by','notes',
                    'case_ref','tail_snap','xw_snap','xw_limit',
                    'si_cause','si_cause_label','si_problem_airport',
                    'si_airport_focus','si_section_priority',
                    'dossier_status','close_time','closed_at',
                    'metar_evolution','taf_vs_actual','station_picture','auto_summary']:
            try:
                _ = getattr(logs[0], col, 'NO_ROWS') if logs else 'NO_ROWS'
                cols_ok[col] = 'OK'
            except Exception as ce:
                cols_ok[col] = str(ce)
        return jsonify({
            'total_logs': len(logs),
            'columns': cols_ok,
            'sample_id': logs[0].id if logs else None,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/log_disruption', methods=['POST'])
@login_required
def log_disruption_manual():
    """Manual cancellation/disruption logging by controller."""
    try:
        data = request.json or {}
        flt      = str(data.get('flight','')).strip().upper()
        e_type   = str(data.get('event','CANCEL')).strip().upper()
        origin   = str(data.get('origin','')).strip().upper()
        dest     = str(data.get('dest','')).strip().upper()
        ba_code  = str(data.get('ba_code','')).strip().upper()
        notes_in = str(data.get('notes','')).strip()
        if not flt: return jsonify({"error":"flight required"}), 400

        now_utc = datetime.now(timezone.utc)
        log_id  = f"{flt}_{now_utc.strftime('%Y-%m-%d')}_{e_type}_M"

        # If a dossier for this flight+event already exists today, return it
        existing = db.session.get(DisruptionLog, log_id)
        if existing:
            return jsonify({"ok": True, "case_ref": existing.case_ref or log_id,
                            "id": log_id, "existing": True})

        # Resolve actual_dest_in early — needed for airport capture logic
        actual_dest_in = str(data.get("actual_dest", "")).strip().upper() or dest

        # Capture BOTH airports — DEP and ARR
        wx_snap = t_snap = n_snap = "N/A"
        _wx_airports = [a for a in [actual_dest_in or dest, dest, origin] if a and a != "N/A"]
        _wx_airports = list(dict.fromkeys(_wx_airports))  # deduplicate preserving order
        _wx_snaps = []; _t_snaps = []; _n_snaps = []
        for _apt in _wx_airports:
            try:
                if _apt in raw_weather_cache:
                    _m = raw_weather_cache[_apt].get('m')
                    _t = raw_weather_cache[_apt].get('t')
                    if _m: _wx_snaps.append(f"[{_apt}] " + (getattr(_m, 'raw', '') or ''))
                    if _t: _t_snaps.append(f"[{_apt}] " + (getattr(_t, 'raw', '') or ''))
                if _apt in raw_notam_cache and raw_notam_cache[_apt]:
                    _n_snaps.append(f"=== {_apt} NOTAMs ===\n" + "\n\n".join(raw_notam_cache[_apt]))
            except Exception: pass
        wx_snap = "\n".join(_wx_snaps) if _wx_snaps else "N/A"
        t_snap  = "\n".join(_t_snaps)  if _t_snaps  else "N/A"
        n_snap  = "\n\n".join(_n_snaps) if _n_snaps  else "N/A"
        target  = _wx_airports[0] if _wx_airports else (dest or origin)

        try:
            cutoff = now_utc - timedelta(hours=6)
            acars_msgs = AcarsLog.query.filter(
                AcarsLog.flight==flt, AcarsLog.timestamp>=cutoff
            ).order_by(AcarsLog.timestamp.asc()).all()
            a_snap = "\n".join([f"{m.timestamp.strftime('%H:%MZ')} {m.reg}: {m.message}"
                                  for m in acars_msgs]) if acars_msgs else "N/A"
        except Exception:
            a_snap = "N/A"

        try:
            last = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).first()
            last_num = int(last.case_ref.split('-')[-1]) if last and last.case_ref else 0
        except Exception:
            last_num = 0
        c_ref = f"CF-{last_num+1:03d}"

        xw_val = xw_lim = "N/A"
        try:
            # Get current crosswind at target airport
            if target in network_data:
                xw_val = str(network_data[target].get('cur_xw', 'N/A'))
            # Fallback: calculate from METAR if network_data doesn't have it
            if (xw_val == "N/A" or xw_val == "0") and target in raw_weather_cache:
                _m = raw_weather_cache[target].get('m')
                if _m and hasattr(_m, 'data') and _m.data:
                    try:
                        _w_dir = get_safe_num(getattr(_m.data.wind_direction, 'value', None))
                        _w_spd = get_safe_num(getattr(_m.data.wind_speed, 'value', None))
                        _w_gst = get_safe_num(getattr(_m.data.wind_gust, 'value', None)) or 0
                        _rwy_hdg = ops.get(target, {}).get('rwy', None)
                        _is_ow = ops.get(target, {}).get('one_way', False)
                        if _w_dir is not None and _w_spd is not None and _rwy_hdg:
                            _calc_xw, _, _ = calculate_winds(_w_dir, max(_w_spd, _w_gst), _rwy_hdg, _is_ow)
                            xw_val = str(_calc_xw)
                    except Exception: pass
            # Determine operative XW limit using runway condition + ac type
            _apt_base_lim = ops.get(target, {}).get('xw_lim', None)
            _wx_codes = []
            _temp_c = None
            if target in raw_weather_cache:
                _m = raw_weather_cache[target].get('m')
                if _m and hasattr(_m, 'data') and _m.data:
                    try:
                        _wx_codes = [wx.repr for wx in (_m.data.wx_codes or [])]
                        if _m.data.temperature:
                            _temp_c = get_safe_num(_m.data.temperature.value)
                    except Exception: pass
            # Look up scheduled aircraft type from schedule
            _ac_type_str = "A320"  # default
            if not flight_schedule_df.empty:
                try:
                    _flt_match = flight_schedule_df[
                        flight_schedule_df['FLT'].astype(str).str.upper() == flt
                    ]
                    if not _flt_match.empty:
                        _ac_type_str = str(_flt_match.iloc[0].get('AC_TYPE', 'A320'))
                except Exception: pass
            _op_lim, _ac_lim, _cond_key, _cond_label, _binding, _ac_key = \
                get_operative_xw_limit(_ac_type_str, _wx_codes, _apt_base_lim, _temp_c)
            xw_lim = (f"{_op_lim}kt ({_ac_key} / {_cond_label} / "
                      f"binding: {_binding})"
                      if _apt_base_lim else f"{_op_lim}kt ({_ac_key} / {_cond_label})")
        except Exception: pass

        # SI classification from notes
        si_class = classify_si_line(notes_in or '', origin, dest, e_type)
        _m_target = si_class['problem_airport'] or target

        accum_hours = ACCUMULATION_DEFAULTS.get(e_type, 6)
        _close_time = now_utc + timedelta(hours=accum_hours)

        _init_evo = []
        try:
            if _m_target in raw_weather_cache:
                _me = raw_weather_cache[_m_target].get('m')
                if _me:
                    _init_evo.append({
                        'ts': now_utc.strftime('%Y-%m-%dT%H:%MZ'),
                        'icao': _m_target,
                        'raw_metar': getattr(_me, 'raw', '') or ''
                    })
        except Exception: pass

        entry = DisruptionLog(
            id=log_id, flight=flt, date=now_utc.strftime('%Y-%m-%d'), event_type=e_type,
            origin=origin, sched_dest=dest, actual_dest=actual_dest_in,
            weather_snap=wx_snap, taf_snap=t_snap, notam_snap=n_snap,
            acars_snap=a_snap, tail_snap="N/A", xw_snap=xw_val, xw_limit=xw_lim,
            ba_code=ba_code or None, notes=notes_in or None,
            case_ref=c_ref, logged_by=current_user.username,
            metar_history=None,
            si_cause=si_class['cause'],
            si_cause_label=si_class['cause_label'],
            si_problem_airport=si_class['problem_airport'],
            si_airport_focus=si_class['airport_focus'],
            si_section_priority=json.dumps(si_class['section_priority']),
            dossier_status='ACTIVE',
            close_time=_close_time,
            metar_evolution=json.dumps(_init_evo) if _init_evo else '[]',
        )
        db.session.add(entry)
        db.session.commit()

        # Fetch METAR history asynchronously — never blocks the POST response
        def _bg_metar_hist(lid, tgt):
            try:
                _icao = ops.get(tgt, {}).get('icao', tgt)
                _hist = fetch_metar_history(_icao, hours=12)
                if _hist:
                    with app.app_context():
                        _log = db.session.get(DisruptionLog, lid)
                        if _log:
                            _log.metar_history = _hist
                            db.session.commit()
            except Exception as _e:
                print(f'METAR history bg fetch failed: {_e}')
        if target:
            threading.Thread(target=_bg_metar_hist, args=(log_id, target), daemon=True).start()

        return jsonify({"ok": True, "case_ref": c_ref, "id": log_id})

    except Exception as e:
        import traceback
        print(f"log_disruption_manual error: {traceback.format_exc()}")
        try: db.session.rollback()
        except: pass
        return jsonify({"error": str(e)}), 500

# ── DOSSIER DELETE ──────────────────────────────────────────────────────
@app.route('/api/dossier/<path:log_id>', methods=['DELETE'])
@login_required
def delete_dossier(log_id):
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error": "not found"}), 404
    try:
        # Cascade delete evidence
        CaseEvidence.query.filter_by(log_id=log_id).delete()
        db.session.delete(log)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# ── DOSSIER LIFECYCLE CONTROL ──────────────────────────────────────────
@app.route('/api/dossier/<path:log_id>/close', methods=['POST'])
@login_required
def close_dossier_manual(log_id):
    """Manually close an ACTIVE dossier."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error": "not found"}), 404
    if getattr(log, 'dossier_status', 'CLOSED') != 'ACTIVE':
        return jsonify({"error": "Dossier already closed"}), 400
    try:
        now_utc = datetime.now(timezone.utc)
        _close_dossier(log, now_utc)
        db.session.commit()
        return jsonify({"ok": True, "status": "CLOSED",
                        "summary": getattr(log, 'auto_summary', '')})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/dossier/<path:log_id>/extend', methods=['POST'])
@login_required
def extend_dossier(log_id):
    """Extend an ACTIVE dossier's accumulation window."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error": "not found"}), 404
    if getattr(log, 'dossier_status', 'CLOSED') != 'ACTIVE':
        return jsonify({"error": "Dossier already closed — cannot extend"}), 400
    try:
        hours = int((request.json or {}).get('hours', 2))
        hours = max(1, min(hours, 12))  # clamp 1-12
        current_close = getattr(log, 'close_time', None) or datetime.now(timezone.utc)
        log.close_time = current_close + timedelta(hours=hours)
        db.session.commit()
        return jsonify({"ok": True, "new_close": log.close_time.strftime('%H:%MZ'),
                        "extended_by": hours})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/dossier/<path:log_id>/reclassify', methods=['POST'])
@login_required
def reclassify_dossier(log_id):
    """Manually override SI classification on a dossier."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error": "not found"}), 404
    try:
        data = request.json or {}
        new_cause = data.get('cause', '').upper().strip()
        new_airport = data.get('problem_airport', '').upper().strip()

        if new_cause:
            # Look up from SI_CAUSE_RULES
            found = False
            for _, cause_key, cause_label, default_focus in SI_CAUSE_RULES:
                if cause_key == new_cause:
                    log.si_cause = cause_key
                    log.si_cause_label = cause_label
                    log.si_airport_focus = default_focus
                    log.si_section_priority = json.dumps(
                        SI_SECTION_PRIORITY.get(cause_key, {'expand':[], 'suppress':[]}))
                    found = True
                    break
            if not found:
                return jsonify({"error": f"Unknown cause: {new_cause}"}), 400

        if new_airport:
            log.si_problem_airport = new_airport
            # Update focus based on airport role
            if new_airport == log.origin:
                log.si_airport_focus = 'DEPARTURE'
            elif new_airport == log.sched_dest:
                log.si_airport_focus = 'ARRIVAL'

        db.session.commit()
        return jsonify({"ok": True,
                        "cause": log.si_cause,
                        "cause_label": log.si_cause_label,
                        "problem_airport": log.si_problem_airport,
                        "airport_focus": log.si_airport_focus})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# ── CASE EVIDENCE ROUTES ────────────────────────────────────────────────
@app.route('/api/dossier/<path:log_id>/evidence', methods=['GET'])
@login_required
def get_case_evidence(log_id):
    items = CaseEvidence.query.filter_by(log_id=log_id).order_by(CaseEvidence.timestamp).all()
    return jsonify([{
        "id": e.id, "filename": e.filename, "content_type": e.content_type,
        "content_text": e.content_text, "added_by": e.added_by,
        "timestamp": e.timestamp.strftime('%d %b %Y %H:%M UTC') if e.timestamp else '',
        "has_file": bool(e.file_data),
    } for e in items])

@app.route('/api/dossier/<path:log_id>/evidence', methods=['POST'])
@login_required
def add_case_evidence(log_id):
    """Accept a dropped file (multipart) or pasted text (JSON)."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error": "case not found"}), 404
    if request.content_type and 'application/json' in request.content_type:
        data = request.json or {}
        ev = CaseEvidence(log_id=log_id,
            filename=data.get('filename', 'Pasted text'),
            content_type='text/plain',
            content_text=data.get('content_text', ''),
            added_by=current_user.username)
        db.session.add(ev); db.session.commit()
        return jsonify({"ok": True, "id": ev.id})
    f = request.files.get('file')
    if not f: return jsonify({"error": "no file"}), 400
    raw = f.read()
    content_type = f.content_type or 'application/octet-stream'
    filename = f.filename or 'attachment'
    content_text = ''
    try:
        if 'text' in content_type or filename.endswith(('.txt','.eml','.msg','.csv')):
            content_text = raw.decode('utf-8', errors='replace')
        else:
            content_text = f'[{content_type} — {len(raw)} bytes — download to view]'
    except Exception:
        content_text = f'[Binary file — {len(raw)} bytes]'
    ev = CaseEvidence(log_id=log_id, filename=filename, content_type=content_type,
        content_text=content_text[:50000],
        file_data=base64.b64encode(raw).decode(),
        added_by=current_user.username)
    db.session.add(ev); db.session.commit()
    return jsonify({"ok": True, "id": ev.id, "filename": filename,
                    "content_text": content_text[:500]})

@app.route('/api/dossier/<path:log_id>/evidence/<int:ev_id>', methods=['DELETE'])
@login_required
def delete_case_evidence(log_id, ev_id):
    ev = CaseEvidence.query.filter_by(id=ev_id, log_id=log_id).first()
    if not ev: return jsonify({"error": "not found"}), 404
    db.session.delete(ev); db.session.commit()
    return jsonify({"ok": True})

@app.route('/api/dossier/<path:log_id>/evidence/<int:ev_id>/download')
@login_required
def download_case_evidence(log_id, ev_id):
    ev = CaseEvidence.query.filter_by(id=ev_id, log_id=log_id).first()
    if not ev or not ev.file_data: return jsonify({"error": "not found"}), 404
    raw = base64.b64decode(ev.file_data)
    return Response(raw, mimetype=ev.content_type or 'application/octet-stream',
        headers={"Content-Disposition": f'attachment; filename="{ev.filename}"'})

@app.route('/api/dossier/<path:log_id>/notes', methods=['POST'])
@login_required
def update_dossier_notes(log_id):
    """Save controller notes to a dossier."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({"error":"not found"}), 404
    data = request.json or {}
    log.notes   = data.get('notes', log.notes)
    log.ba_code = data.get('ba_code', log.ba_code)
    try:
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/api/dossier/<path:log_id>/section_toggle', methods=['POST'])
@login_required
def section_toggle(log_id):
    """Hide or show a named section for this dossier.
    POST body: {section: 'notams', action: 'hide'|'show'}
    Records an audit entry with user + timestamp.
    """
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({'error': 'not found'}), 404

    data    = request.json or {}
    section = data.get('section', '').strip()
    action  = data.get('action', '').strip().lower()  # 'hide' or 'show'

    valid_keys = {k for k, _ in EU261_SECTIONS}
    if section not in valid_keys:
        return jsonify({'error': f'Unknown section: {section}'}), 400
    if action not in ('hide', 'show'):
        return jsonify({'error': 'action must be hide or show'}), 400

    hidden = get_hidden(log)
    if action == 'hide':
        hidden.add(section)
    else:
        hidden.discard(section)

    # Write audit entry
    audit = get_audit(log)
    audit.append({
        'section':   section,
        'action':    action,
        'user':      current_user.username,
        'timestamp': datetime.now(timezone.utc).strftime('%d %b %Y %H:%MZ'),
        'portal':    False
    })

    log.hidden_sections = json.dumps(sorted(hidden))
    log.section_audit   = json.dumps(audit)
    try:
        db.session.commit()
        return jsonify({'ok': True, 'hidden': sorted(hidden), 'audit': audit})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/dossier/<path:log_id>/sections')
@login_required
def get_sections(log_id):
    """Return current section visibility state + audit trail for a dossier."""
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({'error': 'not found'}), 404
    hidden = get_hidden(log)
    return jsonify({
        'hidden':  sorted(hidden),
        'audit':   get_audit(log),
        'sections': [
            {'key': k, 'label': lbl, 'hidden': k in hidden,
             'source': EVIDENCE_SOURCES.get(k, '')}
            for k, lbl in EU261_SECTIONS
        ]
    })


@app.route('/api/debug_notam')
@login_required
def debug_notam():
    """Admin-only: show parsed NOTAM time windows for a station. ?iata=GLA"""
    iata = request.args.get('iata', '').upper()
    if not iata: return jsonify({"error": "?iata= required"})
    notams = raw_notam_cache.get(iata, [])
    results = []
    for n_text in notams:
        n_upper = str(n_text).upper()
        n_clean = re.sub(r'[BC]\)\s*\d{10}', '', n_upper)
        d_start_n = d_end_n = None
        _tw_patterns = [
            r'D\)\s*(?:\S+\s+)?(\d{4})\s*[-\u2013]\s*(\d{4})',
            r'\b(\d{4})\s*[-\u2013]\s*(\d{4})\b',
            r'\b(\d{4})\s+TO\s+(\d{4})\b',
            r'BETWEEN\s+(\d{4})\s+AND\s+(\d{4})',
        ]
        matched_pat = None
        for _pat in _tw_patterns:
            _m = re.search(_pat, n_clean)
            if _m:
                _h1,_h2 = int(_m.group(1)[:2]), int(_m.group(2)[:2])
                _m1,_m2 = int(_m.group(1)[2:]), int(_m.group(2)[2:])
                if _h1 <= 23 and _h2 <= 23 and _m1 <= 59 and _m2 <= 59:
                    d_start_n = int(_m.group(1)); d_end_n = int(_m.group(2))
                    matched_pat = _pat; break
        results.append({"text": n_text[:200], "d_start": d_start_n, "d_end": d_end_n, "pattern": matched_pat})
    return jsonify({"iata": iata, "count": len(notams), "notams": results})

# ── OPENSKY POSITION OVERLAY ─────────────────────────────────────────────
def _get_opensky_token():
    """Returns True if credentials are configured — OpenSky uses HTTP Basic Auth,
    not OAuth2. The auth.opensky-network.org endpoint is not needed."""
    return bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET)

def _refresh_opensky_positions():
    """Poll OpenSky for all fleet ICAO24 hex codes and update opensky_pos_cache.
    Only fires if OPENSKY_REFRESH_SECS have elapsed since last successful poll."""
    global opensky_cache_time, opensky_pos_cache
    if time.time() - opensky_cache_time < OPENSKY_REFRESH_SECS:
        return  # not due yet
    if not ICAO24_TO_REG:
        return  # fleet_registry not loaded

    if not _get_opensky_token():
        return

    # Build ICAO24 param list (max ~100 per request — we have 45)
    hex_list = list(ICAO24_TO_REG.keys())
    params   = '&'.join(f'icao24={h}' for h in hex_list)
    url      = f'https://opensky-network.org/api/states/all?{params}'
    try:
        resp = requests.get(
            url,
            auth=(OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET),  # HTTP Basic Auth
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            new_cache = {}
            for sv in (data.get('states') or []):
                # OpenSky state vector indices:
                # 0=icao24, 1=callsign, 2=origin_country, 3=time_position,
                # 4=last_contact, 5=lon, 6=lat, 7=baro_alt(m), 8=on_ground,
                # 9=velocity(m/s), 10=heading, 11=vert_rate, 13=geo_alt(m)
                try:
                    icao24  = sv[0].lower()
                    lat     = sv[6]
                    lon     = sv[5]
                    baro_m  = sv[7]  # barometric altitude metres (None if on ground)
                    geo_m   = sv[13] if len(sv) > 13 else None
                    alt_m   = baro_m if baro_m is not None else (geo_m or 0)
                    alt_ft  = round(alt_m * 3.28084)
                    on_gnd  = bool(sv[8])
                    spd_ms  = sv[9] or 0   # m/s
                    spd_kts = round(spd_ms * 1.94384)
                    hdg     = sv[10] or 0
                    ts      = sv[4] or time.time()  # last_contact unix
                    sq      = str(sv[14]).strip() if len(sv) > 14 and sv[14] else None
                    if lat and lon:
                        new_cache[icao24] = {
                            'lat': lat, 'lon': lon,
                            'alt_ft': 0 if on_gnd else alt_ft,
                            'spd_kts': spd_kts,
                            'hdg': hdg,
                            'on_ground': on_gnd,
                            'last_seen': ts,
                            'squawk': sq,
                        }
                except (IndexError, TypeError):
                    continue
            opensky_pos_cache = new_cache
            opensky_cache_time = time.time()
            print(f'OpenSky refresh: {len(new_cache)} aircraft updated')
        elif resp.status_code == 401:
            # Token expired mid-session — force refresh next call
            opensky_token_cache['token'] = None
    except Exception as e:
        print(f'OpenSky poll error: {e}')

def _apply_opensky_overlay():
    """Overwrite stale AE positions in live_flights_memory with fresh OpenSky data.
    Only updates position/speed/heading — AE keeps flight identity, route, reg."""
    if not opensky_pos_cache or not ICAO24_TO_REG:
        return
    now = time.time()
    updated = 0
    for flt, mem in live_flights_memory.items():
        # Get the registration for this flight from AE data
        reg = str(mem['data'].get('aircraft', {}).get('regNumber') or '').upper().strip()
        if not reg:
            continue
        # Look up ICAO24 hex for this registration
        fleet_entry = FLEET.get(reg, {})
        icao24 = fleet_entry.get('icao24', '').lower() if fleet_entry else ''
        if not icao24:
            continue
        pos = opensky_pos_cache.get(icao24)
        if not pos:
            continue
        # Only apply if OpenSky data is fresher than 90 seconds
        if now - pos['last_seen'] > 90:
            continue
        # Overwrite geography in the AE data dict with OpenSky values
        geo = mem['data'].setdefault('geography', {})
        geo['latitude']  = pos['lat']
        geo['longitude'] = pos['lon']
        geo['altitude']  = pos['alt_ft']
        geo['direction'] = pos['hdg']
        # Overwrite speed
        mem['data'].setdefault('speed', {})['horizontal'] = pos['spd_kts'] / 0.539957  # back to km/h for AE compat
        # Keep last_seen fresh so ghost detection doesn't trigger on good OpenSky data
        mem['last_seen'] = now
        updated += 1
    if updated:
        print(f'OpenSky overlay: {updated} positions refreshed')

# ─────────────────────────────────────────────────────────────────────────

@app.route('/api/weather')
@login_required
def get_weather_data():
    global raw_weather_cache, wx_cache_time, raw_notam_cache, notam_cache_time, live_flights_memory, departure_times, arrival_times, aviation_edge_cache_time
    hz = int(request.args.get('horizon', 12))
    show_cf, show_ef, show_bw = request.args.get('cf') == 'true', request.args.get('ef') == 'true', request.args.get('baw') == 'true'
    now_utc = datetime.now(timezone.utc)
    today_date = now_utc.date()
    
    # MASTER CACHE: Only hit Aviation Edge max once every 30 seconds, protecting API limits.
    if AVIATION_EDGE_KEY and (time.time() - aviation_edge_cache_time > 15 or request.args.get('force') == 'true'):
        for code in ACTIVE_CONFIG["tracked_icaos"]:
            try:
                resp = requests.get(f"https://aviation-edge.com/v2/public/flights?key={AVIATION_EDGE_KEY}&airlineIcao={code}", timeout=10)
                if resp.status_code == 200 and isinstance(resp.json(), list):
                    for f in resp.json():
                        flt = str(f.get('flight', {}).get('iataNumber') or '')
                        icao = str(f.get('flight', {}).get('icaoNumber') or '').upper()
                        arr = str(f.get('arrival', {}).get('iataCode') or '').upper()
                        dep = str(f.get('departure', {}).get('iataCode') or '').upper()
                        ac_type = str(f.get('aircraft', {}).get('icaoCode') or '').upper()
                        
                        group = ACTIVE_CONFIG["grouper"](f, icao, arr, dep, ac_type)
                        if group != "UNK" and flt and not flt.startswith('XX'):
                            live_flights_memory[flt] = {'data': f, 'last_seen': time.time()}
                            speed_kts = get_safe_num(f.get('speed', {}).get('horizontal', f.get('geography', {}).get('speed', 0))) * 0.539957
                            alt_ft = get_safe_num(f.get('geography', {}).get('altitude', 0))
                            
                            if speed_kts > 50 and alt_ft > 500:
                                if flt not in departure_times: departure_times[flt] = time.time()
                                
                            if speed_kts <= 50:
                                p_lat = f.get('geography', {}).get('latitude', 0)
                                p_lon = f.get('geography', {}).get('longitude', 0)
                                if arr in base_airports and p_lat and p_lon:
                                    dist_nm = calculate_dist(p_lat, p_lon, base_airports[arr]['lat'], base_airports[arr]['lon'])
                                    if dist_nm < 5:
                                        if flt not in arrival_times: arrival_times[flt] = time.time()
            except: pass
        aviation_edge_cache_time = time.time()
        gc.collect() 

    # ── AE TIMETABLE (scheduler runs independently — see startup thread) ──
    # Timetable is refreshed by a daemon thread started at app startup.
    # Nothing timetable-related runs here to keep weather route fast.
    # ────────────────────────────────────────────────────────────────────

    # ── OPENSKY: runs in background scheduler thread, never here ────────
    # Apply any positions already fetched by the background thread
    _apply_opensky_overlay()
    # ────────────────────────────────────────────────────────────────────

    active_iatas = set()
    dynamic_fleets = {}

    if not flight_schedule_df.empty:
        try:
            w_df = flight_schedule_df
            if 'DATE_OBJ' in w_df.columns:
                t_m = w_df[w_df['DATE_OBJ'] == today_date]
                if not t_m.empty: w_df = t_m
                
            for _, row in w_df.iterrows():
                arr_str = str(row.get('ARR', '')).strip().upper()
                dep_str = str(row.get('DEP', '')).strip().upper()
                ac_type = str(row.get('AC_TYPE', '')).strip().upper()
                flt_str = str(row.get('FLT', '')).strip()
                if arr_str == dep_str: continue 
                
                f_group = "Main"
                if any(x in ac_type for x in ["E90", "E75", "E19", "EMB"]): f_group = "Cityflyer"
                elif any(x in ac_type for x in ["320", "321", "31E", "32E", "31", "32"]): f_group = "Euroflyer"
                else:
                    if flt_str.startswith('BA8') or flt_str.startswith('BA4') or flt_str.startswith('BA7') or flt_str.startswith('BA3') or flt_str.startswith('BA9'): f_group = "Cityflyer"
                    elif flt_str.startswith('BA2'): f_group = "Euroflyer"
                    
                if CLIENT_ENV == "BACF":
                    if f_group == "Cityflyer" and not show_cf: continue
                    if f_group == "Euroflyer" and not show_ef: continue
                    if f_group == "Main" and not show_bw: continue
                    
                for apt in [arr_str, dep_str]:
                    if len(apt) == 3 and apt.isalpha():
                        active_iatas.add(apt)
                        if apt not in dynamic_fleets: dynamic_fleets[apt] = set()
                        dynamic_fleets[apt].add(f_group)
        except: pass

    for flt, mem in list(live_flights_memory.items()):
        if time.time() - mem['last_seen'] > 300: del live_flights_memory[flt]; continue
        f = mem['data']
        
        icao = str(f.get('flight', {}).get('icaoNumber') or '').upper()
        arr = str(f.get('arrival', {}).get('iataCode') or '').upper()
        dep = str(f.get('departure', {}).get('iataCode') or '').upper()
        ac_type = str(f.get('aircraft', {}).get('icaoCode') or '').upper()
        
        group_code = ACTIVE_CONFIG["grouper"](f, icao, arr, dep, ac_type)
        
        f_group = "Both"
        if group_code == "CFE": f_group = "Cityflyer"
        elif group_code == "EFW": f_group = "Euroflyer"
        elif group_code == "BAW": f_group = "Main"
        
        if CLIENT_ENV == "BACF":
            if (group_code == "CFE" and not show_cf) or (group_code == "EFW" and not show_ef) or (group_code == "BAW" and not show_bw): continue
            
        if group_code != "UNK":
            for apt in [arr, dep]:
                if len(apt) == 3:
                    active_iatas.add(apt)
                    if apt not in dynamic_fleets: dynamic_fleets[apt] = set()
                    dynamic_fleets[apt].add(f_group)

    ops = {}
    for iata in active_iatas:
        base_info = base_airports.get(iata)
        if base_info: ops[iata] = base_info
        elif len(iata) == 3 and iata.isalpha():
            try:
                st = Station.from_iata(iata)
                if st and st.icao:
                    r_hdg, rwys_str = 360, "N/A"
                    if hasattr(st, 'runways') and st.runways:
                        try:
                            rwy_names = [f"{r.ident1}/{r.ident2}" for r in st.runways if r.ident1 and r.ident2]
                            if rwy_names: rwys_str = ", ".join(rwy_names)
                            clean_r1 = "".join([c for c in st.runways[0].ident1 if c.isdigit()])
                            if clean_r1: r_hdg = int(clean_r1) * 10
                        except: pass
                    fleet_val = "Both"
                    if iata in dynamic_fleets and len(dynamic_fleets[iata]) == 1: fleet_val = list(dynamic_fleets[iata])[0]
                    ops[iata] = {"icao": st.icao, "name": st.name.split(" ")[0], "lat": st.latitude, "lon": st.longitude, "rwy": r_hdg, "rwys": rwys_str, "fleet": fleet_val, "spec": False, "one_way": False}
            except: pass

    if time.time() - wx_cache_time > 900 or request.args.get('force') == 'true':
        raw_weather_cache = {}; wx_cache_time = time.time()
    if time.time() - notam_cache_time > 3600 or request.args.get('force') == 'true':
        raw_notam_cache = {}; notam_cache_time = time.time()

    # Parallel fetch — ops airports + key diversion alternates
    # Hardcoded list keeps pool size predictable (4 extra airports only)
    _fetch_pool = {**ops, **{k: v for k, v in DIVERT_ALT_WX.items() if k not in ops}}
    airports_needing_fetch = {
        iata: v for iata, v in _fetch_pool.items()
        if iata not in raw_weather_cache or iata not in raw_notam_cache
    }
    if airports_needing_fetch:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(_fetch_airport_wx, iata, v['icao']): iata
                for iata, v in airports_needing_fetch.items()
            }
            for future in as_completed(futures, timeout=30):
                try:
                    iata, result = future.result(timeout=15)
                    if 'wx' in result and iata not in raw_weather_cache:
                        raw_weather_cache[iata] = result['wx']
                    if 'notam' in result and iata not in raw_notam_cache:
                        raw_notam_cache[iata] = result['notam']
                except Exception:
                    pass

    gc.collect()

    network_data = {}
    valid_cld = ['BKN', 'OVC', 'VV']

    for iata, info in ops.items():
        obj = raw_weather_cache.get(iata, {})
        m, t = obj.get('m'), obj.get('t')
        
        v_lim = 1500 if info.get('spec') else 800
        limit_xw = info.get('xw_lim', 25)
        limit_tw = info.get('tw_lim', 15)
        is_one_way = info.get('one_way', False)
        
        m_iss, a_issues, f_iss, f_a_iss = [], [], [], []
        cur_xw, cur_tw = 0, 0

        if not m or not hasattr(m, 'data') or not m.data: m_iss.append("WX PENDING/OFFLINE")
        if m and hasattr(m, 'data') and m.data:
            vis = get_safe_num(m.data.visibility.value if m.data.visibility else None, 9999)
            if vis < v_lim: m_iss.append(f"VIS {int(vis)}m")
            
            if hasattr(m.data, 'runway_visibility') and m.data.runway_visibility:
                for rvr in m.data.runway_visibility:
                    try:
                        r_val = get_safe_num(rvr.visibility.value if getattr(rvr, 'visibility', None) else None, 9999)
                        r_rwy = getattr(rvr, 'runway', 'UNK')
                        if r_val < v_lim: m_iss.append(f"RVR RWY{r_rwy} {int(r_val)}m")
                    except: pass
            
            cld = [(get_safe_num(getattr(c, 'base', 0)) * 100) if getattr(c, 'base', None) is not None else 0 for c in m.data.clouds if getattr(c, 'type', '') in valid_cld]
            if cld:
                min_cld = min(cld)
                if iata == "LCY":
                    if min_cld <= 200: m_iss.append(f"CIG {int(min_cld)}ft")
                    elif min_cld <= 400: a_issues.append(f"CIG {int(min_cld)}ft")
                elif info.get('spec'):
                    if min_cld < 500: m_iss.append(f"CIG {int(min_cld)}ft")
                    elif min_cld < 1000: a_issues.append(f"CIG {int(min_cld)}ft")
                else: 
                    if min_cld < 200: m_iss.append(f"CIG {int(min_cld)}ft")
                    elif min_cld < 500: a_issues.append(f"CIG {int(min_cld)}ft")
                
            if hasattr(m.data, 'wx_codes') and m.data.wx_codes:
                for wx in m.data.wx_codes:
                    code = wx.repr
                    is_light_precip = code.startswith('-')
                    # TS/FG/FZ/GR/SQ/FC/DS always critical regardless of intensity
                    # SN/RASN/PL/SH — light (-) = amber advisory, moderate/heavy = critical
                    _always_crit = any(k in code for k in ['TS', 'FG', 'FZ', 'GR', 'SQ', 'FC', 'DS'])
                    _precip_wx   = any(k in code for k in ['SN', 'RASN', 'PL', 'IC', 'GS'])
                    if _always_crit:
                        m_iss.append(code)
                    elif _precip_wx:
                        if is_light_precip: a_issues.append(code)  # -SN, -RASN = amber
                        else: m_iss.append(code)                    # SN, RASN = red
            
            w_dir = get_safe_num(m.data.wind_direction.value if m.data.wind_direction else None)
            w_spd = get_safe_num(m.data.wind_speed.value if m.data.wind_speed else None)
            w_gst = get_safe_num(m.data.wind_gust.value if m.data.wind_gust else None)
            cur_xw, cur_tw, cur_dep_tw = calculate_winds(w_dir, max(w_spd, w_gst), info['rwy'], is_one_way)
            
            if cur_xw >= limit_xw: m_iss.append(f"XW {cur_xw}kt")
            elif cur_xw >= limit_xw - 5: a_issues.append(f"XW {cur_xw}kt (APPROACHING LIMIT)")
            if is_one_way and cur_tw >= limit_tw: m_iss.append(f"TW {cur_tw}kt (ARR)")
            if is_one_way and cur_dep_tw >= limit_tw: m_iss.append(f"TW {cur_dep_tw}kt (DEP)")
            elif is_one_way and cur_dep_tw >= limit_tw - 3: a_issues.append(f"TW {cur_dep_tw}kt DEP (APPROACHING)")

        if t and hasattr(t, 'data') and t.data and hasattr(t.data, 'forecast'):
            for line in t.data.forecast:
                try:
                    if getattr(line, 'probability', None) and line.probability.value == 30: continue
                    start = line.start_time.dt.replace(tzinfo=timezone.utc)
                    end = line.end_time.dt.replace(tzinfo=timezone.utc)
                    if end <= now_utc: continue
                    if (start - now_utc).total_seconds() / 3600 > hz: continue

                    t_type = getattr(line, 'type', '') or ''
                    prob = getattr(line, 'probability', None)
                    prob_str = f"PROB{prob.value} " if prob else ""
                    s_hr, e_hr = start.strftime('%H'), end.strftime('%H')

                    is_tomorrow = start.date() > now_utc.date()
                    tmrw_tag = "TOMORROW " if is_tomorrow else ""
                    trend_prefix = " ".join(f"{prob_str}{t_type} {tmrw_tag}{s_hr}-{e_hr}Z ".split()) + " " if (t_type or prob_str) else f"FM {tmrw_tag}{s_hr}-{e_hr}Z "

                    # PROB40 or TEMPO = amber (uncertain/temporary); FM/BECMG = red (expected)
                    is_amber = t_type == 'TEMPO' or (prob and prob.value >= 40)
                    bucket = f_a_iss if is_amber else f_iss

                    if hasattr(line, 'wx_codes') and line.wx_codes:
                        for wx in line.wx_codes:
                            code = wx.repr
                            is_lt = code.startswith('-')
                            _always_crit = any(k in code for k in ['TS', 'FG', 'FZ', 'GR', 'SQ', 'FC', 'DS'])
                            _precip_wx   = any(k in code for k in ['SN', 'RASN', 'PL', 'IC', 'GS'])
                            if _always_crit:
                                bucket.append(f"{trend_prefix}{code}")
                            elif _precip_wx:
                                # Light precip always amber; moderate+ follows the TAF line bucket
                                if is_lt: f_a_iss.append(f"{trend_prefix}{code}")
                                else: bucket.append(f"{trend_prefix}{code}")

                    f_cld = [(get_safe_num(getattr(c, 'base', 0)) * 100) if getattr(c, 'base', None) is not None else 0 for c in line.clouds if getattr(c, 'type', '') in valid_cld]
                    if f_cld:
                        min_cld = min(f_cld)
                        if iata == "LCY":
                            if min_cld <= 400: bucket.append(f"{trend_prefix}CIG {int(min_cld)}ft")
                        elif info.get('spec'):
                            if min_cld < 1000: bucket.append(f"{trend_prefix}CIG {int(min_cld)}ft")
                        else:
                            if min_cld < 500: bucket.append(f"{trend_prefix}CIG {int(min_cld)}ft")

                    f_vis = get_safe_num(line.visibility.value if getattr(line, 'visibility', None) else None, 9999)
                    if f_vis < v_lim: bucket.append(f"{trend_prefix}VIS {int(f_vis)}m")

                    fw_dir = get_safe_num(line.wind_direction.value if getattr(line, 'wind_direction', None) else None)
                    fw_spd = get_safe_num(line.wind_speed.value if getattr(line, 'wind_speed', None) else None)
                    fw_gst = get_safe_num(line.wind_gust.value if getattr(line, 'wind_gust', None) else None)
                    fw_xw, fw_tw, fw_dep_tw = calculate_winds(fw_dir, max(fw_spd, fw_gst), info['rwy'], is_one_way)

                    if fw_xw >= limit_xw: bucket.append(f"{trend_prefix}XW {fw_xw}kt")
                    elif fw_xw >= limit_xw - 5: bucket.append(f"{trend_prefix}XW {fw_xw}kt (APPROACHING LIMIT)")
                    if is_one_way and fw_tw >= limit_tw: bucket.append(f"{trend_prefix}TW {fw_tw}kt (ARR)")
                    if is_one_way and fw_dep_tw >= limit_tw: bucket.append(f"{trend_prefix}TW {fw_dep_tw}kt (DEP)")
                    elif is_one_way and fw_dep_tw >= limit_tw - 3: bucket.append(f"{trend_prefix}TW {fw_dep_tw}kt DEP (APPROACHING)")
                except: pass

        all_notams = raw_notam_cache.get(iata, [])
        crit_n, adv_n, future_n = [], [], []
        crit_n_windows = []  # [(b_dt_or_None, c_dt_or_None), ...] parallel to crit_n

        for n_text in all_notams:
            n_upper = str(n_text).upper()
            is_active_hazard = True
            b_dt_n = c_dt_n = None
            d_start_n = d_end_n = None  # Daily schedule HHMM ints
            try:
                b_match = re.search(r'B\)\s*(\d{10})', n_upper)
                if b_match:
                    b_dt_n = datetime.strptime(b_match.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
                    if b_dt_n > now_utc + timedelta(hours=hz): is_active_hazard = False
                c_match = re.search(r'C\)\s*(\d{10})', n_upper)
                if c_match:
                    c_dt_n = datetime.strptime(c_match.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
                    if c_dt_n < now_utc: is_active_hazard = False
                # Extract daily time window (D) schedule or E) text) for per-flight ETA check
                # Strip B)/C) 10-digit timestamps so they don't confuse HHMM matching
                n_clean = re.sub(r'[BC]\)\s*\d{10}', '', n_upper)
                _tw_patterns = [
                    r'D\)\s*(?:\S+\s+)?(\d{4})\s*[-\u2013]\s*(\d{4})',  # D) 2300-0600 or D) MON-SUN 2300-0600
                    r'\b(\d{4})\s*[-\u2013]\s*(\d{4})\b',                  # standalone HHMM-HHMM
                    r'\b(\d{4})\s+TO\s+(\d{4})\b',                           # 2300 TO 0600
                    r'BETWEEN\s+(\d{4})\s+AND\s+(\d{4})',                     # BETWEEN 2300 AND 0600
                ]
                for _pat in _tw_patterns:
                    _m = re.search(_pat, n_clean)
                    if _m:
                        _h1,_h2 = int(_m.group(1)[:2]), int(_m.group(2)[:2])
                        _m1,_m2 = int(_m.group(1)[2:]), int(_m.group(2)[2:])
                        if _h1 <= 23 and _h2 <= 23 and _m1 <= 59 and _m2 <= 59:
                            d_start_n = int(_m.group(1))
                            d_end_n   = int(_m.group(2))
                            break
            except: pass

            # PPR: only flag if specifically about runway use (landing/takeoff), not parking/stands/apron
            is_ppr = (
                bool(re.search(r'\bPPR\b|PRIOR PERMISSION REQUIRED', n_upper)) and
                bool(re.search(r'\bRWY\b|\bRUNWAY\b', n_upper)) and
                not bool(re.search(r'\bPARK\b|\bSTAND\b|\bAPRON\b|\bGATE\b|\bHANGAR\b|\bWINGSPAN\b|\bWINGS\b|\bACFT\s+SIZE\b', n_upper))
            )

            # Critical NOTAM keyword sets
            _crit_keys = [
                'RWY CLSD', 'AD CLSD', 'ILS U/S', 'SNOWTAM',
                # LDG/TKOF forbidden = AD effectively closed
                'LDG, TKOF', 'TKOF, LDG', 'LDG AND TKOF', 'TKOF AND LDG',
                'LANDING AND TAKEOFF FORBIDDEN', 'TAKEOFF AND LANDING FORBIDDEN',
                'LDG FORBIDDEN', 'TKOF FORBIDDEN', 'LDG/TKOF FORBIDDEN',
                'LDG, TKOF AND TAX FORBIDDEN',
                # Explicit closure language
                'AD NOT AVBL', 'AERODROME NOT AVAILABLE',
                'AD OPS LTD', 'AD OPERATIONS LIMITED',
                'INDUSTRIAL ACTION', 'ATC STRIKE', 'STRIKE ACTION',
            ]
            _adv_keys = ['TWY CLSD', 'WIP', 'OBST', 'STAND CLSD', 'APRON CLSD']

            if is_active_hazard:
                if any(k in n_upper for k in _crit_keys) or is_ppr:
                    crit_n.append(n_text)
                    crit_n_windows.append((b_dt_n, c_dt_n, d_start_n, d_end_n))
                elif any(k in n_upper for k in _adv_keys): adv_n.append(n_text)
            else:
                if any(k in n_upper for k in _crit_keys) or is_ppr: future_n.append(n_text)
                elif any(k in n_upper for k in _adv_keys): pass  # future advisory — not shown separately

        # Only treat critical NOTAMs as red if their daily window is currently active
        def _notam_window_active_now(win_entry):
            b, c, ds, de = (win_entry + (None, None, None, None))[:4]
            if ds is None or de is None:
                return True  # No daily window = always active when in B/C range
            now_hhmm = now_utc.hour * 100 + now_utc.minute
            if de < ds:  # Overnight e.g. 2300-0600
                return now_hhmm >= ds or now_hhmm <= de
            return ds <= now_hhmm <= de

        crit_n_active_now = [n for n, w in zip(crit_n, crit_n_windows) if _notam_window_active_now(w)]
        sev = 3 if (crit_n_active_now or [i for i in m_iss if i != "WX PENDING/OFFLINE"]) else (2 if a_issues else (1 if (f_iss or f_a_iss) else 0))
        handler, phone, email = get_station_contact(iata)
        restrictions = parse_notam_restrictions(all_notams, now_utc)
        # Merge AIP ops hours — skip if NOTAMs already cover same type
        notam_types = {r['type'] for r in restrictions if r.get('source') != 'AIP'}
        for ae in get_ops_restrictions(iata, now_utc):
            if ae['type'] == 'CLOSURE'   and 'CLOSURE'   in notam_types: continue
            if ae['type'] == 'NIGHT_JET' and 'NIGHT_JET' in notam_types: continue
            restrictions.append(ae)
        _sort = {"CLOSURE": 0, "RWY_CLOSURE": 1, "CURFEW": 2, "NIGHT_JET": 3}
        restrictions.sort(key=lambda x: (0 if x['active'] else 1, _sort.get(x['type'], 9)))

        # Airport physically closed right now (ops hours or CLOSURE NOTAM) — used to exclude from alternates
        is_closed = any(r.get('active') and r['type'] == 'CLOSURE' for r in restrictions)

        network_data[iata] = {
            "name": info['name'], "lat": info['lat'], "lon": info['lon'], "color": "#d6001a" if sev == 3 else ("#eb8f34" if sev > 0 else ("#808080" if m_iss else "#008000")),
            "issues": m_iss, "a_issues": a_issues, "f_issues": f_iss, 
            "f_issues_short": list(dict.fromkeys([f.split('Z ')[-1] if 'Z ' in f else f for f in f_iss])),
            "f_a_issues": f_a_iss, 
            "critical_notams": crit_n, "critical_notam_windows": crit_n_windows, "advisory_notams": adv_n, "future_notams": future_n, "all_notams": all_notams,
            "cur_xw": cur_xw, "cur_tw": cur_tw, "rwy": info['rwy'], "rwys": info.get('rwys', 'N/A'),
            "curfew": info.get('curfew'),
            "handler": handler, "phone": phone, "email": email, "fleet": info.get('fleet', 'Other'),
            "raw_m": m.raw if m else "N/A", "raw_t": t.raw if t else "N/A",
            "severity": sev, "is_closed": is_closed, "hazard_count": len(m_iss) + len(a_issues) + len(f_iss) + len(crit_n),
            "restrictions": restrictions,
            "inbounds": [], "alternates": [], "departures": []
        }

    # --- POPULATE DIVERSION ALTERNATES (after all severities known) ---
    all_known_coords = {**COMMON_ALT_AIRPORTS}
    for k, v in ops.items(): all_known_coords[k] = v

    for iata, nd_info in network_data.items():
        candidates = DIVERSION_CANDIDATES.get(iata, [])
        if not candidates:
            nearby = [(calculate_dist(nd_info['lat'], nd_info['lon'], v['lat'], v['lon']), k)
                      for k, v in ops.items() if k != iata]
            nearby.sort(); candidates = [x[1] for x in nearby[:6]]

        alts = []
        for cand in candidates:
            cand_coords = all_known_coords.get(cand)
            if not cand_coords: continue
            dist = round(calculate_dist(nd_info['lat'], nd_info['lon'], cand_coords['lat'], cand_coords['lon']))
            if cand in network_data:
                cnd = network_data[cand]
                sev_c = cnd['severity']
                color = cnd['color']
                is_cand_closed = cnd.get('is_closed', False)
                active = cnd['issues'] + cnd['a_issues']
                fcst   = cnd.get('f_issues_short', [])
                wx_status = ("ACT: " + ", ".join(active[:2])) if active else (("FCST: " + ", ".join(fcst[:2])) if fcst else "STABLE")
            elif cand in raw_weather_cache:
                # Off-network alternate — we fetched its wx, evaluate it simply
                _awx = raw_weather_cache[cand]
                _am  = _awx.get('m')
                _am_raw = getattr(_am, 'raw', '') or ''
                # Quick severity from METAR: look for low vis/ceiling keywords
                _sev_c = 0; _color = "#008000"
                _issues_alt = []
                if _am and hasattr(_am, 'data') and _am.data:
                    try:
                        _vis = get_safe_num(_am.data.visibility.value if _am.data.visibility else None, 9999)
                        if _vis < 800: _sev_c = 3; _color = "#d6001a"; _issues_alt.append(f"VIS {int(_vis)}m")
                        elif _vis < 3000: _sev_c = max(_sev_c,2); _color="#eb8f34"; _issues_alt.append(f"VIS {int(_vis)}m")
                        _w_spd = get_safe_num(_am.data.wind_speed.value if _am.data.wind_speed else None)
                        _w_gst = get_safe_num(_am.data.wind_gust.value if _am.data.wind_gust else None) if _am.data.wind_gust else _w_spd
                        _cand_info = COMMON_ALT_AIRPORTS.get(cand, {})
                        _cxw, _, _ = calculate_winds(None, max(_w_spd,_w_gst), _cand_info.get('rwy',360), False) if _am.data.wind_direction else (0,0,0)
                    except: pass
                    try:
                        for _c in (_am.data.wx_codes or []):
                            _code = _c.repr
                            if any(k in _code for k in ['TS','FG','FZ','SN','GR']):
                                _sev_c = max(_sev_c, 1 if _code.startswith('-') else 3)
                                _color = "#d6001a" if _sev_c>=3 else "#eb8f34"
                                _issues_alt.append(_code)
                    except: pass
                sev_c = _sev_c; color = _color; is_cand_closed = False
                _m_raw = getattr(_am, 'raw', '') or ''
                wx_status = ("ACT: " + ", ".join(_issues_alt[:2])) if _issues_alt else ("METAR: " + _m_raw[:35] + "…" if _m_raw else "STABLE")
            else:
                sev_c = -1; color = "#555555"; wx_status = "FETCHING…"; is_cand_closed = False
            alts.append({'iata': cand, 'name': cand_coords.get('name', cand), 'dist': dist,
                         'severity': sev_c, 'color': color, 'wx_status': wx_status, 'in_network': cand in network_data,
                         'is_closed': is_cand_closed})

        alts.sort(key=lambda x: (max(x['severity'], 0), x['dist']))
        network_data[iata]['alternates'] = alts[:6]

    def evaluate_flight_wx(arr, target_dt, ac_type, reg="UNK"):
        issues = []
        is_red, is_amber, cat_badge = False, False, False
        if not target_dt or arr not in raw_weather_cache: return is_red, is_amber, cat_badge, issues
            
        curfew_str = ops.get(arr, {}).get('curfew')
        if curfew_str:
            try:
                ch, cm = map(int, curfew_str.split(':'))
                curfew_start = target_dt.replace(hour=ch, minute=cm, second=0, microsecond=0)
                if target_dt.hour < 12 and ch > 18: curfew_start -= timedelta(days=1)
                elif target_dt.hour > 18 and ch < 12: curfew_start += timedelta(days=1)
                
                curfew_end = curfew_start + timedelta(hours=7)
                
                if curfew_start <= target_dt <= curfew_end: is_red = True; issues.append(f"CURFEW VIOLATED ({curfew_str}Z)")
                elif curfew_start - timedelta(minutes=15) <= target_dt < curfew_start: is_amber = True; issues.append(f"CURFEW RISK ({curfew_str}Z)")
            except: pass

        t = raw_weather_cache[arr].get('t')
        valid_lines = []
        if t and hasattr(t, 'data') and t.data and hasattr(t.data, 'forecast'):
            for line in t.data.forecast:
                try:
                    start = line.start_time.dt.replace(tzinfo=timezone.utc)
                    end = line.end_time.dt.replace(tzinfo=timezone.utc)
                    if start <= target_dt <= end:
                        if getattr(line, 'probability', None) and line.probability.value == 30: continue
                        valid_lines.append(line)
                except: pass
                
        if not valid_lines: return is_red, is_amber, cat_badge, issues

        max_xw, min_vis, min_cld = 0, 9999, 99999
        for target_line in valid_lines:
            try:
                w_dir = get_safe_num(target_line.wind_direction.value if getattr(target_line, 'wind_direction', None) else None)
                w_spd = get_safe_num(target_line.wind_speed.value if getattr(target_line, 'wind_speed', None) else None)
                w_gst = get_safe_num(target_line.wind_gust.value if getattr(target_line, 'wind_gust', None) else None)
                xw, tw, _ = calculate_winds(w_dir, max(w_spd, w_gst), ops.get(arr, {}).get('rwy', 360), ops.get(arr, {}).get('one_way', False))
                if xw > max_xw: max_xw = xw
                # Check tailwind for one-way / spec airports
                limit_tw_line = ops.get(arr, {}).get('tw_lim', 15)
                if ops.get(arr, {}).get('one_way') or ops.get(arr, {}).get('spec'):
                    if tw >= limit_tw_line:
                        issues.append(f'TW {tw}kt (limit {limit_tw_line}kt)')
                        is_red = True
                    elif tw >= limit_tw_line - 3:
                        issues.append(f'TW {tw}kt (approaching limit)')
                        is_amber = True
                vis = get_safe_num(target_line.visibility.value if getattr(target_line, 'visibility', None) else None, 9999)
                if vis < min_vis: min_vis = vis
                clds = [(get_safe_num(getattr(c, 'base', 0)) * 100) if getattr(c, 'base', None) is not None else 0 for c in target_line.clouds if getattr(c, 'type', '') in valid_cld]
                if clds and min(clds) < min_cld: min_cld = min(clds)
                
                if hasattr(target_line, 'wx_codes') and target_line.wx_codes:
                    for wx in target_line.wx_codes:
                        code = wx.repr
                        if code not in issues:
                            _is_lt = code.startswith('-')
                            _always_crit = any(k in code for k in ['TS', 'FG', 'FZ', 'GR', 'SQ', 'FC', 'DS'])
                            _precip_wx   = any(k in code for k in ['SN', 'RASN', 'PL', 'IC', 'GS'])
                            if _always_crit:
                                issues.append(code); is_amber = True
                            elif _precip_wx:
                                issues.append(code)
                                if _is_lt: is_amber = True    # -SN/-RASN = amber
                                else:      is_red   = True    # SN/RASN = red
            except: pass

        limit_xw = ops.get(arr, {}).get('xw_lim', 25)
        if max_xw >= limit_xw: is_red = True; issues.append(f"XW {max_xw}kt")
        elif max_xw >= limit_xw - 5: is_amber = True; issues.append(f"XW {max_xw}kt")
            
        is_emb = ac_type and ('E19' in ac_type or 'E90' in ac_type or 'EMB' in ac_type or 'E75' in ac_type)
        is_spec = ops.get(arr, {}).get('spec', False)
        
        cat2_tails = ['G-LCAB', 'G-LCAC', 'G-LCAD', 'G-LCAE', 'G-LCAF', 'G-LCAG', 'G-LCAH', 'G-LCYV']
        
        if min_vis < 800:
            if is_emb:
                if reg in cat2_tails:
                    cat_badge = 'CAT II'
                    if min_vis < 300: is_red = True; issues.append(f"VIS {int(min_vis)}m (CAT II)")
                    else: is_amber = True; issues.append(f"VIS {int(min_vis)}m (CAT II)")
                else:
                    if min_vis < 550: is_red = True; issues.append(f"VIS {int(min_vis)}m (CAT I ONLY)")
                    else: is_amber = True; issues.append(f"VIS {int(min_vis)}m")
            else:
                cat_badge = 'CAT III' 
                if min_vis < 75: is_red = True; issues.append(f"VIS {int(min_vis)}m")
                else: is_amber = True; issues.append(f"VIS {int(min_vis)}m")

        if min_cld < 99999:
            if arr == "LCY":
                if min_cld <= 200: is_red = True; issues.append(f"CIG {int(min_cld)}ft")
                elif min_cld <= 400: is_amber = True; issues.append(f"CIG {int(min_cld)}ft")
            elif is_spec:
                if min_cld < 500: is_red = True; issues.append(f"CIG {int(min_cld)}ft")
                elif min_cld < 1000: is_amber = True; issues.append(f"CIG {int(min_cld)}ft")
            else:
                if min_cld < 200: is_red = True; issues.append(f"CIG {int(min_cld)}ft")
                elif min_cld < 500: is_amber = True; issues.append(f"CIG {int(min_cld)}ft")
                    
        return is_red, is_amber, cat_badge, issues

    if not flight_schedule_df.empty:
        working_df = flight_schedule_df
        if 'DATE_OBJ' in working_df.columns:
            t_m = working_df[working_df['DATE_OBJ'] == today_date]
            if not t_m.empty: working_df = t_m
            
        for _, row in working_df.iterrows():
            arr_str = str(row.get('ARR', '')).strip().upper()
            dep_str = str(row.get('DEP', '')).strip().upper()
            ac_type = str(row.get('AC_TYPE', '')).strip().upper()
            flt_str = str(row.get('FLT', '')).strip()
            if arr_str == dep_str: continue 
            
            f_group = "Main"
            if any(x in ac_type for x in ["E90", "E75", "E19", "EMB"]): f_group = "Cityflyer"
            elif any(x in ac_type for x in ["320", "321", "31E", "32E", "31", "32"]): f_group = "Euroflyer"
            else:
                if flt_str.startswith('BA8') or flt_str.startswith('BA4') or flt_str.startswith('BA7') or flt_str.startswith('BA3') or flt_str.startswith('BA9'): f_group = "Cityflyer"
                elif flt_str.startswith('BA2'): f_group = "Euroflyer"
                
            if CLIENT_ENV == "BACF":
                if f_group == "Cityflyer" and not show_cf: continue
                if f_group == "Euroflyer" and not show_ef: continue
                if f_group == "Main" and not show_bw: continue

            sta_raw = str(row.get('STA', '')).strip()
            std_raw = str(row.get('STD', '')).strip()
            reg = str(row.get('AC_REG', 'UNK')).strip().upper()
            
            if flt_str in live_flights_memory:
                live_reg = str(live_flights_memory[flt_str]['data'].get('aircraft', {}).get('regNumber') or '').upper()
                # ONLY let Aviation Edge override the registration if the schedule/AAR is blank
                if live_reg and live_reg != 'UNK' and (reg == 'UNK' or reg == 'NAN' or not reg): reg = live_reg

            sta = sta_raw.split('T')[1][:5] if 'T' in sta_raw else sta_raw[:5]
            if not sta or sta.lower() == 'nan': sta = "N/A"
            std = std_raw.split('T')[1][:5] if 'T' in std_raw else std_raw[:5]
            if not std or std.lower() == 'nan': std = "N/A"
            
            dep_date = row.get('DATE_OBJ', today_date)
            
            std_dt = None
            if std != "N/A":
                try:
                    s_dep = std.replace(":", "").zfill(4)
                    std_dt = datetime.combine(dep_date, datetime.strptime(s_dep, "%H%M").time()).replace(tzinfo=timezone.utc)
                except: pass
                
            sta_dt = None
            if sta != "N/A":
                try:
                    s_arr = sta.replace(":", "").zfill(4)
                    sta_dt = datetime.combine(dep_date, datetime.strptime(s_arr, "%H%M").time()).replace(tzinfo=timezone.utc)
                    if std_dt and sta_dt < std_dt: sta_dt += timedelta(days=1)
                except: pass
            
            if arr_str in network_data:
                is_old_inbound = False
                if flt_str in arrival_times: is_old_inbound = True 
                elif sta_dt:
                    if (now_utc - sta_dt).total_seconds() > 3600:
                        still_flying = False
                        for mem_flt, mem_data in live_flights_memory.items():
                            if mem_flt == flt_str:
                                is_g = time.time() - mem_data['last_seen'] > 180
                                if is_g and (now_utc - sta_dt).total_seconds() > 7200:
                                    still_flying = False
                                else:
                                    still_flying = True
                                break
                        if not still_flying: is_old_inbound = True

                if not is_old_inbound:
                    is_red, is_amber, cat_badge, issues = evaluate_flight_wx(arr_str, sta_dt, ac_type, reg)
                    _crit_wins = network_data.get(arr_str, {}).get('critical_notam_windows', [])
                    has_crit_notam = _notam_affects_flight(_crit_wins, sta_dt)
                    if has_crit_notam:
                        if "CRIT NOTAM" not in issues: issues.append("CRIT NOTAM")
                        is_red = True
                    dest_risk = "RED" if is_red else ("AMBER" if is_amber else "GREEN")
                    ib_col = "#d6001a" if dest_risk == "RED" else ("#eb8f34" if dest_risk == "AMBER" else "#008000")
                    reason_str = f" (🔴 {', '.join(issues)})" if is_red else (f" (🟠 {', '.join(issues)})" if is_amber else "")
                    
                    network_data[arr_str]['inbounds'].append({ "flt": flt_str, "dep": dep_str, "time": sta, "stat": f"SCHED{reason_str}", "color": ib_col, "cat3": cat_badge, "fcst_xw": 0, "lim_xw": ops.get(arr_str, {}).get('xw_lim', 25), "is_live": False })
                
            if dep_str in network_data:
                is_old_departure = False
                if flt_str in departure_times:
                    if time.time() - departure_times[flt_str] > 3600: is_old_departure = True
                elif std_dt:
                    if (now_utc - std_dt).total_seconds() > 5400: 
                        still_here = False
                        for mem_flt, mem_data in live_flights_memory.items():
                            if mem_flt == flt_str:
                                spd = get_safe_num(mem_data['data'].get('speed', {}).get('horizontal', mem_data['data'].get('geography', {}).get('speed', 0))) * 0.539957
                                f_dep = mem_data['data'].get('departure', {}).get('iataCode', '').upper()
                                is_g = time.time() - mem_data['last_seen'] > 180
                                if is_g and (now_utc - std_dt).total_seconds() > 7200:
                                    still_here = False
                                elif f_dep == dep_str and spd < 50: 
                                    still_here = True
                                break
                        if not still_here: is_old_departure = True
                
                if not is_old_departure:
                    # Upgrade: Calculate the destination weather risk for the departure board!
                    is_red, is_amber, cat_badge, issues = evaluate_flight_wx(arr_str, sta_dt, ac_type, reg)
                    _crit_wins = network_data.get(arr_str, {}).get('critical_notam_windows', [])
                    has_crit_notam = _notam_affects_flight(_crit_wins, sta_dt)
                    if has_crit_notam:
                        if "CRIT NOTAM" not in issues: issues.append("CRIT NOTAM")
                        is_red = True
                    dest_risk = "RED" if is_red else ("AMBER" if is_amber else "GREEN")
                    dep_col = "#d6001a" if dest_risk == "RED" else ("#eb8f34" if dest_risk == "AMBER" else "#008000")
                    
                    # Look up by flight+date first, fall back to bare flight key
                    _dep_date_str = dep_date.strftime('%Y%m%d') if hasattr(dep_date, 'strftime') else str(dep_date).replace('-','')
                    pax = (pax_figures.get(f"{flt_str}_{_dep_date_str}")
                           or pax_figures.get(f"BA{flt_str.lstrip('BA')}_{_dep_date_str}")
                           or pax_figures.get(flt_str)
                           or pax_figures.get(f"BA{flt_str.lstrip('BA')}"))
                    _dep_tt = _tt_lookup(flt_str)
                    network_data[dep_str]['departures'].append({ 
                        "flt": flt_str, 
                        "arr": arr_str, 
                        "time": std, 
                        "reg": reg,
                        "color": dep_col,
                        "pax_m": pax['m'] if pax else None,
                        "pax_c": pax['c'] if pax else None,
                        "dep_delay_min": _dep_tt.get('dep_delay_min', 0),
                        "gate_dep":      _dep_tt.get('gate_dep', ''),
                        "cancelled":     False,
                        "cat3":          False,
                        "fcst_xw":       0,
                        "lim_xw":        99,
                    })

        res_flights = []
        live_tracked_flts = []
        
        for flt, mem in list(live_flights_memory.items()):
            f = mem['data']
            is_ghost = (time.time() - mem['last_seen'] > 180) 
            try:
                icao = str(f.get('flight', {}).get('icaoNumber') or '').upper()
                arr = str(f.get('arrival', {}).get('iataCode') or '').upper()
                dep = str(f.get('departure', {}).get('iataCode') or '').upper()
                ac_type = str(f.get('aircraft', {}).get('icaoCode') or '').upper()
                live_reg = str(f.get('aircraft', {}).get('regNumber') or 'UNK').upper()
                
                group = ACTIVE_CONFIG["grouper"](f, icao, arr, dep, ac_type)
                if group == "UNK": continue
                
                if CLIENT_ENV == "BACF":
                    if (group == "CFE" and not show_cf) or (group == "EFW" and not show_ef) or (group == "BAW" and not show_bw): continue
                
                sta_text, sched_arr, sta_dt, is_diverted = "N/A", "UNK", None, False
                if not flight_schedule_df.empty:
                    w_df = flight_schedule_df
                    if 'DATE_OBJ' in w_df.columns:
                        t_m = w_df[w_df['DATE_OBJ'] == today_date]
                        if not t_m.empty: w_df = t_m
                    match = w_df[w_df['FLT'].astype(str).str.contains(flt, na=False)]
                    if not match.empty: 
                        sta_text = str(match.iloc[0]['STA']).strip()
                        sta = sta_text.split('T')[1][:5] if 'T' in sta_text else sta_text[:5]
                        sta_text = sta
                        
                        sched_arr = str(match.iloc[0]['ARR']).upper().strip()
                        # Check divert_memory first — RRT may have already updated ARR in schedule
                        # Check divert_memory — DB-backed, shared across workers
                        _mem_orig = (_divert_memory_get(flt) or '').upper()
                        if _mem_orig and _mem_orig != arr:
                            sched_arr = _mem_orig  # restore original dest
                            is_diverted = True
                            print(f'is_diverted=True via DB divert_memory: {flt} mem_orig={_mem_orig} arr={arr}')
                        elif sched_arr and sched_arr != "NAN" and arr and sched_arr != arr:
                            is_diverted = True
                        try:
                            s_arr = sta.replace(":", "").zfill(4)
                            sta_dt = datetime.combine(match.iloc[0].get('DATE_OBJ', today_date), datetime.strptime(s_arr, "%H%M").time()).replace(tzinfo=timezone.utc)
                        except: pass
                        
                        if live_reg == 'UNK':
                            live_reg = str(match.iloc[0].get('AC_REG', 'UNK')).strip().upper()

                p_lat, p_lon = f.get('geography', {}).get('latitude', 0), f.get('geography', {}).get('longitude', 0)
                if p_lat and p_lon and not is_ghost:
                    if flt not in flight_trails: flight_trails[flt] = []
                    # Store [lat, lon, alt_ft] so frontend can draw phase-aware trails
                    if not flight_trails[flt] or flight_trails[flt][-1][:2] != [p_lat, p_lon]:
                        flight_trails[flt].append([p_lat, p_lon, alt_ft])

                alt_ft = get_safe_num(f.get('geography', {}).get('altitude', 0))
                speed_kts = get_safe_num(f.get('speed', {}).get('horizontal', f.get('geography', {}).get('speed', 0))) * 0.539957
                # Read squawk — AE uses aircraft.squawk or system.squawk depending on feed version
                raw_squawk = (str(f.get('aircraft', {}).get('squawk') or '').strip() or
                              str(f.get('system', {}).get('squawk') or '').strip() or None)
                # Prefer OpenSky squawk if fresher (OpenSky is more reliable for transponder codes)
                if not raw_squawk and live_reg:
                    _sq_entry = FLEET.get(live_reg, {})
                    _sq_hex = _sq_entry.get('icao24', '').lower() if _sq_entry else ''
                    if _sq_hex and _sq_hex in opensky_pos_cache:
                        raw_squawk = opensky_pos_cache[_sq_hex].get('squawk')
                squawk = str(raw_squawk).zfill(4) if raw_squawk and str(raw_squawk).isdigit() else None
                math_eta, eta_dt = "N/A", None
                is_arrived = False
                
                if arr in ops and p_lat and p_lon:
                    dist_nm = calculate_dist(p_lat, p_lon, ops[arr]['lat'], ops[arr]['lon'])
                    # Ground-truth arrival: very low + close overrides stale speed data
                    # (fixes the 'stuck at FL2 for 30min' problem when AE lags a landing)
                    if alt_ft < 500 and dist_nm < 10:
                        is_arrived = True
                        if flt not in arrival_times: arrival_times[flt] = time.time()
                    elif speed_kts > 50:
                        effective_speed = speed_kts
                        if alt_ft < 10000 and dist_nm > 50: effective_speed = 350
                        eta_dt = now_utc + timedelta(hours=(dist_nm / effective_speed))
                        math_eta = eta_dt.strftime("%H:%M") + "Z"
                        
                        if is_ghost and dist_nm < 15 and alt_ft < 10000:
                            is_arrived = True
                            if flt not in arrival_times: arrival_times[flt] = time.time()
                        # STA already passed + aircraft low and near = early landing
                        elif sta_dt and now_utc > sta_dt and alt_ft < 3000 and dist_nm < 20:
                            is_arrived = True
                            if flt not in arrival_times: arrival_times[flt] = time.time()
                    else:
                        math_eta = "TAXI"
                        eta_dt = now_utc
                        if dist_nm < 5: 
                            is_arrived = True 
                            if flt not in arrival_times: arrival_times[flt] = time.time()
                elif speed_kts <= 50: 
                    math_eta = "TAXI"
                    eta_dt = now_utc
                    is_arrived = True 

                if is_ghost and sta_dt and (now_utc - sta_dt).total_seconds() > 7200:
                    is_arrived = True
                    if flt not in arrival_times: arrival_times[flt] = time.time()

                delay_trig = False
                try:
                    if sta_dt and eta_dt and (eta_dt - sta_dt).total_seconds() / 60.0 >= 150: delay_trig = True
                except: pass
                
                target_dt = eta_dt if eta_dt else sta_dt
                is_red, is_amber, cat_badge, issues = evaluate_flight_wx(arr, target_dt, ac_type, live_reg)
                _crit_wins = network_data.get(arr, {}).get('critical_notam_windows', [])
                has_crit_notam = _notam_affects_flight(_crit_wins, target_dt)
                if has_crit_notam:
                    if "CRIT NOTAM" not in issues: issues.append("CRIT NOTAM")
                    is_red = True
                
                dest_risk = "PURPLE" if is_diverted else ("RED" if is_red else ("CYAN" if delay_trig else ("AMBER" if is_amber else "GREEN")))
                # Always populate reason_str so tooltip shows WHY the badge is set
                if is_diverted:
                    reason_str = f" (🟣 DIVERT {sched_arr}→{arr})"
                elif is_red:
                    reason_str = f" (🔴 {', '.join(issues)})"
                elif delay_trig:
                    reason_str = f" (🔵 DELAY +150 MIN)"
                elif is_amber:
                    reason_str = f" (🟠 {', '.join(issues)})"
                else:
                    reason_str = ""
                
                if is_diverted or delay_trig:
                    e_type = "DIVERT" if is_diverted else "DELAY_150"
                    log_id = f"{flt}_{now_utc.strftime('%Y-%m-%d')}_{e_type}"
                    print(f'AUTO-DOSSIER: {flt} e_type={e_type} sched_arr={sched_arr} arr={arr} mem={divert_memory.get(flt,"?")}')
                    if not db.session.get(DisruptionLog, log_id):
                        wx_snap, t_snap, n_snap = "N/A", "N/A", "N/A"
                        target_iata = sched_arr if is_diverted else arr
                        if target_iata in raw_weather_cache:
                            if raw_weather_cache[target_iata].get('m'): wx_snap = getattr(raw_weather_cache[target_iata].get('m'), 'raw', 'N/A')
                            if raw_weather_cache[target_iata].get('t'): t_snap = getattr(raw_weather_cache[target_iata].get('t'), 'raw', 'N/A')
                        if target_iata in raw_notam_cache:
                            n_snap = "\n\n".join(raw_notam_cache[target_iata]) if raw_notam_cache[target_iata] else "N/A"

                        # Link ACARS messages for this flight (last 6 hours)
                        cutoff = now_utc - timedelta(hours=6)
                        acars_msgs = AcarsLog.query.filter(
                            AcarsLog.flight == flt,
                            AcarsLog.timestamp >= cutoff
                        ).order_by(AcarsLog.timestamp.asc()).all()
                        a_snap = "\n".join([f"{m.timestamp.strftime('%H:%MZ')} {m.reg}: {m.message}" for m in acars_msgs]) if acars_msgs else "N/A"

                        # Auto case ref (CF-NNN)
                        last = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).first()
                        last_num = 0
                        if last and last.case_ref:
                            try: last_num = int(last.case_ref.split('-')[-1])
                            except: pass
                        c_ref = f"CF-{last_num+1:03d}"

                        # Crosswind snapshot — condition-aware operative limit
                        xw_val = "N/A"; xw_lim = "N/A"
                        if target_iata in network_data:
                            xw_val = str(network_data[target_iata].get('cur_xw', 'N/A'))
                        try:
                            _apt_bl = ops.get(target_iata, {}).get('xw_lim', None)
                            _wxc = []
                            _tc = None
                            if target_iata in raw_weather_cache:
                                _mm = raw_weather_cache[target_iata].get('m')
                                if _mm and hasattr(_mm, 'data') and _mm.data:
                                    _wxc = [w.repr for w in (_mm.data.wx_codes or [])]
                                    try: _tc = get_safe_num(_mm.data.temperature.value)
                                    except: pass
                            _atype = str(live_reg[:4]) if live_reg else "A320"
                            _ol, _al, _ck, _cl, _bf, _ak = get_operative_xw_limit(
                                ac_type, _wxc, _apt_bl, _tc)
                            xw_lim = (f"{_ol}kt ({_ak} / {_cl} / binding:{_bf})"
                                      if _apt_bl else f"{_ol}kt ({_ak} / {_cl})")
                        except Exception: pass

                        db.session.add(DisruptionLog(
                            id=log_id, flight=flt, date=now_utc.strftime('%Y-%m-%d'), event_type=e_type,
                            origin=dep, sched_dest=sched_arr, actual_dest=arr,
                            weather_snap=wx_snap, taf_snap=t_snap, notam_snap=n_snap,
                            acars_snap=a_snap, tail_snap=live_reg or "N/A",
                            xw_snap=xw_val, xw_limit=xw_lim,
                            case_ref=c_ref, logged_by="AUTO-OCC",
                            metar_history=None  # fetched async below
                        ))
                        try:
                            db.session.commit()
                            # Fetch 12hr METAR history async — never block the weather route
                            _hist_iata = sched_arr if is_diverted else arr
                            _hist_icao = ops.get(_hist_iata, {}).get('icao', _hist_iata)
                            def _auto_metar_bg(lid, icao):
                                try:
                                    _h = fetch_metar_history(icao, hours=12)
                                    if _h:
                                        with app.app_context():
                                            _l = db.session.get(DisruptionLog, lid)
                                            if _l:
                                                _l.metar_history = _h
                                                db.session.commit()
                                except Exception as _e:
                                    print(f'Auto-cap METAR hist failed: {_e}')
                            threading.Thread(
                                target=_auto_metar_bg,
                                args=(log_id, _hist_icao),
                                daemon=True
                            ).start()
                        except: db.session.rollback()

                dep_lat, dep_lon = ops.get(dep, {}).get('lat'), ops.get(dep, {}).get('lon')
                arr_lat, arr_lon = ops.get(arr, {}).get('lat'), ops.get(arr, {}).get('lon')

                # --- AUTO-TACTICAL DETECTION (GO-AROUND & DIVERT) ---
                tactical_alert = ""
                if p_lat and p_lon and arr in ops:
                    t_dist = calculate_dist(p_lat, p_lon, ops[arr]['lat'], ops[arr]['lon'])
                    
                    if flt in flight_tactical_state:
                        prev_state = flight_tactical_state[flt]
                        prev_alt = prev_state.get('alt', alt_ft)
                        prev_dist = prev_state.get('dist', t_dist)
                        
                        # IDEA 2: Missed Approach Trap (Was below 5000ft, within 15nm, now climbing >300ft)
                        if prev_alt < 5000 and t_dist < 15:
                            if alt_ft > prev_alt + 300:
                                tactical_alert = "⚠️ MISSED APPROACH DETECTED"
                                
                        # IDEA 3: Divert Trajectory (Distance is increasing, altitude > 4000ft)
                        if t_dist > prev_dist + 2:
                            if prev_state.get('alert') == "⚠️ MISSED APPROACH DETECTED" or t_dist < 50:
                                if alt_ft > 4000:
                                    tactical_alert = "⚠️ POSSIBLE DIVERT"
                                    
                        # Persist the alert once triggered
                        if not tactical_alert and prev_state.get('alert'):
                            tactical_alert = prev_state.get('alert')

                    # Save state for the next 3-minute pull
                    flight_tactical_state[flt] = {
                        'alt': alt_ft,
                        'dist': t_dist,
                        'alert': tactical_alert
                    }

                # ── Squawk alert logging ───────────────────────────────────────
                is_emergency_sq = squawk in SQUAWK_EMERGENCY_CODES
                if is_emergency_sq:
                    sq_info = SQUAWK_EMERGENCY[squawk]
                    now_ts = time.time()
                    if flt not in squawk_alert_log:
                        squawk_alert_log[flt] = {
                            'squawk': squawk, 'meaning': sq_info[0], 'severity': sq_info[1],
                            'icon': sq_info[2], 'first_seen': now_utc.strftime('%H:%MZ'),
                            'last_seen': now_utc.strftime('%H:%MZ'),
                            'reg': live_reg, 'dep': dep, 'arr': arr, 'flt': flt
                        }
                        print(f'SQUAWK ALERT: {flt} ({live_reg}) squawking {squawk} — {sq_info[0]}')
                    else:
                        squawk_alert_log[flt]['last_seen'] = now_utc.strftime('%H:%MZ')
                        squawk_alert_log[flt]['squawk'] = squawk
                elif flt in squawk_alert_log and squawk_alert_log[flt]['squawk'] not in SQUAWK_EMERGENCY_CODES:
                    del squawk_alert_log[flt]  # no longer squawking emergency
                # ─────────────────────────────────────────────────────────────

                # Enrich with timetable data (gates, delays, ATD, ATA)
                _tt = _tt_lookup(flt)

                if not is_arrived:
                    res_flights.append({
                        "flt": flt, "dep": dep, "arr": arr, "sched_arr": sched_arr, "group": group, "type": ac_type,
                        "lat": p_lat, "lon": p_lon, "dep_lat": dep_lat, "dep_lon": dep_lon, "arr_lat": arr_lat, "arr_lon": arr_lon,
                        "alt": alt_ft, "fl_str": f"FL{max(0, round(alt_ft / 100)):03d}", "hdg": f.get('geography', {}).get('direction', 0),
                        "spd": speed_kts, "reg": live_reg, "sta": sta_text, "math_eta": math_eta,
                        "dest_risk": dest_risk, "eta_reason": reason_str, "is_diverted": is_diverted, "is_ghost": is_ghost, "acars": acars_cache.get(flt),
                        "tactical_alert": tactical_alert,
                        "squawk": squawk,
                        "squawk_emergency": is_emergency_sq,
                        "squawk_meaning": SQUAWK_EMERGENCY[squawk][0] if is_emergency_sq else None,
                        "squawk_icon": SQUAWK_EMERGENCY[squawk][2] if is_emergency_sq else None,
                        # Timetable enrichment
                        "gate_dep":      _tt.get('gate_dep', ''),
                        "gate_arr":      _tt.get('gate_arr', ''),
                        "dep_delay_min": _tt.get('dep_delay_min', 0),
                        "arr_delay_min": _tt.get('arr_delay_min', 0),
                        "atd":           _tt.get('atd', ''),
                        "ata":           _tt.get('ata', ''),
                        "etd_live":      _tt.get('etd', ''),
                        "eta_timetable": _tt.get('eta_live', ''),
                        "ae_status":     _tt.get('status', ''),
                    })
                    
                    if arr in network_data:
                        found = False
                        for ib in network_data[arr]['inbounds']:
                            if ib['flt'] == flt:
                                _ib_tt = _tt_lookup(flt)
                                found, ib['stat'], ib['color'], ib['cat3'], ib['is_live'] = True, "DIVERTING" if is_diverted else f"ETA {math_eta}{reason_str}", "#a855f7" if is_diverted else ("#d6001a" if dest_risk=="RED" else ("#00ffff" if dest_risk=="CYAN" else ("#eb8f34" if dest_risk=="AMBER" else "#008000"))), cat_badge, True
                                ib['arr_delay_min'] = _ib_tt.get('arr_delay_min', 0)
                                ib['gate_arr']      = _ib_tt.get('gate_arr', '')
                                break
                        if not found:
                            network_data[arr]['inbounds'].append({ "flt": flt, "dep": dep, "time": math_eta, "stat": "DIVERTING" if is_diverted else f"ETA {math_eta}{reason_str}", "color": "#a855f7" if is_diverted else ("#d6001a" if dest_risk=="RED" else ("#00ffff" if dest_risk=="CYAN" else ("#eb8f34" if dest_risk=="AMBER" else "#008000"))), "cat3": cat_badge, "fcst_xw": 0, "lim_xw": ops.get(arr, {}).get('xw_lim', 25), "is_live": True })
                
                    live_tracked_flts.append(flt)
            except Exception as e:
                print(f"Skipping bad flight data point: {e}")
                continue
            
        for iata in network_data:
            network_data[iata]['inbounds'] = sorted(network_data[iata]['inbounds'], key=lambda x: str(x.get('time', '9999')))
            network_data[iata]['departures'] = sorted(network_data[iata]['departures'], key=lambda x: str(x.get('time', '9999')))

        if len(live_flights_memory) > 0:
            for old_flt in list(flight_trails.keys()):
                if old_flt not in live_tracked_flts: del flight_trails[old_flt]

        return jsonify({"weather": network_data, "fleet": res_flights, "acars_all": acars_cache})

@app.route('/api/timetable_status')
@login_required
def timetable_status():
    """Debug — shows timetable cache health and sample entries."""
    total = sum(len(v.get('arr',[])) + len(v.get('dep',[])) for v in ae_timetable_cache.values())
    age   = round(time.time() - ae_timetable_cache_time)
    sample = {}
    for iata, data in list(ae_timetable_cache.items())[:3]:
        sample[iata] = {
            'arrivals':   len(data.get('arr', [])),
            'departures': len(data.get('dep', [])),
            'first_arr':  data['arr'][0].get('flight',{}).get('iataNumber') if data.get('arr') else None,
        }
    return jsonify({
        'airports':  len(ae_timetable_cache),
        'total_entries': total,
        'cache_age_sec': age,
        'next_refresh_sec': max(0, AE_TIMETABLE_TTL - age),
        'sample': sample,
    })

@app.route('/api/squawk_alerts')
@login_required
def get_squawk_alerts():
    """Return current emergency squawk alerts across tracked fleet."""
    # Pull squawk from live flight data if available
    active = list(squawk_alert_log.values())
    return jsonify({
        'alerts': active,
        'count':  len(active),
        'emergency_count': sum(1 for a in active if a['severity'] == 'RED'),
    })

@app.route('/api/opensky_status')
@login_required
def opensky_status():
    """Debug endpoint — returns OpenSky cache health."""
    return jsonify({
        'count':        len(opensky_pos_cache),
        'total_hexes':  len(ICAO24_TO_REG),
        'last_poll_age': ('never' if opensky_cache_time == 0 
                          else round(time.time() - opensky_cache_time)),
        'last_poll_utc': (None if opensky_cache_time == 0 
                          else datetime.fromtimestamp(opensky_cache_time, timezone.utc).strftime('%H:%MZ')),
        'token_ok':     bool(OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET),
        'aircraft':     [
            {'icao24': k, 'reg': ICAO24_TO_REG.get(k, '?'),
             'alt_ft': v['alt_ft'], 'spd_kts': v['spd_kts'],
             'on_ground': v['on_ground'],
             'age_sec': round(time.time() - v['last_seen'])}
            for k, v in opensky_pos_cache.items()
        ]
    })

@app.route('/api/flight_trace')
@login_required
def flight_trace():
    flt = request.args.get('flt')
    if not flt: return jsonify({"trail": []})
    local_trail = flight_trails.get(flt, [])
    if not AVIATION_EDGE_KEY: return jsonify({"trail": local_trail})
    url = f"https://aviation-edge.com/v2/public/historicalTrack?key={AVIATION_EDGE_KEY}&flightIata={flt}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                trail = []
                for pt in data:
                    lat = pt.get('latitude') or pt.get('geography', {}).get('latitude')
                    lon = pt.get('longitude') or pt.get('geography', {}).get('longitude')
                    alt = pt.get('altitude') or pt.get('geography', {}).get('altitude') or 0
                    if lat and lon: trail.append([lat, lon, alt])
                if len(trail) > 1: return jsonify({"trail": trail})
    except: pass
    return jsonify({"trail": local_trail})

@app.route('/api/flight_brief')
@login_required
def flight_brief():
    flt = request.args.get('flt')
    arr = request.args.get('arr')
    if not AVIATION_EDGE_KEY or not arr or not flt: return jsonify({"error": "Missing data"})
    url = f"https://aviation-edge.com/v2/public/timetable?key={AVIATION_EDGE_KEY}&iataCode={arr}&type=arrival"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for d in data:
                    if d.get('flight', {}).get('iataNumber', '').upper() == flt.upper():
                        arr_data = d.get('arrival', {})
                        dep_data = d.get('departure', {})
                        def f_time(t): return "N/A" if not t else (t.split('T')[1][:5] if 'T' in t else str(t)[:5])
                        return jsonify({
                            "gate": arr_data.get('gate') or 'TBC',
                            "terminal": arr_data.get('terminal') or 'TBC',
                            "delay": arr_data.get('delay') or 0,
                            "sta": f_time(arr_data.get('scheduledTime')),
                            "eta": f_time(arr_data.get('estimatedTime')),
                            "ata": f_time(arr_data.get('actualTime')),
                            "atd": f_time(dep_data.get('actualTime'))
                        })
        return jsonify({"error": "No timetable match found"})
    except: return jsonify({"error": "API Timeout"})

@app.route('/api/dep_gate')
@login_required
def dep_gate():
    flt = request.args.get('flt', '').upper()
    if not flt or flight_schedule_df.empty: return jsonify({"error": "Upload Schedule First"})
    working_df = flight_schedule_df
    if 'DATE_OBJ' in working_df.columns:
        today_match = working_df[working_df['DATE_OBJ'] == datetime.now(timezone.utc).date()]
        if not today_match.empty: working_df = today_match
    match = working_df[working_df['FLT'].astype(str).str.upper().str.contains(flt, na=False)]
    if match.empty: return jsonify({"error": "Flight not found"})
    dep_iata = str(match.iloc[0]['DEP']).upper()
    arr_iata = str(match.iloc[0]['ARR']).upper()
    if not AVIATION_EDGE_KEY: return jsonify({"error": "No API Key"})
    url = f"https://aviation-edge.com/v2/public/timetable?key={AVIATION_EDGE_KEY}&iataCode={dep_iata}&type=departure"
    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for d in data:
                    if d.get('flight', {}).get('iataNumber', '').upper() == flt:
                        dep_data = d.get('departure', {})
                        def f_time(t): return "N/A" if not t else (t.split('T')[1][:5] if 'T' in t else str(t)[:5])
                        return jsonify({
                            "flt": flt, "route": f"{dep_iata} ➔ {arr_iata}",
                            "gate": dep_data.get('gate') or 'TBC',
                            "terminal": dep_data.get('terminal') or 'TBC',
                            "std": f_time(dep_data.get('scheduledTime')),
                            "etd": f_time(dep_data.get('estimatedTime')),
                            "atd": f_time(dep_data.get('actualTime'))
                        })
        return jsonify({"error": "No Gate Filed Yet"})
    except: return jsonify({"error": "API Timeout"})

@app.route('/api/coach_route')
@login_required
def coach_route():
    origin = request.args.get('origin', '').upper()
    dest = request.args.get('dest', '').upper()
    def get_coords(iata):
        if iata in base_airports: return base_airports[iata]['lat'], base_airports[iata]['lon']
        try:
            st = Station.from_iata(iata)
            if st and st.latitude and st.longitude: return st.latitude, st.longitude
        except: pass
        return None, None
    lat1, lon1 = get_coords(origin)
    lat2, lon2 = get_coords(dest)
    if not lat1 or not lat2: return jsonify({"error": "Airport GPS coordinates not found in global database."})
    dist_nm = calculate_dist(lat1, lon1, lat2, lon2)
    flight_mins = round((dist_nm / 350.0) * 60 + 30) 
    f_hrs = flight_mins // 60
    f_mins = flight_mins % 60
    flight_time_str = f"{f_hrs}h {f_mins}m" if f_hrs > 0 else f"{f_mins}m"
    osrm_url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
    try:
        resp = requests.get(osrm_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('code') == 'Ok':
                route = data['routes'][0]
                dist_miles = round(route['distance'] * 0.000621371, 1)
                duration_mins = round(route['duration'] / 60)
                hrs = duration_mins // 60
                mins = duration_mins % 60
                time_str = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
                return jsonify({
                    "distance": dist_miles, "time": time_str, "geojson": route['geometry'],
                    "flight_dist": dist_nm, "flight_time": flight_time_str
                })
    except: pass
    return jsonify({"error": "No road route found between these airports.", "flight_only": True, "flight_dist": dist_nm, "flight_time": flight_time_str})

@app.route('/api/icing_sigmets')
@login_required
def get_icing_sigmets():
    try:
        resp = requests.get('https://aviationweather.gov/api/data/isigmet?format=geojson', timeout=10)
        return jsonify(resp.json())
    except Exception as e: return jsonify({"error": str(e), "features": []})

@app.route('/api/tail_route')
@login_required
def get_tail_route():
    reg = request.args.get('reg', '').strip().upper()
    if not reg or flight_schedule_df.empty: return jsonify([])
    
    # UPGRADE: Pull Today AND Tomorrow's flights for this tail!
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    tomorrow = today + timedelta(days=1)
    
    try:
        w_df = flight_schedule_df
        if 'DATE_OBJ' in w_df.columns:
            # Filter for dates that are >= today and <= tomorrow
            t_m = w_df[(w_df['DATE_OBJ'] >= today) & (w_df['DATE_OBJ'] <= tomorrow)]
            if not t_m.empty: w_df = t_m
            
        t_df = w_df[w_df['AC_REG'].str.upper() == reg]
        if t_df.empty: return jsonify([])

        # Sort by date then STD so routing shows in correct chronological order
        sort_cols = [c for c in ['DATE_OBJ', 'STD'] if c in t_df.columns]
        if sort_cols: t_df = t_df.sort_values(sort_cols)

        route = []
        for _, row in t_df.iterrows():
            d_obj = row.get('DATE_OBJ', today)
            is_tmrw = (d_obj == tomorrow)
            date_tag = " (TMRW)" if is_tmrw else ""

            # Suppress tomorrow's flights if reg is still UNK (no AAR received yet)
            row_reg = str(row.get('AC_REG', 'UNK')).strip().upper()
            if is_tmrw and row_reg in ('UNK', '', 'NAN'):
                continue

            sta_raw = str(row.get('STA', '')).strip()
            std_raw = str(row.get('STD', '')).strip()
            sta = sta_raw.split('T')[1][:5] if 'T' in sta_raw else sta_raw[:5]
            std = std_raw.split('T')[1][:5] if 'T' in std_raw else std_raw[:5]

            route.append({
                "flt": str(row.get('FLT', '')).strip(),
                "dep": str(row.get('DEP', '')).strip(),
                "arr": str(row.get('ARR', '')).strip(),
                "std": f"{std}{date_tag}"
            })
        return jsonify(route)
    except Exception as e:
        return jsonify([])

@app.route('/api/flight_details')
@login_required
def get_flight_details():
    flt = request.args.get('flt', '').strip().upper()
    if not flt:
        return jsonify({"error": "No flight specified"})
        
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    # If no schedule loaded, try timetable cache directly
    if flight_schedule_df.empty:
        _tt_fb = _tt_lookup(flt)
        return jsonify({
            "flt": flt, "dep": "", "arr": "",
            "std": _tt_fb.get("std","--:--"), "sta": _tt_fb.get("sta","--:--"),
            "reg": _tt_fb.get("aircraft_reg",""),
            "gate_dep": _tt_fb.get("gate_dep",""), "gate_arr": _tt_fb.get("gate_arr",""),
            "dep_delay_min": _tt_fb.get("dep_delay_min",0),
            "arr_delay_min": _tt_fb.get("arr_delay_min",0),
            "atd": _tt_fb.get("atd","--:--"), "etd": _tt_fb.get("etd","--:--"),
            "ata": _tt_fb.get("ata","--:--"), "eta_live": _tt_fb.get("eta_live","--:--"),
            "fa_status": _tt_fb.get("status",""),
            "delay_str": "", "runway_off": "", "runway_on": "",
            "flight_only": True
        })

    try:
        w_df = flight_schedule_df
        if 'DATE_OBJ' in w_df.columns:
            t_m = w_df[w_df['DATE_OBJ'] == today]
            if not t_m.empty: w_df = t_m
            
        # Find this specific flight in today's schedule
        f_df = w_df[w_df['FLT'].str.upper() == flt]
        if f_df.empty:
            # Not in schedule CSV — try timetable cache before giving up
            _tt_only = _tt_lookup(flt)
            if _tt_only:
                return jsonify({
                    "flt": flt, "dep": _tt_only.get("dep",""), "arr": _tt_only.get("arr",""),
                    "std": _tt_only.get("std","--:--"), "sta": _tt_only.get("sta","--:--"),
                    "reg": _tt_only.get("aircraft_reg",""),
                    "gate_dep": _tt_only.get("gate_dep",""), "gate_arr": _tt_only.get("gate_arr",""),
                    "terminal_dep": _tt_only.get("terminal_dep",""),
                    "terminal_arr": _tt_only.get("terminal_arr",""),
                    "dep_delay_min": _tt_only.get("dep_delay_min",0),
                    "arr_delay_min": _tt_only.get("arr_delay_min",0),
                    "delay_str": f'+{_tt_only["dep_delay_min"]}min DEP' if _tt_only.get("dep_delay_min",0) >= 5 else "",
                    "atd": _tt_only.get("atd","--:--"), "etd": _tt_only.get("etd","--:--"),
                    "ata": _tt_only.get("ata","--:--"), "eta_live": _tt_only.get("eta_live","--:--"),
                    "fa_status": _tt_only.get("status",""), "runway_off": "", "runway_on": "",
                    "flight_only": True  # flag: data from timetable cache only
                })
            # Genuinely unknown flight — return empty data not an error
            return jsonify({"flt": flt, "dep": "", "arr": "", "std": "--:--", "sta": "--:--",
                            "reg": "", "gate_dep": "", "gate_arr": "", "dep_delay_min": 0,
                            "arr_delay_min": 0, "atd": "--:--", "ata": "--:--",
                            "etd": "--:--", "eta_live": "--:--", "fa_status": "",
                            "delay_str": "", "runway_off": "", "runway_on": ""})
            
        row = f_df.iloc[0]
        
        sta_raw = str(row.get('STA', '')).strip()
        std_raw = str(row.get('STD', '')).strip()
        sta = sta_raw.split('T')[1][:5] if 'T' in sta_raw else sta_raw[:5]
        std = std_raw.split('T')[1][:5] if 'T' in std_raw else std_raw[:5]
        
        reg = str(row.get('AC_REG', 'UNK')).strip().upper()
        
        # Checking if Aviation Edge has a live registration override
        if flt in live_flights_memory:
            live_reg = str(live_flights_memory[flt]['data'].get('aircraft', {}).get('regNumber') or '').upper()
            if live_reg and live_reg != 'UNK': reg = live_reg

        # Enrich from AE timetable cache
        _tt = _tt_lookup(flt)

        # Use timetable times if available, fall back to schedule CSV
        std_out = _tt.get('std') or (std + ' Z')
        sta_out = _tt.get('sta') or (sta + ' Z')

        dep_iata = str(row.get('DEP', '')).strip()
        arr_iata = str(row.get('ARR', '')).strip()

        # Build delay string
        dep_delay = _tt.get('dep_delay_min', 0) or 0
        arr_delay = _tt.get('arr_delay_min', 0) or 0
        delay_str = ''
        if dep_delay >= 5:  delay_str = f'+{dep_delay}min DEP'
        elif arr_delay >= 5: delay_str = f'+{arr_delay}min ARR'

        # AE status → friendly label
        ae_status = _tt.get('status', '')
        status_map = {
            'SCHEDULED': 'SCHEDULED', 'ACTIVE': 'AIRBORNE',
            'LANDED': 'LANDED', 'CANCELLED': 'CANCELLED',
            'DIVERTED': 'DIVERTED', 'DELAYED': 'DELAYED',
        }
        fa_status = status_map.get(ae_status, ae_status)

        return jsonify({
            "flt":          flt,
            "dep":          dep_iata,
            "arr":          arr_iata,
            "std":          std_out,
            "sta":          sta_out,
            "reg":          reg,
            "gate_dep":     _tt.get('gate_dep', ''),
            "gate_arr":     _tt.get('gate_arr', ''),
            "terminal_dep": _tt.get('terminal_dep', ''),
            "terminal_arr": _tt.get('terminal_arr', ''),
            "dep_delay_min": dep_delay,
            "arr_delay_min": arr_delay,
            "delay_str":    delay_str,
            "atd":          _tt.get('atd', '--:--'),
            "etd":          _tt.get('etd', '--:--'),
            "ata":          _tt.get('ata', '--:--'),
            "eta_live":     _tt.get('eta_live', '--:--'),
            "fa_status":    fa_status,
            "runway_off":   '',
            "runway_on":    '',
        })
    except Exception as e:
        return jsonify({"error": "Error processing flight details."})


# ── EU261 PDF EVIDENCE PACK ───────────────────────────────────────────────────

def _build_evidence_pdf(log, extra_evidence=None):
    """Generate EU261 evidence PDF using reportlab.
    Respects hidden_sections, includes 12hr METAR history, source attribution,
    and a full section audit trail footnote."""
    if not HAS_PDF:
        return None

    NAVY  = colors.HexColor('#0A1628')
    CYAN  = colors.HexColor('#0EA5E9')
    WHITE = colors.white
    LGREY = colors.HexColor('#F0F4F8')
    DGREY = colors.HexColor('#64748B')
    GREEN = colors.HexColor('#10B981')
    AMBER = colors.HexColor('#F59E0B')
    RED   = colors.HexColor('#EF4444')
    BLACK = colors.HexColor('#0F172A')
    BLUE2 = colors.HexColor('#1E3A5F')

    # ── gather data ────────────────────────────────────────────────────────
    hidden  = get_hidden(log)
    audit   = get_audit(log)

    case_ref   = getattr(log,'case_ref',None) or log.id
    flight     = log.flight or 'N/A'
    event_date = log.date or 'N/A'
    event_type = log.event_type or 'N/A'
    origin     = log.origin or 'N/A'
    dest       = log.sched_dest or 'N/A'
    actual_dest= log.actual_dest or dest
    tail       = getattr(log,'tail_snap',None) or 'N/A'
    logged_by  = getattr(log,'logged_by',None) or 'N/A'
    ts_str     = log.timestamp.strftime('%d %b %Y %H:%MZ') if log.timestamp else 'N/A'
    ba_code    = log.ba_code or 'N/A'
    notes_txt  = log.notes or 'No controller notes recorded.'
    xw_snap    = getattr(log,'xw_snap',None) or 'N/A'
    xw_limit   = getattr(log,'xw_limit',None) or 'N/A'
    m_hist     = getattr(log,'metar_history',None) or None
    gen_str    = datetime.utcnow().strftime('%d %b %Y %H:%MZ')

    # Evidence score counts ALL fields including new metar_history
    ev_fields = [log.weather_snap, log.taf_snap, log.notam_snap,
                 getattr(log,'acars_snap',None), log.ba_code,
                 getattr(log,'logged_by',None), log.notes,
                 getattr(log,'xw_snap',None), m_hist]
    ev_score  = sum(1 for f in ev_fields if f and str(f).strip() not in ('','N/A','None'))
    ev_max    = len(ev_fields)
    ev_pct    = int(ev_score / ev_max * 100)
    ev_col    = GREEN if ev_pct >= 80 else (AMBER if ev_pct >= 50 else RED)

    # ── styles ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=18*mm)

    base = getSampleStyleSheet()
    def sty(name, **kw):
        return ParagraphStyle(name, parent=base['Normal'], **kw)

    S_TITLE   = sty('title',  fontSize=20, textColor=WHITE, fontName='Helvetica-Bold', leading=24)
    S_SUB     = sty('sub',    fontSize=9,  textColor=DGREY, fontName='Helvetica')
    S_HEAD    = sty('head',   fontSize=12, textColor=CYAN,  fontName='Helvetica-Bold', leading=16)
    S_LABEL   = sty('lbl',    fontSize=8,  textColor=DGREY, fontName='Helvetica-Bold')
    S_VALUE   = sty('val',    fontSize=10, textColor=BLACK, fontName='Helvetica')
    S_MONO    = sty('mono',   fontSize=8,  textColor=BLACK, fontName='Courier', leading=11)
    S_MONO_SM = sty('monosm', fontSize=7,  textColor=BLACK, fontName='Courier', leading=10)
    S_MISS    = sty('miss',   fontSize=10, textColor=RED,   fontName='Helvetica-BoldOblique')
    S_DISC    = sty('disc',   fontSize=7,  textColor=DGREY, fontName='Helvetica-Oblique', leading=10)
    S_SRC     = sty('src',    fontSize=7,  textColor=colors.HexColor('#475569'),
                    fontName='Helvetica-Oblique', leading=9,
                    leftIndent=4, borderPadding=(2,4,2,4))
    S_SHDR    = sty('shdr',   fontSize=10, textColor=WHITE, fontName='Helvetica-Bold',
                    backColor=NAVY, leftPadding=6, rightPadding=6)
    S_HIDDEN  = sty('hidden', fontSize=9,  textColor=AMBER, fontName='Helvetica-BoldOblique')
    S_BOX_BODY= sty('box',    fontSize=7.5, textColor=BLACK, fontName='Courier',
                    leading=11, leftIndent=6, rightIndent=6, backColor=LGREY,
                    borderPadding=(4,6,4,6))

    def safe(txt):
        if not txt: return ''
        s = str(txt).replace('\x00','').encode('utf-8','replace').decode('utf-8')
        return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    def safe_para(txt, style):
        if not txt: return Paragraph('', style)
        lines = safe(str(txt)).split('\n')
        return Paragraph('<br/>'.join(lines), style)

    def source_line(section_key):
        """Italic attribution line shown below each section."""
        src_txt = EVIDENCE_SOURCES.get(section_key, '')
        if not src_txt: return []
        return [Paragraph(f'&#9432; Source: {safe(src_txt)}', S_SRC), Spacer(1, 2*mm)]

    def section_header(txt):
        return [
            Spacer(1, 4*mm),
            Table([[Paragraph(safe(txt), S_SHDR)]],
                  colWidths=[180*mm],
                  style=TableStyle([
                      ('BACKGROUND',(0,0),(-1,-1), NAVY),
                      ('LEFTPADDING',(0,0),(-1,-1),8),
                      ('RIGHTPADDING',(0,0),(-1,-1),8),
                      ('TOPPADDING',(0,0),(-1,-1),5),
                      ('BOTTOMPADDING',(0,0),(-1,-1),5),
                  ])),
            Spacer(1, 3*mm)
        ]

    def hidden_notice(section_key, label):
        """Placeholder block shown in PDF when a section is suppressed."""
        who = next((a['user'] for a in reversed(audit)
                    if a['section']==section_key and a['action']=='hide'), 'unknown')
        when = next((a['timestamp'] for a in reversed(audit)
                     if a['section']==section_key and a['action']=='hide'), '')
        return [
            Table([[Paragraph(
                f'&#9888; SECTION SUPPRESSED — {safe(label)}<br/>'
                f'<font size="7">Excluded by {safe(who)} at {safe(when)}. '
                f'Data retained in system — available on request.</font>',
                S_HIDDEN)]],
                colWidths=[180*mm],
                style=TableStyle([
                    ('BACKGROUND',(0,0),(-1,-1), colors.HexColor('#1C1400')),
                    ('LEFTPADDING',(0,0),(-1,-1),10),
                    ('RIGHTPADDING',(0,0),(-1,-1),10),
                    ('TOPPADDING',(0,0),(-1,-1),6),
                    ('BOTTOMPADDING',(0,0),(-1,-1),6),
                    ('BOX',(0,0),(-1,-1),1,AMBER),
                ])),
            Spacer(1, 3*mm)
        ]

    def kv_table(rows):
        data = [[Paragraph(safe(k).upper(), S_LABEL), safe_para(v, S_VALUE)] for k,v in rows]
        t = Table(data, colWidths=[45*mm, 135*mm])
        t.setStyle(TableStyle([
            ('ROWBACKGROUNDS',(0,0),(-1,-1),[LGREY, WHITE]),
            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
            ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
        return t

    def content_box(txt):
        if not txt or str(txt).strip() in ('','N/A','None'):
            return [Paragraph('NOT CAPTURED — data was not available at time of event.', S_MISS),
                    Spacer(1,3*mm)]
        raw  = str(txt).strip()
        # Split NOTAMs on ICAO NOTAM IDs
        notam_split = re.split(r'(?=\b[A-Z]\d{4}/\d{2}\b)', raw)
        entries = [e.strip() for e in notam_split if e.strip()]
        if len(entries) > 1:
            flowables = []
            for i, entry in enumerate(entries):
                bg = LGREY if i % 2 == 0 else colors.HexColor('#E8F0F8')
                p_sty = sty(f'n{i}', fontSize=7.5, textColor=BLACK, fontName='Courier',
                            leading=11, backColor=bg, borderPadding=(4,8,4,8))
                flowables.append(Paragraph('<br/>'.join(safe(entry).split('\n')), p_sty))
                if i < len(entries)-1:
                    flowables.append(HRFlowable(width='100%',thickness=0.5,
                                                color=colors.HexColor('#CBD5E1')))
            return flowables + [Spacer(1,3*mm)]
        lines  = safe(raw).split('\n')
        chunks = []
        for start in range(0, len(lines), 40):
            chunks.append(Paragraph('<br/>'.join(lines[start:start+40]), S_BOX_BODY))
        return chunks + [Spacer(1,3*mm)]

    # ── build story ────────────────────────────────────────────────────────
    story = []

    # Cover banner
    hdr_t = Table([[
        Paragraph('EU261 EVIDENCE PACK', S_TITLE),
        Paragraph(f'<b>{safe(case_ref)}</b><br/><font size="8">{safe(gen_str)}</font>',
                  sty('cr', fontSize=11, textColor=WHITE, fontName='Helvetica-Bold', alignment=2))
    ]], colWidths=[120*mm, 60*mm])
    hdr_t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),NAVY),
        ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
        ('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))
    story.append(hdr_t)

    # Score + suppression warning
    suppressed_count = len(hidden)
    badge_right = f'<b>Evidence: {ev_score}/{ev_max} ({ev_pct}%)</b>'
    if suppressed_count:
        badge_right += f'<br/><font size="7" color="#F59E0B">&#9888; {suppressed_count} section(s) suppressed</font>'
    badge_t = Table([[
        Paragraph('OCC Intelligence Platform  |  Confidential Legal Document', S_SUB),
        Paragraph(badge_right, sty('badge', fontSize=10, textColor=WHITE,
                                   fontName='Helvetica-Bold', alignment=2))
    ]], colWidths=[120*mm, 60*mm])
    badge_t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(0,0),colors.HexColor('#0D1F3C')),
        ('BACKGROUND',(1,0),(1,0),ev_col),
        ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
        ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))
    story.append(badge_t)
    story.append(Spacer(1,6*mm))

    # Flight details
    story += section_header('FLIGHT DETAILS')
    route_str = f'{origin} → {dest}' + (f' (diverted → {actual_dest})' if actual_dest != dest else '')

    # SI classification data for cover page
    _pdf_cause = getattr(log, 'si_cause_label', '') or 'Not classified'
    _pdf_prob_apt = getattr(log, 'si_problem_airport', '') or 'N/A'
    _pdf_focus = getattr(log, 'si_airport_focus', '') or 'N/A'
    _pdf_status = getattr(log, 'dossier_status', '') or 'CLOSED'

    story.append(kv_table([
        ('Flight',    flight),    ('Event Type', event_type),
        ('Date',      event_date),('Route',      route_str),
        ('Tail Reg',  tail),      ('BA Code',    ba_code),
        ('Logged By', logged_by), ('Timestamp',  ts_str),
        ('Cause',     _pdf_cause),
        ('Problem Airport', f'{_pdf_prob_apt} ({_pdf_focus})'),
        ('Dossier Status', _pdf_status),
    ]))
    story.append(Spacer(1,4*mm))

    # Evidence checklist — updated for 11 items
    story += section_header('EVIDENCE CHECKLIST')
    _has_tva = bool(getattr(log, 'taf_vs_actual', None) and getattr(log, 'taf_vs_actual', '') not in ('', '[]', 'null'))
    _has_sp = bool(getattr(log, 'station_picture', None) and getattr(log, 'station_picture', '') not in ('', '{}', 'null'))
    checklist = [
        ('metar_taf',     'METAR / Weather Snapshot',   log.weather_snap),
        ('metar_history', '12hr METAR History',          m_hist),
        ('notams',        'NOTAM Data',                  log.notam_snap),
        ('acars',         'ACARS Messages',               getattr(log,'acars_snap',None)),
        ('crosswind',     'Crosswind Assessment',         xw_snap if xw_snap != 'N/A' else None),
        ('conditions_evo','TAF vs Actual (Conditions)',   'yes' if _has_tva else None),
        ('ops_context',   'Operational Context',          'yes' if _has_sp else None),
        ('controller_log','Controller Log',               getattr(log,'logged_by',None)),
        (None,            'Controller Notes',             log.notes),
        (None,            'BA Delay Code',                log.ba_code),
        ('supporting_evidence','Supporting Evidence',     'yes' if extra_evidence else None),
    ]
    cl_data = [['', 'Evidence Item', 'Status', 'Visibility', 'Preview']]
    for key, label, val in checklist:
        present   = bool(val and str(val).strip() not in ('','N/A','None'))
        is_hidden = key in hidden if key else False
        tick      = '✓' if present else '✗'
        t_col     = '#10B981' if present else '#EF4444'
        vis_txt   = 'HIDDEN' if is_hidden else ('INCLUDED' if present else '—')
        vis_col   = '#F59E0B' if is_hidden else ('#10B981' if present else '#64748B')
        preview   = (str(val)[:50].replace('\n',' ')+'…') if val and present else 'Not captured'
        cl_data.append([
            Paragraph(f'<font color="{t_col}"><b>{tick}</b></font>',
                      sty('tk', fontSize=11, fontName='Helvetica-Bold', alignment=1)),
            Paragraph(safe(label), sty('cli', fontSize=9,
                      fontName='Helvetica-Bold' if present else 'Helvetica', textColor=BLACK)),
            Paragraph('CAPTURED' if present else 'MISSING',
                      sty('cls', fontSize=8, textColor=GREEN if present else RED,
                          fontName='Helvetica-Bold')),
            Paragraph(safe(vis_txt),
                      sty('vis', fontSize=8, textColor=colors.HexColor(vis_col),
                          fontName='Helvetica-Bold')),
            Paragraph(safe(preview), sty('clp', fontSize=8, textColor=DGREY)),
        ])
    cl_t = Table(cl_data, colWidths=[8*mm, 60*mm, 22*mm, 22*mm, 68*mm])
    cl_t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),NAVY),
        ('TEXTCOLOR',(0,0),(-1,0),WHITE),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,0),8),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[LGREY,WHITE]),
        ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
        ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))
    story.append(cl_t)
    story.append(Spacer(1,3*mm))
    story.append(Paragraph(
        'This document contains automatically captured operational data at the time of the '
        'recorded disruption. Intended for EU Regulation 261/2004 / UK261 proceedings. '
        'OCC Intelligence Platform — Confidential.',
        S_DISC))

    # ── SECTION 1: METAR / TAF ─────────────────────────────────────────────
    story.append(PageBreak())
    story += section_header('1. WEATHER — METAR & TAF')
    if 'metar_taf' in hidden:
        story += hidden_notice('metar_taf', '1. Weather (METAR/TAF)')
    else:
        story.append(kv_table([('Airport', dest), ('Captured at', ts_str)]))
        story.append(Spacer(1,3*mm))
        story.extend(content_box(
            f"--- METAR ---\n{log.weather_snap or 'Not captured'}"
            f"\n\n--- TAF ---\n{log.taf_snap or 'Not captured'}"))
        story += source_line('metar_taf')

    # ── SECTION 2: 12HR METAR HISTORY ──────────────────────────────────────
    story += section_header('2. 12HR METAR OBSERVATION HISTORY')
    if 'metar_history' in hidden:
        story += hidden_notice('metar_history', '2. 12hr METAR History')
    else:
        story.append(kv_table([
            ('Airport',     dest),
            ('Period',      'Last 12 hours at time of capture'),
            ('Observations',str(len(m_hist.splitlines())) + ' records' if m_hist else 'Not captured'),
        ]))
        story.append(Spacer(1,3*mm))
        story.extend(content_box(m_hist))
        story += source_line('metar_history')

    # ── SECTION 3: CROSSWIND ───────────────────────────────────────────────
    story.append(PageBreak())
    story += section_header('3. CROSSWIND ASSESSMENT')
    if 'crosswind' in hidden:
        story += hidden_notice('crosswind', '3. Crosswind Assessment')
    else:
        try:
            exceed = (xw_snap not in ('N/A','') and xw_limit not in ('N/A','')
                      and float(xw_snap.split()[0]) > float(xw_limit.split()[0]))
            exceed_txt = 'YES — EXCEEDS OPERATING LIMIT' if exceed else 'Within limits / see data'
        except Exception:
            exceed_txt = 'See data'
        story.append(kv_table([
            ('Aircraft Tail',    tail),
            ('Observed XW',      xw_snap),
            ('Aircraft Limit',   xw_limit),
            ('Limit Exceeded?',  exceed_txt),
        ]))
        story.append(Spacer(1,3*mm))
        xw_body = None if xw_snap == 'N/A' else (
            f"Observed crosswind: {xw_snap}\nAircraft crosswind limit: {xw_limit}\n"
            f"Exceedance: {exceed_txt}")
        story.extend(content_box(xw_body))
        story += source_line('crosswind')

    # ── SECTION 4: NOTAMS ──────────────────────────────────────────────────
    story.append(PageBreak())
    story += section_header('4. NOTAM DATA')
    if 'notams' in hidden:
        story += hidden_notice('notams', '4. NOTAM Data')
    else:
        story.append(kv_table([('Airport', dest), ('Captured at', ts_str)]))
        story.append(Spacer(1,3*mm))
        story.extend(content_box(log.notam_snap))
        story += source_line('notams')

    # ── SECTION 5: ACARS ───────────────────────────────────────────────────
    story.append(PageBreak())
    story += section_header('5. ACARS MESSAGES')
    if 'acars' in hidden:
        story += hidden_notice('acars', '5. ACARS Messages')
    else:
        story.append(kv_table([
            ('Flight', flight), ('Tail', tail), ('Window', 'Last 6hrs before event')]))
        story.append(Spacer(1,3*mm))
        story.extend(content_box(getattr(log,'acars_snap',None)))
        story += source_line('acars')

    # ── SECTION 6: CONDITIONS EVOLUTION (TAF vs ACTUAL) ────────────────────
    story.append(PageBreak())
    story += section_header('6. CONDITIONS EVOLUTION — TAF vs ACTUAL')
    if 'conditions_evo' in hidden:
        story += hidden_notice('conditions_evo', '6. Conditions Evolution (TAF vs Actual)')
    else:
        _si_cause_lbl = getattr(log, 'si_cause_label', '') or 'Not classified'
        _si_prob_apt = getattr(log, 'si_problem_airport', '') or dest
        _si_focus = getattr(log, 'si_airport_focus', '') or 'N/A'
        _doss_status = getattr(log, 'dossier_status', '') or 'CLOSED'
        _close_at = getattr(log, 'closed_at', None)
        _close_str = _close_at.strftime('%d %b %Y %H:%MZ') if _close_at else 'N/A'

        story.append(kv_table([
            ('Disruption Cause', _si_cause_lbl),
            ('Problem Airport', f'{_si_prob_apt} ({_si_focus})'),
            ('Dossier Status', _doss_status),
            ('Accumulation Closed', _close_str),
        ]))
        story.append(Spacer(1,3*mm))

        # METAR evolution table
        _me_raw = getattr(log, 'metar_evolution', '[]') or '[]'
        try:
            _me_list = json.loads(_me_raw)
        except Exception:
            _me_list = []

        _tva_raw = getattr(log, 'taf_vs_actual', '[]') or '[]'
        try:
            _tva_list = json.loads(_tva_raw)
        except Exception:
            _tva_list = []

        if _tva_list:
            tva_data = [['Time', 'Wind', 'Vis', 'Cloud', 'Deviation']]
            for _t in _tva_list:
                dev_txt = _t.get('deviation', '')
                dev_col = RED if dev_txt and dev_txt != 'WITHIN LIMITS' else GREEN
                tva_data.append([
                    Paragraph(safe(_t.get('ts', '')), sty('tt', fontSize=7, fontName='Courier', textColor=BLACK)),
                    Paragraph(safe(_t.get('actual_wind', '')), sty('tw', fontSize=7, fontName='Courier', textColor=BLACK)),
                    Paragraph(safe(_t.get('actual_vis', '')), sty('tv', fontSize=7, fontName='Courier', textColor=BLACK)),
                    Paragraph(safe(_t.get('actual_cloud', '')), sty('tc', fontSize=7, fontName='Courier', textColor=BLACK)),
                    Paragraph(safe(dev_txt), sty('td', fontSize=7, fontName='Helvetica-Bold', textColor=dev_col)),
                ])
            tva_t = Table(tva_data, colWidths=[36*mm, 36*mm, 28*mm, 36*mm, 44*mm])
            tva_t.setStyle(TableStyle([
                ('BACKGROUND',(0,0),(-1,0),NAVY),
                ('TEXTCOLOR',(0,0),(-1,0),WHITE),
                ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
                ('FONTSIZE',(0,0),(-1,0),8),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[LGREY,WHITE]),
                ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
                ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
                ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),
                ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ]))
            story.append(tva_t)
            # Deviation summary
            _devs = [t for t in _tva_list if t.get('deviation','') != 'WITHIN LIMITS']
            if _devs:
                story.append(Spacer(1,2*mm))
                story.append(Paragraph(
                    f'<b>&#9888; {len(_devs)} of {len(_tva_list)} observations showed conditions '
                    f'worse than forecast.</b> This demonstrates the disruption was caused by '
                    f'extraordinary meteorological conditions that exceeded the available forecast.',
                    sty('tvas', fontSize=9, textColor=RED, fontName='Helvetica-Bold')))
            else:
                story.append(Spacer(1,2*mm))
                story.append(Paragraph(
                    'Actual conditions matched or were better than forecast during the accumulation window.',
                    sty('tvas2', fontSize=9, textColor=GREEN, fontName='Helvetica')))
        elif _me_list:
            story.extend(content_box('\n'.join(
                f"{e.get('ts','')} [{e.get('icao','')}] {e.get('raw_metar','')}"
                for e in _me_list)))
        else:
            story.extend(content_box(None))

        # Auto-summary narrative
        _auto_summ = getattr(log, 'auto_summary', '') or ''
        if _auto_summ:
            story.append(Spacer(1,3*mm))
            story.append(Paragraph('<b>Auto-Generated Summary</b>', sty('ash', fontSize=10,
                                    textColor=CYAN, fontName='Helvetica-Bold')))
            story.append(Spacer(1,2*mm))
            story.append(Paragraph(safe(_auto_summ), sty('asbody', fontSize=9,
                                    textColor=BLACK, fontName='Helvetica', leading=13)))

        story.append(Spacer(1,3*mm))
        story += source_line('conditions_evo')

    # ── SECTION 7: OPERATIONAL CONTEXT ─────────────────────────────────────
    story.append(PageBreak())
    story += section_header('7. OPERATIONAL CONTEXT — STATION PICTURE')
    if 'ops_context' in hidden:
        story += hidden_notice('ops_context', '7. Operational Context (Station Picture)')
    else:
        _sp_raw = getattr(log, 'station_picture', None) or '{}'
        try:
            _sp = json.loads(_sp_raw)
        except Exception:
            _sp = {}

        if _sp:
            story.append(kv_table([
                ('Airport', _sp.get('airport', _si_prob_apt if '_si_prob_apt' in dir() else dest)),
                ('Window', _sp.get('window', 'N/A')),
                ('Total Arrivals', str(_sp.get('total_arrivals', 'N/A'))),
                ('Diversions', str(_sp.get('diversions', 'N/A'))),
                ('Avg Delay', _sp.get('avg_delay', 'N/A')),
            ]))
            story.append(Spacer(1,3*mm))
            if _sp.get('divert_details'):
                story.append(Paragraph('<b>Diversion Breakdown</b>', sty('dbd', fontSize=9,
                              textColor=CYAN, fontName='Helvetica-Bold')))
                story.extend(content_box(_sp['divert_details']))
        else:
            story.append(Paragraph(
                'Operational context data not yet available. This section auto-populates during '
                'the active accumulation window as the system tracks diversions and delays at '
                'the affected station.',
                sty('spna', fontSize=9, textColor=DGREY, fontName='Helvetica-Oblique')))
        story.append(Spacer(1,3*mm))
        story += source_line('ops_context')

    # ── SECTION 8: CONTROLLER LOG ──────────────────────────────────────────
    story.append(PageBreak())
    story += section_header('8. CONTROLLER DECISION LOG')
    if 'controller_log' in hidden:
        story += hidden_notice('controller_log', '8. Controller Decision Log')
    else:
        story.append(kv_table([
            ('Logged By', logged_by), ('BA Code', ba_code), ('Timestamp', ts_str)]))
        story.append(Spacer(1,3*mm))
        story.extend(content_box(
            f"Logged by: {logged_by}\nTimestamp: {ts_str}\nBA Delay Code: {ba_code}\n"
            f"\n--- Controller Notes ---\n{notes_txt}"))
        story += source_line('controller_log')

    # ── SECTION 9: SUPPORTING EVIDENCE ────────────────────────────────────
    if extra_evidence:
        story.append(PageBreak())
        story += section_header('9. SUPPORTING EVIDENCE — CONTROLLER ATTACHMENTS')
        if 'supporting_evidence' in hidden:
            story += hidden_notice('supporting_evidence', '9. Supporting Evidence')
        else:
            story.append(Paragraph(
                'The following items were manually attached to this case by the operations team.',
                sty('ei', fontSize=9, textColor=DGREY, fontName='Helvetica-Oblique')))
            story.append(Spacer(1,3*mm))
            for i, ev in enumerate(extra_evidence, 1):
                item_hdr = Table([[Paragraph(
                    f'<b>Item {i} — {safe(ev.get("filename","Unnamed"))}</b>  '
                    f'<font size="8" color="#64748B">Added {safe(ev.get("timestamp",""))}'
                    f' by {safe(ev.get("added_by",""))}</font>',
                    sty('evhdr', fontSize=10, textColor=WHITE, fontName='Helvetica-Bold'))]],
                    colWidths=[180*mm])
                item_hdr.setStyle(TableStyle([
                    ('BACKGROUND',(0,0),(-1,-1),BLUE2),
                    ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
                    ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
                ]))
                story.append(item_hdr)
                story.append(Spacer(1,1*mm))
                raw_text = ev.get('content_text') or '(no text content)'
                lines = raw_text.split('\n')
                header_lines, body_lines, in_body = [], [], False
                for ln in lines:
                    if not in_body and re.match(
                            r'^(From|To|Subject|Date|Cc|Bcc|Sent|QU |\.QU|ANPOC).*', ln, re.I):
                        header_lines.append(ln)
                    elif not in_body and ln.strip() == '':
                        in_body = True
                    else:
                        in_body = True; body_lines.append(ln)
                if header_lines:
                    hdr_rows = []
                    for hl in header_lines:
                        if ':' in hl:
                            k, _, v = hl.partition(':')
                            hdr_rows.append([Paragraph(safe(k.strip()), S_LABEL),
                                             Paragraph(safe(v.strip()), S_MONO_SM)])
                        else:
                            hdr_rows.append([Paragraph('', S_LABEL),
                                             Paragraph(safe(hl), S_MONO_SM)])
                    if hdr_rows:
                        hkv = Table(hdr_rows, colWidths=[30*mm, 150*mm])
                        hkv.setStyle(TableStyle([
                            ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#EFF6FF')),
                            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
                            ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
                            ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),
                            ('VALIGN',(0,0),(-1,-1),'TOP'),
                        ]))
                        story.append(hkv)
                        story.append(Spacer(1,1*mm))
                    body_txt = '\n'.join(body_lines).strip() or raw_text
                else:
                    body_txt = raw_text
                story.extend(content_box(body_txt))
                story.append(Spacer(1,4*mm))
            story += source_line('supporting_evidence')
        sec_final = 10
    else:
        sec_final = 9

    # ── SECTION AUDIT TRAIL ────────────────────────────────────────────────
    story.append(PageBreak())
    story += section_header(f'{sec_final}. EVIDENCE SOURCES & AUDIT TRAIL')

    # Source attribution table
    story.append(Paragraph('<b>Evidence Sources</b>', sty('srch', fontSize=10,
                            textColor=CYAN, fontName='Helvetica-Bold')))
    story.append(Spacer(1,3*mm))
    src_data = [['Section', 'Data Source']]
    for key, lbl in EU261_SECTIONS:
        if key in ('supporting_evidence',) and not extra_evidence:
            continue
        src_txt = EVIDENCE_SOURCES.get(key, 'OCC Intelligence Platform')
        src_data.append([
            Paragraph(safe(lbl), sty('slbl', fontSize=8, fontName='Helvetica-Bold', textColor=BLACK)),
            Paragraph(safe(src_txt), sty('stxt', fontSize=8, fontName='Helvetica', textColor=BLACK)),
        ])
    src_t = Table(src_data, colWidths=[55*mm, 125*mm])
    src_t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),NAVY),
        ('TEXTCOLOR',(0,0),(-1,0),WHITE),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,0),8),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[LGREY,WHITE]),
        ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
        ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
    ]))
    story.append(src_t)
    story.append(Spacer(1,6*mm))

    # Section modification audit trail
    story.append(Paragraph('<b>Section Modification Audit Log</b>',
                            sty('audh', fontSize=10, textColor=CYAN, fontName='Helvetica-Bold')))
    story.append(Spacer(1,3*mm))
    if audit:
        aud_data = [['Timestamp', 'User', 'Section', 'Action', 'Portal']]
        for entry in audit:
            sec_label = next((lbl for k,lbl in EU261_SECTIONS if k==entry.get('section')),
                             entry.get('section','?'))
            aud_data.append([
                Paragraph(safe(entry.get('timestamp','')),
                          sty('at', fontSize=8, fontName='Courier', textColor=BLACK)),
                Paragraph(safe(entry.get('user','')),
                          sty('au', fontSize=8, fontName='Helvetica-Bold', textColor=BLACK)),
                Paragraph(safe(sec_label),
                          sty('as', fontSize=8, fontName='Helvetica', textColor=BLACK)),
                Paragraph(safe(entry.get('action','').upper()),
                          sty('aa', fontSize=8, fontName='Helvetica-Bold',
                              textColor=RED if entry.get('action')=='hide' else GREEN)),
                Paragraph('Legal Portal' if entry.get('portal') else 'OCC Dashboard',
                          sty('ap', fontSize=7, fontName='Helvetica', textColor=DGREY)),
            ])
        aud_t = Table(aud_data, colWidths=[38*mm, 28*mm, 55*mm, 20*mm, 39*mm])
        aud_t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),NAVY),
            ('TEXTCOLOR',(0,0),(-1,0),WHITE),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,0),8),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[LGREY,WHITE]),
            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CBD5E1')),
            ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ]))
        story.append(aud_t)
        story.append(Spacer(1,4*mm))
        story.append(Paragraph(
            f'This audit log records all modifications to section visibility for case {case_ref}. '
            f'Suppressed sections are retained in the OCC system and available for disclosure on request. '
            f'Any suppression action is the sole responsibility of the named user.',
            S_DISC))
    else:
        story.append(Paragraph('No section modifications recorded — all sections included as captured.',
                                sty('noaud', fontSize=9, textColor=GREEN,
                                    fontName='Helvetica-BoldOblique')))

    # ── CASE SUMMARY & SIGNATURE ───────────────────────────────────────────
    story.append(PageBreak())
    story += section_header(f'{sec_final + 1}. CASE SUMMARY & SIGNATURE')
    story.append(kv_table([
        ('Case Reference', case_ref),    ('Flight',    flight),
        ('Date',          event_date),   ('Event Type',event_type),
        ('Route',         route_str),    ('Tail Reg',  tail),
        ('Evidence Score',f'{ev_score}/{ev_max} ({ev_pct}%)'),
        ('Status', 'COMPLETE' if ev_pct >= 80 else 'PARTIAL — see evidence checklist'),
        ('Sections Suppressed', str(suppressed_count) if suppressed_count else 'None'),
    ]))
    story.append(Spacer(1,8*mm))
    sig_t = Table([
        [Paragraph('Controller Signature:', S_LABEL), Paragraph('_'*60, S_VALUE), '', ''],
        [Paragraph('Date:', S_LABEL),                 Paragraph('_'*40, S_VALUE), '', ''],
    ], colWidths=[40*mm, 80*mm, 10*mm, 50*mm])
    sig_t.setStyle(TableStyle([
        ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    story.append(sig_t)
    story.append(Spacer(1,4*mm))
    story.append(Paragraph(
        f'I confirm that the above information accurately reflects the operational circumstances '
        f'at the time of the recorded disruption. Case reference: {case_ref}. '
        f'Generated by OCC Intelligence Platform (Halo) on {gen_str}.',
        S_DISC))

    doc.build(story)
    buf.seek(0)
    return buf


@app.route('/api/dossier/<path:log_id>/pdf')
@login_required
def dossier_pdf(log_id):
    """Download EU261 evidence PDF for a dossier."""
    try:
        import traceback
        log = db.session.get(DisruptionLog, log_id)
        if not log:
            return Response('Dossier not found', status=404, mimetype='text/plain')
        if not HAS_PDF:
            return Response(
                'PDF library not installed — add reportlab to requirements.txt',
                status=500, mimetype='text/plain')
        # Load supporting evidence
        extra_list = []
        try:
            evs = CaseEvidence.query.filter_by(log_id=log_id).order_by(CaseEvidence.id).all()
            extra_list = [{'filename': e.filename, 'content_text': e.content_text,
                           'timestamp': e.timestamp.strftime('%d %b %Y %H:%MZ') if e.timestamp else '',
                           'added_by': e.added_by or ''} for e in evs]
        except Exception:
            pass
        buf = _build_evidence_pdf(log, extra_evidence=extra_list or None)
        if not buf:
            return Response('PDF generation failed', status=500, mimetype='text/plain')
        case_ref = (getattr(log,'case_ref',None) or log_id).replace('/', '-')
        return Response(buf.read(), mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="EU261_{case_ref}.pdf"'})
    except Exception:
        import traceback
        return Response(f'PDF error: {traceback.format_exc()}', status=500, mimetype='text/plain')


# ── LEGAL PORTAL ─────────────────────────────────────────────────────────

LEGAL_PORTAL_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCC Legal Portal</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0A1628;color:#E8EEF4;font-family:Arial,sans-serif;min-height:100vh}
.topbar{background:#0D1F3C;border-bottom:2px solid #0EA5E9;padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
.topbar h1{font-size:18px;color:#0EA5E9;letter-spacing:1px}
.topbar .meta{font-size:11px;color:#64748B}
.controls{padding:16px 24px;background:#0A1628;border-bottom:1px solid #1E3A5F;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.controls input,.controls select{background:#112240;border:1px solid #1E3A5F;color:#E8EEF4;padding:7px 12px;border-radius:4px;font-size:13px}
.controls input:focus,.controls select:focus{outline:none;border-color:#0EA5E9}
.btn{padding:7px 16px;border-radius:4px;border:none;cursor:pointer;font-size:13px;font-weight:bold}
.btn-cyan{background:#0EA5E9;color:#0A1628}
.btn-grey{background:#1E3A5F;color:#E8EEF4}
.btn-amber{background:#F59E0B;color:#0A1628}
.btn-red{background:#EF4444;color:#fff}
.table-wrap{padding:16px 24px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#112240;color:#0EA5E9;padding:10px 12px;text-align:left;font-size:11px;letter-spacing:1px;border-bottom:1px solid #1E3A5F}
td{padding:9px 12px;border-bottom:1px solid #1E3A5F;color:#E8EEF4;vertical-align:top}
tr:hover td{background:#112240;cursor:pointer}
.badge{display:inline-block;padding:2px 8px;border-radius:3px;font-size:10px;font-weight:bold}
.ev-bar{height:6px;border-radius:3px;background:#1E3A5F;width:80px;display:inline-block;vertical-align:middle;margin-right:6px}
.ev-fill{height:6px;border-radius:3px}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;overflow:auto}
.modal.open{display:flex;align-items:flex-start;justify-content:center;padding:30px 10px}
.modal-box{background:#0D1F3C;border:1px solid #1E3A5F;border-radius:6px;width:100%;max-width:860px;max-height:85vh;overflow-y:auto}
.modal-hdr{background:#112240;padding:14px 18px;border-bottom:1px solid #1E3A5F;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0}
.modal-hdr h2{font-size:15px;color:#0EA5E9}
.modal-body{padding:18px}
.section-hdr{background:#0EA5E9;color:#0A1628;padding:5px 12px;font-size:11px;font-weight:bold;letter-spacing:1px;margin:12px -18px 8px;border-left:none}
.evidence-row{display:flex;gap:8px;padding:6px 0;border-bottom:1px solid #1E3A5F;align-items:flex-start}
.evidence-label{min-width:160px;font-size:11px;color:#64748B;font-weight:bold}
.evidence-val{font-size:12px;color:#E8EEF4;font-family:monospace;white-space:pre-wrap;word-break:break-all}
.kv-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.kv-item{background:#112240;border-radius:4px;padding:8px 12px}
.kv-label{font-size:10px;color:#64748B;font-weight:bold;letter-spacing:0.5px;margin-bottom:2px}
.kv-value{font-size:13px;color:#E8EEF4}
.empty{text-align:center;padding:40px;color:#64748B;font-size:14px}
#login-page{display:flex;align-items:center;justify-content:center;min-height:100vh;flex-direction:column;gap:20px}
.login-box{background:#0D1F3C;border:1px solid #1E3A5F;border-radius:8px;padding:32px;width:320px}
.login-box h2{color:#0EA5E9;margin-bottom:20px;font-size:16px;letter-spacing:1px}
.login-box input{width:100%;background:#112240;border:1px solid #1E3A5F;color:#E8EEF4;padding:10px;border-radius:4px;margin-bottom:12px;font-size:14px}
.login-box .err{color:#EF4444;font-size:12px;margin-top:8px}
</style>
</head>
<body>
<div id="login-page" style="display:{{login_display}}">
  <div class="login-box">
    <h2>OCC LEGAL PORTAL</h2>
    <p style="color:#64748B;font-size:12px;margin-bottom:16px">Enter the legal access password to continue.</p>
    <input type="password" id="lp-pwd" placeholder="Password" onkeydown="if(event.key==='Enter')legalLogin()">
    <button class="btn btn-cyan" style="width:100%;padding:10px" onclick="legalLogin()">SIGN IN</button>
    <div id="lp-err" class="err"></div>
  </div>
</div>

<div id="main-page" style="display:{{main_display}}">
<div class="topbar">
  <h1>⚖️ OCC LEGAL PORTAL — EU261 EVIDENCE</h1>
  <div class="meta">Read-only access &nbsp;|&nbsp; <a href="/legal/logout" style="color:#0EA5E9">Sign Out</a></div>
</div>
<div class="controls">
  <input type="text" id="f-flight" placeholder="Flight e.g. BA1234" oninput="applyFilters()" style="width:160px">
  <input type="text" id="f-date"   placeholder="Date e.g. 2025-03-01" oninput="applyFilters()" style="width:160px">
  <input type="text" id="f-route"  placeholder="Route e.g. LCY-GLA" oninput="applyFilters()" style="width:140px">
  <select id="f-score" onchange="applyFilters()">
    <option value="">All completeness</option>
    <option value="high">Complete (80%+)</option>
    <option value="mid">Partial (50-79%)</option>
    <option value="low">Incomplete (&lt;50%)</option>
  </select>
  <button class="btn btn-grey" onclick="loadDossiers()">🔄 REFRESH</button>
  <span id="case-count" style="font-size:12px;color:#64748B;margin-left:auto"></span>
</div>

<div class="table-wrap">
<table>
  <thead><tr>
    <th>CASE REF</th><th>FLIGHT</th><th>DATE</th><th>ROUTE</th>
    <th>EVENT</th><th>EVIDENCE</th><th>LOGGED BY</th><th>ACTIONS</th>
  </tr></thead>
  <tbody id="dossier-tbody"><tr><td colspan="8" class="empty">Loading cases…</td></tr></tbody>
</table>
</div>
</div>

<!-- Section Manager Modal -->
<div class="modal" id="legal-section-modal">
<div class="modal-box">
  <div class="modal-hdr">
    <h2 id="lsm-title" style="font-size:15px;">🗂 MANAGE PDF SECTIONS</h2>
    <div style="display:flex;gap:8px;align-items:center;">
      <a id="lsm-pdf-btn" href="#" target="_blank" class="btn btn-amber"
         style="font-size:12px;text-decoration:none;padding:5px 12px;">⬇ DOWNLOAD PDF</a>
      <button class="btn btn-grey" style="font-size:12px;padding:5px 12px" onclick="closeLsm()">✕ CLOSE</button>
    </div>
  </div>
  <div class="modal-body">
    <div style="background:rgba(14,165,233,0.08);border:1px solid rgba(14,165,233,0.2);
                border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:12px;color:#94a3b8;">
      ℹ️ <b style="color:#e8eef4;">Hidden sections are not deleted</b> — data is retained and available
      on request. All changes are recorded in the audit log below.
    </div>
    <div id="lsm-sections" style="margin-bottom:16px;"></div>
    <div style="border-top:1px solid #1E3A5F;padding-top:12px;">
      <div style="font-size:11px;font-weight:700;color:#0EA5E9;text-transform:uppercase;
                  letter-spacing:0.5px;margin-bottom:8px;">📋 Audit Trail</div>
      <div id="lsm-audit" style="background:#0A1628;border-radius:6px;border:1px solid #1E3A5F;
                                  min-height:40px;overflow:hidden;"></div>
    </div>
  </div>
</div>
</div>

<!-- Case Detail Modal -->
<div class="modal" id="case-modal">
<div class="modal-box">
  <div class="modal-hdr">
    <h2 id="modal-title">Evidence Pack</h2>
    <div style="display:flex;gap:8px">
      <a id="modal-pdf-btn" href="#" target="_blank" class="btn btn-amber" style="font-size:12px;text-decoration:none;padding:5px 12px">⬇ PDF</a>
      <button class="btn btn-grey" style="font-size:12px;padding:5px 12px" onclick="closeModal()">✕ CLOSE</button>
    </div>
  </div>
  <div class="modal-body" id="modal-body"></div>
</div>
</div>

<script>
let allDossiers = [];

function legalLogin() {
  const pwd = document.getElementById('lp-pwd').value;
  fetch('/legal/auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pwd})})
    .then(r=>r.json()).then(d=>{
      if(d.ok) { location.reload(); }
      else { document.getElementById('lp-err').textContent = 'Incorrect password.'; }
    });
}

function loadDossiers() {
  fetch('/api/legal/dossiers')
    .then(r=>r.json()).then(data=>{
      if(data.error) { document.getElementById('dossier-tbody').innerHTML=`<tr><td colspan="8" class="empty">${data.error}</td></tr>`; return; }
      allDossiers = data;
      applyFilters();
    }).catch(()=>{
      document.getElementById('dossier-tbody').innerHTML='<tr><td colspan="8" class="empty">Session expired — please refresh and sign in again.</td></tr>';
    });
}

function applyFilters() {
  const ff = document.getElementById('f-flight').value.toUpperCase();
  const fd = document.getElementById('f-date').value;
  const fr = document.getElementById('f-route').value.toUpperCase();
  const fs = document.getElementById('f-score').value;
  let rows = allDossiers.filter(d => {
    if(ff && !d.flight.includes(ff)) return false;
    if(fd && !d.date.includes(fd)) return false;
    if(fr && !(d.origin+d.dest).includes(fr.replace('-',''))) return false;
    if(fs==='high' && d.ev_score/d.ev_max < 0.8) return false;
    if(fs==='mid' && (d.ev_score/d.ev_max >= 0.8 || d.ev_score/d.ev_max < 0.5)) return false;
    if(fs==='low' && d.ev_score/d.ev_max >= 0.5) return false;
    return true;
  });
  document.getElementById('case-count').textContent = `${rows.length} case${rows.length!==1?'s':''} shown`;
  const pct = d => Math.round(d.ev_score/d.ev_max*100);
  const col = d => pct(d)>=80?'#10B981':pct(d)>=50?'#F59E0B':'#EF4444';
  const evBadge = d => `<div class="ev-bar"><div class="ev-fill" style="width:${pct(d)}%;background:${col(d)}"></div></div><span style="font-size:11px;color:${col(d)};font-weight:bold">${d.ev_score}/${d.ev_max} (${pct(d)}%)</span>`;
  const evTypeBadge = t => {
    const c = {CANCELLATION:'#EF4444',DIVERSION:'#8B5CF6',DELAY:'#F59E0B'}[t]||'#64748B';
    return `<span class="badge" style="background:${c}20;color:${c};border:1px solid ${c}">${t}</span>`;
  };
  if(!rows.length){ document.getElementById('dossier-tbody').innerHTML='<tr><td colspan="8" class="empty">No cases match your filters.</td></tr>'; return; }
  document.getElementById('dossier-tbody').innerHTML = rows.map(d=>`
    <tr onclick="openCase('${d.id}')">
      <td><b style="color:#0EA5E9">${d.case_ref}</b></td>
      <td>${d.flight}</td>
      <td>${d.date}</td>
      <td>${d.origin} → ${d.dest||'?'}</td>
      <td>${evTypeBadge(d.event_type)}</td>
      <td>${evBadge(d)}</td>
      <td style="font-size:11px;color:#64748B">${d.logged_by||'AUTO'}</td>
      <td onclick="event.stopPropagation()" style="white-space:nowrap">
        <button class="btn btn-grey" style="font-size:10px;padding:4px 8px;margin-right:4px" onclick="openLegalSectionManager('${d.id}','${d.flight}')">🗂</button>
        <a href="/api/dossier/${d.id}/pdf" target="_blank" class="btn btn-amber" style="font-size:10px;padding:4px 8px;text-decoration:none">⬇ PDF</a>
      </td>
    </tr>`).join('');
}

function openCase(id) {
  const d = allDossiers.find(x=>x.id===id);
  if(!d) return;
  const pct = Math.round(d.ev_score/d.ev_max*100);
  const col = pct>=80?'#10B981':pct>=50?'#F59E0B':'#EF4444';
  document.getElementById('modal-title').textContent = `Case ${d.case_ref} — ${d.flight} — ${d.date}`;
  document.getElementById('modal-pdf-btn').href = `/api/dossier/${d.id}/pdf`;

  const ev = (label, val) => `
    <div class="evidence-row">
      <div class="evidence-label">${label}</div>
      <div class="evidence-val" style="color:${val&&val!=='N/A'?'#E8EEF4':'#EF4444'}">${val||'NOT CAPTURED'}</div>
    </div>`;

  document.getElementById('modal-body').innerHTML = `
    <div class="kv-grid">
      <div class="kv-item"><div class="kv-label">FLIGHT</div><div class="kv-value">${d.flight}</div></div>
      <div class="kv-item"><div class="kv-label">DATE</div><div class="kv-value">${d.date}</div></div>
      <div class="kv-item"><div class="kv-label">ROUTE</div><div class="kv-value">${d.origin} → ${d.dest||'?'}</div></div>
      <div class="kv-item"><div class="kv-label">EVENT</div><div class="kv-value">${d.event_type}</div></div>
      <div class="kv-item"><div class="kv-label">TAIL</div><div class="kv-value">${d.tail||'N/A'}</div></div>
      <div class="kv-item"><div class="kv-label">BA CODE</div><div class="kv-value">${d.ba_code||'N/A'}</div></div>
      <div class="kv-item"><div class="kv-label">LOGGED BY</div><div class="kv-value">${d.logged_by||'AUTO-OCC'}</div></div>
      <div class="kv-item" style="background:#0D2E1C;border:1px solid ${col}"><div class="kv-label" style="color:${col}">EVIDENCE</div><div class="kv-value" style="color:${col};font-weight:bold">${d.ev_score}/${d.ev_max} (${pct}%)</div></div>
    </div>
    <div class="section-hdr">✈️ AIRFIELD OPERATIONAL LIMITS — ${d.dest||'DEST'}</div>
    <div id="legal-af-limits-${id.replace(/[^a-z0-9]/gi,'_')}" style="color:#64748B;font-size:12px;padding:8px 0;">Loading airfield data…</div>
    <div class="section-hdr">WEATHER & TAF ${d.hidden_sections&&d.hidden_sections.includes('metar_taf')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${ev('METAR at decision time', d.weather_snap)}
    ${ev('TAF', d.taf_snap)}
    <div class="section-hdr">12HR METAR OBSERVATION HISTORY ${d.hidden_sections&&d.hidden_sections.includes('metar_history')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${d.metar_history && d.metar_history !== 'N/A'
      ? `<pre style="margin:0;font-size:10px;color:#E8EEF4;white-space:pre-wrap;
                     max-height:200px;overflow-y:auto;background:#0A1628;
                     padding:8px;border-radius:4px;border-left:3px solid #0EA5E9;
                     margin-bottom:8px;">${d.metar_history.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`
      : `<div style="color:#64748B;font-size:11px;font-style:italic;margin-bottom:8px;">
             Not captured on this dossier.</div>`
    }
    <div class="section-hdr">CROSSWIND ${d.hidden_sections&&d.hidden_sections.includes('crosswind')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${ev('Observed Crosswind', d.xw_snap)}
    ${ev('Aircraft Limit', d.xw_limit)}
    <div class="section-hdr">NOTAMs ${d.hidden_sections&&d.hidden_sections.includes('notams')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${ev('Relevant NOTAMs', d.notam_snap)}
    <div class="section-hdr">ACARS MESSAGES ${d.hidden_sections&&d.hidden_sections.includes('acars')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${ev('ACARS Log', d.acars_snap)}
    <div class="section-hdr">🧠 SI CLASSIFICATION</div>
    ${ev('Disruption Cause', d.si_cause_label || 'Not classified')}
    ${ev('Problem Airport', (d.si_problem_airport||'N/A') + ' (' + (d.si_airport_focus||'N/A') + ')')}
    ${ev('Dossier Status', d.dossier_status || 'CLOSED')}
    <div class="section-hdr">📈 CONDITIONS EVOLUTION ${d.hidden_sections&&d.hidden_sections.includes('conditions_evo')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${d.auto_summary ? ev('Auto-Summary', d.auto_summary) : ev('Summary', 'Not yet generated — accumulation may still be active.')}
    <div class="section-hdr">CONTROLLER NOTES ${d.hidden_sections&&d.hidden_sections.includes('controller_log')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    ${ev('Notes', d.notes)}
    <div class="section-hdr">📎 SUPPORTING EVIDENCE ${d.hidden_sections&&d.hidden_sections.includes('supporting_evidence')?'<span style="color:#f59e0b;font-size:10px;margin-left:8px;">🚫 HIDDEN FROM PDF</span>':''}</div>
    <div id="legal-evidence-${id.replace(/[^a-z0-9]/gi,'_')}" style="color:#64748B;font-size:12px;padding:4px 0;">Loading evidence…</div>
    <div class="section-hdr">TIMESTAMPS</div>
    ${ev('Logged At', d.timestamp_full)}
  `;
  document.getElementById('case-modal').classList.add('open');

  // Async load supporting evidence
  fetch('/api/legal/evidence/'+encodeURIComponent(d.id))
    .then(r=>r.json()).then(items=>{
      const el = document.getElementById('legal-evidence-'+id.replace(/[^a-z0-9]/gi,'_'));
      if(!el) return;
      if(!items.length) { el.innerHTML='<span style="color:#64748B;font-size:11px;">No supporting evidence attached.</span>'; return; }
      el.innerHTML = items.map(e=>`
        <div style="background:#112240;border-radius:4px;padding:8px 10px;margin-bottom:6px;border-left:2px solid #0EA5E9;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="color:#0EA5E9;font-size:11px;font-weight:bold;">${e.filename||'Evidence'}</span>
            <span style="color:#64748B;font-size:10px;">${e.timestamp||''} ${e.added_by?'— '+e.added_by:''}</span>
          </div>
          ${e.content_text?`<pre style="margin:0;font-size:10px;color:#E8EEF4;white-space:pre-wrap;max-height:150px;overflow-y:auto;background:#0A1628;padding:6px;border-radius:3px;">${e.content_text.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`:'<span style="color:#64748B;font-size:10px;">[File attachment]</span>'}
        </div>`).join('');
    }).catch(()=>{ const el=document.getElementById('legal-evidence-'+id.replace(/[^a-z0-9]/gi,'_')); if(el) el.innerHTML='<span style="color:#EF4444">Could not load evidence.</span>'; });

  // Async load airfield limits
  if(d.dest) {
    fetch('/api/airfield_limits?iata='+d.dest)
      .then(r=>r.json()).then(af=>{
        const el = document.getElementById('legal-af-limits-'+id.replace(/[^a-z0-9]/gi,'_'));
        if(!el) return;
        if(!af.found) { el.innerHTML='<span style="color:#64748B">No specific operational limits on record for '+d.dest+'. Standard procedures apply.</span>'; return; }
        const catCol = af.cat&&af.cat.includes('C')?'#EF4444':af.cat&&af.cat.includes('B')?'#F59E0B':'#10B981';
        el.innerHTML = `
          <div style="border:1px solid ${catCol}40;border-radius:6px;padding:12px;margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
              <b style="color:#E8EEF4;font-size:13px;">${af.name}</b>
              <span style="background:${catCol};color:#0A1628;padding:2px 8px;border-radius:3px;font-size:10px;font-weight:bold;">${af.cat}</span>
            </div>
            <p style="color:#94A3B8;margin:0 0 8px;font-size:11px;line-height:1.5;">${af.notes||''}</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px;">
              <div style="background:#112240;border-radius:4px;padding:7px 10px;">
                <div style="font-size:9px;color:#0EA5E9;font-weight:bold;margin-bottom:2px;">CROSSWIND LIMIT</div>
                <div style="color:#E8EEF4;font-weight:bold;">${af.xw_limit||'—'}</div>
              </div>
              <div style="background:#112240;border-radius:4px;padding:7px 10px;">
                <div style="font-size:9px;color:#F59E0B;font-weight:bold;margin-bottom:2px;">TAILWIND LIMIT</div>
                <div style="color:#E8EEF4;font-weight:bold;">${af.tailwind||'—'}</div>
              </div>
              <div style="background:#112240;border-radius:4px;padding:7px 10px;">
                <div style="font-size:9px;color:#94A3B8;font-weight:bold;margin-bottom:2px;">VIS MINIMUM</div>
                <div style="color:#E8EEF4;font-size:11px;">${af.vis_min||'—'}</div>
              </div>
              <div style="background:#112240;border-radius:4px;padding:7px 10px;">
                <div style="font-size:9px;color:#94A3B8;font-weight:bold;margin-bottom:2px;">OPS HOURS</div>
                <div style="color:#E8EEF4;font-size:11px;">${af.ops_hours||'—'}</div>
              </div>
            </div>
            ${af.special&&af.special.length?`
            <div style="font-size:9px;color:#0EA5E9;font-weight:bold;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">⚠️ SPECIAL CONSIDERATIONS FOR LEGAL CONTEXT</div>
            <ul style="margin:0;padding-left:16px;color:#94A3B8;font-size:11px;line-height:1.8;">
              ${af.special.map(s=>`<li>${s}</li>`).join('')}
            </ul>`:''}
            ${af.ppr_required?`<div style="margin-top:8px;background:#1a0000;border:1px solid #EF4444;border-radius:4px;padding:6px 10px;font-size:11px;color:#EF4444;font-weight:bold;">🔒 PPR REQUIRED at this airport</div>`:''}
          </div>`;
      }).catch(()=>{ const el=document.getElementById('legal-af-limits-'+id.replace(/[^a-z0-9]/gi,'_')); if(el) el.innerHTML='<span style="color:#64748B">Could not load airfield data.</span>'; });
  }
}

function closeModal() { document.getElementById('case-modal').classList.remove('open'); }

// ── Section manager for legal portal ─────────────────────────────────────
let _lsmLogId = null;

function openLegalSectionManager(logId, flight) {
  _lsmLogId = logId;
  document.getElementById('lsm-title').textContent = `🗂 SECTIONS — ${flight || logId}`;
  document.getElementById('lsm-pdf-btn').href = `/api/dossier/${encodeURIComponent(logId)}/pdf`;
  document.getElementById('lsm-sections').innerHTML = '<div style="color:#64748B;padding:10px;font-size:12px;">Loading…</div>';
  document.getElementById('lsm-audit').innerHTML = '';
  document.getElementById('legal-section-modal').classList.add('open');
  _loadLsmState(logId);
}

function closeLsm() {
  document.getElementById('legal-section-modal').classList.remove('open');
}

function _loadLsmState(logId) {
  fetch(`/api/legal/dossier/${encodeURIComponent(logId)}/sections`)
    .then(r => r.json()).then(data => {
      _renderLsmSections(data, logId);
      _renderLsmAudit(data.audit || []);
    }).catch(() => {
      document.getElementById('lsm-sections').innerHTML =
        '<div style="color:#EF4444;padding:10px;font-size:12px;">Failed to load sections.</div>';
    });
}

function _renderLsmSections(data, logId) {
  const sections = data.sections || [];
  let html = '<div style="font-size:11px;font-weight:700;color:#0EA5E9;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">PDF Sections</div>';
  sections.forEach(sec => {
    const isHidden = sec.hidden;
    html += `<div style="display:flex;align-items:center;justify-content:space-between;
                 padding:8px 10px;margin-bottom:4px;border-radius:6px;
                 background:${isHidden ? 'rgba(245,158,11,0.08)' : 'rgba(14,165,233,0.07)'};
                 border:1px solid ${isHidden ? 'rgba(245,158,11,0.3)' : 'rgba(14,165,233,0.15)'};">
      <div style="flex:1;">
        <div style="font-size:12px;font-weight:600;color:${isHidden ? '#f59e0b' : '#e8eef4'};">
          ${isHidden ? '🚫' : '✅'} ${sec.label}
        </div>
        <div style="font-size:10px;color:#64748B;font-style:italic;margin-top:2px;">${sec.source||''}</div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;margin-left:12px;">
        <span style="font-size:10px;font-weight:700;color:${isHidden?'#f59e0b':'#10b981'};">
          ${isHidden ? 'HIDDEN' : 'INCLUDED'}
        </span>
        ${isHidden
          ? `<button class="btn btn-grey" style="font-size:11px;padding:3px 10px;background:rgba(16,185,129,0.2);color:#10b981;border:1px solid rgba(16,185,129,0.4);"
                     onclick="lsmToggle('${logId}','${sec.key}','show')">✅ INCLUDE</button>`
          : `<button class="btn btn-grey" style="font-size:11px;padding:3px 10px;background:rgba(245,158,11,0.2);color:#f59e0b;border:1px solid rgba(245,158,11,0.4);"
                     onclick="lsmToggle('${logId}','${sec.key}','hide')">🚫 HIDE</button>`
        }
      </div>
    </div>`;
  });
  const hiddenCount = sections.filter(s => s.hidden).length;
  html += `<div style="font-size:11px;color:#64748B;margin-top:10px;padding:6px 10px;background:rgba(0,0,0,0.2);border-radius:4px;">
    ${hiddenCount === 0
      ? '✅ All sections included in PDF'
      : `⚠️ <b style="color:#f59e0b;">${hiddenCount} section${hiddenCount>1?'s':''} hidden from PDF</b>`}
  </div>`;
  document.getElementById('lsm-sections').innerHTML = html;
}

function _renderLsmAudit(audit) {
  const el = document.getElementById('lsm-audit');
  if (!audit || !audit.length) {
    el.innerHTML = '<div style="padding:10px;font-size:11px;color:#10B981;font-style:italic;">No modifications recorded.</div>';
    return;
  }
  let html = `<div style="display:grid;grid-template-columns:110px 80px 1fr 55px;gap:4px;
                           padding:6px 8px;background:#0D2240;font-size:10px;font-weight:700;
                           color:#0EA5E9;font-family:Arial,sans-serif;">
    <span>TIMESTAMP</span><span>USER</span><span>SECTION</span><span>ACTION</span></div>`;
  [...audit].reverse().forEach(e => {
    const isHide = e.action === 'hide';
    const portal = e.portal ? ' (Legal)' : ' (OCC)';
    html += `<div style="display:grid;grid-template-columns:110px 80px 1fr 55px;gap:4px;
                         padding:5px 8px;border-bottom:1px solid #1E3A5F;font-size:10px;
                         font-family:'Courier New',monospace;">
      <span style="color:#94a3b8;">${e.timestamp||'—'}</span>
      <span style="color:#e8eef4;font-weight:600;">${e.user||'—'}</span>
      <span style="color:#cbd5e1;">${e.section||'—'}</span>
      <span style="color:${isHide?'#f59e0b':'#10b981'};font-weight:700;">
        ${isHide?'🚫 HIDE':'✅ SHOW'}<span style="font-size:9px;color:#64748B;">${portal}</span>
      </span>
    </div>`;
  });
  el.innerHTML = html;
}

function lsmToggle(logId, key, action) {
  fetch(`/api/legal/dossier/${encodeURIComponent(logId)}/section_toggle`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({section: key, action: action})
  }).then(r => r.json()).then(data => {
    if (data.ok) _loadLsmState(logId);
    else alert('Error: ' + (data.error || 'unknown'));
  }).catch(() => alert('Network error — please try again.'));
}
// ─────────────────────────────────────────────────────────────────────────

document.getElementById('case-modal').addEventListener('click', e=>{ if(e.target===e.currentTarget) closeModal(); });

{{auto_load}}
</script>
</body>
</html>'''


@app.route('/legal')
def legal_portal():
    authed = session.get('legal_authed', False)
    html = LEGAL_PORTAL_HTML.replace(
        '{{login_display}}', 'none' if authed else 'flex'
    ).replace(
        '{{main_display}}', 'block' if authed else 'none'
    ).replace(
        '{{auto_load}}', 'document.addEventListener("DOMContentLoaded", loadDossiers);' if authed else ''
    )
    return Response(html, mimetype='text/html')


@app.route('/legal/auth', methods=['POST'])
def legal_auth():
    data = request.json or {}
    legal_pw = os.environ.get('LEGAL_PASSWORD', 'legal2026')
    if data.get('password') == legal_pw:
        session['legal_authed'] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False})


@app.route('/legal/logout')
def legal_logout():
    session.pop('legal_authed', None)
    return redirect('/legal')


@app.route('/api/legal/dossiers')
def legal_dossiers():
    """Legal-portal-accessible dossier endpoint — checks legal session."""
    # Safe auth check — current_user may not be set on unauthenticated requests
    try:
        authed = session.get('legal_authed', False) or (current_user and current_user.is_authenticated)
    except Exception:
        authed = session.get('legal_authed', False)
    if not authed:
        return jsonify({"error": "Unauthorised — please sign in via /legal"}), 401
    try:
        logs = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).all()
    except Exception as e:
        print(f"legal_dossiers DB error: {e}")
        return jsonify({"error": f"Database error: {e}"}), 500
    result = []
    for log in logs:
        try:
            m_hist   = getattr(log, 'metar_history', None)
            ev_fields = [
                log.weather_snap, getattr(log,'taf_snap',None),
                getattr(log,'notam_snap',None), getattr(log,'acars_snap',None),
                getattr(log,'ba_code',None), getattr(log,'logged_by',None),
                getattr(log,'notes',None), getattr(log,'xw_snap',None), m_hist
            ]
            ev_score = sum(1 for f in ev_fields if f and str(f).strip() not in ('','N/A','None'))
            result.append({
                "id":             log.id,
                "case_ref":       getattr(log,'case_ref',None) or log.id,
                "flight":         log.flight or '',
                "date":           log.date or '',
                "event_type":     log.event_type or 'CANCELLATION',
                "origin":         log.origin or '',
                "dest":           log.sched_dest or '',
                "actual_dest":    log.actual_dest or '',
                "tail":           getattr(log,'tail_snap',None) or 'N/A',
                "ba_code":        log.ba_code or 'N/A',
                "logged_by":      getattr(log,'logged_by',None) or 'N/A',
                "notes":          log.notes or '',
                "weather_snap":   log.weather_snap,
                "taf_snap":       getattr(log,'taf_snap',None),
                "notam_snap":     getattr(log,'notam_snap',None),
                "acars_snap":     getattr(log,'acars_snap',None),
                "xw_snap":        getattr(log,'xw_snap',None) or 'N/A',
                "xw_limit":       getattr(log,'xw_limit',None) or 'N/A',
                "metar_history":  m_hist,
                "hidden_sections": sorted(get_hidden(log)),
                "ev_score":       ev_score,
                "ev_max":         9,
                "timestamp_full": log.timestamp.strftime('%d %b %Y %H:%MZ') if log.timestamp else 'N/A',
                # SI Classification
                "si_cause":          getattr(log,'si_cause',None) or '',
                "si_cause_label":    getattr(log,'si_cause_label',None) or '',
                "si_problem_airport":getattr(log,'si_problem_airport',None) or '',
                "si_airport_focus":  getattr(log,'si_airport_focus',None) or '',
                # Living Dossier Lifecycle
                "dossier_status":    getattr(log,'dossier_status',None) or 'CLOSED',
                "auto_summary":      getattr(log,'auto_summary',None) or '',
            })
        except Exception as e:
            print(f"legal_dossiers: skipping log {getattr(log,'id','?')}: {e}")
            continue
    return jsonify(result)



@app.route('/api/legal/dossier/<path:log_id>/section_toggle', methods=['POST'])
def legal_section_toggle(log_id):
    """Legal portal version of section_toggle — marks portal=True in audit."""
    try:
        authed = session.get('legal_authed', False) or (current_user and current_user.is_authenticated)
    except Exception:
        authed = session.get('legal_authed', False)
    if not authed:
        return jsonify({'error': 'Unauthorised'}), 401
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({'error': 'not found'}), 404
    data    = request.json or {}
    section = data.get('section', '').strip()
    action  = data.get('action', '').strip().lower()
    valid_keys = {k for k, _ in EU261_SECTIONS}
    if section not in valid_keys:
        return jsonify({'error': f'Unknown section: {section}'}), 400
    if action not in ('hide', 'show'):
        return jsonify({'error': 'action must be hide or show'}), 400
    hidden = get_hidden(log)
    if action == 'hide': hidden.add(section)
    else: hidden.discard(section)
    audit = get_audit(log)
    # Determine username — legal session uses 'legal_user', OCC uses login
    username = current_user.username if current_user.is_authenticated else 'legal_portal'
    audit.append({
        'section':   section,
        'action':    action,
        'user':      username,
        'timestamp': datetime.now(timezone.utc).strftime('%d %b %Y %H:%MZ'),
        'portal':    True
    })
    log.hidden_sections = json.dumps(sorted(hidden))
    log.section_audit   = json.dumps(audit)
    try:
        db.session.commit()
        return jsonify({'ok': True, 'hidden': sorted(hidden), 'audit': audit})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/legal/dossier/<path:log_id>/sections')
def legal_get_sections(log_id):
    """Legal portal version of get_sections."""
    try:
        authed = session.get('legal_authed', False) or (current_user and current_user.is_authenticated)
    except Exception:
        authed = session.get('legal_authed', False)
    if not authed:
        return jsonify({'error': 'Unauthorised'}), 401
    log = db.session.get(DisruptionLog, log_id)
    if not log: return jsonify({'error': 'not found'}), 404
    hidden = get_hidden(log)
    return jsonify({
        'hidden':   sorted(hidden),
        'audit':    get_audit(log),
        'sections': [
            {'key': k, 'label': lbl, 'hidden': k in hidden,
             'source': EVIDENCE_SOURCES.get(k, '')}
            for k, lbl in EU261_SECTIONS
        ]
    })

@app.route('/api/legal/evidence/<path:log_id>')
def legal_evidence(log_id):
    """Legal-portal-accessible evidence endpoint."""
    try:
        authed = session.get('legal_authed', False) or (current_user and current_user.is_authenticated)
    except Exception:
        authed = session.get('legal_authed', False)
    if not authed:
        return jsonify({"error": "Unauthorised"}), 401
    items = CaseEvidence.query.filter_by(log_id=log_id).order_by(CaseEvidence.timestamp).all()
    return jsonify([{
        "id": e.id, "filename": e.filename, "content_type": e.content_type,
        "content_text": e.content_text, "added_by": e.added_by,
        "timestamp": e.timestamp.strftime('%d %b %Y %H:%M UTC') if e.timestamp else '',
    } for e in items])



@app.route('/api/export_slots')
@login_required
def export_slots():
    if not current_user.is_admin: return "Access Denied: Admins Only", 403
    logs = SlotLog.query.order_by(SlotLog.timestamp.desc()).all()
    csv_data = "ID,Flight,Date,Station,Status,Timestamp,Reply\n"
    for log in logs:
        safe_reply = str(log.coordinator_reply).replace('\n', ' ').replace(',', ';') if log.coordinator_reply else ""
        csv_data += f"{log.id},{log.flight},{log.date},{log.station},{log.status},{log.timestamp},{safe_reply}\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-disposition": "attachment; filename=Slot_Management_Log.csv"})

@app.route('/api/feedback', methods=['POST'])
@login_required
def send_feedback():
    data = request.json
    text = data.get('text', '').strip()
    if not text: return jsonify({"error": "No text provided"}), 400
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url: return jsonify({"error": "Webhook URL not configured"}), 500
    try:
        payload = {"text": f"**🚨 OCC Dashboard Feedback**\n\n**User:** {current_user.username}\n\n**Message:** {text}"}
        resp = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code in [200, 202]: return jsonify({"message": "Sent!"})
        else: return jsonify({"error": f"Teams Rejected: {resp.status_code}"}), 500
    except: return jsonify({"error": "Network Error"}), 500

@app.route('/api/export_disruptions')
@login_required
def export_disruptions():
    if not current_user.is_admin: return "Access Denied: Admins Only", 403
    logs = DisruptionLog.query.order_by(DisruptionLog.timestamp.desc()).all()
    csv_data = "ID,Flight,Date,Event,Origin,Sched_Dest,Actual_Dest,Timestamp,Weather_Snap\n"
    for log in logs:
        safe_wx = str(log.weather_snap).replace('\n', ' ').replace(',', ';') if log.weather_snap else ""
        csv_data += f"{log.id},{log.flight},{log.date},{log.event_type},{log.origin},{log.sched_dest},{log.actual_dest},{log.timestamp},{safe_wx}\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-disposition": "attachment; filename=EU261_Disruption_Log.csv"})

@app.route('/api/export_acars')
@login_required
def export_acars():
    if not current_user.is_admin: return "Access Denied: Admins Only", 403
    logs = AcarsLog.query.order_by(AcarsLog.timestamp.desc()).all()
    csv_data = "ID,Flight,Reg,Timestamp,Message\n"
    for log in logs:
        safe_msg = str(log.message).replace('\n', ' ').replace(',', ';') if log.message else ""
        csv_data += f"{log.id},{log.flight},{log.reg},{log.timestamp},{safe_msg}\n"
    return Response(csv_data, mimetype="text/csv", headers={"Content-disposition": "attachment; filename=ACARS_Log.csv"})

@app.route('/api/upload_schedule', methods=['POST'])
@login_required
def upload_schedule():
    global flight_schedule_df
    if not current_user.is_admin: return jsonify({"error": "Admin required"}), 403
    if 'file' not in request.files: return jsonify({"error": "No file uploaded"}), 400
    
    existing_tails = {}
    if not flight_schedule_df.empty and 'FLT' in flight_schedule_df.columns:
        for _, row in flight_schedule_df.iterrows():
            flt = str(row.get('FLT', '')).strip().upper()
            reg = str(row.get('AC_REG', 'UNK')).strip().upper()
            if flt and reg != 'UNK' and reg != 'NAN':
                existing_tails[flt] = reg

    file_bytes = request.files['file'].read()
    new_df = load_schedule_robust(file_bytes)
    
    if not new_df.empty:
        for flt, reg in existing_tails.items():
            new_df.loc[new_df['FLT'].astype(str).str.upper() == flt, 'AC_REG'] = str(reg)
            
        csv_data = new_df.to_csv(index=False)
        record = db.session.get(AppData, 'schedule')
        if not record:
            record = AppData(id='schedule', data=csv_data)
            db.session.add(record)
        else: 
            record.data = csv_data
        db.session.commit()
        refresh_schedule_cache()
        return jsonify({"message": f"Schedule updated. Preserved {len(existing_tails)} live AAR tails!"})
    
    return jsonify({"error": "Invalid CSV format."}), 400

@app.route('/api/upload_contacts', methods=['POST'])
@login_required
def upload_contacts():
    if not current_user.is_admin: return jsonify({"error": "Admin required"}), 403
    if 'file' not in request.files: return jsonify({"error": "No file uploaded"}), 400
    file_bytes = request.files['file'].read()
    record = db.session.get(AppData, 'contacts')
    if not record:
        record = AppData(id='contacts', data=file_bytes.decode('utf-8', errors='ignore'))
        db.session.add(record)
    else: record.data = file_bytes.decode('utf-8', errors='ignore')
    db.session.commit()
    refresh_contacts_cache()
    return jsonify({"message": "Contacts DB updated successfully!"})

@app.route('/api/contacts')
@login_required
def get_contacts():
    if contacts_df.empty: return jsonify({})
    try:
        res = {}
        cols = contacts_df.columns.tolist()
        st_col = next((c for c in cols if 'station' in str(c).lower() or 'iata' in str(c).lower()), cols[0])
        co_col = next((c for c in cols if 'company' in str(c).lower() or 'name' in str(c).lower() or 'agent' in str(c).lower()), cols[1] if len(cols) > 1 else None)
        ph_col = next((c for c in cols if 'tel' in str(c).lower() or 'phone' in str(c).lower()), cols[2] if len(cols) > 2 else None)
        em_col = next((c for c in cols if 'email' in str(c).lower()), cols[3] if len(cols) > 3 else None)

        for _, row in contacts_df.iterrows():
            current_st = ""
            st_raw = str(row[st_col]).strip().upper() if pd.notna(row[st_col]) else ""
            if st_raw and st_raw != 'NAN': current_st = st_raw
            if not current_st: continue

            co = str(row[co_col]).strip() if co_col and pd.notna(row[co_col]) else ""
            ph = str(row[ph_col]).strip() if ph_col and pd.notna(row[ph_col]) else ""
            em = str(row[em_col]).strip() if em_col and pd.notna(row[em_col]) else ""
            
            if (not co or co.lower() == 'nan') and (not ph or ph.lower() == 'nan'): continue
            if ph.lower().startswith('tel:'): ph = ph[4:].strip()
                
            if current_st not in res: res[current_st] = []
            res[current_st].append({"company": co, "phone": ph, "email": em})
            
        return jsonify(res)
    except: return jsonify({})

@app.route('/manifest.json')
def serve_manifest(): return app.send_static_file('manifest.json')
@app.route('/sw.js')
def serve_sw(): return app.send_static_file('sw.js')

def _parse_paxfigs_text(text):
    """Parse raw CSV text into clean csv_bytes and records list."""
    records = []
    output_lines = []
    for line in text.splitlines():
        clean = line.strip().replace('"', '').replace('\r', '')
        if not clean: continue
        parts = [p.strip() for p in clean.split(',')]
        if len(parts) < 6: continue
        if not re.match(r'^\d{8}$', parts[0]): continue  # skip header/blank
        date_str = parts[0]
        carrier  = parts[1].strip()
        flt_num  = parts[2].strip()
        dep      = parts[4].upper() if len(parts) > 4 else ''
        m_pax    = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        c_pax    = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0
        output_lines.append(f"{date_str},{carrier},{flt_num},,{dep},{m_pax},{c_pax}")
        records.append({"date": date_str, "carrier": carrier, "flt_num": flt_num,
                        "flt": f"BA{flt_num}", "flt_date_key": f"BA{flt_num}_{date_str}",
                        "dep": dep, "m": m_pax, "c": c_pax})
    csv_bytes = "\n".join(output_lines).encode("utf-8")
    return csv_bytes, records


def _sftp_push(csv_bytes):
    """Push csv_bytes to AIMS SFTP. Returns (success, message)."""
    if not AIMS_PASSWORD:
        return False, "AIMS_PASSWORD not set in Render environment variables."
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=AIMS_HOST, port=AIMS_PORT, username=AIMS_USER,
                    password=AIMS_PASSWORD, timeout=15)
        sftp = ssh.open_sftp()
        # Navigate to absolute remote dir, then upload
        remote_path = AIMS_REMOTE_DIR.rstrip('/') + "/paxfigs.csv"
        try:
            sftp.chdir(AIMS_REMOTE_DIR)
            with sftp.open("paxfigs.csv", "w") as f:
                f.write(csv_bytes.decode("utf-8"))
        except IOError:
            # Fall back: write to absolute path directly
            with sftp.open(remote_path, "w") as f:
                f.write(csv_bytes.decode("utf-8"))
        sftp.close(); ssh.close()
        return True, f"Uploaded to {AIMS_HOST}:{remote_path}"
    except ImportError:
        return False, "paramiko not installed on server."
    except Exception as e:
        msg = str(e)
        if 'Name or service not known' in msg or 'Errno -2' in msg:
            return False, f"AIMS SFTP unreachable from Render (DNS: {AIMS_HOST}). PAX figures ARE loaded in dashboard — SFTP push requires VPN/tunnel or AIMS firewall rule for Render's IP."
        return False, msg


@app.route('/api/upload_paxfigs', methods=['GET', 'POST'])
@login_required
def upload_paxfigs():
    global pax_figures
    if request.method == 'GET':
        today = datetime.utcnow().strftime('%Y%m%d')
        today_figs = {k: v for k, v in pax_figures.items() if v.get('date') == today}
        return jsonify({
            "loaded": len(pax_figures),
            "today": len(today_figs),
            "dates": sorted(set(v.get('date','') for v in pax_figures.values())),
            "aims_configured": bool(AIMS_PASSWORD),
        })

    # POST — accept zip or csv
    uploaded = request.files.get('file')
    if not uploaded:
        return jsonify({"error": "No file provided"}), 400

    fname = uploaded.filename.lower()
    if fname.endswith('.zip'):
        import zipfile, io as _io
        try:
            zf = zipfile.ZipFile(_io.BytesIO(uploaded.read()))
            csv_name = next((n for n in zf.namelist() if n.lower().endswith('.csv')), None)
            if not csv_name: return jsonify({"error": "No CSV found inside zip"}), 400
            text = zf.read(csv_name).decode('utf-8-sig', errors='replace')
        except Exception as e:
            return jsonify({"error": f"Could not read zip: {e}"}), 400
    else:
        text = uploaded.read().decode('utf-8-sig', errors='replace')

    csv_bytes, records = _parse_paxfigs_text(text)
    if not records:
        return jsonify({"error": "No valid flight rows found in file"}), 400

    # Store in memory + persist to DB
    for r in records:
        # Key by flight+date so 3-day uploads don't overwrite each other
        pax_figures[r['flt_date_key']] = {"m": r['m'], "c": r['c'], "dep": r['dep'], "date": r['date']}
    try:
        record = db.session.get(AppData, 'pax_figures')
        if not record:
            db.session.add(AppData(id='pax_figures', data=json.dumps(pax_figures)))
        else:
            record.data = json.dumps(pax_figures)
        db.session.commit()
    except Exception as e:
        print(f'Warning: could not persist pax_figures: {e}')

    dates = sorted(set(r['date'] for r in records))

    # Push to AIMS SFTP
    sftp_ok, sftp_msg = _sftp_push(csv_bytes)

    return jsonify({
        "status": "ok",
        "loaded": len(records),
        "dates": dates,
        "aims_push": sftp_ok,
        "aims_msg": sftp_msg,
    })


@app.route('/api/pax_figures')
@login_required
def get_pax_figures():
    flt = request.args.get('flt', '').upper().strip()
    date = request.args.get('date', datetime.utcnow().strftime('%Y%m%d'))
    if flt:
        result = pax_figures.get(flt) or pax_figures.get(f"BA{flt.lstrip('BA')}")
        return jsonify(result or {})
    return jsonify({k: v for k, v in pax_figures.items() if v.get('date') == date})


@app.route('/api/debug_notams')
@login_required
def debug_notams():
    iata = request.args.get('iata', '').upper().strip()
    if not iata: return jsonify({"error": "Pass ?iata=XXX"})
    notams = raw_notam_cache.get(iata, [])
    if not notams:
        info = base_airports.get(iata)
        if info: notams = fetch_faa_notams(info['icao'])
    return jsonify({"iata": iata, "count": len(notams), "notams": notams})


if __name__ == '__main__': app.run(debug=True)
