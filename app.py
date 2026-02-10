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
    
    [data-testid="stSidebar"] .stTextInput input {
        color: #002366 !important;
        background-color: white !important;
        font-weight: bold;
    }
    
    .ba-header { background-color: #002366; padding: 20px; color: white; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    [data-testid="stSidebar"] { background-color: #002366 !important; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small, .reason-box span { color: #002366 !important; }
    
    /* SPECIAL CATEGORY BADGE */
    .spec-badge { background-color: #eb8f34; color: white; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: bold; margin-left: 5px; }
    </style>
    """, unsafe_allow_html=True)

# 3. SPECIAL CATEGORY LIST
# These airports have higher minima requirements due to terrain or approach types
special_cat_airports = ["EGLC", "LPMA", "LOWI", "LIRQ", "LFLB"]

# 4. UTILITIES
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

# 5. SESSION STATE
if 'manual_stations' not in st.session_state:
    st.session_state.manual_stations = {}

# --- MASTER FLEET DATABASE ---
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer"},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer"},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer"},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer"},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer"},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer"},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer"},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer"}
}

# 6. DATA PROCESSING
all_airports = {**base_airports, **st.session_state.manual_stations}

@st.cache_data(ttl=1800)
def get_fleet_weather(airport_dict):
    results = {}
    for iata, info in airport_dict.items():
        try:
            icao = info['icao']
            m = Metar(icao); m.update()
            t = Taf(icao); t.update()
            
            # Minima Logic
            is_special = icao in special_cat_airports
            v_limit = 1500 if is_special else 800
            c_limit = 500 if is_special else 200
            
            # Forecast Analysis
            t_vis = 9999; t_cig = 9999
            if t.data:
                for line in t.data.forecast:
                    if line.visibility: t_vis = min(t_vis, line.visibility.value)
                    if line.clouds:
                        for layer in line.clouds:
                            if layer.type in ['BKN', 'OVC'] and layer.base:
                                t_cig = min(t_cig, layer.base * 100)

            results[iata] = {
                "vis": m.data.visibility.value if m.data.visibility else 9999,
                "ceiling": 9999, "raw_metar": m.raw, "raw_taf": t.raw,
                "lat": info['lat'], "lon": info['lon'], "icao": icao,
                "f_vis": t_vis, "f_cig": t_cig, "is_special": is_special,
                "v_limit": v_limit, "c_limit": c_limit
            }
        except: continue
    return results

weather_data = get_fleet_weather(all_airports)

# 7. MAP RENDER
st.markdown(f'<div class="ba-header"><div>OCC HUD: SPECIAL CATEGORY MONITOR</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)

m = folium.Map(location=[48.0, 5.0], zoom_start=4, tiles="CartoDB dark_matter")

for iata, d in weather_data.items():
    # Alert Logic
    is_below = d['vis'] < d['v_limit'] or d['f_vis'] < d['v_limit']
    color = "#d6001a" if is_below else "#008000"
    
    spec_tag = "<span class='spec-badge'>SPECIAL CAT</span>" if d['is_special'] else ""
    
    popup_html = f"""
    <div style="width: 450px; color: black !important; font-family: sans-serif;">
        <h4 style="margin:0; color:#002366;">{iata} / {d['icao']} {spec_tag}</h4>
        <p style="font-size:11px; margin-bottom:10px;"><b>Minima:</b> {d['v_limit']}m / {d['c_limit']}ft</p>
        <div style="display:flex; gap:10px;">
            <div style="flex:1; background:#f0f2f6; padding:8px;"><b>METAR:</b><br>{d['raw_metar']}</div>
            <div style="flex:1; background:#f0f2f6; padding:8px;"><b>TAF:</b><br>{d['raw_taf']}</div>
        </div>
    </div>
    """
    folium.CircleMarker(
        location=[d['lat'], d['lon']], radius=8, color=color, fill=True,
        popup=folium.Popup(popup_html, max_width=500)
    ).add_to(m)

st_folium(m, width=1400, height=500, key="special_map")

# 8. ANALYSIS SECTION
st.markdown("### ⚠️ Network Status")
cols = st.columns(len(weather_data))
for i, (iata, d) in enumerate(weather_data.items()):
    with cols[i % len(cols)]:
        status = "🔴" if (d['vis'] < d['v_limit']) else "🟢"
        st.write(f"{status} **{iata}**")
