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
    [data-testid="stSidebar"] button {
        background-color: #005a9c !important;
        color: white !important;
        border: 1px solid white !important;
    }
    
    .marquee {
        width: 100%; background-color: #d6001a; color: white; white-space: nowrap;
        overflow: hidden; box-sizing: border-box; padding: 12px; font-weight: bold;
        border-radius: 5px; margin-bottom: 15px; font-family: 'Arial', sans-serif;
        border: 2px solid white;
    }
    .marquee span { display: inline-block; padding-left: 100%; animation: marquee 25s linear infinite; }
    @keyframes marquee { 0% { transform: translate(0, 0); } 100% { transform: translate(-100%, 0); } }

    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; font-weight: bold; height: 3.5em; width: 100%; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; font-weight: bold; height: 3.5em; width: 100%; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span, .reason-box li { color: #002366 !important; }
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
if 'manual_stations' not in st.session_state:
    st.session_state.manual_stations = {}
if 'investigate_iata' not in st.session_state:
    st.session_state.investigate_iata = "None"

# --- MASTER FLEET DATABASE (42 STATIONS) ---
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer"},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer"},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer"},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer"},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer"},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer"},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer"},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer"},
    "SEN": {"icao": "EGMC", "lat": 51.571, "lon": 0.701, "rwy": 230, "fleet": "Cityflyer"},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer"},
    "AGP": {"icao": "LEMG", "lat": 36.675, "lon": -4.499, "rwy": 130, "fleet": "Cityflyer"},
    "BER": {"icao": "EDDB", "lat": 52.362, "lon": 13.501, "rwy": 250, "fleet": "Cityflyer"},
    "FRA": {"icao": "EDDF", "lat": 50.033, "lon": 8.571, "rwy": 250, "fleet": "Cityflyer"},
    "LIN": {"icao": "LIML", "lat": 45.445, "lon": 9.277, "rwy": 360, "fleet": "Cityflyer"},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer"},
    "GVA": {"icao": "LSGG", "lat": 46.237, "lon": 6.109, "rwy": 220, "fleet": "Cityflyer"},
    "ZRH": {"icao": "LSZH", "lat": 47.458, "lon": 8.548, "rwy": 160, "fleet": "Cityflyer"},
    "MAD": {"icao": "LEMD", "lat": 40.494, "lon": -3.567, "rwy": 140, "fleet": "Cityflyer"},
    "IBZ": {"icao": "LEIB", "lat": 38.873, "lon": 1.373, "rwy": 60, "fleet": "Cityflyer"},
    "PMI": {"icao": "LEPA", "lat": 39.551, "lon": 2.738, "rwy": 240, "fleet": "Cityflyer"},
    "FAO": {"icao": "LPFR", "lat": 37.017, "lon": -7.965, "rwy": 280, "fleet": "Cityflyer"},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer"},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer"},
    "OPO": {"icao": "LPPR", "lat": 41.242, "lon": -8.678, "rwy": 350, "fleet": "Euroflyer"},
    "LYS": {"icao": "LFLL", "lat": 45.726, "lon": 5.090, "rwy": 350, "fleet": "Euroflyer"},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer"},
    "SZG": {"icao": "LOWS", "lat": 47.794, "lon": 13.004, "rwy": 330, "fleet": "Euroflyer"},
    "BOD": {"icao": "LFBD", "lat": 44.828, "lon": -0.716, "rwy": 230, "fleet": "Euroflyer"},
    "GNB": {"icao": "LFLS", "lat": 45.363, "lon": 5.330, "rwy": 90, "fleet": "Euroflyer"},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer"},
    "TRN": {"icao": "LIMF", "lat": 45.202, "lon": 7.649, "rwy": 360, "fleet": "Euroflyer"},
    "VRN": {"icao": "LIPX", "lat": 45.396, "lon": 10.888, "rwy": 40, "fleet": "Euroflyer"},
    "ALC": {"icao": "LEAL", "lat": 38.282, "lon": -0.558, "rwy": 100, "fleet": "Euroflyer"},
    "SVQ": {"icao": "LEZL", "lat": 37.418, "lon": -5.893, "rwy": 270, "fleet": "Euroflyer"},
    "RAK": {"icao": "GMMX", "lat": 31.606, "lon": -8.036, "rwy": 100, "fleet": "Euroflyer"},
    "AGA": {"icao": "GMAD", "lat": 30.325, "lon": -9.413, "rwy": 90, "fleet": "Euroflyer"},
    "SSH": {"icao": "HESH", "lat": 27.977, "lon": 34.394, "rwy": 40, "fleet": "Euroflyer"},
    "PFO": {"icao": "LCPH", "lat": 34.718, "lon": 32.486, "rwy": 290, "fleet": "Euroflyer"},
    "LCA": {"icao": "LCLK", "lat": 34.875, "lon": 33.625, "rwy": 220, "fleet": "Euroflyer"},
    "FUE": {"icao": "GCLP", "lat": 28.452, "lon": -13.864, "rwy": 10, "fleet": "Euroflyer"},
    "TFS": {"icao": "GCTS", "lat": 28.044, "lon": -16.572, "rwy": 70, "fleet": "Euroflyer"},
    "ACE": {"icao": "GCRR", "lat": 28.945, "lon": -13.605, "rwy": 30, "fleet": "Euroflyer"},
}

