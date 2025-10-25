# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# video file editing
# - 2025-07-29
# - Yerin Kim
# ─────────────────────────────────────────────────────────────────────────────#
# 1. Load pkl file
# 2. copy files (from input to output)
# 3. generating command (_, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1500,3500,30,100,1922,16)       
# ─────────────────────────────────────────────────────────────────────────────#
import os
import re
import shutil
from tqdm import tqdm

_input_path_d = "Y:\\"
_input_path_e = "Z:\\"
_output_root = "C:\\4DReplay\\v4_aid\\videos\\input\\baseball\\KBO"

##### 1. Load pkl file #####

def get_time_range_files(pkl_path, before_ms=1500, after_ms=3500, fps=60, dur_ms=1000):
    folder = os.path.dirname(pkl_path)
    pkl_name = os.path.basename(pkl_path)

    match = re.match(r'(\d+_\d+)_(\d+)_.*\.pkl', pkl_name)
    if not match:
        print(f"❌ 잘못된 파일명 형식: {pkl_name}")
        return []

    base_key = match.group(1)
    frame_index = int(match.group(2))

    mp4_files = [f for f in os.listdir(folder) if f.endswith('.mp4') and f.startswith(base_key.split('_')[0])]
    mp4_files.sort(key=lambda x: int(x.split('_')[1].split('.')[0]))

    ref_filename = f"{base_key}.mp4"
    if ref_filename not in mp4_files:
        print(f"❌ 기준 mp4 없음: {ref_filename}")
        return []

    ref_idx = mp4_files.index(ref_filename)
    # ref_path = os.path.join(folder, ref_filename)
    ref_time_ms = (frame_index / fps) * 1000

    selected = []

    # ▶ 뒤쪽 누적
    acc_after = 0
    for i in range(ref_idx, len(mp4_files)):
        full = os.path.join(folder, mp4_files[i])

        dur_to_use = dur_ms - ref_time_ms if i == ref_idx else dur_ms
        selected.append(full)
        acc_after += dur_to_use
        if acc_after >= after_ms:
            break

    # ◀ 앞쪽 누적
    acc_before = ref_time_ms
    for i in range(ref_idx - 1, -1, -1):
        full = os.path.join(folder, mp4_files[i])
        acc_before += dur_ms
        selected.insert(0, full)
        if acc_before >= before_ms:
            break

    return selected

# ▶ 전체 폴더 순회
_input_folder_d = [_input_path_d + path for path in os.listdir(_input_path_d)]
_input_folder_e = [_input_path_e + path for path in os.listdir(_input_path_e)]

pkl_paths = []
for folder in (_input_folder_d + _input_folder_e):
    pkl_files = [f for f in os.listdir(folder) if f.endswith('.pkl')]
    pkl_paths.extend([os.path.join(folder, f) for f in pkl_files])

# ▶ 결과 수집: pkl 기준으로 mp4 리스트 저장
filtered_by_pkl = dict()

for pkl in pkl_paths:
    selected = get_time_range_files(pkl)
    if selected:
        filtered_by_pkl[pkl] = selected

# ▶ 전체 mp4 수 확인
total_mp4_count = sum(len(v) for v in filtered_by_pkl.values())
print(f"📂 기준 pkl 개수: {len(filtered_by_pkl)}")
print(f"🎬 총 추출된 mp4 파일 수: {total_mp4_count}")


##### 2. copy files (from input to output) #####

os.makedirs(_output_root, exist_ok=True)

# ▶ 복사 수행
for pkl_path, mp4_list in tqdm(filtered_by_pkl.items(), desc="📦 pkl+mp4 복사", unit="pkl"):
    # 예: Y:\2025_07_22_18_19_10\027011_1535_46_baseball_data.pkl
    parent_folder = os.path.basename(os.path.dirname(pkl_path))
    dst_folder = os.path.join(_output_root, parent_folder)
    os.makedirs(dst_folder, exist_ok=True)

    # ✅ 1. pkl 파일 복사
    pkl_dst_path = os.path.join(dst_folder, os.path.basename(pkl_path))
    if not os.path.exists(pkl_dst_path):
        try:
            shutil.copy2(pkl_path, pkl_dst_path)
        except Exception as e:
            print(f"❌ pkl 복사 실패: {pkl_path} → {e}")

    # ✅ 2. 해당 mp4 파일들 복사
    for mp4_path in mp4_list:
        filename = os.path.basename(mp4_path)
        dst_path = os.path.join(dst_folder, filename)

        if os.path.exists(dst_path):
            continue

        try:
            shutil.copy2(mp4_path, dst_path)
        except Exception as e:
            print(f"❌ mp4 복사 실패: {mp4_path} → {e}")


##### 3. generating command #####

command_list = []

# ▶ 하위 폴더 순회
for date_folder in os.listdir(_output_root):
    folder_path = os.path.join(_output_root, date_folder)
    if not os.path.isdir(folder_path):
        continue

    # ▶ 하위 폴더 안의 pkl 파일들 확인
    for file in os.listdir(folder_path):
        if not file.endswith('.pkl'):
            continue

        filename = file  # 예: 027011_1537_19_baseball_data.pkl
        name_parts = filename.split('_')

        if len(name_parts) < 4:
            print(f"⚠️ 파일명 형식 이상: {filename}")
            continue

        camera_id = name_parts[0]              # '027011'
        camera_ip_class = int(camera_id[:3])   # '027' → 27
        camera_ip_list = int(camera_id[3:])    # '011' → 11
        select_time = int(name_parts[1])       # '1537'
        select_frame = int(name_parts[2])      # '19'

        # 조건에 따른 시간 설정
        start_time, end_time = -1500, 3000  # 12, 13
        if camera_ip_list == 11:
            end_time = 3500
        if camera_ip_list == 14:
            end_time = 0

        # 0x01xx 코드 생성
        hex_camera_code = f"0x01{camera_ip_list:02d}"

        # ▶ command 문자열 구성 (상대경로로)
        command = (
            f"_, output = aid.create_ai_file("
            f"{hex_camera_code}, "
            f"'./videos/input/baseball/KBO/{date_folder}', "
            f"'./videos/output/baseball', "
            f"{camera_ip_class}, {camera_ip_list}, "
            f"{start_time}, {end_time}, "
            f"30, 100, "
            f"{select_time}, {select_frame})"
        )

        command_list.append(command)