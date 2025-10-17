# ─────────────────────────────────────────────────────────────────────────────#
# utility functions
# - 2025-10-15
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

def fd_format_elapsed_time(elapsed_sec: float) -> str:
    """Format seconds as human-readable h m s string."""
    hours = int(elapsed_sec // 3600)
    minutes = int((elapsed_sec % 3600) // 60)
    seconds = elapsed_sec % 60

    if hours > 0:
        return f"{hours}h {minutes}m {seconds:.2f}s"
    elif minutes > 0:
        return f"{minutes}m {seconds:.2f}s"
    else:
        return f"{seconds:.2f}s"