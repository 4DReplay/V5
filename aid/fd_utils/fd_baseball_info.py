import requests
import json
import os

import fd_utils.fd_config as conf
from fd_utils.fd_logging import fd_log

class BaseballAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-Api-Key": api_key}
        self.memory_cache = {
            "teams": None,
            "team_players": {},  # team_id → players list
            "players": {}        # pid → object dict
        }

        if conf._extra_all_star:
            self.load_player_data_from_json(conf.resource("aid/fd_utils/all_star_teams.json"))

    def load_player_data_from_json(self, json_path):
        try:
            base_dir = os.path.dirname(__file__)
            full_path = os.path.join(base_dir, json_path)

            with open(full_path, "r", encoding="utf-8") as f:
                data = json.load(f)  # list 타입

            players_list = []

            for team in data:
                team_code = team.get("team_code")
                team_name = team.get("team_name")
                players = team.get("players", [])

                for player in players:
                    # 팀 정보 주입
                    player["team_code"] = team_code
                    player["team_name"] = team_name
                    players_list.append(player)

            self.memory_cache["players"] = players_list
            fd_log.info(f"📂 선수 정보 JSON 로드 완료: {len(players_list)}명")

        except Exception as e:
            import traceback
            fd_log.error(f"❌ 선수 JSON 파일 로드 실패: {e}")
            traceback.print_exc()



    def get_teams_info(self):
        if self.memory_cache["teams"] is not None:
            return self.memory_cache["teams"]

        url = "https://jhcugyn16h.execute-api.ap-northeast-2.amazonaws.com/api/teams_table_ver2"
        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            teams = response.json().get("result", [])
            self.memory_cache["teams"] = teams
            return teams
        else:
            fd_log.error(f"❌ 팀 정보 조회 실패: {response.status_code}")
            return []

    def get_team_players_info(self, team_id: int, season: int):
        if conf._extra_all_star:
            return [p for p in self.memory_cache["players"] if p.get("team_code") == str(team_id)]
    
        if str(team_id) in self.memory_cache["team_players"]:
            return self.memory_cache["team_players"][str(team_id)]

        url = f"https://jhcugyn16h.execute-api.ap-northeast-2.amazonaws.com/api/team_player_info_table_ver1?season={season}&team_id={team_id}"
        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            players = response.json().get("result", [])
            self.memory_cache["team_players"][str(team_id)] = players
            return players
        else:
            fd_log.error(f"❌ 팀 선수 조회 실패: {response.status_code}")
            return []

    def get_player_info(self, pid: int, season: int):
        if pid in self.memory_cache["players"]:
            return self.memory_cache["players"][pid]

        url = f"https://jhcugyn16h.execute-api.ap-northeast-2.amazonaws.com/api/player_info_table_ver1?season={season}&pid={pid}"
        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            player = response.json().get("result", {})
            self.memory_cache["players"][pid] = player
            return player
        else:
            fd_log.error(f"❌ 선수 정보 조회 실패: {response.status_code}")
            return {}

    def get_player_info_by_backnum(self, team_id: int, season: int, backnum):
        players = self.get_team_players_info(team_id, season)
        for player in players:
            if str(player.get("BackNum", "")).strip() == str(backnum).strip():
                if conf._extra_all_star:
                    # ID 없이 JSON player 객체 그대로 반환
                    return player
                # else:
                #     pid = player.get("ID")
                #     return self.get_player_info(pid, season)
                else:
                    pid = player.get("ID")
                    player_info = self.get_player_info(pid, season)
                    if str(team_id) == "7" and str(backnum) == "30":
                        player_info[0]["NAME"] = "톨허스트"
                        fd_log.info("✅ team_id=7, BackNum=30 선수 이름을 톨허스트로 변경 완료")
                    if str(team_id) == "16" and str(backnum) == "3":
                        player_info[0]["NAME"] = "스티븐슨"
                        fd_log.info("✅ team_id=16, BackNum=3 선수 이름을 스티븐슨로 변경 완료")
                    if str(team_id) == "16" and str(backnum) == "32":
                        player_info[0]["NAME"] = "패트릭"
                        fd_log.info("✅ team_id=16, BackNum=32 선수 이름을 패트릭로 변경 완료")
                    return player_info
                
        fd_log.error(f"❌ 등번호 '{backnum}'에 해당하는 선수를 찾을 수 없습니다.")
        return None    
    
    def cache_all_active_team_players(self, season: int):
        """활성화된 KBO1 팀들의 선수 정보를 모두 메모리에 저장"""
        teams = self.get_teams_info()
        active_teams = [t for t in teams if t.get("level") == "kbo1" and t.get("active") == "Y"]        

        fd_log.info(f"🔎 활성화된 팀 {len(active_teams)}개에 대해 선수 정보 캐싱 시작")

        for team in active_teams:
            team_id = team.get("team_id1")
            try:
                fd_log.info(f"⏳ 팀 {team_id} 선수 정보 요청 중...")
                players = self.get_team_players_info(team_id, season)
                fd_log.info(f"✅ 팀 {team_id} 선수 {len(players)}명 캐시 완료")
            except Exception as e:
                fd_log.error(f"❌ 팀 {team_id} 선수 조회 중 오류 발생: {e}")

        fd_log.info("🏁 모든 active 팀 선수 정보 캐싱 완료")
