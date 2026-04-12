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

# 🚀 CRITICAL FIX: The Color Palette is now globally defined here!
NEA_PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']

def init_db():
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

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sys_date_param ON system_log_data(nepali_year, nepali_month, nepali_day, parameter_name);')
    conn.commit()
    conn.close()


def run_query(query, params=()):
    conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


# ==========================================
# 2. DATA EXTRACTION ENGINE (CSV & Excel)
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
                hr = int(match.group(1))
                mnt = int(match.group(2))
                current_time_cols[col_idx] = f"{hr:02d}:{mnt:02d}:00"
            else:
                try:
                    num = float(val)
                    if 0 < num <= 24:
                        hr = int(num)
                        mnt = int(round((num - hr) * 60))
                        current_time_cols[col_idx] = f"{hr:02d}:{mnt:02d}:00"
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

        if "Total IPP" in raw_name:
            db_param_name = raw_name
            current_block = "NEA_SUB_ZONE"
        elif "Total NEA SUBSIDIARIES" in raw_name:
            db_param_name = raw_name
            current_block = "ROR_ZONE"
        elif "Total ROR" in raw_name:
            db_param_name = raw_name
            current_block = "STORAGE_ZONE"
        elif "Total STORGE" in raw_name or "Total Storage" in raw_name:
            db_param_name = raw_name
            current_block = "IMPORT_ZONE"
        elif "Total IMPORT" in raw_name:
            db_param_name = "SUMMARY_TOTAL_IMPORT"
            current_block = "AFTER_IMPORT"
        elif "TOTAL NATIONAL LOAD" in raw_name or "NATIONAL LOAD" in raw_name:
            current_block = "EXPORT_ZONE"
            db_param_name = raw_name
        elif "Total EXPORT" in raw_name:
            db_param_name = "SUMMARY_TOTAL_EXPORT"
            current_block = "FINAL_TOTALS"
        else:
            if current_block == "IPP_ZONE":
                db_param_name = f"ZONE_IPP_{raw_name}"
            elif current_block == "NEA_SUB_ZONE":
                db_param_name = f"ZONE_NEASUB_{raw_name}"
            elif current_block == "ROR_ZONE":
                db_param_name = f"ZONE_ROR_{raw_name}"
            elif current_block == "STORAGE_ZONE":
                db_param_name = f"ZONE_STORAGE_{raw_name}"
            elif current_block == "IMPORT_ZONE":
                db_param_name = f"ZONE_IMPORT_{raw_name}"
            elif current_block == "EXPORT_ZONE":
                db_param_name = f"ZONE_EXPORT_{raw_name}"
            else:
                db_param_name = raw_name

        for col_idx, time_interval in time_columns.items():
            cell_value = row[col_idx] if col_idx < len(row) else None
            try:
                clean_val = str(cell_value).replace(',', '').strip()
                if clean_val in ['', '-']:
                    final_value = 0.0
                else:
                    final_value = float(clean_val)
                    if current_block == "EXPORT_ZONE" or db_param_name == "SUMMARY_TOTAL_EXPORT":
                        final_value = -abs(final_value)
            except:
                final_value = 0.0

            cursor.execute('''
                INSERT INTO system_log_data
                (nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value)
                VALUES (?, ?, ?, ?, ?, ?) 
                ON CONFLICT(nepali_year, nepali_month, nepali_day, time_interval, parameter_name) 
                DO UPDATE SET value = excluded.value
            ''', (year, month, day, time_interval, db_param_name, final_value))
            rows_inserted += 1

    return rows_inserted


def process_file(file_path):
    time.sleep(2)
    filename = os.path.basename(file_path)

    if not filename.lower().endswith(('.xlsx', '.xls', '.csv')) or filename.startswith('~'):
        return "ERROR", f"Skipped `{filename}`: Unsupported or temporary file."

    year, month = parse_filename(filename)
    if year is None or month is None:
        return "ERROR", f"⚠️ OVERWRITE PREVENTED: `{filename}` does not contain a clear Year and Month (e.g., '10-2082')."

    try:
        mtime = os.path.getmtime(file_path)
        conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
        cursor = conn.cursor()

        cursor.execute("SELECT mtime FROM processed_files WHERE filename = ?", (file_path,))
        row = cursor.fetchone()
        if row and row[0] == mtime:
            conn.close()
            return "SKIPPED", f"Skipped `{filename}` (Already recorded)."

        file_bytes = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with open(file_path, 'rb') as f:
                    file_bytes = f.read()
                break
            except PermissionError:
                time.sleep(2)

        if file_bytes is None:
            conn.close()
            return "ERROR", f"File `{filename}` is heavily locked by Excel. Please finish saving."

        file_io = io.BytesIO(file_bytes)

        rows_inserted = 0
        if filename.lower().endswith('.csv'):
            match_day = re.search(r'-\s*(\d+)\.csv$', filename.lower())
            day = int(match_day.group(1)) if match_day else None
            if day is not None and day > 0:
                df = pd.read_csv(file_io, header=None, encoding='utf-8', on_bad_lines='skip')
                rows_inserted += extract_data(df, year, month, day, cursor)
        else:
            xl = pd.ExcelFile(file_io, engine='openpyxl' if filename.endswith('.xlsx') else None)
            for sheet_name in xl.sheet_names:
                if sheet_name.strip().isdigit():
                    day = int(sheet_name.strip())
                    if day > 0: 
                        df = xl.parse(sheet_name, header=None)
                        rows_inserted += extract_data(df, year, month, day, cursor)

        if rows_inserted > 0:
            cursor.execute('''
                INSERT INTO processed_files (filename, mtime) 
                VALUES (?, ?) ON CONFLICT(filename) DO UPDATE SET mtime=excluded.mtime
            ''', (file_path, mtime))
            conn.commit()
            conn.close()
            st.cache_data.clear()
            return "SUCCESS", f"✅ Added `{filename}` (Year {year}, Month {month})."
        else:
            conn.commit()
            conn.close()
            return "ERROR", f"No valid day sheets found inside `{filename}`."

    except Exception as e:
        return "ERROR", f"Error reading `{filename}`: {str(e)}"


