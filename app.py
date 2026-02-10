import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG & BRANDING
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

st.markdown("""
    <style>
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; font-family: 'Arial', sans-serif; }
    /* Custom Styling for Clickable Alert Buttons */
    div.stButton > button {
        width: 100%;
        border-radius: 4px;
        border: none;
        color: white;
        text-align: left;
        padding: 12px;
        margin-bottom: 5px;
        font-weight: bold;
    }
    /* Hover effects to show they are clickable */
    div.stButton > button:hover { opacity: 0.8; color: white; border: 1px solid white; }
    </style>
    """, unsafe_allow_html=True)

# 2. FULL 2026 FLEET DATABASE (Verified Complete)
airports = {
    # --- CITYFLYER ---
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

    # --- EUROFLYER ---
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
    "ALG": {"icao": "DAAG", "name": "Algiers", "fleet": "Euroflyer", "rwy": 270, "lat": 36.691, "lon": 3.215},
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
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774}
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# --- UI HEADER ---
st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

# Sidebar
st.sidebar.image("https://upload.wikimedia.org/wikipedia/en/thumb/d/de/British_Airways_Logo.svg/1200px-British_Airways_Logo.svg.png", use_container_width=True)
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

# Initialize Session State for Investigator
if 'investigate_iata' not in st.session_state:
    st.session_state.investigate_iata = "None"

counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
active_alerts = {}
map_markers = []

# Process Weather Data
for iata, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            metar = Metar(info['icao']); metar.update()
            temp = metar.data.temperature.value if metar.data.temperature else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            xw = get_xwind(w_dir, w_spd, info['rwy'])
            
            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)

            reasons = []
            color = "#008000"
            alert_type = None

            if xw > 25: reasons.append(f"Excessive X-Wind ({xw}kt)"); color = "#d6001a"; alert_type = "red"
            if vis < 800: reasons.append(f"Low Visibility ({vis}m)"); color = "#d6001a"; alert_type = "red"
            if ceiling < 200: reasons.append(f"Low Ceiling ({ceiling}ft)"); color = "#d6001a"; alert_type = "red"
            
            if not alert_type:
                if xw > 18 or vis < 1500 or ceiling < 500:
                    color = "#eb8f34"; alert_type = "amber"
                    if xw > 18: reasons.append(f"Marginal X-Wind ({xw}kt)")
                    if vis < 1500: reasons.append(f"LVO Conditions ({vis}m)")
                    if ceiling < 500: reasons.append(f"LVO Ceiling ({ceiling}ft)")
            
            if iata == "IVL" and temp <= -25:
                color = "#005a9c"; alert_type = "arctic"; reasons.append(f"Extreme Cold ({temp}°C)")

            if alert_type:
                active_alerts[iata] = {"type": alert_type, "reasons": reasons, "metar": metar.raw}
                counts[info['fleet']][ "red" if alert_type=="red" else "orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "name": info['name'], "temp": temp, "xw": xw, "ceiling": ceiling, "raw": metar.raw})
        except: continue

# Map View Settings
map_center = [48.0, 5.0]; zoom = 4
if st.session_state.investigate_iata != "None":
    target = airports[st.session_state.investigate_iata]
    map_center = [target["lat"], target["lon"]]; zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
for mkr in map_markers:
    folium.CircleMarker(
        location=[mkr['lat'], mkr['lon']],
        radius=12 if mkr['iata'] == st.session_state.investigate_iata else (6 if zoom < 6 else 12),
        color=mkr['color'], fill=True, fill_opacity=0.9,
        popup=folium.Popup(f"<b>{mkr['iata']}</b><br>{mkr['raw']}", max_width=300, show=(mkr['iata'] == st.session_state.investigate_iata))
    ).add_to(m)

# UI RENDER
c1, c2 = st.columns(2)
with c1: st.metric("Cityflyer Fleet", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
with c2: st.metric("Euroflyer Fleet", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")
m_col, a_col = st.columns([3.5, 1])
with m_col: st_folium(m, width=1100, height=750, key="occ_v_clickable")
with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    # Clickable Buttons
    for iata, data in active_alerts.items():
        btn_color = "#d6001a" if data["type"] == "red" else "#eb8f34" if data["type"] == "amber" else "#005a9c"
        if st.button(f"{iata}: Investigating Issues", key=f"btn_{iata}", help="Click to open analysis"):
            st.session_state.investigate_iata = iata
            st.rerun()
    
    if st.session_state.investigate_iata != "None" and st.session_state.investigate_iata in active_alerts:
        data = active_alerts[st.session_state.investigate_iata]
        st.markdown(f"""
        <div style="background:white; border:1px solid #ddd; padding:15px; border-radius:5px; margin-top:10px; border-top:5px solid #002366; color:black;">
            <h4 style="margin-top:0;">{st.session_state.investigate_iata} Analysis</h4>
            <ul>{"".join([f"<li>{r}</li>" for r in data['reasons']])}</ul>
            <hr><small><b>METAR:</b> {data['metar']}</small><br>
            <button onclick="window.location.reload();" style="margin-top:10px; border:none; background:#eee; padding:5px; border-radius:3px; cursor:pointer;">Clear Analysis</button>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Reset View"):
            st.session_state.investigate_iata = "None"
            st.rerun()
