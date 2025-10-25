import sqlite3
import os
import json
from datetime                   import datetime, timedelta
from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log

class BaseballDB:
    def __init__(self, db_file):
        ''' SQLite database connection (creates folder if it does not exist) '''
        self.db_file = os.path.abspath(db_file)

        db_dir = os.path.dirname(self.db_file)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            fd_log.info(f"📂 Created folder: {db_dir}")

        try:
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()
            fd_log.info(f"✅ Database connection established: {self.db_file}")

            if os.path.exists(self.db_file):
                fd_log.info(f"✅ Database file exists: {self.db_file}")
                self.update_table_schema()
            else:
                self.create_tables()

        except sqlite3.Error as e:
            fd_log.info(f"❌ SQLite connection error: {e}")
            raise

    def update_table_schema(self):
        ''' 기존 DB 파일이 존재할 경우, 누락된 컬럼을 자동으로 추가 (테이블 없으면 생성) '''
        expected_schema = {
            "pitches": {
                "play_id": "TEXT",
                "team_code": "TEXT",
                "player_no": "TEXT",
                "pitch_type": "TEXT",
                "location_height": "REAL",
                "location_side": "REAL",
                "location_time": "REAL",
                "location_speed": "REAL",
                "release_speed": "REAL",
                "release_spin_rate": "REAL",
                "tracking_video_path": "TEXT",
                "tracking_data_path": "TEXT",
                "event_time": "TEXT"
            },
            "hits": {
                "play_id": "TEXT",
                "team_code": "TEXT",
                "player_no": "TEXT",
                "landing_bearing": "REAL",
                "landing_distance": "REAL",
                "landing_hang_time": "REAL",
                "landing_x": "REAL",
                "landing_y": "REAL",
                "launch_speed": "REAL",
                "launch_vertical_angle": "REAL",
                "launch_horizontal_angle": "REAL",
                "launch_spin_axis": "REAL",
                "tracking_video_path": "TEXT",
                "tracking_data_path": "TEXT",
                "event_time": "TEXT"
            }
        }

        # ✅ 추가: raw 테이블 생성 (없으면)
        raw_table_creates = {
            "pitches_raw_data": '''
                CREATE TABLE IF NOT EXISTS pitches_raw_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    play_id TEXT NOT NULL,
                    event_time TEXT,
                    pitch_type TEXT,
                    location_height REAL,
                    location_side REAL,
                    location_time REAL,
                    location_speed REAL,
                    location_middle_height REAL,
                    location_middle_side REAL,
                    location_back_height REAL,
                    location_back_side REAL,
                    movement_horizontal REAL,
                    movement_induced_vertical REAL,
                    movement_spin_axis REAL,
                    movement_vertical REAL,
                    movement_tilt TEXT,
                    movement_side0 REAL,
                    movement_height0 REAL,
                    release_extension REAL,
                    release_height REAL,
                    release_side REAL,
                    release_speed REAL,
                    release_spin_rate REAL,
                    release_horizontal_angle REAL,
                    release_vertical_angle REAL,
                    ninep_x0_x REAL,
                    ninep_x0_y REAL,
                    ninep_x0_z REAL,
                    ninep_v0_x REAL,
                    ninep_v0_y REAL,
                    ninep_v0_z REAL,
                    ninep_a0_x REAL,
                    ninep_a0_y REAL,
                    ninep_a0_z REAL,
                    ninep_pfxx REAL,
                    ninep_pfxz REAL
                );
            ''',
            "hits_raw_data": '''
                CREATE TABLE IF NOT EXISTS hits_raw_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    play_id TEXT NOT NULL,
                    event_time TEXT,
                    landing_bearing REAL,
                    landing_dist_f REAL,
                    landing_distance REAL,
                    landing_hang_time REAL,
                    landing_x REAL,
                    landing_y REAL,
                    launch_speed REAL,
                    launch_vertical_angle REAL,
                    launch_horizontal_angle REAL,
                    launch_spin_axis REAL
                );
            '''
        }

        for raw_table, raw_sql in raw_table_creates.items():
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (raw_table,))
            if not self.cursor.fetchone():
                try:
                    self.cursor.execute(raw_sql)
                    fd_log.info(f"✅ Raw 테이블 생성됨: {raw_table}")
                except sqlite3.Error as e:
                    fd_log.info(f"❌ Raw 테이블 생성 실패: {raw_table}, 오류: {e}")

        # 기존 pitches/hits 테이블 컬럼 체크
        for table_name, columns in expected_schema.items():
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table_name,))
            table_exists = self.cursor.fetchone()

            if not table_exists:
                columns_sql = ", ".join([f"{col} {typ}" for col, typ in columns.items()])
                create_sql = f"CREATE TABLE {table_name} ({columns_sql});"
                try:
                    self.cursor.execute(create_sql)
                    fd_log.info(f"✅ 테이블 생성됨: {table_name}")
                except sqlite3.OperationalError as e:
                    fd_log.info(f"❌ 테이블 생성 실패: {e}")
                continue

            self.cursor.execute(f"PRAGMA table_info({table_name});")
            existing_columns = [row[1] for row in self.cursor.fetchall()]

            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type};"
                    try:
                        self.cursor.execute(alter_sql)
                        fd_log.info(f"✅ 컬럼 추가됨: {table_name}.{column_name} ({column_type})")
                    except sqlite3.OperationalError as e:
                        fd_log.info(f"❌ ALTER TABLE 실패: {e}")

        self.conn.commit()


    def create_tables(self):
        ''' 테이블 생성 (투구, 타구) '''
        queries = [
            '''
            CREATE TABLE IF NOT EXISTS pitches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,  
                play_id TEXT NOT NULL,
                team_code TEXT NOT NULL,
                player_no TEXT NOT NULL,
                pitch_type TEXT,
                location_height REAL,
                location_side REAL,
                location_time REAL,
                location_speed REAL,
                release_speed REAL,
                release_spin_rate REAL,
                tracking_video_path TEXT,
                tracking_data_path TEXT,
                event_time TEXT
            );
            ''',
            '''
            CREATE TABLE IF NOT EXISTS hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,  
                play_id TEXT NOT NULL,
                team_code TEXT NOT NULL,
                player_no TEXT NOT NULL,
                landing_bearing REAL,
                landing_distance REAL,
                landing_hang_time REAL,
                landing_x REAL,
                landing_y REAL,
                launch_speed REAL,
                launch_vertical_angle REAL,
                launch_horizontal_angle REAL,
                launch_spin_axis REAL,
                tracking_video_path TEXT,
                tracking_data_path TEXT,
                event_time TEXT
            );
            ''',
            '''
            CREATE TABLE IF NOT EXISTS pitches_raw_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                play_id TEXT NOT NULL,
                event_time TEXT,
                pitch_type TEXT,
                location_height REAL,
                location_side REAL,
                location_time REAL,
                location_speed REAL,
                location_middle_height REAL,
                location_middle_side REAL,
                location_back_height REAL,
                location_back_side REAL,
                movement_horizontal REAL,
                movement_induced_vertical REAL,
                movement_spin_axis REAL,
                movement_vertical REAL,
                movement_tilt TEXT,
                movement_side0 REAL,
                movement_height0 REAL,
                release_extension REAL,
                release_height REAL,
                release_side REAL,
                release_speed REAL,
                release_spin_rate REAL,
                release_horizontal_angle REAL,
                release_vertical_angle REAL,
                ninep_x0_x REAL,
                ninep_x0_y REAL,
                ninep_x0_z REAL,
                ninep_v0_x REAL,
                ninep_v0_y REAL,
                ninep_v0_z REAL,
                ninep_a0_x REAL,
                ninep_a0_y REAL,
                ninep_a0_z REAL,
                ninep_pfxx REAL,
                ninep_pfxz REAL
            );      
            ''',
            '''
            CREATE TABLE IF NOT EXISTS hits_raw_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                play_id TEXT NOT NULL,
                event_time TEXT,
                landing_bearing REAL,
                landing_dist_f REAL,
                landing_distance REAL,
                landing_hang_time REAL,
                landing_x REAL,
                landing_y REAL,
                launch_speed REAL,
                launch_vertical_angle REAL,
                launch_horizontal_angle REAL,
                launch_spin_axis REAL
            );
            '''
        ]
        for query in queries:
            self.cursor.execute(query)
        self.conn.commit()
        fd_log.info("✅ Table structure created/verified.")

    @staticmethod
    def safe_get(data, keys, default_value=None, value_type=str):
        ''' JSON 데이터에서 안전하게 값을 가져오고, None일 경우 기본값을 반환 '''
        try:
            for key in keys:
                data = data[key]
            return value_type(data) if data is not None else default_value
        except (KeyError, TypeError, ValueError):
            return default_value
        
    def insert_raw_data(self, json_data):
        kind = json_data.get("Kind")

        if kind == "Pitch":
            raw_query = '''
            INSERT INTO pitches_raw_data (
                play_id, event_time, pitch_type,
                location_height, location_side, location_time, location_speed,
                location_middle_height, location_middle_side,
                location_back_height, location_back_side,
                movement_horizontal, movement_induced_vertical, movement_spin_axis,
                movement_vertical, movement_tilt, movement_side0, movement_height0,
                release_extension, release_height, release_side, release_speed,
                release_spin_rate, release_horizontal_angle, release_vertical_angle,
                ninep_x0_x, ninep_x0_y, ninep_x0_z,
                ninep_v0_x, ninep_v0_y, ninep_v0_z,
                ninep_a0_x, ninep_a0_y, ninep_a0_z,
                ninep_pfxx, ninep_pfxz
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            '''

            raw_values = (
                json_data.get("PlayId"),
                json_data.get("Time"),
                self.safe_get(json_data, ["data", "pitchType"], None, str),

                self.safe_get(json_data, ["data", "Location", "Height"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Side"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Time"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Speed"], 0.0, float),

                self.safe_get(json_data, ["data", "LocationMiddle", "Height"], 0.0, float),
                self.safe_get(json_data, ["data", "LocationMiddle", "Side"], 0.0, float),

                self.safe_get(json_data, ["data", "LocationBack", "Height"], 0.0, float),
                self.safe_get(json_data, ["data", "LocationBack", "Side"], 0.0, float),

                self.safe_get(json_data, ["data", "Movement", "Horizontal"], 0.0, float),
                self.safe_get(json_data, ["data", "Movement", "InducedVertical"], 0.0, float),
                self.safe_get(json_data, ["data", "Movement", "SpinAxis"], 0.0, float),
                self.safe_get(json_data, ["data", "Movement", "Vertical"], 0.0, float),
                self.safe_get(json_data, ["data", "Movement", "Tilt"], "", str),
                self.safe_get(json_data, ["data", "Movement", "Side0"], 0.0, float),
                self.safe_get(json_data, ["data", "Movement", "Height0"], 0.0, float),

                self.safe_get(json_data, ["data", "Release", "Extension"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "Height"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "Side"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "Speed"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "SpinRate"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "HorizontalAngle"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "VerticalAngle"], 0.0, float),

                self.safe_get(json_data, ["data", "NineP", "X0", "X"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "X0", "Y"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "X0", "Z"], 0.0, float),

                self.safe_get(json_data, ["data", "NineP", "V0", "X"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "V0", "Y"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "V0", "Z"], 0.0, float),

                self.safe_get(json_data, ["data", "NineP", "A0", "X"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "A0", "Y"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "A0", "Z"], 0.0, float),

                self.safe_get(json_data, ["data", "NineP", "Pfxx"], 0.0, float),
                self.safe_get(json_data, ["data", "NineP", "Pfxz"], 0.0, float)
            )

        elif kind == "Hit":
            raw_query = '''
            INSERT INTO hits_raw_data (
                play_id, event_time,
                landing_bearing, landing_dist_f, landing_distance, landing_hang_time,
                landing_x, landing_y,
                launch_speed, launch_vertical_angle, launch_horizontal_angle, launch_spin_axis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            '''
            raw_values = (
                json_data["PlayId"],
                json_data["Time"],
                self.safe_get(json_data, ["data", "LandingFlat", "Bearing"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "Dist_f"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "Distance"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "HangTime"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "X"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "Y"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "Speed"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "VerticalAngle"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "HorizontalAngle"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "SpinAxis"], 0.0, float)
            )

        else:
            fd_log.info(f"❌ 알 수 없는 Kind 유형: {kind}")
            return

        try:
            self.cursor.execute(raw_query, raw_values)
            self.conn.commit()
            fd_log.info(f"📥 Raw structured data inserted into {kind.lower()}_raw_data for PlayId {json_data.get('PlayId')}.")
        except Exception as e:
            fd_log.info(f"❌ Raw structured data 삽입 오류: {e}")
            fd_log.info("📛 VALUES 개수:", len(raw_values))
            fd_log.info("📛 SQL placeholders 개수:", raw_query.count("?"))


    def insert_data(self, json_data, tracking_video_path , tracking_data_path):
        ''' JSON 데이터를 SQLite 데이터베이스에 삽입 (PlayId 사용) '''

        if isinstance(json_data, str):
            try:
                json_data = json.loads(json_data)
            except json.JSONDecodeError as e:
                fd_log.info(f"❌ JSON 디코딩 오류: {e}")
                return  

        if not isinstance(json_data, dict):
            fd_log.info(f"❌ 잘못된 JSON 데이터 형식: {type(json_data)}")
            return

        required_keys = ["PlayId", "Kind", "Time", "data"]
        for key in required_keys:
            if key not in json_data:
                fd_log.info(f"❌ JSON 데이터에 {key} 키가 없습니다.")
                return

        play_id = json_data["PlayId"]
        event_time = json_data["Time"].replace("T", " ").split("+")[0]

        tracking_data_path = str(tracking_data_path) if tracking_data_path else ""
        tracking_video_path = str(tracking_video_path) if tracking_video_path else ""
        team_code = conf._team_code
        player_no = conf._player_no

        if json_data["Kind"] == "Pitch":
            query = '''
            INSERT INTO pitches (
                play_id, team_code, player_no, pitch_type,
                location_height, location_side, location_time, location_speed,
                release_speed, release_spin_rate,
                tracking_video_path, tracking_data_path, event_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            '''
            values = (
                play_id, str(team_code), str(player_no),
                self.safe_get(json_data, ["data", "pitchType"], "Unknown", str),
                self.safe_get(json_data, ["data", "Location", "Height"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Side"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Time"], 0.0, float),
                self.safe_get(json_data, ["data", "Location", "Speed"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "Speed"], 0.0, float),
                self.safe_get(json_data, ["data", "Release", "SpinRate"], 0.0, float),
                tracking_video_path,
                tracking_data_path,
                event_time
            )

        elif json_data["Kind"] == "Hit":
            query = '''
            INSERT INTO hits (
                play_id, team_code, player_no,
                landing_bearing, landing_distance, landing_hang_time,
                landing_x, landing_y,
                launch_speed, launch_vertical_angle, launch_horizontal_angle, launch_spin_axis,
                tracking_video_path, tracking_data_path, event_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            '''
            values = (
                play_id, str(team_code), str(player_no),
                self.safe_get(json_data, ["data", "LandingFlat", "Bearing"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "Distance"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "HangTime"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "X"], 0.0, float),
                self.safe_get(json_data, ["data", "LandingFlat", "Y"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "Speed"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "VerticalAngle"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "HorizontalAngle"], 0.0, float),
                self.safe_get(json_data, ["data", "Launch", "SpinAxis"], 0.0, float),
                tracking_video_path,
                tracking_data_path,
                event_time
            )
        else:
            fd_log.info(f"❌ 알 수 없는 Kind 유형: {json_data['Kind']}")
            return

        try:
            self.cursor.execute(query, values)
            self.conn.commit()
            fd_log.info(f"✅ Data inserted successfully for PlayId {play_id}.")
        except Exception as e:
            fd_log.info(f"❌ 데이터 삽입 오류: {e}")


    def fetch_pitches(self):
        self.cursor.execute("SELECT * FROM pitches ORDER BY event_time DESC;")
        rows = self.cursor.fetchall()
        return [dict(row) for row in rows]

    def fetch_hits(self):
        self.cursor.execute("SELECT * FROM hits ORDER BY event_time DESC;")
        rows = self.cursor.fetchall()
        return [dict(row) for row in rows]
    
    def fetch_raw_pitches(self):
        self.cursor.execute("SELECT * FROM pitches_raw_data ORDER BY event_time DESC;")
        rows = self.cursor.fetchall()
        return [dict(row) for row in rows]

    def fetch_raw_hits(self):
        self.cursor.execute("SELECT * FROM hits_raw_data ORDER BY event_time DESC;")
        rows = self.cursor.fetchall()
        return [dict(row) for row in rows]

    def count_pitches(self, play_id=None):
        query = "SELECT COUNT(*) FROM pitches WHERE play_id = ?;" if play_id else "SELECT COUNT(*) FROM pitches;"
        self.cursor.execute(query, (play_id,) if play_id else ())
        count = self.cursor.fetchone()[0]
        fd_log.info(f"📊 Total pitches data count: {count} (PlayId: {play_id})")
        return count

    def count_hits(self, play_id=None):
        query = "SELECT COUNT(*) FROM hits WHERE play_id = ?;" if play_id else "SELECT COUNT(*) FROM hits;"
        self.cursor.execute(query, (play_id,) if play_id else ())
        count = self.cursor.fetchone()[0]
        fd_log.info(f"📊 Total hits data count: {count} (PlayId: {play_id})")
        return count
    
    def count_raw_pitches(self, play_id=None):
        query = "SELECT COUNT(*) FROM pitches_raw_data WHERE play_id = ?;" if play_id else "SELECT COUNT(*) FROM pitches_raw_data;"
        self.cursor.execute(query, (play_id,) if play_id else ())
        count = self.cursor.fetchone()[0]
        fd_log.info(f"📊 Total pitches raw data count: {count} (PlayId: {play_id})")
        return count

    def count_raw_hits(self, play_id=None):
        query = "SELECT COUNT(*) FROM hits_raw_data WHERE play_id = ?;" if play_id else "SELECT COUNT(*) FROM hits_raw_data;"
        self.cursor.execute(query, (play_id,) if play_id else ())
        count = self.cursor.fetchone()[0]
        fd_log.info(f"📊 Total hits raw data count: {count} (PlayId: {play_id})")
        return count

    def close(self):
        self.conn.close()
        fd_log.info("🔌 데이터베이스 연결이 종료되었습니다.")

    def get_tracking_data_paths(self, team_code, player_no, pitch_type_counts):
        '''
        각 pitch_type 별로 최근 날짜순으로 지정된 개수만큼 tracking_data_path 반환

        :param team_code: 팀 코드 (str)
        :param player_no: 선수 번호 (str)
        :param pitch_type_counts: dict, 예: {'Fastball': 3, 'Curveball': 2}
        :return: dict 형태의 tracking_data_path 결과 (pitch_type별 구분)
        '''
        pkl_list = {}

        for pitch_type, limit in pitch_type_counts.items():
            query = f'''
                SELECT tracking_data_path FROM pitches
                WHERE team_code = ? AND player_no = ? AND pitch_type = ?
                ORDER BY event_time DESC
                LIMIT {int(limit)};
            '''
            self.cursor.execute(query, (team_code, player_no, pitch_type))
            rows = self.cursor.fetchall()
            paths = [row["tracking_data_path"] for row in rows if row["tracking_data_path"]]
            pkl_list[pitch_type] = paths

        return True, pkl_list
    
    def get_next_hit_after(self, folder_input: str, selected_moment_sec: int):
        base_time_str = os.path.basename(folder_input)
        base_dt = datetime.strptime(base_time_str, "%Y_%m_%d_%H_%M_%S")
        target_dt = base_dt + timedelta(seconds=selected_moment_sec)
        target_time_str = target_dt.strftime("%Y-%m-%dT%H:%M:%S")

        query = '''
        SELECT * FROM hits_raw_data
        WHERE event_time > ?
        ORDER BY event_time ASC
        LIMIT 1;
        '''
        self.cursor.execute(query, (target_time_str,))
        row = self.cursor.fetchone()

        if row:
            hit_data = dict(row)
            #self.save_pitch_data_to_file(hit_data,"next_hit.json")
            conf._playId_hit = hit_data.get("play_id")
            conf._landingflat_distance = hit_data.get("landing_distance")
            conf._landingflat_bearing = hit_data.get("landing_bearing")
            conf._landingflat_hangtime = hit_data.get("landing_hang_time")
            conf._landingflat_x = hit_data.get("landing_x")
            conf._landingflat_y = hit_data.get("landing_y")
            conf._launch_speed = hit_data.get("launch_speed")
            conf._launch_v_angle = hit_data.get("launch_vertical_angle")
            conf._launch_h_angle = hit_data.get("launch_horizontal_angle")
            conf._launch_spinaxis = hit_data.get("launch_spin_axis")
            return True
        return False


    
    def get_next_pitch_after(self, folder_input: str, selected_moment_sec: int):
        base_time_str = os.path.basename(folder_input)
        base_dt = datetime.strptime(base_time_str, "%Y_%m_%d_%H_%M_%S")
        target_dt = base_dt + timedelta(seconds=selected_moment_sec)
        target_time_str = target_dt.strftime("%Y-%m-%dT%H:%M:%S")

        query = '''
        SELECT * FROM pitches_raw_data
        WHERE event_time > ?
        ORDER BY event_time ASC
        LIMIT 1;
        '''
        self.cursor.execute(query, (target_time_str,))
        row = self.cursor.fetchone()

        if row:
            pitch_data = dict(row)
            #self.save_pitch_data_to_file(pitch_data,"next_pitch.json")
            conf._playId_pitch = pitch_data.get("play_id")
            conf._release_speed = pitch_data.get("release_speed")
            conf._release_spinrate = int(pitch_data.get("release_spin_rate", 0))
            conf._pitch_type = pitch_data.get("pitch_type")
            return True
        return False
    
    def get_prev_pitch_before(self, folder_input: str, selected_moment_sec: int):
        base_time_str = os.path.basename(folder_input)
        base_dt = datetime.strptime(base_time_str, "%Y_%m_%d_%H_%M_%S")
        target_dt = base_dt + timedelta(seconds=selected_moment_sec)
        target_time_str = target_dt.strftime("%Y-%m-%dT%H:%M:%S")

        query = '''
        SELECT * FROM pitches_raw_data
        WHERE event_time < ?
        ORDER BY event_time DESC
        LIMIT 1;
        '''
        self.cursor.execute(query, (target_time_str,))
        row = self.cursor.fetchone()

        if row:
            pitch_data = dict(row)
            #self.save_pitch_data_to_file(pitch_data,"pre_pitch.json")
            conf._release_speed = pitch_data.get("release_speed")
            conf._release_spinrate = int(pitch_data.get("release_spin_rate", 0))
            conf._pitch_type = pitch_data.get("pitch_type")
            return True
        return False
    
    def save_pitch_data_to_file(self, pitch_data: dict, filename: str, output_dir: str = "./output"):
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(pitch_data, f, indent=4, ensure_ascii=False)

        fd_log.info(f"Pitch data saved to {filepath}")