# ==========================================
# 3. BACKGROUND FOLDER MONITOR
# ==========================================
class FileWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')):
            time.sleep(1)
            process_file(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')):
            time.sleep(1)
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
# 4. STREAMLIT DASHBOARD UI & CATEGORIZATION
# ==========================================
def categorize_params(param_list):
    categories = {}
    for p in param_list:
        p_up = p.upper()
        if p_up.startswith('ZONE_IPP_'):
            cat = "🔌 IPPs (Independent Power Producers)"
        elif p_up.startswith('ZONE_ROR_'):
            cat = "🌊 RORs (Run of River)"
        elif p_up.startswith('ZONE_STORAGE_'):
            cat = "🔋 Storage Plants"
        elif p_up.startswith('ZONE_NEASUB_'):
            cat = "🏢 NEA Subsidiaries"
        elif p_up.startswith('ZONE_IMPORT_'):
            cat = "⬇️ Import Zones"
        elif p_up.startswith('ZONE_EXPORT_'):
            cat = "⬆️ Export Zones"
        elif 'SUMMARY' in p_up or 'TOTAL' in p_up or 'LOAD' in p_up:
            cat = "📊 System Totals & Summaries"
        else:
            cat = "📁 Other / Miscellaneous"

        if cat not in categories: categories[cat] = []
        categories[cat].append(p)
    return categories


def clean_param_name(p):
    return p.replace('ZONE_IPP_', 'IPP: ').replace('ZONE_ROR_', 'ROR: ').replace('ZONE_STORAGE_', 'STORAGE: ').replace(
        'ZONE_NEASUB_', 'SUB: ').replace('ZONE_IMPORT_', 'IMPORT: ').replace('ZONE_EXPORT_', 'EXPORT: ')


@st.cache_data(show_spinner=False)
def convert_df(df): return df.to_csv(index=False).encode('utf-8')


def update_chart_layout(fig, title, yaxis_title="Power (MW)", xaxis_title="Time", legend_orientation="v"):
    legend_settings = dict(orientation="v", y=1, x=1.02) if legend_orientation == "v" else dict(orientation="h", y=-0.25, x=0, yanchor="top")
    right_margin = 150 if legend_orientation == "v" else 10
    
    fig.update_layout(
        title=title, hovermode="x unified", template="plotly_white",
        plot_bgcolor='rgba(255,255,255,1)', paper_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(title=xaxis_title, type="category", showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec', tickangle=-45, automargin=True),
        yaxis=dict(title=yaxis_title, showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec', automargin=True),
        legend=legend_settings, margin=dict(l=10, r=right_margin, t=50, b=60)
    )
    return fig


def draw_stacked_chart(df_pivot, cols, title, total_col=None, show_export=False, export_col=None):
    fig = go.Figure()
    for i, col in enumerate(cols):
        fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[col], name=clean_param_name(col), stackgroup='one', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)])))
    if show_export and export_col and export_col in df_pivot.columns:
        fig.add_trace(go.Scatter(x=df_pivot.index, y=-df_pivot[export_col].abs(), name='Total EXPORT', stackgroup='two', line=dict(width=0.5, color='#d62728')))
    if total_col and total_col in df_pivot.columns:
        fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[total_col], name='TOTAL', line=dict(color='#1e1e1e', width=3)))
    
    fig = update_chart_layout(fig, title, legend_orientation="v")
    st.plotly_chart(fig, use_container_width=True)


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
        .stTabs [aria-selected="true"] { 
            background-color: #e8f0fe; color: #0052cc !important; 
            border-bottom: 3px solid #0052cc !important; font-weight: bold; 
        }
        [data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e5ec; }
        .streamlit-expanderHeader { background-color: #ffffff; border-radius: 5px; border: 1px solid #e0e5ec; }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ Nepal Electricity Authority - LDC Dashboard")

years_df = run_query("SELECT DISTINCT nepali_year FROM system_log_data ORDER BY nepali_year DESC")

if years_df.empty:
    st.warning("Database is currently empty.")
    st.info(
        f"**Step 1:** Ensure your files have the Month and Year in the name (e.g. `Log-10-2082.xlsx`).\n\n**Step 2:** Place them into the `{FOLDER_TO_WATCH}` folder.\n\n**Step 3:** Click Force Scan.")

    if st.button("🚀 Force Manual Scan (Scan All Files)"):
        new_files, skipped_files, error_files = 0, 0, 0
        if os.path.exists(FOLDER_TO_WATCH):
            for root, dirs, files in os.walk(FOLDER_TO_WATCH):
                for filename in files:
                    if filename.lower().endswith(('.xlsx', '.xls', '.csv')) and not filename.startswith('~'):
                        file_path = os.path.join(root, filename)
                        st.write(f"Checking `{filename}`...")
                        status, message = process_file(file_path)
                        if status == "SKIPPED":
                            skipped_files += 1
                        elif status == "SUCCESS":
                            new_files += 1; st.success(message)
                        else:
                            error_files += 1; st.error(message)

        st.info(
            f"📊 **Scan Complete:** {new_files} New Files Added | {skipped_files} Previously Recorded Skipped | {error_files} Errors")
        if new_files > 0: time.sleep(3); st.rerun()
    st.stop()

# ==========================================
# 5. SIDEBAR: GLOBAL CONTROLS
# ==========================================
if st.sidebar.button("🚀 Force Manual Scan", use_container_width=True):
    new_files, skipped_files, error_files = 0, 0, 0
    if os.path.exists(FOLDER_TO_WATCH):
        with st.sidebar.status("Scanning Folder...", expanded=True) as status_box:
            for root, dirs, files in os.walk(FOLDER_TO_WATCH):
                for filename in files:
                    if filename.lower().endswith(('.xlsx', '.xls', '.csv')) and not filename.startswith('~'):
                        file_path = os.path.join(root, filename)
                        status, message = process_file(file_path)
                        if status == "SKIPPED":
                            skipped_files += 1
                        elif status == "SUCCESS":
                            new_files += 1; st.write(f"✅ {filename}")
                        else:
                            error_files += 1; st.write(f"❌ Error in {filename}")

            status_box.update(label="Scan Complete!", state="complete", expanded=False)

        st.sidebar.success(f"**{new_files}** New files processed.")
        st.sidebar.info(f"**{skipped_files}** Previous files skipped.")
        if error_files > 0: st.sidebar.error(f"**{error_files}** Files failed.")
        if new_files > 0: time.sleep(2); st.rerun()
    else:
        st.sidebar.error("LDC_Data folder not found.")

st.sidebar.divider()
st.sidebar.header("📅 Primary Date Filter")
st.sidebar.markdown("*(Drives Tabs 1, 2, 3 & Baseline)*")

if not years_df.empty:
    selected_year = st.sidebar.selectbox("Select Nepali Year", years_df['nepali_year'].tolist())
    months_df = run_query(
        "SELECT DISTINCT nepali_month FROM system_log_data WHERE nepali_year = ? ORDER BY nepali_month",
        (selected_year,))
    selected_month = st.sidebar.selectbox("Select Nepali Month", months_df['nepali_month'].tolist())
    
    # DB Filter: Added nepali_day > 0 to ignore old bad data
    days_df = run_query(
        "SELECT DISTINCT nepali_day FROM system_log_data WHERE nepali_year = ? AND nepali_month = ? AND nepali_day > 0 ORDER BY nepali_day",
        (selected_year, selected_month))
        
    if not days_df.empty:
        selected_day = st.sidebar.selectbox("Select Nepali Day", days_df['nepali_day'].tolist())
        sidebar_date_str = f"{selected_year}/{str(selected_month).zfill(2)}/{str(selected_day).zfill(2)}"
    else:
        selected_day = None
        sidebar_date_str = "No valid days found"
        st.sidebar.warning("No valid day data for this month.")

# --- TAB 4 SPECIFIC SIDEBAR CONTROLS ---
st.sidebar.divider()
st.sidebar.header("📊 Comparison Studio")

compare_mode = st.sidebar.radio("Comparison Strategy:", [
    "🗓️ Date-wise",
    "📈 Parameters",
    "📅 Daily Peak/Load Across Months",
    "📊 Monthly Peak/Load"
])

if compare_mode == "🗓️ Date-wise":
    all_params_query = "SELECT DISTINCT parameter_name FROM system_log_data ORDER BY parameter_name"
    all_params_df = run_query(all_params_query)

    if not all_params_df.empty:
        param_list = all_params_df['parameter_name'].tolist()
        categorized = categorize_params(param_list)
        cat_options = sorted(list(categorized.keys()))

        selected_cats = st.sidebar.multiselect("1. Filter Categories", cat_options, default=cat_options)

        if selected_cats:
            filtered_params = sorted([p for cat in selected_cats for p in categorized[cat]])
            default_param_idx = filtered_params.index(
                'TOTAL SYSTEM LOAD (ACTUAL)') if 'TOTAL SYSTEM LOAD (ACTUAL)' in filtered_params else 0
            comp_param = st.sidebar.selectbox("2. Select Specific Parameter", filtered_params, index=default_param_idx,
                                              format_func=clean_param_name)

            # DB Filter: Added nepali_day > 0
            dates_query = "SELECT DISTINCT nepali_year, nepali_month, nepali_day FROM system_log_data WHERE parameter_name = ? AND nepali_day > 0 ORDER BY nepali_year DESC, nepali_month DESC, nepali_day DESC"
            dates_df = run_query(dates_query, (comp_param,))

            if not dates_df.empty:
                dates_df['Formatted_Date'] = dates_df['nepali_year'].astype(str) + "/" + dates_df[
                    'nepali_month'].astype(str).str.zfill(2) + "/" + dates_df['nepali_day'].astype(str).str.zfill(2)
                date_list = [d for d in dates_df['Formatted_Date'].tolist() if d != sidebar_date_str]
                selected_comp_days = st.sidebar.multiselect("3. Additional Dates to Overlay", date_list)
            else:
                selected_comp_days = []
        else:
            comp_param, selected_comp_days = None, []
    else:
        comp_param, selected_comp_days = None, []

elif compare_mode == "📈 Parameters":
    if selected_day:
        params_avail = run_query(
            "SELECT DISTINCT parameter_name FROM system_log_data WHERE nepali_year=? AND nepali_month=? AND nepali_day=?",
            (selected_year, selected_month, selected_day))
        if not params_avail.empty:
            param_list = params_avail['parameter_name'].tolist()
            categorized = categorize_params(param_list)
            cat_options = sorted(list(categorized.keys()))

            selected_cats = st.sidebar.multiselect("1. Filter Categories", cat_options, default=cat_options)

            if selected_cats:
                filtered_params = sorted([p for cat in selected_cats for p in categorized[cat]])
                default_param_idx = filtered_params.index(
                    'TOTAL SYSTEM LOAD (ACTUAL)') if 'TOTAL SYSTEM LOAD (ACTUAL)' in filtered_params else (
                    filtered_params.index('SUMMARY_TOTAL_IMPORT') if 'SUMMARY_TOTAL_IMPORT' in filtered_params else 0)
                baseline_param = st.sidebar.selectbox("2. BASELINE Parameter", filtered_params, index=default_param_idx,
                                                      format_func=clean_param_name)

                comp_list = [p for p in filtered_params if p != baseline_param]
                selected_comp_params = st.sidebar.multiselect("3. Additional Parameters to Overlay", comp_list,
                                                              format_func=clean_param_name)
            else:
                baseline_param, selected_comp_params = None, []
        else:
            baseline_param, selected_comp_params = None, []
    else:
        baseline_param, selected_comp_params = None, []

elif compare_mode in ["📅 Daily Peak/Load Across Months", "📊 Monthly Peak/Load"]:
    all_params_query = "SELECT DISTINCT parameter_name FROM system_log_data ORDER BY parameter_name"
    all_params_df = run_query(all_params_query)

    if not all_params_df.empty:
        param_list = all_params_df['parameter_name'].tolist()
        categorized = categorize_params(param_list)
        cat_options = sorted(list(categorized.keys()))

        selected_cats = st.sidebar.multiselect("1. Filter Categories", cat_options, default=cat_options)

        if selected_cats:
            filtered_params = sorted([p for cat in selected_cats for p in categorized[cat]])
            default_param_idx = filtered_params.index(
                'TOTAL SYSTEM LOAD (ACTUAL)') if 'TOTAL SYSTEM LOAD (ACTUAL)' in filtered_params else 0
            comp_param = st.sidebar.selectbox("2. Select Specific Parameter", filtered_params, index=default_param_idx,
                                              format_func=clean_param_name)

            ym_query = "SELECT DISTINCT nepali_year, nepali_month FROM system_log_data WHERE parameter_name = ? ORDER BY nepali_year DESC, nepali_month DESC"
            ym_df = run_query(ym_query, (comp_param,))

            if not ym_df.empty:
                ym_df['Formatted_YM'] = ym_df['nepali_year'].astype(str) + "/" + ym_df['nepali_month'].astype(
                    str).str.zfill(2)
                ym_list = ym_df['Formatted_YM'].tolist()

                selected_comp_months = st.sidebar.multiselect("3. Select Months to Compare (First is Baseline)",
                                                              ym_list, default=ym_list[:min(2, len(ym_list))])
            else:
                selected_comp_months = []
        else:
            comp_param, selected_comp_months = None, []
    else:
        comp_param, selected_comp_months = None, []

# ==========================================
# 6. MAIN UI TABS (DISPLAY ONLY)
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs([
    "⚡ Daily Operations",
    "📆 Monthly Peak Load Graph",
    "📈 Historical Monthly Peak Load",
    "🔍 Comparison Studio"
])

with tab1:
    sub_tab_main, sub_tab_import, sub_tab_subs, sub_tab_ipp, sub_tab_ror, sub_tab_storage = st.tabs([
        "Main System", "Total IMPORT", "Total NEA SUBSIDIARIES", "Total IPP", "Total ROR", "Total STORAGE"
    ])

    if selected_day:
        query_all = '''
            SELECT time_interval as Time, parameter_name, value as MW
            FROM system_log_data
            WHERE nepali_year = ? AND nepali_month = ? AND nepali_day = ?
            ORDER BY time_interval ASC
        '''
        df_all = run_query(query_all, (selected_year, selected_month, selected_day))

        if not df_all.empty:
            # --- SUB-TAB: MAIN SYSTEM ---
            with sub_tab_main:
                all_db_params = df_all['parameter_name'].unique()
                raw_supply_components = [
                    p for p in all_db_params
                    if (
                                   'TOTAL' in p.upper() or 'INTERRUPTION' in p.upper() or 'ROR' in p.upper() or 'STORGE' in p.upper() or 'STORAGE' in p.upper())
                       and 'IMPORT' not in p.upper()
                       and 'EXPORT' not in p.upper()
                       and 'LOAD' not in p.upper()
                       and 'OTHER IPP' not in p.upper()
                       and 'ZONE_' not in p.upper()
                ]

                import_tag = 'SUMMARY_TOTAL_IMPORT'
                export_tag = 'SUMMARY_TOTAL_EXPORT'

                required_params = raw_supply_components.copy()
                if import_tag in all_db_params: required_params.append(import_tag)
                if export_tag in all_db_params: required_params.append(export_tag)

                df_summary = df_all[df_all['parameter_name'].isin(required_params)].copy()

                if not df_summary.empty:
                    df_pivot = df_summary.pivot(index='Time', columns='parameter_name', values='MW').fillna(0.0)
                    df_pivot = df_pivot.sort_index()

                    valid_supply_components = []
                    for col in raw_supply_components:
                        if col in df_pivot.columns:
                            if df_pivot[col].abs().sum() > 0 or 'STORGE' in col.upper() or 'STORAGE' in col.upper():
                                valid_supply_components.append(col)

                    if import_tag in df_pivot.columns and import_tag not in valid_supply_components:
                        valid_supply_components.append(import_tag)

                    if export_tag not in df_pivot.columns: df_pivot[export_tag] = 0.0
                    if import_tag not in df_pivot.columns: df_pivot[import_tag] = 0.0

                    df_pivot[import_tag] = df_pivot[import_tag] - df_pivot[export_tag].abs()
                    df_pivot['DYNAMIC_NATIONAL_LOAD'] = df_pivot[valid_supply_components].sum(axis=1)
                    df_pivot['DYNAMIC_SYSTEM_LOAD'] = df_pivot['DYNAMIC_NATIONAL_LOAD'] + df_pivot[export_tag].abs()

                    sys_peak_hour = df_pivot['DYNAMIC_SYSTEM_LOAD'].idxmax()
                    sys_peak_val = df_pivot.loc[sys_peak_hour, 'DYNAMIC_SYSTEM_LOAD']
                    nat_peak_hour = df_pivot['DYNAMIC_NATIONAL_LOAD'].idxmax()
                    nat_peak_val = df_pivot.loc[nat_peak_hour, 'DYNAMIC_NATIONAL_LOAD']

                    # --- ADDING ENERGY AND LOAD FACTOR HERE ---
                    total_energy_mwh = df_pivot['DYNAMIC_SYSTEM_LOAD'].sum()
                    load_factor = (df_pivot['DYNAMIC_SYSTEM_LOAD'].mean() / sys_peak_val * 100) if sys_peak_val > 0 else 0

                    st.markdown("<br>", unsafe_allow_html=True)
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric(f"📈 Max Sys Load ({sys_peak_hour})", f"{sys_peak_val:.2f} MW")
                    m2.metric(f"📊 Max Nat Peak ({nat_peak_hour})", f"{nat_peak_val:.2f} MW")
                    m3.metric(f"⚡ Total Energy", f"{total_energy_mwh:,.0f} MWh")
                    m4.metric(f"⚙️ Load Factor", f"{load_factor:.1f}%")
                    st.markdown("<br>", unsafe_allow_html=True)

                    fig_main = go.Figure()

                    for i, col in enumerate(valid_supply_components):
                        legend_name = col.replace('SUMMARY_TOTAL_', 'Total ').replace('STORGE', 'Storage').replace('Storge',
                                                                                                                   'Storage')
                        if col == import_tag: legend_name = "Total IMPORT (Net)"

                        fig_main.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=legend_name,
                            stackgroup='supply_stack', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)]),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    if df_pivot[export_tag].abs().sum() > 0:
                        fig_main.add_trace(go.Scatter(
                            x=df_pivot.index, y=-df_pivot[export_tag].abs(),
                            name='Total EXPORT', stackgroup='export_stack',
                            line=dict(width=0.5, color='#d62728'),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig_main.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['DYNAMIC_SYSTEM_LOAD'],
                        name='SYSTEM LOAD', line=dict(color='#ff3366', width=2.5, dash='dot')
                    ))

                    fig_main.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['DYNAMIC_NATIONAL_LOAD'],
                        name='NATIONAL LOAD', line=dict(color='#1e1e1e', width=3)
                    ))

                    fig_main = update_chart_layout(fig_main, "System Operation & Load Curve", legend_orientation="v")
                    fig_main.add_hline(y=0, line_width=2, line_color="#1e1e1e")
                    st.plotly_chart(fig_main, use_container_width=True)

                    with st.expander("🔍 View Filtered Supply Components & Loads"):
                        display_cols = ['DYNAMIC_SYSTEM_LOAD', 'DYNAMIC_NATIONAL_LOAD'] + valid_supply_components + [
                            export_tag]
                        st.dataframe(df_pivot[display_cols].style.format("{:.2f}"), use_container_width=True)
                else:
                    st.info("No system summary data found.")

            # --- SUB-TAB: TOTAL IMPORT ---
            with sub_tab_import:
                df_imp = df_all[df_all['parameter_name'].str.startswith('ZONE_IMPORT_', na=False)].copy()

                if not df_imp.empty:
                    df_imp['Display_Name'] = df_imp['parameter_name'].str.replace('ZONE_IMPORT_', '')
                    df_pivot = df_imp.pivot(index='Time', columns='Display_Name', values='MW').fillna(0)
                    df_pivot = df_pivot.sort_index()
                    df_pivot['CALCULATED_TOTAL'] = df_pivot.sum(axis=1)

                    fig = go.Figure()
                    for col in df_pivot.columns:
                        if col == 'CALCULATED_TOTAL': continue
                        fig.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=col,
                            stackgroup='import_flow', line=dict(width=1),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['CALCULATED_TOTAL'],
                        name='TOTAL IMPORT', line=dict(color='#1e1e1e', width=3),
                        hovertemplate='<b>TOTAL</b>: %{y:.2f} MW<extra></extra>'
                    ))

                    fig = update_chart_layout(fig, "Dynamic Breakdown: Import & Export",
                                              yaxis_title="MW (+Import / -Export)", legend_orientation="v")
                    fig.add_hline(y=0, line_width=2, line_color="#1e1e1e")
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No Import zone data found.")

            # --- SUB-TAB: TOTAL NEA SUBSIDIARIES ---
            with sub_tab_subs:
                zone_prefix = 'ZONE_NEASUB_'
                df_subs = df_all[df_all['parameter_name'].str.startswith(zone_prefix, na=False)].copy()

                if not df_subs.empty:
                    df_subs['Display_Name'] = df_subs['parameter_name'].str.replace(zone_prefix, '')
                    df_pivot = df_subs.pivot(index='Time', columns='Display_Name', values='MW').fillna(0.0)
                    df_pivot = df_pivot.sort_index()

                    valid_cols = [c for c in df_pivot.columns if df_pivot[c].abs().sum() > 0]
                    df_pivot['CALCULATED_TOTAL'] = df_pivot[valid_cols].sum(axis=1)

                    fig_subs = go.Figure()
                    for i, col in enumerate(valid_cols):
                        fig_subs.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=col,
                            stackgroup='subs_stack', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)]),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig_subs.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['CALCULATED_TOTAL'],
                        name='CALCULATED TOTAL', line=dict(color='#1e1e1e', width=3),
                        hovertemplate='<b>TOTAL SUBSIDIARIES</b>: %{y:.2f} MW<extra></extra>'
                    ))

                    fig_subs = update_chart_layout(fig_subs, "Dynamic Breakdown: NEA Subsidiaries", legend_orientation="v")
                    st.plotly_chart(fig_subs, use_container_width=True)

                    with st.expander("🔍 View Component Breakdown & Verification"):
                        st.dataframe(df_pivot[valid_cols + ['CALCULATED_TOTAL']].style.format("{:.2f}"),
                                     use_container_width=True)
                else:
                    st.info("No rows found in the NEA Subsidiaries zone.")

            # --- SUB-TAB: TOTAL IPP ---
            with sub_tab_ipp:
                zone_prefix = 'ZONE_IPP_'
                df_ipp = df_all[df_all['parameter_name'].str.startswith(zone_prefix, na=False)].copy()

                if not df_ipp.empty:
                    df_ipp['Display_Name'] = df_ipp['parameter_name'].str.replace(zone_prefix, '')
                    df_pivot = df_ipp.pivot(index='Time', columns='Display_Name', values='MW').fillna(0.0)
                    df_pivot = df_pivot.sort_index()

                    valid_cols = [c for c in df_pivot.columns if df_pivot[c].abs().sum() > 0]
                    df_pivot['CALCULATED_TOTAL'] = df_pivot[valid_cols].sum(axis=1)

                    fig_ipp = go.Figure()
                    for i, col in enumerate(valid_cols):
                        fig_ipp.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=col,
                            stackgroup='ipp_stack', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)]),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig_ipp.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['CALCULATED_TOTAL'],
                        name='CALCULATED TOTAL', line=dict(color='#1e1e1e', width=3),
                        hovertemplate='<b>TOTAL IPP</b>: %{y:.2f} MW<extra></extra>'
                    ))

                    fig_ipp = update_chart_layout(fig_ipp, "Dynamic Breakdown: Independent Power Producers (IPP)",
                                                  legend_orientation="v")
                    st.plotly_chart(fig_ipp, use_container_width=True)

                    with st.expander("🔍 View Component Breakdown & Verification"):
                        st.dataframe(df_pivot[valid_cols + ['CALCULATED_TOTAL']].style.format("{:.2f}"),
                                     use_container_width=True)
                else:
                    st.info("No rows found in the IPP zone.")

            # --- SUB-TAB: TOTAL ROR ---
            with sub_tab_ror:
                zone_prefix = 'ZONE_ROR_'
                df_ror = df_all[df_all['parameter_name'].str.startswith(zone_prefix, na=False)].copy()

                if not df_ror.empty:
                    df_ror['Display_Name'] = df_ror['parameter_name'].str.replace(zone_prefix, '')
                    df_pivot = df_ror.pivot(index='Time', columns='Display_Name', values='MW').fillna(0.0)
                    df_pivot = df_pivot.sort_index()

                    valid_cols = [c for c in df_pivot.columns if df_pivot[c].abs().sum() > 0]
                    df_pivot['CALCULATED_TOTAL'] = df_pivot[valid_cols].sum(axis=1)

                    fig_ror = go.Figure()
                    for i, col in enumerate(valid_cols):
                        fig_ror.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=col,
                            stackgroup='ror_stack', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)]),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig_ror.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['CALCULATED_TOTAL'],
                        name='CALCULATED TOTAL', line=dict(color='#1e1e1e', width=3),
                        hovertemplate='<b>TOTAL ROR</b>: %{y:.2f} MW<extra></extra>'
                    ))

                    fig_ror = update_chart_layout(fig_ror, "Dynamic Breakdown: Run of River (ROR) Plants",
                                                  legend_orientation="v")
                    st.plotly_chart(fig_ror, use_container_width=True)

                    with st.expander("🔍 View Component Breakdown & Verification"):
                        st.dataframe(df_pivot[valid_cols + ['CALCULATED_TOTAL']].style.format("{:.2f}"),
                                     use_container_width=True)
                else:
                    st.info("No rows found in the ROR zone.")

            # --- SUB-TAB: TOTAL STORAGE ---
            with sub_tab_storage:
                zone_prefix = 'ZONE_STORAGE_'
                df_storage = df_all[df_all['parameter_name'].str.startswith(zone_prefix, na=False)].copy()

                if not df_storage.empty:
                    df_storage['Display_Name'] = df_storage['parameter_name'].str.replace(zone_prefix, '')
                    df_pivot = df_storage.pivot(index='Time', columns='Display_Name', values='MW').fillna(0.0)
                    df_pivot = df_pivot.sort_index()

                    valid_cols = list(df_pivot.columns)
                    df_pivot['CALCULATED_TOTAL'] = df_pivot[valid_cols].sum(axis=1)

                    fig_storage = go.Figure()
                    for i, col in enumerate(valid_cols):
                        fig_storage.add_trace(go.Scatter(
                            x=df_pivot.index, y=df_pivot[col], name=col,
                            stackgroup='storage_stack', line=dict(width=0.5, color=NEA_PALETTE[i % len(NEA_PALETTE)]),
                            hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'
                        ))

                    fig_storage.add_trace(go.Scatter(
                        x=df_pivot.index, y=df_pivot['CALCULATED_TOTAL'],
                        name='CALCULATED TOTAL', line=dict(color='#1e1e1e', width=3),
                        hovertemplate='<b>TOTAL STORAGE</b>: %{y:.2f} MW<extra></extra>'
                    ))

                    fig_storage = update_chart_layout(fig_storage, "Storage Plants", legend_orientation="v")
                    st.plotly_chart(fig_storage, use_container_width=True)

                    with st.expander("🔍 View Component Breakdown & Verification"):
                        st.dataframe(df_pivot[valid_cols + ['CALCULATED_TOTAL']].style.format("{:.2f}"),
                                     use_container_width=True)
                else:
                    st.info("No rows found in the Storage zone.")

