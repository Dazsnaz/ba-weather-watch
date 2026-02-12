import streamlit as st
import folium
from streamlit_folium import st_folium
from avwx import Metar, Taf
import math
import re
import urllib.parse
from datetime import datetime

# 1. PAGE CONFIG
st.set_page_config(layout="wide", page_title="BA OCC Command HUD", page_icon="✈️")

# 2. HUD STYLING
st.markdown("""
    <style>
    .section-header { color: #002366 !important; font-weight: bold; font-size: 1.5rem; margin-top: 20px; border-bottom: 2px solid #d6001a; padding-bottom: 5px; display: flex; align-items: center; }
    html, body, [class*="st-"], div, p, h1, h2, h4, label { color: white !important; }
    [data-testid="stTextArea"] textarea { color: #002366 !important; background-color: #ffffff !important; font-weight: bold; font-family: 'Courier New', monospace; }
    [data-testid="stSidebar"] { background-color: #002366 !important; min-width: 250px !important; }
    [data-testid="stSidebar"] .stTextInput input { color: #002366 !important; background-color: white !important; font-weight: bold; }
    
    .stButton > button { 
        background-color: #005a9c !important; color: white !important; border: 1px solid white !important; 
        width: 100% !important; text-transform: uppercase; font-size: 0.72rem !important; 
        height: 42px !important; line-height: 1.0 !important; white-space: nowrap !important; 
        display: flex; align-items: center; justify-content: center; text-align: center; 
        padding: 0 10px !important; border-radius: 4px !important;
    }
    
    .ba-header { background-color: #002366; padding: 20px; border-radius: 5px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
    div.stButton > button[kind="primary"] { background-color: #d6001a !important; }
    div.stButton > button[kind="secondary"] { background-color: #eb8f34 !important; }
    
    .reason-box { background-color: #ffffff; border: 1px solid #ddd; padding: 25px; border-radius: 5px; margin-top: 20px; border-top: 10px solid #d6001a; color: #002366 !important; box-shadow: 0 4px 10px rgba(0,0,0,0.1); }
    .reason-box h3, .reason-box p, .reason-box b, .reason-box small { color: #002366 !important; }
    
    .limits-table { width: 100%; font-size: 0.8rem; border-collapse: collapse; margin-top: 10px; color: white !important; }
    .limits-table td, .limits-table th { border: 1px solid rgba(255,255,255,0.2); padding: 4px; text-align: left; }
    
    .copy-btn { margin-left: 15px; cursor: pointer; font-size: 1.3rem; filter: grayscale(1); transition: 0.2s; }
    .copy-btn:hover { filter: grayscale(0); transform: scale(1.2); }
    </style>

    <script>
    function tacticalCopy(encodedText) {
        const text = decodeURIComponent(encodedText);
        const textArea = document.createElement("textarea");
        textArea.value = text;
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        alert("TACTICAL DATA COPIED");
    }
    </script>
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
    # 1. Bold low visibility (0000 to 1500)
    text = re.sub(r'(\b(0\d{2}\d|1[0-4]\d{2})\b)', r'<b>\1</b>', text)
    # 2. Bold low ceilings (BKN/OVC below 010)
    text = re.sub(r'((BKN|OVC|SCT)00[0-9])', r'<b>\1</b>', text)
    # 3. Bold tactical hazards
    text = re.sub(r'(\b(FG|TSRA|SHSN|SN|FZRA|FZDZ|TS)\b)', r'<b>\1</b>', text)
    # 4. Bold high gusts (G25 or higher)
    text = re.sub(r'(\b\d{5}G(2[5-9]|[3-9]\d)KT\b)', r'<b>\1</b>', text)
    # 5. Bold Probability/Tempo blocks for tactical emphasis
    text = re.sub(r'(\b(TEMPO|PROB\d{2})\b)', r'<b>\1</b>', text)
    return text

# 4. MASTER DATABASE
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
    "MLA": {"icao": "LMML", "lat": 35.857, "lon": 14.477, "rwy": 310, "fleet": "Euroflyer", "spec": False},
    "ALG": {"icao": "DAAG", "lat": 36.691, "lon": 3.215, "rwy": 230, "fleet": "Euroflyer", "spec": False},
    "TFS": {"icao": "GCTS", "lat": 28.044, "lon": -16.572, "rwy": 70, "fleet": "Euroflyer", "spec": False},
    "ACE": {"icao": "GCRR", "lat": 28.945, "lon": -13.605, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "LPA": {"icao": "GCLP", "lat": 27.931, "lon": -15.386, "rwy": 30, "fleet": "Euroflyer", "spec": False},
    "FUE": {"icao": "GCLP", "lat": 28.452, "lon": -13.864, "rwy": 10, "fleet": "Euroflyer", "spec": False},
    "IVL": {"icao": "EFIV", "lat": 68.607, "lon": 27.405, "rwy": 40, "fleet": "Euroflyer", "spec": False},
    "PSA": {"icao": "LIRP", "lat": 43.683, "lon": 10.392, "rwy": 40, "fleet": "Alternate", "spec": False},
    "BLQ": {"icao": "LIPE", "lat": 44.535, "lon": 11.288, "rwy": 120, "fleet": "Alternate", "spec": False},
    "PSO": {"icao": "LPPS", "lat": 33.070, "lon": -16.341, "rwy": 180, "fleet": "Alternate", "spec": True},
    "MUC": {"icao": "EDDM", "lat": 48.353, "lon": 11.786, "rwy": 80, "fleet": "Alternate", "spec": False},
    "BCN": {"icao": "LEBL", "lat": 41.297, "lon": 2.083, "rwy": 70, "fleet": "Alternate", "spec": False},
}

preferred_alts = {"FLR": ["PSA", "BLQ"], "FNC": ["PSO"], "INN": ["MUC"], "PMI": ["BCN"], "IBZ": ["BCN"]}

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

# 7. SCHEDULED DATA FETCH
now = datetime.now()
refresh_block = 0 if now.minute < 30 else 30
sync_key = now.strftime('%Y%m%d%H') + str(refresh_block)

@st.cache_data(ttl=None)
def get_intel_global(airport_dict, schedule_key):
    res = {}
    for iata, info in airport_dict.items():
        try:
            m = Metar(info['icao']); m.update(); t = Taf(info['icao']); t.update()
            v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
            w_vis, w_cig, w_time, w_prob, w_issues = 9999, 9999, "", False, []
            if t.data:
                for line in t.data.forecast:
                    v = line.visibility.value if line.visibility else 9999
                    c, cloud_label = 9999, ""
                    if line.clouds:
                        for lyr in line.clouds:
                            if lyr.type in ['BKN', 'OVC'] and lyr.base:
                                val = lyr.base * 100
                                if val < c: c = val; cloud_label = f"{lyr.type}{int(lyr.base):03d}"
                    raw = line.raw
                    line_issues = []
                    if any(x in raw for x in ["FG", "TSRA", "FZRA", "FZDZ", "SN", "SHSN"]):
                        for phenom in ["FG", "TSRA", "FZRA", "FZDZ", "SN", "SHSN"]:
                            if phenom in raw: line_issues.append(phenom)
                    if v < v_lim: line_issues.append(f"VIS {int(v)}m")
                    if c < c_lim: line_issues.append(f"CLOUD {cloud_label}")
                    
                    if line_issues and (v < w_vis or c < w_cig or "FZ" in str(line_issues)):
                        w_vis, w_cig, w_issues, w_prob = v, c, line_issues, ("PROB" in raw)
                        w_time = f"{line.start_time.dt.strftime('%H')}-{line.end_time.dt.strftime('%H')}Z"
                        if "FZ" in str(line_issues): break
            res[iata] = {
                "vis": m.data.visibility.value if (m.data and m.data.visibility) else 9999,
                "cig": 9999, "cig_label": "", "status": "online", "raw_m": m.raw or "N/A", "raw_t": t.raw or "N/A",
                "f_issues": w_issues, "f_time": w_time, "f_prob": w_prob,
                "w_dir": m.data.wind_direction.value if (m.data and m.data.wind_direction) else 0,
                "w_spd": m.data.wind_speed.value if (m.data and m.data.wind_speed) else 0,
                "w_gst": m.data.wind_gust.value if (m.data and m.data.wind_gust) else 0,
            }
            if m.data and m.data.clouds:
                for lyr in m.data.clouds:
                    if lyr.type in ['BKN', 'OVC'] and lyr.base:
                        val = lyr.base * 100
                        if val < res[iata]["cig"]: res[iata]["cig"] = val; res[iata]["cig_label"] = f"{lyr.type}{int(lyr.base):03d}"
        except: res[iata] = {"status": "offline", "raw_m": "N/A", "raw_t": "N/A", "f_issues": []}
    return res

weather_data = get_intel_global(base_airports, sync_key)

# 8. FILTER & UI LOOP
metar_alerts, taf_alerts, green_stations, map_markers = {}, {}, [], []
for iata, info in base_airports.items():
    data = weather_data.get(iata)
    if not data: continue
    is_shown = (info['fleet'] == "Cityflyer" and show_cf) or (info['fleet'] == "Euroflyer" and show_ef)
    v_lim, c_lim = (1500, 500) if info['spec'] else (800, 200)
    color, m_issues, actual_str, forecast_str = "#008000", [], "STABLE", "NIL"
    xw = calculate_xwind(data.get('w_dir', 0), max(data.get('w_spd', 0), data.get('w_gst', 0)), info['rwy'])
    
    if data['status'] == "online":
        m_raw = data['raw_m']
        for p in ["FG", "TSRA", "FZRA", "SN", "SHSN"]:
            if p in m_raw: m_issues.append(p)
        if data['vis'] < v_lim: m_issues.append(f"VIS {int(data['vis'])}m")
        if data['cig'] < c_lim: m_issues.append(f"CLOUD {data['cig_label']}")
        if xw >= 25: m_issues.append("X-WIND")
        
        if is_shown or info['fleet'] == "Alternate":
            if m_issues: 
                color = "#d6001a" if any(x in str(m_issues) for x in ["VIS", "CLOUD", "FZRA"]) else "#eb8f34"
                actual_str = " & ".join(m_issues)
                btn_label = " / ".join([x.split(' ')[0] for x in m_issues])
                if info['fleet'] != "Alternate": metar_alerts[iata] = {"type": btn_label, "detail": actual_str, "hex": "primary" if color == "#d6001a" else "secondary"}
            elif info['fleet'] != "Alternate": green_stations.append(iata)
                
            if data.get('f_issues'):
                p_tag = " (PROB)" if data['f_prob'] else ""
                forecast_str = f"{' & '.join(data['f_issues'])}{p_tag} @ {data.get('f_time','')}"
                f_btn_label = " + ".join([x.split(' ')[0] for x in data['f_issues']])
                t_hex = "primary" if any(x in str(data['f_issues']) for x in ["VIS", "CLOUD", "FZRA"]) else "secondary"
                if info['fleet'] != "Alternate": taf_alerts[iata] = {"type": f_btn_label, "detail": forecast_str, "time": data.get('f_time',''), "prob": data.get('f_prob', False), "hex": t_hex}
                if color == "#008000": color = "#eb8f34"

    if is_shown:
        r1, r2 = int(info['rwy']/10), int(((info['rwy']+180)%360)/10)
        rwy_str = f"{min(r1,r2):02d}/{max(r1,r2):02d}"
        popup_html = f"""<div style="width:600px; color:black !important; font-family:sans-serif; font-size:16px; line-height:1.4;"><b style="color:#002366; font-size:20px;">{iata} STATUS</b><div style="margin-top:5px; padding:12px; border-left:8px solid {color}; background:#f4f4f4;"><b style="color:#002366; font-size:18px;">RWY {rwy_str} Live X-Wind:</b> <span style="color:{'#d6001a' if xw >= 25 else '#002366'}; font-weight:900;">{xw} KT</span><br><b>ACTUAL:</b> {actual_str}<br><b>FORECAST:</b> {forecast_str}</div><hr><div><b>METAR:</b><br>{bold_hazard(data['raw_m'])}<br><br><b>TAF:</b><br>{bold_hazard(data['raw_t'])}</div></div>"""
        map_markers.append({"iata": iata, "lat": info['lat'], "lon": info['lon'], "color": color, "popup": popup_html})

# --- UI RENDER ---
st.markdown(f'<div class="ba-header"><div>OCC WEATHER HUD</div><div>{datetime.now().strftime("%H:%M")} UTC</div></div>', unsafe_allow_html=True)
m = folium.Map(location=[45.0, 5.0], zoom_start=4, tiles=("CartoDB dark_matter" if map_theme == "Dark Mode" else "CartoDB positron"), scrollWheelZoom=False)
for mkr in map_markers:
    folium.CircleMarker(location=[mkr['lat'], mkr['lon']], radius=8, color=mkr['color'], fill=True, popup=folium.Popup(mkr['popup'], max_width=650)).add_to(m)
st_folium(m, width=1000, height=1000, key="map_v139")

# 10. ALERTS
st.markdown('<div class="section-header">🔴 Actual Alerts (METAR)</div>', unsafe_allow_html=True)
if metar_alerts:
    cols = st.columns(3)
    for i, (iata, d) in enumerate(metar_alerts.items()):
        with cols[i % 3]:
            if st.button(f"{iata} NOW {d['type']}", key=f"m_{iata}", type=d['hex']): st.session_state.investigate_iata = iata

st.markdown('<div class="section-header">🟠 Forecast Alerts (TAF)</div>', unsafe_allow_html=True)
if taf_alerts:
    cols_f = st.columns(3)
    for i, (iata, d) in enumerate(taf_alerts.items()):
        with cols_f[i % 3]:
            p_tag = " PROB" if d['prob'] else ""
            if st.button(f"{iata} {d['time']} {d['type']}{p_tag}", key=f"f_{iata}", type=d['hex']): st.session_state.investigate_iata = iata

# 11. ANALYSIS
if st.session_state.investigate_iata != "None":
    iata = st.session_state.investigate_iata
    d, info = weather_data.get(iata, {}), base_airports.get(iata, {"lat":0, "lon":0, "rwy":0})
    issue_desc = (taf_alerts.get(iata, {}) or metar_alerts.get(iata, {}) or {}).get('detail', "STABLE")
    xw_val = calculate_xwind(d.get('w_dir', 0), max(d.get('w_spd', 0), d.get('w_gst', 0)), info.get('rwy'))
    
    impact = "Standard operations."
    if any(x in issue_desc for x in ["VIS", "CLOUD"]): impact = "LVP likely. CAT III currency req."
    elif "FZ" in issue_desc: impact = "Embraer fleet restricted."
    elif "X-WIND" in issue_desc: impact = "Critical crosswind (>=25kt)."

    alt_iata, alt_note = "None", ""
    network_targets = preferred_alts.get(iata, [])
    found_network = False
    for target in network_targets:
        t_data = weather_data.get(target)
        if t_data and not any(x in str(t_data.get('f_issues', [])) for x in ["VIS", "CLOUD", "FZRA"]):
            alt_iata = target; alt_note = f"(Preferred BAW Alternate)"; found_network = True; break
    if not found_network:
        min_dist = 9999
        for g in green_stations:
            if g != iata:
                g_info = base_airports.get(g, {"lat":0,"lon":0})
                dist = calculate_dist(info['lat'], info['lon'], g_info['lat'], g_info['lon'])
                if dist < min_dist: min_dist = dist; alt_iata = g
        alt_note = f"({min_dist} NM)" if alt_iata != "None" else ""

    brief_copy = f"{iata} STRATEGY: {issue_desc}\nSummary: Live crosswind {xw_val}kt for RWY {info.get('rwy')}°.\nImpact: {impact}\nAlternate: {alt_iata} {alt_note}."
    encoded_brief = urllib.parse.quote(brief_copy)
    
    st.markdown(f"""
        <div class="reason-box">
            <h3>{iata} Strategy Brief: {issue_desc} <span class="copy-btn" onclick="tacticalCopy('{encoded_brief}')">📋</span></h3>
            <p><b>WX Summary:</b> Live crosswind <b>{xw_val}kt</b> for RWY {info.get('rwy')}°.</p>
            <p><b>Impact:</b> {impact}</p>
            <p style="color:#d6001a !important; font-size:1.1rem;"><b>✈️ Strategic Alternate:</b> {alt_iata} {alt_note}</p>
            <hr><div style="display:flex; gap:20px;">
                <div style="flex:1;"><b>METAR:</b><br><small>{bold_hazard(d.get('raw_m'))}</small></div>
                <div style="flex:1;"><b>TAF:</b><br><small>{bold_hazard(d.get('raw_t'))}</small></div>
            </div>
        </div>""", unsafe_allow_html=True)
    if st.button("Close Analysis"): st.session_state.investigate_iata = "None"; st.rerun()

# 12. HANDOVER
h_txt_clean = f"HANDOVER {datetime.now().strftime('%H:%M')}Z\n" + "="*35 + "\n"
for i_ata, d_taf in taf_alerts.items(): h_txt_clean += f"{i_ata}: {d_taf['detail']}\n"
encoded_handover = urllib.parse.quote(h_txt_clean)

st.markdown(f'<div class="section-header">📝 Shift Handover Log <span class="copy-btn" onclick="tacticalCopy(\'{encoded_handover}\')">📋</span></div>', unsafe_allow_html=True)
st.text_area("Live Report:", value=h_txt_clean, height=200, label_visibility="collapsed")
