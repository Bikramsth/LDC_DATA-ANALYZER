import os
import re
import time
import sqlite3
import threading
import io
import datetime
import zipfile
import shutil
import queue
import gc
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, Reference

# ==========================================
# 1. CONFIGURATION & DATABASE SETUP
# ==========================================
FOLDER_TO_WATCH = "./LDC_Data"
ERROR_FOLDER = "./LDC_Data_Errors"
BACKUP_FOLDER = "./LDC_Backups"
DATABASE_NAME = "NEA_LDC_Database.db"

NEA_PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']

file_queue = queue.Queue()

@st.cache_resource
def init_db():
    max_retries = 5
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
            cursor = conn.cursor()
            
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.execute('PRAGMA synchronous=NORMAL;')
            cursor.execute('PRAGMA cache_size=-64000;')
            cursor.execute('PRAGMA temp_store=MEMORY;')
            cursor.execute('PRAGMA mmap_size=2147483648;')

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

            updates = {
                "Total IPP": "SUMMARY_TOTAL_IPP", "TOTAL IPP": "SUMMARY_TOTAL_IPP",
                "Total NEA SUBSIDIARIES": "SUMMARY_TOTAL_NEA_SUBS", "TOTAL NEA SUBSIDIARIES": "SUMMARY_TOTAL_NEA_SUBS",
                "Total ROR": "SUMMARY_TOTAL_ROR", "TOTAL ROR": "SUMMARY_TOTAL_ROR",
                "Total STORGE": "SUMMARY_TOTAL_STORAGE", "Total Storage": "SUMMARY_TOTAL_STORAGE", "TOTAL STORAGE": "SUMMARY_TOTAL_STORAGE",
                "Total IMPORT": "SUMMARY_TOTAL_IMPORT", "TOTAL IMPORT": "SUMMARY_TOTAL_IMPORT",
                "Total EXPORT": "SUMMARY_TOTAL_EXPORT", "TOTAL EXPORT": "SUMMARY_TOTAL_EXPORT"
            }
            for old_name, new_name in updates.items():
                cursor.execute("UPDATE OR IGNORE system_log_data SET parameter_name = ? WHERE parameter_name = ?", (new_name, old_name))
                cursor.execute("DELETE FROM system_log_data WHERE parameter_name = ?", (old_name,))

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

def backup_database():
    if not os.path.exists(BACKUP_FOLDER):
        os.makedirs(BACKUP_FOLDER)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_FOLDER, f"NEA_LDC_Backup_{timestamp}.db")
    
    try:
        source_conn = sqlite3.connect(DATABASE_NAME)
        backup_conn = sqlite3.connect(backup_file)
        with backup_conn:
            source_conn.backup(backup_conn, pages=250, sleep=0.1)
        source_conn.close()
        backup_conn.close()
        return True, f"✅ Safe backup created: {backup_file}"
    except Exception as e:
        return False, f"❌ Backup failed: {e}"

def automated_backup_worker():
    while True:
        time.sleep(86400) 
        try: backup_database()
        except Exception: pass

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
        if m_after: month = int(m_after.group(1))
        elif m_before: month = int(m_before.group(1))
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
                    if 0 < num <= 24: current_time_cols[col_idx] = f"{int(num):02d}:{int(round((num - int(num)) * 60)):02d}:00"
                except: pass
        if len(current_time_cols) >= 20:
            time_columns = current_time_cols
            header_row_index = idx
            break

    if header_row_index == -1: return 0

    daily_data = {time_interval: {} for time_interval in time_columns.values()}

    for row_idx in range(header_row_index + 1, len(df)):
        row = df.iloc[row_idx]
        if pd.isna(row[0]) or str(row[0]).strip() == "": continue

        raw_name = str(row[0]).strip()
        upper_name = re.sub(r'\s+', ' ', raw_name.upper())

        if "TOTAL IPP" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_IPP", "NEA_SUB_ZONE"
        elif "TOTAL NEA SUB" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_NEA_SUBS", "ROR_ZONE"
        elif "TOTAL ROR" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_ROR", "STORAGE_ZONE"
        elif "TOTAL STORGE" in upper_name or "TOTAL STORAGE" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_STORAGE", "IMPORT_ZONE"
        elif "TOTAL IMPORT" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_IMPORT", "AFTER_IMPORT"
        elif "TOTAL NATIONAL LOAD" in upper_name or ("NATIONAL LOAD" in upper_name and "TOTAL" not in upper_name):
            current_block = "EXPORT_ZONE"
            continue  
        elif "TOTAL EXPORT" in upper_name: db_param_name, current_block = "SUMMARY_TOTAL_EXPORT", "FINAL_TOTALS"
        elif "SYSTEM LOAD" in upper_name:
            current_block = "FINAL_TOTALS"
            continue  
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
                    final_value = float(clean_val)
            except:
                final_value = 0.0
            daily_data[time_interval][db_param_name] = final_value

    bulk_insert_data = []

    for time_interval, params in daily_data.items():
        ipp = abs(params.get('SUMMARY_TOTAL_IPP', 0.0))
        nea = abs(params.get('SUMMARY_TOTAL_NEA_SUBS', 0.0))
        ror = abs(params.get('SUMMARY_TOTAL_ROR', 0.0))
        storage = abs(params.get('SUMMARY_TOTAL_STORAGE', 0.0))
        imp = abs(params.get('SUMMARY_TOTAL_IMPORT', 0.0))
        exp = abs(params.get('SUMMARY_TOTAL_EXPORT', 0.0))

        interruption = sum(abs(v) for k, v in params.items() if 'INTERRUPT' in k.upper() or 'TRIP' in k.upper())

        sys_actual = ipp + nea + ror + storage + imp
        nat_actual = sys_actual + exp - imp
        sys_expected = sys_actual + interruption

        params['TOTAL SYSTEM LOAD (ACTUAL)'] = sys_actual
        params['TOTAL SYSTEM LOAD (EXPECTED)'] = sys_expected
        params['TOTAL NATIONAL LOAD (ACTUAL)'] = nat_actual
        params['COMBINED_INTERRUPTION'] = interruption

        for p_name, val in params.items():
            bulk_insert_data.append((year, month, day, time_interval, p_name, val))
            rows_inserted += 1

    cursor.executemany('''
        INSERT INTO system_log_data (nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value)
        VALUES (?, ?, ?, ?, ?, ?) 
        ON CONFLICT(nepali_year, nepali_month, nepali_day, time_interval, parameter_name) 
        DO UPDATE SET value = excluded.value
    ''', bulk_insert_data)

    return rows_inserted

