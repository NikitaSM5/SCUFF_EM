"""Upper-half-space Gain from planar SCUFF RWG currents, exp(-i*omega*t).

Reciprocity gives the far field from the current's 2D Fourier transform and
the TE/TM plane-wave response of a PEC-backed dielectric slab. Gain uses
accepted port power, not radiated power (directivity) or incident power
(realised gain). Lengths and current density retain SCUFF's native mm units.
"""
import math

import numpy as np
from numpy.polynomial.legendre import leggauss

ZVAC = 376.73031346177
C_MM_PER_S = 299792458000.0


def _wavenumber(result):
    # Stored omega is SCUFF's native wavenumber. Reuse it even for older runs
    # whose GHz conversion used a rounded speed of light.
    value = result.metadata.get("omega", 2*np.pi*result.metadata["frequency_ghz"]*1e9/C_MM_PER_S)
    if not np.isfinite(value) or value <= 0:
        raise ValueError("Invalid SCUFF wavenumber")
    return value


def slab_factors(theta, k, thickness_mm, epsilon):
    """TE field multiplier and TM multiplier including cos(theta).

    Tangential admittances (in units of 1/Zvac): TE=(cos(theta), gamma),
    TM=(1/cos(theta), epsilon/gamma). The lower load has reflection -1.
    Using the round-trip exponential avoids overflow in lossy/evanescent slabs.
    """
    # One-sided grazing limit also covers a slab-mode cutoff at the horizon.
    c = np.maximum(np.cos(theta), 1e-8)
    gamma = np.sqrt((epsilon-1)+c*c+0j)
    propagation = np.exp(2j*k*thickness_mm*gamma)
    quotient = np.empty_like(gamma)
    small = np.abs(gamma) < 1e-10
    quotient[small] = -2j*k*thickness_mm
    quotient[~small] = -np.expm1(2j*k*thickness_mm*gamma[~small])/gamma[~small]
    te = 2*c*quotient/(c*quotient+1+propagation)
    tm = 2*c*gamma**2*quotient/(gamma**2*quotient+epsilon*c*(1+propagation))
    return te, tm


def current_quadrature(vertices, triangles, rwg, currents, order):
    """Gauss-Legendre/Duffy triangle samples of the affine RWG surface current."""
    xyz = vertices[triangles]
    twice_area = np.linalg.norm(np.cross(xyz[:, 1]-xyz[:, 0], xyz[:, 2]-xyz[:, 0]), axis=1)
    if np.any(twice_area <= 0):
        raise ValueError("Degenerate radiation mesh triangle")
    lengths = np.linalg.norm(vertices[rwg[:, 0]]-vertices[rwg[:, 1]], axis=1)
    slope = np.zeros(len(triangles), dtype=complex)
    offset = np.zeros((len(triangles), 2), dtype=complex)
    for q_column, panel_column, sign in ((2, 4, 1), (3, 5, -1)):
        panel = rwg[:, panel_column]
        if np.any(panel < 0):
            raise ValueError("Gain requires full RWGs for the planar voltage-gap model")
        coefficient = sign*currents*lengths/twice_area[panel]
        np.add.at(slope, panel, coefficient)
        np.add.at(offset, panel, -coefficient[:, None]*vertices[rwg[:, q_column], :2])
    nodes, weights = leggauss(order)
    nodes, weights = (nodes+1)/2, weights/2
    u, v = np.meshgrid(nodes, nodes, indexing="ij")
    wu, wv = np.meshgrid(weights, weights, indexing="ij")
    barycentric = np.column_stack(((1-u).ravel()*(1-v).ravel(), u.ravel(), ((1-u)*v).ravel()))
    points = np.einsum("qi,tic->tqc", barycentric, xyz[:, :, :2])
    j = slope[:, None, None]*points+offset[:, None, :]
    area_weights = twice_area[:, None]*(wu*wv*(1-u)).ravel()[None, :]
    return points.reshape(-1, 2), (j*area_weights[:, :, None]).reshape(-1, 2)


