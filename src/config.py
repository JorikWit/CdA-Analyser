"""Configuration module for CDA analyzer"""

# Default parameters
DEFAULT_PARAMETERS = {
    'min_segment_length': 50,  # meters
    'min_duration': 15,   # seconds
    'max_slope_variation': 1.00,  # degrees
    'min_speed': 1.0,     # m/s
    'max_speed': 20.0,    # m/s
    'speed_steady_threshold': .50,    # m/s
    'power_steady_threshold': 150.0,   # watts
    'slope_steady_threshold': 5.0,    # degrees

    'cda_keep_percent': 75.0,       # iteratively remove largest abs CdA outliers until x% remains
    'subsegment_min_duration_s': 5.0,   # seconds per sub-segment
    'subsegment_min_points':     5,     # minimum data-points per sub-segment
    'rider_mass': 75.0,  # kg
    'bike_mass': 10.0,   # kg
    'rolling_resistance': 0.003,
    'drivetrain_loss': 0.0275,
    'wind_effect_factor' : 0.25, # (0.0 - 1.0)  look at cli angle +- 0 and angle 180 must be 0 -> +-0.20 cda diff +  CdA standard deviation < 0.05
    'use_weather_api': True,  # Use weather API data in calculations when data is available
    'use_open_elevation_api': False,  # Use Open-Elevation API for altitude data (batch request, all points in 1 call)
    'weather_sample_distance_m': 3000.0,  # Distance-based weather samples loaded at FIT import

    # Sub-segment splitting: each steady segment is divided into chunks so that
    # local GPS bearing, slope and acceleration are computed per chunk rather
    # than over the full segment length.  Weather data is still fetched once.
}

# Weather API settings
OPEN_METEO_URL_FORCAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_URL_ARCIVE  = "https://archive-api.open-meteo.com/v1/archive"

# Elevation API settings
OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"

