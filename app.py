import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math

# Page Config
st.set_page_config(layout="wide", page_title="BA Weather Watch 2026")
st.title("✈️ BA Cityflyer & Euroflyer Weather Watch")

# 1. Expanded 2026 Airport Database
airports = {
    "EGLC": {"name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "EGKK": {"name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "EGST": {"name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "EGLF": {"name": "Glasgow", "fleet": "Cityflyer", "rwy": 230, "lat": 55.871, "lon": -4.433},
    "LFTH": {"name": "Toulon (St Tropez)", "fleet": "Cityflyer", "rwy": 310, "lat": 43.097, "lon": 6.146},
    "LIEO": {"name": "Olbia (Sardinia)", "fleet": "Cityflyer", "rwy": 50, "lat": 40.898, "lon": 9.517},
    "LESO": {"name": "San Sebastián", "fleet": "Cityflyer", "rwy": 220, "lat": 43.356, "lon": -1.791},
    "LIRQ": {"name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "LOWS": {"name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004}
}

def calculate_xwind(wind_dir, wind_spd, rwy_hdg):
    if not wind_dir or not wind_spd: return 0
    angle = abs(wind_dir - rwy_hdg)
    return wind_spd * math.sin(math.radians(angle))

# --- SIDEBAR SEARCH & FILTERS ---
st.sidebar.header("Search & Filters")
search_icao = st.sidebar.selectbox("Jump to Airport", ["Select..."] + list(airports.keys()))

fleet_selection = st.sidebar.multiselect(
    "Filter Fleet", 
    ["Cityflyer", "Euroflyer"], 
    default=["Cityflyer", "Euroflyer"]
)

# Initial Map Center
start_lat, start_lon, start_zoom = 48.0, 5.0, 5
if search_icao != "Select...":
    start_lat = airports[search_icao]["lat"]
    start_lon = airports[search_icao]["lon"]
    start_zoom = 10

m = folium.Map(location=[start_lat, start_lon], zoom_start=start_zoom, tiles="CartoDB positron")
warnings = []

# --- PROCESS WEATHER ---
for icao, info in airports.items():
    if info['fleet'] in fleet_selection:
        try:
            metar = Metar(icao)
            metar.update()
            taf = Taf(icao)
            taf.update()
            
            # Weather Data
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xwind = calculate_xwind(w_dir, w_spd, info['rwy'])
            
            # Logic Gates
            color = "green"
            if xwind > 25 or vis < 800:
                color = "red"
                warnings.append(f"🔴 {icao}: {info['name']} - BELOW LIMITS")
            elif xwind > 18 or vis < 1500:
                color = "orange"
                warnings.append(f"🟠 {icao}: {info['name']} - CAUTION")

            # Add Marker
            popup_html = f"""
            <div style='width:250px'>
                <b>{info['name']} ({icao})</b><br>
                <b>X-Wind:</b> {round(xwind,1)}kts<br>
                <hr>
                <b>METAR:</b> {metar.raw}<br><br>
                <b>TAF:</b> {taf.raw}
            </div>
            """
            folium.CircleMarker(
                location=[info['lat'], info['lon']],
                radius=12, color=color, fill=True, fill_opacity=0.7,
                popup=folium.Popup(popup_html, max_width=300)
            ).add_to(m)
        except:
            continue

# Display Layout
col1, col2 = st.columns([3, 1])
with col1:
    st_folium(m, width="100%", height=600, key="main_map")
with col2:
    st.subheader("⚠️ Ops Alerts")
    if not warnings:
        st.success("All airports clear.")
    for w in warnings:
        st.write(w)
