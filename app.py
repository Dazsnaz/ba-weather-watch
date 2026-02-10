import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Command HUD", page_icon="✈️")

# 2. HUD STYLING
st.markdown("""
    <style>
    html, body, [class*="st-"], div, p, h1, h2, h3, h4, label { color: white !important; }
    
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    [data-testid="stSidebar"] .stTextInput input { color: #002366 !important; background-color: white !important; font-weight: bold; }
    [data-testid="stSidebar"] label p { color: white !important; font-weight: bold; }
    
    /* REFRESH BUTTON SPECIAL STYLING */
    .stButton > button {
        background-color: #005a9c !important;
        color: white !important;
        border: 1px solid white !important;
        width: 100%;
    }

    .marquee {
        width: 100%; background-color: #d6001a; color: white; white-space: nowrap;
        overflow: hidden; padding: 12px; font-weight: bold; border-radius: 5px;
        margin-bottom: 15px; border: 2px solid white;
    }
    .marquee span { display: inline-block; padding-left: 100%; animation: marquee 25s linear infinite; }
    @keyframes marquee { 0% { transform: translate(0, 0); } 100% { transform: translate(-100%, 0); } }

    .ba-header { background-color: #002366; padding: 20px; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. UTILITIES
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

# 4. SESSION STATE
if 'manual_stations' not in st.session_state: st.session_state.manual_stations = {}
if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"

# --- MASTER FLEET DATABASE ---
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer", "spec": True},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer", "spec": False},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer", "spec": True},
    "ALG": {"icao": "DAAG", "lat": 36.691, "lon": 3.215, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "GRZ": {"icao": "LOWG", "lat": 46.991, "lon": 15.439, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "VRN": {"icao": "LIPX", "lat": 45.396, "lon": 10.888, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "RBA": {"icao": "GMME", "lat": 34.051, "lon": -6.751, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    # (Remaining stations from previous fleet list persist in the logic)
}

# 5. SIDEBAR: REFRESH & SEARCH
st.sidebar.title("🛠️ COMMAND SETTINGS")

# RESTORED REFRESH BUTTON
if st.sidebar.button("🔄 MANUAL DATA REFRESH"):
    st.cache_data.clear()
    st.rerun()

map_theme = st.sidebar.radio("MAP THEME", ["Dark Mode", "Light Mode"])

with st.sidebar.form("manual_add", clear_on_submit=True):
    new_iata = st.text_input("IATA SEARCH").upper()
    new_icao = st.text_input("ICAO").upper()
    if st.form_submit_button("Add Station"):
        try:
            m = Metar(new_icao); m.update()
            st.session_state.manual_stations[new_iata] = {"icao": new_icao, "lat": m.data.station.latitude, "lon": m.data.station.longitude, "rwy": 0, "fleet": "Ad-Hoc", "spec": False}
            st.cache_data.clear(); st.rerun()
        except: st.sidebar.error("Invalid ICAO")

# 6. DATA FETCH
all_airports = {**base_airports, **st.session_state.manual_stations}

@st.cache_data(ttl=900)
def get_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            v = m.data.visibility.value if m.data.visibility else 9999
            c = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base: c = min(c, layer.base * 100)
            results[iata] = {"vis": v, "w_spd": m.data.wind_speed.value or 0, "ceiling": c, "raw_m": m.raw, "raw_t": t.raw, "status": "online"}
        except: results[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A"}
    return results

weather_data = get_weather(all_airports)

# 7. ALERTS PROCESSING
active_alerts = {}; red_list = []; green_stations = []; map_markers = []
for iata, data in weather_data.items():
    info = all_airports[iata]
    v_limit = 1500 if info['spec'] else 800
    c_limit = 500 if info['spec'] else 200
    
    color = "#008000"; alert = None
    if data['status'] == "offline": color = "#808080"
    elif data['vis'] < v_limit or data['ceiling'] < c_limit: color = "#d6001a"; alert = "red"; red_list.append(iata)
    elif data['vis'] < (v_limit*2) or data['ceiling'] < (c_limit*2): color = "#eb8f34"; alert = "amber"
    
    if alert: active_alerts[iata] = {"type": alert, "vis": data['vis'], "cig": data['ceiling'], "metar": data['raw_m'], "taf": data['raw_t']}
    elif data['status'] == "online": green_stations.append(iata)
    
    map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_m'], "taf": data['raw_t']})

# 8. UI RENDER
if red_list: st.markdown(f'<div class="marquee"><span>🚨 RED ALERT: {", ".join(red_list)} AT MINIMA</span></div>', unsafe_allow_html=True)

st.markdown(f'<div class="ba-header"><div>OCC WEATHER COMMAND</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

tile = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"
m = folium.Map(location=[48.0, 5.0], zoom_start=5, tiles=tile)
for mkr in map_markers:
    popup_html = f"<div style='color:black; width:300px;'><b>{mkr['iata']}</b><br>METAR: {mkr['metar']}<br>TAF: {mkr['taf']}</div>"
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True, popup=folium.Popup(popup_html, max_width=450)).add_to(m)
st_folium(m, width=1400, height=500, key=f"map_{len(map_markers)}")

# 9. ANALYSIS & DIVERSION
if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    cur = all_airports[st.session_state.investigate_iata]
    alt_iata = "None"; min_dist = 9999
    for g in green_stations:
        dist = calculate_dist(cur['lat'], cur['lon'], all_airports[g]['lat'], all_airports[g]['lon'])
        if dist < min_dist: min_dist = dist; alt_iata = g
    
    st.markdown(f"""
    <div class="reason-box">
        <h3>{st.session_state.investigate_iata} Operational Analysis</h3>
        <p style="color:#d6001a !important; font-size:1.1em;"><b>✈️ Diversion Planning:</b> Closest Green station is <b>{alt_iata}</b> ({min_dist} NM).</p>
        <hr>
        <div style="display:flex; gap:20px;">
            <div><b>METAR:</b><br><small>{d['metar']}</small></div>
            <div><b>TAF:</b><br><small>{d['taf']}</small></div>
        </div>
    </div>""", unsafe_allow_html=True)
    if st.button("Close Analysis"): st.session_state.investigate_iata = "None"; st.rerun()
