import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Weather Dashboard", page_icon="✈️")

# 2. CUSTOM OCC STYLING (White Fonts & Navy Sidebar)
st.markdown("""
    <style>
    html, body, [class*="st-"], div, p, h1, h2, h3, h4, label {
        color: white !important;
    }
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; font-family: 'Arial', sans-serif; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    span[data-baseweb="tag"] { background-color: #005a9c !important; color: white !important; }
    div[data-baseweb="select"] > div { background-color: #005a9c !important; color: white !important; border: none !important; }
    
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; color: white !important; border: none !important; width: 100%; font-weight: bold; height: 3em; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; color: white !important; border: none !important; width: 100%; font-weight: bold; height: 3em; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 20px; border-radius: 5px; margin-top: 10px; border-top: 8px solid #002366; color: #002366 !important; line-height: 1.6; }
    .reason-box h3, .reason-box h4, .reason-box p, .reason-box b, .reason-box li { color: #002366 !important; }
    </style>
    """, unsafe_allow_html=True)

# 3. FULL FLEET DATABASE
airports = {
    # --- CITYFLYER (CF) ---
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

    # --- EUROFLYER (EF) ---
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
    "FNC": {"icao": "LPMA", "name": "Madeira", "fleet": "Euroflyer", "rwy": 50, "lat": 32.694, "lon": -16.774},
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
            results[iata] = {
                "temp": m.data.temperature.value if m.data.temperature else 0,
                "vis": m.data.visibility.value if m.data.visibility else 9999,
                "w_dir": m.data.wind_direction.value if m.data.wind_direction else 0,
                "w_spd": m.data.wind_speed.value if m.data.wind_speed else 0,
                "ceiling": 9999, "raw_metar": m.raw, "raw_taf": t.raw
            }
            if m.data.clouds:
                for layer in m.data.clouds:
                    if layer.type in ['BKN', 'OVC'] and layer.base:
                        results[iata]["ceiling"] = min(results[iata]["ceiling"], layer.base * 100)
        except: continue
    return results

# DATA FETCH
weather_data = get_fleet_weather(airports)

# SIDEBAR & SEARCH
st.sidebar.markdown("### 🔍 Airport Search")
search_iata = st.sidebar.text_input("Enter IATA Code (e.g. LCY)", "").upper()
st.sidebar.markdown("---")
fleet_filter = st.sidebar.multiselect("Active Fleet", ["Cityflyer", "Euroflyer"], default=["Cityflyer", "Euroflyer"])
map_theme = st.sidebar.radio("Map Theme", ["Light Mode", "Dark Mode"])
tile_style = "CartoDB positron" if map_theme == "Light Mode" else "CartoDB dark_matter"

if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"
if search_iata in airports: st.session_state.investigate_iata = search_iata

# PROCESS STATUS & ALERTS
active_alerts = {}
counts = {"Cityflyer": {"green": 0, "orange": 0, "red": 0}, "Euroflyer": {"green": 0, "orange": 0, "red": 0}}
map_markers = []

for iata, data in weather_data.items():
    info = airports[iata]
    if info['fleet'] in fleet_filter:
        xw = get_xwind(data['w_dir'], data['w_spd'], info['rwy'])
        color = "#008000"; alert_type = None
        
        if xw > 25 or data['vis'] < 800 or data['ceiling'] < 200:
            color = "#d6001a"; alert_type = "red"
        elif xw > 18 or data['vis'] < 1500 or data['ceiling'] < 500:
            color = "#eb8f34"; alert_type = "amber"
        
        if iata == "IVL" and data['temp'] <= -25: color = "#005a9c"; alert_type = "arctic"

        if alert_type:
            active_alerts[iata] = {"type": alert_type, "vis": data['vis'], "ceiling": data['ceiling'], "xw": xw, "metar": data['raw_metar'], "taf": data['raw_taf']}
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
    
    m = folium.Map(location=map_center, zoom_start=zoom, tiles=tile_style)
    for mkr in map_markers:
        popup_html = f"""<div style="width: 500px; color: black !important;">
            <h4 style="color: #002366; border-bottom: 2px solid #002366;">{mkr['iata']} Details</h4>
            <div style="display: flex; gap: 10px;">
                <div style="flex: 1; background: #eee; padding: 5px;"><b>METAR:</b><br>{mkr['metar']}</div>
                <div style="flex: 1; background: #eee; padding: 5px;"><b>TAF:</b><br>{mkr['taf']}</div>
            </div></div>"""
        folium.CircleMarker(
            location=[mkr['lat'], mkr['lon']],
            radius=12 if mkr['iata'] == st.session_state.investigate_iata else 7,
            color=mkr['color'], fill=True, fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=550)
        ).add_to(m)
    st_folium(m, width=1000, height=700, key="occ_v4")

with a_col:
    st.markdown("#### ⚠️ Operational Alerts")
    for iata, d in active_alerts.items():
        if st.button(f"{iata}: Check Operational Impact", key=f"btn_{iata}", type="primary" if d['type'] == "red" else "secondary"):
            st.session_state.investigate_iata = iata
            st.rerun()
    
    if st.session_state.investigate_iata in active_alerts:
        d = active_alerts[st.session_state.investigate_iata]
        
        # PREDICTIVE ANALYSIS LOGIC
        impact_reasons = []
        if d['vis'] < 800: impact_reasons.append(f"current visibility is {d['vis']}m which is below standard operating limits")
        if d['ceiling'] < 200: impact_reasons.append(f"the cloud base is at {d['ceiling']}ft, below ILS Cat I minima")
        if d['xw'] > 25: impact_reasons.append(f"crosswinds are currently {d['xw']}kt, exceeding aircraft limitations")
        
        reason_str = " and ".join(impact_reasons)
        
        st.markdown(f"""
        <div class="reason-box">
            <h3 style="margin:0;">{st.session_state.investigate_iata} Analysis</h3>
            <p><b>Issue:</b> {st.session_state.investigate_iata} {reason_str}.</p>
            <p><b>Impact:</b> This forecast is currently below operating limits and <b>may cause diversions or ATC slots</b> due to the weather. 
            Expect <b>long delays to the operation</b> if conditions persist as forecast in the TAF.</p>
            <hr>
            <p style="font-size: 11px;"><b>Current METAR:</b> {d['metar']}</p>
            <p style="font-size: 11px;"><b>Forecast TAF:</b> {d['taf']}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Clear Investigation"):
            st.session_state.investigate_iata = "None"; st.rerun()
