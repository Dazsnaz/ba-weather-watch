import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math

st.set_page_config(layout="wide", page_title="BA Fleet Weather")
st.title("✈️ BA Cityflyer & Euroflyer Weather Watch")

# 1. Airport Database (Add as many as you need here)
airports = {
    "EGLC": {"name": "London City", "fleet": "Cityflyer", "rwy": 270},
    "EGKK": {"name": "Gatwick", "fleet": "Euroflyer", "rwy": 260},
    "EHAM": {"name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180},
    "LIRQ": {"name": "Florence", "fleet": "Cityflyer", "rwy": 050},
}

def calculate_xwind(wind_dir, wind_spd, rwy_hdg):
    if not wind_dir or not wind_spd: return 0
    angle = abs(wind_dir - rwy_hdg)
    return wind_spd * math.sin(math.radians(angle))

# Sidebar Filters
fleet_selection = st.sidebar.multiselect("Filter Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])

m = folium.Map(location=[50.0, 0.0], zoom_start=5, tiles="CartoDB positron")
warnings = []

# Process Weather
for icao, info in airports.items():
    if info['fleet'] in fleet_selection:
        try:
            metar = Metar(icao)
            metar.update()
            taf = Taf(icao)
            taf.update()
            
            # Values
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xwind = calculate_xwind(w_dir, w_spd, info['rwy'])
            
            # Logic
            color = "green"
            if xwind > 25 or vis < 800:
                color = "red"
                warnings.append(f"🔴 {icao}: LIMIT EXCEEDED")
            elif xwind > 18 or vis < 1500:
                color = "orange"
                warnings.append(f"🟠 {icao}: CAUTION")

            folium.CircleMarker(
                location=[metar.station.latitude, metar.station.longitude],
                radius=12, color=color, fill=True,
                tooltip=f"{info['name']} ({icao}) - Click for TAF",
                popup=folium.Popup(f"<b>METAR:</b> {metar.raw}<br><br><b>TAF:</b> {taf.raw}", max_width=300)
            ).add_to(m)
        except: continue

# Display Layout
col1, col2 = st.columns([3, 1])
with col1:
    st_folium(m, width=900, height=600)
with col2:
    st.subheader("⚠️ Alerts")
    for w in warnings: st.write(w)
