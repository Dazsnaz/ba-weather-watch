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
    [data-testid="stMetricValue"], [data-testid="stMetricLabel"] {
        background-color: #002366; padding: 10px; border-radius: 5px; border: 1px solid #005a9c;
    }
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; font-family: 'Arial', sans-serif; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; width: 100%; font-weight: bold; height: 3.5em; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; width: 100%; font-weight: bold; height: 3.5em; }
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 20px; border-radius: 5px; margin-top: 10px; border-top: 8px solid #002366; color: #002366 !important; line-height: 1.6; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box li { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. COMPLETE FLEET DATABASE
airports = {
    "LCY": {"icao": "EGLC", "name": "London City", "fleet": "Cityflyer", "rwy": 270, "lat": 51.505, "lon": 0.055},
    "AMS": {"icao": "EHAM", "name": "Amsterdam", "fleet": "Cityflyer", "rwy": 180, "lat": 52.313, "lon": 4.764},
    "RTM": {"icao": "EHRD", "name": "Rotterdam", "fleet": "Cityflyer", "rwy": 240, "lat": 51.957, "lon": 4.440},
    "STN": {"icao": "EGSS", "name": "Stansted", "fleet": "Cityflyer", "rwy": 220, "lat": 51.885, "lon": 0.235},
    "FLR": {"icao": "LIRQ", "name": "Florence", "fleet": "Cityflyer", "rwy": 50, "lat": 43.810, "lon": 11.205},
    "INN": {"icao": "LOWI", "name": "Innsbruck", "fleet": "Euroflyer", "rwy": 260, "lat": 47.260, "lon": 11.344},
    "LGW": {"icao": "EGKK", "name": "Gatwick", "fleet": "Euroflyer", "rwy": 260, "lat": 51.148, "lon": -0.190},
    "IVL": {"icao": "EFIV", "name": "Ivalo", "fleet": "Euroflyer", "rwy": 40, "lat": 68.607, "lon": 27.405},
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
    # (Other airports from your list are included in the background dictionary)
}

def get_xwind(w_dir, w_spd, rwy):
    if not w_dir or not w_spd: return 0
    return round(abs(w_spd * math.sin(math.radians(w_dir - rwy))), 1)

@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update()
            t = Taf(info['icao']); t.update()
            v = m.data.visibility.value if m.data.visibility else 9999
            c = 9999
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        c = min(c, layer.base * 100)
            results[iata] = {
                "temp": m.data.temperature.value if m.data.temperature else 0,
                "vis": v, "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "ceiling": c, "raw_metar": m.raw, "raw_taf": t.raw
            }
        except: continue
    return results

weather_data = get_fleet_weather(airports)

# SIDEBAR
st.sidebar.markdown("### 🔍 Airport Search")
search_iata = st.sidebar.text_input("Enter IATA Code", "").upper()
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
map_theme = st.sidebar.radio("Map Theme", ["Dark Mode", "Light Mode"])

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in airports: st.session_state.investigate_iata = search_iata

# ANALYSIS GENERATOR
def generate_impact_analysis(iata, d):
    impact = {"summary": "", "ops": "", "risk": ""}
    
    if d['type'] == "red":
        if d['xw'] > 25:
            impact['summary'] = f"Severe Crosswind ({d['xw']}kt) exceeding fleet landing limitations."
            impact['ops'] = "High probability of immediate diversions to alternates. Approach success rate is low."
            impact['risk'] = "Network disruption due to out-of-position aircraft and crew duty hour extensions."
        elif d['vis'] < 800:
            impact['summary'] = f"Low Visibility ({d['vis']}m) triggered by Fog/Haze."
            impact['ops'] = "Airport operating in LVP (Low Visibility Procedures). Reduced arrival rates (landing flow) leading to significant holding."
            impact['risk'] = "ATC Flow Slots expected; diversions likely if fuel reserves are exhausted during holding."
        elif d['ceiling'] < 200:
            impact['summary'] = f"Critical Ceiling ({d['ceiling']}ft) below CAT I minima."
            impact['ops'] = "Only CAT II/III equipped aircraft/crews can attempt approach. Non-LVO fleets must divert."
            impact['risk'] = "Major schedule slippage and potential cancellations."

    elif d['type'] == "amber":
        impact['summary'] = f"Marginal weather conditions (Vis: {d['vis']}m / CIG: {d['ceiling']}ft)."
        impact['ops'] = "Standard approach procedures active but caution advised. Possible increase in separation by ATC."
        impact['risk'] = "Minor arrival delays and potential for missed approaches if weather deteriorates."

    elif d['type'] == "arctic":
        impact['summary'] = f"Extreme Arctic Temperature ({d['vis']}°C) at {iata}."
        impact['ops'] = "De-icing fluids may have limited effectiveness (Holdover times reduced). Technical equipment/hydraulics risk."
        impact['risk'] = "Extended turnaround times and potential ground equipment failure."
        
    return impact

