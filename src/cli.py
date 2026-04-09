"""Command Line Interface for CDA analyzer"""

import argparse
import json
import sys
import logging
from fit_parser import FITParser
from analyzer import CDAAnalyzer
from weather import WeatherService
from config import DEFAULT_PARAMETERS

def main():
    """Main CLI execution"""
    parser = argparse.ArgumentParser(description='Analyze bike ride FIT files to estimate CdA')
    parser.add_argument('fit_file', help='Path to FIT file')
    parser.add_argument('-p', '--parameters', help='Path to parameters JSON file')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('-o', '--output', help='Output JSON file for results')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    parameters = DEFAULT_PARAMETERS.copy()
    
    # Parse FIT file
    print(f"Parsing FIT file: {args.fit_file}")
    fit_parser = FITParser()
    try:
        use_elev_api = parameters.get('use_open_elevation_api', False)
        df = fit_parser.parse_fit_file(args.fit_file, use_elev_api)
        print(f"Successfully parsed {len(df)} data points")
        print(f"Elevation source: {fit_parser.elevation_source}")
    except Exception as e:
        print(f"Error parsing FIT file: {e}")
        sys.exit(1)
    
    # Analyze ride
    print("Analyzing ride data...")
    analyzer = CDAAnalyzer(parameters)
    analyzer.elevation_source = fit_parser.elevation_source
    weather_service = WeatherService()

    while True:
        # Load parameters
        parameters = _load_parameters(args.parameters)
        analyzer.update_parameters(parameters)
        
        try:
            results = analyzer.analyze_ride(df, weather_service)
        except Exception as e:
            print(f"Error during analysis: {e}")
            print("Please adjust parameters and try again.")
            continue
        
        # Display results
        _display_results(results)
        
        # Save results if requested
        if args.output:
            _save_results(results, args.output)

        input("Ctrl-C to exit. Enter to change parameters...")

def _load_parameters(param_file):
    """Load parameters from file or use defaults"""
    if param_file:
        try:
            with open(param_file, 'r') as f:
                parameters = json.load(f)
            print("Loaded parameters from file")
            return parameters
        except Exception as e:
            print(f"Error loading parameters file: {e}")
            print("Using default parameters")
    
    # Ask for parameters interactively
    print("\nUsing default parameters. Press Enter to accept defaults:")
    parameters = DEFAULT_PARAMETERS.copy()
    
    for key, default_value in DEFAULT_PARAMETERS.items():
        while True:
            try:
                user_input = input(f"{key} [{default_value}]: ").strip()
                if user_input == "":
                    break
                if isinstance(default_value, int):
                    parameters[key] = int(user_input)
                elif isinstance(default_value, float):
                    parameters[key] = float(user_input)
                else:
                    parameters[key] = user_input
                break
            except ValueError:
                print("Invalid input. Please enter a valid number.")
    
    return parameters

def _display_results(results):
    """Display analysis results"""
    print("\n" + "="*100)
    print("CDA ANALYSIS RESULTS")
    print("="*100)
    
    # Print parameters used
    print("\nParameters used:")
    for key, value in results['parameters'].items():
        print(f"  {key}: {value}")
    
    # Print segment results
    print(f"\nSegment Analysis ({len(results['segments'])} steady segments found):")
    print("-" * 160)
    print(f"{'ID':<3} {'Dur':>6} {'Dist':>8} {'v_g':>6} {'v_w':>6} {'v_a':>6} {'Angle':>6} {'Yaw':>5} {'Slope':>6} {'Power':>6} {'CdA':>7}")
    print(f"{'':3} {'(s)':>6} {'(m)':>8} {'(m/s)':>6} {'(m/s)':>6} {'(m/s)':>6} {'(deg)':>6} {'(deg)':>5} {'(deg)':>6} {'(W)':>6} {'':>7}")
    print("-" * 160)

    for segment in results['segments']:
        print(f"{segment['segment_id']:<3} "
              f"{segment['duration']:>6.0f} "
              f"{segment['distance']:>8.0f} "
              f"{segment.get('v_ground', segment['speed']):>6.2f} "
              f"{segment.get('v_wind', segment['effective_wind']):>+6.2f} "
              f"{segment.get('v_air', segment['air_speed']):>6.2f} "
              f"{segment['wind_angle']:>6.0f} "
              f"{segment.get('yaw', 0.0):>5.1f} "
              f"{segment['slope']:>6.1f} "
              f"{segment['power']:>6.0f} "
              f"{segment['cda']:>7.4f} ")
    
    # Print summary
    print("\nSummary:")
    print("-" * 60)
    summary = results['summary']
    if summary:
        print(f"Total segments analyzed: {summary['total_segments']}")
        print(f"GPS coords: {'Yes' if summary.get('has_gps_coordinates', False) else 'No'}  |  Elev source: {summary.get('elevation_source', 'Unknown')}")
        keep_percent = summary.get('keep_percent', results.get('parameters', {}).get('cda_keep_percent', 80.0))
        kept_used = summary.get('kept_segments_used', summary['total_segments'])
        print(f"Weighted CdA (all segments): {summary.get('weighted_cda_all', summary['weighted_cda']):.4f}")
        print(f"Weighted CdA (keep {keep_percent:.0f}%): {summary.get('weighted_cda_kept', summary['weighted_cda']):.4f} [{kept_used} segments]")
        print(f"Average CdA: {summary['average_cda']:.4f}")
        print(f"CdA standard deviation: {summary['cda_std']:.4f}")

        if summary.get('wind_coefficients'):
            a, b, c = summary['wind_coefficients']
            print(f"Wind Angle Formula: CdA = {a:.2e}*θ² + {b:.2e}*θ + {c:.2e} (θ in degrees)")
        else:
            print("Wind Angle Formula: Insufficient data")

        print(f"Average wind speed (meteo): {summary['avg_wind_speed']:.1f} m/s")
        print(f"Average ground speed (v_g): {summary.get('avg_ground_speed', 0):.2f} m/s")
        print(f"Average wind component (v_w): {summary.get('avg_wind_component', 0):+.2f} m/s  (+headwind / -tailwind)")
        print(f"Average air speed (v_a): {summary['avg_air_speed']:.2f} m/s")
        print(f"Total analysis duration: {summary['total_duration']:.0f} seconds")
        print(f"Total distance analyzed: {summary['total_distance']:.0f} meters")
    else:
        print("No steady segments found for analysis")

def _save_results(results, output_file):
    """Save results to JSON file"""
    try:
        # Convert datetime objects to strings for JSON serialization
        output_results = results.copy()
        for segment in output_results['segments']:
            if 'start_time' in segment:
                segment['start_time'] = segment['start_time'].isoformat()
            if 'end_time' in segment:
                segment['end_time'] = segment['end_time'].isoformat()
        
        with open(output_file, 'w') as f:
            json.dump(output_results, f, indent=2)
        print(f"\nResults saved to {output_file}")
    except Exception as e:
        print(f"Error saving results: {e}")