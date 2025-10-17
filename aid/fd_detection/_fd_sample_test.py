import numpy as np
import matplotlib.pyplot as plt
from filterpy.kalman import KalmanFilter
from scipy.interpolate import Akima1DInterpolator, CubicSpline

# 유효 좌표
valid_x = [977, 705, 666, 640, 619, 604, 585, 572, 564]
valid_y = [640, 166, 162, 205, 271, 354, 458, 565, 696]
t_points = np.arange(len(valid_x))
t_dense = np.linspace(t_points[0], t_points[-1], 1000)

# Akima 보간
akima_x = Akima1DInterpolator(t_points, valid_x)(t_dense)
akima_y = Akima1DInterpolator(t_points, valid_y)(t_dense)
akima_measurements = np.column_stack((akima_x, akima_y))

# Kalman Filter 설정
kf = KalmanFilter(dim_x=4, dim_z=2)
dt = 1.0
kf.F = np.array([[1, 0, dt, 0],
                 [0, 1, 0, dt],
                 [0, 0, 1,  0],
                 [0, 0, 0,  1]])
kf.H = np.array([[1, 0, 0, 0],
                 [0, 1, 0, 0]])
kf.x = np.array([akima_x[0], akima_y[0], 0, 0])
kf.P *= 1000.
kf.R *= 10.0
kf.Q = np.eye(4)

# Kalman 필터 적용
kalman_filtered = []
for z in akima_measurements:
    kf.predict()
    kf.update(z)
    kalman_filtered.append(kf.x[:2].copy())
kalman_filtered = np.array(kalman_filtered)

# Cubic Spline 보간
cs_x = CubicSpline(t_points, valid_x)(t_dense)
cs_y = CubicSpline(t_points, valid_y)(t_dense)

# 시각화
plt.figure(figsize=(10, 6))
plt.plot(akima_x, akima_y, label="Akima 보간", color='skyblue')
plt.plot(kalman_filtered[:, 0], kalman_filtered[:, 1], label="Kalman 필터 결과", color='darkgreen')
plt.plot(cs_x, cs_y, label="CubicSpline 보간", color='orange', linestyle='--')
plt.scatter(valid_x, valid_y, color='red', label='유효 좌표')
plt.gca().invert_yaxis()
plt.xlabel("X")
plt.ylabel("Y")
plt.title("Akima + Kalman + CubicSpline 비교")
plt.legend()
plt.grid(True)
plt.tight_layout()
plt.show()
