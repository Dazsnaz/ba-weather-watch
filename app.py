import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
import re
import io
import os
import pandas as pd
from datetime import datetime, timedelta, timezone

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC HUD", page_icon="✈️", initial_sidebar_state="expanded")

# 2. V30.7 "PLAY NICE" CSS + COLOR & DROPDOWN FIXES
st.markdown('<meta http-equiv="refresh" content="900">', unsafe_allow_html=True)

st.markdown("""
    <style>
    /* 1. REMOVE ALL PADDING SO MAP TOUCHES THE EDGES */
    .block-container, [data-testid="stMainBlockContainer"] {
        padding-top: 0rem !important;
        padding-bottom: 0rem !important;
        padding-left: 0rem !important;
        padding-right: 0rem !important;
        max-width: 100% !important;
    }
    
    /* 2. HEADER RESTORED: Matches dark background so Arrow stays visible */
    header[data-testid="stHeader"] {
        background-color: #001a33 !important;
    }
    .stAppToolbar { display: none !important; }
    
    /* 3. BA RED SIDEBAR ARROW (Highly visible in top left) */
    [data-testid="collapsedControl"], button[kind="header"] {
        background-color: #d6001a !important;
        border-radius: 5px !important;
        margin-top: 10px !important;
        margin-left: 10px !important;
        padding: 5px !important;
    }
    [data-testid="collapsedControl"] svg, button[kind="header"] svg {
        fill: white !important;
        color: white !important;
    }
    
    /* 4. FORCE MAP TO FILL EXACT VIEWPORT HEIGHT */
    iframe[title="streamlit_folium.st_folium"] {
        height: 100vh !important;
    }
    
    /* 5. FORCE SIDEBAR BACKGROUND TO DARK BLUE */
    section[data-testid="stSidebar"] { background-color: #002366 !important; border-right: 3px solid #d6001a !important; }
    section[data-testid="stSidebar"] > div { background-color: #002366 !important; }
    
    /* 6. SIDEBAR TEXT CONTRAST (White text on Blue) */
    section[data-testid="stSidebar"] p, 
    section[data-testid="stSidebar"] span, 
    section[data-testid="stSidebar"] h1, 
    section[data-testid="stSidebar"] h2, 
    section[data-testid="stSidebar"] h3, 
    section[data-testid="stSidebar"] label {
        color: white !important;
    }
    
    /* 7. DROPDOWN MENU FIX (Dark text on White background) */
    div[data-baseweb="select"] > div { background-color: white !important; border: 2px solid #d6001a !important; }
    div[data-baseweb="select"] * { color: #002366 !important; font-weight: bold !important; }
    ul[role="listbox"] { background-color: white !important; }
    ul[role="listbox"] li { color: #002366 !important; font-weight: bold !important; }
    div[data-testid="stDateInput"] div { background-color: white !important; color: #002366 !important; font-weight: bold !important;}
    
    /* 8. ALERT BUTTONS */
    .stButton button { width: 100% !important; border: 1px solid white !important; font-weight: bold !important; }
    .stButton button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; }
    .stButton button[kind="primary"] { background-color: #d6001a !important; color: white !important; }
    
    /* 9. EXPANDERS */
    div[data-testid="stExpander"] { background-color: #001a33 !important; border: 1px solid #005a9c !important; border-radius: 8px !important; margin-bottom: 10px !important;}
    div[data-testid="stExpander"] summary p { font-size: 1.1rem !important; color: white !important; }
    
    /* 10. OVERLAYS */
    .floating-hud { position: absolute; top: 65px; right: 20px; background-color: rgba(0, 35, 102, 0.85); border: 2px solid #d6001a; padding: 10px 25px; border-radius: 8px; color: white; font-weight: bold; z-index: 9999; backdrop-filter: blur(5px); box-shadow: 0 4px 10px rgba(0,0,0,0.5); display: flex; gap: 20px; font-size: 1.1rem; pointer-events: none; }
    
    .leaflet-tooltip, .leaflet-popup-content-wrapper { background: white !important; border: 2px solid #002366 !important; padding: 0 !important; opacity: 1 !important; }
    </style>
""", unsafe_allow_html=True)