def process_file(file_path):
    time.sleep(1)
    filename = os.path.basename(file_path)
    if not os.path.exists(ERROR_FOLDER): os.makedirs(ERROR_FOLDER)
    if not filename.lower().endswith(('.xlsx', '.xls', '.csv')) or filename.startswith('~'): return

    year, month = parse_filename(filename)
    if not year or not month: return

    try:
        mtime = os.path.getmtime(file_path)
        conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
        cursor = conn.cursor()

        if cursor.execute("SELECT mtime FROM processed_files WHERE filename = ?", (file_path,)).fetchone() == (mtime,):
            conn.close()
            return

        file_bytes = None
        for _ in range(3):
            try:
                with open(file_path, 'rb') as f: file_bytes = f.read()
                break
            except PermissionError: time.sleep(1)

        if not file_bytes: return
        file_io, rows_inserted = io.BytesIO(file_bytes), 0

        try:
            if filename.lower().endswith('.csv'):
                day_match = re.search(r'-\s*(\d+)\.csv$', filename.lower())
                day = int(day_match.group(1)) if day_match else None
                if day is not None and day > 0:  
                    df = pd.read_csv(file_io, header=None, on_bad_lines='skip')
                    rows_inserted += extract_data(df, year, month, day, cursor)
                    del df
            else:
                xl = pd.ExcelFile(file_io, engine='openpyxl')
                for sheet in xl.sheet_names:
                    if sheet.strip().isdigit():
                        day = int(sheet.strip())
                        if day > 0:  
                            rows_inserted += extract_data(xl.parse(sheet, header=None), year, month, day, cursor)
                del xl

        except zipfile.BadZipFile:
            raise ValueError("File is corrupted or password protected.")
        except Exception as parse_err:
            raise ValueError(f"Parsing failed: {str(parse_err)}")

        if rows_inserted > 0:
            cursor.execute("INSERT INTO processed_files (filename, mtime) VALUES (?, ?) ON CONFLICT(filename) DO UPDATE SET mtime=excluded.mtime", (file_path, mtime))
            conn.commit()
            st.cache_data.clear()

    except ValueError as ve:
        try: shutil.move(file_path, os.path.join(ERROR_FOLDER, filename))
        except Exception: pass
    except Exception: pass
    finally:
        if 'conn' in locals(): conn.close()

# ==========================================
# 3. BACKGROUND FOLDER MONITOR
# ==========================================
def process_queue_worker():
    while True:
        file_path = file_queue.get()
        try:
            process_file(file_path)
            gc.collect()
        except Exception: pass
        finally: file_queue.task_done()

class FileWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')): file_queue.put(event.src_path)
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.xlsx', '.xls', '.csv')): file_queue.put(event.src_path)

@st.cache_resource
def start_background_monitor():
    if not os.path.exists(FOLDER_TO_WATCH): os.makedirs(FOLDER_TO_WATCH)
    threading.Thread(target=process_queue_worker, daemon=True).start()
    threading.Thread(target=automated_backup_worker, daemon=True).start()
    event_handler = FileWatcher()
    observer = Observer()
    observer.schedule(event_handler, FOLDER_TO_WATCH, recursive=True)
    threading.Thread(target=observer.start, daemon=True).start()
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
        if p_up.startswith('ZONE_IPP_'): cat = "🔌 IPPs"
        elif p_up.startswith('ZONE_ROR_'): cat = "🌊 RORs"
        elif p_up.startswith('ZONE_STORAGE_'): cat = "🔋 Storage"
        elif p_up.startswith('ZONE_NEASUB_'): cat = "🏢 NEA Subs"
        elif p_up.startswith('ZONE_IMPORT_'): cat = "⬇️ Import"
        elif p_up.startswith('ZONE_EXPORT_'): cat = "⬆️ Export"
        elif 'SUMMARY' in p_up or 'TOTAL' in p_up or 'LOAD' in p_up or 'INTERRUPTION' in p_up: cat = "📊 System Totals"
        else: cat = "📁 Other"
        categories.setdefault(cat, []).append(p)
    return categories

def clean_param_name(p):
    return p.replace('SUMMARY_TOTAL_IPP', 'Total IPP').replace('SUMMARY_TOTAL_NEA_SUBS', 'Total NEA Subsidiaries').replace('SUMMARY_TOTAL_ROR', 'Total ROR').replace('SUMMARY_TOTAL_STORAGE', 'Total Storage').replace('SUMMARY_TOTAL_IMPORT', 'Total IMPORT').replace('SUMMARY_TOTAL_EXPORT', 'Total EXPORT').replace('COMBINED_INTERRUPTION', 'Interruption / Tripping').replace('ZONE_IPP_', 'IPP: ').replace('ZONE_ROR_', 'ROR: ').replace('ZONE_STORAGE_', 'STORAGE: ').replace('ZONE_NEASUB_', 'SUB: ').replace('ZONE_IMPORT_', 'IMPORT: ').replace('ZONE_EXPORT_', 'EXPORT: ')

@st.cache_data(show_spinner=False)
def convert_df(df): return df.to_csv(index=False).encode('utf-8')

def update_chart_layout(fig, title, yaxis_title="Power (MW)", xaxis_title="Time", legend_orientation="v"):
    legend_settings = dict(orientation="v", y=1, x=1.02)
    fig.update_layout(
        title=title, hovermode="x unified", template="plotly_white",
        plot_bgcolor='rgba(255,255,255,1)', paper_bgcolor='rgba(0,0,0,0)',
        xaxis=dict(title=xaxis_title, type="category", showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec', tickangle=-45, automargin=True),
        yaxis=dict(title=yaxis_title, showgrid=True, gridcolor='#f0f2f6', linecolor='#e0e5ec', automargin=True),
        legend=legend_settings, margin=dict(l=10, r=150, t=50, b=60)
    )
    return fig

def draw_stacked_chart(df_pivot, cols, title, total_col=None, show_export=False, export_col=None, stack=True, split_signs=False):
    fig = go.Figure()
    
    for i, col in enumerate(cols):
        base_color = NEA_PALETTE[i % len(NEA_PALETTE)]
        
        if stack:
            fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[col], name=clean_param_name(col), stackgroup='one', line=dict(width=0.5, color=base_color)))
        else:
            if split_signs:
                fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[col], name=f"{clean_param_name(col)}", mode='lines', line=dict(width=2, color=base_color)))
                if (df_pivot[col] < 0).any():
                    neg_series = df_pivot[col].clip(upper=0)
                    fig.add_trace(go.Scatter(
                        x=df_pivot.index, 
                        y=neg_series, 
                        name=f"{clean_param_name(col)} (Exporting)", 
                        mode='lines', 
                        line=dict(width=2, color='#d62728'),
                        fill='tozeroy',
                        fillcolor='rgba(214, 39, 40, 0.2)',
                        showlegend=False
                    ))
            else:
                fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[col], name=clean_param_name(col), mode='lines', line=dict(width=2, color=base_color)))
    
    if show_export and export_col and export_col in df_pivot.columns:
        if stack:
            fig.add_trace(go.Scatter(x=df_pivot.index, y=-df_pivot[export_col].abs(), name='Total EXPORT', stackgroup='two', line=dict(width=0.5, color='#d62728')))
        else:
            fig.add_trace(go.Scatter(x=df_pivot.index, y=-df_pivot[export_col].abs(), name='Total EXPORT', mode='lines', line=dict(width=2, color='#d62728')))
    
    if total_col and total_col in df_pivot.columns:
        fig.add_trace(go.Scatter(x=df_pivot.index, y=df_pivot[total_col], name='TOTAL', line=dict(color='#1e1e1e', width=3)))

    fig = update_chart_layout(fig, title, legend_orientation="v")
    
    if not stack or split_signs:
        fig.add_hline(y=0, line_width=1.5, line_color="#1e1e1e", opacity=0.8)
        
    st.plotly_chart(fig, use_container_width=True)

