import re
import os
import argparse


def parse_time_to_seconds(time_str):
    """Convert HH:MM:SS to total seconds."""
    h, m, s = map(int, time_str.split(":"))
    return h * 3600 + m * 60 + s


def format_seconds_to_minsec_dash(total_seconds):
    """Format seconds to -XmYs (or -Xm if no seconds)."""
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    if seconds == 0:
        return f"-{minutes}m"
    return f"-{minutes}m{seconds}s"


def convert_distance_based(line, watt_delta):
    """Distance-based export (default)."""
    parts = re.split(r"\t+", line.strip())
    if len(parts) < 8:
        return None

    distance = parts[3].strip()  # e.g. "2.87 km"
    power_match = re.search(r"(\d+)", parts[7])
    if not power_match:
        return None

    power = int(power_match.group(1))
    low = power - watt_delta
    high = power + watt_delta

    return f"- {distance} {low}-{high}W"


def convert_time_based(line, watt_delta, time_percent):
    """Time-based export with optional % time increase."""
    parts = re.split(r"\t+", line.strip())
    if len(parts) < 8:
        return None

    interval_time = parts[1].strip()  # e.g. "00:04:33"
    total_seconds = parse_time_to_seconds(interval_time)

    if time_percent:
        total_seconds = int(round(total_seconds * (1 + time_percent / 100.0)))

    formatted_time = format_seconds_to_minsec_dash(total_seconds)

    power_match = re.search(r"(\d+)", parts[7])
    if not power_match:
        return None

    power = int(power_match.group(1))
    low = power - watt_delta
    high = power + watt_delta

    return f"{formatted_time} {low}-{high}W"


def main():
    parser = argparse.ArgumentParser(
        description="Convert race plan export to distance or time based training format.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:"
            "  python script.py intervals.txt"
            "  python script.py intervals.txt --watt-delta 15"
            "  python script.py intervals.txt --mode time"
            "  python script.py intervals.txt --mode time --time-percent 5"
            "  python script.py intervals.txt --mode time --time-percent 5 --watt-delta 15"
        ),
    )

    parser.add_argument("input_file", help="Input TXT file")
    parser.add_argument(
        "--mode",
        choices=["distance", "time"],
        default="distance",
        help="Export mode: distance (default) or time",
    )
    parser.add_argument(
        "--time-percent",
        type=float,
        default=0.0,
        help="Increase interval time by percentage (time mode only)",
    )
    parser.add_argument(
        "--watt-delta",
        type=int,
        default=10,
        help="Plus/minus watt adjustment (default 10)",
    )

    args = parser.parse_args()
    base_name = os.path.splitext(args.input_file)[0]

    # Build dynamic output filename based on parameters
    parts = []

    if args.mode == "distance":
        parts.append("distance")
    else:
        parts.append("time")
        if args.time_percent:
            parts.append(f"{int(args.time_percent)}pct")

    parts.append(f"{args.watt_delta}w")

    suffix = "_".join(parts)
    output_file = f"{base_name}_{suffix}.txt"

    with open(args.input_file, "r") as f_in, open(output_file, "w") as f_out:
        for line in f_in:
            if args.mode == "distance":
                converted = convert_distance_based(line, args.watt_delta)
            else:
                converted = convert_time_based(
                    line, args.watt_delta, args.time_percent
                )

            if converted:
                f_out.write(converted + "\n")

    print(f"Export complete: {output_file}")


if __name__ == "__main__":
    main()
