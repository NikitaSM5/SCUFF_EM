"""Analytical limits and native-current checks for the layered far-field postprocessor."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from antenna import Substrate, create_planar_antenna, solve_antenna, calculate_gain
from antenna.farfield import current_quadrature, radiation_intensity, slab_factors


class SlabTests(unittest.TestCase):
    def test_air_over_pec_matches_horizontal_image_current(self):
        theta = np.deg2rad(np.arange(0, 90, 5))
        k, h = .067, 5
        te, tm = slab_factors(theta, k, h, 1+0j)
        image = 1-np.exp(2j*k*h*np.cos(theta))
        np.testing.assert_allclose(te, image, atol=1e-14)
        np.testing.assert_allclose(tm, image*np.cos(theta), atol=1e-14)

    def test_pec_at_current_plane_cancels_radiation(self):
        te, tm = slab_factors(np.deg2rad(np.arange(91)), .1, 0, 4.4+0j)
        np.testing.assert_allclose(te, 0, atol=1e-14)
        np.testing.assert_allclose(tm, 0, atol=1e-14)

    def test_quarter_wave_backed_slab_at_normal_incidence(self):
        k, epsilon = .067, 4.4
        h = np.pi/(2*k*np.sqrt(epsilon))
        te, tm = slab_factors(np.array([0.]), k, h, epsilon)
        np.testing.assert_allclose(te, 2, atol=1e-14)
        np.testing.assert_allclose(tm, 2, atol=1e-14)

    def test_thick_lossy_slab_has_halfspace_fresnel_limit(self):
        theta = np.deg2rad([0, 20, 60, 89])
        epsilon = 4.4+.8j
        c, gamma = np.cos(theta), np.sqrt(epsilon-np.sin(theta)**2)
        te, tm = slab_factors(theta, .1, 100000, epsilon)
        np.testing.assert_allclose(te, 2*c/(c+gamma), atol=1e-14)
        np.testing.assert_allclose(tm, 2*c*gamma/(gamma+epsilon*c), atol=1e-14)

    def test_critical_angle_and_grazing_remain_finite(self):
        with np.errstate(all="raise"):
            te, tm = slab_factors(np.deg2rad([0, 45, 90]), .067, 5, .5+0j)
        self.assertTrue(np.isfinite(te).all() and np.isfinite(tm).all())
        # A TE slab cutoff has a nonzero grazing limit; it must not be forced to zero.
        k, epsilon = .067, 4.4
        te, _ = slab_factors(np.array([np.pi/2]), k, np.pi/(2*k*np.sqrt(epsilon-1)), epsilon)
        self.assertAlmostEqual(abs(te[0]), 2, places=5)

    def test_quadrature_integrates_one_rwg_exactly(self):
        vertices = np.array([[0, -1, 0], [0, 1, 0], [-2, 0, 0], [2, 0, 0]], float)
        triangles = np.array([[2, 0, 1], [3, 1, 0]])
        rwg = np.array([[0, 1, 2, 3, 0, 1]])
        current = np.array([1+2j])
        _, weighted = current_quadrature(vertices, triangles, rwg, current, 6)
        expected = current[0]*2/3*(vertices[3, :2]-vertices[2, :2])
        np.testing.assert_allclose(weighted.sum(axis=0), expected, atol=1e-14)


@unittest.skipUnless(os.environ.get("SCUFF_RUN_NATIVE_TESTS") == "1", "Opt-in native SCUFF currents")
class NativeGainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scuff gain ")
        cls.root = Path(cls.temp.name)
        geometry = create_planar_antenna([[1]], 5, (0, 0), cls.root / "geometry", mesh_size_mm=1.25)
        cls.result = solve_antenna(geometry, 3.2, work_dir=cls.root / "run")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def copied(self):
        r = self.result
        return SimpleNamespace(vertices_mm=r.vertices_mm.copy(), triangles=r.triangles, rwg=r.rwg,
                               x=r.x.copy(), metadata=deepcopy(r.metadata))

    def test_gain_energy_and_grid(self):
        gain = calculate_gain(self.result)
        self.assertEqual(gain["angular_samples"], 19*73)
        self.assertGreater(gain["accepted_power_w"], 0)
        self.assertGreater(gain["radiation_efficiency_upper"], .1)
        self.assertLess(gain["radiation_efficiency_upper"], 1.02)
        self.assertGreater(gain["gain_peak_dbi"], 3)
        self.assertLess(gain["gain_peak_dbi"], 6)
        self.assertLess(gain["quadrature_peak_relative_error"], 1e-8)

    def test_quadrature_convergence_away_from_peak(self):
        theta, phi = [0, 20, 45, 80, 89.9], [0, 55, 170, 300, 355]
        a = radiation_intensity(self.result, theta, phi, quadrature_order=6)
        b = radiation_intensity(self.result, theta, phi, quadrature_order=10)
        np.testing.assert_allclose(a, b, rtol=1e-10, atol=1e-20)

    def test_translation_and_excitation_scaling_do_not_change_gain(self):
        baseline = calculate_gain(self.result, 15)
        changed = self.copied()
        changed.vertices_mm[:, :2] += [10, -17]
        changed.x *= 2j
        changed.metadata["port_voltage_V"] = [0, 2]
        other = calculate_gain(changed, 15)
        self.assertAlmostEqual(baseline["gain_peak_dbi"], other["gain_peak_dbi"], places=9)
        self.assertAlmostEqual(other["accepted_power_w"]/baseline["accepted_power_w"], 4)

    def test_air_ground_energy_balance(self):
        geometry = create_planar_antenna([[1]], 5, (0, 0), self.root / "air-geometry",
                                         substrate=Substrate(5, 1, 0), mesh_size_mm=1.25)
        result = solve_antenna(geometry, 3.2, work_dir=self.root / "air-run")
        gain = calculate_gain(result, 2)
        # No guided slab waves or dielectric absorption when epsilon=1.
        self.assertAlmostEqual(gain["radiation_efficiency_upper"], 1, delta=.01)

    def test_nonpassive_and_legacy_ports_are_rejected(self):
        changed = self.copied()
        changed.metadata["input_impedance_ohm"] = [-1, 10]
        with self.assertRaisesRegex(ValueError, "accepted"):
            calculate_gain(changed)
        changed.metadata["input_impedance_ohm"] = [0, 0]
        with self.assertRaisesRegex(ValueError, "impedance"):
            calculate_gain(changed)
        changed = self.copied()
        changed.metadata["geometry"].pop("feed")
        with self.assertRaisesRegex(ValueError, "voltage-gap"):
            calculate_gain(changed)
