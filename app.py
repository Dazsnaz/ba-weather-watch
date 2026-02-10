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

# 2. DATABASE (IATA Keys)
airports = {
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
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
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "JER": {"icao": "EGJJ", "name": "Jersey", "fleet": "Euroflyer", "rwy": 260, "lat": 49.208, "lon": -2.195},
    "OPO": {"icao": "LPPR", "name": "Porto", "fleet": "Euroflyer", "rwy": 350, "lat": 41.242, "lon": -8.678},
    "FAO": {"icao": "LPFR", "name": "Faro", "fleet": "Euroflyer", "rwy": 280, "lat": 37.017, "lon": -7.965},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "SSH": {"icao": "HESH", "name": "Sharm El Sheikh", "fleet": "Euroflyer", "rwy": 40, "lat": 27.977, "lon": 34.394},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

def check_taf_trends(taf_obj):
    warnings = []
    trend_keys = ["TEMPO", "PROB30", "PROB40", "BECMG"]
    for line in taf_obj.data.forecast:
        raw = line.raw
        if any(key in raw for key in trend_keys):
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

map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])
tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"

m = folium.Map(location=[48.0, 5.0], zoom_start=4, tiles=tile_style)
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
alerts = []

# Process Weather
for iata, info in airports.items():
    if info['fleet'] in fleet_filter:
        try:
            metar = Metar(info['icao']); metar.update()
            taf = Taf(info['icao']); taf.update()
            
            # 1. Extraction
            temp = metar.data.temperature.value if metar.data.temperature else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            xw = get_xwind(w_dir, w_spd, info['rwy'])
            raw_metar = metar.raw
            
            # 2. Ceiling
            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)

            # 3. Trends & De-Ice Logic
            trend_warnings = check_taf_trends(taf)
            deice_codes = ["SN", "FZ", "IC", "PL", "SG", "GR"]
            is_deice = any(code in raw_metar for code in deice_codes)

            # --- COLOR LOGIC ---
            color = "#008000"
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a"
                alerts.append({"type": "red", "msg": f"{iata}: BELOW MINIMA"})
                counts[info['fleet']]["red"] += 1
            elif iata == "IVL" and temp <= -25:
                color = "#005a9c"
                alerts.append({"type": "arctic", "msg": f"❄️ {iata}: COLD ALERT ({temp}°C)"})
                counts[info['fleet']]["orange"] += 1
            elif is_deice:
                color = "#eb8f34"
                alerts.append({"type": "amber", "msg": f"🧊 {iata}: DE-ICE REQ ({raw_metar[:15]})"})
                counts[info['fleet']]["orange"] += 1
            elif xw > 18 or vis < 1500 or ceiling < 500 or trend_warnings:
                color = "#eb8f34"
                alerts.append({"type": "amber", "msg": f"{iata}: MARGINAL / TREND"})
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            popup_html = f"""
            <div style="width: 400px; font-family: Arial; padding: 10px;">
                <h3 style="color: #002366; border-bottom: 2px solid #002366;">{info['name']} ({iata})</h3>
                <b>Conditions:</b> {temp}°C | XW {xw}kt | CIG {ceiling}ft<br>
                <div style="margin-top:10px; padding:8px; background:#f0f0f0; font-family:monospace; font-size:11px;">
                    {raw_metar}<br><br>{taf.raw}
                </div>
            </div>"""
            
            folium.CircleMarker(location=[info['lat'], info['lon']], radius=8, 
                                color=color, fill=True, fill_opacity=0.9,
                                popup=folium.Popup(popup_html, max_width=450)).add_to(m)
        except: continue

# DISPLAY UI
m1, m2 = st.columns([3.5, 1])
with m1: st_folium(m, width=1100, height=750, key="occ_v_deice")
with m2:
    st.markdown("#### ⚠️ Operational Alerts")
    for a in alerts:
        cls = f"alert-card-{a['type']}"
        st.markdown(f'<div class="{cls}">{a["msg"]}</div>', unsafe_allow_html=True)
    st.info(f"Notes: {op_notes}")
