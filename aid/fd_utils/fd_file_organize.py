
import os
import ctypes
from fd_utils.fd_logging import fd_log

# 파일 잠금을 강제로 해제하고 삭제하는 함수
def force_delete(file_path):
    try:
        # 파일 속성을 제거 (읽기 전용 등)
        FILE_ATTRIBUTE_NORMAL = 0x80
        ctypes.windll.kernel32.SetFileAttributesW(file_path, FILE_ATTRIBUTE_NORMAL)

        # 파일 삭제
        os.remove(file_path)
        fd_log.info(f"Deleted: {file_path}")
    except PermissionError:
        fd_log.info(f"PermissionError: Could not delete {file_path}")
    except FileNotFoundError:
        fd_log.info(f"FileNotFoundError: File not found {file_path}")
    except Exception as e:
        fd_log.info(f"Error: {e}")
# 삭제할 파일이 있는 디렉토리 경로
'''
folder_path = r"D:\OneDrive - 4DREPLAY\1.Workspace\_Dev\v4_aid\videos\input\cricket\2024_12_16_11_27_21"
# 제외할 파일 번호 목록
exclude_ranges = [
    (135, 139), (310, 313), (352, 357), (455, 463), (691, 696), 
    (767, 772), (846, 850), (903, 908), (1038, 1044), (1079, 1085),
    (1119, 1126), (1169, 1175), (1212, 1219), (1276, 1280), (1325, 1329),
    (1363, 1369), (1405, 1411), (1445, 1450), (1544, 1549), (1592, 1596),
    (1633, 1639), (1680, 1685), (1725, 1730), (1775, 1780), (1830, 1835),
    (1881, 1886), (1924, 1928), (1964, 1967), (2056, 2060), (2208, 2213),
    (2256, 2260), (2309, 2313), (2351, 2354), (2395, 2399)
]
'''
#'''
folder_path = r"D:\OneDrive - 4DREPLAY\1.Workspace\_Dev\v4_aid\videos\input\cricket\2024_12_15_14_02_46"
# 제외할 파일 번호 목록
exclude_ranges = [
    (103, 109), (170, 174), (1066, 1070), (1108, 1113), (1161, 1166), 
    (1209, 1213), (1264, 1268), (1303, 1307), (1384, 1388), (1419, 1421),
    (1438, 1441), (1476, 1479), (1490, 1493), (1529, 1533), (1734, 1738),
    (1810, 1814), (1850, 1853), (1893, 1896), (1960, 1963), (2009, 2012),
    (2032, 2035)]
#'''

# 파일 패턴 설정
start_prefix = 1  # 시작 접두사 숫자 (001011)
end_prefix = 90    # 끝 접두사 숫자 (001070)
start_number = 1    # 시작 번호
end_number = 2452   # 끝 번호
file_extension = ".mp4"  # 삭제할 확장자

# 제외 범위 확인 함수
def is_excluded(file_number, exclude_ranges):
    for start, end in exclude_ranges:
        if start <= file_number <= end:
            return True
    return False

# 강제 삭제 실행
try:
    for prefix in range(start_prefix, end_prefix + 1):
        folder_prefix = f"0010{prefix}"
        for file_number in range(start_number, end_number + 1):
            if not is_excluded(file_number, exclude_ranges):
                file_name = f"{folder_prefix}_{file_number}{file_extension}"
                file_path = os.path.join(folder_path, file_name)
                if os.path.exists(file_path):
                    force_delete(file_path)
                else:
                    fd_log.info(f"File not found: {file_path}")
            else:
                fd_log.info(f"Excluded: {folder_prefix}_{file_number}{file_extension}")
except Exception as e:
    fd_log.info(f"An unexpected error occurred: {e}")