with tab2:
    st.subheader(f"📊 Daily Peak Loads for Month {selected_month}, {selected_year}")

    # DB Filter: Added nepali_day > 0
    query_monthly = '''
        SELECT nepali_day, time_interval, parameter_name, value
        FROM system_log_data
        WHERE nepali_year = ? AND nepali_month = ? AND nepali_day > 0
          AND parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'SUMMARY_TOTAL_EXPORT')
    '''
    df_raw = run_query(query_monthly, (selected_year, selected_month))

    if not df_raw.empty:
        df_pivot = df_raw.pivot_table(
            index=['nepali_day', 'time_interval'],
            columns='parameter_name',
            values='value'
        ).fillna(0.0).reset_index()

        if 'TOTAL SYSTEM LOAD (ACTUAL)' not in df_pivot.columns: df_pivot['TOTAL SYSTEM LOAD (ACTUAL)'] = 0.0
        if 'SUMMARY_TOTAL_EXPORT' not in df_pivot.columns: df_pivot['SUMMARY_TOTAL_EXPORT'] = 0.0

        df_pivot['System Peak Load'] = df_pivot['TOTAL SYSTEM LOAD (ACTUAL)']
        df_pivot['National Peak Load'] = df_pivot['System Peak Load'] - df_pivot['SUMMARY_TOTAL_EXPORT'].abs()

        idx_sys = df_pivot.groupby('nepali_day')['System Peak Load'].idxmax()
        df_sys = df_pivot.loc[idx_sys, ['nepali_day', 'time_interval', 'System Peak Load']]
        df_sys.rename(columns={'System Peak Load': 'Peak_MW'}, inplace=True)
        df_sys['Load Type'] = 'System Peak Load'

        idx_nat = df_pivot.groupby('nepali_day')['National Peak Load'].idxmax()
        df_nat = df_pivot.loc[idx_nat, ['nepali_day', 'time_interval', 'National Peak Load']]
        df_nat.rename(columns={'National Peak Load': 'Peak_MW'}, inplace=True)
        df_nat['Load Type'] = 'National Peak Load'

        df_combined = pd.concat([df_sys, df_nat]).sort_values('nepali_day')

        fig_monthly = px.line(
            df_combined, x='nepali_day', y='Peak_MW', color='Load Type',
            custom_data=['time_interval'], markers=True,
            title="System vs National Peak Load Each Day",
            color_discrete_sequence=['#ff3366', '#0052cc']
        )

        fig_monthly.update_traces(
            hovertemplate='<b>%{fullData.name}</b><br>Day %{x}<br>Peak Load: %{y:.2f} MW<br>Time of Peak: %{customdata[0]}<extra></extra>')
        fig_monthly = update_chart_layout(fig_monthly, "System vs National Peak Load Each Day",
                                          xaxis_title="Day of Month", legend_orientation="h")

        st.plotly_chart(fig_monthly, use_container_width=True)

