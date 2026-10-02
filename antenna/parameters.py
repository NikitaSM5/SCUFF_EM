"""One-port parameters expressed in the exp(+j*w*t) convention used by FEKO."""
import cmath
import math


def from_impedance(impedance, frequency_hz, reference_ohm):
    z = complex(impedance)
    if not all(math.isfinite(v) for v in (z.real, z.imag, reference_ohm)) or reference_ohm <= 0:
        raise ValueError("Invalid impedance or reference impedance")
    if abs(z + reference_ohm) == 0:
        raise ValueError("Singular impedance to S11 conversion")
    return from_reflection((z-reference_ohm)/(z+reference_ohm), frequency_hz, reference_ohm)


def from_reflection(value, frequency_hz, reference_ohm):
    value = complex(value)
    if not all(math.isfinite(v) for v in (value.real, value.imag, frequency_hz, reference_ohm)) or frequency_hz <= 0 or reference_ohm <= 0:
        raise ValueError("Invalid reflection sample")
    magnitude = abs(value)
    z = reference_ohm*(1+value)/(1-value) if abs(1-value) > 1e-14 else None
    return dict(frequency_hz=frequency_hz, reference_ohm=reference_ohm,
                s11_real=value.real, s11_imag=value.imag, s11_magnitude=magnitude,
                s11_db=20*math.log10(magnitude) if magnitude else None,
                s11_phase_deg=math.degrees(cmath.phase(value)),
                resistance_ohm=z.real if z is not None else None,
                reactance_ohm=z.imag if z is not None else None,
                vswr=(1+magnitude)/(1-magnitude) if magnitude < 1 else None,
                reflected_power_percent=100*magnitude*magnitude,
                passive=magnitude <= 1+1e-8)