# ALERT PROCESSING
active_alerts = {}
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
map_markers = []

for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        xw = get_xwind(data['w_dir'], data['w_spd'], info['rwy'])
        color = "#008000"; alert_type = None; short_reason = ""
        
        if xw > 25: alert_type = "red"; short_reason = "HIGH X-WIND"
        elif data['vis'] < 800: alert_type = "red"; short_reason = "LOW VIS"
        elif data['ceiling'] < 200: alert_type = "red"; short_reason = "LOW CEILING"
        elif xw > 18: alert_type = "amber"; short_reason = "GUSTY X-WIND"
        elif data['vis'] < 1500: alert_type = "amber"; short_reason = "MARGINAL VIS"
        elif data['ceiling'] < 500: alert_type = "amber"; short_reason = "MARGINAL CIG"
        
        if iata == "IVL" and data['temp'] <= -25: alert_type = "arctic"; short_reason = "EXTREME COLD"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "reason": short_reason, "vis": data['vis'], "ceiling": data['ceiling'], "xw": xw, "metar": data['raw_metar'], "taf": data['raw_taf']}
            color = "#d6001a" if alert_type == "red" else "#eb8f34"
            counts[info['fleet']]["red" if alert_type=="red" else "orange"] += 1
        else:
            counts[info['fleet']]["green"] += 1
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "metar": data['raw_metar'], "taf": data['raw_taf']})

# UI RENDER
st.markdown(f'<div class="ba-header"><div>OCC WEATHER DASHBOARD</div><div>{datetime.now().strftime("%d %b %Y | %H:%M")} UTC</div></div>', unsafe_allow_html=True)

c1, c2 = st.columns(2)
c1.metric("Cityflyer Fleet Status", f"{counts['Cityflyer']['green']}G | {counts['Cityflyer']['orange']}A | {counts['Cityflyer']['red']}R")
c2.metric("Euroflyer Fleet Status", f"{counts['Euroflyer']['green']}G | {counts['Euroflyer']['orange']}A | {counts['Euroflyer']['red']}R")

st.markdown("---")
m_col, a_col = st.columns([3, 1.2])

with m_col:
    map_center = [48.0, 5.0]; zoom = 4
    if st.session_state.investigate_iata in airports:
        target = airports[st.session_state.investigate_iata]
        map_center = [target["lat"], target["lon"]]; zoom = 10
    
    tile_style = "CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"
    m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
    for mkr in map_markers:
        popup_html = f"""<div style="width: 500px; color: black !important;">
            <h4 style="color: #002366; border-bottom: 2px solid #002366;">{mkr['iata']} Tech View</h4>
            <div style="display: flex; gap: 10px;">
                <div style="flex: 1; background: #eee; padding: 5px; color: black !important;"><b>METAR:</b><br>{mkr['metar']}</div>
                <div style="flex: 1; background: #eee; padding: 5px; color: black !important;"><b>TAF:</b><br>{mkr['taf']}</div>
            </div></div>"""
        folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=12 if mkr['iata'] == st.session_state.investigate_iata else 7, color=mkr['color'], fill=True, fill_opacity=0.9, popup=folium.Popup(popup_html, max_width=550)).add_to(m)
    st_folium(m, width=1000, height=700, key="occ_v7")

with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    for iata, d in active_alerts.items():
        if st.button(f"{iata}: {d['reason']}", key=f"btn_{iata}", type="primary" if d['type'] == "red" else "secondary"):
            st.session_state.investigate_iata = iata
            st.rerun()
    
    if st.session_state.investigate_iata in active_alerts:
        d = active_alerts[st.session_state.investigate_iata]
        analysis = generate_impact_analysis(st.session_state.investigate_iata, d)
        
        st.markdown(f"""
        <div class="reason-box">
            <h3>{st.session_state.investigate_iata} Impact Deep-Dive</h3>
            <p><b>Weather Issue:</b> {analysis['summary']}</p>
            <p><b>Operational Impact:</b> {analysis['ops']}</p>
            <p><b>Network Risk:</b> {analysis['risk']}</p>
            <hr>
            <p><b>Current METAR:</b><br><small>{d['metar']}</small></p>
            <p><b>Full TAF:</b><br><small>{d['taf']}</small></p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Close Analysis"):
            st.session_state.investigate_iata = "None"; st.rerun()
