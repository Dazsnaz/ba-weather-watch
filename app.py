import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG & BRANDING
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

# CUSTOM CSS FOR OCC SIDEBAR AND LIGHTER BLUE TABS
st.markdown("""
    <style>
    /* Header Styling */
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; font-family: 'Arial', sans-serif; }
    
    /* SIDEBAR NAVY BLUE BACKGROUND */
    [data-testid="stSidebar"] {
        background-color: #002366 !important;
        color: white !important;
    }
    
    /* ACTIVE FLEET TABS / MULTISELECT STYLING */
    span[data-baseweb="tag"] {
        background-color: #005a9c !important; /* Lighter Royal Blue */
        color: white !important;
    }
    div[data-baseweb="select"] > div {
        background-color: #005a9c !important;
        color: white !important;
        border: none !important;
    }
    
    /* Text colors in sidebar */
    [data-testid="stSidebar"] h3, [data-testid="stSidebar"] label, [data-testid="stSidebar"] p {
        color: white !important;
    }

    /* Button Colors */
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; width: 100%; font-weight: bold; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; width: 100%; font-weight: bold; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 15px; border-radius: 5px; margin-top: 10px; border-top: 5px solid #002366; color: black; }
    </style>
    """, unsafe_allow_html=True)

# 2. CACHED WEATHER ENGINE
@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            results[iata] = {
                "temp": m.data.temperature.value if m.data.temperature else 0,
                "vis": m.data.visibility.value if m.data.visibility else 9999,
                "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "ceiling": 9999, "raw_metar": m.raw, "raw_taf": t.raw
            }
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        results[iata]["ceiling"] = min(results[iata]["ceiling"], layer.base * 100)
        except: continue
    return results

