# ─────────────────────────────────────────────────────────────────────────────#
# /A/I/D/
# fd_dashboard.py
# - 2025/06/01
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import sys
import time
import queue
from datetime import datetime
from collections import deque

from fd_utils.fd_config_manager     import conf
from fd_utils.fd_logging            import fd_log

from fd_manager.fd_create_clip          import play_and_create_multi_clips
from fd_detection.fd_live_buffer        import fd_rtsp_client_start
from fd_detection.fd_live_buffer        import fd_rtsp_client_stop

from fd_detection.fd_live_detect_detail import refresh_information

from PyQt5.QtWidgets import QWidget, QTextEdit, QLabel, QVBoxLayout, QHBoxLayout, QGroupBox, QTableWidget, QTableWidgetItem, QApplication, QSizePolicy, QComboBox, QPushButton, QLineEdit, QCheckBox, QRadioButton, QButtonGroup, QSplitter

from PyQt5.QtGui import QTextCursor, QPixmap, QFont, QIcon
from PyQt5.QtCore import Qt, QTimer

# 모드 상수
MODE_MANUAL = 0
MODE_AUTO   = 1
MODE_SEMI   = 2   # 반자동

# ─────────────────────────────────────────────────────────────────────────────##
# class DetectDashboard(QWidget):
# owner : hongsu jung
# date : 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────#
class DetectDashboard(QWidget):
    def __init__(self, conf):
        super().__init__()
        self.conf = conf
        self.setWindowTitle("Control - Live Status")
        self.setFont(QFont("맑은고딕", 9))
        self.resize(600, 600)

        # 반자동용: 최근 감지 번호 큐(투수/타자)
        self.pitcher_recent = deque(maxlen=30)
        self.batter_recent  = deque(maxlen=30)

        self.team_names = [
            conf._team_name_1, conf._team_name_2, conf._team_name_3, conf._team_name_4, conf._team_name_5,
            conf._team_name_6, conf._team_name_7, conf._team_name_8, conf._team_name_9, conf._team_name_10
        ]

        main_layout = QVBoxLayout()
        self.setLayout(main_layout)

        # 🔘 Top/Bottom Toggle Button
        self.toggle_button = QPushButton("Current: Top")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.clicked.connect(self.toggle_pitching_team)
        main_layout.addWidget(self.toggle_button)

        # Pitcher 섹션 추가
        pitcher_group, self.pitcher_icon, self.pitcher_team, self.pitcher_back, self.pitcher_auto, self.pitcher_name, self.pitcher_handedness, self.pitcher_table = self.create_player_section("Pitcher")
        self.pitcher_group = pitcher_group  # ⬅️ 추가
        main_layout.addWidget(pitcher_group)

        # Batter 섹션 추가
        batter_group, self.batter_icon, self.batter_team, self.batter_back, self.batter_auto, self.batter_name, self.batter_handedness, self.batter_table = self.create_player_section("Batter")
        self.batter_group = batter_group    # ⬅️ 추가
        main_layout.addWidget(batter_group)

        # 초기 팀 설정
        self.update_team_selections(top=True)
    
    
    def create_player_section(self, title):
        group = QGroupBox(title)
        layout = QHBoxLayout()
        layout.setSpacing(20)
        group.setLayout(layout)

        # 왼쪽 아이콘
        icon = QLabel()
        icon.setPixmap(QPixmap("images/unknown.png").scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(icon, alignment=Qt.AlignTop)

        # ▶ 오른쪽 정보 세로 정렬
        info_layout = QVBoxLayout()
        info_layout.setSpacing(3)  # 줄간격 +1px
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        # Team
        team_label = QLabel("Team:")
        team_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        info_layout.addWidget(team_label)

        team_combo = QComboBox()
        team_combo.addItems(self.team_names)
        team_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        team_combo.setMaximumWidth(120)
        info_layout.addWidget(team_combo)

        # Back# + 모드 라디오버튼
        back_row = QHBoxLayout()
        back_label = QLabel("Back#:")
        back_input = QLineEdit("-")
        back_input.setFixedWidth(60)

        # ---- 라디오버튼 3종 (수동/자동/반자동) ----
        mode_group = QButtonGroup(group)
        rb_manual = QRadioButton("수동")
        rb_auto   = QRadioButton("자동")
        rb_semi   = QRadioButton("반자동")   # 후보 3개 중 선택

        mode_group.addButton(rb_manual, MODE_MANUAL)
        mode_group.addButton(rb_auto,   MODE_AUTO)
        mode_group.addButton(rb_semi,   MODE_SEMI)

        rb_auto.setChecked(True)  # 기본은 '자동' (기존 동작과 동일)

        back_row.addWidget(back_label)
        back_row.addWidget(back_input)
        back_row.addSpacing(6)
        back_row.addWidget(rb_manual)
        back_row.addWidget(rb_auto)
        back_row.addWidget(rb_semi)
        info_layout.addLayout(back_row)

        # === Pending 박스 (Back# 바로 아래, 중앙정렬) ===
        pending_box = QGroupBox("Pending") #  (click to apply)
        pending_box.setAlignment(Qt.AlignCenter)
        pending_layout = QHBoxLayout()
        pending_layout.setContentsMargins(6, 6, 6, 6)
        pending_layout.setSpacing(6)
        pending_box.setLayout(pending_layout)

        cand_btns = []
        for _ in range(3):
            btn = QPushButton("-")
            btn.setEnabled(False)
            btn.setFixedWidth(48)
            cand_btns.append(btn)
            pending_layout.addWidget(btn)
        pending_box.hide()  # 기본 숨김
        info_layout.addWidget(pending_box)

        # Name / Handedness
        name_label = QLabel("Name: -")
        name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        info_layout.addWidget(name_label)

        handedness_label = QLabel("Handedness: -")
        handedness_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        info_layout.addWidget(handedness_label)

        # ---- 모드별 UI 적용 ----
        def apply_mode_ui():
            mode = mode_group.checkedId()
            if mode == MODE_MANUAL:
                back_input.setReadOnly(False)
                back_input.setStyleSheet("")
                pending_box.hide()
            elif mode == MODE_AUTO:
                back_input.setReadOnly(True)
                back_input.setStyleSheet("background:#f5f5f5;")
                pending_box.hide()
            else:  # MODE_SEMI
                back_input.setReadOnly(True)
                back_input.setStyleSheet("background:#f5f5f5;")
                # 표시/갱신은 update_player_info에서 처리
        apply_mode_ui()

        def on_mode_changed(_):
            # 하위 모듈 호환 위해 기존 플래그도 유지: '자동'일 때만 True
            is_auto = (mode_group.checkedId() == MODE_AUTO)
            if title == "Pitcher":
                conf._live_pitcher_auto = is_auto
                conf._live_pitcher_mode = mode_group.checkedId()
            else:
                conf._live_batter_auto = is_auto
                conf._live_batter_mode = mode_group.checkedId()
            apply_mode_ui()

        rb_manual.toggled.connect(on_mode_changed)
        rb_auto.toggled.connect(on_mode_changed)
        rb_semi.toggled.connect(on_mode_changed)

        # 수동 입력 시 이름/손잡이 갱신
        def on_back_input_changed():
            if mode_group.checkedId() == MODE_MANUAL:
                try:
                    number = int(back_input.text().strip())
                    if title == "Pitcher":
                        conf._live_pitcher_no = number
                    else:
                        conf._live_batter_no = number

                    name = self.get_player_name(number, team_combo)
                    name_label.setText(f"Name: {name}")

                    handedness_str = self.get_player_handedness(number, team_combo)
                    handed = None
                    if title == "Pitcher":
                        if '좌투' in handedness_str: handed = 'Left'
                        elif '우투' in handedness_str: handed = 'Right'
                    else:
                        if '좌타' in handedness_str: handed = 'Left'
                        elif '우타' in handedness_str: handed = 'Right'
                    handedness_label.setText(f"Handedness: {handed}")
                except ValueError:
                    name_label.setText("Name: -")
                    handedness_label.setText("Handedness: -")

        back_input.textChanged.connect(on_back_input_changed)

        # 후보 버튼 클릭 = 확정 적용(반자동 전용)
        def on_pick(text: str):
            if mode_group.checkedId() != MODE_SEMI:
                return
            try:
                sel = int(text)
            except Exception:
                return
            back_input.setText(str(sel))
            if title == "Pitcher":
                conf._live_pitcher_no = sel
            else:
                conf._live_batter_no = sel
            name = self.get_player_name(sel, team_combo)
            name_label.setText(f"Name: {name}")
            handedness_str = self.get_player_handedness(sel, team_combo)
            handed = None
            if title == "Pitcher":
                if '좌투' in handedness_str: handed = 'Left'
                elif '우투' in handedness_str: handed = 'Right'
            else:
                if '좌타' in handedness_str: handed = 'Left'
                elif '우타' in handedness_str: handed = 'Right'
            handedness_label.setText(f"Handedness: {handed}")

        for btn in cand_btns:
            btn.clicked.connect(lambda _=False, b=btn: on_pick(b.text()))

        # 왼쪽(정보) 붙이기
        left_layout = QVBoxLayout()
        left_layout.addLayout(info_layout)
        layout.addLayout(left_layout, stretch=0)

        # 오른쪽 History
        right_layout = QVBoxLayout()
        history_label = QLabel("📄 History")
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Name", "Time", "File"])
        table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(history_label)
        right_layout.addWidget(table)

        layout.addLayout(right_layout, stretch=1)

        # 섹션별 참조 저장(후보 갱신용)
        group._pending_box = pending_box
        group._cand_btns   = cand_btns
        group._mode_group  = mode_group

        # return 형식 유지(5번째에 'auto' 대신 mode_group 반환)
        return group, icon, team_combo, back_input, mode_group, name_label, handedness_label, table


    def toggle_pitching_team(self):
        is_top = self.toggle_button.isChecked()
        self.toggle_button.setText("Current: Top" if is_top else "Current: Bottom")
        self.update_team_selections(top=is_top)


    def update_team_selections(self, top: bool):
        home = self.conf._home_team_name
        away = self.conf._away_team_name
        if top:
            self.pitcher_team.setCurrentText(home)
            self.batter_team.setCurrentText(away)
        else:
            self.pitcher_team.setCurrentText(away)
            
            self.batter_team.setCurrentText(home)

        # 2025-08-11
        # 🔹 전역에 팀 번호 커밋
        self.conf._live_pitcher_team = self.get_team_id(self.pitcher_team) + 1
        self.conf._live_batter_team  = self.get_team_id(self.batter_team) + 1
        fd_log.info(f"[AId] Pitcher Team: {self.conf._live_pitcher_team}, Batter Team: {self.conf._live_batter_team}")

        # Pitcher Update from back number
        # pitcher_number = self.extract_back_number(self.pitcher_back)
        pitcher_number = self.extract_back_number(self.pitcher_back, self.pitcher_auto)
        if pitcher_number is not None:
            name = self.get_player_name(pitcher_number, self.pitcher_team)
            self.pitcher_name.setText(f"Name: {name}")

        # Batter Update from back number
        # batter_number = self.extract_back_number(self.batter_back)
        batter_number = self.extract_back_number(self.batter_back, self.batter_auto)
        if batter_number is not None:
            name = self.get_player_name(batter_number, self.batter_team)
            self.batter_name.setText(f"Name: {name}")

    # def extract_back_number(self, label: QLabel):
    #     text = label.text().strip()
    #     if text.startswith("Back#:"):
    #         try:
    #             return int(text.replace("Back#:", "").strip())
    #         except ValueError:
    #             return None
    #     return None
    def extract_back_number(self, back_input: QLineEdit, auto_checkbox: QCheckBox):
        try:
            return int(back_input.text().strip())
        except ValueError:
            return None

    def build_section_layout(self, icon, team_combo, back_label, name_label, handedness_label, table):
        # Pitcher 또는 Batter 섹션 전체 (좌우 레이아웃)
        section_layout = QHBoxLayout()

        # 왼쪽 Info 박스
        left_box = QGroupBox()
        left_layout = QHBoxLayout()
        left_box.setLayout(left_layout)

        left_layout.addWidget(icon)
        left_layout.addWidget(QLabel("Team:"))
        left_layout.addWidget(team_combo)
        left_layout.addWidget(back_label)
        left_layout.addWidget(name_label)
        left_layout.addWidget(handedness_label)

        section_layout.addWidget(left_box, stretch=1)

        # 오른쪽 테이블
        right_layout = QVBoxLayout()
        label = QLabel("📄 History")
        right_layout.addWidget(label)
        right_layout.addWidget(table)

        section_layout.addLayout(right_layout, stretch=2)
        return section_layout

    def update_player_info(self, type, status, number, handedness = ""):
        # 컨텍스트
        if type == self.conf._player_type.pitcher:
            icon, team, back_input, mode_group, name_label, handed_label = \
                self.pitcher_icon, self.pitcher_team, self.pitcher_back, self.pitcher_auto, self.pitcher_name, self.pitcher_handedness
            group = getattr(self, "pitcher_group", None)
            recent_q = getattr(self, "pitcher_recent", deque(maxlen=30))
            self.pitcher_recent = recent_q
        elif type == self.conf._player_type.batter:
            icon, team, back_input, mode_group, name_label, handed_label = \
                self.batter_icon, self.batter_team, self.batter_back, self.batter_auto, self.batter_name, self.batter_handedness
            group = getattr(self, "batter_group", None)
            recent_q = getattr(self, "batter_recent", deque(maxlen=30))
            self.batter_recent = recent_q
        else:
            return

        icon_path = self.get_status_icon_path(status)

        # 부재 처리
        if status == self.conf._object_status_absent:
            name_label.setText("")
            back_input.setText("-")
            handed_label.setText("")
            recent_q.clear()
            if group and hasattr(group, "_pending_box"):
                group._pending_box.hide()
            return

        icon.setPixmap(QPixmap(icon_path).scaled(32, 32))

        mode = mode_group.checkedId()

        if mode == MODE_AUTO:
            # 들어온 번호를 즉시 적용 (기존 Auto)
            try:
                number = int(number)
            except Exception:
                return
            back_input.setText(str(number))
            show_num = number
            if group and hasattr(group, "_pending_box"):
                group._pending_box.hide()

        elif mode == MODE_MANUAL:
            # 감지 무시, 현재 입력값 사용
            if group and hasattr(group, "_pending_box"):
                group._pending_box.hide()
            try:
                show_num = int(back_input.text().strip())
            except ValueError:
                return

        else:  # MODE_SEMI (반자동)
            # 최신 감지 번호 누적
            try:
                n = int(number)
                recent_q.append(n)
            except Exception:
                pass

            # 최신부터 '서로 다른' 3개 추출
            uniq = []
            seen = set()
            for v in reversed(recent_q):
                if v not in seen:
                    uniq.append(v)
                    seen.add(v)
                if len(uniq) == 3:
                    break

            # 후보 표시
            if group and hasattr(group, "_pending_box"):
                if len(uniq) > 0:
                    for i, btn in enumerate(group._cand_btns):
                        if i < len(uniq):
                            btn.setText(str(uniq[i]))
                            btn.setEnabled(True)
                            btn.show()
                        else:
                            btn.setText("-")
                            btn.setEnabled(False)
                            btn.hide()
                    group._pending_box.show()
                else:
                    group._pending_box.hide()

            # 현재 표시용 번호는 입력칸 값(확정 이전엔 그대로)
            try:
                show_num = int(back_input.text().strip())
            except ValueError:
                return

        # 표시용 이름/손잡이 갱신
        name = self.get_player_name(show_num, team)
        name_label.setText(f"Name: {name}")

        handedness_str = self.get_player_handedness(show_num, team)
        handed = None
        if type == self.conf._player_type.pitcher:
            if '좌투' in handedness_str:    handed = 'Left'
            elif '우투' in handedness_str:  handed = 'Right'
        elif type == self.conf._player_type.batter:
            if '좌타' in handedness_str:    handed = 'Left'
            elif '우타' in handedness_str:  handed = 'Right'
        handed_label.setText(f"Handedness: {handed}")

        # prev_* 저장(기존 유지)
        if type == self.conf._player_type.pitcher:
            if handed == 'Right'    : conf._prev_pit_handed = conf._object_handedness_right
            elif handed == 'Left'   : conf._prev_pit_handed = conf._object_handedness_left
        elif type == self.conf._player_type.batter:
            if handed == 'Right'    : conf._prev_bat_handed = conf._object_handedness_right
            elif handed == 'Left'   : conf._prev_bat_handed = conf._object_handedness_left


    ########################################################
    # player team info
    ########################################################
    def get_team_id(self, team):
        current_index  = team.currentIndex()
        return current_index
    
    def get_team_code_by_index(self, index: int):
        return getattr(conf, f"_team_code_{index}", None)
    
    ########################################################
    # player name from number
    ########################################################
    def get_player_name(self, number, team):
        if number != self.conf._object_number_unknown:                            
            if self.conf._trackman_mode and self.conf._api_client:  
                team_id = self.get_team_id(team) + 1
                pitcher_team_id = self.get_team_code_by_index(team_id)
                player_info = self.conf._api_client.get_player_info_by_backnum(
                    team_id=pitcher_team_id, season=datetime.now().year, backnum=number)
                if player_info and len(player_info) > 0:
                    return player_info[0].get("NAME", "Unknown")
        return ""

    ########################################################
    # player handedness 
    ########################################################
    def get_player_handedness(self, number, team):
        if number != self.conf._object_number_unknown:
            if self.conf._trackman_mode and self.conf._api_client:
                team_id = self.get_team_id(team) + 1
                team_code = self.get_team_code_by_index(team_id)
                player_info = self.conf._api_client.get_player_info_by_backnum(
                    team_id=team_code, season=datetime.now().year, backnum=number)
                if player_info and len(player_info) > 0:
                    # 예: "R", "L", "S" 또는 한글 반환
                    return player_info[0].get("HitType", "Unknown")
        return ""
    
    ########################################################
    # icon
    ########################################################
    def get_status_icon_path(self, status):
        status_icon_map = {
            conf._object_status_unknown         : "images/unknown.png",
            conf._object_status_absent          : "images/absent.png",
            conf._object_status_present         : "images/check.png",            
            conf._object_status_present_batter  : "images/present-batter.png",
            conf._object_status_present_pitcher : "images/present-pitcher.png",
            conf._object_status_present_golfer  : "images/present-golfer.png",            
            conf._object_status_present_nascar  : "images/present-nascar.png"
        }
        return status_icon_map.get(status, "images/unknown.png")