@st.cache_data(show_spinner=False)
def generate_nea_excel_report(df_piv, df_all, date_str, metrics_dict, val_comps):
    output = io.BytesIO()
    num_intervals = len(df_piv)
    interval_hrs = 24.0 / num_intervals if num_intervals > 0 else 1.0

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        wb = writer.book
        ws_sum = wb.create_sheet(title="Executive_Summary", index=0)

        display_cols = (['TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL SYSTEM LOAD (EXPECTED)', 'TOTAL NATIONAL LOAD (ACTUAL)']
                        + val_comps + ['SUMMARY_TOTAL_EXPORT', 'COMBINED_INTERRUPTION'])
        display_cols = list(dict.fromkeys([c for c in display_cols if c in df_piv.columns]))
        
        df_export = df_piv[display_cols].copy()
        df_export.columns = [clean_param_name(c) for c in df_export.columns]
        df_export.index.name = "Time Interval"
        df_export.reset_index().to_excel(writer, sheet_name="Hourly_Load_Log", index=False)
        ws_log = wb["Hourly_Load_Log"]

        def write_zone_matrix_sheet(prefix, sheet_name):
            df_zone = df_all[df_all['parameter_name'].str.startswith(prefix)].copy()
            if df_zone.empty:
                pd.DataFrame({"Message": [f"No data."]}).to_excel(writer, sheet_name=sheet_name, index=False)
                return
            df_zone['MW'] = df_zone['MW'].abs()
            df_p = df_zone.pivot(index='Time', columns='parameter_name', values='MW').fillna(0.0)
            df_p.columns = [clean_param_name(c) for c in df_p.columns]
            df_p['TOTAL_ZONE_MW'] = df_p.sum(axis=1)
            df_p.reset_index().to_excel(writer, sheet_name=sheet_name, index=False)

        write_zone_matrix_sheet('ZONE_IPP_', 'IPP_Generation_Matrix')
        write_zone_matrix_sheet('ZONE_ROR_', 'ROR_Generation_Matrix')
        ws_ipp, ws_ror = wb['IPP_Generation_Matrix'], wb['ROR_Generation_Matrix']

        nea_blue, sub_header_fill = PatternFill(start_color="0052CC", end_color="0052CC", fill_type="solid"), PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
        zebra_fill, card_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid"), PatternFill(start_color="F4F7F6", end_color="F4F7F6", fill_type="solid")

        font_title, font_subtitle = Font(name="Segoe UI", size=16, bold=True, color="FFFFFF"), Font(name="Segoe UI", size=11, italic=True, color="E0E5EC")
        font_sec_hdr, font_tbl_hdr = Font(name="Segoe UI", size=11, bold=True, color="0052CC"), Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
        font_bold, font_regular = Font(name="Segoe UI", size=10, bold=True, color="1E1E1E"), Font(name="Segoe UI", size=10, color="1E1E1E")

        border_cell = Border(left=Side(border_style="thin", color="D0D7DE"), right=Side(border_style="thin", color="D0D7DE"), top=Side(border_style="thin", color="D0D7DE"), bottom=Side(border_style="thin", color="D0D7DE"))
        border_kpi = Border(left=Side(border_style="thick", color="0052CC"), right=Side(border_style="thin", color="D0D7DE"), top=Side(border_style="thin", color="D0D7DE"), bottom=Side(border_style="thin", color="D0D7DE"))
        align_center, align_left, align_right = Alignment(horizontal="center", vertical="center", wrap_text=True), Alignment(horizontal="left", vertical="center"), Alignment(horizontal="right", vertical="center")

        ws_sum.views.sheetView[0].showGridLines = True
        ws_sum.merge_cells("A1:I1")
        ws_sum["A1"], ws_sum["A1"].font, ws_sum["A1"].fill, ws_sum["A1"].alignment = "⚡ NEPAL ELECTRICITY AUTHORITY - LOAD DISPATCH CENTRE (LDC)", font_title, nea_blue, align_center
        ws_sum.row_dimensions[1].height = 32

        ws_sum.merge_cells("A2:I2")
        ws_sum["A2"], ws_sum["A2"].font, ws_sum["A2"].fill, ws_sum["A2"].alignment = f"Daily System Operation & Load Summary Report  |  Nepali Date: {date_str}", font_subtitle, nea_blue, align_center
        ws_sum.row_dimensions[2].height = 20

        ws_sum.merge_cells("A4:I4")
        ws_sum["A4"], ws_sum["A4"].font, ws_sum["A4"].fill = "1. KEY OPERATIONAL METRICS (DAILY PEAKS & LOAD FACTOR)", font_sec_hdr, sub_header_fill
        ws_sum.row_dimensions[4].height = 22

        kpi_headers = ["Metric Parameter", "Recorded Value", "Peak Time / Unit", "", "System Indicator", "Value", "Remarks"]
        for col_idx, text in enumerate(kpi_headers, start=1):
            if col_idx == 4: continue
            cell = ws_sum.cell(row=5, column=col_idx, value=text)
            cell.font, cell.fill, cell.border, cell.alignment = font_bold, card_fill, border_cell, align_center

        kpi_rows = [
            ("System Peak Load (Actual)", round(metrics_dict['sys_act_val'], 2), f"at {metrics_dict['sys_act_hr']} (MW)", "", "Total System Energy", round(metrics_dict['energy_mwh'] * interval_hrs, 2), "MWh (Integrated)"),
            ("System Peak Load (Expected)", round(metrics_dict['sys_exp_val'], 2), f"at {metrics_dict['sys_exp_hr']} (MW)", "", "Daily System Load Factor", round(metrics_dict['load_factor'], 2), "% Efficiency"),
            ("National Peak Load (Actual)", round(metrics_dict['nat_act_val'], 2), f"at {metrics_dict['nat_act_hr']} (MW)", "", "Max Recorded Interruption", round(df_piv['COMBINED_INTERRUPTION'].max(), 2), "MW Tripping/Shedding")
        ]

        for r_offset, row_data in enumerate(kpi_rows, start=6):
            ws_sum.row_dimensions[r_offset].height = 20
            for c_idx, val in enumerate(row_data, start=1):
                if c_idx == 4: continue
                cell = ws_sum.cell(row=r_offset, column=c_idx, value=val)
                cell.border = border_kpi if c_idx in (1, 5) else border_cell
                if c_idx in (1, 5): cell.font, cell.alignment = font_bold, align_left
                elif c_idx in (2, 6): cell.font, cell.number_format, cell.alignment = font_bold, "#,##0.00", align_right
                else: cell.font, cell.alignment = font_regular, align_center

        ws_sum.merge_cells("A10:G10")
        ws_sum["A10"], ws_sum["A10"].font, ws_sum["A10"].fill = "2. GENERATION, TRADE & LOAD BREAKDOWN SUMMARY", font_sec_hdr, sub_header_fill
        ws_sum.row_dimensions[10].height = 22

        tbl_headers = ["Component / Parameter", "Max Peak (MW)", "Peak Time", "Min (MW)", "Average (MW)", "Daily Energy (MWh)", "Share of Sys Load (%)"]
        ws_sum.row_dimensions[11].height = 24
        for c_idx, h_text in enumerate(tbl_headers, start=1):
            cell = ws_sum.cell(row=11, column=c_idx, value=h_text)
            cell.font, cell.fill, cell.alignment, cell.border = font_tbl_hdr, nea_blue, align_center, border_cell

        total_sys_energy = (df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].sum() * interval_hrs) or 1.0

        for idx, col_key in enumerate(display_cols, start=12):
            ws_sum.row_dimensions[idx].height = 19
            series = df_piv[col_key]
            c_name = clean_param_name(col_key)
            max_v, min_v, avg_v, eng_v = float(series.max()), float(series.min()), float(series.mean()), float(series.sum() * interval_hrs)
            max_t = str(series.idxmax()) if max_v > 0 else "-"
            share_v = (eng_v / total_sys_energy * 100.0) if col_key in val_comps else 0.0

            row_vals = [c_name, max_v, max_t, min_v, avg_v, eng_v, share_v if col_key in val_comps else "-"]
            is_total_row = "TOTAL" in col_key.upper()

            for c_idx, val in enumerate(row_vals, start=1):
                cell = ws_sum.cell(row=idx, column=c_idx, value=val)
                cell.font, cell.border = (font_bold if is_total_row else font_regular), border_cell
                cell.fill = sub_header_fill if is_total_row else (zebra_fill if idx % 2 == 1 else PatternFill(fill_type=None))
                if c_idx == 1: cell.alignment = align_left
                elif c_idx == 3: cell.alignment = align_center
                elif isinstance(val, (int, float)): cell.number_format, cell.alignment = "#,##0.00", align_right
                else: cell.alignment = align_center

        def get_top_n_plants(prefix, n=5):
            df_zone = df_all[df_all['parameter_name'].str.startswith(prefix)].copy()
            if df_zone.empty: return []
            df_zone['MW'] = df_zone['MW'].abs()
            df_p = df_zone.pivot(index='Time', columns='parameter_name', values='MW').fillna(0.0)
            records = []
            for col in df_p.columns:
                s = df_p[col]
                pk_mw = float(s.max())
                if pk_mw > 0: records.append((col.replace(prefix, '').strip(), pk_mw, str(s.idxmax()), float(s.mean()), float(s.sum() * interval_hrs)))
            records.sort(key=lambda x: (x[1], x[4]), reverse=True)
            return records[:n]

        top_ipps, top_rors = get_top_n_plants('ZONE_IPP_', 5), get_top_n_plants('ZONE_ROR_', 5)
        sec3_start_row = 12 + len(display_cols) + 2
        ws_sum.merge_cells(start_row=sec3_start_row, start_column=1, end_row=sec3_start_row, end_column=9)
        ws_sum.cell(row=sec3_start_row, column=1, value="3. TOP 5 INDIVIDUAL IPP & ROR PLANTS BY PEAK GENERATION (MW)")
        ws_sum.cell(row=sec3_start_row, column=1).font, ws_sum.cell(row=sec3_start_row, column=1).fill = font_sec_hdr, sub_header_fill
        ws_sum.row_dimensions[sec3_start_row].height = 22

        hdr_row = sec3_start_row + 1
        ws_sum.row_dimensions[hdr_row].height = 24
        
        for idx_c, h_text in enumerate(["Rank", "Top 5 IPP Plants", "Peak (MW)", "Peak Time / Energy"], start=1):
            c = ws_sum.cell(row=hdr_row, column=idx_c, value=h_text)
            c.font, c.fill, c.alignment, c.border = font_tbl_hdr, nea_blue, align_center, border_cell

        for idx_c, h_text in enumerate(["Rank", "Top 5 ROR Plants (NEA)", "Peak (MW)", "Peak Time / Energy"], start=6):
            c = ws_sum.cell(row=hdr_row, column=idx_c, value=h_text)
            c.font, c.fill, c.alignment, c.border = font_tbl_hdr, nea_blue, align_center, border_cell

        for i in range(5):
            r_curr = hdr_row + 1 + i
            ws_sum.row_dimensions[r_curr].height = 20
            row_fill = zebra_fill if i % 2 == 1 else PatternFill(fill_type=None)

            ipp_vals = [f"#{i+1}", top_ipps[i][0], top_ipps[i][1], f"{top_ipps[i][2]} ({top_ipps[i][4]:,.1f} MWh)"] if i < len(top_ipps) else [f"#{i+1}", "N/A", 0.0, "-"]
            for c_offset, val in enumerate(ipp_vals, start=1):
                cell = ws_sum.cell(row=r_curr, column=c_offset, value=val)
                cell.font, cell.fill, cell.border = (font_bold if c_offset in (1, 3) else font_regular), row_fill, border_cell
                if c_offset == 1: cell.alignment = align_center
                elif c_offset == 2: cell.alignment = align_left
                elif c_offset == 3: cell.number_format, cell.alignment = "#,##0.00", align_right
                else: cell.alignment = align_center

            ror_vals = [f"#{i+1}", top_rors[i][0], top_rors[i][1], f"{top_rors[i][2]} ({top_rors[i][4]:,.1f} MWh)"] if i < len(top_rors) else [f"#{i+1}", "N/A", 0.0, "-"]
            for c_offset, val in enumerate(ror_vals, start=6):
                cell = ws_sum.cell(row=r_curr, column=c_offset, value=val)
                cell.font, cell.fill, cell.border = (font_bold if c_offset in (6, 8) else font_regular), row_fill, border_cell
                if c_offset == 6: cell.alignment = align_center
                elif c_offset == 7: cell.alignment = align_left
                elif c_offset == 8: cell.number_format, cell.alignment = "#,##0.00", align_right
                else: cell.alignment = align_center

        for ws in [ws_log, ws_ipp, ws_ror]:
            ws.views.sheetView[0].showGridLines = True
            ws.freeze_panes = "B2"
            ws.row_dimensions[1].height = 25
            for c_idx in range(1, ws.max_column + 1):
                cell = ws.cell(row=1, column=c_idx)
                cell.font, cell.fill, cell.alignment, cell.border = font_tbl_hdr, nea_blue, align_center, border_cell
            for r_idx in range(2, ws.max_row + 1):
                ws.row_dimensions[r_idx].height = 18
                for c_idx in range(1, ws.max_column + 1):
                    cell = ws.cell(row=r_idx, column=c_idx)
                    cell.font, cell.border = font_regular, border_cell
                    if r_idx % 2 == 1: cell.fill = zebra_fill
                    if c_idx == 1: cell.alignment = align_center
                    else: cell.number_format, cell.alignment = "#,##0.00", align_right

        chart = LineChart()
        chart.title, chart.style, chart.y_axis.title, chart.x_axis.title = f"24-Hour System & National Load Curve ({date_str})", 10, "Power (MW)", "Time Interval"
        chart.width, chart.height = 26, 12

        data_ref = Reference(ws_log, min_col=2, min_row=1, max_col=min(4, ws_log.max_column), max_row=ws_log.max_row)
        cats_ref = Reference(ws_log, min_col=1, min_row=2, max_row=ws_log.max_row)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        ws_sum.add_chart(chart, f"A{hdr_row + 7}")

        for ws in [ws_sum, ws_log, ws_ipp, ws_ror]:
            for col in ws.columns:
                ws.column_dimensions[get_column_letter(col[0].column)].width = max(max(len(str(c.value or "")) for c in col) + 4, 14)
        ws_sum.column_dimensions["E"].width = 4 

    return output.getvalue()


