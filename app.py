import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG & BRANDING
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

# Custom CSS for BA OCC Branding and Landscape Popups
st.markdown("""
    <style>
    .main { background-color: #f5f5f5; }
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
    /* Fixed Logo Styling */
    [data-testid="stSidebarNav"] {
        padding-top: 20px;
    }
    </style>
    """, unsafe_allow_html=True)

# Auto-refresh every 30 mins
st.markdown('<meta http-equiv="refresh" content="1800">', unsafe_allow_html=True)

# 2. DATABASE (IATA Keys)
airports = {
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "RTM": {"icao": "EHRD", "name": "Rotterdam", "fleet": "Cityflyer", "rwy": 240, "lat": 51.957, "lon": 4.440},
    "DUB": {"icao": "EIDW", "name": "Dublin", "fleet": "Cityflyer", "rwy": 280, "lat": 53.421, "lon": -6.270},
    "GLA": {"icao": "EGPF", "name": "Glasgow", "fleet": "Cityflyer", "rwy": 230, "lat": 55.871, "lon": -4.433},
    "EDI": {"icao": "EGPH", "name": "Edinburgh", "fleet": "Cityflyer", "rwy": 240, "lat": 55.950, "lon": -3.363},
    "BHD": {"icao": "EGAC", "name": "Belfast City", "fleet": "Cityflyer", "rwy": 220, "lat": 54.618, "lon": -5.872},
    "STN": {"icao": "EGSS", "name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "LIN": {"icao": "LIML", "name": "Milan Linate", "fleet": "Cityflyer", "rwy": 360, "lat": 45.445, "lon": 9.277},
    "CMF": {"icao": "LFLB", "name": "Chambery", "fleet": "Cityflyer", "rwy": 180, "lat": 45.638, "lon": 5.880},
    "ZRH": {"icao": "LSZH", "name": "Zurich", "fleet": "Cityflyer", "rwy": 160, "lat": 47.458, "lon": 8.548},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "SSH": {"icao": "HESH", "name": "Sharm El Sheikh", "fleet": "Euroflyer", "rwy": 40, "lat": 27.977, "lon": 34.394},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# --- HEADER ---
st.markdown(f"""
    <div class="ba-header">
        <div style="font-size: 26px; font-weight: bold; letter-spacing: 1px;">OCC WEATHER DASHBOARD</div>
        <div style="text-align: right; font-weight: bold;">
            {datetime.now().strftime('%d %b %Y | %H:%M')} UTC
        </div>
    </div>
    """, unsafe_allow_html=True)

# Sidebar
# Using a reliable official BA logo source
st.sidebar.image("https://brand.britishairways.com/content/dam/paris/ba-logo.png", use_container_width=True)
st.sidebar.markdown("### Fleet Controls")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
search_iata = st.sidebar.selectbox("Jump to Airport (IATA)", ["Select..."] + sorted([k for k, v in airports.items() if v['fleet'] in fleet_filter]))
op_notes = st.sidebar.text_area("Daily Operational Notes", "No significant delays.")

map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

# Map Logic
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
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xw = get_xwind(w_dir, w_spd, info['rwy'])

            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base is not None:
                        h = layer.base * 100
                        if h < ceiling: ceiling = h

            color = "#008000"
            status = "NORMAL"
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a"; status = "BELOW MINIMA"
                red_alerts.append(f"{iata}: {info['name']} (Limits)")
                counts[info['fleet']]["red"] += 1
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "#eb8f34"; status = "CAUTION / LVO"
                amber_alerts.append(f"{iata}: {info['name']} (LVO)")
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            # Radius adjusts with zoom to prevent clutter
            radius = 7 if zoom < 6 else 14
            
            # --- LANDSCAPE POPUP HTML ---
            popup_html = f"""
            <div style="width: 400px; font-family: Arial, sans-serif; padding: 10px; background-color: white;">
                <h3 style="margin: 0; color: #002366; border-bottom: 2px solid #002366;">{info['name']} ({iata})</h3>
                <table style="width: 100%; margin-top: 10px; border-collapse: collapse;">
                    <tr>
                        <td style="font-weight: bold; width: 30%;">Status:</td>
                        <td style="color: {color}; font-weight: bold;">{status}</td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Conditions:</td>
                        <td>CIG: {ceiling if ceiling < 9999 else 'SKC'}ft | XW: {xw}kt</td>
                    </tr>
                </table>
                <div style="margin-top: 10px; background-color: #f0f0f0; padding: 8px; border-radius: 4px; font-size: 12px; font-family: monospace;">
                    <b>METAR:</b><br>{metar.raw}
                </div>
            </div>
            """
            
            folium.CircleMarker(
                location=[info['lat'], info['lon']],
                radius=radius, color=color, fill=True, fill_opacity=0.9,
                popup=folium.Popup(popup_html, max_width=450)
            ).add_to(m)
        except: continue

# DISPLAY MAIN
col_s1, col_s2 = st.columns(2)
with col_s1:
    st.markdown("### Cityflyer Status")
    c1, c2, c3 = st.columns(3); c1.metric("Green", counts["Cityflyer"]["green"]); c2.metric("Amber", counts["Cityflyer"]["orange"]); c3.metric("Red", counts["Cityflyer"]["red"])
with col_s2:
    st.markdown("### Euroflyer Status")
    c4, c5, c6 = st.columns(3); c4.metric("Green", counts["Euroflyer"]["green"]); c5.metric("Amber", counts["Euroflyer"]["orange"]); c6.metric("Red", counts["Euroflyer"]["red"])

st.markdown("---")

c_map, c_alerts = st.columns([3.5, 1])
with c_map:
    st_folium(m, width=1100, height=750, key="occ_v1")
with c_alerts:
    st.markdown("#### ⚠️ Critical Alerts")
    for r in red_alerts: st.markdown(f'<div class="alert-card-red">{r}</div>', unsafe_allow_html=True)
    for a in amber_alerts: st.markdown(f'<div class="alert-card-amber">{a}</div>', unsafe_allow_html=True)
    if not red_alerts and not amber_alerts: st.success("Operations Normal")
    st.markdown("---")
    st.markdown("#### 📝 Operational Notes")
    st.info(op_notes)    "LCA": {"icao": "LCLK", "name": "Larnaca", "fleet": "Euroflyer", "rwy": 220, "lat": 34.875, "lon": 33.625},
    "FUE": {"icao": "GCLP", "name": "Fuerteventura", "fleet": "Euroflyer", "rwy": 10, "lat": 28.452, "lon": -13.864},
    "TFS": {"icao": "GCTS", "name": "Tenerife South", "fleet": "Euroflyer", "rwy": 70, "lat": 28.044, "lon": -16.572},
    "ACE": {"icao": "GCRR", "name": "Lanzarote", "fleet": "Euroflyer", "rwy": 30, "lat": 28.945, "lon": -13.605},
    "LPA": {"icao": "GCLP", "name": "Gran Canaria", "fleet": "Euroflyer", "rwy": 30, "lat": 27.931, "lon": -15.386},
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "MLA": {"icao": "LMML", "name": "Malta", "fleet": "Euroflyer", "rwy": 310, "lat": 35.857, "lon": 14.477},
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
    "IBZ": {"icao": "LEIB", "name": "Ibiza", "fleet": "Euroflyer", "rwy": 60, "lat": 38.873, "lon": 1.373},
    "PMI": {"icao": "LEPA", "name": "Palma", "fleet": "Euroflyer", "rwy": 240, "lat": 39.551, "lon": 2.738},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# --- HEADER SECTION ---
st.markdown(f"""
    <div class="ba-header">
        <div style="font-size: 28px; font-weight: bold;">British Airways | OCC Weather Dashboard</div>
        <div style="text-align: right;">
            <span style="font-size: 14px;">{datetime.now().strftime('%d %b - %H:%M')} UTC</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

# Sidebar
st.sidebar.image("https://www.britishairways.com/content/dam/paris/ba-logo.png", width=200)
st.sidebar.title("Fleet Controls")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
search_iata = st.sidebar.selectbox("Jump to Airport (IATA)", ["Select..."] + sorted([k for k, v in airports.items() if v['fleet'] in fleet_filter]))
st.sidebar.markdown("---")
op_notes = st.sidebar.text_area("Daily Operational Notes", "No slot delays reported.")

# Map Theme
map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

# Logic for Map
map_center = [48.0, 5.0]; zoom = 4
if search_iata != "Select...":
    map_center = [airports[search_iata]["lat"], airports[search_iata]["lon"]]; zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style, control_scale=True)
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
red_alerts = []; amber_alerts = []

# Process Weather
for iata, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            metar = Metar(info['icao']); metar.update()
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            xw = get_xwind(w_dir, w_spd, info['rwy'])

            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base is not None:
                        h = layer.base * 100
                        if h < ceiling: ceiling = h

            color = "#008000" # Green
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a" # BA Red
                red_alerts.append(f"{iata}: {info['name']} - Below Limits")
                counts[info['fleet']]["red"] += 1
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "#eb8f34" # Amber
                amber_alerts.append(f"{iata}: {info['name']} - LVO Caution")
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            # Zoom-adaptive radius logic (larger when zoomed in)
            radius_size = 8 if zoom < 6 else 15

            folium.CircleMarker(
                location=[info['lat'], info['lon']],
                radius=radius_size, color=color, fill=True, fill_opacity=0.9,
                popup=f"<b>{iata}</b><br>CIG: {ceiling}ft<br>XW: {xw}kt<br>{metar.raw}"
            ).add_to(m)
        except: continue

# DISPLAY MAIN DASHBOARD
col_stats1, col_stats2 = st.columns(2)
with col_stats1:
    st.subheader("Cityflyer Fleet Status")
    c1, c2, c3 = st.columns(3)
    c1.metric("Green", counts["Cityflyer"]["green"])
    c2.metric("Amber", counts["Cityflyer"]["orange"])
    c3.metric("Red", counts["Cityflyer"]["red"])
with col_stats2:
    st.subheader("Euroflyer Fleet Status")
    c4, c5, c6 = st.columns(3)
    c4.metric("Green", counts["Euroflyer"]["green"])
    c5.metric("Amber", counts["Euroflyer"]["orange"])
    c6.metric("Red", counts["Euroflyer"]["red"])

st.markdown("---")

col_map, col_alerts = st.columns([3.5, 1])

with col_map:
    st_folium(m, width=1100, height=750, key="occ_map")

with col_alerts:
    st.markdown("#### ⚠️ Critical Alerts")
    for a in red_alerts:
        st.markdown(f'<div class="alert-card-red">{a}</div>', unsafe_allow_html=True)
    for a in amber_alerts:
        st.markdown(f'<div class="alert-card-amber">{a}</div>', unsafe_allow_html=True)
    if not red_alerts and not amber_alerts:
        st.success("No active weather alerts.")
    
    st.markdown("---")
    st.markdown("#### 📝 Operational Notes")
    st.info(op_notes)