"""
Eeklo
    Jorik 
        rider_mass: 75.0
        bike_mass: 10.0
        rolling_resistance: 0.005
        min_segment_length: 250
        max_slope_variation: 0.5
        min_speed: 9.0
        max_speed: 20.0
        min_duration: 30
        wind_effect_factor: 0.05
        Weighted CdA: 0.2945
        Average CdA: 0.2914
        CdA standard deviation: 0.0387

    Sam
        'rider_mass': 68.0,  # kg
        'bike_mass': 9.0,   # kg
        'rolling_resistance': 0.0035,
        'min_segment_length': 250,  # meters
        'max_slope_variation': 0.50,  # degrees
        'min_speed': 8.3,     # m/s
        'max_speed': 20.0,    # m/s
        'min_duration': 30,   # seconds
        'wind_effect_factor' : 0.07
        Weighted CdA: 0.2515
        Average CdA: 0.2518
        CdA standard deviation: 0.0268

    Xiano
        'rider_mass': 68.0,  # kg
        'bike_mass': 9.0,   # kg
        'rolling_resistance': 0.0035,
        'min_segment_length': 250,  # meters
        'max_slope_variation': 0.50,  # degrees
        'min_speed': 8.3,     # m/s
        'max_speed': 20.0,    # m/s
        'min_duration': 30,   # seconds
        'wind_effect_factor' : 0.1, # (0.0 - 1.0)  look at cli angle +- 0 and angle 180 must be 0 -> +-0.20 cda diff +  CdA standard deviation < 0.12
        Weighted CdA: 0.2241
        Average CdA: 0.2228
        CdA standard deviation: 0.0221

Kappelle
    Lars
        'rider_mass': 77.0,  # kg
        'bike_mass': 10.0,   # kg
        'rolling_resistance': 0.0035,
        'min_segment_length': 250,  # meters
        'max_slope_variation': 0.50,  # degrees
        'min_speed': 8.3,     # m/s
        'max_speed': 20.0,    # m/s
        'min_duration': 30,   # seconds
        'wind_effect_factor' : 0.07, # (0.0 - 1.0)  look at cli angle +- 0 and angle 180 must be 0 -> +-0.20 cda diff +  CdA standard deviation < 0.12
        Weighted CdA: 0.3115
        Average CdA: 0.3060 
        CdA standard deviation: 0.0616

    Jorik
        'rider_mass': 75.0,  # kg
        'bike_mass': 10.0,   # kg
        'rolling_resistance': 0.005,
        'min_segment_length': 250,  # meters
        'max_slope_variation': 0.50,  # degrees
        'min_speed': 8.3,     # m/s
        'max_speed': 20.0,    # m/s
        'min_duration': 30,   # seconds
        'wind_effect_factor' : 0.08, # (0.0 - 1.0)  look at cli angle +- 0 and angle 180 must be 0 -> +-0.20 cda diff +  CdA standard deviation < 0.12
        Weighted CdA: 0.2967
        Average CdA: 0.2926
        CdA standard deviation: 0.0421

Test nieuwe banden :
    'rider_mass': 75.0,  # kg
    'bike_mass': 10.0,   # kg
    'rolling_resistance': 0.0035,
    'min_segment_length': 250,  # meters
    'max_slope_variation': 0.50,  # degrees
    'min_speed': 9.0,     # m/s
    'max_speed': 20.0,    # m/s
    'min_duration': 30,   # seconds
    'wind_effect_factor' : 0.1, # (0.0 - 1.0)  look at cli angle +- 0 and angle 180 must be 0 -> +-0.20 cda diff +  CdA standard deviation < 0.05
    Weighted CdA: 0.2939
    Average CdA: 0.2945
    CdA standard deviation: 0.0255

Damme 
    Lars
        rider_mass: 77.0
        bike_mass: 10.0
        rolling_resistance: 0.0038
        min_segment_length: 250
        max_slope_variation: 0.5
        min_speed: 8.3
        max_speed: 20.0
        min_duration: 30
        wind_effect_factor: 0.05
        Weighted CdA: 0.3369
        Average CdA: 0.3347
        CdA standard deviation: 0.0420

    Jorik
        rider_mass: 75.0
        bike_mass: 10.0
        gravity: 9.81
        min_segment_length: 250
        max_slope_variation: 0.5
        min_speed: 8.3
        max_speed: 20.0
        min_duration: 30
        wind_effect_factor: 0.05
        Weighted CdA: 0.2949
        Average CdA: 0.2991
        CdA standard deviation: 0.0569

Lievegem
    Lars
        rider_mass: 77.0
        bike_mass: 10.0
        gravity: 9.81
        min_segment_length: 250
        max_slope_variation: 0.5
        min_speed: 8.3
        max_speed: 20.0
        min_duration: 30
        wind_effect_factor: 0.05
        Weighted CdA: 0.3169
        Average CdA: 0.3165
        CdA standard deviation: 0.0518
        
    Jorik
        rider_mass: 75.0
        bike_mass: 10.0
        rolling_resistance: 0.005
        min_segment_length: 250
        max_slope_variation: 0.5
        min_speed: 8.3
        max_speed: 20.0
        min_duration: 30
        wind_effect_factor: 0.07
        Weighted CdA: 0.2860
        Average CdA: 0.2886
        CdA standard deviation: 0.0433

kappelle2023
    rider_mass: 75.0
    bike_mass: 10.0
    rolling_resistance: 0.005
    min_segment_length: 250
    max_slope_variation: 0.5
    min_speed: 8.3
    max_speed: 20.0
    min_duration: 30
    wind_effect_factor: 0.03
    Weighted CdA: 0.3573
    Average CdA: 0.3579
    CdA standard deviation: 0.0414

250W (geen drivechain loss?)
    old bike +- 35.6km/u                       cda 0.36 alle met berekening 0.36 maar deze kan iets lager 0.3515 (damme2024)
    new bike +- 38.9km/u
    tt (0.24 cda, 0.0035 crr) +- 41.2km/u

    verschil wattage @ 40km/u 32W    

Banden 
    verschil cda        @35km/u
        0.288           197.9
        0.275           190.6
    
    cda = 0.29
    crr 0.005           199.0 W
    crr 0.0035          188.3

10W verschill
    260W cda 0.288 crr 0.0035       39.4km/u
    250W cda 0.288 crr 0.0035       38.9km/u

    260W cda 0.288 crr 0.005        38.8km/u
    250W cda 0.288 crr 0.005        38.2km/u

Lars (250W) 
    bike cda 0.31       38.0km/u
    tt   cda 0.30       38.4km/u

    bike cda 0.316       37.7km/u
    tt   cda 0.302       38.3km/u

    beter pos? 0.275    39.5km/u

    Power diff @ 35km/u
    0.316               202.9W
    0.302               195.0W
    (0.275               179.8W)    

    7.9W
    (22.1W)


"""


