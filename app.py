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
    .alert-card-red { background-color: #d6001a; color: white; padding: 12px; border-radius: 4px; margin-bottom: 8px; border-left: 8px solid #8b0000; font-weight: bold; }
    .alert-card-amber { background-color: #eb8f34; color: white; padding: 12px; border-radius: 4px; margin-bottom: 8px; border-left: 8px solid #c46210; font-weight: bold; }
    .alert-card-arctic { background-color: #005a9c; color: white; padding: 12px; border-radius: 4px; margin-bottom: 8px; border-left: 8px solid #add8e6; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

# 2. COMPLETE 2026 FLEET DATABASE
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

# Data Containers
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
alerts = []
map_markers = []

# Process Data
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

            color = "#008000"
            alert_type = None
            msg = ""

            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a"; alert_type = "red"; msg = "BELOW MINIMA"
                counts[info['fleet']]["red"] += 1
            elif iata == "IVL" and temp <= -25:
                color = "#005a9c"; alert_type = "arctic"; msg = f"EXTREME COLD ({temp}°C)"
                counts[info['fleet']]["orange"] += 1
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "#eb8f34"; alert_type = "amber"; msg = "MARGINAL / LVO"
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            if alert_type:
                alerts.append({"iata": iata, "type": alert_type, "msg": f"{iata}: {msg}"})

            map_markers.append({
                "iata": iata, "lat": info['lat'], "lon": info['lon'], 
                "color": color, "name": info['name'], "temp": temp, 
                "xw": xw, "ceiling": ceiling, "raw": metar.raw
            })
        except: continue

# --- ALERT INVESTIGATOR ---
st.sidebar.markdown("---")
investigate = st.sidebar.selectbox("🚨 Alert Investigator", ["Select Alert..."] + [a['msg'] for a in alerts])

map_center = [48.0, 5.0]; zoom = 4
selected_iata = None

if investigate != "Select Alert...":
    selected_iata = investigate.split(":")[0]
    target = airports[selected_iata]
    map_center = [target["lat"], target["lon"]]; zoom = 10

m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)

for mkr in map_markers:
    popup_html = f"""
    <div style="width: 350px; font-family: Arial; background: white; padding: 10px; border-radius: 5px;">
        <h4 style="color: #002366; border-bottom: 2px solid #002366; margin-top: 0;">{mkr['name']} ({mkr['iata']})</h4>
        <b>Temp:</b> {mkr['temp']}°C | <b>X-Wind:</b> {mkr['xw']}kt | <b>CIG:</b> {mkr['ceiling']}ft<br>
        <div style="margin-top:10px; padding:8px; background:#f0f0f0; font-family:monospace; font-size:11px; border-radius: 3px;">{mkr['raw']}</div>
    </div>"""
    
    folium.CircleMarker(
        location=[mkr['lat'], mkr['lon']],
        radius=12 if mkr['iata'] == selected_iata else (6 if zoom < 6 else 12),
        color=mkr['color'], fill=True, fill_opacity=0.9,
        popup=folium.Popup(popup_html, max_width=400, show=(mkr['iata'] == selected_iata))
    ).add_to(m)

# UI RENDER
c1, c2 = st.columns(2)
with c1: st.metric("Cityflyer Fleet", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
with c2: st.metric("Euroflyer Fleet", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")
m_col, a_col = st.columns([3.5, 1])
with m_col: st_folium(m, width=1100, height=750, key="occ_v_interactive")
with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    for a in alerts:
        st.markdown(f'<div class="alert-card-{a["type"]}">{a["msg"]}</div>', unsafe_allow_html=True)
    st.info("Notes: No active slot delays.")