# 3. UTILITIES & ROBUST CSV LOADER
def get_safe_num(val, default=0):
    if val is None: return default
    try: return float(val)
    except (ValueError, TypeError): return default

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
        if 'DEP' in df.columns: df['DEP'] = df['DEP'].astype(str).str.strip().str.upper()
        if 'ARR' in df.columns: df['ARR'] = df['ARR'].astype(str).str.strip().str.upper()
        df['DATE_OBJ'] = pd.to_datetime(df['DATE'], format='%d/%m/%y', errors='coerce').dt.date
        df['DATE_OBJ'] = df['DATE_OBJ'].fillna(pd.to_datetime(df['DATE'], dayfirst=True, errors='coerce').dt.date)
        return df
    except Exception as e:
        return pd.DataFrame()

# 4. MASTER DATABASE
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer", "spec": True},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer", "spec": False},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Both", "spec": False},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer", "spec": True},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer", "spec": True},
    "ZRH": {"icao": "LSZH", "lat": 47.458, "lon": 8.548, "rwy": 160, "fleet": "Cityflyer", "spec": False},
    "GVA": {"icao": "LSGG", "lat": 46.237, "lon": 6.109, "rwy": 220, "fleet": "Both", "spec": False},
    "BER": {"icao": "EDDB", "lat": 52.362, "lon": 13.501, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "FRA": {"icao": "EDDF", "lat": 50.033, "lon": 8.571, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "LIN": {"icao": "LIML", "lat": 45.445, "lon": 9.277, "rwy": 360, "fleet": "Cityflyer", "spec": False},
    "MAD": {"icao": "LEMD", "lat": 40.494, "lon": -3.567, "rwy": 140, "fleet": "Cityflyer", "spec": False},
    "IBZ": {"icao": "LEIB", "lat": 38.873, "lon": 1.373, "rwy": 60, "fleet": "Both", "spec": False},
    "PMI": {"icao": "LEPA", "lat": 39.551, "lon": 2.738, "rwy": 240, "fleet": "Both", "spec": False},
    "AGP": {"icao": "LEMG", "lat": 36.675, "lon": -4.499, "rwy": 130, "fleet": "Both", "spec": False},
    "FAO": {"icao": "LPFR", "lat": 37.017, "lon": -7.965, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "SEN": {"icao": "EGMC", "lat": 51.571, "lon": 0.701, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Both", "spec": False},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Both", "spec": True},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer", "spec": True},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Both", "spec": False},
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
    "PSA": {"icao": "LIRP", "lat": 43.683, "lon": 10.392, "rwy": 40, "fleet": "Both", "spec": False},
    "BLQ": {"icao": "LIPE", "lat": 44.535, "lon": 11.288, "rwy": 120, "fleet": "Both", "spec": False},
    "PXO": {"icao": "LPPS", "lat": 33.073, "lon": -16.349, "rwy": 180, "fleet": "Both", "spec": False},
    "MUC": {"icao": "EDDM", "lat": 48.353, "lon": 11.786, "rwy": 80, "fleet": "Both", "spec": False},
}

# STARTUP MEMORY (This fixes the AttributeError crash!)
if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if "map_center" not in st.session_state: st.session_state.map_center = [50.0, 10.0]
if "map_zoom" not in st.session_state: st.session_state.map_zoom = 5
SCHEDULE_FILE = "active_schedule.csv"

# 5. WEATHER ENGINE (Runs first so Sidebar can use the data)
@st.cache_data(ttl=900)
def get_raw_weather_master(airport_dict):
    raw_res = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update(); t = Taf(info['icao']); t.update()
            raw_res[iata] = {"m_obj": m, "t_obj": t, "status": "online"}
        except: raw_res[iata] = {"status": "offline"}
    return raw_res

raw_weather_bundle = get_raw_weather_master(base_airports)

def process_weather_for_horizon(bundle, airport_dict, horizon_limit, xw_threshold):
    processed = {}
    cutoff_time = datetime.now(timezone.utc) + timedelta(hours=horizon_limit)
    for iata, data in bundle.items():
        if data['status'] == "offline" or "m_obj" not in data:
            processed[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A", "f_issues": [], "f_wind_spd":0, "f_wind_dir":0, "w_spd":0, "w_dir":0, "f_time": ""}
            continue
        
        m, t, info = data['m_obj'], data.get('t_obj'), airport_dict[iata]
        v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
        
        m_vis = m.data.visibility.value if (m.data and hasattr(m.data, 'visibility') and m.data.visibility) else 9999
        m_cig = 9999
        if m.data and hasattr(m.data, 'clouds') and m.data.clouds:
            for lyr in m.data.clouds:
                if lyr.type in ['BKN', 'OVC'] and lyr.base: m_cig = min(m_cig, lyr.base * 100)
        
        w_issues = []
        f_time = ""
        
        if t and hasattr(t, 'data') and t.data and hasattr(t.data, 'forecast') and t.data.forecast:
            for line in t.data.forecast:
                if not hasattr(line, 'start_time') or not line.start_time or line.start_time.dt > cutoff_time: continue
                l_raw = line.raw.upper()
                if re.search(r'(-SN|\+SN|\bSN\b|\bFZ|\bFG\b)', l_raw): w_issues.append("WINTER/FOG")
                if (hasattr(line, 'visibility') and line.visibility and line.visibility.value is not None and line.visibility.value < v_lim): w_issues.append("VIS")
                
                l_dir = get_safe_num(line.wind_direction.value, info['rwy']) if (hasattr(line, 'wind_direction') and line.wind_direction) else info['rwy']
                l_spd = max(get_safe_num(line.wind_speed.value) if (hasattr(line, 'wind_speed') and line.wind_speed) else 0, get_safe_num(line.wind_gust.value) if (hasattr(line, 'wind_gust') and line.wind_gust) else 0)
                
                if calculate_xwind(l_dir, l_spd, info['rwy']) >= xw_threshold: w_issues.append("XWIND")
                elif l_spd > 25: w_issues.append("WINDY")
                
                if iata == "FLR" and abs(l_spd * math.cos(math.radians(l_dir - 50))) >= 10: w_issues.append("TAILWIND(>10kt)")
                if w_issues and not f_time: f_time = f"{line.start_time.dt.strftime('%H')}Z"; break
        
        w_dir = get_safe_num(m.data.wind_direction.value) if (m.data and hasattr(m.data, 'wind_direction') and m.data.wind_direction) else 0
        w_spd = get_safe_num(m.data.wind_speed.value) if (m.data and hasattr(m.data, 'wind_speed') and m.data.wind_speed) else 0
        w_gst = get_safe_num(m.data.wind_gust.value) if (m.data and hasattr(m.data, 'wind_gust') and m.data.wind_gust) else 0
        
        processed[iata] = {"vis": m_vis, "cig": m_cig, "status": "online", "w_dir": w_dir, "w_spd": w_spd, "w_gst": w_gst, "raw_m": m.raw or "N/A", "raw_t": t.raw if t else "N/A", "f_issues": list(set(w_issues)), "f_time": f_time}
    return processed

# Placeholder to store user settings before scanning weather
temp_horizon_hours = 6
temp_xw_limit = 25

# 6. SIDEBAR MASTER PANEL (Strategy Brief & Menus inside!)
with st.sidebar:
    st.markdown("<h2 style='text-align: center; color: white; margin-bottom: 0px;'>✈️ COMMAND HUD</h2>", unsafe_allow_html=True)
    
    # ---- DYNAMIC STRATEGY BRIEF RENDERED INSIDE THE SIDEBAR ----
    if st.session_state.investigate_iata != "None":
        iata = st.session_state.investigate_iata
        st.markdown("<hr style='margin:10px 0; border: 1px solid #d6001a;'>", unsafe_allow_html=True)
        st.markdown(f"<h3 style='color: #eb8f34; text-align: center; margin-top: 0px;'>📋 {iata} STRATEGY BRIEF</h3>", unsafe_allow_html=True)
        
        # Pull data just for this station
        d = process_weather_for_horizon(raw_weather_bundle, base_airports, 24, 25).get(iata, {})
        info = base_airports.get(iata, {"rwy": 0, "lat": 0, "lon": 0})
        cur_w_dir = get_safe_num(d.get('w_dir', 0))
        cur_w_spd = get_safe_num(d.get('w_spd', 0))
        cur_w_gst = get_safe_num(d.get('w_gst', 0))
        cur_xw = calculate_xwind(cur_w_dir, max(cur_w_spd, cur_w_gst), info['rwy'])
        
        # Calculate Alternates
        preferred_alts = []
        if iata == "FLR": preferred_alts = ["PSA", "BLQ"]
        elif iata == "FNC": preferred_alts = ["PXO"]
        elif iata == "INN": preferred_alts = ["MUC"]
        
        alt_list = []
        for g in base_airports.keys():
            if g != iata:
                dist = calculate_dist(info['lat'], info['lon'], base_airports[g]['lat'], base_airports[g]['lon'])
                alt_wx = process_weather_for_horizon(raw_weather_bundle, base_airports, 24, 25).get(g, {})
                alt_w_dir = get_safe_num(alt_wx.get('w_dir', 0))
                alt_w_spd = get_safe_num(alt_wx.get('w_spd', 0))
                alt_w_gst = get_safe_num(alt_wx.get('w_gst', 0))
                alt_xw = calculate_xwind(alt_w_dir, max(alt_w_spd, alt_w_gst), base_airports[g]['rwy'])
                score = dist
                if g in preferred_alts: score -= 1000 
                alt_list.append({"iata": g, "dist": dist, "xw": alt_xw, "score": score})
        
        alt_list = sorted(alt_list, key=lambda x: x['score'])[:3]
        alt_rows = "".join([f"<tr style='border-bottom: 1px solid #aaa;'><td><b>{a['iata']}</b></td><td>{a['dist']}</td><td>{a['xw']}kt</td></tr>" for a in alt_list])
        
        st.markdown(f"""
        <div style="background-color: white; padding: 15px; border-radius: 8px; color: #002366; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3);">
            <b style="font-size: 15px;">Live {int(info['rwy']/10):02d}/{int(((info['rwy']+180)%360)/10):02d} X-Wind:</b> <b style="color:#d6001a;">{cur_xw} kt</b><br>
            <hr style="margin: 10px 0; border: 1px solid #ccc;">
            <b style="color:#d6001a;">Tactical Alternates:</b>
            <table style="width:100%; text-align: left; font-size: 13px; margin-top: 5px;">
                <tr style="background-color: #002366; color: white;"><th>Alt</th><th>Dist</th><th>X-Wind</th></tr>
                {alt_rows}
            </table>
            <hr style="margin: 10px 0; border: 1px solid #ccc;">
            <b style="color:#002366;">METAR:</b><br><span style="font-family: monospace; font-size: 12px;">{bold_hazard(d.get('raw_m'))}</span><br><br>
            <b style="color:#002366;">TAF:</b><br><span style="font-family: monospace; font-size: 12px;">{bold_hazard(d.get('raw_t'))}</span>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("❌ CLOSE STRATEGY BRIEF", type="primary", use_container_width=True):
            st.session_state.investigate_iata = "None"
            st.rerun()
            
    st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
    alerts_container = st.container() # For red/amber buttons
    st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
    
    # TUCKED AWAY SETTINGS & SCHEDULE
    with st.expander("⚙️ SCHEDULE & SETTINGS", expanded=False):
        uploaded_file = st.file_uploader("Upload CSV Schedule", type=["csv"])
        if uploaded_file is not None:
            with open(SCHEDULE_FILE, "wb") as f: f.write(uploaded_file.getvalue())
            st.success("✅ Global Schedule Updated!")
        
        selected_date = st.date_input("📅 Operations Date:", value=datetime.now().date())
        if st.button("🔄 MANUAL DATA REFRESH"): st.cache_data.clear(); st.rerun()

    with st.expander("🎯 TACTICAL FILTERS", expanded=False):
        time_horizon = st.radio("SCAN WINDOW", ["Next 6 Hours", "Next 12 Hours", "Next 24 Hours"], index=0)
        temp_horizon_hours = 6 if "6" in time_horizon else (12 if "12" in time_horizon else 24)
        temp_xw_limit = st.slider("X-WIND LIMIT (KT)", 15, 35, 25)
        
        filter_map = {"XWIND": "XWIND", "WINDY (Gusts >25)": "WINDY", "FOG": "FOG", "WINTER": "WINTER", "TSRA": "TSRA", "VIS (<Limits)": "VIS", "LOW CLOUD": "CLOUD", "FLR TAILWIND": "TAILWIND(>10kt)"}
        hazard_filter = st.selectbox("ISOLATE HAZARD", ["Show All Network", "Any Amber/Red Alert", "XWIND", "WINDY (Gusts >25)", "FOG", "WINTER", "TSRA", "VIS (<Limits)", "LOW CLOUD", "FLR TAILWIND"])
        
        show_cf = st.checkbox("Cityflyer (CFE)", value=True)
        show_ef = st.checkbox("Euroflyer (EFW)", value=True)
        map_theme = st.radio("MAP THEME", ["Dark Mode", "Light Mode"])

    log_container = st.container()

# 7. PARSE SCHEDULE & PROCESS WEATHER
flight_schedule = pd.DataFrame()
active_stations = set()
if os.path.exists(SCHEDULE_FILE):
    with open(SCHEDULE_FILE, "rb") as f: saved_bytes = f.read()
    flight_schedule = load_schedule_robust(saved_bytes)
    if not flight_schedule.empty and 'DATE_OBJ' in flight_schedule.columns:
        flight_schedule = flight_schedule[flight_schedule['DATE_OBJ'] == selected_date]
        if not flight_schedule.empty: active_stations = set(flight_schedule['DEP'].dropna()) | set(flight_schedule['ARR'].dropna())

display_airports = {k: v for k, v in base_airports.items() if k in active_stations} if (not flight_schedule.empty and active_stations) else {k: v for k, v in base_airports.items() if k not in ["PSA", "BLQ", "PXO", "MUC"]}

weather_data = process_weather_for_horizon(raw_weather_bundle, base_airports, temp_horizon_hours, temp_xw_limit)

current_utc_date = datetime.now(timezone.utc).date()
current_utc_time_str = datetime.now(timezone.utc).strftime('%H%M')
display_time = datetime.now(timezone.utc).strftime("%H:%M")

# 8. MAP MARKERS & ALERTS (WITH AIRCRAFT/FLEET AWARENESS)
metar_alerts, taf_alerts, map_markers = {}, {}, []
for iata, info in display_airports.items():
    data = weather_data.get(iata)
    if not data: continue
    
    # -------------------------------------------------------------
    # DYNAMIC FLEET AWARENESS CHECK
    # Scans the actual CSV to see if E90 (CF) or 32E/31E (EF) are flying today
    # -------------------------------------------------------------
    is_cf_station, is_ef_station = False, False
    if not flight_schedule.empty:
        sf = flight_schedule[(flight_schedule['DEP'] == iata) | (flight_schedule['ARR'] == iata)]
        if not sf.empty:
            ac_types = sf['AC'].dropna().astype(str).str.upper().unique()
            if any('E90' in ac for ac in ac_types): is_cf_station = True
            if any(ac in ['31E', '32E', '320', '319'] for ac in ac_types): is_ef_station = True
            if not is_cf_station and not is_ef_station: # Catchall
                is_cf_station = (info.get('fleet') in ['Cityflyer', 'Both'])
                is_ef_station = (info.get('fleet') in ['Euroflyer', 'Both'])
        else:
            is_cf_station = (info.get('fleet') in ['Cityflyer', 'Both'])
            is_ef_station = (info.get('fleet') in ['Euroflyer', 'Both'])
    else:
        is_cf_station = (info.get('fleet') in ['Cityflyer', 'Both'])
        is_ef_station = (info.get('fleet') in ['Euroflyer', 'Both'])
        
    # If user unchecked CF, and it's ONLY a CF station, skip it.
    if not ((is_cf_station and show_cf) or (is_ef_station and show_ef)): continue

    v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
    m_issues = []
    
    cur_w_dir = get_safe_num(data.get('w_dir', 0))
    cur_w_spd = get_safe_num(data.get('w_spd', 0))
    cur_w_gst = get_safe_num(data.get('w_gst', 0))
    cur_xw = calculate_xwind(cur_w_dir, max(cur_w_spd, cur_w_gst), info['rwy'])
    raw_m = data['raw_m'].upper()
    
    if re.search(r'\bFG\b', raw_m): m_issues.append("FOG")
    if re.search(r'(-SN|\+SN|\bSN\b|\bFZ)', raw_m): m_issues.append("WINTER")
    if data.get('vis', 9999) < v_lim: m_issues.append("VIS")
    if data.get("cig", 9999) < c_lim: m_issues.append("CLOUD")
    if re.search(r'\bTS|VCTS', raw_m): m_issues.append("TSRA")
    if cur_xw >= temp_xw_limit: m_issues.append("XWIND")
    if cur_w_gst > 25 and "XWIND" not in m_issues: m_issues.append("WINDY")
    
    if iata == "FLR":
        tw_comp = abs(max(cur_w_spd, cur_w_gst) * math.cos(math.radians(cur_w_dir - 50)))
        if tw_comp >= 10: m_issues.append("TAILWIND(>10kt)")
    
    trend_icon = "➡️"
    if not m_issues and data['f_issues']: trend_icon = "📈"
    elif m_issues and not data['f_issues']: trend_icon = "📉"
    
    color = "#008000"
    if m_issues: color = "#d6001a" if any(x in m_issues for x in ["FOG","WINTER","VIS","TSRA","XWIND","TAILWIND(>10kt)"]) else "#eb8f34"
    elif data['f_issues']: color = "#eb8f34"
    
    rwy_text = f"RWY {int(info['rwy']/10):02d}/{int(((info['rwy']+180)%360)/10):02d}"
    if m_issues: metar_alerts[iata] = {"type": "/".join(m_issues), "hex": "primary" if color == "#d6001a" else "secondary"}
    if data['f_issues']: taf_alerts[iata] = {"type": "+".join(data['f_issues']), "time": data['f_time'], "hex": "secondary"}
    
    if hazard_filter == "Any Amber/Red Alert" and color == "#008000": continue
    elif hazard_filter not in ["Show All Network", "Any Amber/Red Alert"] and filter_map.get(hazard_filter) not in m_issues and filter_map.get(hazard_filter) not in data['f_issues']: continue
    
    m_bold, t_bold = bold_hazard(data.get('raw_m', 'N/A')), bold_hazard(data.get('raw_t', 'N/A'))
    
    inbound_html = ""
    if not flight_schedule.empty:
        arr_flights = flight_schedule[flight_schedule['ARR'] == iata].sort_values(by='STA')
        if not arr_flights.empty:
            rows = ""
            for _, row in arr_flights.iterrows():
                sta_raw = str(row['STA']).strip()
                sta_clean = sta_raw.replace(':', '') 
                flight_date = row['DATE_OBJ']
                if flight_date < current_utc_date: continue
                if flight_date == current_utc_date and sta_clean < current_utc_time_str: continue
                
                flt, dep, arr, canc = str(row['FLT']).strip(), str(row['DEP']).strip(), str(row['ARR']).strip(), row.get('Cancellation Reason', None)
                f_status, f_color = "SCHED", "#008000"
                if pd.notna(canc) and str(canc).strip() != "": f_status, f_color = "CANC", "#d6001a"
                elif color == "#d6001a": f_status, f_color = "AT RISK", "#d6001a"
                elif color == "#eb8f34": f_status, f_color = "CAUTION", "#eb8f34"
                    
                rows += f"<tr style='border-bottom: 1px solid #ddd;'><td style='color:{f_color}; font-weight:bold; padding:4px;'>{f_status}</td><td style='padding:4px;'>{flt}</td><td style='padding:4px;'>{dep}</td><td style='padding:4px;'>{arr}</td><td style='padding:4px;'>{sta_raw}</td></tr>"
            if rows: inbound_html = f"""<div style='margin-top:15px; border-top: 2px solid #002366; padding-top:10px;'><b style='color:#002366; font-size:14px;'>🛬 YET TO ARRIVE ({selected_date.strftime('%d/%m/%Y')})</b><div style='max-height: 200px; overflow-y: auto; margin-top:5px; border: 1px solid #ccc; background: #fff;'><table style='width:100%; text-align:left; font-size:12px; border-collapse: collapse; color: #000;'><tr style='background:#002366; color:#fff;'><th style='padding:5px;'>Status</th><th style='padding:5px;'>FLT</th><th style='padding:5px;'>DEP</th><th style='padding:5px;'>ARR</th><th style='padding:5px;'>STA</th></tr>{rows}</table></div></div>"""
    
    shared_content = f"""<div style="width:580px; color:black !important; font-family:monospace; font-size:14px; background:white; padding:15px; border-radius:5px;"><b style="color:#002366; font-size:18px;">{iata} STATUS {trend_icon}</b><div style="margin-top:8px; padding:10px; border-left:6px solid {color}; background:#f9f9f9; font-size:16px;"><b style="color:#002366;">{rwy_text} X-Wind:</b> <b>{cur_xw} KT</b><br><b>ACTUAL:</b> {"/".join(m_issues) if m_issues else "STABLE"}<br><b>FORECAST ({temp_horizon_hours}H):</b> {"+".join(data['f_issues']) if data['f_issues'] else "NIL"}</div><hr style="border:1px solid #ddd;"><div style="display:flex; gap:12px;"><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; white-space: pre-wrap; word-wrap: break-word;"><b>METAR</b><br>{m_bold}</div><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; white-space: pre-wrap; word-wrap: break-word;"><b>TAF</b><br>{t_bold}</div></div>{inbound_html}</div>"""
    map_markers.append({"lat": info['lat'], "lon": info['lon'], "color": color, "content": shared_content, "iata": iata, "trend": trend_icon})

# 9. INJECT BUTTONS INTO THE SIDEBAR
with alerts_container:
    if not metar_alerts and not taf_alerts: st.success("✅ Network Stable - No Active Hazards")
    if metar_alerts:
        st.markdown("<p style='color:white; margin-bottom: 5px;'>🔴 <b>ACTUAL HAZARDS (NOW)</b></p>", unsafe_allow_html=True)
        for iata, d in metar_alerts.items():
            if st.button(f"{iata} | {d['type']}", key=f"m_{iata}", type=d['hex']): st.session_state.investigate_iata = iata
    if taf_alerts:
        st.markdown(f"<p style='color:white; margin-top: 15px; margin-bottom: 5px;'>🟠 <b>FORECAST HAZARDS ({temp_horizon_hours}H)</b></p>", unsafe_allow_html=True)
        for iata, d in taf_alerts.items():
            if st.button(f"{iata} | {d['time']} {d['type']}", key=f"f_{iata}", type="secondary"): st.session_state.investigate_iata = iata

with log_container:
    h_txt = f"HANDOVER {display_time}Z | SCAN WINDOW: {temp_horizon_hours}H\n" + "="*50 + "\n"
    for i_ata, d_taf in taf_alerts.items(): h_txt += f"{i_ata}: {d_taf['type']} ({d_taf['time']})\n"
    with st.expander("📝 SHIFT HANDOVER LOG", expanded=False):
        st.text_area("Handover Report:", value=h_txt, height=200, label_visibility="collapsed")

# 10. RENDER FULL SCREEN MAP
st.markdown(f'<div class="floating-hud"><div>📡 Command Edition</div><div>|</div><div style="color: #eb8f34;">{display_time} Z</div></div>', unsafe_allow_html=True)

m = folium.Map(location=st.session_state.map_center, zoom_start=st.session_state.map_zoom, tiles=("CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"), scrollWheelZoom=False)
for mkr in map_markers:
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True, popup=folium.Popup(mkr['content'], max_width=650, auto_pan=True, auto_pan_padding=(150, 150)), tooltip=folium.Tooltip(mkr['content'], direction='top', sticky=False)).add_to(m)

# 1200 height pushes it out so it fills modern screens cleanly without absolute vh hacks
st_folium(m, width=None, height=1200, use_container_width=True, key="map_stable_v30")