with tab3:
    st.subheader("📈 Historical Trend: Maximum Load per Month")

    # DB Filter: Added nepali_day > 0
    query_historical = '''
        SELECT nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value
        FROM system_log_data
        WHERE parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'SUMMARY_TOTAL_EXPORT') AND nepali_day > 0
    '''
    df_raw = run_query(query_historical)

    if not df_raw.empty:
        df_pivot = df_raw.pivot_table(
            index=['nepali_year', 'nepali_month', 'nepali_day', 'time_interval'],
            columns='parameter_name',
            values='value'
        ).fillna(0.0).reset_index()

        if 'TOTAL SYSTEM LOAD (ACTUAL)' not in df_pivot.columns: df_pivot['TOTAL SYSTEM LOAD (ACTUAL)'] = 0.0
        if 'SUMMARY_TOTAL_EXPORT' not in df_pivot.columns: df_pivot['SUMMARY_TOTAL_EXPORT'] = 0.0

        df_pivot['System Peak Load'] = df_pivot['TOTAL SYSTEM LOAD (ACTUAL)']
        df_pivot['National Peak Load'] = df_pivot['System Peak Load'] - df_pivot['SUMMARY_TOTAL_EXPORT'].abs()

        idx_sys = df_pivot.groupby(['nepali_year', 'nepali_month'])['System Peak Load'].idxmax()
        df_sys = df_pivot.loc[
            idx_sys, ['nepali_year', 'nepali_month', 'nepali_day', 'time_interval', 'System Peak Load']]
        df_sys.rename(columns={'System Peak Load': 'Peak_MW'}, inplace=True)
        df_sys['Load Type'] = 'System Peak Load'

        idx_nat = df_pivot.groupby(['nepali_year', 'nepali_month'])['National Peak Load'].idxmax()
        df_nat = df_pivot.loc[
            idx_nat, ['nepali_year', 'nepali_month', 'nepali_day', 'time_interval', 'National Peak Load']]
        df_nat.rename(columns={'National Peak Load': 'Peak_MW'}, inplace=True)
        df_nat['Load Type'] = 'National Peak Load'

        df_combined = pd.concat([df_sys, df_nat])
        df_combined['Year/Month'] = df_combined['nepali_year'].astype(str) + "/" + df_combined['nepali_month'].astype(
            str).str.zfill(2)
        df_combined.sort_values(['nepali_year', 'nepali_month'], inplace=True)

        fig_hist = px.line(
            df_combined, x='Year/Month', y='Peak_MW', color='Load Type',
            custom_data=['nepali_day', 'time_interval'], markers=True,
            title="System vs National Growth: Monthly Peaks Over Time",
            color_discrete_sequence=['#ff3366', '#0052cc']
        )

        fig_hist.update_traces(
            hovertemplate='<b>%{fullData.name}</b><br>Month: %{x}<br>Peak Load: %{y:.2f} MW<br>Day of Peak: %{customdata[0]}<br>Time of Peak: %{customdata[1]}<extra></extra>')
        fig_hist = update_chart_layout(fig_hist, "System vs National Growth: Monthly Peaks Over Time",
                                       xaxis_title="Nepali Year/Month", legend_orientation="h")

        st.plotly_chart(fig_hist, use_container_width=True)

