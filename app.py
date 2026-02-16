import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
import re
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Command HUD", page_icon="✈️")

# 2. HUD STYLING (UNIFIED INTERFACE)
st.markdown("""
    <style>
    .section-header { color: #002366 !important; font-weight: bold; font-size: 1.5rem; margin-top: 20px; border-bottom: 2px solid #d6001a; padding-bottom: 5px; }
    html, body, [class*="st-"], div, p, h1, h2, h4, label { color: white !important; }
    [data-testid="stTextArea"] textarea { color: #002366 !important; background-color: #ffffff !important; font-weight: bold; font-family: 'Courier New', monospace; }
    [data-testid="stSidebar"] { background-color: #002366 !important; min-width: 250px !important; border-right: 2px solid #d6001a; }
    [data-testid="stSidebar"] .stTextInput input { color: #002366 !important; background-color: white !important; font-weight: bold; }
    
    /* CONCISE ALERT BUTTONS */
    .stButton > button { 
        background-color: #005a9c !important; color: white !important; border: 1px solid white !important; 
        width: 100%; text-transform: uppercase; font-size: 0.72rem !important; height: 50px !important; 
        line-height: 1.1 !important; white-space: nowrap !important; overflow: hidden;
        text-overflow: ellipsis; display: flex; align-items: center; justify-content: center; 
        text-align: center; padding: 2px 10px !important;
    }
    
    .ba-header { background-color: #002366; padding: 20px; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; }
    
    /* STRATEGY & MAP INTERFACE FIXES */
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
    .reason-box h3, .reason-box p, .reason-box b { color: #002366 !important; }
    
    /* Force tooltips to use the same background as popups */
    .leaflet-tooltip { 
        background-color: white !important; 
        border: 2px solid #002366 !important; 
        border-radius: 6px !important; 
        box-shadow: 0 4px 12px rgba(0,0,0,0.3) !important;
        padding: 0 !important; 
        opacity: 1 !important;
    }
    </style>
    """, unsafe_allow_html=True)

# 3. UTILITIES
def calculate_dist(lat1, lon1, lat2, lon2):
    R = 3440.065 
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)

def calculate_xwind(wind_dir, wind_spd, rwy_hdg):
    if wind_dir is None or wind_spd is None or rwy_hdg is None: return 0
    angle = math.radians(wind_dir - rwy_hdg)
    return round(abs(wind_spd * math.sin(angle)))

def bold_hazard(text):
    if not text or text == "N/A": return text
    text = re.sub(r'(\b\d{4}\b)', r'<b>\1</b>', text)
    text = re.sub(r'((BKN|OVC)\d{3})', r'<b>\1</b>', text)
    text = re.sub(r'(\b(FG|TSRA|SN|-SN|FZRA|FZDZ|TS|VIS|CLOUD|FOG|XWIND|WIND)\b)', r'<b>\1</b>', text)
    text = re.sub(r'(\b\d{3}\d{2}(G\d{2})?KT\b)', r'<b>\1</b>', text)
    return text

