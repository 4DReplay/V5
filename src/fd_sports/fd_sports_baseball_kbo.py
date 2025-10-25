# ─────────────────────────────────────────────────────────────────────────────#
# fd_sports_baseball_kbo.py
# - 2025/10/19
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

def get_team_code_by_index(index: int):
    return getattr(conf, f"_team_code_{index}", None)