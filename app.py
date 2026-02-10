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
    .stButton > button { background-color: #005a9c !important; color: white !important; border: 1px solid white !important; width: 100%; text-transform: uppercase; }
    .marquee { width: 100%; background-color: #d6001a; color: white; white-space: nowrap; overflow: hidden; padding: 12px; font-weight: bold; border-radius: 5px; margin-bottom: 15px; border: 2px solid white; }
    .marquee span { display: inline-block; padding-left: 100%; animation: marquee 25s linear infinite; }
    @keyframes marquee { 0% { transform: translate(0, 0); } 100% { transform: translate(-100%, 0); } }
    .ba-header { background-color: #002366; padding: 20px; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; }
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span, .reason-box i { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. UTILITIES
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 # Nautical Miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

# 4. MASTER DATABASE (FULL 42+ STATIONS)
base_airports = {
    # CITYFLYER (CFE)
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer", "spec": True},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer", "spec": False},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "SEN": {"icao": "EGMC", "lat": 51.571, "lon": 0.701, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer", "spec": True},
    "AGP": {"icao": "LEMG", "lat": 36.675, "lon": -4.499, "rwy": 130, "fleet": "Cityflyer", "spec": False},
    "BER": {"icao": "EDDB", "lat": 52.362, "lon": 13.501, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "FRA": {"icao": "EDDF", "lat": 50.033, "lon": 8.571, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "LIN": {"icao": "LIML", "lat": 45.445, "lon": 9.277, "rwy": 360, "fleet": "Cityflyer", "spec": False},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer", "spec": True},
    "GVA": {"icao": "LSGG", "lat": 46.237, "lon": 6.109, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "ZRH": {"icao": "LSZH", "lat": 47.458, "lon": 8.548, "rwy": 160, "fleet": "Cityflyer", "spec": False},
    "MAD": {"icao": "LEMD", "lat": 40.494, "lon": -3.567, "rwy": 140, "fleet": "Cityflyer", "spec": False},
    "IBZ": {"icao": "LEIB", "lat": 38.873, "lon": 1.373, "rwy": 60, "fleet": "Cityflyer", "spec": False},
    "PMI": {"icao": "LEPA", "lat": 39.551, "lon": 2.738, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "FAO": {"icao": "LPFR", "lat": 37.017, "lon": -7.965, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    # EUROFLYER (EFW)
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "OPO": {"icao": "LPPR", "lat": 41.242, "lon": -8.678, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "LYS": {"icao": "LFLL", "lat": 45.726, "lon": 5.090, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer", "spec": True},
    "SZG": {"icao": "LOWS", "lat": 47.794, "lon": 13.004, "rwy": 330, "fleet": "Euroflyer", "spec": False},
    "BOD": {"icao": "LFBD", "lat": 44.828, "lon": -0.716, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "GNB": {"icao": "LFLS", "lat": 45.363, "lon": 5.330, "rwy": 90, "fleet": "Euroflyer", "spec": False},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "TRN": {"icao": "LIMF", "lat": 45.202, "lon": 7.649, "rwy": 360, "fleet": "Euroflyer", "spec": False},
    "VRN": {"icao": "LIPX", "lat": 45.396, "lon": 10.888, "rwy": 40, "fleet": "Euroflyer", "spec": False},
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
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer", "spec": True},
    "ALG": {"icao": "DAAG", "lat": 36.691, "lon": 3.215, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "GRZ": {"icao": "LOWG", "lat": 46.991, "lon": 15.439, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "RBA": {"icao": "GMME", "lat": 34.051, "lon": -6.751, "rwy": 30, "fleet": "Euroflyer", "spec": False},
}

# 5. SESSION STATE
if 'manual_stations' not in st.session_state: st.session_state.manual_stations = {}
if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"

# 6. SIDEBAR
st.sidebar.title("🛠️ COMMAND SETTINGS")
if st.sidebar.button("🔄 MANUAL DATA REFRESH"):
    st.cache_data.clear(); st.rerun()

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

# 7. DATA FETCH & PREDICTIVE LOGIC
all_airports = {**base_airports, **st.session_state.manual_stations}

@st.cache_data(ttl=900)
def get_weather_intelligence(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            
            # Forecast Analysis (TAF Parsing)
            f_vis, f_cig = 9999, 9999
            if t.data:
                for line in t.data.forecast:
                    if line.visibility: f_vis = min(f_vis, line.visibility.value)
                    if line.clouds:
                        for layer in line.clouds:
                            if layer.type in ['BKN', 'OVC'] and layer.base:
                                f_cig = min(f_cig, layer.base * 100)

            results[iata] = {
                "vis": m.data.visibility.value if m.data.visibility else 9999,
                "ceiling": 9999, "w_dir": m.data.wind_direction.value or 0, "w_spd": m.data.wind_speed.value or 0,
                "f_vis": f_vis, "f_cig": f_cig, "raw_m": m.raw, "raw_t": t.raw, "status": "online"
            }
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        results[iata]["ceiling"] = min(results[iata]["ceiling"], layer.base * 100)
        except: results[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A"}
    return results

weather_data = get_weather_intelligence(all_airports)

# 8. TACTICAL ALERT PROCESSING
active_alerts = {}; red_list = []; green_stations = []; map_markers = []

for iata, data in weather_data.items():
    info = all_airports[iata]
    v_limit, c_limit = (1500, 500) if info['spec'] else (800, 200)
    
    # Crosswind Calculation
    xw = round(abs(data.get('w_spd', 0) * math.sin(math.radians(data.get('w_dir', 0) - info['rwy']))), 1) if info['rwy'] else 0
    
    color = "#008000"; alert_type = None; reason = ""; impact = ""

    if data['status'] == "offline":
        color = "#808080"
    elif data['vis'] < v_limit or data['ceiling'] < c_limit:
        color = "#d6001a"; alert_type = "MINIMA"; reason = f"Currently below limits: {data['vis']}m / {data['ceiling']}ft"; impact = "STATION CLOSED. Immediate diversions required."; red_list.append(iata)
    elif data['f_vis'] < v_limit or data['f_cig'] < c_limit:
        color = "#d6001a"; alert_type = "FCAST-MIN"; reason = "Forecast indicates drop below minima"; impact = "Action: Review fuel and alternate availability for approaching traffic."
    elif xw > 25:
        color = "#eb8f34"; alert_type = "X-WIND"; reason = f"Crosswind at {xw}kt"; impact = "Approach limits reached for most fleet. Expect go-arounds."
    elif "TSRA" in data['raw_t']:
        color = "#eb8f34"; alert_type = "TSRA"; reason = "Thunderstorms forecast in TAF"; impact = "Convective flow restrictions. Possible holding required."
    elif "FG" in data['raw_m']:
        color = "#eb8f34"; alert_type = "FOG"; reason = "Active Fog observed"; impact = "LVP likely in effect. arrival rates reduced."
    elif data['f_vis'] < (v_limit * 2):
        color = "#eb8f34"; alert_type = "FCAST-VIS"; reason = "Forecast marginal visibility"; impact = "Monitor trend. Prepare for LVP procedures."

    if alert_type:
        active_alerts[iata] = {"type": alert_type, "reason": reason, "impact": impact, "metar": data['raw_m'], "taf": data['raw_t'], "color": "primary" if color == "#d6001a" else "secondary"}
    elif data['status'] == "online":
        green_stations.append(iata)
    
    map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_m'], "taf": data['raw_t']})

# --- UI RENDER ---
if red_list: st.markdown(f'<div class="marquee"><span>🚨 CRITICAL: {", ".join(red_list)} AT MINIMA</span></div>', unsafe_allow_html=True)
st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

# 9. MAP RENDER
tile = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"
m = folium.Map(location=[45.0, 5.0], zoom_start=5, tiles=tile)

for mkr in map_markers:
    popup_html = f"""
    <div style="width: 450px; color: black !important; font-family: monospace;">
        <h4 style="margin:0; color:#002366; border-bottom:1px solid #002366;">{mkr['iata']} Status</h4>
        <div style="display:flex; gap:10px; margin-top:10px;">
            <div style="flex:1; background:#f0f2f6; padding:8px; border-radius:4px;"><b>METAR</b><br><small>{mkr['metar']}</small></div>
            <div style="flex:1; background:#f0f2f6; padding:8px; border-radius:4px;"><b>TAF</b><br><small>{mkr['taf']}</small></div>
        </div>
    </div>
    """
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True, popup=folium.Popup(popup_html, max_width=500)).add_to(m)
st_folium(m, width=1400, height=500, key=f"map_v{len(map_markers)}")

# 10. ANALYSIS
st.markdown("### ⚠️ Strategic Operational Analysis")
if active_alerts:
    cols = st.columns(6)
    for i, (iata, d) in enumerate(active_alerts.items()):
        with cols[i % 6]:
            if st.button(f"{iata}: {d['type']}", key=f"btn_{iata}", type=d['color']):
                st.session_state.investigate_iata = iata

if st.session_state.investigate_iata in active_alerts:
    d = active_alerts[st.session_state.investigate_iata]
    cur = all_airports[st.session_state.investigate_iata]
    alt_iata = "None"; min_dist = 9999
    for g in green_stations:
        dist = calculate_dist(cur['lat'], cur['lon'], all_airports[g]['lat'], all_airports[g]['lon'])
        if dist < min_dist: min_dist = dist; alt_iata = g

    st.markdown(f"""
    <div class="reason-box">
        <h3>{st.session_state.investigate_iata} Strategy Brief: {d['type']}</h3>
        <p><b>Weather Summary:</b> {d['reason']}</p>
        <p><b>Impact Statement:</b> <i>{d['impact']}</i></p>
        <p style="color:#d6001a !important; font-size:1.1em;"><b>✈️ Strategic Alternate:</b> Closest Green station is <b>{alt_iata}</b> ({min_dist} NM).</p>
        <hr>
        <div style="display:flex; gap:20px;">
            <div><b>METAR:</b><br><small>{d['metar']}</small></div>
            <div><b>TAF:</b><br><small>{d['taf']}</small></div>
        </div>
    </div>""", unsafe_allow_html=True)
    if st.button("Close Analysis"): st.session_state.investigate_iata = "None"; st.rerun()
