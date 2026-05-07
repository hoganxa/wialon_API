# wialon_API
# fetching drivers' login time based on Truck's GPS using Wialon API
# connect the data with database / cloud TO GET TOKEN NUMBER stored in DB

import pymysql
import pandas as pd
import requests
import json
import os
import logging
from datetime import datetime

# --- [ KONFIGURASI PENGGUNA ] ---

DB_HOST = 'put localhost here'
DB_USER = 'put username here'
DB_PASSWORD = 'your database password'
DB_NAME = 'your database name'

WIALON_API_URL = 'https://hst-api.wialon.com/wialon/ajax.html'

# --- Pengaturan Folder Output ---
BASE_DIR = r"C:\path\to\directory"
CSV_FILENAME = "document_name.csv"
LOG_FILENAME = "log.txt" #optional, to locate error

CSV_PATH = os.path.join(BASE_DIR, CSV_FILENAME)
LOG_PATH = os.path.join(BASE_DIR, LOG_FILENAME)
# --- [ END OF CONFIGURATION ] ---

def setup_logging():
    if not os.path.exists(BASE_DIR):
        try: os.makedirs(BASE_DIR)
        except OSError as e: logger.info(f"CRITICAL: Failed to create a folder {BASE_DIR}: {e}"); return None

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if logger.hasHandlers(): logger.handlers.clear()
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    try:
        fh = logging.FileHandler(LOG_PATH, encoding='utf-8'); fh.setFormatter(formatter); logger.addHandler(fh)
    except: pass
    sh = logging.StreamHandler(); sh.setFormatter(formatter); logger.addHandler(sh)
    return logger

def format_timestamp(ts):
    """Change Unix timestamp into time string."""
    if not ts: return None
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def wialon_request(svc, params, sid=None):
    data = {'svc': svc, 'params': json.dumps(params)}
    if sid: data['sid'] = sid
    try:
        response = requests.post(WIALON_API_URL, data=data, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def get_unit_by_imei(imei, sid):
    """
    Seeking Unit ID and Unit Name based on IMEI (sys_unique_id).
    """
    if not imei: return None, None
    params = {
        "spec": {
            "itemsType": "avl_unit",
            "propName": "sys_unique_id",
            "propValueMask": str(imei),
            "sortType": "sys_unique_id"
        },
        "force": 1, "from": 0, "to": 0, "flags": 4194561 
    }
    resp = wialon_request("core/search_items", params, sid)
    if 'items' in resp and len(resp['items']) > 0:
        unit = resp['items'][0]
        return unit['id'], unit['nm']
    return None, None

def get_route_data_from_db():
    sql_query = """
    SELECT 
        r.id AS route_id, 
        r.deleted,
        r.name AS route_name, 
        c.name AS car_name, 
        r.start_warehouse_id,
        r.start_date,
        r.driver_amt AS driver_amount,
        rd.driver_username, 
        rd.is_helper, 
        e.name AS driver_name, 
        e.name2 AS driver_name2,
        w.name AS warehouse_name,
        c.id AS car_id,
        c.wialon_tracking_imei,
        c.gps_provider_id AS car_gps,
        gp.token AS wialon_token
    FROM 
        route r
    LEFT JOIN 
        route_driver rd ON r.id = rd.route_id
    LEFT JOIN 
        employee e ON rd.driver_username = e.username 
    LEFT JOIN 
        warehouse w ON r.start_warehouse_id = w.warehouse_id
    LEFT JOIN 
        cars c ON r.car_id = c.id
    JOIN
    	gps_providers gp ON c.gps_provider_id = gp.id
    WHERE
        c.wialon_tracking_imei IS NOT NULL
    AND r.start_date BETWEEN CURDATE() - INTERVAL 90 DAY AND CURDATE()
    AND c.gps_provider_id = 1;
    """
    print("Fetching data from database...")
    try:
        connection = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor
        )
        with connection.cursor() as cursor:
            cursor.execute(sql_query)
            result = cursor.fetchall()
        connection.close()
        print(f"Finished ({len(result)} rute).")
        return pd.DataFrame(result)
    except Exception as e:
        print(f"\n[Database Error] {e}")
        return pd.DataFrame()

def build_wialon_name_map(sid):
    print("Building map on Unit Name in Wialon...")
    name_map = {}
    params = {"spec": {"itemsType": "avl_unit", "propName": "sys_name", "propValueMask": "*", "sortType": "sys_name"}, "force": 1, "flags": 1, "from": 0, "to": 0}
    resp = wialon_request("core/search_items", params, sid)
    if 'items' in resp:
        for unit in resp['items']:
            if unit.get('nm'): name_map[unit['nm']] = unit['id']
    return name_map

