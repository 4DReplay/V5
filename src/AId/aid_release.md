# ─────────────────────────────────────────────────────────────────────────────
# aid_release.md
# ─────────────────────────────────────────────────────────────────────────────
# 📦 Release Notes - [AId]
# ─────────────────────────────────────────────────────────────────────────────

## 📅 Version [5.0.0.0] - 2025-10-20
### ✨ New Features
- [Multi Distribution] : 분산처리를 통한 프로세스 효율화
  As-Is : Single Process
  To-Be : Multi Processing
  Effect : Fast Processing
### ⚙️ Improvements / Changes
- [Configuration] : Property, Config 정리
  As-Is : aid_cfg.json, fd_config.py 로 변수와 함수들에 정의 없이 혼재되어 있고, 분산처리를 위해서는 통합 관리 되어야 함
  To-Be : 
  - 외부입력 -> aid_config_public.json5 (editior가 아닌, web으로 수정가능)
  - 내부사용 -> aid_config_private.json5 (변수 전용, 주석 사용 가능), fd_config_manager.py (코드내 사용할 수 있도록, 변수/함수 전용)
  Effect : 
  - Config 유지보수, 관리 용이
  - 원격지에서의 편집이 가능함. (Web Browser를 통한 편집, 저장)
### 📁 Contributors
- @hongsu (core developer)
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

## 📅 Version [A.B.C.D] - YYYY-MM-DD
### ✨ New Features
- [기능명] :
  As-Is :
  To-Be :
  Effect :
### ⚙️ Improvements / Changes
- [기능명] :
  As-Is :
  To-Be :
  Effect :
### 🐛 Bug Fixes
- [함수/모듈명] :
  Cause :
  Fix :
  Effect :
### 📦 Dependencies
- OpenCV:
- Python:
### 📁 Contributors
