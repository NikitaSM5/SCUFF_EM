"""Pixel antennas on a grounded dielectric, calculated by native SCUFF-EM."""
from .geometry import AntennaGeometry, Substrate, create_planar_antenna, load_geometry
from .solver import AntennaResult, solve_antenna
from .storage import save_matrix, save_result
from .farfield import calculate_gain

__all__ = [
    "AntennaGeometry", "AntennaResult", "Substrate", "create_planar_antenna",
    "load_geometry", "solve_antenna", "save_matrix", "save_result", "calculate_gain",
]
