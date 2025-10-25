# ─────────────────────────────────────────────────────────────────────────────#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_2d_draw
# - 2024/10/28
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from fd_utils.fd_logging        import fd_log


def generate_3d_trajectory(speed, vertical_angle, horizontal_angle, hangtime, steps=50):
    g = 9.81  # 중력 가속도 (m/s^2)
    theta = np.radians(vertical_angle)
    phi = np.radians(horizontal_angle)

    # 속도 분해 (3D)
    vx = speed * np.cos(theta) * np.cos(phi)
    vy = speed * np.cos(theta) * np.sin(phi)
    vz = speed * np.sin(theta)

    t = np.linspace(0, hangtime, steps)
    
    x = vx * t
    y = vy * t
    z = vz * t - 0.5 * g * t**2  # z축은 위로

    # 클램핑: 지면에 도달했을 때까지
    valid_indices = z >= 0
    x = x[valid_indices]
    y = y[valid_indices]
    z = z[valid_indices]

    trajectory = np.stack([x, y, z], axis=1)  # shape: (N, 3)
    return trajectory


trajectory_3d = generate_3d_trajectory(
    157.46458,
    28.011257,
    -28.676937,
    4.888302,
    steps=100
)

fig = plt.figure()
ax = fig.add_subplot(111, projection='3d')

x, y, z = trajectory_3d[:,0], trajectory_3d[:,1], trajectory_3d[:,2]

ax.plot(x, y, z, label='Ball Trajectory', linewidth=2)
ax.scatter(x[0], y[0], z[0], color='green', label='Start')
ax.scatter(x[-1], y[-1], z[-1], color='red', label='End')

ax.set_xlabel('X')
ax.set_ylabel('Y')
ax.set_zlabel('Z (Height)')
ax.set_title('3D Ball Trajectory')
ax.legend()
plt.show()



# ─────────────────────────────────────────────────────────────────────────────#
#
# def CreateTrackingVideo(
#   folder_input, 
#   folder_output, 
#   strCameraID, 
#   nCameraIndex, 
#   nCameraSequence, 
#   array):
#
# ─────────────────────────────────────────────────────────────────────────────#
'''
def fd_draw_3d(array_3d):
    

    num_frames = len(array_3d)
    num_keypoints = len(array_3d[0])

    # Shape: (num_frames, 3, num_keypoints)
    pose_array = np.array(array_3d)  # Shape: (num_keypoints, num_frames, 3)

    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')

    axis_lim = [[0 for z in range(2)] for y in range(3)]
    axis_lim[0][0] = 0
    axis_lim[0][1] = 0
    axis_lim[1][0] = 0
    axis_lim[1][1] = 0
    axis_lim[2][0] = -1
    axis_lim[2][1] = 1

    for frame_idx in range(num_frames):
        pose_3d = pose_array[frame_idx]
        fd_plot_3d_pose(pose_3d, ax, axis_lim)
        plt.title(f"Frame {frame_idx + 1}")
        plt.pause(0.1)  # Pause to create an animation effect

    plt.show()'
    
'''