# 5. SIDEBAR CONTROLS
st.sidebar.markdown("### 🔍 Search & Add")
with st.sidebar.form("manual_add", clear_on_submit=True):
    new_iata = st.text_input("IATA").upper()
    new_icao = st.text_input("ICAO").upper()
    if st.form_submit_button("Add Station"):
        try:
            m = Metar(new_icao); m.update()
            st.session_state.manual_stations[new_iata] = {"icao": new_icao, "lat": m.data.station.latitude, "lon": m.data.station.longitude, "rwy": 0, "fleet": "Ad-Hoc"}
            st.cache_data.clear()
            st.rerun()
        except: st.sidebar.error("Invalid ICAO")

if st.session_state.manual_stations:
    for iata in list(st.session_state.manual_stations.keys()):
        if st.sidebar.button(f"Remove {iata}", key=f"del_{iata}"):
            del st.session_state.manual_stations[iata]
            st.cache_data.clear()
            st.rerun()

st.sidebar.markdown("---")
fleet_filter = st.sidebar.multiselect("Fleets", ["Cityflyer", "Euroflyer", "Ad-Hoc"], default=["Cityflyer", "Euroflyer", "Ad-Hoc"])

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
            results[iata] = {"vis": v, "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0, "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0, "ceiling": c, "raw_m": m.raw, "raw_t": t.raw}
        except: continue
    return results

weather_data = get_weather(all_airports)

# 7. ALERTS LOGIC
active_alerts = {}; red_list = []; green_stations = []; map_markers = []
for iata, data in weather_data.items():
    info = all_airports[iata]
    if info['fleet'] in fleet_filter:
        color = "#008000"; alert = None
        if data['vis'] < 800 or data['ceiling'] < 200: color = "#d6001a"; alert = "red"; red_list.append(iata)
        elif data['vis'] < 1500 or data['ceiling'] < 500: color = "#eb8f34"; alert = "amber"
        
        if alert: active_alerts[iata] = {"type": alert, "vis": data['vis'], "cig": data['ceiling'], "metar": data['raw_m'], "taf": data['raw_t']}
        else: green_stations.append(iata)
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_m'], "taf": data['raw_t']})

# --- UI RENDER ---
if red_list:
    st.markdown(f'<div class="marquee"><span>🚨 RED ALERT: {", ".join(red_list)} BELOW MINIMA</span></div>', unsafe_allow_html=True)

st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

# MAP
m = folium.Map(location=[48.0, 5.0], zoom_start=5, tiles="CartoDB dark_matter")
for mkr in map_markers:
    popup = f"<div style='color:black;'><b>{mkr['iata']}</b><br>METAR: {mkr['metar']}<br>TAF: {mkr['taf']}</div>"
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True, popup=folium.Popup(popup, max_width=400)).add_to(m)
st_folium(m, width=1400, height=500, key="main_map")

# ALERTS BOX
st.markdown("### ⚠️ Operational Alerts")
if active_alerts:
    cols = st.columns(6)
    for i, (iata, d) in enumerate(active_alerts.items()):
        with cols[i % 6]:
            if st.button(f"{iata}: {d['type'].upper()}", key=f"alert_{iata}", type="primary" if d['type'] == "red" else "secondary"):
                st.session_state.investigate_iata = iata

if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    st.markdown(f"""
    <div class="reason-box">
        <h3>{st.session_state.investigate_iata} Analysis</h3>
        <p><b>Impact:</b> High risk of holding/diversions. Visibility: {d['vis']}m, Ceiling: {d['cig']}ft.</p>
        <hr>
        <div style="display:flex; gap:20px;">
            <div><b>METAR:</b><br><small>{d['metar']}</small></div>
            <div><b>TAF:</b><br><small>{d['taf']}</small></div>
        </div>
    </div>""", unsafe_allow_html=True)