# ==========================================
# 5. STREAMLIT APP & UI STYLING
# ==========================================
st.set_page_config(page_title="NEA LDC Dashboard", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
    <style>
        .stApp { background-color: #f4f7f6; color: #1e1e1e; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        div[data-testid="metric-container"] {
            background-color: #ffffff; border: 1px solid #e0e5ec; padding: 15px 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05); border-left: 5px solid #0052cc;
        }
        div[data-testid="stMetricLabel"] { font-weight: 600; color: #5e6e82; }
        div[data-testid="stMetricValue"] { color: #0f172a; font-weight: 700; font-size: 2rem !important; }
        .stTabs [data-baseweb="tab-list"] { gap: 15px; }
        .stTabs [data-baseweb="tab"] { height: 50px; background-color: #ffffff; border-radius: 8px 8px 0px 0px; padding: 10px 20px; border: 1px solid #e0e5ec; border-bottom: none; }
        .stTabs [aria-selected="true"] { background-color: #e8f0fe; color: #0052cc !important; border-bottom: 3px solid #0052cc !important; font-weight: bold; }
        [data-testid="stSidebar"] { background-color: #ffffff; border-right: 1px solid #e0e5ec; }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ Nepal Electricity Authority - LDC Dashboard")

dates_df = run_query("SELECT DISTINCT nepali_year, nepali_month, nepali_day FROM system_log_data WHERE nepali_day > 0")
if dates_df.empty:
    st.warning("Database is empty.")
    if st.button("🚀 Force Manual Scan", use_container_width=True):
        with st.status("Scanning...", expanded=True):
            for r, _, fs in os.walk(FOLDER_TO_WATCH):
                for f in fs:
                    if f.endswith(('.xlsx', '.xls', '.csv')) and not f.startswith('~'): process_file(os.path.join(r, f))
        time.sleep(1)
        st.rerun()
    st.stop()

# ==========================================
# 6. SIDEBAR CONTROLS
# ==========================================
with st.sidebar:
    pending_files = file_queue.qsize()
    if pending_files > 0:
        st.warning(f"⏳ **Processing Backlog:** {pending_files} files remaining...")
        time.sleep(2)
        st.rerun()
    else:
        st.success("✅ **System Sync:** Up to date")

    if st.button("🚀 Force Rescan Files", use_container_width=True):
        conn = sqlite3.connect(DATABASE_NAME, timeout=30.0)
        conn.execute("DELETE FROM processed_files")
        conn.commit()
        conn.close()
        with st.status("Scanning all Sub-folders..."):
            for r, _, fs in os.walk(FOLDER_TO_WATCH):
                for f in fs:
                    if f.endswith(('.xlsx', '.xls', '.csv')) and not f.startswith('~'): process_file(os.path.join(r, f))
        st.cache_data.clear()
        st.rerun()

    if st.button("💾 Create Database Backup", use_container_width=True):
        with st.spinner("Creating safe snapshot..."):
            succ, msg = backup_database()
            if succ: st.success(msg)
            else: st.error(msg)

    st.header("📅 Primary Date Filter")
    dates_df = dates_df.sort_values(by=['nepali_year', 'nepali_month', 'nepali_day'], ascending=[False, False, False])
    selected_year = st.selectbox("Year", dates_df['nepali_year'].unique().tolist())
    selected_month = st.selectbox("Month", dates_df[dates_df['nepali_year'] == selected_year]['nepali_month'].unique().tolist())
    selected_day = st.selectbox("Day", dates_df[(dates_df['nepali_year'] == selected_year) & (dates_df['nepali_month'] == selected_month)]['nepali_day'].unique().tolist())
    sidebar_date_str = f"{selected_year}/{str(selected_month).zfill(2)}/{str(selected_day).zfill(2)}"

    st.divider()
    st.header("📊 Comparison Studio")
    compare_mode = st.radio("Strategy:", ["🗓️ Date-wise", "📈 Parameters", "📅 Daily Peak/Load Across Months", "📊 Monthly Peak/Load"])
    comp_param, selected_comp_days, baseline_param, selected_comp_params, selected_comp_months = None, [], None, [], []

    all_p_df = run_query("SELECT DISTINCT parameter_name FROM system_log_data ORDER BY parameter_name")
    if not all_p_df.empty:
        cat_dict = categorize_params(all_p_df['parameter_name'].tolist())
        default_cat = [k for k in cat_dict.keys() if "Totals" in k] or sorted(list(cat_dict.keys()))[:1]
        sel_cats = st.multiselect("Categories", sorted(list(cat_dict.keys())), default=default_cat)
        if sel_cats:
            filt_p = sorted([p for c in sel_cats for p in cat_dict[c]])
            if compare_mode in ["🗓️ Date-wise", "📅 Daily Peak/Load Across Months", "📊 Monthly Peak/Load"]:
                comp_param = st.selectbox("Parameter", filt_p, format_func=clean_param_name)
                if compare_mode == "🗓️ Date-wise":
                    d_df = run_query("SELECT DISTINCT nepali_year, nepali_month, nepali_day FROM system_log_data WHERE parameter_name=? AND nepali_day > 0 ORDER BY nepali_year DESC, nepali_month DESC, nepali_day DESC", (comp_param,))
                    selected_comp_days = st.multiselect("Overlay Dates", [f"{y}/{str(m).zfill(2)}/{str(d).zfill(2)}" for y, m, d in zip(d_df.nepali_year, d_df.nepali_month, d_df.nepali_day) if f"{y}/{str(m).zfill(2)}/{str(d).zfill(2)}" != sidebar_date_str])
                else:
                    ym_df = run_query("SELECT DISTINCT nepali_year, nepali_month FROM system_log_data WHERE parameter_name=? AND nepali_day > 0 ORDER BY nepali_year DESC, nepali_month DESC", (comp_param,))
                    ym_list = [f"{y}/{str(m).zfill(2)}" for y, m in zip(ym_df.nepali_year, ym_df.nepali_month)]
                    selected_comp_months = st.multiselect("Compare Months", ym_list, default=ym_list[:2] if len(ym_list) >= 2 else ym_list)
            elif compare_mode == "📈 Parameters":
                baseline_param = st.selectbox("Baseline Param", filt_p, format_func=clean_param_name)
                selected_comp_params = st.multiselect("Overlay Params", [p for p in filt_p if p != baseline_param], format_func=clean_param_name)

# ==========================================
# 7. MAIN UI TABS
# ==========================================
t1, t2, t3, t4 = st.tabs(["⚡ Daily Operations", "📆 Monthly Peak Load Graph", "📈 Historical Monthly Peak Load", "🔍 Comparison Studio"])

with t1:
    st1, st2, st3, st4, st5, st6 = st.tabs(["Main System", "Total IMPORT", "Total NEA SUBSIDIARIES", "Total IPP", "Total ROR", "Total STORAGE"])
    
    if selected_day:
        df_all = run_query("SELECT time_interval as Time, parameter_name, value as MW FROM system_log_data WHERE nepali_year=? AND nepali_month=? AND nepali_day=? ORDER BY time_interval ASC", (selected_year, selected_month, selected_day))
        
        if not df_all.empty:
            with st1:
                req_p = ['SUMMARY_TOTAL_IPP', 'SUMMARY_TOTAL_NEA_SUBS', 'SUMMARY_TOTAL_ROR', 'SUMMARY_TOTAL_STORAGE', 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT', 'TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL SYSTEM LOAD (EXPECTED)', 'TOTAL NATIONAL LOAD (ACTUAL)', 'COMBINED_INTERRUPTION']
                df_piv = df_all[df_all['parameter_name'].isin(req_p)].pivot(index='Time', columns='parameter_name', values='MW').fillna(0.0).sort_index()
                
                for col in req_p:
                    if col not in df_piv.columns: df_piv[col] = 0.0
                    else: df_piv[col] = df_piv[col].abs()

                df_piv['Net Import/Export'] = df_piv['SUMMARY_TOTAL_IMPORT'] - df_piv['SUMMARY_TOTAL_EXPORT'].abs()
                supply_cols = ['SUMMARY_TOTAL_IPP', 'SUMMARY_TOTAL_NEA_SUBS', 'SUMMARY_TOTAL_ROR', 'SUMMARY_TOTAL_STORAGE', 'Net Import/Export']
                val_comps = [col for col in supply_cols if df_piv[col].abs().sum() > 0 or 'STORAGE' in col]

                sys_exp_peak_hour, sys_exp_peak_val = df_piv['TOTAL SYSTEM LOAD (EXPECTED)'].idxmax(), df_piv['TOTAL SYSTEM LOAD (EXPECTED)'].max()

                df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] = df_piv['TOTAL NATIONAL LOAD (ACTUAL)'] + df_piv['Net Import/Export'].abs()

                sys_act_peak_hour, sys_act_peak_val = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].idxmax(), df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].max()
                nat_act_peak_hour, nat_act_peak_val = df_piv['TOTAL NATIONAL LOAD (ACTUAL)'].idxmax(), df_piv['TOTAL NATIONAL LOAD (ACTUAL)'].max()
                total_energy_mwh, load_factor = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].sum(), (df_piv['TOTAL SYSTEM LOAD (ACTUAL)'].mean() / sys_act_peak_val * 100) if sys_act_peak_val > 0 else 0

                st.markdown("<br>", unsafe_allow_html=True)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric(f"📈 Sys Load Actual ({sys_act_peak_hour})", f"{sys_act_peak_val:.2f} MW")
                m2.metric(f"📊 Nat Load Actual ({nat_act_peak_hour})", f"{nat_act_peak_val:.2f} MW")
                m3.metric(f"⚡ Total Energy", f"{total_energy_mwh:,.0f} MWh")
                m4.metric(f"⚙️ Load Factor", f"{load_factor:.1f}%")
                
                report_metrics = {'sys_act_hr': sys_act_peak_hour, 'sys_act_val': sys_act_peak_val, 'sys_exp_hr': sys_exp_peak_hour, 'sys_exp_val': sys_exp_peak_val, 'nat_act_hr': nat_act_peak_hour, 'nat_act_val': nat_act_peak_val, 'energy_mwh': total_energy_mwh, 'load_factor': load_factor}
                btn_col1, btn_col2 = st.columns([1, 3])
                with btn_col1:
                    st.download_button("📄 Export NEA Daily Summary Report (.xlsx)", data=generate_nea_excel_report(df_piv, df_all, sidebar_date_str, report_metrics, val_comps), file_name=f"NEA_LDC_Daily_Summary_{sidebar_date_str.replace('/', '-')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
                
                st.markdown("<br>", unsafe_allow_html=True)
                fig_main = go.Figure()
                NEA_PALETTE_MAIN = ['#1f77b4', '#ff7f0e', '#2ca02c', '#9467bd', '#8c564b', '#e377c2']
                
                # 🚀 MAIN SYSTEM TAB: Handle plotting split for Net Import/Export perfectly onto positive/negative axes
                for i, col in enumerate(val_comps):
                    if col == 'Net Import/Export':
                        pos_vals = df_piv[col].clip(lower=0)
                        neg_vals = df_piv[col].clip(upper=0)
                        if pos_vals.sum() > 0:
                            fig_main.add_trace(go.Scatter(x=df_piv.index, y=pos_vals, name='Net Import', stackgroup='supply_stack', line=dict(width=0.5, color=NEA_PALETTE_MAIN[i % len(NEA_PALETTE_MAIN)]), hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'))
                        if neg_vals.abs().sum() > 0:
                            fig_main.add_trace(go.Scatter(x=df_piv.index, y=neg_vals, name='Net Export', stackgroup='export_stack', line=dict(width=0.5, color='#d62728'), hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'))
                    else:
                        fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv[col], name=clean_param_name(col), stackgroup='supply_stack', line=dict(width=0.5, color=NEA_PALETTE_MAIN[i % len(NEA_PALETTE_MAIN)]), hovertemplate='<b>%{fullData.name}</b>: %{y:.2f} MW<extra></extra>'))
                
                fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['TOTAL SYSTEM LOAD (ACTUAL)'], name='TOTAL SYSTEM LOAD (ACTUAL)', line=dict(color='#ff3366', width=2.5, dash='dot')))
                fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['TOTAL NATIONAL LOAD (ACTUAL)'], name='TOTAL NATIONAL LOAD (ACTUAL)', line=dict(color='#1e1e1e', width=3)))
                if df_piv['COMBINED_INTERRUPTION'].sum() > 0:
                    fig_main.add_trace(go.Scatter(x=df_piv.index, y=df_piv['COMBINED_INTERRUPTION'], name='Interruption/Tripping', line=dict(color='#ff7f0e', width=2)))

                fig_main = update_chart_layout(fig_main, "System Operation & Load Curve", legend_orientation="v")
                fig_main.add_hline(y=0, line_width=2, line_color="#1e1e1e")
                st.plotly_chart(fig_main, use_container_width=True)

                with st.expander("🔍 View Filtered Supply Components & Loads"):
                    display_cols = ['TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL NATIONAL LOAD (ACTUAL)'] + val_comps + ['COMBINED_INTERRUPTION']
                    st.dataframe(df_piv[display_cols].style.format("{:.2f}"), use_container_width=True)

            def render_sub(prefix, title, enable_plant_filter=False, use_abs=True, stack=True, split_signs=False, noise_filter=0.0):
                df_sub = df_all[df_all['parameter_name'].str.startswith(prefix)].copy()
                if not df_sub.empty:
                    df_sub['parameter_name'] = df_sub['parameter_name'].str.replace(prefix, '')
                    df_p = df_sub.pivot(index='Time', columns='parameter_name', values='MW').sort_index().interpolate(method='linear').fillna(0.0)
                    
                    if use_abs:
                        df_p = df_p.abs()
                    elif noise_filter > 0.0:
                        df_p = df_p.map(lambda x: 0.0 if abs(x) < noise_filter else x)
                        
                    v_cols = [c for c in df_p.columns if df_p[c].abs().sum() > 0]
                    
                    selected_plants = v_cols
                    if enable_plant_filter and len(v_cols) > 0:
                        st.markdown(f"**🔍 Filter Individual {title.split(':')[-1].strip()} Plants:**")
                        default_selection = v_cols[:5] if len(v_cols) > 5 else v_cols
                        selected_plants = st.multiselect(f"Select plants to display in chart:", options=v_cols, default=default_selection, key=f"filter_{prefix}")
                        if not selected_plants:
                            st.warning("Please select at least one plant to display.")
                            return

                    plot_cols = selected_plants if enable_plant_filter else v_cols
                    df_p['TOTAL'] = df_p[plot_cols].sum(axis=1)
                    
                    draw_stacked_chart(df_p, plot_cols, title, 'TOTAL', show_export=False, stack=stack, split_signs=split_signs)
                    
                    with st.expander(f"🔍 View & Export {title} Data"):
                        st.dataframe(df_p[plot_cols + ['TOTAL']].style.format("{:.2f}"), use_container_width=True)
                        st.download_button("📥 Download CSV", data=convert_df(df_p[plot_cols + ['TOTAL']].reset_index()), file_name=f"{prefix}_{sidebar_date_str.replace('/', '-')}.csv", mime="text/csv")
                else:
                    st.info(f"No {title} data found.")

            with st2:
                # 🚀 TOTAL IMPORT TAB: Plots raw negative and positive signs dynamically without enforcing anything.
                render_sub('ZONE_IMPORT_', 'Dynamic Breakdown: Import (Raw Excel Values)', enable_plant_filter=False, use_abs=False, stack=False, split_signs=False, noise_filter=0.0)
            with st3:
                render_sub('ZONE_NEASUB_', 'Dynamic Breakdown: NEA Subsidiaries', enable_plant_filter=False, use_abs=True, stack=True)
            with st4:
                render_sub('ZONE_IPP_', 'Dynamic Breakdown: Independent Power Producers (IPP)', enable_plant_filter=True, use_abs=True, stack=True)
            with st5:
                render_sub('ZONE_ROR_', 'Dynamic Breakdown: Run of River (ROR) Plants', enable_plant_filter=True, use_abs=True, stack=True)
            with st6:
                render_sub('ZONE_STORAGE_', 'Storage Plants', enable_plant_filter=False, use_abs=True, stack=True)

with t2:
    st.subheader(f"📊 Daily Peak Loads for Month {selected_month}, {selected_year}")
    query_monthly = "SELECT nepali_day, time_interval, parameter_name, value FROM system_log_data WHERE nepali_year = ? AND nepali_month = ? AND nepali_day > 0 AND parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL NATIONAL LOAD (ACTUAL)', 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT')"
    df_raw = run_query(query_monthly, (selected_year, selected_month))

    if not df_raw.empty:
        df_raw['value'] = pd.to_numeric(df_raw['value'], errors='coerce').fillna(0.0).abs()
        df_piv = df_raw.pivot_table(index=['nepali_day', 'time_interval'], columns='parameter_name', values='value').fillna(0.0).reset_index()
        for col in ['TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL NATIONAL LOAD (ACTUAL)', 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT']:
            if col not in df_piv.columns: df_piv[col] = 0.0

        df_piv['TOTAL NATIONAL LOAD (ACTUAL)'] = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] + df_piv['SUMMARY_TOTAL_EXPORT'] - df_piv['SUMMARY_TOTAL_IMPORT']

        sys_d = df_piv.loc[df_piv.groupby('nepali_day')['TOTAL SYSTEM LOAD (ACTUAL)'].idxmax(), ['nepali_day', 'time_interval', 'TOTAL SYSTEM LOAD (ACTUAL)']].rename(columns={'TOTAL SYSTEM LOAD (ACTUAL)': 'Peak_MW'}).assign(**{'Load Type': 'System Peak Load'})
        nat_d = df_piv.loc[df_piv.groupby('nepali_day')['TOTAL NATIONAL LOAD (ACTUAL)'].idxmax(), ['nepali_day', 'time_interval', 'TOTAL NATIONAL LOAD (ACTUAL)']].rename(columns={'TOTAL NATIONAL LOAD (ACTUAL)': 'Peak_MW'}).assign(**{'Load Type': 'National Peak Load'})
        
        df_comb = pd.concat([sys_d, nat_d]).sort_values('nepali_day')
        fig_m = px.line(df_comb, x='nepali_day', y='Peak_MW', color='Load Type', custom_data=['time_interval'], markers=True, color_discrete_sequence=['#ff3366', '#0052cc'])
        fig_m.update_traces(hovertemplate='<b>%{fullData.name}</b><br>Day %{x}<br>Peak Load: %{y:.2f} MW<br>Time: %{customdata[0]}<extra></extra>')
        fig_m = update_chart_layout(fig_m, "System vs National Peak Load Each Day", xaxis_title="Day of Month", legend_orientation="v")
        fig_m.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1, range=[0.8, 32.2]))
        st.plotly_chart(fig_m, use_container_width=True)

