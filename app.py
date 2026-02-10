import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG & AUTO-REFRESH
st.set_page_config(layout="wide", page_title="BA Fleet Weather 2026", page_icon="✈️")
st.markdown('<meta http-equiv="refresh" content="1800">', unsafe_allow_html=True)

# 2. DEFINED FLEET DATABASE
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
    "LIN": {"icao": "LIML", "name": "Milan Linate", "fleet": "Cityflyer", "rwy": 360, "lat": 45.445, "lon": 9.277},
    "CMF": {"icao": "LFLB", "name": "Chambery", "fleet": "Cityflyer", "rwy": 180, "lat": 45.638, "lon": 5.880},
    "ZRH": {"icao": "LSZH", "name": "Zurich", "fleet": "Cityflyer", "rwy": 160, "lat": 47.458, "lon": 8.548},
    "FRA": {"icao": "EDDF", "name": "Frankfurt", "fleet": "Cityflyer", "rwy": 250, "lat": 50.033, "lon": 8.571},
    "BER": {"icao": "EDDB", "name": "Berlin", "fleet": "Cityflyer", "rwy": 250, "lat": 52.362, "lon": 13.501},
    "MAD": {"icao": "LEMD", "name": "Madrid", "fleet": "Cityflyer", "rwy": 140, "lat": 40.494, "lon": -3.567},

    # --- EUROFLYER ---
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "JER": {"icao": "EGJJ", "name": "Jersey", "fleet": "Euroflyer", "rwy": 260, "lat": 49.208, "lon": -2.195},
    "OPO": {"icao": "LPPR", "name": "Porto", "fleet": "Euroflyer", "rwy": 350, "lat": 41.242, "lon": -8.678},
    "FAO": {"icao": "LPFR", "name": "Faro", "fleet": "Euroflyer", "rwy": 280, "lat": 37.017, "lon": -7.965},
    "LYS": {"icao": "LFLL", "name": "Lyon", "fleet": "Euroflyer", "rwy": 350, "lat": 45.726, "lon": 5.090},
    "GVA": {"icao": "LSGG", "name": "Geneva", "fleet": "Euroflyer", "rwy": 220, "lat": 46.237, "lon": 6.109},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "BOD": {"icao": "LFBD", "name": "Bordeaux", "fleet": "Euroflyer", "rwy": 230, "lat": 44.828, "lon": -0.716},
    "GNB": {"icao": "LFLS", "name": "Grenoble", "fleet": "Euroflyer", "rwy": 90, "lat": 45.363, "lon": 5.330},
    "NCE": {"icao": "LFMN", "name": "Nice", "fleet": "Euroflyer", "rwy": 40, "lat": 43.665, "lon": 7.215},
    "TRN": {"icao": "LIMF", "name": "Turin", "fleet": "Euroflyer", "rwy": 360, "lat": 45.202, "lon": 7.649},
    "VRN": {"icao": "LIPX", "name": "Verona", "fleet": "Euroflyer", "rwy": 40, "lat": 45.396, "lon": 10.888},
    "ALC": {"icao": "LEAL", "name": "Alicante", "fleet": "Euroflyer", "rwy": 100, "lat": 38.282, "lon": -0.558},
    "AGP": {"icao": "LEMG", "name": "Malaga", "fleet": "Euroflyer", "rwy": 130, "lat": 36.675, "lon": -4.499},
    "SVQ": {"icao": "LEZL", "name": "Seville", "fleet": "Euroflyer", "rwy": 270, "lat": 37.418, "lon": -5.893},
    "RAK": {"icao": "GMMX", "name": "Marrakesh", "fleet": "Euroflyer", "rwy": 100, "lat": 31.606, "lon": -8.036},
    "ALG": {"icao": "DAAG", "name": "Algiers", "fleet": "Euroflyer", "rwy": 270, "lat": 36.691, "lon": 3.215},
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
    "IBZ": {"icao": "LEIB", "name": "Ibiza", "fleet": "Euroflyer", "rwy": 60, "lat": 38.873, "lon": 1.373},
    "PMI": {"icao": "LEPA", "name": "Palma", "fleet": "Euroflyer", "rwy": 240, "lat": 39.551, "lon": 2.738},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# Sidebar
st.sidebar.title("Fleet Controls")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])

available_iatas = sorted([k for k, v in airports.items() if v['fleet'] in fleet_filter])
search_iata = st.sidebar.selectbox("Jump to Airport (IATA)", ["Select..."] + available_iatas)

map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

map_center = [48.0, 5.0]
zoom = 4
if search_iata != "Select...":
    map_center = [airports[search_iata]["lat"], airports[search_iata]["lon"]]
    zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
warnings = []

# Process Weather
for iata, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            metar = Metar(info['icao'])
            metar.update()
            
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

            color = "green"
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "red"
                warnings.append(f"🔴 {iata}: {info['name']} (Below Limits)")
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "orange"
                warnings.append(f"🟠 {iata}: {info['name']} (LVO/Caution)")
            
            counts[info['fleet']][color] += 1

            try:
                taf = Taf(info['icao']); taf.update()
                taf_txt = taf.raw
            except: taf_txt = "TAF Unavailable"

            popup_content = f"<b>{info['name']} ({iata})</b><br>CIG: {ceiling}ft | XW: {xw}kt<hr>{metar.raw}<br><br>{taf_txt}"
            folium.CircleMarker(
                location=[info['lat'], info['lon']],
                radius=14, color=color, fill=True, fill_opacity=0.8,
                popup=folium.Popup(popup_content, max_width=300)
            ).add_to(m)
        except: continue

# UI
st.title("✈️ BA Weather Dashboard")
st.caption(f"Last Refresh: {datetime.now().strftime('%H:%M:%S')} UTC (Auto-refresh every 30m)")

cols = st.columns(len(fleet_filter) if fleet_filter else 1)
for i, f in enumerate(fleet_filter):
    with cols[i]:
        st.subheader(f"{f} Status")
        m1, m2, m3 = st.columns(3)
        m1.metric("Green", counts[f]["green"])
        m2.metric("Amber", counts[f]["orange"])
        m3.metric("Red", counts[f]["red"])

c1, c2 = st.columns([4, 1])
with c1:
    st_folium(m, width=1200, height=800, key="main_map_v5")
with c2:
    st.subheader("⚠️ Critical Alerts")
    if not warnings:
        st.success("All Clear")
    else:
        for w in warnings:
            if "🔴" in w: st.error(w)
            else: st.warning(w)