# ==========================================
# 7. TAB 4: COMPARISON STUDIO
# ==========================================
with tab4:
    if compare_mode == "🗓️ Date-wise":
        st.subheader("📊 Date-wise Comparison")
        st.info(f"📍 **Baseline Date Locked:** {sidebar_date_str} (Controlled via primary left sidebar)")

        if 'comp_param' in locals() and comp_param:
            fig_comp = go.Figure()
            metrics_data = []

            df_base = run_query('''
                SELECT time_interval, value FROM system_log_data 
                WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? 
                ORDER BY time_interval ASC
            ''', (comp_param, selected_year, selected_month, selected_day))

            baseline_val = None
            if not df_base.empty:
                df_base['value'] = df_base['value'].fillna(0.0)
                fig_comp.add_trace(
                    go.Scatter(x=df_base['time_interval'], y=df_base['value'], name=f"BASELINE: {sidebar_date_str}",
                               mode='lines', line=dict(width=3, color='#1e1e1e')))
                baseline_val = df_base['value'].max()
                max_time_base = df_base.loc[df_base['value'].idxmax(), 'time_interval']

                metrics_data.append({
                    "Target Date": f"BASELINE ({sidebar_date_str})",
                    "Peak (MW)": baseline_val,
                    "Time": max_time_base,
                    "Delta (MW)": 0.0,
                    "Variance (%)": 0.0
                })
            else:
                st.warning(
                    f"No data found for '{clean_param_name(comp_param)}' on your baseline date ({sidebar_date_str}). Select a different parameter or change your primary date.")

            for i, date_str in enumerate(selected_comp_days):
                y, m, d = map(int, date_str.split('/'))
                df_day = run_query('''
                    SELECT time_interval, value FROM system_log_data 
                    WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? 
                    ORDER BY time_interval ASC
                ''', (comp_param, y, m, d))

                if not df_day.empty:
                    df_day['value'] = df_day['value'].fillna(0.0)
                    fig_comp.add_trace(
                        go.Scatter(x=df_day['time_interval'], y=df_day['value'], name=date_str, mode='lines',
                                   line=dict(dash='dot')))

                    max_val = df_day['value'].max()
                    max_time = df_day.loc[df_day['value'].idxmax(), 'time_interval']

                    if baseline_val is not None:
                        delta_mw = max_val - baseline_val
                        delta_pct = (delta_mw / baseline_val) * 100 if baseline_val != 0 else 0
                    else:
                        delta_mw = 0.0
                        delta_pct = 0.0

                    metrics_data.append({
                        "Target Date": date_str,
                        "Peak (MW)": max_val,
                        "Time": max_time,
                        "Delta (MW)": delta_mw,
                        "Variance (%)": delta_pct
                    })

            if metrics_data:
                fig_comp = update_chart_layout(fig_comp, f"Date Comparison: {clean_param_name(comp_param)}",
                                               legend_orientation="h")
                st.plotly_chart(fig_comp, use_container_width=True)

                st.write("### 📈 Peak Comparison Analytics")
                res_df = pd.DataFrame(metrics_data)
                st.dataframe(
                    res_df.style.format({"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Variance (%)": "{:+.2f}%"}),
                    use_container_width=True)
        else:
            st.info("Select a parameter in the sidebar to begin comparison.")

    elif compare_mode == "📈 Parameters":
        st.subheader("📊 Parameter Overlay Studio")
        st.info(f"📍 **Analyzing Date:** {sidebar_date_str} (Controlled via primary left sidebar)")
        if 'params_avail' in locals() and params_avail.empty:
            st.warning("No data available for the selected baseline date.")
        elif 'selected_cats' in locals() and not selected_cats:
            st.warning("Please select at least one category in the sidebar to view parameters.")
        elif 'baseline_param' in locals() and baseline_param:
            fig_comp2 = go.Figure()
            metrics_data2 = []

            df_base = run_query(
                "SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?",
                (baseline_param, selected_year, selected_month, selected_day))

            baseline_val = None
            if not df_base.empty:
                df_base['value'] = df_base['value'].fillna(0.0)
                clean_base = clean_param_name(baseline_param)
                fig_comp2.add_trace(
                    go.Scatter(x=df_base['time_interval'], y=df_base['value'], name=f"BASELINE: {clean_base}",
                               mode='lines', line=dict(width=3, color='#1e1e1e')))

                baseline_val = df_base['value'].max()
                max_time_base = df_base.loc[df_base['value'].idxmax(), 'time_interval']

                metrics_data2.append({
                    "Parameter": f"BASELINE: {clean_base}",
                    "Peak (MW)": baseline_val,
                    "Time": max_time_base,
                    "Delta (MW)": 0.0,
                    "Variance (%)": 0.0
                })

            for param in selected_comp_params:
                df_p = run_query(
                    "SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?",
                    (param, selected_year, selected_month, selected_day))

                if not df_p.empty:
                    df_p['value'] = df_p['value'].fillna(0.0)
                    clean_name = clean_param_name(param)
                    fig_comp2.add_trace(
                        go.Scatter(x=df_p['time_interval'], y=df_p['value'], name=clean_name, mode='lines',
                                   line=dict(dash='dot')))

                    max_val = df_p['value'].max()
                    max_time = df_p.loc[df_p['value'].idxmax(), 'time_interval']

                    if baseline_val is not None:
                        delta_mw = max_val - baseline_val
                        delta_pct = (delta_mw / baseline_val) * 100 if baseline_val != 0 else 0
                    else:
                        delta_mw = 0.0
                        delta_pct = 0.0

                    metrics_data2.append({
                        "Parameter": clean_name,
                        "Peak (MW)": max_val,
                        "Time": max_time,
                        "Delta (MW)": delta_mw,
                        "Variance (%)": delta_pct
                    })

            if metrics_data2:
                fig_comp2 = update_chart_layout(fig_comp2, "Parameter Overlay Analytics", legend_orientation="h")
                st.plotly_chart(fig_comp2, use_container_width=True)

                st.write("### 📈 Peak Comparison Analytics")
                st.dataframe(pd.DataFrame(metrics_data2).style.format(
                    {"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Variance (%)": "{:+.2f}%"}),
                    use_container_width=True)

    elif compare_mode == "📅 Daily Peak/Load Across Months":
        st.subheader("📊 Overlay: Daily Peak Trends Across Months")
        st.markdown(
            "This graph plots the peak power reached on **each individual day** (1 through 32), allowing you to visually overlay the daily peak curve of entire months on top of each other.")

        if 'comp_param' in locals() and comp_param and selected_comp_months:
            fig_comp3 = go.Figure()
            metrics_data3 = []
            baseline_val = None

            for i, ym_str in enumerate(selected_comp_months):
                y, m = map(int, ym_str.split('/'))

                # DB Filter: Added nepali_day > 0
                df_month = run_query('''
                    SELECT nepali_day, time_interval, value 
                    FROM system_log_data 
                    WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0
                ''', (comp_param, y, m))

                if not df_month.empty:
                    df_month['value'] = df_month['value'].fillna(0.0)

                    # Find the absolute max value for each individual day
                    idx_peaks = df_month.groupby('nepali_day')['value'].idxmax()
                    df_peaks = df_month.loc[idx_peaks].sort_values('nepali_day')

                    line_name = f"BASELINE: {ym_str}" if i == 0 else ym_str
                    line_dict = dict(width=3, color='#1e1e1e') if i == 0 else dict(dash='dot')

                    fig_comp3.add_trace(go.Scatter(
                        x=df_peaks['nepali_day'],
                        y=df_peaks['value'],
                        name=line_name,
                        mode='lines+markers',
                        line=line_dict,
                        customdata=df_peaks['time_interval'],
                        hovertemplate='<b>%{fullData.name}</b><br>Day %{x}<br>Peak: %{y:.2f} MW<br>Time: %{customdata}<extra></extra>'
                    ))

                    monthly_abs_peak = df_peaks['value'].max()
                    monthly_avg_peak = df_peaks['value'].mean()

                    if i == 0:
                        baseline_val = monthly_abs_peak
                        delta_mw, delta_pct = 0.0, 0.0
                    else:
                        if baseline_val is not None and baseline_val != 0:
                            delta_mw = monthly_abs_peak - baseline_val
                            delta_pct = (delta_mw / baseline_val) * 100
                        else:
                            delta_mw, delta_pct = 0.0, 0.0

                    metrics_data3.append({
                        "Month Overlay": line_name,
                        "Abs Monthly Peak (MW)": monthly_abs_peak,
                        "Avg Daily Peak (MW)": monthly_avg_peak,
                        "Abs Delta (MW)": delta_mw,
                        "Variance (%)": delta_pct
                    })

            if metrics_data3:
                fig_comp3 = update_chart_layout(fig_comp3,
                                                f"Day-by-Day Peak Curve Comparison: {clean_param_name(comp_param)}",
                                                xaxis_title="Day of the Month", legend_orientation="h")
                fig_comp3.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1))
                st.plotly_chart(fig_comp3, use_container_width=True)

                st.write("### 📈 Overlay Analytics")
                st.dataframe(pd.DataFrame(metrics_data3).style.format({
                    "Abs Monthly Peak (MW)": "{:.2f}",
                    "Avg Daily Peak (MW)": "{:.2f}",
                    "Abs Delta (MW)": "{:+.2f}",
                    "Variance (%)": "{:+.2f}%"
                }), use_container_width=True)
        else:
            st.info("Select a parameter and at least one month from the sidebar to begin comparison.")

    elif compare_mode == "📊 Monthly Peak/Load":
        st.subheader("📊 Macro-Trend: Absolute Peak Comparison")
        st.markdown(
            "This tool finds the single highest value recorded across an entire month, generating a simple bar chart to compare peak demand across seasons.")

        if 'comp_param' in locals() and comp_param and selected_comp_months:
            metrics_data4 = []
            baseline_val = None

            for i, ym_str in enumerate(selected_comp_months):
                y, m = map(int, ym_str.split('/'))

                # DB Filter: Added nepali_day > 0
                q_peak = '''
                    SELECT nepali_day, time_interval, value 
                    FROM system_log_data 
                    WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0
                    ORDER BY value DESC LIMIT 1
                '''
                df_peak = run_query(q_peak, (comp_param, y, m))

                if not df_peak.empty:
                    max_val = df_peak.iloc[0]['value']
                    peak_day = df_peak.iloc[0]['nepali_day']
                    peak_time = df_peak.iloc[0]['time_interval']

                    if i == 0:
                        baseline_val = max_val
                        delta_mw, delta_pct = 0.0, 0.0
                    else:
                        if baseline_val is not None:
                            delta_mw = max_val - baseline_val
                            delta_pct = (delta_mw / baseline_val) * 100 if baseline_val != 0 else 0
                        else:
                            delta_mw, delta_pct = 0.0, 0.0

                    metrics_data4.append({
                        "Target Month": ym_str + " (Baseline)" if i == 0 else ym_str,
                        "Peak (MW)": max_val,
                        "Day of Peak": peak_day,
                        "Time of Peak": peak_time,
                        "Delta (MW)": delta_mw,
                        "Variance (%)": delta_pct
                    })

            if metrics_data4:
                res_df4 = pd.DataFrame(metrics_data4)

                fig_bar = px.bar(
                    res_df4,
                    x="Target Month",
                    y="Peak (MW)",
                    text="Peak (MW)",
                    color="Target Month",
                    title=f"Absolute Monthly Peaks: {clean_param_name(comp_param)}",
                    labels={"Peak (MW)": "Maximum Power (MW)"},
                    color_discrete_sequence=['#0052cc', '#33b2df', '#ff3366', '#fdbb2d']
                )
                fig_bar.update_traces(texttemplate='%{text:.2f} MW', textposition='outside')
                fig_bar.update_layout(
                    template="plotly_white", showlegend=False,
                    yaxis=dict(range=[0, res_df4['Peak (MW)'].max() * 1.15], showgrid=True, gridcolor='#f0f2f6'),
                    plot_bgcolor='rgba(255,255,255,1)', paper_bgcolor='rgba(0,0,0,0)',
                    margin=dict(l=10, r=10, t=50, b=20)
                )

                st.plotly_chart(fig_bar, use_container_width=True)

                st.write("### 📈 Monthly Peak Analytics")
                st.dataframe(res_df4.style.format({
                    "Peak (MW)": "{:.2f}",
                    "Delta (MW)": "{:+.2f}",
                    "Variance (%)": "{:+.2f}%"
                }), use_container_width=True)
            else:
                st.warning("No peak data could be calculated for the selected months.")
        else:
            st.info("Select a parameter and at least one month from the sidebar to begin comparison.")