# 4. MASTER DATABASE (FULL 47 STATIONS)
base_airports = {
    "LCY": {"icao": "EGLC", "lat": 51.505, "lon": 0.055, "rwy": 270, "fleet": "Cityflyer", "spec": True},
    "AMS": {"icao": "EHAM", "lat": 52.313, "lon": 4.764, "rwy": 180, "fleet": "Cityflyer", "spec": False},
    "EDI": {"icao": "EGPH", "lat": 55.950, "lon": -3.363, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "GLA": {"icao": "EGPF", "lat": 55.871, "lon": -4.433, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "BHD": {"icao": "EGAC", "lat": 54.618, "lon": -5.872, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "STN": {"icao": "EGSS", "lat": 51.885, "lon": 0.235, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "RTM": {"icao": "EHRD", "lat": 51.957, "lon": 4.440, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "DUB": {"icao": "EIDW", "lat": 53.421, "lon": -6.270, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "FLR": {"icao": "LIRQ", "lat": 43.810, "lon": 11.205, "rwy": 50, "fleet": "Cityflyer", "spec": True},
    "CMF": {"icao": "LFLB", "lat": 45.638, "lon": 5.880, "rwy": 180, "fleet": "Cityflyer", "spec": True},
    "ZRH": {"icao": "LSZH", "lat": 47.458, "lon": 8.548, "rwy": 160, "fleet": "Cityflyer", "spec": False},
    "GVA": {"icao": "LSGG", "lat": 46.237, "lon": 6.109, "rwy": 220, "fleet": "Cityflyer", "spec": False},
    "BER": {"icao": "EDDB", "lat": 52.362, "lon": 13.501, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "FRA": {"icao": "EDDF", "lat": 50.033, "lon": 8.571, "rwy": 250, "fleet": "Cityflyer", "spec": False},
    "LIN": {"icao": "LIML", "lat": 45.445, "lon": 9.277, "rwy": 360, "fleet": "Cityflyer", "spec": False},
    "MAD": {"icao": "LEMD", "lat": 40.494, "lon": -3.567, "rwy": 140, "fleet": "Cityflyer", "spec": False},
    "IBZ": {"icao": "LEIB", "lat": 38.873, "lon": 1.373, "rwy": 60, "fleet": "Cityflyer", "spec": False},
    "PMI": {"icao": "LEPA", "lat": 39.551, "lon": 2.738, "rwy": 240, "fleet": "Cityflyer", "spec": False},
    "AGP": {"icao": "LEMG", "lat": 36.675, "lon": -4.499, "rwy": 130, "fleet": "Cityflyer", "spec": False},
    "FAO": {"icao": "LPFR", "lat": 37.017, "lon": -7.965, "rwy": 280, "fleet": "Cityflyer", "spec": False},
    "SEN": {"icao": "EGMC", "lat": 51.571, "lon": 0.701, "rwy": 230, "fleet": "Cityflyer", "spec": False},
    "LGW": {"icao": "EGKK", "lat": 51.148, "lon": -0.190, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "JER": {"icao": "EGJJ", "lat": 49.208, "lon": -2.195, "rwy": 260, "fleet": "Euroflyer", "spec": False},
    "INN": {"icao": "LOWI", "lat": 47.260, "lon": 11.344, "rwy": 260, "fleet": "Euroflyer", "spec": True},
    "FNC": {"icao": "LPMA", "lat": 32.694, "lon": -16.774, "rwy": 50, "fleet": "Euroflyer", "spec": True},
    "NCE": {"icao": "LFMN", "lat": 43.665, "lon": 7.215, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "VRN": {"icao": "LIPX", "lat": 45.396, "lon": 10.888, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "OPO": {"icao": "LPPR", "lat": 41.242, "lon": -8.678, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "LYS": {"icao": "LFLL", "lat": 45.726, "lon": 5.090, "rwy": 350, "fleet": "Euroflyer", "spec": False},
    "SZG": {"icao": "LOWS", "lat": 47.794, "lon": 13.004, "rwy": 330, "fleet": "Euroflyer", "spec": False},
    "BOD": {"icao": "LFBD", "lat": 44.828, "lon": -0.716, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "GNB": {"icao": "LFLS", "lat": 45.363, "lon": 5.330, "rwy": 90, "fleet": "Euroflyer", "spec": False},
    "TRN": {"icao": "LIMF", "lat": 45.202, "lon": 7.649, "rwy": 360, "fleet": "Euroflyer", "spec": False},
    "ALC": {"icao": "LEAL", "lat": 38.282, "lon": -0.558, "rwy": 100, "fleet": "Euroflyer", "spec": False},
    "SVQ": {"icao": "LEZL", "lat": 37.418, "lon": -5.893, "rwy": 270, "fleet": "Euroflyer", "spec": False},
    "RAK": {"icao": "GMMX", "lat": 31.606, "lon": -8.036, "rwy": 100, "fleet": "Euroflyer", "spec": False},
    "AGA": {"icao": "GMAD", "lat": 30.325, "lon": -9.413, "rwy": 90, "fleet": "Euroflyer", "spec": False},
    "SSH": {"icao": "HESH", "lat": 27.977, "lon": 34.394, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "PFO": {"icao": "LCPH", "lat": 34.718, "lon": 32.486, "rwy": 290, "fleet": "Euroflyer", "spec": False},
    "LCA": {"icao": "LCLK", "lat": 34.875, "lon": 33.625, "rwy": 220, "fleet": "Euroflyer", "spec": False},
    "FUE": {"icao": "GCLP", "lat": 28.452, "lon": -13.864, "rwy": 10, "fleet": "Euroflyer", "spec": False},
    "TFS": {"icao": "GCTS", "lat": 28.044, "lon": -16.572, "rwy": 70, "fleet": "Euroflyer", "spec": False},
    "ACE": {"icao": "GCRR", "lat": 28.945, "lon": -13.605, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "LPA": {"icao": "GCLP", "lat": 27.931, "lon": -15.386, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "IVL": {"icao": "EFIV", "lat": 68.607, "lon": 27.405, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "MLA": {"icao": "LMML", "lat": 35.857, "lon": 14.477, "rwy": 310, "fleet": "Euroflyer", "spec": False},
    "ALG": {"icao": "DAAG", "lat": 36.691, "lon": 3.215, "rwy": 230, "fleet": "Euroflyer", "spec": False},
}

# 5. SESSION STATE
if 'investigate_iata' not in st.session_state: st.session_state.investigate_iata = "None"

# 6. SIDEBAR
with st.sidebar:
    st.title("🛠️ COMMAND SETTINGS")
    if st.button("🔄 MANUAL DATA REFRESH"):
        st.cache_data.clear(); st.rerun()
    st.markdown("---")
    show_cf = st.checkbox("Cityflyer (CFE)", value=True)
    show_ef = st.checkbox("Euroflyer (EFW)", value=True)
    map_theme = st.radio("MAP THEME", ["Dark Mode", "Light Mode"])
    st.markdown("---")
    st.markdown("📊 **FLEET X-WIND LIMITS**")
    st.markdown("""<table style='width:100%; font-size:0.8rem; color:white;'><tr><th>FLEET</th><th>DRY</th><th>WET</th></tr><tr><td>A320/321</td><td>38 kt</td><td>33 kt</td></tr><tr><td>E190/170</td><td>30 kt</td><td>25 kt</td></tr></table>""", unsafe_allow_html=True)

# 7. BACKGROUND FETCH (STABLE ENGINE)
@st.cache_data(ttl=600)
def get_intel_global(airport_dict):
    res = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update(); t = Taf(info['icao']); t.update()
            v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
            w_vis, w_cig, w_time, w_prob = 9999, 9999, "", False
            w_issues = []
            
            if t.data:
                for line in t.data.forecast:
                    l_raw = line.raw.upper()
                    l_issues = []
                    v = line.visibility.value if line.visibility else 9999
                    c = 9999
                    if line.clouds:
                        for lyr in line.clouds:
                            if lyr.type in ['BKN', 'OVC'] and lyr.base: c = min(c, lyr.base * 100)
                    
                    l_dir = line.wind_direction.value if line.wind_direction else info['rwy']
                    l_spd = line.wind_speed.value if line.wind_speed else 0
                    l_gst = line.wind_gust.value if line.wind_gust else 0
                    l_xw = calculate_xwind(l_dir, max(l_spd, l_gst), info['rwy'])
                    
                    if any(x in l_raw for x in ["SN", "FG", "FZRA", "FZDZ"]): l_issues.append("WINTER/FOG")
                    if v < v_lim: l_issues.append("VIS")
                    if c < c_lim: l_issues.append("CLOUD")
                    if "TSRA" in l_raw: l_issues.append("TSRA")
                    if l_xw >= 25: l_issues.append("XWIND")
                    if l_gst > 25: l_issues.append("WIND")
                    
                    if l_issues:
                        if not w_issues or v < w_vis or c < w_cig or "WINTER" in str(l_issues):
                            w_vis, w_cig, w_issues, w_prob = v, c, l_issues, ("PROB" in l_raw)
                            w_time = f"{line.start_time.dt.strftime('%H')}-{line.end_time.dt.strftime('%H')}Z"
            
            res[iata] = {
                "vis": m.data.visibility.value if (m.data and m.data.visibility) else 9999,
                "cig": 9999, "w_dir": m.data.wind_direction.value if (m.data and m.data.wind_direction) else 0,
                "w_spd": m.data.wind_speed.value if (m.data and m.data.wind_speed) else 0,
                "w_gst": m.data.wind_gust.value if (m.data and m.data.wind_gust) else 0,
                "raw_m": m.raw or "N/A", "raw_t": t.raw or "N/A", "status": "online",
                "f_issues": w_issues, "f_time": w_time, "f_prob": w_prob
            }
            if m.data and m.data.clouds:
                for lyr in m.data.clouds:
                    if lyr.type in ['BKN', 'OVC'] and lyr.base: res[iata]["cig"] = min(res[iata].get("cig", 9999), lyr.base * 100)
        except: res[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A", "f_issues": []}
    return res

weather_data = get_intel_global(base_airports)

# 8. FILTER & UI LOOP
metar_alerts, taf_alerts, green_stations, map_markers = {}, {}, [], []
for iata, info in base_airports.items():
    data = weather_data.get(iata)
    if not data: continue
    is_shown = (info['fleet'] == "Cityflyer" and show_cf) or (info['fleet'] == "Euroflyer" and show_ef)
    if not is_shown: continue

    v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
    color, m_issues, actual_str, forecast_str = "#008000", [], "STABLE", "NIL"
    xw = 0
    r1, r2 = int(info['rwy']/10), int(((info['rwy']+180)%360)/10)

    if data['status'] == "online":
        xw = calculate_xwind(data.get('w_dir', 0), max(data.get('w_spd', 0), data.get('w_gst', 0)), info['rwy'])
        raw_m = data['raw_m'].upper()
        
        if any(x in raw_m for x in [" SN ", "-SN ", "FG ", "FZRA", "FZDZ"]): m_issues.append("WINTER/FOG"); color = "#d6001a"
        if data['vis'] < v_lim: m_issues.append("VIS"); color = "#d6001a"
        if data.get("cig", 9999) < c_lim: m_issues.append("CLOUD"); color = "#d6001a"
        if xw >= 25: m_issues.append("XWIND"); color = "#d6001a"
        if data.get('w_gst', 0) > 25 and "XWIND" not in m_issues: m_issues.append("WIND"); color = "#eb8f34" if color == "#008000" else color
        
        if m_issues: 
            actual_str = "/".join(m_issues)
            metar_alerts[iata] = {"type": actual_str, "hex": "primary" if color == "#d6001a" else "secondary"}
        else: green_stations.append(iata)
        
        if data['f_issues']:
            p_tag = " prob" if data['f_prob'] else ""
            forecast_str = f"{'+'.join(data['f_issues'])}{p_tag} @ {data['f_time']}"
            taf_alerts[iata] = {"type": "+".join(data['f_issues']), "time": data['f_time'], "prob": data['f_prob'], "hex": "secondary"}
            if color == "#008000": color = "#eb8f34"

    m_bold, t_bold = bold_hazard(data.get('raw_m', 'N/A')), bold_hazard(data.get('raw_t', 'N/A'))
    
    # 8.2 POPUP HTML (SHARED BY HOVER AND CLICK)
    popup_html = f"""<div style="width:580px; color:black !important; font-family:monospace; font-size:14px; background:white; padding:15px; border-radius:5px;"><b style="color:#002366; font-size:18px;">{iata} STATUS</b><div style="margin-top:8px; padding:10px; border-left:6px solid {color}; background:#f9f9f9; font-size:16px;"><b style="color:#002366;">RWY {r1:02d}/{r2:02d} Live X-Wind:</b> <span style="color:{'#d6001a' if xw >= 25 else 'black'}; font-weight:bold;">{xw} KT</span><br><b>ACTUAL:</b> {actual_str}<br><b>FORECAST:</b> {forecast_str}</div><hr style="border:1px solid #ddd;"><div style="display:flex; gap:12px;"><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; font-size:14px; white-space:pre-wrap;"><b>METAR</b><br>{m_bold}</div><div style="flex:1; background:#f0f0f0; padding:10px; border-radius:4px; font-size:14px; white-space:pre-wrap;"><b>TAF</b><br>{t_bold}</div></div></div>"""
    map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "popup": popup_html})

# --- UI RENDER ---
st.markdown(f'<div class="ba-header"><div>OCC WINTER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)
m = folium.Map(location=[50.0, 10.0], zoom_start=4, tiles=("CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"), scrollWheelZoom=False)

for mkr in map_markers:
    folium.CircleMarker(
        location=[mkr['lat'], mkr['lon']], radius=7, color=mkr['color'], fill=True,
        # CLICK POPUP (Pinned)
        popup=folium.Popup(mkr['popup'], max_width=650),
        # HOVER TOOLTIP (Centered Top)
        tooltip=folium.Tooltip(mkr['popup'], direction='top', sticky=False)
    ).add_to(m)

st_folium(m, width=1200, height=1200, key="map_unified_v202")

# 10. ALERTS
st.markdown('<div class="section-header">🔴 Actual Alerts (METAR)</div>', unsafe_allow_html=True)
if metar_alerts:
    cols = st.columns(5)
    for i, (iata, d) in enumerate(metar_alerts.items()):
        with cols[i % 5]:
            if st.button(f"{iata} NOW {d['type']}", key=f"m_{iata}", type="primary"): st.session_state.investigate_iata = iata

st.markdown('<div class="section-header">🟠 Forecast Alerts (TAF)</div>', unsafe_allow_html=True)
if taf_alerts:
    cols_f = st.columns(5)
    for i, (iata, d) in enumerate(taf_alerts.items()):
        with cols_f[i % 5]:
            p_tag = " prob" if d['prob'] else ""
            if st.button(f"{iata} {d['time']} {d['type']}{p_tag}", key=f"f_{iata}", type="secondary"): st.session_state.investigate_iata = iata

# 11. STRATEGY BRIEF (ANALYSIS)
if st.session_state.investigate_iata != "None":
    iata = st.session_state.investigate_iata
    d, info = weather_data.get(iata, {}), base_airports.get(iata, {"rwy": 0, "lat": 0, "lon": 0})
    issue_desc = (taf_alerts.get(iata, {}) or metar_alerts.get(iata, {}) or {}).get('type', "STABLE")
    xw_val = calculate_xwind(d.get('w_dir', 0), max(d.get('w_spd', 0), d.get('w_gst', 0)), info['rwy'])
    
    impact = "Standard operations. Monitor trends."
    if "VIS" in issue_desc or "CLOUD" in issue_desc: impact = "LVP procedures likely. CAT III currency required."
    elif "WINTER" in issue_desc: impact = "Winter precip limits breached. Embraer/Airbus de-icing required."
    elif "XWIND" in issue_desc: impact = f"Critical crosswind ({xw_val}kt) exceeds limits. Verify runway state."

    alt_iata, min_dist = "None", 9999
    for g in green_stations:
        if g != iata:
            dist = calculate_dist(info['lat'], info['lon'], base_airports[g]['lat'], base_airports[g]['lon'])
            if dist < min_dist: min_dist = dist; alt_iata = g

    st.markdown(f"""
    <div class="reason-box">
        <h3>{iata} Strategy Brief: {issue_desc}</h3>
        <p><b>WX Summary:</b> Live crosswind <b>{xw_val}kt</b> for RWY {info['rwy']}°. <b>Impact:</b> {impact}</p>
        <p style="color:#d6001a !important; font-size:1.1rem;"><b>✈️ Strategic Alternate:</b> {alt_iata} ({min_dist} NM).</p>
        <hr>
        <div style="display:flex; gap:20px;">
            <div style="flex:1;"><b>METAR:</b><br><small>{bold_hazard(d.get('raw_m'))}</small></div>
            <div style="flex:1;"><b>TAF:</b><br><small>{bold_hazard(d.get('raw_t'))}</small></div>
        </div>
    </div>""", unsafe_allow_html=True)
    if st.button("Close Strategy Brief"): st.session_state.investigate_iata = "None"; st.rerun()
