import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
import re
import io
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Command HUD", page_icon="✈️")

# 2. HUD STYLING (v29.2 CSS RESTORATION)
st.markdown("""
    <style>
    .main { background-color: #001a33 !important; }
    html, body, [class*="st-"], div, p, h1, h2, h4, label { color: white !important; }
    
    .ba-header { 
        background-color: #002366 !important; color: #ffffff !important; 
        padding: 20px; border-radius: 8px; margin-bottom: 20px; 
        border: 2px solid #d6001a; display: flex; justify-content: space-between;
    }

    [data-testid="stSidebar"] { background-color: #002366 !important; min-width: 350px !important; border-right: 3px solid #d6001a; }
    [data-testid="stSidebar"] label p { color: #ffffff !important; font-weight: bold; }
    
    /* SIDEBAR BUTTON COLORS */
    [data-testid="stSidebar"] .stButton > button { background-color: #005a9c !important; color: white !important; border: 1px solid white !important; font-weight: bold !important; }
    
    /* ALERT BUTTON COLORS */
    .stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: 1px solid white !important; font-weight: bold !important; }
    .stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: 1px solid white !important; font-weight: bold !important; }

    /* DROPDOWN & SELECTBOX (NAVY-ON-WHITE) */
    div[data-testid="stSelectbox"] div[data-baseweb="select"], div[data-testid="stDateInput"] div { background-color: white !important; }
    div[data-testid="stSelectbox"] *, div[data-testid="stDateInput"] * { color: #002366 !important; font-weight: 800 !important; }
    [data-baseweb="popover"] * { color: #002366 !important; background-color: white !important; font-weight: bold !important; }

    /* FILE UPLOADER VISIBILITY FIX */
    [data-testid="stFileUploader"] section { 
        background-color: #005a9c !important; 
        border: 1px solid white !important; 
        border-radius: 5px !important; 
        padding: 15px !important;
    }
    [data-testid="stFileUploader"] section * { color: white !important; font-weight: bold !important; }
    [data-testid="stFileUploader"] button { background-color: #002366 !important; color: white !important; border: 1px solid white !important; border-radius: 4px !important; }

    /* HANDOVER LOG */
    [data-testid="stTextArea"] textarea { 
        color: #002366 !important; background-color: #ffffff !important; 
        font-weight: bold !important; font-family: 'Courier New', monospace !important; 
    }

    .reason-box { background-color: #ffffff !important; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
    .reason-box * { color: #002366 !important; }
    .reason-box .alt-highlight { color: #d6001a !important; font-weight: bold !important; }
    
    .section-header { color: #ffffff !important; background-color: #002366; padding: 10px; border-left: 10px solid #d6001a; font-weight: bold; font-size: 1.5rem; margin-top: 30px; }
    .leaflet-tooltip, .leaflet-popup-content-wrapper { background: white !important; border: 2px solid #002366 !important; padding: 0 !important; opacity: 1 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. UTILITIES & ROBUST CSV LOADER
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

def calculate_xwind(wind_dir, wind_spd, rwy_hdg):
    if wind_dir is None or wind_spd is None or rwy_hdg is None: return 0
    angle = math.radians(wind_dir - rwy_hdg)
    return round(abs(wind_spd * math.sin(angle)))

def bold_hazard(text):
    if not text or text == "N/A": return text
    text = re.sub(r'\b(TEMPO|BECMG|PROB\d{2})\b', r'<b>\1</b>', text)
    text = re.sub(r'(\b\d{4}/\d{4}\b)', r'<b>\1</b>', text)
    text = re.sub(r'(\b\d{3}\d{2}G\d{2,3}KT\b)', r'<b>\1</b>', text)
    text = re.sub(r'(\b\d{3}[2-9]\dKT\b)', r'<b>\1</b>', text)
    text = re.sub(r'(\b(FG|TSRA|SN|-SN|\+SN|FZRA|FZDZ|TS|FOG)\b)', r'<b>\1</b>', text)
    text = re.sub(r'\b((?:BKN|OVC)00[0-9])\b', r'<b>\1</b>', text)
    text = re.sub(r'\b((?:BKN|OVC)01[0-5])\b', r'<b>\1</b>', text)
    text = re.sub(r'\b(0[0-9]{3})\b', r'<b>\1</b>', text)
    return text

@st.cache_data
def load_schedule_robust(file_bytes):
    try:
        content = file_bytes.decode('utf-8').splitlines()
        skip_r = 0
        for i, line in enumerate(content):
            if 'DATE' in line and 'FLT' in line and 'DEP' in line and 'ARR' in line:
                skip_r = i
                break
        df = pd.read_csv(io.StringIO(file_bytes.decode('utf-8')), skiprows=skip_r, on_bad_lines='skip')
        df = df.dropna(subset=['FLT'])
        df['DATE_OBJ'] = pd.to_datetime(df['DATE'], format='%d/%m/%y', errors='coerce').dt.date
        df['DATE_OBJ'] = df['DATE_OBJ'].fillna(pd.to_datetime(df['DATE'], dayfirst=True, errors='coerce').dt.date)
        return df
    except Exception as e:
        return pd.DataFrame()

# LIVE RADAR ENGINE
@st.cache_data(ttl=20)
def fetch_raw_radar():
    fleet = []
    try:
        url = "https://opensky-network.org/api/states/all?lamin=30.0&lomin=-20.0&lamax=65.0&lomax=30.0"
        data = requests.get(url, timeout=5).json()
        if "states" in data and data["states"]:
            for s in data["states"]:
                call = (s[1] or "").strip().upper()
                if call.startswith("CFE") or call.startswith("EFW"):
                    fleet.append({
                        "call": call, "lat": s[6], "lon": s[5], 
                        "type": "CFE" if call.startswith("CFE") else "EFW",
                        "alt": round((s[7] or 0) * 3.28084), "hdg": s[10] or 0
                    })
    except: pass
    return fleet

# 4. MASTER DATABASE (FULL 47 STATIONS)
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer", "spec": True},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer", "spec": False},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer", "spec": True},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer", "spec": True},
    "ZRH": {"icao": "LSZH", "lat": 47.458, "lon": 8.548, "rwy": 160, "fleet": "Cityflyer", "spec": False},
    "GVA": {"icao": "LSGG", "lat": 46.237, "lon": 6.109, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "BER": {"icao": "EDDB", "lat": 52.362, "lon": 13.501, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "FRA": {"icao": "EDDF", "lat": 50.033, "lon": 8.571, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "LIN": {"icao": "LIML", "lat": 45.445, "lon": 9.277, "rwy": 360, "fleet": "Cityflyer", "spec": False},
    "MAD": {"icao": "LEMD", "lat": 40.494, "lon": -3.567, "rwy": 140, "fleet": "Cityflyer", "spec": False},
    "IBZ": {"icao": "LEIB", "lat": 38.873, "lon": 1.373, "rwy": 60, "fleet": "Cityflyer", "spec": False},
    "PMI": {"icao": "LEPA", "lat": 39.551, "lon": 2.738, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "AGP": {"icao": "LEMG", "lat": 36.675, "lon": -4.499, "rwy": 130, "fleet": "Cityflyer", "spec": False},
    "FAO": {"icao": "LPFR", "lat": 37.017, "lon": -7.965, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "SEN": {"icao": "EGMC", "lat": 51.571, "lon": 0.701, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer", "spec": True},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer", "spec": True},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "VRN": {"icao": "LIPX", "lat": 45.396, "lon": 10.888, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "OPO": {"icao": "LPPR", "lat": 41.242, "lon": -8.678, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "LYS": {"icao": "LFLL", "lat": 45.726, "lon": 5.090, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "SZG": {"icao": "LOWS", "lat": 47.794, "lon": 13.004, "rwy": 330, "fleet": "Euroflyer", "spec": False},
    "BOD": {"icao": "LFBD", "lat": 44.828, "lon": -0.716, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "GNB": {"icao": "LFLS", "lat": 45.363, "lon": 5.330, "rwy": 90, "fleet": "Euroflyer", "spec": False},
    "TRN": {"icao": "LIMF", "lat": 45.202, "lon": 7.649, "rwy": 360, "fleet": "Euroflyer", "spec": False},
    "ALC": {"icao": "LEAL", "lat": 38.282, "lon": -0.558, "rwy": 100, "fleet": "Euroflyer", "spec": False},
    "SVQ": {"icao": "LEZL", "lat": 37.418, "lon": -5.893, "rwy": 270, "fleet": "Euroflyer", "spec": False},
    "RAK": {"icao": "GMMX", "lat": 31.606, "lon": -8.036, "rwy": 100, "fleet": "Euroflyer", "spec": False},
    "AGA": {"icao": "GMAD", "lat": 30.325, "lon": -9.413, "rwy": 90, "fleet": "Euroflyer", "spec": False},
    "SSH": {"icao": "HESH", "lat": 27.977, "lon": 34.394, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "PFO": {"icao": "LCPH", "lat": 34.718, "lon": 32.486, "rwy": 290, "fleet": "Euroflyer", "spec": False},
    "LCA": {"icao": "LCLK", "lat": 34.875, "lon": 33.625, "rwy": 220, "fleet": "Euroflyer", "spec": False},
    "FUE": {"icao": "GCLP", "lat": 28.452, "lon": -13.864, "rwy": 10, "fleet": "Euroflyer", "spec": False},
    "TFS": {"icao": "GCTS", "lat": 28.044, "lon": -16.572, "rwy": 70, "fleet": "Euroflyer", "spec": False},
    "ACE": {"icao": "GCRR", "lat": 28.945, "lon": -13.605, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "LPA": {"icao": "GCLP", "lat": 27.931, "lon": -15.386, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "IVL": {"icao": "EFIV", "lat": 68.607, "lon": 27.405, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "MLA": {"icao": "LMML", "lat": 35.857, "lon": 14.477, "rwy": 310, "fleet": "Euroflyer", "spec": False},
    "ALG": {"icao": "DAAG", "lat": 36.691, "lon": 3.215, "rwy": 230, "fleet": "Euroflyer", "spec": False},
}

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"

# 5. SIDEBAR WITH CSV UPLOADER & CALENDAR
with st.sidebar:
    st.title("🛠️ COMMAND HUD")
    
    st.markdown("📂 **SCHEDULE INTEGRATION**")
    uploaded_file = st.file_uploader("Upload Daily Flight Schedule (CSV)", type=["csv"])
    
    flight_schedule = pd.DataFrame()
    selected_date = st.date_input("📅 Select Operations Date:", value=datetime.now().date())
    active_stations = set()
    
    if uploaded_file is not None:
        flight_schedule = load_schedule_robust(uploaded_file.getvalue())
        if not flight_schedule.empty and 'DATE_OBJ' in flight_schedule.columns:
            flight_schedule = flight_schedule[flight_schedule['DATE_OBJ'] == selected_date]
            if not flight_schedule.empty:
                st.success(f"Loaded {len(flight_schedule)} flights for {selected_date.strftime('%d %b %Y')}")
                active_stations = set(flight_schedule['DEP'].dropna()) | set(flight_schedule['ARR'].dropna())
            else:
                st.warning(f"No flights found for {selected_date.strftime('%d %b %Y')}. Displaying full network.")
        else:
            st.error("Error reading file. Ensure it's the correct BA CSV export.")
    else:
        st.info("Upload your shift's CSV to dynamically filter active stations & view flight impacts.")
    
    # DYNAMIC STATION FILTERING
    if uploaded_file is not None and active_stations:
        display_airports = {k: v for k, v in base_airports.items() if k in active_stations}
    else:
        display_airports = base_airports
        
    st.markdown("---")
    if st.button("🔄 MANUAL DATA REFRESH"):
        st.cache_data.clear()
        st.rerun()
        
    st.markdown("---")
    st.markdown("📡 **RADAR TRACKING**")
    show_radar = st.checkbox("Enable Live Aircraft Radar", value=True)
    
    st.markdown("---")
    st.markdown("🕒 **INTEL HORIZON**")
    time_horizon = st.radio("SCAN WINDOW", ["Next 6 Hours", "Next 12 Hours", "Next 24 Hours"], index=0)
    horizon_hours = 6 if "6" in time_horizon else (12 if "12" in time_horizon else 24)
    
    st.markdown("---")
    st.markdown("⚠️ **SAFETY LIMITS**")
    xw_limit = st.slider("X-WIND LIMIT (KT)", 15, 35, 25)
    
    st.markdown("---")
    st.markdown("🎯 **TACTICAL FILTERS**")
    filter_map = {"XWIND": "XWIND", "WINDY (Gusts >25)": "WINDY", "FOG": "FOG", "WINTER (Snow/FZRA)": "WINTER", "TSRA": "TSRA", "VIS (<Limits)": "VIS", "LOW CLOUD (<Limits)": "CLOUD"}
    hazard_filter = st.selectbox("ISOLATE HAZARD", ["Show All Network", "Any Amber/Red Alert", "XWIND", "WINDY (Gusts >25)", "FOG", "WINTER (Snow/FZRA)", "TSRA", "VIS (<Limits)", "LOW CLOUD (<Limits)"])
    
    st.markdown("---")
    show_cf = st.checkbox("Cityflyer (CFE)", value=True)
    show_ef = st.checkbox("Euroflyer (EFW)", value=True)
    
    st.markdown("---")
    map_theme = st.radio("MAP THEME", ["Dark Mode", "Light Mode"])

# 6. DATA FETCH & PROCESSING
@st.cache_data(ttl=1800)
def get_raw_weather_master(airport_dict):
    raw_res = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update(); t = Taf(info['icao']); t.update()
            raw_res[iata] = {"m_obj": m, "t_obj": t, "status": "online"}
        except: raw_res[iata] = {"status": "offline"}
    return raw_res

raw_weather_bundle = get_raw_weather_master(display_airports)

def process_weather_for_horizon(bundle, airport_dict, horizon_limit, xw_threshold):
    processed = {}
    cutoff_time = datetime.now(timezone.utc) + timedelta(hours=horizon_limit)
    for iata, data in bundle.items():
        if data['status'] == "offline" or "m_obj" not in data:
            processed[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A", "f_issues": [], "f_wind_spd":0, "f_wind_dir":0, "w_spd":0, "w_dir":0}
            continue
        m, t, info = data['m_obj'], data['t_obj'], airport_dict[iata]
        v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
        
        m_vis = m.data.visibility.value if (m.data and hasattr(m.data, 'visibility') and m.data.visibility) else 9999
        m_cig = 9999
        if m.data and hasattr(m.data, 'clouds') and m.data.clouds:
            for lyr in m.data.clouds:
                if lyr.type in ['BKN', 'OVC'] and lyr.base: m_cig = min(m_cig, lyr.base * 100)
        
        w_issues, f_wind_spd, f_wind_dir, w_time, w_prob = [], 0, 0, "", False
        if t and t.data and hasattr(t.data, 'forecast'):
            for line in t.data.forecast:
                if not line.start_time or not hasattr(line.start_time, 'dt'): continue
                if line.start_time.dt > cutoff_time: continue
                l_raw = line.raw.upper()
                l_issues = []
                l_v = line.visibility.value if (line.visibility and line.visibility.value is not None) else 9999
                l_c = 9999
                if line.clouds:
                    for lyr in line.clouds:
                        if lyr.type in ['BKN', 'OVC'] and lyr.base: l_c = min(l_c, lyr.base * 100)
                
                if re.search(r'\bFG\b', l_raw): l_issues.append("FOG")
                if re.search(r'(-SN|\+SN|\bSN\b|\bFZ)', l_raw): l_issues.append("WINTER")
                if l_v < v_lim: l_issues.append("VIS")
                if l_c < c_lim: l_issues.append("CLOUD")
                if re.search(r'\bTS|VCTS', l_raw): l_issues.append("TSRA")
                
                l_dir = line.wind_direction.value if (line.wind_direction and line.wind_direction.value) else info['rwy']
                l_spd = line.wind_speed.value if (line.wind_speed and line.wind_speed.value) else 0
                l_gst = line.wind_gust.value if (line.wind_gust and line.wind_gust.value) else 0
                peak = max(l_spd, l_gst)
                
                if calculate_xwind(l_dir, peak, info['rwy']) >= xw_threshold: l_issues.append("XWIND")
                elif peak > 25: l_issues.append("WINDY")
                
                if l_issues:
                    for iss in l_issues:
                        if iss not in w_issues: w_issues.append(iss)
                    w_time = f"{line.start_time.dt.strftime('%H')}Z"
                    f_wind_spd, f_wind_dir, w_prob = peak, l_dir, ("PROB" in l_raw)

        processed[iata] = {
            "vis": m_vis, "cig": m_cig, "status": "online",
            "w_dir": m.data.wind_direction.value if (m.data and hasattr(m.data, 'wind_direction') and m.data.wind_direction) else 0,
            "w_spd": m.data.wind_speed.value if (m.data and hasattr(m.data, 'wind_speed') and m.data.wind_speed) else 0,
            "w_gst": m.data.wind_gust.value if (m.data and hasattr(m.data, 'wind_gust') and m.data.wind_gust) else 0,
            "raw_m": m.raw or "N/A", "raw_t": t.raw if t and t.raw else "N/A",
            "f_issues": w_issues, "f_time": w_time, "f_wind_spd": f_wind_spd, "f_wind_dir": f_wind_dir, "f_prob": w_prob
        }
    return processed

weather_data = process_weather_for_horizon(raw_weather_bundle, display_airports, horizon_hours, xw_limit)

# RADAR DATA MAPPING & LOOKUP LOGIC
radar_data = []
if show_radar:
    raw_radar = fetch_raw_radar()
    
    # Build schedule dictionary for fast tooltip lookup
    sched_dict = {}
    if not flight_schedule.empty:
        has_arcid = 'ARCID' in flight_schedule.columns
        for _, r in flight_schedule.iterrows():
            if has_arcid and pd.notna(r['ARCID']) and str(r['ARCID']).strip():
                sched_dict[str(r['ARCID']).strip().upper()] = r
            
            if pd.notna(r['FLT']):
                flt_str = str(r['FLT']).replace('BA', '').strip()
                sched_dict[f"CFE{flt_str}"] = r
                sched_dict[f"EFW{flt_str}"] = r

    for p in raw_radar:
        call = p['call']
        flt, dep, arr = call, "UKN", "UKN"
        
        # Link Radar to Schedule
        if call in sched_dict:
            flt = str(sched_dict[call]['FLT'])
            dep = str(sched_dict[call]['DEP'])
            arr = str(sched_dict[call]['ARR'])

        p['flt'] = flt
        p['dep'] = dep
        p['arr'] = arr
        radar_data.append(p)

# TIME LOGIC FOR SCHEDULE FILTERING
current_utc_date = datetime.now(timezone.utc).date()
current_utc_time_str = datetime.now(timezone.utc).strftime('%H:%M')

# 7. UI LOOP & INBOUND FLIGHT INJECTION
metar_alerts, taf_alerts, green_stations, map_markers = {}, {}, [], []
for iata, info in display_airports.items():
    data = weather_data.get(iata)
    if not data or not ((info['fleet'] == "Cityflyer" and show_cf) or (info['fleet'] == "Euroflyer" and show_ef)): continue
    v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
    m_issues = []
    
    cur_xw = calculate_xwind(data.get('w_dir', 0), max(data.get('w_spd', 0), data.get('w_gst', 0)), info['rwy'])
    raw_m = data['raw_m'].upper()
    
    if re.search(r'\bFG\b', raw_m): m_issues.append("FOG")
    if re.search(r'(-SN|\+SN|\bSN\b|\bFZ)', raw_m): m_issues.append("WINTER")
    if data.get('vis', 9999) < v_lim: m_issues.append("VIS")
    if data.get("cig", 9999) < c_lim: m_issues.append("CLOUD")
    if re.search(r'\bTS|VCTS', raw_m): m_issues.append("TSRA")
    if cur_xw >= xw_limit: m_issues.append("XWIND")
    if data.get('w_gst', 0) > 25 and "XWIND" not in m_issues: m_issues.append("WINDY")
    
    trend_icon = "➡️"
    if not m_issues and data['f_issues']: trend_icon = "📈"
    elif m_issues and not data['f_issues']: trend_icon = "📉"
    
    color = "#008000"
    if m_issues: color = "#d6001a" if any(x in m_issues for x in ["FOG","WINTER","VIS","TSRA","XWIND"]) else "#eb8f34"
    elif data['f_issues']: color = "#eb8f34"
    if not m_issues and not data['f_issues']: green_stations.append(iata)
    
    rwy_text = f"RWY {int(info['rwy']/10):02d}/{int(((info['rwy']+180)%360)/10):02d}"
    if m_issues: metar_alerts[iata] = {"type": "/".join(m_issues), "hex": "primary" if color == "#d6001a" else "secondary"}
    if data['f_issues']: taf_alerts[iata] = {"type": "+".join(data['f_issues']), "time": data['f_time'], "prob": data['f_prob'], "hex": "secondary"}
    
    if hazard_filter == "Any Amber/Red Alert" and color == "#008000": continue
    elif hazard_filter not in ["Show All Network", "Any Amber/Red Alert"]:
        req_tag = filter_map.get(hazard_filter)
        if req_tag not in m_issues and req_tag not in data['f_issues']: continue
    
    m_bold, t_bold = bold_hazard(data.get('raw_m', 'N/A')), bold_hazard(data.get('raw_t', 'N/A'))
    
    # ---------------------------------------------------------
    # CSV FLIGHT INJECTION LOGIC (Filters out past flights)
    # ---------------------------------------------------------
    inbound_html = ""
    if not flight_schedule.empty:
        arr_flights = flight_schedule[flight_schedule['ARR'] == iata].sort_values(by='STA')
        if not arr_flights.empty:
            rows = ""
            for _, row in arr_flights.iterrows():
                sta = str(row['STA']).strip()
                flight_date = row['DATE_OBJ']
                
                # Filter out flights that have already arrived (if looking at today)
                if flight_date < current_utc_date:
                    continue
                if flight_date == current_utc_date and sta < current_utc_time_str:
                    continue
                
                flt = str(row['FLT']).strip()
                dep = str(row['DEP']).strip()
                arr = str(row['ARR']).strip()
                canc = row.get('Cancellation Reason', None)
                
                f_status = "SCHED"
                f_color = "#008000"
                if pd.notna(canc) and str(canc).strip() != "":
                    f_status = "CANC"
                    f_color = "#d6001a"
                elif color == "#d6001a":
                    f_status = "AT RISK"
                    f_color = "#d6001a"
                elif color == "#eb8f34":
                    f_status = "CAUTION"
                    f_color = "#eb8f34"
                    
                rows += f"<tr style='border-bottom: 1px solid #ddd;'><td style='color:{f_color}; font-weight:bold; padding:4px;'>{f_status}</td><td style='padding:4px;'>{flt}</td><td style='padding:4px;'>{dep}</td><td style='padding:4px;'>{arr}</td><td style='padding:4px;'>{sta}</td></tr>"
            
            if rows: # Only show the table if there are actually future flights left
                inbound_html = f"""
                <div style='margin-top:15px; border-top: 2px solid #002366; padding-top:10px;'>
                    <b style='color:#002366; font-size:14px;'>🛬 YET TO ARRIVE ({selected_date.strftime('%d/%m/%Y')})</b>
                    <div style='max-height: 200px; overflow-y: auto; margin-top:5px; border: 1px solid #ccc; background: #fff;'>
                        <table style='width:100%; text-align:left; font-size:12px; border-collapse: collapse; color: #000;'>
                            <tr style='background:#002366; color:#fff;'>
                                <th style='padding:5px;'>Status</th><th style='padding:5px;'>FLT</th><th style='padding:5px;'>DEP</th><th style='padding:5px;'>ARR</th><th style='padding:5px;'>STA</th>
                            </tr>
                            {rows}
                        </table>
                    </div>
                </div>
                """
    
    shared_content = f"""<div style="width:580px; color:black !important; font-family:monospace; font-size:14px; background:white; padding:15px; border-radius:5px;"><b style="color:#002366; font-size:18px;">{iata} STATUS {trend_icon}</b><div style="margin-top:8px; padding:10px; border-left:6px solid {color}; background:#f9f9f9; font-size:16px;"><b style="color:#002366;">{rwy_text} X-Wind:</b> <b>{cur_xw} KT</b><br><b>ACTUAL:</b> {"/".join(m_issues) if m_issues else "STABLE"}<br><b>FORECAST ({time_horizon}):</b> {"+".join(data['f_issues']) if data['f_issues'] else "NIL"}</div><hr style="border:1px solid #ddd;"><div style="display:flex; gap:12px;"><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; white-space: pre-wrap; word-wrap: break-word;"><b>METAR</b><br>{m_bold}</div><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; white-space: pre-wrap; word-wrap: break-word;"><b>TAF</b><br>{t_bold}</div></div>{inbound_html}</div>"""
    map_markers.append({"lat": info['lat'], "lon": info['lon'], "color": color, "content": shared_content, "iata": iata, "trend": trend_icon})

# 8. UI RENDER
st.markdown(f'<div class="ba-header"><div>OCC HUD v29.2 (Radar & Schedule Active)</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

m = folium.Map(location=[50.0, 10.0], zoom_start=4, tiles=("CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"), scrollWheelZoom=False)

# Render Station Markers
for mkr in map_markers:
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True, popup=folium.Popup(mkr['content'], max_width=650, auto_pan=True, auto_pan_padding=(150, 150)), tooltip=folium.Tooltip(mkr['content'], direction='top', sticky=False)).add_to(m)

# Render Aircraft Markers (If Enabled)
if show_radar:
    for p in radar_data:
        p_color = "#00bfff" if p['type']=="CFE" else "#ff4500"
        
        # New Sharp SVG Airplane Vector
        svg_html = f'''
        <div style="transform: translate(-50%, -50%) rotate({p["hdg"]}deg); width: 20px; height: 20px;">
            <svg viewBox="0 0 24 24" width="20" height="20" xmlns="http://www.w3.org/2000/svg">
                <path d="M21,16v-2l-8-5V3.5C13,2.67,12.33,2,11.5,2S10,2.67,10,3.5V9l-8,5v2l8-2.5V19l-2,1.5V22l3.5-1l3.5,1v-1.5L13,19v-5.5L21,16z" 
                      fill="{p_color}" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round"/>
            </svg>
        </div>
        '''
        folium.Marker(
            [p['lat'], p['lon']], 
            icon=folium.DivIcon(html=svg_html, class_name="dummy"),
            tooltip=f"<div style='font-family:Arial; font-size:13px; color:#002366; padding:5px; text-align:center;'><b>FLT: {p['flt']}</b><hr style='margin:4px 0;'>DEP: {p['dep']} | ARR: {p['arr']}<br>Height: {p['alt']} ft</div>"
        ).add_to(m)

st_folium(m, width=1200, height=800, key="map_stable_v292")

# 9. ALERTS & STRATEGY
st.markdown('<div class="section-header">🔴 Actual Alerts (METAR)</div>', unsafe_allow_html=True)
if metar_alerts:
    cols = st.columns(5)
    for i, (iata, d) in enumerate(metar_alerts.items()):
        with cols[i % 5]:
            if st.button(f"{iata} NOW {d['type']}", key=f"m_{iata}", type=d['hex']): st.session_state.investigate_iata = iata
            
st.markdown(f'<div class="section-header">🟠 Forecast Alerts ({time_horizon})</div>', unsafe_allow_html=True)
if taf_alerts:
    cols_f = st.columns(5)
    for i, (iata, d) in enumerate(taf_alerts.items()):
        with cols_f[i % 5]:
            p_tag = " prob" if d['prob'] else ""
            if st.button(f"{iata} {d['time']} {d['type']}{p_tag}", key=f"f_{iata}", type="secondary"): st.session_state.investigate_iata = iata

if st.session_state.investigate_iata != "None":
    iata = st.session_state.investigate_iata
    d, info = weather_data.get(iata, {}), base_airports.get(iata, {"rwy": 0, "lat": 0, "lon": 0})
    issue_desc = (taf_alerts.get(iata, {}) or metar_alerts.get(iata, {}) or {}).get('type', "STABLE")
    cur_xw = calculate_xwind(d.get('w_dir', 0), max(d.get('w_spd', 0), d.get('w_gst', 0)), info['rwy'])
    
    alt_list = []
    for g in [a for a in base_airports.keys() if a not in metar_alerts and a not in taf_alerts]:
        if g != iata and g in base_airports:
            dist = calculate_dist(info['lat'], info['lon'], base_airports[g]['lat'], base_airports[g]['lon'])
            alt_xw = 0
            score = (dist * 0.6) + (alt_xw * 2.5)
            alt_list.append({"iata": g, "dist": dist, "xw": "CHK", "score": score})
    alt_list = sorted(alt_list, key=lambda x: x['score'])[:3]
    rwy_brief = f"RWY {int(info['rwy']/10):02d}/{int(((info['rwy']+180)%360)/10):02d}"
    this_trend = next((m['trend'] for m in map_markers if m['iata'] == iata), "➡️")
    
    st.markdown(f"""<div class="reason-box"><h3>{iata} Strategy Brief {this_trend}</h3><div style="display:flex; gap:40px;"><div style="flex:1;"><p><b>Active Hazards ({time_horizon}):</b> {issue_desc}. Live {rwy_brief} X-Wind <b>{cur_xw}kt</b>.</p><p><b>Tactical Alternate Recommendations:</b></p><table class="alt-table"><tr><th>Alternate</th><th>Dist (NM)</th><th>Horizon XW</th><th>Probability</th></tr>{"".join([f"<tr><td><b>{a['iata']}</b></td><td>{a['dist']}</td><td>{a['xw']} kt</td><td><span class='alt-highlight'>{'HIGH' if a['score'] < 150 else 'STABLE'}</span></td></tr>" for a in alt_list])}</table></div><div style="flex:1;"><div style="padding:10px; background:#f9f9f9; border-radius:5px; border-left:4px solid #002366; margin-bottom:10px;"><b>LIVE METAR</b><div style="font-family:monospace; font-size:14px;">{bold_hazard(d.get('raw_m'))}</div></div><div style="padding:10px; background:#f9f9f9; border-radius:5px; border-left:4px solid #002366;"><b>LIVE TAF</b><div style="font-family:monospace; font-size:14px;">{bold_hazard(d.get('raw_t'))}</div></div></div></div></div>""", unsafe_allow_html=True)
    
    if st.button("Close Strategy Brief"): 
        st.session_state.investigate_iata = "None"
        st.rerun()

# 10. HANDOVER LOG
st.markdown('<div class="section-header">📝 Shift Handover Log</div>', unsafe_allow_html=True)
current_time = datetime.now().strftime('%H:%M')
h_txt = f"HANDOVER {current_time}Z | SCAN WINDOW: {time_horizon}\n" + "="*50 + "\n"
for i_ata, d_taf in taf_alerts.items(): h_txt += f"{i_ata}: {d_taf['type']} ({d_taf['time']})\n"
st.text_area("Handover Report:", value=h_txt, height=200, key="handover_v292_stable")
