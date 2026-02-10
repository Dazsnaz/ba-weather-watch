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
    [data-testid="stSidebar"] .stTextInput input { color: #002366 !important; background-color: white !important; font-weight: bold; }
    [data-testid="stSidebar"] label p { color: white !important; font-weight: bold; }
    [data-testid="stSidebar"] button { background-color: #005a9c !important; color: white !important; border: 1px solid white !important; }
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. MASTER FLEET DATABASE (Verified Coordinates)
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer"},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer"},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer"},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer"},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer"},
    # ... rest of your airports here
}

# 4. DATA PROCESSING
@st.cache_data(ttl=900)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            
            # Extract Cloud Base
            c = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        c = min(c, layer.base * 100)
            
            results[iata] = {
                "vis": m.data.visibility.value if m.data.visibility else 9999,
                "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "ceiling": c, "raw_metar": m.raw, "raw_taf": t.raw,
                "status": "online"
            }
        except:
            # Ghost Marker Data
            results[iata] = {"status": "offline", "raw_metar": "DATA UNAVAILABLE", "raw_taf": "DATA UNAVAILABLE"}
    return results

weather_data = get_fleet_weather(base_airports)

# 5. UI RENDER
st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

m = folium.Map(location=[49.5, -1.0], zoom_start=6, tiles="CartoDB dark_matter")

for iata, info in base_airports.items():
    data = weather_data.get(iata, {"status": "offline"})
    
    # Determine Color
    if data['status'] == "offline":
        color = "#808080" # Grey for missing data
    else:
        # Standard Alert Logic
        color = "#008000"
        if data.get('vis', 9999) < 800 or data.get('ceiling', 9999) < 200:
            color = "#d6001a"
    
    popup_html = f"<b>{iata}</b><br>METAR: {data['raw_metar']}"
    folium.CircleMarker(
        location=[info['lat'], info['lon']], 
        radius=8, color=color, fill=True, 
        popup=folium.Popup(popup_html, max_width=300)
    ).add_to(m)

st_folium(m, width=1400, height=500)
