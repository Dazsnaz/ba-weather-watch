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
    .ba-header {
        background-color: #002366;
        padding: 20px;
        color: white;
        border-radius: 5px;
        margin-bottom: 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-family: 'Arial', sans-serif;
    }
    .alert-card-red {
        background-color: #d6001a;
        color: white;
        padding: 12px;
        border-radius: 4px;
        margin-bottom: 8px;
        border-left: 8px solid #8b0000;
        font-weight: bold;
    }
    .alert-card-amber {
        background-color: #eb8f34;
        color: white;
        padding: 12px;
        border-radius: 4px;
        margin-bottom: 8px;
        border-left: 8px solid #c46210;
        font-weight: bold;
    }
    </style>
    """, unsafe_allow_html=True)

# 2. UPDATED AIRPORT LIST (IATA KEYS)
airports = {
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "ZRH": {"icao": "LSZH", "name": "Zurich", "fleet": "Cityflyer", "rwy": 160, "lat": 47.458, "lon": 8.548},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "SSH": {"icao": "HESH", "name": "Sharm El Sheikh", "fleet": "Euroflyer", "rwy": 40, "lat": 27.977, "lon": 34.394},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# --- TREND ANALYSIS FUNCTION ---
def check_taf_trends(taf_obj):
    warnings = []
    trend_keys = ["TEMPO", "PROB30", "PROB40", "BECMG"]
    for line in taf_obj.data.forecast:
        raw = line.raw
        if any(key in raw for key in trend_keys):
            # Check for low vis or low ceilings in the forecast line
            vis = line.visibility.value if line.visibility else 9999
            ceiling = 9999
            if line.clouds:
                for layer in line.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)
            
            if vis < 1500 or ceiling < 500:
                warnings.append(f"Trend: {raw[:15]}...")
    return warnings

# --- UI HEADER ---
st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

# Sidebar
st.sidebar.image("https://upload.wikimedia.org/wikipedia/en/thumb/d/de/British_Airways_Logo.svg/1200px-British_Airways_Logo.svg.png", use_container_width=True)
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
search_iata = st.sidebar.selectbox("Jump to Airport", ["Select..."] + sorted([k for k, v in airports.items() if v['fleet'] in fleet_filter]))
op_notes = st.sidebar.text_area("Operational Notes", "Normal Ops.")

# Map Theme
map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

map_center = [48.0, 5.0]; zoom = 4
if search_iata != "Select...":
    map_center = [airports[search_iata]["lat"], airports[search_iata]["lon"]]; zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
red_alerts = []; amber_alerts = []

# Process Weather
for iata, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            metar = Metar(info['icao']); metar.update()
            taf = Taf(info['icao']); taf.update()
            
            # Current Conditions (METAR)
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xw = get_xwind(w_dir, w_spd, info['rwy'])
            
            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)

            # Trend Check (TAF)
            trend_warnings = check_taf_trends(taf)

            color = "#008000"
            status = "NORMAL"
            
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a"; status = "BELOW MINIMA"
                red_alerts.append(f"{iata}: CURRENTLY BELOW MINIMA")
                counts[info['fleet']]["red"] += 1
            elif xw > 18 or vis < 1500 or ceiling < 500 or trend_warnings:
                color = "#eb8f34"; status = "CAUTION / TREND"
                msg = f"{iata}: Trend Deterioration" if trend_warnings else f"{iata}: Marginal"
                amber_alerts.append(msg)
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            popup_html = f"""
            <div style="width: 450px; font-family: Arial; padding: 10px;">
                <h3 style="color: #002366; border-bottom: 2px solid #002366;">{info['name']} ({iata})</h3>
                <b>Current:</b> CIG {ceiling}ft | XW {xw}kt | VIS {vis}m<br>
                <b>Trends:</b> {', '.join(trend_warnings) if trend_warnings else 'No significant trends'}<br>
                <div style="margin-top:10px; padding:8px; background:#f0f0f0; font-family:monospace; font-size:11px;">
                    <b>METAR:</b> {metar.raw}<br><br><b>TAF:</b> {taf.raw}
                </div>
            </div>"""
            
            folium.CircleMarker(location=[info['lat'], info['lon']], radius=7 if zoom < 6 else 14, 
                                color=color, fill=True, fill_opacity=0.9,
                                popup=folium.Popup(popup_html, max_width=500)).add_to(m)
        except: continue

# DISPLAY DASHBOARD
c1, c2 = st.columns(2)
with c1: st.metric("Cityflyer Status", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
with c2: st.metric("Euroflyer Status", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")
m1, m2 = st.columns([3.5, 1])
with m1: st_folium(m, width=1100, height=750, key="occ_v_trends")
with m2:
    st.markdown("#### ⚠️ Alerts")
    for r in red_alerts: st.markdown(f'<div class="alert-card-red">{r}</div>', unsafe_allow_html=True)
    for a in amber_alerts: st.markdown(f'<div class="alert-card-amber">{a}</div>', unsafe_allow_html=True)
    st.info(f"Notes: {op_notes}")
