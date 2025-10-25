import cv2
import os
import numpy as np
import time
import subprocess
import threading
import tempfile
import shutil

from scipy.interpolate import make_interp_spline
from queue import Queue
from threading import Thread

from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log
from fd_utils.fd_file_edit      import fd_get_video_info
from fd_utils.fd_file_edit      import fd_extract_frames_from_file

debug_preview = False

class PostStabil:
    def __init__(self):
        self._logo = False
        self._logo_file = False
        self._fps = 30
        self._width = 1920
        self._height = 1080
        self._output_w = 1920
        self._output_h = 1080        
        self._input_file = ''
        self._output_file = ''
        self._overlay_file = ''        
        self._frame_buffer = []
        pass
    
    # ─────────────────────────────────────────────────────────────────────────────
    # def fd_poststabil(input_file, output_file, logo, logopath, swipeperiods,
    #                   use_multithread=False, num_threads=4)
    #
    # [date] 2025-06-09
    # 전체 파이프라인: 입력 → 트래킹 → 마스크 → 곡선 보간 → 인코딩을 포함한 메인 실행 함수
    # 멀티스레드 인코딩 옵션 추가 및 시간 로그 유지
    # ─────────────────────────────────────────────────────────────────────────────
    def fd_poststabil(self, input_file, output_file, logo, logopath, swipeperiods,
                    use_multithread=True, num_threads=4):
        
        self.swipeperiods = swipeperiods  # 인스턴스에 저장해서 encode_video에서 사용
        self._input_file = input_file
        self._output_file = output_file
        self._logo = logo
        self._logo_file = logopath

        t_total_start = time.perf_counter()
        timings = {}

        # ▶ Step 1: Load video frames
        fd_log.info(f"[Stabil]▶ Step 1: Load video frames")
        t0 = time.perf_counter()        
        self._load_video_frames(input_file)
        if not self._frame_buffer:
            return
        self._output_w, self._output_h = conf._output_width, conf._output_height
        timings["Load & Init"] = (time.perf_counter() - t0) * 1000
        
        # ▶ Step 2: Reference center point
        fd_log.info(f"[Stabil]▶ Step 2: Reference center point")        
        first_swipe = swipeperiods[0]
        ref_cx = int(first_swipe["roi_left"] + first_swipe["roi_width"] / 2)
        ref_cy = int(first_swipe["roi_top"] + first_swipe["roi_height"] / 2)
        

        # ▶ Step 3: Logo check
        if logo:
            fd_log.info(f"[Stabil]▶ Step 3: Logo check")                
            self._overlay_file = self.check_logo_overlay(logo, logopath)

        # ▶ Step 4: Tracking & transforms
        fd_log.info(f"[Stabil]▶ Step 4: Tracking & transforms")                
        t0 = time.perf_counter()
        transform_list, mask_accum = self.compute_transforms_and_mask(self._frame_buffer, swipeperiods, ref_cx, ref_cy, self._output_w, self._output_h)
        timings["Tracking & Transform"] = (time.perf_counter() - t0) * 1000
        

        # ▶ Step 5: Crop area calculation
        fd_log.info(f"[Stabil]▶ Step 5: Crop area calculation")                        
        t0 = time.perf_counter()
        crop_coords = self.compute_crop_area(mask_accum)
        timings["Crop Area Calculation"] = (time.perf_counter() - t0) * 1000
        fd_log.info(f"[Post Stabil][End] ▶ Step 5: Crop area calculation")                        

        # ▶ Step 6: Transform extrapolation
        fd_log.info(f"[Stabil]▶ Step 6: Transform extrapolation")                        
        t0 = time.perf_counter()
        total_frames = len(self._frame_buffer)
        transform_dict = self.extrapolate_transforms(transform_list, swipeperiods, total_frames)
        timings["Transform Extrapolation"] = (time.perf_counter() - t0) * 1000
        fd_log.info(f"[Post Stabil][End] ▶ Step 6: Transform extrapolation")                        

        # ▶ Step 7: Encode video
        fd_log.info(f"[Stabil]▶ Step 7: Encode video")                        
        t0 = time.perf_counter()
        if use_multithread:
            self.encode_video_multithreaded(self._frame_buffer, transform_dict, crop_coords,
                                            self._output_w, self._output_h, output_file, self._overlay_file,
                                            self._fps, num_threads)
        else:
            self.encode_video(self._frame_buffer, transform_dict, crop_coords,
                            self._output_w, self._output_h, output_file, self._overlay_file, self._fps)
        timings["Encoding"] = (time.perf_counter() - t0) * 1000
        
        
        # ▶ Total
        t_total_end = time.perf_counter()
        timings["Total Time"] = (t_total_end - t_total_start) * 1000

        # ▶ Print final report
        fd_log.info("\n\033[33m33m──────────────────────────────────────────────────────────────────────────────────────────============ \033[0m")
        fd_log.info(f"\033[33m──=========== \033[0m🚩 Post-Stabilization Output: \033[34m{output_file}\033[0m")
        for idx, (key, val) in enumerate(timings.items(), 1):
            label = f"{idx}️⃣ {key}" if "Total" not in key else f"🏁 {key}"
            fd_log.info(f"\033[33m──=========== \033[0m{label:<30}: \033[32m{val:,.2f} ms\033[0m")
        fd_log.info("\033[33m33m──────────────────────────────────────────────────────────────────────────────────────────============ \033[0m")



    # ─────────────────────────────────────────────────────────────────────────────
    # def is_similar_frame(frame1, frame2, index, threshold=4)
    # [date] 2025-06-04
    # 두 프레임 간의 회색조 차이를 평균으로 계산해 유사한 프레임인지 판단
    # ─────────────────────────────────────────────────────────────────────────────
    def is_similar_frame(self, frame1, frame2, index, threshold=4):
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY) if frame1.ndim == 3 else frame1
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY) if frame2.ndim == 3 else frame2
        diff = cv2.absdiff(gray1, gray2)
        mean_diff = np.sum(diff) / (diff.shape[0] * diff.shape[1])
        #fd_log.info(f"index{index} diff: {mean_diff}")
        return mean_diff < threshold

    # ─────────────────────────────────────────────────────────────────────────────
    # def get_ffmpeg_command(output_w, output_h, overlay_file, output_path, fps)
    # [date] 2025-06-04
    # 로고 유무에 따라 FFmpeg 커맨드 라인 옵션을 구성해 인코딩 설정 생성
    # ─────────────────────────────────────────────────────────────────────────────
    def get_ffmpeg_command(self, output_path, fps):
        output_w = self._output_w
        output_h = self._output_h
        fps = self._fps

        if self._overlay_file:
            filter_complex = (
                "[0:v]format=rgba[base];"
                "[1:v]format=rgba[ovl];"
                "[base][ovl]overlay=0:0,"
                "unsharp=5:5:0.5:5:5:0.0,"
                "scale=in_range=limited:out_range=full,"
                "colorspace=all=bt709:iall=bt709:fast=1[out]"
            )
        else:
            filter_complex = (
                "format=rgba,"
                "unsharp=5:5:0.5:5:5:0.0,"
                "scale=in_range=limited:out_range=full,"
                "colorspace=all=bt709:iall=bt709:fast=1[out]"
            )

        command = [
            "ffmpeg", "-y",
            "-loglevel", "error",
            "-thread_queue_size", "1024",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{output_w}x{output_h}",
            "-r", str(fps),
            "-i", "-"
        ]

        if self._overlay_file:
            command += ["-i", self._overlay_file]

        command += [
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "copy",
            "-preset", str(conf._output_preset),
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-b:v", conf._output_bitrate,            
            "-bufsize", "20M",
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov",
            "-probesize", "1000000",
            "-analyzeduration", "2000000",
            "-f", "mp4",
            output_path
        ]

        return command

    # ─────────────────────────────────────────────────────────────────────────────
    # def load_video_frames(input_file)
    # [date] 2025-06-04
    # 영상 파일을 메모리로 전체 프레임 로딩하여 fps, 해상도, 총 프레임 수 반환
    # ─────────────────────────────────────────────────────────────────────────────
    def _load_video_frames(self, input_file):
        # 사전 정보 가져오기
        self._fps, self._width, self._height = fd_get_video_info(input_file)
        self._frame_buffer = fd_extract_frames_from_file(input_file, conf._file_type_post_stabil)        

    # ─────────────────────────────────────────────────────────────────────────────
    # def check_logo_overlay(logo, logopath)
    # [date] 2025-06-04
    # 로고 삽입 여부 및 로고 경로 유효성 검사
    # ─────────────────────────────────────────────────────────────────────────────
    def check_logo_overlay(self, logo, logopath):
        overlay_file = None
        if logo and isinstance(logopath, str) and os.path.exists(logopath):
            overlay_file = logopath
            fd_log.info(f"[INFO] Logo overlay enabled: {overlay_file}")
        else:
            fd_log.info("[INFO] Logo overlay disabled or file not found.")
        return overlay_file

    # ─────────────────────────────────────────────────────────────────────────────
    # def compute_transforms_and_mask(frame_buffer, swipeperiods, ref_cx, ref_cy, width, height)
    # [date] 2025-06-04
    # CSRT 트래커로 중심점을 추출하고, B-spline으로 곡선 보간한 후 이동 벡터 계산 및 마스크 누적
    # ─────────────────────────────────────────────────────────────────────────────
    def compute_transforms_and_mask(self, frame_buffer, swipeperiods, ref_cx, ref_cy, width, height):
        transform_list = []
        mask_accum = np.ones((height, width), dtype=np.uint8)
        vis_frame_buffer = [f.copy() for f in frame_buffer]  # 시각화용 복사본

        for swipe in swipeperiods:
            start, end = swipe["start"], swipe["end"]
            roi = (
                int(swipe["roi_left"]),
                int(swipe["roi_top"]),
                int(swipe["roi_width"]),
                int(swipe["roi_height"])
            )
            fixed_width = roi[2]
            fixed_height = roi[3]

            fd_log.info(f"\n[INFO] Scanning swipe [{start} - {end}]...")
            tracker = cv2.TrackerCSRT_create()
            frame = frame_buffer[start]
            tracker.init(frame, roi)

            cx_seq = []
            cy_seq = []
            frame_ids = []

            cur_bbox = roi
            prev_frame = frame.copy()

            for i in range(start, end + 1):
                frame = frame_buffer[i]
                if i > start and not self.is_similar_frame(prev_frame, frame, i):
                    success, bbox = tracker.update(frame)
                    if success:
                        cx = int(bbox[0] + fixed_width / 2)
                        cy = int(bbox[1] + fixed_height / 2)
                        cur_bbox = (int(bbox[0]), int(bbox[1]), fixed_width, fixed_height)
                    prev_frame = frame.copy()
                else:
                    cx = cur_bbox[0] + fixed_width // 2
                    cy = cur_bbox[1] + fixed_height // 2

                frame_ids.append(i)
                cx_seq.append(cx)
                cy_seq.append(cy)

            # Spline 보간
            spl_x = make_interp_spline(frame_ids, cx_seq, k=3)
            spl_y = make_interp_spline(frame_ids, cy_seq, k=3)

            prev_tracked = None
            prev_stabil = None
            for i in frame_ids:
                cx, cy = cx_seq[i - start], cy_seq[i - start]
                smooth_cx = spl_x(i)
                smooth_cy = spl_y(i)
                dx = ref_cx - smooth_cx
                dy = ref_cy - smooth_cy
                M = np.float32([[1, 0, dx], [0, 1, dy]])
                transform_list.append((i, M))

                shifted = cv2.warpAffine(frame_buffer[i], M, (width, height))
                gray = cv2.cvtColor(shifted, cv2.COLOR_BGR2GRAY)
                mask = (gray > 0).astype(np.uint8)
                mask_accum = cv2.bitwise_and(mask_accum, mask)

                # ▶ 시각화용 궤적 선 그리기
                vis = vis_frame_buffer[i]
                tracked_pt = (int(cx), int(cy))
                stabil_pt = (int(smooth_cx), int(smooth_cy))
                
                cv2.circle(vis, stabil_pt, 2, (0, 255, 0), -1)      # 초록: 보정 중심                
                if prev_stabil:
                    cv2.line(vis, prev_stabil, stabil_pt, (0, 255, 0), 1)
                
                prev_stabil = stabil_pt

        # 시각화 결과 저장
        if debug_preview :
            self.preview_visualization(vis_frame_buffer, conf._output_fps)

        return transform_list, mask_accum
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # def preview_visualization(self, vis_frames, fps):
    # [date] 2025-06-04    
    # ─────────────────────────────────────────────────────────────────────────────
    def preview_visualization(self, vis_frames, fps):
        fd_log.info(f"[INFO] Previewing tracking visualization...")

        # 누적 궤적을 저장할 리스트
        tracked_trail = []
        stabil_trail = []

        for idx, f in enumerate(vis_frames):           
            
            vis = f.copy()

            # 원래 프레임에 누적된 궤적을 그리기
            for i in range(1, len(stabil_trail)):
                cv2.line(vis, stabil_trail[i - 1], stabil_trail[i], (0, 255, 0), 1)

            # 현재 프레임에 존재하는 점이면 궤적에 추가
            # (점이 시각화에 포함되어 있는 경우에만 가능)
            gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            red_mask = (f[:, :, 2] > 150) & (f[:, :, 1] < 100) & (f[:, :, 0] < 100)
            green_mask = (f[:, :, 1] > 150) & (f[:, :, 0] < 100) & (f[:, :, 2] < 100)

            # 중심점들을 찾기 (간단한 색상 기반 마스킹)
            red_points = np.column_stack(np.where(red_mask))
            green_points = np.column_stack(np.where(green_mask))

            if red_points.size > 0:
                cy, cx = np.mean(red_points, axis=0).astype(int)
                tracked_trail.append((cx, cy))

            if green_points.size > 0:
                cy, cx = np.mean(green_points, axis=0).astype(int)
                stabil_trail.append((cx, cy))

            cv2.imshow("Stabilization Tracking Preview", vis)
            key = cv2.waitKey(0)
            if key == 27:
                fd_log.info("[INFO] Preview interrupted by user.")
                break

        cv2.destroyAllWindows()

    # ─────────────────────────────────────────────────────────────────────────────
    # def extrapolate_transforms(transform_list, swipeperiods, total_frames)
    # [date] 2025-06-04
    # swipe 구간 이후 프레임에 대해 마지막 변환 행렬을 보간 적용
    # ─────────────────────────────────────────────────────────────────────────────
    def extrapolate_transforms(self, transform_list, swipeperiods, total_frames):
        transform_dict = dict(transform_list)
        for swipe_idx, swipe in enumerate(swipeperiods):
            end = swipe["end"]
            if end in transform_dict:
                last_M = transform_dict[end]
                # ✅ 타입과 shape 확인
                if not isinstance(last_M, np.ndarray) or last_M.shape != (2, 3):
                    fd_log.info(f"[WARN] Invalid transform at frame {end}, using identity.")
                    last_M = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
                next_start = swipeperiods[swipe_idx + 1]["start"] if swipe_idx + 1 < len(swipeperiods) else total_frames
                for i in range(end + 1, next_start):
                    transform_dict[i] = last_M
        return transform_dict

    # ─────────────────────────────────────────────────────────────────────────────
    # def compute_crop_area(mask_accum)
    # [date] 2025-06-04
    # 유효 프레임 누적 마스크로부터 최종 크롭 영역 계산
    # ─────────────────────────────────────────────────────────────────────────────
    def compute_crop_area(self, mask_accum):
        x, y, w, h = cv2.boundingRect(mask_accum)
        center_x = x + w // 2
        center_y = y + h // 2
        fd_log.info(f"[INFO] Bounding box center: ({center_x}, {center_y})")

        target_ratio = 16 / 9
        max_crop_w1 = w
        max_crop_h1 = int(w / target_ratio)
        max_crop_h2 = h
        max_crop_w2 = int(h * target_ratio)

        if max_crop_h1 <= h:
            crop_w = max_crop_w1
            crop_h = max_crop_h1
        else:
            crop_w = max_crop_w2
            crop_h = max_crop_h2

        crop_x0 = max(center_x - crop_w // 2, 0)
        crop_y0 = max(center_y - crop_h // 2, 0)
        crop_x1 = crop_x0 + crop_w
        crop_y1 = crop_y0 + crop_h

        max_h, max_w = mask_accum.shape
        if crop_x1 > max_w:
            crop_x0 = max_w - crop_w
            crop_x1 = max_w
        if crop_y1 > max_h:
            crop_y0 = max_h - crop_h
            crop_y1 = max_h

        fd_log.info(f"[INFO] Final 16:9 crop in mask: x={crop_x0}, y={crop_y0}, w={crop_w}, h={crop_h}")
        return int(crop_x0), int(crop_y0), int(crop_x1), int(crop_y1)

    # ─────────────────────────────────────────────────────────────────────────────
    # def encode_video(frame_buffer, transform_dict, crop_coords, output_w, output_h, output_file, overlay_file, fps)
    # [date] 2025-06-04
    # 이동 행렬 적용 및 크롭/리사이즈 후 실시간 FFmpeg 파이프 인코딩 수행
    # ─────────────────────────────────────────────────────────────────────────────
    def encode_video(self, frame_buffer, transform_dict, crop_coords, output_w, output_h, output_file, overlay_file, fps):
        crop_x0, crop_y0, crop_x1, crop_y1 = crop_coords
        ffmpeg_command = self.get_ffmpeg_command(output_w, output_h, overlay_file, output_file, fps)
        fd_log.info("[INFO] Starting real-time encoding with FFmpeg...")

        process = subprocess.Popen(
            ffmpeg_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=10**8
        )

        try:
            for i, frame in enumerate(frame_buffer):
                M = transform_dict.get(i)
                shifted = self._warp_frame_gpu(frame, M)

                # Crop + Resize
                cropped = shifted[crop_y0:crop_y1, crop_x0:crop_x1]
                resized = self._resize_gpu(cropped, (output_w, output_h))
                process.stdin.write(resized.tobytes())

                if i % 50 == 0:
                    fd_log.info(f"[ENCODE] Processed frame {i}/{len(frame_buffer)}")

            process.stdin.close()
            process.communicate(timeout=60)
        except Exception as e:
            fd_log.info(f"[ERROR] Encoding failed: {e}")
            process.kill()


    def _resize_gpu(self, frame, size):
        gpu_frame = cv2.cuda_GpuMat()
        gpu_frame.upload(frame)
        gpu_resized = cv2.cuda.resize(gpu_frame, size)
        return gpu_resized.download()

    def _warp_frame_gpu(self, frame, M):
        # translation-only 최적화 with remap
        if M is None:
            M = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
        else:
            M = np.array(M, dtype=np.float32)
            if M.size != 6:
                M = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
            else:
                M = M.reshape((2, 3))

        dx, dy = M[0, 2], M[1, 2]
        h, w = frame.shape[:2]

        x_map, y_map = np.meshgrid(np.arange(w), np.arange(h))
        x_map = (x_map - dx).astype(np.float32)
        y_map = (y_map - dy).astype(np.float32)

        gpu_frame = cv2.cuda_GpuMat()
        gpu_frame.upload(frame)
        gpu_x_map = cv2.cuda_GpuMat()
        gpu_y_map = cv2.cuda_GpuMat()
        gpu_x_map.upload(x_map)
        gpu_y_map.upload(y_map)

        gpu_shifted = cv2.cuda.remap(gpu_frame, gpu_x_map, gpu_y_map, interpolation=cv2.INTER_LINEAR)
        return gpu_shifted.download()


    # ─────────────────────────────────────────────────────────────────────────────
    # def encode_video_multithreaded(self, frame_buffer, transform_dict, crop_coords, output_w, output_h, output_file, overlay_file, fps, num_threads=4)
    # [date] 2025-06-09
    # 멀티스레딩으로 프레임을 분할해 여러 FFmpeg 프로세스에 병렬 인코딩 수행 후 파일 병합
    # ─────────────────────────────────────────────────────────────────────────────
    def encode_video_multithreaded(self, frame_buffer, transform_dict, crop_coords,
                                  output_w, output_h, output_file, overlay_file, fps,
                                  num_threads=4):
        crop_x0, crop_y0, crop_x1, crop_y1 = crop_coords
        total_frames = len(frame_buffer)
        chunk_size = (total_frames + num_threads - 1) // num_threads

        temp_dir = tempfile.mkdtemp(prefix="poststab_enc_")
        temp_files = [os.path.join(temp_dir, f"part_{i}.mp4") for i in range(num_threads)]

        def worker(thread_idx):
            start_idx = thread_idx * chunk_size
            end_idx = min(start_idx + chunk_size, total_frames)
            fd_log.info(f"[THREAD-{thread_idx}] Encoding frames {start_idx} to {end_idx - 1}")

            ffmpeg_command = self.get_ffmpeg_command(temp_files[thread_idx], fps)

            process = subprocess.Popen(
                ffmpeg_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=10**8
            )
            try:
                for i in range(start_idx, end_idx):
                    frame = frame_buffer[i]
                    M = transform_dict.get(i)
                    shifted = self._warp_frame_gpu(frame, M)

                    cropped = shifted[crop_y0:crop_y1, crop_x0:crop_x1]
                    resized = self._resize_gpu(cropped, (output_w, output_h))

                    process.stdin.write(resized.tobytes())

                    if (i - start_idx) % 50 == 0:
                        fd_log.info(f"[THREAD-{thread_idx}] Processed frame {i - start_idx}/{end_idx - start_idx}")

                process.stdin.close()
                process.communicate(timeout=60)
                fd_log.info(f"[THREAD-{thread_idx}] Finished encoding.")
            except Exception as e:
                fd_log.info(f"[THREAD-{thread_idx} ERROR] Encoding failed: {e}")
                process.kill()

        threads = []
        for t in range(num_threads):
            thread = threading.Thread(target=worker, args=(t,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # 파일 병합 - concat demuxer 사용
        concat_list_path = os.path.join(temp_dir, "concat_list.txt")
        with open(concat_list_path, "w") as f:
            for temp_file in temp_files:
                f.write(f"file '{temp_file}'\n")

        concat_command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy",
            output_file
        ]
        fd_log.info("[INFO] Merging encoded parts into final output...")
        subprocess.run(concat_command, check=True)
        fd_log.info("[INFO] Merge completed.")

        # 임시 디렉터리 삭제
        shutil.rmtree(temp_dir)
        fd_log.info("[INFO] Temporary files cleaned up.")