with t3:
    st.subheader("📈 Historical Trend: Maximum Load per Month")
    query_historical = "SELECT nepali_year, nepali_month, nepali_day, time_interval, parameter_name, value FROM system_log_data WHERE parameter_name IN ('TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL NATIONAL LOAD (ACTUAL)', 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT') AND nepali_day > 0"
    df_raw = run_query(query_historical)

    if not df_raw.empty:
        df_raw['value'] = pd.to_numeric(df_raw['value'], errors='coerce').fillna(0.0).abs()
        df_piv = df_raw.pivot_table(index=['nepali_year', 'nepali_month', 'nepali_day', 'time_interval'], columns='parameter_name', values='value').fillna(0.0).reset_index()
        for col in ['TOTAL SYSTEM LOAD (ACTUAL)', 'TOTAL NATIONAL LOAD (ACTUAL)', 'SUMMARY_TOTAL_IMPORT', 'SUMMARY_TOTAL_EXPORT']:
            if col not in df_piv.columns: df_piv[col] = 0.0

        df_piv['TOTAL NATIONAL LOAD (ACTUAL)'] = df_piv['TOTAL SYSTEM LOAD (ACTUAL)'] + df_piv['SUMMARY_TOTAL_EXPORT'] - df_piv['SUMMARY_TOTAL_IMPORT']

        s_d = df_piv.loc[df_piv.groupby(['nepali_year', 'nepali_month'])['TOTAL SYSTEM LOAD (ACTUAL)'].idxmax(), ['nepali_year', 'nepali_month', 'nepali_day', 'time_interval', 'TOTAL SYSTEM LOAD (ACTUAL)']].rename(columns={'TOTAL SYSTEM LOAD (ACTUAL)': 'Peak_MW'}).assign(**{'Load Type': 'System Peak Load'})
        n_d = df_piv.loc[df_piv.groupby(['nepali_year', 'nepali_month'])['TOTAL NATIONAL LOAD (ACTUAL)'].idxmax(), ['nepali_year', 'nepali_month', 'nepali_day', 'time_interval', 'TOTAL NATIONAL LOAD (ACTUAL)']].rename(columns={'TOTAL NATIONAL LOAD (ACTUAL)': 'Peak_MW'}).assign(**{'Load Type': 'National Peak Load'})

        df_c = pd.concat([s_d, n_d])
        df_c['YM'] = df_c['nepali_year'].astype(str) + "/" + df_c['nepali_month'].astype(str).str.zfill(2)
        df_c.sort_values(['nepali_year', 'nepali_month'], inplace=True)

        fig_h = px.line(df_c, x='YM', y='Peak_MW', color='Load Type', custom_data=['nepali_day', 'time_interval'], markers=True, color_discrete_sequence=['#ff3366', '#0052cc'])
        fig_h.update_traces(hovertemplate='<b>%{fullData.name}</b><br>Month: %{x}<br>Peak Load: %{y:.2f} MW<br>Day: %{customdata[0]}<br>Time: %{customdata[1]}<extra></extra>')
        fig_h = update_chart_layout(fig_h, "System vs National Growth: Monthly Peaks Over Time", xaxis_title="Nepali Year/Month", legend_orientation="v")
        st.plotly_chart(fig_h, use_container_width=True)