def radiation_intensity(result, theta_deg, phi_deg, *, quadrature_order=8):
    """Return U(theta,phi) in W/sr; no power normalisation or FEKO data involved."""
    geometry = result.metadata["geometry"]
    if geometry.get("feed", {}).get("model") != "planar_delta_gap_voltage":
        raise ValueError("Gain is supported only for the planar voltage-gap feed")
    if not np.allclose(result.vertices_mm[:, 2], 0, atol=1e-10, rtol=0):
        raise ValueError("Gain requires currents in the z=0 plane")
    theta, phi = np.broadcast_arrays(np.deg2rad(theta_deg), np.deg2rad(phi_deg))
    shape = theta.shape
    theta, phi = theta.ravel(), phi.ravel()
    if not np.isfinite(theta).all() or not np.isfinite(phi).all() or np.any((theta < 0) | (theta > np.pi/2)):
        raise ValueError("Expected finite upper-hemisphere angles")
    if type(quadrature_order) is not int or not 2 <= quadrature_order <= 32:
        raise ValueError("Invalid far-field quadrature order")
    k = _wavenumber(result)
    points, current_weights = current_quadrature(result.vertices_mm, result.triangles, result.rwg,
                                                result.x, quadrature_order)
    spectrum = np.empty((len(theta), 2), dtype=complex)
    # Bound the temporary phase matrix to roughly 16 MB, independent of mesh size.
    chunk = max(1, min(128, 1000000//len(points)))
    for first in range(0, len(theta), chunk):
        sl = slice(first, first+chunk)
        qx, qy = k*np.sin(theta[sl])*np.cos(phi[sl]), k*np.sin(theta[sl])*np.sin(phi[sl])
        phase = np.exp(-1j*(qx[:, None]*points[None, :, 0]+qy[:, None]*points[None, :, 1]))
        spectrum[sl] = np.einsum("aq,qc->ac", phase, current_weights, optimize=False)
    parallel = spectrum[:, 0]*np.cos(phi)+spectrum[:, 1]*np.sin(phi)
    transverse = -spectrum[:, 0]*np.sin(phi)+spectrum[:, 1]*np.cos(phi)
    substrate = geometry["substrate"]
    epsilon = substrate["epsilon_r"]*(1+1j*substrate["loss_tangent"])
    te, tm = slab_factors(theta, k, substrate["thickness_mm"], epsilon)
    intensity = k*k*ZVAC/(32*np.pi**2)*(np.abs(tm*parallel)**2+np.abs(te*transverse)**2)
    if not np.isfinite(intensity).all():
        raise RuntimeError("Non-finite SCUFF radiation intensity")
    return intensity.reshape(shape)


def calculate_gain(result, angle_step_deg=5):
    """Sample Gain on the same theta=0..90, phi=0..360 grid as FEKO."""
    if type(angle_step_deg) is not int or angle_step_deg <= 0 or 90 % angle_step_deg:
        raise ValueError("Angular step must be a positive integer divisor of 90")
    z = complex(*result.metadata["input_impedance_ohm"])
    if not np.isfinite(z) or z == 0:
        raise ValueError("Gain requires a finite, nonzero port impedance")
    voltage = complex(*result.metadata.get("port_voltage_V", [1, 0]))
    accepted = .5*(voltage*(voltage/z).conjugate()).real
    if not np.isfinite(accepted) or accepted <= 0:
        raise ValueError("Gain is undefined for non-positive accepted port power")
    theta = np.arange(0, 91, angle_step_deg, dtype=float)
    phi = np.arange(0, 361, angle_step_deg, dtype=float)
    tt, pp = np.meshgrid(theta, phi)
    vertices = result.vertices_mm[result.triangles]
    max_edge = np.linalg.norm(vertices-np.roll(vertices, 1, axis=1), axis=2).max()
    k = _wavenumber(result)
    order = max(6, math.ceil(k*max_edge)+4)
    if order > 24:
        raise ValueError("Radiation mesh is electrically too coarse; refine the mesh")
    intensity = radiation_intensity(result, tt, pp, quadrature_order=order)
    intensity[:, 0] = intensity[0, 0]  # At the north pole all phi samples are the same direction.
    peak_index = np.unravel_index(np.argmax(intensity), intensity.shape)
    check = radiation_intensity(result, tt[peak_index], pp[peak_index], quadrature_order=order+2).item()
    error = abs(check-intensity[peak_index])/max(check, 1e-300)
    if error > 1e-5:
        raise RuntimeError("Far-field quadrature did not converge; refine the mesh")
    gain = 4*np.pi*intensity/accepted
    maximum = float(gain[peak_index])
    if maximum <= 0:
        raise ValueError("No radiated field for Gain calculation")
    radiated = float(np.trapezoid(np.trapezoid(intensity*np.sin(np.deg2rad(tt)), np.deg2rad(theta), axis=1),
                             np.deg2rad(phi)))
    return dict(gain_peak_dbi=10*math.log10(maximum), theta_deg=float(tt[peak_index]),
                phi_deg=float(pp[peak_index]), angular_samples=int(gain.size),
                accepted_power_w=accepted, radiated_power_upper_w=radiated,
                radiation_efficiency_upper=radiated/accepted, angle_step_deg=angle_step_deg,
                quadrature_order=order, quadrature_peak_relative_error=float(error),
                wavenumber_per_mm=float(k),
                definition="gain_not_realised_gain_upper_hemisphere",
                method="SCUFF_RWG_currents_grounded_slab_TE_TM_reciprocity",
                gain_linear_points=np.column_stack((tt.ravel(), pp.ravel(), gain.ravel())).tolist())
