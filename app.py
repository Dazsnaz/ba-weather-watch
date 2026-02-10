import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

# 2. CUSTOM OCC STYLING
st.markdown("""
    <style>
    html, body, [class*="st-"], div, p, h1, h2, h3, h4, label { color: white !important; }
    
    [data-testid="stSidebar"] .stTextInput input {
        color: #002366 !important;
        background-color: white !important;
        font-weight: bold;
    }
    [data-testid="stSidebar"] label p { color: white !important; font-weight: bold; }

    .marquee {
        width: 100%; background-color: #d6001a; color: white; white-space: nowrap;
        overflow: hidden; box-sizing: border-box; padding: 12px; font-weight: bold;
        border-radius: 5px; margin-bottom: 15px; font-family: 'Arial', sans-serif;
        border: 2px solid white;
    }
    .marquee span { display: inline-block; padding-left: 100%; animation: marquee 25s linear infinite; }
    @keyframes marquee { 0% { transform: translate(0, 0); } 100% { transform: translate(-100%, 0); } }

    [data-testid="stMetricValue"], [data-testid="stMetricLabel"] {
        background-color: #002366; padding: 10px; border-radius: 5px; border: 1px solid #005a9c;
    }
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; font-weight: bold; height: 3.5em; width: 100%; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; font-weight: bold; height: 3.5em; width: 100%; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. UTILITIES
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

# 4. SESSION STATE FOR AD-HOC STATIONS
if 'manual_stations' not in st.session_state:
    st.session_state.manual_stations = {}

# --- MASTER FLEET DATABASE ---
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer"},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer"},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer"},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer"},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer"},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer"},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer"},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer"},
}

# Combine baseline with manual stations
airports = {**base_airports, **st.session_state.manual_stations}

# SIDEBAR: STATION MANAGEMENT
st.sidebar.markdown("### ➕ Manual Station Add")
with st.sidebar.form("add_station", clear_on_submit=True):
    new_iata = st.text_input("IATA (e.g. CDG)").upper()
    new_icao = st.text_input("ICAO (e.g. LFPG)").upper()
    submitted = st.form_submit_button("Add to Map")
    if submitted and new_iata and new_icao:
        # Fetch initial data to get Lat/Lon
        try:
            m = Metar(new_icao); m.update()
            st.session_state.manual_stations[new_iata] = {
                "icao": new_icao, "lat": m.data.station.latitude, 
                "lon": m.data.station.longitude, "rwy": 0, "fleet": "Ad-Hoc"
            }
            st.rerun()
        except:
            st.sidebar.error("Invalid ICAO")

if st.session_state.manual_stations:
    st.sidebar.markdown("### 🗑️ Manage Ad-Hoc")
    for iata in list(st.session_state.manual_stations.keys()):
        if st.sidebar.button(f"Remove {iata}", key=f"del_{iata}"):
            del st.session_state.manual_stations[iata]
            st.rerun()

# 5. WEATHER LOGIC
@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            v = m.data.visibility.value if m.data.visibility else 9999
            c = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        c = min(c, layer.base * 100)
            results[iata] = {
                "vis": v, "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "ceiling": c, "raw_metar": m.raw, "raw_taf": t.raw,
                "lat": info['lat'], "lon": info['lon']
            }
        except: continue
    return results

weather_data = get_fleet_weather(airports)

# SIDEBAR SEARCH & FILTERS
st.sidebar.markdown("---")
search_iata = st.sidebar.text_input("Search Current Map", "").upper()
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer", "Ad-Hoc"], default=["Cityflyer", "Euroflyer", "Ad-Hoc"])

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in airports: st.session_state.investigate_iata = search_iata

# PROCESS ALERTS
active_alerts = {}; green_stations = []; red_airports = []; map_markers = []
for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        color = "#008000"; alert_type = None; reason = ""
        # Alert thresholds (simplified for demo)
        if data['vis'] < 800 or data['ceiling'] < 200: alert_type = "red"; reason = "CRITICAL WX"
        elif data['vis'] < 1500 or data['ceiling'] < 500: alert_type = "amber"; reason = "MARGINAL WX"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "reason": reason, "vis": data['vis'], "ceiling": data['ceiling'], "metar": data['raw_metar'], "taf": data['raw_taf']}
            color = "#d6001a" if alert_type == "red" else "#eb8f34"
            if alert_type == "red": red_airports.append(f"{iata}")
        else: green_stations.append(iata)
        
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_metar'], "taf": data['raw_taf']})

# --- RENDER ---
if len(red_airports) >= 3:
    st.markdown(f'<div class="marquee"><span>🚨 NETWORK ADVISORY: Red Alerts at {", ".join(red_airports)}</span></div>', unsafe_allow_html=True)

st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

# MAP
map_center = [48.0, 5.0]; zoom = 4
if st.session_state.investigate_iata in airports:
    target = airports[st.session_state.investigate_iata]
    map_center = [target["lat"], target["lon"]]; zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles="CartoDB dark_matter")
for mkr in map_markers:
    popup_html = f"<b>{mkr['iata']}</b><br>METAR: {mkr['metar']}"
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=10, color=mkr['color'], fill=True, popup=popup_html).add_to(m)
st_folium(m, width=1400, height=500, key="occ_map")

# ALERTS & ANALYSIS
st.markdown("### ⚠️ Operational Alerts")
if active_alerts:
    cols = st.columns(6)
    for idx, (iata, d) in enumerate(active_alerts.items()):
        with cols[idx % 6]:
            if st.button(f"{iata}: {d['reason']}", key=f"btn_{iata}", type="primary" if d['type'] == "red" else "secondary"):
                st.session_state.investigate_iata = iata
                st.rerun()

if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    # Diversion Assist
    alt_iata = "None"; min_dist = 9999
    cur = weather_data[st.session_state.investigate_iata]
    for g in green_stations:
        dist = calculate_dist(cur['lat'], cur['lon'], weather_data[g]['lat'], weather_data[g]['lon'])
        if dist < min_dist: min_dist = dist; alt_iata = g
    
    st.markdown(f"""<div class="reason-box"><h3>{st.session_state.investigate_iata} Analysis</h3>
    <p><b>Diversion Planning:</b> Closest Green station is <b>{alt_iata}</b> ({min_dist} NM).</p>
    <p>METAR: <small>{d['metar']}</small></p></div>""", unsafe_allow_html=True)
