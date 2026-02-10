import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime, timedelta

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

# 2. CUSTOM OCC STYLING
st.markdown("""
    <style>
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; font-family: 'Arial', sans-serif; }
    [data-testid="stSidebar"] { background-color: #002366 !important; color: white !important; }
    span[data-baseweb="tag"] { background-color: #005a9c !important; color: white !important; }
    div[data-baseweb="select"] > div { background-color: #005a9c !important; color: white !important; border: none !important; }
    [data-testid="stSidebar"] h3, [data-testid="stSidebar"] label { color: white !important; }
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; width: 100%; font-weight: bold; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; width: 100%; font-weight: bold; }
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 20px; border-radius: 5px; margin-top: 10px; border-top: 8px solid #002366; color: black; line-height: 1.6; }
    .impact-stat { background-color: #f0f2f6; padding: 10px; border-radius: 5px; font-weight: bold; color: #002366; text-align: center; margin-bottom: 10px; border: 1px solid #002366; }
    </style>
    """, unsafe_allow_html=True)

# 3. CACHED WEATHER ENGINE
@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            
            temp = m.data.temperature.value if m.data.temperature else 0
            vis = m.data.visibility.value if m.data.visibility else 9999
            w_dir = m.data.wind_direction.value if m.data.wind_direction else 0
            w_spd = m.data.wind_speed.value if m.data.wind_speed else 0
            
            ceiling = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        ceiling = min(ceiling, layer.base * 100)
            
            forecast_low = False
            if t.data.forecast:
                for line in t.data.forecast[:2]:
                    f_vis = line.visibility.value if line.visibility else 9999
                    if f_vis < 1500: forecast_low = True

            results[iata] = {
                "temp": temp, "vis": vis, "w_dir": w_dir, "w_spd": w_spd,
                "ceiling": ceiling, "raw_metar": m.raw, "raw_taf": t.raw, "f_low": forecast_low
            }
        except: continue
    return results

# 4. DATABASE (Cityflyer & Euroflyer)
airports = {
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "RTM": {"icao": "EHRD", "name": "Rotterdam", "fleet": "Cityflyer", "rwy": 240, "lat": 51.957, "lon": 4.440},
    "DUB": {"icao": "EIDW", "name": "Dublin", "fleet": "Cityflyer", "rwy": 280, "lat": 53.421, "lon": -6.270},
    "GLA": {"icao": "EGPF", "name": "Glasgow", "fleet": "Cityflyer", "rwy": 230, "lat": 55.871, "lon": -4.433},
    "EDI": {"icao": "EGPH", "name": "Edinburgh", "fleet": "Cityflyer", "rwy": 240, "lat": 55.950, "lon": -3.363},
    "BHD": {"icao": "EGAC", "name": "Belfast City", "fleet": "Cityflyer", "rwy": 220, "lat": 54.618, "lon": -5.872},
    "STN": {"icao": "EGSS", "name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "SZG": {"icao": "LOWS", "name": "Salzburg", "fleet": "Euroflyer", "rwy": 330, "lat": 47.794, "lon": 13.004},
    "MLA": {"icao": "LMML", "name": "Malta", "fleet": "Euroflyer", "rwy": 310, "lat": 35.857, "lon": 14.477},
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

# LOAD DATA
weather_data = get_fleet_weather(airports)

# 5. SIDEBAR NAVIGATION
st.sidebar.markdown("### Airport Search")
search_iata = st.sidebar.text_input("Enter IATA Code", "").upper()

st.sidebar.markdown("---")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
map_theme = st.sidebar.radio("Map Theme", ["Light Mode", "Dark Mode"])
tile_style = "CartoDB positron" if map_theme == "Light Mode" else "CartoDB dark_matter"

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in airports: st.session_state.investigate_iata = search_iata

# 6. PROCESS DATA
active_alerts = {}
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
map_markers = []

for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        xw = get_xwind(data['w_dir'], data['w_spd'], info['rwy'])
        color = "#008000"
        alert_type = None
        if xw > 25 or data['vis'] < 800 or data['ceiling'] < 200:
            color = "#d6001a"; alert_type = "red"
        elif xw > 18 or data['vis'] < 1500 or data['ceiling'] < 500:
            color = "#eb8f34"; alert_type = "amber"
        
        if iata == "IVL" and data['temp'] <= -25:
            color = "#005a9c"; alert_type = "arctic"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "vis": data['vis'], "ceiling": data['ceiling'], "xw": xw, "f_low": data['f_low'], "metar": data['raw_metar']}
            counts[info['fleet']]["red" if alert_type=="red" else "orange"] += 1
        else:
            counts[info['fleet']]["green"] += 1
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "raw": data['raw_metar']})

# 7. HEADER
st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

col1, col2 = st.columns(2)
col1.metric("Cityflyer Fleet Status", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
col2.metric("Euroflyer Fleet Status", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")

# 8. MAIN DISPLAY
m_col, a_col = st.columns([3, 1.2])

with m_col:
    map_center = [48.0, 5.0]; zoom = 4
    if st.session_state.investigate_iata in airports:
        target = airports[st.session_state.investigate_iata]
        map_center = [target["lat"], target["lon"]]; zoom = 10
    
    m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
    for mkr in map_markers:
        is_sel = mkr['iata'] == st.session_state.investigate_iata
        folium.CircleMarker(
            location=[mkr['lat'], mkr['lon']],
            radius=14 if is_sel else (6 if zoom < 6 else 10),
            color=mkr['color'], fill=True, fill_opacity=0.9,
            popup=folium.Popup(f"<b>{mkr['iata']}</b><br>{mkr['raw']}", max_width=300, show=is_sel)
        ).add_to(m)
    st_folium(m, width=1000, height=700, key="occ_map")

with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    for iata, d in active_alerts.items():
        btn_kind = "primary" if d['type'] == "red" else "secondary"
        if st.button(f"{iata}: Critical Weather Issues", key=f"btn_{iata}", type=btn_kind):
            st.session_state.investigate_iata = iata
            st.rerun()
    
    if st.session_state.investigate_iata in active_alerts:
        d = active_alerts[st.session_state.investigate_iata]
        
        # IMPACT STATS LOGIC (Simulated for CFE/EFW/BA in 2026)
        # In a production env, this pulls from the Flight Ops API
        at_risk_arrivals = 3 if st.session_state.investigate_iata == "LCY" else 2
        
        st.markdown(f"""
        <div class="reason-box">
            <h3 style="margin:0; color:#002366;">{st.session_state.investigate_iata} Impact Analysis</h3>
            
            <div style="margin-top:15px;" class="impact-stat">
                {at_risk_arrivals} Scheduled BA/CFE/EFW Arrivals (T+4hrs)
            </div>

            <p><b>Summary:</b> {st.session_state.investigate_iata} visibility is {d['vis']}m with a cloud base of {d['ceiling']}ft.</p>
            <p><b>Trend:</b> Weather is {"forecast to remain below limits." if d['f_low'] else "improving shortly."}</p>
            <p><b>Risk:</b> High probability of diversions and network disruption for upcoming fleet arrivals.</p>
            <hr>
            <small><b>Latest METAR:</b><br>{d['metar']}</small>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("Close Analysis", key="close_btn"):
            st.session_state.investigate_iata = "None"
            st.rerun()