# 3. FULL 2026 FLEET DATABASE
airports = {
    # CITYFLYER
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "RTM": {"icao": "EHRD", "name": "Rotterdam", "fleet": "Cityflyer", "rwy": 240, "lat": 51.957, "lon": 4.440},
    "DUB": {"icao": "EIDW", "name": "Dublin", "fleet": "Cityflyer", "rwy": 280, "lat": 53.421, "lon": -6.270},
    "GLA": {"icao": "EGPF", "name": "Glasgow", "fleet": "Cityflyer", "rwy": 230, "lat": 55.871, "lon": -4.433},
    "EDI": {"icao": "EGPH", "name": "Edinburgh", "fleet": "Cityflyer", "rwy": 240, "lat": 55.950, "lon": -3.363},
    "BHD": {"icao": "EGAC", "name": "Belfast City", "fleet": "Cityflyer", "rwy": 220, "lat": 54.618, "lon": -5.872},
    "STN": {"icao": "EGSS", "name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "SEN": {"icao": "EGMC", "name": "Southend", "fleet": "Cityflyer", "rwy": 230, "lat": 51.571, "lon": 0.701},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "AGP": {"icao": "LEMG", "name": "Malaga", "fleet": "Cityflyer", "rwy": 130, "lat": 36.675, "lon": -4.499},
    "BER": {"icao": "EDDB", "name": "Berlin", "fleet": "Cityflyer", "rwy": 250, "lat": 52.362, "lon": 13.501},
    "FRA": {"icao": "EDDF", "name": "Frankfurt", "fleet": "Cityflyer", "rwy": 250, "lat": 50.033, "lon": 8.571},
    "LIN": {"icao": "LIML", "name": "Milan Linate", "fleet": "Cityflyer", "rwy": 360, "lat": 45.445, "lon": 9.277},
    "CMF": {"icao": "LFLB", "name": "Chambery", "fleet": "Cityflyer", "rwy": 180, "lat": 45.638, "lon": 5.880},
    "GVA": {"icao": "LSGG", "name": "Geneva", "fleet": "Cityflyer", "rwy": 220, "lat": 46.237, "lon": 6.109},
    "ZRH": {"icao": "LSZH", "name": "Zurich", "fleet": "Cityflyer", "rwy": 160, "lat": 47.458, "lon": 8.548},
    "MAD": {"icao": "LEMD", "name": "Madrid", "fleet": "Cityflyer", "rwy": 140, "lat": 40.494, "lon": -3.567},
    "IBZ": {"icao": "LEIB", "name": "Ibiza", "fleet": "Cityflyer", "rwy": 60, "lat": 38.873, "lon": 1.373},
    "PMI": {"icao": "LEPA", "name": "Palma", "fleet": "Cityflyer", "rwy": 240, "lat": 39.551, "lon": 2.738},
    "FAO": {"icao": "LPFR", "name": "Faro", "fleet": "Cityflyer", "rwy": 280, "lat": 37.017, "lon": -7.965},
    # EUROFLYER
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "JER": {"icao": "EGJJ", "name": "Jersey", "fleet": "Euroflyer", "rwy": 260, "lat": 49.208, "lon": -2.195},
    "OPO": {"icao": "LPPR", "name": "Porto", "fleet": "Euroflyer", "rwy": 350, "lat": 41.242, "lon": -8.678},
    "LYS": {"icao": "LFLL", "name": "Lyon", "fleet": "Euroflyer", "rwy": 350, "lat": 45.726, "lon": 5.090},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "BOD": {"icao": "LFBD", "name": "Bordeaux", "fleet": "Euroflyer", "rwy": 230, "lat": 44.828, "lon": -0.716},
    "GNB": {"icao": "LFLS", "name": "Grenoble", "fleet": "Euroflyer", "rwy": 90, "lat": 45.363, "lon": 5.330},
    "NCE": {"icao": "LFMN", "name": "Nice", "fleet": "Euroflyer", "rwy": 40, "lat": 43.665, "lon": 7.215},
    "TRN": {"icao": "LIMF", "name": "Turin", "fleet": "Euroflyer", "rwy": 360, "lat": 45.202, "lon": 7.649},
    "VRN": {"icao": "LIPX", "name": "Verona", "fleet": "Euroflyer", "rwy": 40, "lat": 45.396, "lon": 10.888},
    "ALC": {"icao": "LEAL", "name": "Alicante", "fleet": "Euroflyer", "rwy": 100, "lat": 38.282, "lon": -0.558},
    "SVQ": {"icao": "LEZL", "name": "Seville", "fleet": "Euroflyer", "rwy": 270, "lat": 37.418, "lon": -5.893},
    "RAK": {"icao": "GMMX", "name": "Marrakesh", "fleet": "Euroflyer", "rwy": 100, "lat": 31.606, "lon": -8.036},
    "AGA": {"icao": "GMAD", "name": "Agadir", "fleet": "Euroflyer", "rwy": 90, "lat": 30.325, "lon": -9.413},
    "SSH": {"icao": "HESH", "name": "Sharm El Sheikh", "fleet": "Euroflyer", "rwy": 40, "lat": 27.977, "lon": 34.394},
    "PFO": {"icao": "LCPH", "name": "Paphos", "fleet": "Euroflyer", "rwy": 290, "lat": 34.718, "lon": 32.486},
    "LCA": {"icao": "LCLK", "name": "Larnaca", "fleet": "Euroflyer", "rwy": 220, "lat": 34.875, "lon": 33.625},
    "FUE": {"icao": "GCLP", "name": "Fuerteventura", "fleet": "Euroflyer", "rwy": 10, "lat": 28.452, "lon": -13.864},
    "TFS": {"icao": "GCTS", "name": "Tenerife South", "fleet": "Euroflyer", "rwy": 70, "lat": 28.044, "lon": -16.572},
    "ACE": {"icao": "GCRR", "name": "Lanzarote", "fleet": "Euroflyer", "rwy": 30, "lat": 28.945, "lon": -13.605},
    "LPA": {"icao": "GCLP", "name": "Gran Canaria", "fleet": "Euroflyer", "rwy": 30, "lat": 27.931, "lon": -15.386},
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "MLA": {"icao": "LMML", "name": "Malta", "fleet": "Euroflyer", "rwy": 310, "lat": 35.857, "lon": 14.477},
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# PULL CACHED DATA
weather_data = get_fleet_weather(airports)

# HEADER
st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

# SIDEBAR STYLING
st.sidebar.image("https://upload.wikimedia.org/wikipedia/en/thumb/d/de/British_Airways_Logo.svg/1024px-British_Airways_Logo.svg.png", use_container_width=True)
st.sidebar.markdown("### Fleet Selection")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])

