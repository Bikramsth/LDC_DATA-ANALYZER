import os
import re
import time
import sqlite3
import threading
import io
import datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ==========================================
# 1. CONFIGURATION & DATABASE SETUP
# ==========================================
FOLDER_TO_WATCH = "./LDC_Data"
DATABASE_NAME = "NEA_LDC_Database.db"

# Custom NEA Color Palette for Visual Clarity
NEA_PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']

@st.cache_resource
def init_db():
    """Initializes the database and auto-cleans old data. Cached so it only runs ONCE per session."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
            cursor = conn.cursor()
            cursor.execute('PRAGMA journal_mode=WAL;')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_log_data (
                    nepali_year INTEGER,
                    nepali_month INTEGER,
                    nepali_day INTEGER,
                    time_interval TEXT,
                    parameter_name TEXT,
                    value REAL,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (nepali_year, nepali_month, nepali_day, time_interval, parameter_name)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_files (
                    filename TEXT UNIQUE,
                    mtime REAL,
                    processed_date DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_sys_date_param ON system_log_data(nepali_year, nepali_month, nepali_day, parameter_name);')

            # 🚀 AUTO-CLEANER: Fixes old duplicated data and standardizes Interruption
            updates = {
                "Total IPP": "SUMMARY_TOTAL_IPP",
                "TOTAL IPP": "SUMMARY_TOTAL_IPP",
                "Total NEA SUBSIDIARIES": "SUMMARY_TOTAL_NEA_SUBS",
                "TOTAL NEA SUBSIDIARIES": "SUMMARY_TOTAL_NEA_SUBS",
                "Total ROR": "SUMMARY_TOTAL_ROR",
                "TOTAL ROR": "SUMMARY_TOTAL_ROR",
                "Total STORGE": "SUMMARY_TOTAL_STORAGE",
                "Total Storage": "SUMMARY_TOTAL_STORAGE",
                "TOTAL STORAGE": "SUMMARY_TOTAL_STORAGE",
                "Total IMPORT": "SUMMARY_TOTAL_IMPORT",
                "TOTAL IMPORT": "SUMMARY_TOTAL_IMPORT",
                "Total EXPORT": "SUMMARY_TOTAL_EXPORT",
                "TOTAL EXPORT": "SUMMARY_TOTAL_EXPORT",
                "Total Interruption": "SUMMARY_TOTAL_INTERRUPTION",
                "TOTAL INTERRUPTION": "SUMMARY_TOTAL_INTERRUPTION",
                "INTERRUPTION": "SUMMARY_TOTAL_INTERRUPTION",
                "Interruption": "SUMMARY_TOTAL_INTERRUPTION",
                "Interruption/Tripping": "SUMMARY_TOTAL_INTERRUPTION",
                "INTERRUPTION/TRIPPING": "SUMMARY_TOTAL_INTERRUPTION",
                "Interruption / Tripping": "SUMMARY_TOTAL_INTERRUPTION",
                "ZONE_EXPORT_Interruption/Tripping": "SUMMARY_TOTAL_INTERRUPTION",
                "ZONE_EXPORT_INTERRUPTION/TRIPPING": "SUMMARY_TOTAL_INTERRUPTION"
            }
            for old_name, new_name in updates.items():
                cursor.execute("UPDATE OR IGNORE system_log_data SET parameter_name = ? WHERE parameter_name = ?",
                               (new_name, old_name))
                cursor.execute("DELETE FROM system_log_data WHERE parameter_name = ?", (old_name,))
                
            # 🚀 FIX: Convert all historical negative Export values to positive magnitudes!
            cursor.execute("UPDATE system_log_data SET value = ABS(value)")

            conn.commit()
            conn.close()
            return True

        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                time.sleep(1)
            else:
                raise e


@st.cache_data(ttl=60)
def run_query(query, params=()):
    conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

# ==========================================
# 2. DATA EXTRACTION ENGINE
# ==========================================
def parse_filename(filename):
    year, month = None, None
    year_match = re.search(r'\b(20[7-9]\d)\b', filename)
    if year_match:
        year = int(year_match.group(1))
        m_after = re.search(rf'{year}[-_.\s]+(1[0-2]|0?[1-9])\b', filename)
        m_before = re.search(rf'\b(1[0-2]|0?[1-9])[-_.\s]+{year}', filename)

        if m_after:
            month = int(m_after.group(1))
        elif m_before:
            month = int(m_before.group(1))
        else:
            months = re.findall(r'\b(1[0-2]|0?[1-9])\b', filename)
            for m in months:
                if int(m) != year:
                    month = int(m)
                    break
    return year, month


def extract_data(df, year, month, day, cursor):
    rows_inserted = 0
    header_row_index = -1
    time_columns = {}
    current_block = "IPP_ZONE"

    for idx, row in df.iterrows():
        current_time_cols = {}
        for col_idx in range(1, len(row)):
            val = row[col_idx]
            if pd.isna(val): continue
            str_val = str(val).strip()
            match = re.search(r'\b(\d{1,2}):(\d{2})', str_val)
            if match:
                current_time_cols[col_idx] = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}:00"
            else:
                try:
                    num = float(val)
                    if 0 < num <= 24: current_time_cols[
                        col_idx] = f"{int(num):02d}:{int(round((num - int(num)) * 60)):02d}:00"
                except:
                    pass
        if len(current_time_cols) >= 20:
            time_columns = current_time_cols
            header_row_index = idx
            break

    if header_row_index == -1: return 0

    for row_idx in range(header_row_index + 1, len(df)):
        row = df.iloc[row_idx]
        if pd.isna(row[0]) or str(row[0]).strip() == "": continue

        raw_name = str(row[0]).strip()
        upper_name = re.sub(r'\s+', ' ', raw_name.upper())

        # 🚀 STRICT NORMALIZATION: Forces uniform parameter names
        if "TOTAL IPP" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_IPP", "NEA_SUB_ZONE"
        elif "TOTAL NEA SUB" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_NEA_SUBS", "ROR_ZONE"
        elif "TOTAL ROR" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_ROR", "STORAGE_ZONE"
        elif "TOTAL STORGE" in upper_name or "TOTAL STORAGE" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_STORAGE", "IMPORT_ZONE"
        elif "TOTAL IMPORT" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_IMPORT", "AFTER_IMPORT"
        elif "TOTAL NATIONAL LOAD" in upper_name or ("NATIONAL LOAD" in upper_name and "TOTAL" not in upper_name):
            db_param_name, current_block = "TOTAL NATIONAL LOAD", "EXPORT_ZONE"
        elif "TOTAL EXPORT" in upper_name:
            db_param_name, current_block = "SUMMARY_TOTAL_EXPORT", "FINAL_TOTALS"
        elif "SYSTEM LOAD" in upper_name:
            db_param_name, current_block = "TOTAL SYSTEM LOAD (ACTUAL)", "FINAL_TOTALS"
        elif "INTERRUPT" in upper_name or "TRIP" in upper_name: 
            db_param_name, current_block = "SUMMARY_TOTAL_INTERRUPTION", "FINAL_TOTALS"
        else:
            prefixes = {"IPP_ZONE": "ZONE_IPP_", "NEA_SUB_ZONE": "ZONE_NEASUB_", "ROR_ZONE": "ZONE_ROR_",
                        "STORAGE_ZONE": "ZONE_STORAGE_", "IMPORT_ZONE": "ZONE_IMPORT_", "EXPORT_ZONE": "ZONE_EXPORT_"}
            db_param_name = f"{prefixes.get(current_block, '')}{raw_name}"

        for col_idx, time_interval in time_columns.items():
            cell_value = row[col_idx] if col_idx < len(row) else None
            try:
                clean_val = str(cell_value).replace(',', '').strip()
                if clean_val in ['', '-']:
                    final_value = 0.0
                else:
                    # 🚀 FIX: EVERYTHING is now stored strictly as a positive magnitude!
                    final_value = abs(float(clean_val)) 
            except:
                final_value = 0.0

            cursor.execute('''
                INSERT INTO system_log_data (nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value)
                VALUES (?, ?, ?, ?, ?, ?) 
                ON CONFLICT(nepali_year, nepali_month, nepali_day, time_interval, parameter_name) 
                DO UPDATE SET value = excluded.value
            ''', (year, month, day, time_interval, db_param_name, final_value))
            rows_inserted += 1

    return rows_inserted


def process_file(file_path):
    time.sleep(1)
    filename = os.path.basename(file_path)
    if not filename.lower().endswith(('.xlsx', '.xls', '.csv')) or filename.startswith('~'):
        return "ERROR", f"Skipped `{filename}`: Unsupported format"

    year, month = parse_filename(filename)
    if not year or not month: return "ERROR", f"⚠️ Missing Year/Month in `{filename}`."

    try:
        mtime = os.path.getmtime(file_path)
        conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
        cursor = conn.cursor()

        if cursor.execute("SELECT mtime FROM processed_files WHERE filename = ?", (file_path,)).fetchone() == (mtime,):
            conn.close()
            return "SKIPPED", f"Skipped `{filename}`"

        file_bytes = None
        for _ in range(3):
            try:
                with open(file_path, 'rb') as f:
                    file_bytes = f.read()
                break
            except PermissionError:
                time.sleep(1)

        if not file_bytes: return "ERROR", f"`{filename}` locked by Excel."

        file_io, rows_inserted = io.BytesIO(file_bytes), 0

        if filename.lower().endswith('.csv'):
            day_match = re.search(r'-\s*(\d+)\.csv$', filename.lower())
            day = int(day_match.group(1)) if day_match else None
            if day is not None and day > 0:  # Strict Day 0 check
                rows_inserted += extract_data(pd.read_csv(file_io, header=None, on_bad_lines='skip'), year, month, day,
                                              cursor)
        else:
            xl = pd.ExcelFile(file_io, engine='openpyxl' if filename.endswith('.xlsx') else None)
            for sheet in xl.sheet_names:
                if sheet.strip().isdigit():
                    day = int(sheet.strip())
                    if day > 0:  # Strict Day 0 check
                        rows_inserted += extract_data(xl.parse(sheet, header=None), year, month, day, cursor)

        if rows_inserted > 0:
            cursor.execute(
                "INSERT INTO processed_files (filename, mtime) VALUES (?, ?) ON CONFLICT(filename) DO UPDATE SET mtime=excluded.mtime",
                (file_path, mtime))
            conn.commit()
            conn.close()
            try:
                st.cache_data.clear()
            except Exception:
                pass
            return "SUCCESS", f"✅ Added `{filename}`."
        
        conn.close()
        return "ERROR", "No valid day sheets found."
    except Exception as e:
        return "ERROR", str(e)


# ==========================================
# 3. BACKGROUND FOLDER MONITOR
# ==========================================
class FileWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')):
            time.sleep(2)
            process_file(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            time.sleep(2)
            for root, dirs, files in os.walk(event.src_path):
                for f in files:
                    if f.lower().endswith(('.xlsx', '.xls', '.csv')) and not f.startswith('~'):
                        process_file(os.path.join(root, f))
        elif event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')):
            time.sleep(2)
            process_file(event.src_path)


@st.cache_resource
def start_background_monitor():
    if not os.path.exists(FOLDER_TO_WATCH):
        os.makedirs(FOLDER_TO_WATCH)
    event_handler = FileWatcher()
    observer = Observer()
    observer.schedule(event_handler, FOLDER_TO_WATCH, recursive=True)
    monitor_thread = threading.Thread(target=observer.start, daemon=True)
    monitor_thread.start()
    return observer


init_db()
observer = start_background_monitor()


# ==========================================
# 4. UI HELPER FUNCTIONS
# ==========================================
def categorize_params(param_list):
    categories = {}
    for p in param_list:
        p_up = p.upper()
        if p_up.startswith('ZONE_IPP_'):
            cat = "🔌 IPPs"
        elif p_up.startswith('ZONE_ROR_'):
            cat = "🌊 RORs"
        elif p_up.startswith('ZONE_STORAGE_'):
            cat = "🔋 Storage"
        elif p_up.startswith('ZONE_NEASUB_'):
            cat = "🏢 NEA Subs"
        elif p_up.startswith('ZONE_IMPORT_'):
            cat = "⬇️ Import"
        elif p_up.startswith('ZONE_EXPORT_'):
            cat = "⬆️ Export"
        elif 'SUMMARY' in p_up or 'TOTAL' in p_up or 'LOAD' in p_up or 'INTERRUPTION' in p_up:
            cat = "📊 System Totals"
        else:
            cat = "📁 Other"
        categories.setdefault(cat, []).append(p)
    return categories


def clean_param_name(p):
    return p.replace('SUMMARY_TOTAL_IPP', 'Total IPP').replace('SUMMARY_TOTAL_NEA_SUBS',
                                                               'Total NEA Subsidiaries').replace('SUMMARY_TOTAL_ROR',
                                                                                                 'Total ROR').replace(
        'SUMMARY_TOTAL_STORAGE', 'Total Storage').replace('SUMMARY_TOTAL_IMPORT', 'Total IMPORT').replace(
        'SUMMARY_TOTAL_EXPORT', 'Total EXPORT').replace('SUMMARY_TOTAL_INTERRUPTION', 'Interruption / Tripping').replace('ZONE_IPP_', 'IPP: ').replace('ZONE_ROR_', 'ROR: ').replace(
        'ZONE_STORAGE_', 'STORAGE: ').replace('ZONE_NEASUB_', 'SUB: ').replace('ZONE_IMPORT_', 'IMPORT: ').replace(
        'ZONE_EXPORT_', 'EXPORT: ')


@st.cache_data(show_spinner=False)
def convert_df(df): return df.to_csv(index=False).encode('utf-8')

def update_chart_layout(fig, title, yaxis_title="Power (MW)", xaxis_title="Time", legend_orientation="v"):
    legend_settings = dict(orientation="v", y=1, x=1.02) if legend_orientation == "v" else dict(orientation="h", y=-0.25, x=0, yanchor="top")
    right_margin = 150 if legend_orientation == "v" else 10

    fig.update_layout(
        title=title, hovermode="x unified", template="plotly_white",
        plot_bgcolor='rgba(255,255,255,1)', paper_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(title=xaxis_title, type="category", showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec',
                   tickangle=-45, automargin=True),
        yaxis=dict(title=yaxis_title, showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec', automargin=True),
        legend=legend_settings, margin=dict(l=10, r=right_margin, t=50, b=60)
    )
    return fig


def draw_stacked_chart(df_pivot, cols, title, total_col=None):
    fig = go.Figure()
    for i, col in enumerate(cols):
        fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[col], name=clean_param_name(col), stackgroup='one',
                                 line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)])))
    if total_col and total_col in df_pivot.columns:
        fig.add_trace(
            go.Scatter(x=df_pivot.index, y=df_pivot[total_col], name='TOTAL', line=dict(color='#1e1e1e', width=3)))

    fig = update_chart_layout(fig, title, legend_orientation="v")
    st.plotly_chart(fig, use_container_width=True)


# ==========================================
# 5. STREAMLIT APP & UI STYLING
# ==========================================
st.set_page_config(page_title="NEA LDC Dashboard", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
        .stApp { background-color: #f4f7f6; color: #1e1e1e; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        div[data-testid="metric-container"] {
            background-color: #ffffff; border: 1px solid #e0e5ec; padding: 15px 20px;
            border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
            border-left: 5px solid #0052cc; transition: transform 0.2s ease;
        }
        div[data-testid="metric-container"]:hover { transform: translateY(-2px); box-shadow: 0 6px 12px rgba(0, 0, 0, 0.1); }
        div[data-testid="stMetricLabel"] { font-weight: 600; color: #5e6e82; }
        div[data-testid="stMetricValue"] { color: #0f172a; font-weight: 700; font-size: 2rem !important; }
        .stTabs [data-baseweb="tab-list"] { gap: 15px; }
        .stTabs [data-baseweb="tab"] { 
            height: 50px; background-color: #ffffff; border-radius: 8px 8px 0px 0px; 
            padding: 10px 20px; box-shadow: 0 -2px 5px rgba(0,0,0,0.02); 
            border: 1px solid #e0e5ec; border-bottom: none;
        }
        .stTabs [aria-selected="true"] { background-color: #e8f0fe; color: #0052cc !important; border-bottom: 3px solid #0052cc !important; font-weight: bold; }
        [data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e5ec; }
        .streamlit-expanderHeader { background-color: #ffffff; border-radius: 5px; border: 1px solid #e0e5ec; }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ Nepal Electricity Authority - LDC Dashboard")

years_df = run_query("SELECT DISTINCT nepali_year FROM system_log_data ORDER BY nepali_year DESC")

if years_df.empty:
    st.warning("Database is currently empty.")
    st.info("Place your Log Excel files into the `./LDC_Data` folder. Sub-folders are supported!")
    if st.button("🚀 Force Manual Scan", use_container_width=True):
        with st.status("Scanning Folder and Sub-Folders...", expanded=True) as status_box:
            for root, dirs, files in os.walk(FOLDER_TO_WATCH):
                for f in files:
                    if f.endswith(('.xlsx', '.xls', '.csv')) and not f.startswith('~'):
                        process_file(os.path.join(root, f))
            status_box.update(label="Scan Complete!", state="complete", expanded=False)
        time.sleep(2)
        st.cache_data.clear()
        st.rerun()
    st.stop()

# ==========================================
# 6. SIDEBAR CONTROLS
# ==========================================
with st.sidebar:
    if st.button("🚀 Force Rescan Files", use_container_width=True):
        with st.status("Scanning all Sub-folders..."):
            for root, dirs, files in os.walk(FOLDER_TO_WATCH):
                for f in files:
                    if f.endswith(('.xlsx', '.xls', '.csv')) and not f.startswith('~'):
                        process_file(os.path.join(root, f))
        st.cache_data.clear() 
        st.rerun()

    st.header("📅 Primary Date Filter")
    selected_year = st.selectbox("Year", years_df['nepali_year'].tolist())
    months_df = run_query(
        "SELECT DISTINCT nepali_month FROM system_log_data WHERE nepali_year = ? ORDER BY nepali_month",
        (selected_year,))
    selected_month = st.selectbox("Month", months_df['nepali_month'].tolist())

    days_df = run_query(
        "SELECT DISTINCT nepali_day FROM system_log_data WHERE nepali_year = ? AND nepali_month = ? AND nepali_day > 0 ORDER BY nepali_day",
        (selected_year, selected_month))

    if not days_df.empty:
        selected_day = st.selectbox("Day", days_df['nepali_day'].tolist())
        sidebar_date_str = f"{selected_year}/{str(selected_month).zfill(2)}/{str(selected_day).zfill(2)}"
    else:
        selected_day = None
        sidebar_date_str = "No valid days found"
        st.warning("No valid day data for this month.")

    st.divider()
    st.header("📊 Comparison Studio")

    compare_mode = st.radio("Strategy:", [
        "🗓️ Date-wise",
        "📈 Parameters",
        "📅 Daily Peak/Load Across Months",
        "📊 Monthly Peak/Load"
    ])

    comp_param, selected_comp_days, baseline_param, selected_comp_params, selected_comp_months = None, [], None, [], []
    all_p_df = run_query("SELECT DISTINCT parameter_name FROM system_log_data ORDER BY parameter_name")

    if not all_p_df.empty:
        cat_dict = categorize_params(all_p_df['parameter_name'].tolist())
        sel_cats = st.multiselect("Categories", sorted(list(cat_dict.keys())),
                                  default=[k for k in cat_dict.keys() if "Totals" in k] or sorted(
                                      list(cat_dict.keys()))[:1])
        if sel_cats:
            filt_p = sorted([p for c in sel_cats for p in cat_dict[c]])

            if compare_mode in ["🗓️ Date-wise", "📅 Daily Peak/Load Across Months", "📊 Monthly Peak/Load"]:
                comp_param = st.selectbox("Parameter", filt_p, format_func=clean_param_name)
                if compare_mode == "🗓️ Date-wise":
                    d_df = run_query(
                        "SELECT DISTINCT nepali_year, nepali_month, nepali_day FROM system_log_data WHERE parameter_name=? AND nepali_day > 0",
                        (comp_param,))
                    d_list = [f"{y}/{str(m).zfill(2)}/{str(d).zfill(2)}" for y, m, d in
                              zip(d_df.nepali_year, d_df.nepali_month, d_df.nepali_day) if
                              f"{y}/{str(m).zfill(2)}/{str(d).zfill(2)}" != sidebar_date_str]
                    selected_comp_days = st.multiselect("Overlay Dates", d_list)
                else:
                    ym_df = run_query(
                        "SELECT DISTINCT nepali_year, nepali_month FROM system_log_data WHERE parameter_name=? AND nepali_day > 0",
                        (comp_param,))
                    ym_list = [f"{y}/{str(m).zfill(2)}" for y, m in zip(ym_df.nepali_year, ym_df.nepali_month)]
                    selected_comp_months = st.multiselect("Compare Months", ym_list, default=ym_list[:2])
            elif compare_mode == "📈 Parameters":
                baseline_param = st.selectbox("Baseline Param", filt_p, format_func=clean_param_name)
                selected_comp_params = st.multiselect("Overlay Params", [p for p in filt_p if p != baseline_param],
                                                      format_func=clean_param_name)

# ==========================================
# 7. MAIN UI TABS
# ==========================================
t1, t2, t3, t4 = st.tabs([
    "⚡ Daily Operations",
    "📆 Monthly Peak Load Graph",
    "📈 Historical Monthly Peak Load",
    "🔍 Comparison Studio"
])

with t1:
    # 🚀 EXPLICIT TABS: Added "Total EXPORT" physically to the UI
    st1, st2, st3, st4, st5, st6, st7 = st.tabs([
        "Main System", "Total IMPORT", "Total EXPORT", "Total NEA SUBSIDIARIES", "Total IPP", "Total ROR", "Total STORAGE"
    ])

    if selected_day:
        df_all = run_query(
            "SELECT time_interval as Time, parameter_name, value as MW FROM system_log_data WHERE nepali_year=? AND nepali_month=? AND nepali_day=? ORDER BY time_interval ASC",
            (selected_year, selected_month, selected_day))

        if not df_all.empty:
            with st1:
                all_db_params = df_all['parameter_name'].unique()

                raw_supply_components = [p for p in all_db_params if
                                         p in ['SUMMARY_TOTAL_IPP', 'SUMMARY_TOTAL_NEA_SUBS', 'SUMMARY_TOTAL_ROR',
                                               'SUMMARY_TOTAL_STORAGE']]

                if not raw_supply_components:
                    raw_supply_components = [
                        p for p in all_db_params if (
                                                        'TOTAL' in p.upper() or 'INTERRUPTION' in p.upper() or 'ROR' in p.upper() or 'STORGE' in p.upper() or 'STORAGE' in p.upper())
                                                    and 'IMPORT' not in p.upper() and 'EXPORT' not in p.upper() and 'LOAD' not in p.upper() and 'OTHER IPP' not in p.upper() and 'ZONE_' not in p.upper()
                    ]

                import_tag, export_tag, int_tag = 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT', 'SUMMARY_TOTAL_INTERRUPTION'
                req_p = raw_supply_components.copy()
                if import_tag in all_db_params: req_p.append(import_tag)
                if export_tag in all_db_params: req_p.append(export_tag)
                if int_tag in all_db_params: req_p.append(int_tag)

                df_piv = df_all[df_all['parameter_name'].isin(req_p)].pivot(index='Time', columns='parameter_name',
                                                                            values='MW').fillna(0.0).sort_index()

                val_comps = [col for col in raw_supply_components if
                             col in df_piv.columns and (df_piv[col].abs().sum() > 0 or 'STORAGE' in col.upper())]
                if import_tag in df_piv.columns and import_tag not in val_comps: val_comps.append(import_tag)

                if export_tag not in df_piv.columns: df_piv[export_tag] = 0.0
                if import_tag not in df_piv.columns: df_piv[import_tag] = 0.0
                if int_tag not in df_piv.columns: df_piv[int_tag] = 0.0

                df_piv[import_tag] -= df_piv[export_tag].abs()

                df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] = df_piv[val_comps].sum(axis=1)
                df_piv['TOTAL SYSTEM LOAD (EXPECTED)'] = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] + df_piv[int_tag]
                df_piv['TOTAL NATIONAL LOAD (ACTUAL)'] = df_piv['TOTAL SYSTEM LOAD (EXPECTED)'] + df_piv[export_tag].abs()

                sys_act_peak_hour, sys_act_peak_val = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].idxmax(), df_piv[
                    'TOTAL SYSTEM LOAD (ACTUAL)'].max()
                sys_exp_peak_hour, sys_exp_peak_val = df_piv['TOTAL SYSTEM LOAD (EXPECTED)'].idxmax(), df_piv[
                    'TOTAL SYSTEM LOAD (EXPECTED)'].max()
                nat_act_peak_hour, nat_act_peak_val = df_piv['TOTAL NATIONAL LOAD (ACTUAL)'].idxmax(), df_piv[
                    'TOTAL NATIONAL LOAD (ACTUAL)'].max()

                total_energy_mwh, load_factor = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].sum(), (
                            df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].mean() / sys_act_peak_val * 100) if sys_act_peak_val > 0 else 0

                st.markdown("<br>", unsafe_allow_html=True)
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric(f"📈 Sys Load Actual ({sys_act_peak_hour})", f"{sys_act_peak_val:.2f} MW")
                m2.metric(f"⚠️ Sys Load Expected ({sys_exp_peak_hour})", f"{sys_exp_peak_val:.2f} MW")
                m3.metric(f"📊 Nat Load Actual ({nat_act_peak_hour})", f"{nat_act_peak_val:.2f} MW")
                m4.metric(f"⚡ Total Energy", f"{total_energy_mwh:,.0f} MWh")
                m5.metric(f"⚙️ Load Factor", f"{load_factor:.1f}%")
                st.markdown("<br>", unsafe_allow_html=True)

                fig_main = go.Figure()
                NEA_PALETTE_MAIN = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2',
                                    '#7f7f7f', '#bcbd22', '#17becf']

                for i, col in enumerate(val_comps):
                    fig_main.add_trace(go.Scatter(
                        x=df_piv.index, y=df_piv[col], name=clean_param_name(col),
                        stackgroup='supply_stack',
                        line=dict(width=0.5, color=NEA_PALETTE_MAIN[i % len(NEA_PALETTE_MAIN)]),
                        hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                    ))

                # 🚀 FIX: Plot export downwards, but explicitly use `customdata` to show the POSITIVE magnitude on hover!
                if df_piv[export_tag].sum() > 0:
                    fig_main.add_trace(go.Scatter(
                        x=df_piv.index, 
                        y=-df_piv[export_tag], 
                        name='Total EXPORT',
                        stackgroup='export_stack', 
                        line=dict(width=0.5, color='#d62728'),
                        customdata=df_piv[export_tag],
                        hovertemplate='<b>%{fullData.name}</b>: %{customdata:.2f} MW<extra></extra>'
                    ))

                fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['TOTAL SYSTEM LOAD (ACTUAL)'], name='TOTAL SYSTEM LOAD (ACTUAL)',
                                              line=dict(color='#ff3366', width=2.5, dash='dot')))
                fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['TOTAL SYSTEM LOAD (EXPECTED)'], name='TOTAL SYSTEM LOAD (EXPECTED)',
                                              line=dict(color='#8c564b', width=2.5, dash='dashdot')))
                fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['TOTAL NATIONAL LOAD (ACTUAL)'], name='TOTAL NATIONAL LOAD (ACTUAL)',
                                              line=dict(color='#1e1e1e', width=3)))

                if df_piv[int_tag].sum() > 0:
                    fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv[int_tag], name='Interruption / Tripping',
                                                  line=dict(color='#ff7f0e', width=2)))

                fig_main = update_chart_layout(fig_main, "System Operation & Load Curve", legend_orientation="v")
                fig_main.add_hline(y=0, line_width=2, line_color="#1e1e1e")
                st.plotly_chart(fig_main, use_container_width=True)

                with st.expander("🔍 View Filtered Supply Components & Loads"):
                    display_cols = ['TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL SYSTEM LOAD (EXPECTED)', 'TOTAL NATIONAL LOAD (ACTUAL)'] + val_comps + [export_tag, int_tag]
                    st.dataframe(df_piv[display_cols].style.format("{:.2f}"), use_container_width=True)


            def render_sub(prefix, title):
                df_sub = df_all[df_all['parameter_name'].str.startswith(prefix)].copy()
                if not df_sub.empty:
                    df_sub['parameter_name'] = df_sub['parameter_name'].str.replace(prefix, '')
                    df_p = df_sub.pivot(index='Time', columns='parameter_name', values='MW').sort_index().interpolate(
                        method='linear').fillna(0.0)
                    v_cols = [c for c in df_p.columns if df_p[c].abs().sum() > 0]
                    df_p['TOTAL'] = df_p[v_cols].sum(axis=1)
                    draw_stacked_chart(df_p, v_cols, title, 'TOTAL')
                    with st.expander(f"🔍 View & Export {title} Data"):
                        st.dataframe(df_p[v_cols + ['TOTAL']].style.format("{:.2f}"), use_container_width=True)
                        st.download_button("📥 Download CSV", data=convert_df(df_p[v_cols + ['TOTAL']].reset_index()),
                                           file_name=f"{prefix}_{sidebar_date_str.replace('/', '-')}.csv",
                                           mime="text/csv")
                else:
                    st.info(f"No {title} data found.")


            with st2:
                render_sub('ZONE_IMPORT_', 'Dynamic Breakdown: Import')
            with st3:
                render_sub('ZONE_EXPORT_', 'Dynamic Breakdown: Export') # 🚀 Explicit Export Tab mapped correctly
            with st4:
                render_sub('ZONE_NEASUB_', 'Dynamic Breakdown: NEA Subsidiaries')
            with st5:
                render_sub('ZONE_IPP_', 'Dynamic Breakdown: Independent Power Producers (IPP)')
            with st6:
                render_sub('ZONE_ROR_', 'Dynamic Breakdown: Run of River (ROR) Plants')
            with st7:
                render_sub('ZONE_STORAGE_', 'Storage Plants')

with t2:
    st.subheader(f"📊 Daily Peak Loads for Month {selected_month}, {selected_year}")
    query_monthly = "SELECT nepali_day, time_interval, parameter_name, value FROM system_log_data WHERE nepali_year = ? AND nepali_month = ? AND nepali_day > 0 AND parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'SUMMARY_TOTAL_EXPORT')"
    df_raw = run_query(query_monthly, (selected_year, selected_month))

    if not df_raw.empty:
        df_piv = df_raw.pivot_table(index=['nepali_day', 'time_interval'], columns='parameter_name',
                                    values='value').fillna(0.0).reset_index()
        if 'TOTAL SYSTEM LOAD (ACTUAL)' not in df_piv.columns: df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] = 0.0
        if 'SUMMARY_TOTAL_EXPORT' not in df_piv.columns: df_piv['SUMMARY_TOTAL_EXPORT'] = 0.0
        df_piv['Sys Peak'] = df_piv['TOTAL SYSTEM LOAD (ACTUAL)']
        
        # SUMMARY_TOTAL_EXPORT is now purely positive, so we just add it to system load!
        df_piv['Nat Peak'] = df_piv['Sys Peak'] + df_piv['SUMMARY_TOTAL_EXPORT']

        sys_d = df_piv.loc[
            df_piv.groupby('nepali_day')['Sys Peak'].idxmax(), ['nepali_day', 'time_interval', 'Sys Peak']].rename(
            columns={'Sys Peak': 'Peak_MW'}).assign(**{'Load Type': 'System Peak Load'})
        nat_d = df_piv.loc[
            df_piv.groupby('nepali_day')['Nat Peak'].idxmax(), ['nepali_day', 'time_interval', 'Nat Peak']].rename(
            columns={'Nat Peak': 'Peak_MW'}).assign(**{'Load Type': 'National Peak Load'})
        df_comb = pd.concat([sys_d, nat_d]).sort_values('nepali_day')

        fig_m = px.line(df_comb, x='nepali_day', y='Peak_MW', color='Load Type', custom_data=['time_interval'],
                        markers=True, color_discrete_sequence=['#ff3366', '#0052cc'])
        fig_m.update_traces(
            hovertemplate='<b>%{fullData.name}</b><br>Day %{x}<br>Peak Load: %{y:.2f} MW<br>Time: %{customdata[0]}<extra></extra>')
        fig_m = update_chart_layout(fig_m, "System vs National Peak Load Each Day", xaxis_title="Day of Month",
                                    legend_orientation="v")
        fig_m.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1, range=[0.8, 32.2]))
        st.plotly_chart(fig_m, use_container_width=True)

with t3:
    st.subheader("📈 Historical Trend: Maximum Load per Month")
    df_raw = run_query(
        "SELECT nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value FROM system_log_data WHERE parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'SUMMARY_TOTAL_EXPORT') AND nepali_day > 0")

    if not df_raw.empty:
        df_piv = df_raw.pivot_table(index=['nepali_year', 'nepali_month', 'nepali_day', 'time_interval'],
                                    columns='parameter_name', values='value').fillna(0.0).reset_index()
        if 'TOTAL SYSTEM LOAD (ACTUAL)' not in df_piv.columns: df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] = 0.0
        if 'SUMMARY_TOTAL_EXPORT' not in df_piv.columns: df_piv['SUMMARY_TOTAL_EXPORT'] = 0.0
        df_piv['Sys'] = df_piv['TOTAL SYSTEM LOAD (ACTUAL)']
        
        # SUMMARY_TOTAL_EXPORT is now positive
        df_piv['Nat'] = df_piv['Sys'] + df_piv['SUMMARY_TOTAL_EXPORT']

        s_d = df_piv.loc[
            df_piv.groupby(['nepali_year', 'nepali_month'])['Sys'].idxmax(), ['nepali_year', 'nepali_month',
                                                                              'nepali_day', 'time_interval',
                                                                              'Sys']].rename(
            columns={'Sys': 'Peak_MW'}).assign(**{'Load Type': 'System Peak Load'})
        n_d = df_piv.loc[
            df_piv.groupby(['nepali_year', 'nepali_month'])['Nat'].idxmax(), ['nepali_year', 'nepali_month',
                                                                              'nepali_day', 'time_interval',
                                                                              'Nat']].rename(
            columns={'Nat': 'Peak_MW'}).assign(**{'Load Type': 'National Peak Load'})
        df_c = pd.concat([s_d, n_d])
        df_c['YM'] = df_c['nepali_year'].astype(str) + "/" + df_c['nepali_month'].astype(str).str.zfill(2)
        df_c.sort_values(['nepali_year', 'nepali_month'], inplace=True)

        fig_h = px.line(df_c, x='YM', y='Peak_MW', color='Load Type', custom_data=['nepali_day', 'time_interval'],
                        markers=True, color_discrete_sequence=['#ff3366', '#0052cc'])
        fig_h.update_traces(
            hovertemplate='<b>%{fullData.name}</b><br>Month: %{x}<br>Peak Load: %{y:.2f} MW<br>Day: %{customdata[0]}<br>Time: %{customdata[1]}<extra></extra>')
        fig_h = update_chart_layout(fig_h, "System vs National Growth: Monthly Peaks Over Time",
                                    xaxis_title="Nepali Year/Month", legend_orientation="v")
        st.plotly_chart(fig_h, use_container_width=True)

with t4:
    if compare_mode == "🗓️ Date-wise" and comp_param:
        st.subheader("📊 Date-wise Comparison")
        st.info(f"📍 **Baseline Date Locked:** {sidebar_date_str}")
        fig_comp = go.Figure()
        metrics = []

        df_base = run_query(
            "SELECT time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? ORDER BY time_interval ASC",
            (comp_param, selected_year, selected_month, selected_day))
        base_v = None
        if not df_base.empty:
            df_base = df_base.set_index('time_interval').interpolate(method='linear', limit_direction='both').fillna(
                0).reset_index()
            fig_comp.add_trace(
                go.Scatter(x=df_base['time_interval'], y=df_base['value'], name=f"BASE: {sidebar_date_str}",
                           mode='lines', line=dict(width=3, color='#1e1e1e')))
            base_v = df_base['value'].max()
            metrics.append({"Target Date": f"BASE ({sidebar_date_str})", "Peak (MW)": base_v,
                            "Time": df_base.loc[df_base['value'].idxmax(), 'time_interval'], "Delta (MW)": 0.0,
                            "Var (%)": 0.0})

        for i, d_str in enumerate(selected_comp_days):
            y, m, d = map(int, d_str.split('/'))
            df_d = run_query(
                "SELECT time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? ORDER BY time_interval ASC",
                (comp_param, y, m, d))
            if not df_d.empty:
                df_d = df_d.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
                fig_comp.add_trace(go.Scatter(x=df_d['time_interval'], y=df_d['value'], name=d_str, mode='lines',
                                              line=dict(dash='dot', color=NEA_PALETTE[i % len(NEA_PALETTE)])))
                p_v = df_d['value'].max()
                metrics.append(
                    {"Target Date": d_str, "Peak (MW)": p_v, "Time": df_d.loc[df_d['value'].idxmax(), 'time_interval'],
                     "Delta (MW)": (p_v - base_v) if base_v else 0,
                     "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics:
            fig_comp = update_chart_layout(fig_comp, f"Date Comparison: {clean_param_name(comp_param)}",
                                           legend_orientation="v")
            st.plotly_chart(fig_comp, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics).style.format(
                {"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)

    elif compare_mode == "📈 Parameters" and baseline_param:
        st.subheader("📊 Parameter Overlay Studio")
        st.info(f"📍 **Analyzing Date:** {sidebar_date_str}")
        fig_comp2 = go.Figure()
        metrics2 = []

        df_base = run_query(
            "SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?",
            (baseline_param, selected_year, selected_month, selected_day))
        base_v = None
        if not df_base.empty:
            df_base = df_base.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
            fig_comp2.add_trace(go.Scatter(x=df_base['time_interval'], y=df_base['value'],
                                           name=f"BASE: {clean_param_name(baseline_param)}", mode='lines',
                                           line=dict(width=3, color='#1e1e1e')))
            base_v = df_base['value'].max()
            metrics2.append({"Parameter": f"BASE: {clean_param_name(baseline_param)}", "Peak (MW)": base_v,
                             "Time": df_base.loc[df_base['value'].idxmax(), 'time_interval'], "Delta (MW)": 0.0,
                             "Var (%)": 0.0})

        for i, p in enumerate(selected_comp_params):
            df_p = run_query(
                "SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?",
                (p, selected_year, selected_month, selected_day))
            if not df_p.empty:
                df_p = df_p.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
                fig_comp2.add_trace(
                    go.Scatter(x=df_p['time_interval'], y=df_p['value'], name=clean_param_name(p), mode='lines',
                               line=dict(dash='dot', color=NEA_PALETTE[i % len(NEA_PALETTE)])))
                p_v = df_p['value'].max()
                metrics2.append({"Parameter": clean_param_name(p), "Peak (MW)": p_v,
                                 "Time": df_p.loc[df_p['value'].idxmax(), 'time_interval'],
                                 "Delta (MW)": (p_v - base_v) if base_v else 0,
                                 "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics2:
            fig_comp2 = update_chart_layout(fig_comp2, "Parameter Overlay Analytics", legend_orientation="v")
            st.plotly_chart(fig_comp2, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics2).style.format(
                {"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)

    elif compare_mode == "📅 Daily Peak/Load Across Months" and comp_param and selected_comp_months:
        st.subheader("📊 Overlay: Daily Peak Trends Across Months")
        fig_comp3 = go.Figure()
        metrics3 = []
        base_v = None

        for i, ym_str in enumerate(selected_comp_months):
            y, m = map(int, ym_str.split('/'))
            df_m = run_query(
                "SELECT nepali_day, time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0",
                (comp_param, y, m))
            if not df_m.empty:
                df_peaks = df_m.loc[df_m.groupby('nepali_day')['value'].idxmax()].sort_values('nepali_day')
                l_name, l_dict = (f"BASE: {ym_str}", dict(width=3, color='#1e1e1e')) if i == 0 else (ym_str,
                                                                                                     dict(dash='dot',
                                                                                                          color=
                                                                                                          NEA_PALETTE[
                                                                                                              i % len(
                                                                                                                  NEA_PALETTE)]))
                fig_comp3.add_trace(
                    go.Scatter(x=df_peaks['nepali_day'], y=df_peaks['value'], name=l_name, mode='lines+markers',
                               line=l_dict, customdata=df_peaks['time_interval'],
                               hovertemplate='Day %{x}<br>Peak: %{y:.2f} MW<br>Time: %{customdata}<extra></extra>'))

                m_peak = df_peaks['value'].max()
                if i == 0: base_v = m_peak
                metrics3.append({"Month": l_name, "Abs Peak": m_peak, "Avg Peak": df_peaks['value'].mean(),
                                 "Delta": (m_peak - base_v) if base_v else 0,
                                 "Var (%)": ((m_peak - base_v) / base_v * 100) if base_v else 0})

        if metrics3:
            fig_comp3 = update_chart_layout(fig_comp3, f"Day-by-Day Peak Curve: {clean_param_name(comp_param)}",
                                            xaxis_title="Day of the Month", legend_orientation="v")
            fig_comp3.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1, range=[0.8, 32.2]))
            st.plotly_chart(fig_comp3, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics3).style.format(
                {"Abs Peak": "{:.2f}", "Avg Peak": "{:.2f}", "Delta": "{:+.2f}", "Var (%)": "{:+.2f}%"}),
                         use_container_width=True)

    elif compare_mode == "📊 Monthly Peak/Load" and comp_param and selected_comp_months:
        st.subheader("📊 Macro-Trend: Absolute Peak Comparison")
        metrics4 = []
        base_v = None

        for i, ym_str in enumerate(selected_comp_months):
            y, m = map(int, ym_str.split('/'))
            df_p = run_query(
                "SELECT nepali_day, time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0 ORDER BY value DESC LIMIT 1",
                (comp_param, y, m))
            if not df_p.empty:
                p_v, p_d, p_t = df_p.iloc[0]['value'], df_p.iloc[0]['nepali_day'], df_p.iloc[0]['time_interval']
                if i == 0: base_v = p_v
                metrics4.append(
                    {"Month": f"BASE: {ym_str}" if i == 0 else ym_str, "Peak (MW)": p_v, "Day": p_d, "Time": p_t,
                     "Delta (MW)": (p_v - base_v) if base_v else 0,
                     "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics4:
            df_m4 = pd.DataFrame(metrics4)
            fig_bar = px.bar(df_m4, x="Month", y="Peak (MW)", text="Peak (MW)", color="Month",
                             title=f"Absolute Monthly Peaks: {clean_param_name(comp_param)}",
                             color_discrete_sequence=['#0052cc', '#33b2df', '#ff3366', '#fdbb2d'])
            fig_bar.update_traces(texttemplate='%{text:.2f} MW', textposition='outside')
            fig_bar = update_chart_layout(fig_bar, f"Absolute Monthly Peaks: {clean_param_name(comp_param)}",
                                          xaxis_title="")
            fig_bar.update_layout(showlegend=False, yaxis=dict(range=[0, df_m4['Peak (MW)'].max() * 1.15]))
            st.plotly_chart(fig_bar, use_container_width=True)
            st.dataframe(df_m4.style.format({"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}),
                         use_container_width=True)
