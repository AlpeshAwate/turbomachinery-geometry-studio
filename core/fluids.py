"""
Thermodynamic Fluid Properties Database & Viscosity Corrections.
Provides temperature-dependent density, dynamic viscosity, kinematic viscosity,
and vapor pressure for common industrial liquids.
"""

import math
from dataclasses import dataclass
from typing import Dict, Any, Tuple


FLUID_TEMPERATURE_RANGES: Dict[str, Tuple[float, float]] = {
    "Water": (0.0, 150.0),
    "Diesel Fuel": (-10.0, 100.0),
    "Gasoline": (-20.0, 60.0),
    "Crude Oil (Medium)": (0.0, 120.0),
    "Ethanol": (-10.0, 75.0),
    "Ethylene Glycol 50%": (-30.0, 120.0),
    "Liquid Methane (LNG)": (-180.0, -140.0),
}


def validate_fluid_temperature(liquid_type: str, temp_c: float) -> float:
    """Return a validated temperature instead of silently evaluating another state."""
    if liquid_type not in FLUID_TEMPERATURE_RANGES:
        raise ValueError(f"Unsupported fluid: {liquid_type}")
    low, high = FLUID_TEMPERATURE_RANGES[liquid_type]
    if not low <= temp_c <= high:
        raise ValueError(
            f"{liquid_type} property model is valid from {low:g} to {high:g} °C; "
            f"received {temp_c:g} °C."
        )
    return float(temp_c)

@dataclass
class FluidState:
    name: str
    temperature_c: float
    density: float          # kg / m^3
    dynamic_viscosity: float # Pa * s (N*s/m^2)
    kinematic_viscosity: float # m^2 / s
    vapor_pressure: float   # Pa (absolute)

def get_water_properties(temp_c: float) -> FluidState:
    """Calculates water properties between 0°C and 150°C (pressurized liquid)."""
    t = validate_fluid_temperature("Water", temp_c)
    
    # Density formula (Kell, 1975 approximation)
    rho = 999.84 * (1.0 - ((t - 3.98)**2 * (t + 283.0)) / (503570.0 * (t + 67.9)))
    
    # Dynamic viscosity (Vogel equation approximation for water)
    # mu in Pa*s
    t_k = t + 273.15
    mu = 2.414e-5 * (10.0 ** (247.8 / (t_k - 140.0)))
    
    nu = mu / rho
    
    # Vapor pressure (Antoine equation)
    # P in mmHg -> Pa: log10(P) = A - B / (C + T)
    if t < 100.0:
        # Antoine constants for water 0-100 C
        a, b, c = 8.07131, 1730.63, 233.426
    else:
        # Antoine constants for water 100-150 C
        a, b, c = 8.14019, 1810.94, 244.485
    p_mmhg = 10.0 ** (a - b / (c + t))
    p_v = p_mmhg * 133.322 # Pa
    
    return FluidState(
        name="Water",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_diesel_properties(temp_c: float) -> FluidState:
    """Standard Diesel Fuel (EN 590) properties vs temperature."""
    t = validate_fluid_temperature("Diesel Fuel", temp_c)
    # Density approx: ~835 kg/m3 at 15C, -0.7 kg/m3 per K
    rho = 845.0 - 0.70 * t
    # Viscosity approx (Andrade formula)
    t_k = t + 273.15
    # nu at 20C approx 3.5 cSt (3.5e-6 m2/s), at 40C approx 2.5 cSt
    nu = 1.0e-6 * math.exp(2100.0 / t_k - 5.9)
    mu = nu * rho
    # Vapor pressure (very low, approx 0.1 to 1 kPa)
    p_v = 100.0 * math.exp(0.04 * t)
    
    return FluidState(
        name="Diesel Fuel",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_gasoline_properties(temp_c: float) -> FluidState:
    """Automotive Gasoline properties vs temperature."""
    t = validate_fluid_temperature("Gasoline", temp_c)
    rho = 750.0 - 0.95 * t
    t_k = t + 273.15
    nu = 0.5e-6 * math.exp(1200.0 / t_k - 4.1)
    mu = nu * rho
    # Reid vapor pressure ~ 50 to 90 kPa at 37.8 C
    p_v = 30000.0 * math.exp(0.035 * t)
    
    return FluidState(
        name="Gasoline",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_crude_oil_properties(temp_c: float) -> FluidState:
    """Medium Crude Oil (32 API) properties vs temperature."""
    t = validate_fluid_temperature("Crude Oil (Medium)", temp_c)
    rho = 875.0 - 0.65 * t
    t_k = t + 273.15
    # Viscous liquid: ~25 cSt at 20C, drops sharply with temperature
    nu = 1.0e-6 * math.exp(3400.0 / t_k - 8.35)
    mu = nu * rho
    p_v = 5000.0 * math.exp(0.03 * t)
    
    return FluidState(
        name="Crude Oil (Medium)",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_ethanol_properties(temp_c: float) -> FluidState:
    """Ethanol properties vs temperature."""
    t = validate_fluid_temperature("Ethanol", temp_c)
    rho = 805.0 - 0.85 * t
    t_k = t + 273.15
    mu = 1.0e-3 * math.exp(1600.0 / t_k - 5.25)
    nu = mu / rho
    # Antoine equation for Ethanol
    a, b, c = 8.20417, 1642.89, 230.300
    p_mmhg = 10.0 ** (a - b / (c + t))
    p_v = p_mmhg * 133.322
    
    return FluidState(
        name="Ethanol",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_glycol_water_properties(temp_c: float) -> FluidState:
    """50/50 Water-Ethylene Glycol mixture (industrial coolant)."""
    t = validate_fluid_temperature("Ethylene Glycol 50%", temp_c)
    rho = 1075.0 - 0.62 * t
    t_k = t + 273.15
    # Significantly more viscous than pure water
    mu = 1.0e-3 * math.exp(2300.0 / t_k - 6.6)
    nu = mu / rho
    p_v = 0.85 * (10.0 ** (8.07131 - 1730.63 / (233.426 + max(0.0, t)))) * 133.322
    
    return FluidState(
        name="Ethylene Glycol 50%",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

def get_liquid_methane_properties(temp_c: float) -> FluidState:
    """Cryogenic Liquid Methane / LNG (around -161°C at 1 bar)."""
    t = validate_fluid_temperature("Liquid Methane (LNG)", temp_c)
    rho = 422.0 - 1.4 * (t - (-161.5))
    nu = 0.28e-6 # very low kinematic viscosity
    mu = nu * rho
    # High vapor pressure near boiling
    p_v = 101325.0 * math.exp(0.07 * (t - (-161.5)))
    
    return FluidState(
        name="Liquid Methane (LNG)",
        temperature_c=t,
        density=rho,
        dynamic_viscosity=mu,
        kinematic_viscosity=nu,
        vapor_pressure=p_v
    )

FLUID_DISPATCH = {
    "Water": get_water_properties,
    "Diesel Fuel": get_diesel_properties,
    "Gasoline": get_gasoline_properties,
    "Crude Oil (Medium)": get_crude_oil_properties,
    "Ethanol": get_ethanol_properties,
    "Ethylene Glycol 50%": get_glycol_water_properties,
    "Liquid Methane (LNG)": get_liquid_methane_properties
}

def get_fluid_properties(liquid_type: str, temp_c: float) -> FluidState:
    """Factory function to get thermodynamic state for specified liquid and temperature."""
    if liquid_type not in FLUID_DISPATCH:
        raise ValueError(f"Unsupported fluid: {liquid_type}")
    func = FLUID_DISPATCH[liquid_type]
    return func(temp_c)