with t4:
    if compare_mode == "🗓️ Date-wise" and comp_param:
        st.subheader("📊 Date-wise Comparison")
        fig_comp, metrics = go.Figure(), []

        df_base = run_query("SELECT time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? ORDER BY time_interval ASC", (comp_param, selected_year, selected_month, selected_day))
        base_v = None
        if not df_base.empty:
            df_base['value'] = pd.to_numeric(df_base['value'], errors='coerce').fillna(0.0).abs()
            df_base = df_base.set_index('time_interval').interpolate(method='linear', limit_direction='both').fillna(0).reset_index()
            fig_comp.add_trace(go.Scatter(x=df_base['time_interval'], y=df_base['value'], name=f"BASE: {sidebar_date_str}", mode='lines', line=dict(width=3, color='#1e1e1e')))
            base_v = df_base['value'].max()
            metrics.append({"Target Date": f"BASE ({sidebar_date_str})", "Peak (MW)": base_v, "Time": df_base.loc[df_base['value'].idxmax(), 'time_interval'], "Delta (MW)": 0.0, "Var (%)": 0.0})

        for i, d_str in enumerate(selected_comp_days):
            y, m, d = map(int, d_str.split('/'))
            df_d = run_query("SELECT time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day = ? ORDER BY time_interval ASC", (comp_param, y, m, d))
            if not df_d.empty:
                df_d['value'] = pd.to_numeric(df_d['value'], errors='coerce').fillna(0.0).abs()
                df_d = df_d.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
                fig_comp.add_trace(go.Scatter(x=df_d['time_interval'], y=df_d['value'], name=d_str, mode='lines', line=dict(dash='dot', color=NEA_PALETTE[i % len(NEA_PALETTE)])))
                p_v = df_d['value'].max()
                metrics.append({"Target Date": d_str, "Peak (MW)": p_v, "Time": df_d.loc[df_d['value'].idxmax(), 'time_interval'], "Delta (MW)": (p_v - base_v) if base_v else 0, "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics:
            fig_comp = update_chart_layout(fig_comp, f"Date Comparison: {clean_param_name(comp_param)}", legend_orientation="v")
            st.plotly_chart(fig_comp, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics).style.format({"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)

    elif compare_mode == "📈 Parameters" and baseline_param:
        st.subheader("📊 Parameter Overlay Studio")
        fig_comp2, metrics2 = go.Figure(), []

        df_base = run_query("SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?", (baseline_param, selected_year, selected_month, selected_day))
        base_v = None
        if not df_base.empty:
            df_base['value'] = pd.to_numeric(df_base['value'], errors='coerce').fillna(0.0).abs()
            df_base = df_base.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
            fig_comp2.add_trace(go.Scatter(x=df_base['time_interval'], y=df_base['value'], name=f"BASE: {clean_param_name(baseline_param)}", mode='lines', line=dict(width=3, color='#1e1e1e')))
            base_v = df_base['value'].max()
            metrics2.append({"Parameter": f"BASE: {clean_param_name(baseline_param)}", "Peak (MW)": base_v, "Time": df_base.loc[df_base['value'].idxmax(), 'time_interval'], "Delta (MW)": 0.0, "Var (%)": 0.0})

        for i, p in enumerate(selected_comp_params):
            df_p = run_query("SELECT time_interval, value FROM system_log_data WHERE parameter_name=? AND nepali_year=? AND nepali_month=? AND nepali_day=?", (p, selected_year, selected_month, selected_day))
            if not df_p.empty:
                df_p['value'] = pd.to_numeric(df_p['value'], errors='coerce').fillna(0.0).abs()
                df_p = df_p.set_index('time_interval').interpolate(method='linear').fillna(0).reset_index()
                fig_comp2.add_trace(go.Scatter(x=df_p['time_interval'], y=df_p['value'], name=clean_param_name(p), mode='lines', line=dict(dash='dot', color=NEA_PALETTE[i % len(NEA_PALETTE)])))
                p_v = df_p['value'].max()
                metrics2.append({"Parameter": clean_param_name(p), "Peak (MW)": p_v, "Time": df_p.loc[df_p['value'].idxmax(), 'time_interval'], "Delta (MW)": (p_v - base_v) if base_v else 0, "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics2:
            fig_comp2 = update_chart_layout(fig_comp2, "Parameter Overlay Analytics", legend_orientation="v")
            st.plotly_chart(fig_comp2, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics2).style.format({"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)

    elif compare_mode == "📅 Daily Peak/Load Across Months" and comp_param and selected_comp_months:
        st.subheader("📊 Overlay: Daily Peak Trends Across Months")
        fig_comp3, metrics3 = go.Figure(), []
        base_v = None

        for i, ym_str in enumerate(selected_comp_months):
            y, m = map(int, ym_str.split('/'))
            df_m = run_query("SELECT nepali_day, time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0", (comp_param, y, m))
            if not df_m.empty:
                df_m['value'] = pd.to_numeric(df_m['value'], errors='coerce').fillna(0.0).abs()
                df_peaks = df_m.loc[df_m.groupby('nepali_day')['value'].idxmax()].sort_values('nepali_day')
                l_name, l_dict = (f"BASE: {ym_str}", dict(width=3, color='#1e1e1e')) if i == 0 else (ym_str, dict(dash='dot', color=NEA_PALETTE[i % len(NEA_PALETTE)]))
                fig_comp3.add_trace(go.Scatter(x=df_peaks['nepali_day'], y=df_peaks['value'], name=l_name, mode='lines+markers', line=l_dict, customdata=df_peaks['time_interval'], hovertemplate='Day %{x}<br>Peak: %{y:.2f} MW<br>Time: %{customdata}<extra></extra>'))
                m_peak = df_peaks['value'].max()
                if i == 0: base_v = m_peak
                metrics3.append({"Month": l_name, "Abs Peak": m_peak, "Avg Peak": df_peaks['value'].mean(), "Delta": (m_peak - base_v) if base_v else 0, "Var (%)": ((m_peak - base_v) / base_v * 100) if base_v else 0})

        if metrics3:
            fig_comp3 = update_chart_layout(fig_comp3, f"Day-by-Day Peak Curve: {clean_param_name(comp_param)}", xaxis_title="Day of the Month", legend_orientation="v")
            fig_comp3.update_layout(xaxis=dict(tickmode='linear', tick0=1, dtick=1, range=[0.8, 32.2]))
            st.plotly_chart(fig_comp3, use_container_width=True)
            st.dataframe(pd.DataFrame(metrics3).style.format({"Abs Peak": "{:.2f}", "Avg Peak": "{:.2f}", "Delta": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)

    elif compare_mode == "📊 Monthly Peak/Load" and comp_param and selected_comp_months:
        st.subheader("📊 Macro-Trend: Absolute Peak Comparison")
        metrics4 = []
        base_v = None

        for i, ym_str in enumerate(selected_comp_months):
            y, m = map(int, ym_str.split('/'))
            df_p = run_query("SELECT nepali_day, time_interval, value FROM system_log_data WHERE parameter_name = ? AND nepali_year = ? AND nepali_month = ? AND nepali_day > 0", (comp_param, y, m))
            if not df_p.empty:
                df_p['value'] = pd.to_numeric(df_p['value'], errors='coerce').fillna(0.0).abs()
                df_p = df_p.sort_values(by='value', ascending=False).head(1)
                p_v, p_d, p_t = df_p.iloc[0]['value'], df_p.iloc[0]['nepali_day'], df_p.iloc[0]['time_interval']
                if i == 0: base_v = p_v
                metrics4.append({"Month": f"BASE: {ym_str}" if i == 0 else ym_str, "Peak (MW)": p_v, "Day": p_d, "Time": p_t, "Delta (MW)": (p_v - base_v) if base_v else 0, "Var (%)": ((p_v - base_v) / base_v * 100) if base_v else 0})

        if metrics4:
            df_m4 = pd.DataFrame(metrics4)
            fig_bar = px.bar(df_m4, x="Month", y="Peak (MW)", text="Peak (MW)", color="Month", title=f"Absolute Monthly Peaks: {clean_param_name(comp_param)}", color_discrete_sequence=['#0052cc', '#33b2df', '#ff3366', '#fdbb2d'])
            fig_bar.update_traces(texttemplate='%{text:.2f} MW', textposition='outside')
            fig_bar = update_chart_layout(fig_bar, f"Absolute Monthly Peaks: {clean_param_name(comp_param)}", xaxis_title="")
            fig_bar.update_layout(showlegend=False, yaxis=dict(range=[0, df_m4['Peak (MW)'].max() * 1.15]))
            st.plotly_chart(fig_bar, use_container_width=True)
            st.dataframe(df_m4.style.format({"Peak (MW)": "{:.2f}", "Delta (MW)": "{:+.2f}", "Var (%)": "{:+.2f}%"}), use_container_width=True)
