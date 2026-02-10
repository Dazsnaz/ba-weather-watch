import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# Page Config
st.set_page_config(layout="wide", page_title="BA Fleet Weather 2026", page_icon="✈️")

# 1. Full 2026 Airport Database (Runway headings corrected - no leading zeros)
airports = {
    "EGLC": {"name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "EGKK": {"name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "EGSS": {"name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "EGPH": {"name": "Edinburgh", "fleet": "Cityflyer", "rwy": 240, "lat": 55.950, "lon": -3.363},
    "EGAA": {"name": "Belfast City", "fleet": "Cityflyer", "rwy": 220, "lat": 54.618, "lon": -5.872},
    "EIDW": {"name": "Dublin", "fleet": "Cityflyer", "rwy": 280, "lat": 53.421, "lon": -6.270},
    "EHAM": {"name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "EHRD": {"name": "Rotterdam", "fleet": "Cityflyer", "rwy": 240, "lat": 51.957, "lon": 4.440},
    "LSZH": {"name": "Zurich", "fleet": "Cityflyer", "rwy": 160, "lat": 47.458, "lon": 8.548},
    "EDDB": {"name": "Berlin", "fleet": "Euroflyer", "rwy": 250, "lat": 52.362, "lon": 13.501},
    "EDDL": {"name": "Dusseldorf", "fleet": "Cityflyer", "rwy": 230, "lat": 51.289, "lon": 6.767},
    "EDDF": {"name": "Frankfurt", "fleet": "Cityflyer", "rwy": 250, "lat": 50.033, "lon": 8.571},
    "LEMD": {"name": "Madrid", "fleet": "Euroflyer", "rwy": 140, "lat": 40.494, "lon": -3.567},
    "LFBD": {"name": "Bordeaux", "fleet": "Cityflyer", "rwy": 230, "lat": 44.828, "lon": -0.716},
    "LSGG": {"name": "Geneva", "fleet": "Euroflyer", "rwy": 220, "lat": 46.237, "lon": 6.109},
    "LIRQ": {"name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "EGMC": {"name": "Southend", "fleet": "Cityflyer", "rwy": 230, "lat": 51.571, "lon": 0.701},
    "LEIB": {"name": "Ibiza", "fleet": "Euroflyer", "rwy": 60, "lat": 38.873, "lon": 1.373},
    "LEPA": {"name": "Palma", "fleet": "Euroflyer", "rwy": 240, "lat": 39.551, "lon": 2.738},
    "LEAL": {"name": "Alicante", "fleet": "Euroflyer", "rwy": 100, "lat": 38.282, "lon": -0.558},
    "LEMG": {"name": "Malaga", "fleet": "Euroflyer", "rwy": 130, "lat": 36.675, "lon": -4.499},
    "LPFR": {"name": "Faro", "fleet": "Euroflyer", "rwy": 280, "lat": 37.017, "lon": -7.965},
    "LPPR": {"name": "Porto", "fleet": "Euroflyer", "rwy": 350, "lat": 41.242, "lon": -8.678},
    "LPMA": {"name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
    "GCTS": {"name": "Tenerife South", "fleet": "Euroflyer", "rwy": 70, "lat": 28.044, "lon": -16.572},
    "GCRR": {"name": "Lanzarote", "fleet": "Euroflyer", "rwy": 30, "lat": 28.945, "lon": -13.605},
    "GCLP": {"name": "Fuerteventura", "fleet": "Euroflyer", "rwy": 10, "lat": 28.452, "lon": -13.864},
    "HESH": {"name": "Sharm El Sheikh", "fleet": "Euroflyer", "rwy": 40, "lat": 27.977, "lon": 34.394},
    "DAAG": {"name": "Algiers", "fleet": "Euroflyer", "rwy": 270, "lat": 36.691, "lon": 3.215},
    "GMMX": {"name": "Marrakesh", "fleet": "Euroflyer", "rwy": 100, "lat": 31.606, "lon": -8.036},
    "GMAD": {"name": "Agadir", "fleet": "Euroflyer", "rwy": 90, "lat": 30.325, "lon": -9.413},
    "LOWS": {"name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "EGJJ": {"name": "Jersey", "fleet": "Cityflyer", "rwy": 260, "lat": 49.208, "lon": -2.195},
    "LOWI": {"name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "LEZL": {"name": "Seville", "fleet": "Euroflyer", "rwy": 270, "lat": 37.418, "lon": -5.893},
    "EFIV": {"name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "LCLK": {"name": "Larnaca", "fleet": "Euroflyer", "rwy": 220, "lat": 34.875, "lon": 33.625},
    "LCPH": {"name": "Paphos", "fleet": "Euroflyer", "rwy": 290, "lat": 34.718, "lon": 32.486},
    "LOWG": {"name": "Graz", "fleet": "Euroflyer", "rwy": 340, "lat": 46.991, "lon": 15.440}
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# Sidebar: Controls
st.sidebar.title("Fleet Controls")
search_icao = st.sidebar.selectbox("Jump to Airport", ["Select..."] + sorted(list(airports.keys())))
fleet_filter = st.sidebar.multiselect("Fleet Select", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])

# Dashboard State
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
map_center = [48.0, 5.0]
zoom = 4

if search_icao != "Select...":
    map_center = [airports[search_icao]["lat"], airports[search_icao]["lon"]]
    zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles="CartoDB positron")
warnings = []

# --- IMPROVED PROCESS WEATHER LOOP ---
for icao, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            # Separate METAR and TAF fetches so one failure doesn't kill both
            metar = Metar(icao)
            metar.update()
            
            # 1. Weather Data
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xw = get_xwind(w_dir, w_spd, info['rwy'])

            # 2. Ceiling Logic
            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.altitude is not None:
                        h = layer.altitude * 100
                        if h < ceiling: ceiling = h

            # 3. Traffic Light Decisions
            color = "green"
            status_label = "Normal"
            
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "red"
                status_label = "🔴 BELOW MINIMA"
                warnings.append(f"🔴 {icao}: {info['name']} (Below Limits)")
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "orange"
                status_label = f"🟠 CAUTION: Marginal (CIG: {ceiling}ft)"
                warnings.append(f"🟠 {icao}: {info['name']} (Caution)")
            
            counts[info['fleet']][color] += 1

            # Try to get TAF, but don't fail if it's unavailable
            try:
                taf = Taf(icao)
                taf.update()
                taf_raw = taf.raw
            except:
                taf_raw = "TAF temporarily unavailable"

            # Marker Popup
            popup_html = f"""
            <div style='width:250px'>
                <b>{info['name']} ({icao})</b><br>
                <b>Status:</b> {status_label}<br>
                <b>Ceiling:</b> {ceiling if ceiling < 9999 else 'Unlimited'}ft | <b>X-Wind:</b> {xw}kt<br>
                <hr>
                <b>METAR:</b> {metar.raw}<br><br>
                <b>TAF:</b> {taf_raw}
            </div>
            """
            folium.CircleMarker(
                location=[info['lat'], info['lon']],
                radius=12, color=color, fill=True, fill_opacity=0.8,
                popup=folium.Popup(popup_html, max_width=300)
            ).add_to(m)
            
        except Exception as e:
            # If an airport fails, show a warning in the sidebar instead of just disappearing
            st.sidebar.error(f"Could not load {icao}: {e}")
            continue

# DISPLAY UI
st.title("✈️ BA Weather Dashboard")
st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')} UTC")

# Fleet Metrics
cols = st.columns(2)
for i, f in enumerate(["Cityflyer", "Euroflyer"]):
    with cols[i]:
        st.subheader(f"{f} Status")
        m1, m2, m3 = st.columns(3)
        m1.metric("Green", counts[f]["green"])
        m2.metric("Amber", counts[f]["orange"])
        m3.metric("Red", counts[f]["red"])

# Map & Warnings
c1, c2 = st.columns([3, 1])
with c1:
    st_folium(m, width="100%", height=600, key="main_map")
with c2:
    st.subheader("⚠️ Critical Alerts")
    if not warnings:
        st.success("All airports clear.")
    else:
        for w in warnings:
            if "🔴" in w:
                st.error(w)
            else:
                st.warning(w)
