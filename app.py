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
if 'manual_stations' not in st.session_state:
    st.session_state.manual_stations = {}

# --- MASTER FLEET DATABASE ---
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
    "LPA": {"icao": "GCLP", "lat": 27.931, "lon": -15.386, "rwy": 30, "fleet": "Euroflyer"},
    "IVL": {"icao": "EFIV", "lat": 68.607, "lon": 27.405, "rwy": 40, "fleet": "Euroflyer"},
    "MLA": {"icao": "LMML", "lat": 35.857, "lon": 14.477, "rwy": 310, "fleet": "Euroflyer"},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer"},
}

# 5. SIDEBAR: MANUAL ADD
st.sidebar.markdown("### ➕ Manual Station Add")
with st.sidebar.form("manual_add", clear_on_submit=True):
    new_iata = st.text_input("IATA (e.g. CDG)").upper()
    new_icao = st.text_input("ICAO (e.g. LFPG)").upper()
    submit_add = st.form_submit_button("Add to Map")
    if submit_add and new_iata and new_icao:
        try:
            m = Metar(new_icao); m.update()
            st.session_state.manual_stations[new_iata] = {
                "icao": new_icao, "lat": m.data.station.latitude, 
                "lon": m.data.station.longitude, "rwy": 0, "fleet": "Ad-Hoc"
            }
            st.cache_data.clear() 
            st.rerun()
        except: st.sidebar.error("Invalid ICAO")

if st.session_state.manual_stations:
    st.sidebar.markdown("### 🗑️ Manage Ad-Hoc")
    for iata in list(st.session_state.manual_stations.keys()):
        if st.sidebar.button(f"Remove {iata}", key=f"del_{iata}"):
            del st.session_state.manual_stations[iata]
            st.cache_data.clear()
            st.rerun()

# 6. DATA PROCESSING
all_airports = {**base_airports, **st.session_state.manual_stations}

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
                "lat": info['lat'], "lon": info['lon'], "icao": info['icao']
            }
        except: continue
    return results

weather_data = get_fleet_weather(all_airports)

# SIDEBAR REFRESH & SEARCH
st.sidebar.markdown("---")
if st.sidebar.button("🔄 Manual Data Refresh"):
    st.cache_data.clear()
    st.rerun()

search_iata = st.sidebar.text_input("IATA SEARCH", "").upper()
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer", "Ad-Hoc"], default=["Cityflyer", "Euroflyer", "Ad-Hoc"])

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in all_airports: st.session_state.investigate_iata = search_iata

# 7. ALERTS PROCESSING
active_alerts = {}; green_stations = []; red_airports = []; map_markers = []
for iata, data in weather_data.items():
    info = all_airports[iata]
    if info['fleet'] in fleet_filter:
        rwy_hiding = info.get('rwy', 0)
        xw = 0
        if rwy_hiding and isinstance(data['w_dir'], (int, float)):
             xw = round(abs(data['w_spd'] * math.sin(math.radians(data['w_dir'] - rwy_hiding))), 1)
        
        color = "#008000"; alert_type = None; reason = ""
        if xw > 25 or data['vis'] < 800 or data['ceiling'] < 200: 
            alert_type = "red"; reason = "CRITICAL"
        elif xw > 18 or data['vis'] < 1500 or data['ceiling'] < 500: 
            alert_type = "amber"; reason = "MARGINAL"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "reason": reason, "vis": data['vis'], "ceiling": data['ceiling'], "xw": xw, "metar": data['raw_metar'], "taf": data['raw_taf']}
            color = "#d6001a" if alert_type == "red" else "#eb8f34"
            if alert_type == "red": red_airports.append(f"{iata}")
        else: green_stations.append(iata)
        
        map_markers.append({"iata": iata, "icao": info['icao'], "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_metar'], "taf": data['raw_taf']})

# --- UI RENDER ---
if len(red_airports) >= 3:
    st.markdown(f'<div class="marquee"><span>🚨 NETWORK ADVISORY: Red Alerts at {", ".join(red_airports)}</span></div>', unsafe_allow_html=True)

st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

# 8. MAP RENDER
map_center = [48.0, 5.0]; zoom = 4
if st.session_state.investigate_iata in all_airports:
    target = all_airports[st.session_state.investigate_iata]
    map_center = [target["lat"], target["lon"]]; zoom = 10

tile_style = "CartoDB dark_matter" if st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"]) == "Dark Mode" else "CartoDB positron"
m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)

for mkr in map_markers:
    # DUAL PANEL POPUP LOGIC
    popup_html = f"""
    <div style="width: 500px; color: black !important; font-family: 'Courier New', monospace;">
        <h4 style="margin:0; color:#002366; border-bottom:1px solid #002366;">{mkr['iata']} / {mkr['icao']} Weather Data</h4>
        <div style="display:flex; gap:10px; margin-top:10px;">
            <div style="flex:1; background:#f0f2f6; padding:10px; border-radius:5px;">
                <b>METAR</b><br><small>{mkr['metar']}</small>
            </div>
            <div style="flex:1; background:#f0f2f6; padding:10px; border-radius:5px;">
                <b>TAF</b><br><small>{mkr['taf']}</small>
            </div>
        </div>
    </div>
    """
    folium.CircleMarker(
        location=[mkr['lat'], mkr['lon']], 
        radius=14 if mkr['iata'] == st.session_state.investigate_iata else 7, 
        color=mkr['color'], fill=True, fill_opacity=0.9,
        popup=folium.Popup(popup_html, max_width=600)
    ).add_to(m)

# Use a dynamic key based on markers length to force map refresh
st_folium(m, width=1400, height=500, key=f"occ_map_v{len(map_markers)}")

# 9. ALERTS & ANALYSIS
st.markdown("### ⚠️ Operational Alerts")
if active_alerts:
    cols = st.columns(6)
    for idx, (iata, d) in enumerate(active_alerts.items()):
        with cols[idx % 6]:
            if st.button(f"{iata}: {d['reason']}", key=f"btn_{iata}", type="primary" if d['type'] == "red" else "secondary"):
                st.session_state.investigate_iata = iata; st.rerun()

if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    alt_iata = "None"; min_dist = 9999
    cur = weather_data[st.session_state.investigate_iata]
    for g in green_stations:
        dist = calculate_dist(cur['lat'], cur['lon'], weather_data[g]['lat'], weather_data[g]['lon'])
        if dist < min_dist: min_dist = dist; alt_iata = g
    
    st.markdown(f"""
    <div class="reason-box">
        <h3>{st.session_state.investigate_iata} Operational Analysis</h3>
        <p><b>Weather Detail:</b> {d['reason']} identified. Vis: {d['vis']}m | Ceiling: {d['ceiling']}ft | XW: {d['xw']}kt.</p>
        <p style="color:#d6001a !important; font-size:1.1em;"><b>✈️ Diversion Planning:</b> Closest Green station is <b>{alt_iata}</b> ({min_dist} NM).</p>
        <p><b>Impact:</b> High risk of ATC slots or holding. Flow rate likely restricted.</p>
        <hr>
        <div style="display:flex; gap:20px;">
            <div><b>METAR:</b><br><small>{d['metar']}</small></div>
            <div><b>TAF:</b><br><small>{d['taf']}</small></div>
        </div>
    </div>""", unsafe_allow_html=True)
    if st.button("Close Analysis"): st.session_state.investigate_iata = "None"; st.rerun()