map_theme = st.sidebar.radio("Map Theme", ["Light Mode", "Dark Mode"])
tile_style = "CartoDB positron" if map_theme == "Light Mode" else "CartoDB dark_matter"

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"

# PROCESS DATA
active_alerts = {}
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
map_markers = []

for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        xw = get_xwind(data['w_dir'], data['w_spd'], info['rwy'])
        reasons = []
        color = "#008000"
        alert_type = None

        if xw > 25 or data['vis'] < 800 or data['ceiling'] < 200:
            color = "#d6001a"; alert_type = "red"
            if xw > 25: reasons.append(f"X-Wind {xw}kt")
            if data['vis'] < 800: reasons.append(f"Vis {data['vis']}m")
            if data['ceiling'] < 200: reasons.append(f"Ceiling {data['ceiling']}ft")
        elif xw > 18 or data['vis'] < 1500 or data['ceiling'] < 500:
            color = "#eb8f34"; alert_type = "amber"
            if xw > 18: reasons.append(f"X-Wind {xw}kt")
            if data['vis'] < 1500: reasons.append(f"LVO Vis")
            if data['ceiling'] < 500: reasons.append(f"LVO Ceiling")
        
        if iata == "IVL" and data['temp'] <= -25:
            color = "#005a9c"; alert_type = "arctic"; reasons.append(f"Cold Alert {data['temp']}°C")

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "reasons": reasons, "metar": data['raw_metar']}
            counts[info['fleet']]["red" if alert_type=="red" else "orange"] += 1
        else:
            counts[info['fleet']]["green"] += 1
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "raw": data['raw_metar']})

# ALIGN STATUS BAR
stat_col1, stat_col2 = st.columns(2)
with stat_col1:
    st.metric("Cityflyer Fleet Status", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
with stat_col2:
    st.metric("Euroflyer Fleet Status", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")

# MAIN MAP AND ALERTS
m_col, a_col = st.columns([3.5, 1])

with m_col:
    map_center = [48.0, 5.0]; zoom = 4
    if st.session_state.investigate_iata in airports:
        target = airports[st.session_state.investigate_iata]
        map_center = [target["lat"], target["lon"]]; zoom = 10
    
    m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
    for mkr in map_markers:
        is_sel = mkr['iata'] == st.session_state.investigate_iata
        folium.CircleMarker(
            location=[mkr['lat'], mkr['lon']],
            radius=14 if is_sel else (6 if zoom < 6 else 10),
            color=mkr['color'], fill=True, fill_opacity=0.9,
            popup=folium.Popup(f"<b>{mkr['iata']}</b><br>{mkr['raw']}", max_width=300, show=is_sel)
        ).add_to(m)
    st_folium(m, width=1100, height=750, key="occ_main_map")

with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    for iata, d in active_alerts.items():
        btn_kind = "primary" if d['type'] == "red" else "secondary"
        if st.button(f"{iata}: Investigating Issues", key=f"btn_{iata}", type=btn_kind):
            st.session_state.investigate_iata = iata
            st.rerun()
    
    if st.session_state.investigate_iata in active_alerts:
        d = active_alerts[st.session_state.investigate_iata]
        st.markdown(f"""
        <div class="reason-box">
            <h4 style="margin:0;">{st.session_state.investigate_iata} Analysis</h4>
            <ul>{"".join([f"<li>{r}</li>" for r in d['reasons']])}</ul>
            <hr><small>{d['metar']}</small>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Close Analysis", key="close_btn"):
            st.session_state.investigate_iata = "None"
            st.rerun()
