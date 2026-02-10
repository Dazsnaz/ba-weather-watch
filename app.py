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

# 2. DEFINED FLEET DATABASE
airports = {
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    # ... (Include all other airports as defined previously)
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

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
            
            # Extract Temp for Arctic Logic
            temp = metar.data.temperature.value if metar.data.temperature else 0
            vis = metar.data.visibility.value if metar.data.visibility else 9999
            w_dir = metar.data.wind_direction.value if metar.data.wind_direction else 0
            w_spd = metar.data.wind_speed.value if metar.data.wind_speed else 0
            xw = get_xwind(w_dir, w_spd, info['rwy'])
            
            # Ceiling Logic
            ceiling = 9999
            if metar.data.clouds:
                for layer in metar.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)

            # --- COLOR LOGIC ---
            color = "#008000"
            status_text = "NORMAL"
            
            # Red Alert (Safety Minima)
            if xw > 25 or vis < 800 or ceiling < 200:
                color = "#d6001a"
                alerts.append({"type": "red", "msg": f"{iata}: BELOW MINIMA"})
                counts[info['fleet']]["red"] += 1
            # Arctic Alert (Temp <= -25C for IVL)
            elif iata == "IVL" and temp <= -25:
                color = "#005a9c" # Arctic Blue
                alerts.append({"type": "arctic", "msg": f"❄️ {iata}: EXTREME COLD ({temp}°C)"})
                counts[info['fleet']]["orange"] += 1
            # Amber Alert (Caution)
            elif xw > 18 or vis < 1500 or ceiling < 500:
                color = "#eb8f34"
                alerts.append({"type": "amber", "msg": f"{iata}: MARGINAL/LVO"})
                counts[info['fleet']]["orange"] += 1
            else:
                counts[info['fleet']]["green"] += 1

            popup_html = f"""
            <div style="width: 400px; font-family: Arial; padding: 10px;">
                <h3 style="color: #002366; border-bottom: 2px solid #002366;">{info['name']} ({iata})</h3>
                <b>Temp:</b> {temp}°C | <b>X-Wind:</b> {xw}kt | <b>CIG:</b> {ceiling}ft<br>
                <div style="margin-top:10px; padding:8px; background:#f0f0f0; font-family:monospace; font-size:11px;">
                    {metar.raw}
                </div>
            </div>"""
            
            folium.CircleMarker(location=[info['lat'], info['lon']], radius=8, 
                                color=color, fill=True, fill_opacity=0.9,
                                popup=folium.Popup(popup_html, max_width=450)).add_to(m)
        except: continue

# DISPLAY UI
st.title("OCC Weather Dashboard")
m1, m2 = st.columns([3.5, 1])
with m1: st_folium(m, width=1100, height=750, key="occ_v_arctic")
with m2:
    st.markdown("#### ⚠️ Operational Alerts")
    for a in alerts:
        cls = f"alert-card-{a['type']}"
        st.markdown(f'<div class="{cls}">{a["msg"]}</div>', unsafe_allow_html=True)
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
