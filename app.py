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
    
    /* FIX: SIDEBAR SEARCH VISIBILITY */
    [data-testid="stSidebar"] .stTextInput input {
        color: #002366 !important;
        background-color: white !important;
        font-weight: bold;
    }
    [data-testid="stSidebar"] label p { color: white !important; font-weight: bold; }

    /* SCROLLING ALERT FLASH */
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

# --- FLEET DATABASE ---
airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer"},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer"},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer"},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer"},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer"},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer"},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer"},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer"},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer"},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer"},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer"},
    "SZG": {"icao": "LOWS", "lat": 47.794, "lon": 13.004, "rwy": 330, "fleet": "Euroflyer"},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer"},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer"}
}

@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            v = m.data.visibility.value if m.data.visibility else 9999
            g = m.data.wind_gust.value if m.data.wind_gust else 0
            c = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        c = min(c, layer.base * 100)
            results[iata] = {
                "vis": v, "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "gust": g, "ceiling": c, "raw_metar": m.raw, "raw_taf": t.raw,
                "lat": info['lat'], "lon": info['lon']
            }
        except: continue
    return results

weather_data = get_fleet_weather(airports)

# SIDEBAR
st.sidebar.markdown("### 🔍 Airport Search")
search_iata = st.sidebar.text_input("Enter IATA Code", "").upper()
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in airports: st.session_state.investigate_iata = search_iata

# PROCESS ALERTS
active_alerts = {}; green_stations = []; red_airports = []; gust_alerts = []
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}

for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        xw = round(abs(data['w_spd'] * math.sin(math.radians(data['w_dir'] - info['rwy']))), 1)
        color = "#008000"; alert_type = None; reason = ""
        
        if data['gust'] > 35: gust_alerts.append(f"{iata} ({data['gust']}KT)")
        
        if xw > 25: alert_type = "red"; reason = "HIGH X-WIND"
        elif data['vis'] < 800: alert_type = "red"; reason = "LOW VIS"
        elif data['ceiling'] < 200: alert_type = "red"; reason = "LOW CEILING"
        elif xw > 18: alert_type = "amber"; reason = "MARGINAL X-WIND"
        elif data['vis'] < 1500: alert_type = "amber"; reason = "MARGINAL VIS"
        elif data['ceiling'] < 500: alert_type = "amber"; reason = "MARGINAL CIG"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "reason": reason, "vis": data['vis'], "ceiling": data['ceiling'], "xw": xw, "metar": data['raw_metar'], "taf": data['raw_taf']}
            counts[info['fleet']]["red" if alert_type=="red" else "orange"] += 1
            if alert_type == "red": red_airports.append(f"{iata} ({reason})")
        else: 
            counts[info['fleet']]["green"] += 1
            green_stations.append(iata)

# --- UI RENDER ---

# DYNAMIC MARQUEE (Network Events + Wind Gusts)
flash_list = red_airports + gust_alerts
if len(flash_list) >= 3:
    alert_text = "  |  ".join(flash_list)
    st.markdown(f'<div class="marquee"><span>🚨 NETWORK ADVISORY: {alert_text}</span></div>', unsafe_allow_html=True)

st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

# MAP & METRICS (Simplified for space)
c1, c2 = st.columns(2)
c1.metric("Cityflyer Status", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
c2.metric("Euroflyer Status", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

# ANALYSIS & DIVERSION
if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    
    # Calculate Trend
    trend = "➡️ Stable"
    if "BECMG" in d['taf'] or "TEMPO" in d['taf']: trend = "📈 Variable/Improving"
    if "FG" in d['metar'] or "DZ" in d['metar']: trend = "📉 Deteriorating"
    
    # Calculate Diversion
    alt_iata = "None"; alt_dist = 9999
    cur_lat, cur_lon = weather_data[st.session_state.investigate_iata]['lat'], weather_data[st.session_state.investigate_iata]['lon']
    for g_iata in green_stations:
        dist = calculate_dist(cur_lat, cur_lon, weather_data[g_iata]['lat'], weather_data[g_iata]['lon'])
        if dist < alt_dist: alt_dist = dist; alt_iata = g_iata
    
    st.markdown(f"""
    <div class="reason-box">
        <h3>{st.session_state.investigate_iata} Analysis | Trend: {trend}</h3>
        <p><b>Weather:</b> {d['reason']} | Vis: {d['vis']}m | Cig: {d['ceiling']}ft | XW: {d['xw']}kt.</p>
        <p style="color:#d6001a !important;"><b>✈️ Diversion Assist:</b> Nearest Green Alternate: <b>{alt_iata}</b> ({alt_dist} NM).</p>
        <p><b>Impact:</b> High risk of <b>ATC holding or diversions</b>. Expect operational delays.</p>
        <hr>
        <small><b>METAR:</b> {d['metar']} | <b>TAF:</b> {d['taf']}</small>
    </div>
    """, unsafe_allow_html=True)