def main():
    logger = setup_logging()
    if not logger: return

    # 1. Fetch DB Data FIRST (To get the tokens)
    route_df = get_route_data_from_db()
    if route_df.empty: logger.warning("No data found."); return

    # 2. Check for Token column
    if 'wialon_token' not in route_df.columns:
        logger.critical("Column 'wialon_token' missing from database result.")
        return

    logger.info(f"Processing data ({len(route_df)} rows)...")
    
    csv_data = []

    # 3. Group by Token (In case multiple tokens exist in the result)
    # This handles the logic: Take value of API_TOKEN from wialon_token column
    unique_tokens = route_df['wialon_token'].unique()

    for token in unique_tokens:
        if not token:
            logger.warning("Found row with empty Wialon Token. Skipping.")
            continue

        # A. Login with the specific token from DB
        logger.info(f"Logging in with token: {token[:10]}...")
        login = wialon_request("token/login", {"token": token})
        
        if 'eid' not in login:
            logger.error(f"Login Failed for token {token[:10]}... Error: {login}")
            continue

    sid = login['eid']
    logger.info(f"session id: {sid}")

    name_map = build_wialon_name_map(sid)
    if not name_map: return
    logger.info(f"Processing data and arranging in ({CSV_FILENAME})...")

    imei_cache = {}

    for index, route in route_df.iterrows():
        result_base = {
            "route_id": route['route_id'],
            "route_name": route['route_name'],
            "car_name": route['car_name'],
            "date": None,
            "driver_amount": route['driver_amount'],
            "driver_username": route['driver_username'],
            "is_helper": route['is_helper'],
            "driver_name": route['driver_name'],
            "driver_name2": route['driver_name2'],
            "warehouse_name": route['warehouse_name'],
            "car_id": route['car_id'],
            "tracking_imei": route['wialon_tracking_imei'],
            "start_time": None,
            "end_time": None,
            "data_source": "No Data"
        }

        wialon_imei = route['wialon_tracking_imei']
        date_obj = route['start_date']

        if not wialon_imei:
            result_base['data_source'] = "No IMEI in DB"
            csv_data.append(result_base)
            continue

        if wialon_imei in imei_cache:
            unit_id, unit_name = imei_cache[wialon_imei]
        else:
            unit_id, unit_name = get_unit_by_imei(wialon_imei, sid)
            # Save to cache (even if None, so as not to re-request the wrong IMEI)
            imei_cache[wialon_imei] = (unit_id, unit_name)

        if not unit_id:
            result_base['data_source'] = f"Unit Not Found"
            csv_data.append(result_base)
            continue

        # B. Validasi Tanggal
        try:
            if not isinstance(date_obj, datetime): date_obj = pd.to_datetime(date_obj)
            t_from = int(date_obj.replace(hour=0, minute=0, second=0).timestamp())
            t_to = int(date_obj.replace(hour=23, minute=59, second=59).timestamp())
            result_base['date'] = date_obj.strftime('%Y-%m-%d')
        except:
            result_base['data_source'] = "Date Error"
            csv_data.append(result_base)
            continue

        # TASK 1: Load Messages
        msg_params = {"itemId": unit_id, "timeFrom": t_from, "timeTo": t_to, "flags": 0, "flagsMask": 0, "loadCount": 4294967295}
        raw_msgs_resp = wialon_request("messages/load_interval", msg_params, sid)

        if 'error' in raw_msgs_resp:
            result_base['data_source'] = f"Wialon Error: {raw_msgs_resp['error']}"
            csv_data.append(result_base)
            continue

        # TASK 2: Get Trips
        trips_params = {"itemId": unit_id, "msgsSource": "1", "timeFrom": t_from, "timeTo": t_to}
        trips_resp = wialon_request("unit/get_trips", trips_params, sid)

        trips_list = []
        if isinstance(trips_resp, list): trips_list = trips_resp
        elif isinstance(trips_resp, dict): trips_list = trips_resp.get('trips', [])

        if trips_list:
            start_raw = trips_list[0].get('from', {}).get('t')
            end_raw = trips_list[-1].get('to', {}).get('t')
            result_base['start_time'] = format_timestamp(start_raw)
            result_base['end_time'] = format_timestamp(end_raw)
            result_base['data_source'] = f"Wialon API ({len(trips_list)} Trips)"
        elif raw_msgs_resp.get('count', 0) > 0:
            msgs = raw_msgs_resp.get('messages', [])
            if msgs:
                result_base['start_time'] = format_timestamp(msgs[0].get('t'))
                result_base['end_time'] = format_timestamp(msgs[-1].get('t'))
                result_base['data_source'] = f"Raw GPS"
        else:
            result_base['data_source'] = f"No GPS Activity"

        csv_data.append(result_base)

    # --- Save CSV (APPEND MODE LOGIC) ---
    if csv_data:
        df_new = pd.DataFrame(csv_data)
        
        # 1. ENFORCE DATA TYPES
        # A. Format Numeric
        numeric_cols = {
            "route_id": "Int64",
            "driver_amount": "Int64",
            "tracking_imei": "Int64"
        }
        
        for col, dtype in numeric_cols.items():
            if col in df_new.columns:
                # Convert to numeric first to handle strings numbers, then to Int64
                df_new[col] = pd.to_numeric(df_new[col], errors='coerce').astype(dtype)

        # B. Format Date(YYYY-MM-DD)
        if "date" in df_new.columns:
            df_new["date"] = pd.to_datetime(df_new["date"], errors='coerce').dt.date

        # C. Format Time (24-hour format HH:MM:SS)
        # Note: format_timestamp function already returns string "HH:MM:SS" or similar.
        # But to ensure consistency, treat them as strings in CSV or enforce datetime format.
        time_cols = ["start_time", "end_time"]
        for col in time_cols:
            if col in df_new.columns:
                # Ensure they are datetime objects first, then format
                # If they are already strings "YYYY-MM-DD HH:MM:SS", we strip to time
                temp_series = pd.to_datetime(df_new[col], errors='coerce')
                df_new[col] = temp_series.dt.strftime('%H:%M:%S')

        # D. Format Text 
        text_cols = [
            "route_name", "car_name", "driver_username", "warehouse_name", "data_source"
        ]
        for col in text_cols:
            if col in df_new.columns:
                df_new[col] = df_new[col].astype(str).replace('nan', '')

        # ---------------------------------------------------------

        cols = [
            "route_id", "route_name", "car_name", "date", "driver_amount", 
            "driver_username", "warehouse_name", "tracking_imei", 
            "start_time", "end_time", "data_source"
        ]
        
        final_cols = [c for c in cols if c in df_new.columns]
        df_new = df_new[final_cols]

        try:
            if os.path.exists(CSV_PATH):
                logger.info(f"File '{CSV_FILENAME}' found. Combining data...")
                
                # Read existing file
                try:
                    df_existing = pd.read_csv(CSV_PATH, encoding='utf-8')
                except UnicodeDecodeError:
                    logger.warning("Encoding UTF-8 failed, trying ISO-8859-1")
                    df_existing = pd.read_csv(CSV_PATH, encoding='ISO-8859-1')

                # Apply the same data type enforcement to existing data to ensure clean merge
                for col, dtype in numeric_cols.items():
                    if col in df_existing.columns:
                        df_existing[col] = pd.to_numeric(df_existing[col], errors='coerce').astype(dtype)

                # Check if data in df_new is the same as df_existing (ignoring rows with missing data in df_new)
                if df_new.empty:
                    logger.info("No new data to add. Keeping the existing data.")
                else:
                    df_combined = pd.concat([df_existing, df_new], ignore_index=True)

                    # --- UPDATED LOGIC START ---
                # 5. Deduplicate (Overwrite old data with new data based on route_id and date)
                before_dedup = len(df_combined)
                
                # NOTE: using subset=['route_id', 'date'] will keep only ONE row per route per day.
                # If you have multiple drivers for one route, this will keep only the last one processed.
                df_combined.drop_duplicates(subset=['route_id', 'date'], keep='last', inplace=True)
                
                after_dedup = len(df_combined)
                
                if before_dedup > after_dedup:
                    logger.info(f"Removed {before_dedup - after_dedup} duplicate rows (updated with latest data).")
                
                # 6. Save back
                df_combined.to_csv(CSV_PATH, index=False, encoding='utf-8-sig')
                logger.info(f"SUCCESS: Data combined and saved. Total rows: {len(df_combined)}")
                # --- UPDATED LOGIC END ---
            else:
                logger.info(f"File is not exist. Creating the new '{CSV_FILENAME}'")
                df_new.to_csv(CSV_PATH, index=False)
                logger.info(f"SUCCESS: New file saved. Total or rows: {len(df_new)}")
        except Exception as e:
            logger.critical(f"Failed to save CSV: {e}")
    else:
        logger.warning("No new data to be saved.")

    logger.info("--- END PROCESS ---")

if __name__ == "__main__":
    main()
