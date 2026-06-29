#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

# LLM assisted! May contain BS

# Defaults
FREQUENCY          = 0.000040        # Sets period to 40,000 ns (25 kHz)
DUTY_CYCLE_DEFAULT = 0.4             # Initiate with 40% speed by default
BOOT_CONFIG_FILES = [
    # --- Raspberry Pi Platforms (Uses 'dtoverlay=') ---
    "/boot/config.txt",  # Standard/Legacy Raspberry Pi & DietPi (on Pi)
    "/boot/firmware/config.txt",  # Modern Raspberry Pi OS (Bookworm+) & Ubuntu
    # --- Armbian, Orange Pi & DietPi Platforms (Uses 'overlays=') ---
    "/boot/armbianEnv.txt",  # Armbian (Massively popular on Allwinner/Rockchip SBCs)
    "/boot/orangepiEnv.txt",  # Official Orange Pi OS (e.g., Orange Pi 5 series)
    "/boot/dietpiEnv.txt",  # DietPi env file for non-Raspberry Pi boards
    # --- Generic U-Boot Platforms (Uses variable syntax variants) ---
    "/boot/uEnv.txt",  # Mainline U-Boot env (e.g., BeagleBone, custom boards)
    "/boot/bootEnv.txt",  # Alternative U-Boot variable storage name
]

import sys
import os
import time
import pathlib
import argparse
import random
import string
import signal
import atexit
try:
    from periphery import PWM
    PERIPHERY_LOADED = True
except ImportError:
    PERIPHERY_LOADED = False

# Initialize a global dictionary to persist fan states between executions (hysteresis)
fan_states = {}
# Track operational data to display on USR1
adjust_result = {}


def adjust_fan_speed(fan_object, default_duty, args):
    """
    Evaluates CPU and NVMe temperatures against preset scaling dictionaries
    and sets the fan.duty_cycle to the highest required speed with hysteresis.
    State is tracked using a global dictionary indexed by the fan object ID.
    """
    hysteresis_delta = 2.0 
    
    # Generate a unique key for this fan object instance
    fan_key = id(fan_object)
    
    # Retrieve the last known duty cycle, or fallback to default
    last_duty = fan_states.get(fan_key, default_duty)

    # Define preset dictionary configurations
    # The script looks for the HIGHEST temperature that is GREATER than the key.
    cpu_presets = {
        59.0: 1.00,  # 100% Speed - CPU Ceiling Trigger
        56.0: 0.85,  #  85%
        53.0: 0.75,  #  75%
        50.0: 0.65,  #  65%
        47.0: 0.55,  #  55%
        44.0: 0.50,  #  50%
        41.0: 0.45,  #  45%
        38.0: 0.40,  #  40%
        1.0:  0.30,  #  30% - Umbrella value
    }
 
    nvme_presets = {
        59.0: 1.00,  # 100% Speed - Storage Ceiling Tracker
        56.0: 0.85,  #  85%
        53.0: 0.75,  #  75%
        50.0: 0.65,  #  65%
        47.0: 0.55,  #  55%
        44.0: 0.50,  #  50%
        41.0: 0.45,  #  45%
        38.0: 0.40,  #  40%
        1.0:  0.30,  #  30% - Umbrella value
    }

    # Helper function to evaluate curves with hysteresis
    def get_target_duty(current_temp, presets, current_fan_duty):
        for threshold in presets.keys():
            # Apply the temperature drop cushion if the fan is already running 
            # at or above this preset's designated speed target.
            offset = hysteresis_delta if current_fan_duty >= presets[threshold] else 0.0
            
            if current_temp >= (threshold - offset):
                return presets[threshold]
        return default_duty

    # Core logic:
    # - Always collect CPU unless ignored. If not found and not ignored - set to full speed
    # - Try to collect NVME. If found but no temperature - set to full speed
    # - Otherwise - set speed (duty) according to highest temperature
    is_malfunction = False

    cpu_target_duty = None
    cpu_temp = None
    if args.collect_cpu:
        temperature_file = find_cpu_thermal_zone_file()
        if temperature_file:
            with open(temperature_file, "r") as f:
                cpu_temp = int(f.read().strip()) / 1000.0

            cpu_target_duty = get_target_duty(cpu_temp, cpu_presets, last_duty)
        else:
            is_malfunction = True

    nvme_target_duty = None
    max_nvme_temp = None
    if is_nvme_drive_present():
        nvme_temps = read_nvme_temperatures()
        max_nvme_temp = max(nvme_temps, default=None)
        if max_nvme_temp:
            nvme_target_duty = get_target_duty(max_nvme_temp, nvme_presets, last_duty)
        else:
            is_malfunction = True

    # Determine highest required speed
    if is_malfunction:
        final_duty = 1.0
        cpu_target_duty  = -1.0  # display only
        nvme_target_duty = -1.0  # display only
    elif cpu_target_duty and nvme_target_duty:
        final_duty = max(cpu_target_duty, nvme_target_duty)
    elif cpu_target_duty:
        final_duty = cpu_target_duty
        nvme_target_duty = -1.0  # display only
    elif nvme_target_duty:
        final_duty = nvme_target_duty
        cpu_target_duty  = -1.0  # display only
    else:
        final_duty = default_duty

    # Save state to the global tracking dictionary and apply to hardware
    fan_states[fan_key] = final_duty
    fan_object.duty_cycle = final_duty

    result = {'cpu_temp':   cpu_temp,      'cpu_target_duty':  f'{cpu_target_duty:.2f}',
              'nvme_temp':  max_nvme_temp, 'nvme_target_duty': f'{nvme_target_duty:.2f}',
              'final_duty': f'{final_duty:.2f}',
    }

    # Optional console logging for debugging/monitoring
    return result


def read_nvme_temperatures():
    """
    Scans the Linux sysfs hwmon interface to read all NVMe SSD temperatures
    using only native file operations.
    """
    hwmon_base_path = "/sys/class/hwmon"
    temperatures = []
#    return []  # test for malfunction

    if not os.path.exists(hwmon_base_path):
        return temperatures

    for hwmon_dir in os.listdir(hwmon_base_path):
        dir_path = os.path.join(hwmon_base_path, hwmon_dir)
        name_file = os.path.join(dir_path, "name")
        
        if not os.path.exists(name_file):
            continue

        try:
            with open(name_file, "r") as f:
                if f.read().strip() == "nvme":
                    # Directly collect all temp*_input files in this nvme folder
                    for file_name in os.listdir(dir_path):
                        if file_name.startswith("temp") and file_name.endswith("_input"):
                            input_file = os.path.join(dir_path, file_name)
                            try:
                                with open(input_file, "r") as f_in:
                                    # Convert millidegrees to standard Celsius float
                                    celsius = int(f_in.read().strip()) / 1000.0
                                    temperatures.append(celsius)
                            except (IOError, ValueError):
                                continue
        except IOError:
            continue

    return temperatures


def find_cpu_thermal_zone_file():
    """
    Scans the system device tree to find the exact file path tracking 
    the core CPU or SoC thermal zone.
    
    Returns:
        str: The absolute file path to the active 'temp' file if found.
        None: If no valid matching thermal zone exists on the system.
    """
    base_path = "/sys/class/thermal"
#    return []  # test for malfunction
    
    # Safety Check: If the base kernel thermal directory is missing, exit early
    if not os.path.exists(base_path):
        return None

    try:
        zones = os.listdir(base_path)
    except IOError:
        return None

    result = None
    for zone in zones:
        if zone.startswith("thermal_zone"):
            type_file = os.path.join(base_path, zone, "type")
            temperature_file = os.path.join(base_path, zone, "temp")
            
            # Verify both 'type' and 'temp' files physically exist before reading
            if os.path.exists(type_file) and os.path.exists(temperature_file):
                try:
                    with open(type_file, "r") as f:
                        content = f.read().lower()
                        # Match standard Linux architecture naming formats
                        if "cpu" in content or "soc" in content or "core" in content or "center" in content:
                            result = temperature_file
                            break
                except IOError:
                    continue
                    
    return result


def is_nvme_drive_present() -> bool:
    """
    Queries the live Linux hardware subsystem class registers to determine if 
    any physical NVMe storage controller is attached to the board.
    
    Returns:
        bool: True if at least one NVMe device is registered, False otherwise.
    """
    nvme_subsystem_path = "/sys/class/nvme/"
    
    # Verify if the kernel's core NVMe module tree folder even exists
    if not os.path.exists(nvme_subsystem_path):
        return False
        
    try:
        # Check if the directory contains any active structural handles
        # Live controller directories show up naming strings like 'nvme0'
        devices = os.listdir(nvme_subsystem_path)
        
        # Filter for entries starting with 'nvme' to ignore hidden system files
        nvme_controllers = [d for d in devices if d.startswith("nvme")]
        
        if nvme_controllers:
            # Match found (e.g., ['nvme0'])
            return True
            
    except IOError:
        # Catch safe environment bounds errors if permissions drop
        pass
        
    return False


def get_pwmchip_address_map():
    """
    Scans /sys/class/pwm/, extracts the underlying hardware memory addresses
    from system symlinks, and maps them to their respective pwmchip integers.
    """
    pwm_path = "/sys/class/pwm/"
    address_map = {}
    
    if not os.path.exists(pwm_path):
        print(f"[ERROR] Path {pwm_path} not found. Ensure PWM overlays are enabled.", file=sys.stderr)
        return {}
        
    for item in os.listdir(pwm_path):
        full_path = os.path.join(pwm_path, item)
        
        # We must follow the symbolic link to read the hardware address
        if os.path.islink(full_path):
            try:
                # Isolate the integer from 'pwmchipX'
                chip_integer = int(item.replace("pwmchip", ""))
                
                # Read the symlink destination target
                # Example target: '../../devices/platform/fd8b0010.pwm/pwm/pwmchip2'
                real_target_path = os.readlink(full_path)
                
                # Split the path segments to isolate the hardware block name
                path_segments = real_target_path.split('/')
                
                # Look for the segment containing '.pwm'
                hardware_address = next((seg for seg in path_segments if ".pwm" in seg), None)
                
                if hardware_address:
                    address_map[hardware_address] = chip_integer
                    
            except (ValueError, StopIteration):
                # Avoid crashing on unexpected kernel naming formats
                continue
                
    return address_map


def is_pwm_overlay_enabled() -> bool:
    """Checks system environment configs to determine if any 'pwm*' device tree
    overlay is explicitly enabled for the GPIO expansion headers.

    Returns:
        bool: True if an active PWM overlay is found, False otherwise.
    """
    for file_path in BOOT_CONFIG_FILES:
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, "r") as f:
                for line in f:
                    clean_line = line.strip()

                    # Ignore commented lines (#) or empty spaces
                    if clean_line.startswith("#") or not clean_line:
                        continue

                    # Determine which key assignment format is used on this line
                    target_key = None
                    if "dtoverlay=" in clean_line:
                        target_key = "dtoverlay="
                    elif "overlays=" in clean_line:
                        target_key = "overlays="

                    if target_key:
                        # Extract everything to the right of the matching equals sign
                        _, overlays_value = clean_line.split(target_key, 1)

                        # Look for 'pwm' in the listed string (e.g., 'pwm15-m2' or 'pwm0')
                        if overlays_value.lower().startswith('pwm'):
                            print(f"[Overlay Check] Active PWM assignment found in: {file_path}")
                            print(f"Config line: '{clean_line}'")
                            return True

        except (IOError, ValueError):
            # Catch for formatting failures or read permission blips
            continue

    # Fallback: If config files don't flag it, verify if sysfs exported a chip channel anyway
    try:
        pwm_base = "/sys/class/pwm/"
        if os.path.exists(pwm_base) and any(
            x.startswith("pwmchip") for x in os.listdir(pwm_base)
        ):
            chips = [ x for x in os.listdir(pwm_base) if x.startswith("pwmchip")]
            if len(chips) > 1:
                return True
    except IOError:
        pass

    print("[WARNING] No active 'pwm*' line detected in boot configs.", file=sys.stderr)

    return False


def check_for_conflicting_overlays() -> list:
    """
    Scans the system boot files for active overlays that conflict with PWM pins
    (like SPI or I2S audio channels). Supports lines starting with 'overlays=' or 'dtoverlay='.
    
    Returns:
        list: A list of found conflicting overlay names. Empty list if system is clear.
    """
    # List the keyword markers of hardware buses that hijack PWM pins
    conflict_keywords = ["spi", "i2s", "audio-codec"]
    found_conflicts = []

    for file_path in BOOT_CONFIG_FILES:
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, "r") as f:
                for line in f:
                    clean_line = line.strip()

                    # Ignore comment blocks and blank lines
                    if clean_line.startswith("#") or not clean_line:
                        continue

                    # Check for either prefix keyword
                    prefix = None
                    if clean_line.startswith("dtoverlay="):
                        prefix = "dtoverlay="
                    elif clean_line.startswith("overlays="):
                        prefix = "overlays="

                    if prefix:
                        _, overlays_value = clean_line.split(prefix, 1)
                        # Split by spaces or commas depending on formatting styles
                        active_overlays = overlays_value.replace(",", " ").split()

                        for overlay in active_overlays:
                            # Check if the active overlay contains any blacklisted keyword
                            if any(kw in overlay.lower() for kw in conflict_keywords):
                                found_conflicts.append(overlay)

        except (IOError, ValueError):
            continue

    return list(set(found_conflicts))  # Return unique conflicts found


def pre_flight_hardware_check() -> bool:
    """
    Validates environment overlays before trying to access hardware pins.
    """
    
    # 1. Verify PWM is actually present
    if not is_pwm_overlay_enabled():
        print("""[CRITICAL] Cannot start. No active 'pwm*' overlay is enabled.""", file=sys.stderr)
        return False
        
    # 2. Check for blockers
    conflicts = check_for_conflicting_overlays()
    if conflicts:
        print(f"""[WARNING] Detected conflicting overlays active: {conflicts}
These hardware profiles might hijack the pins required by the fan.
If fan fails to change speed, try disabling these buses."""
, file=sys.stderr)
    else:
        print("No conflicting SPI or I2S overlays detected on the pins.")

    # 3. Check sensor availaibility. Speed handled in adjust_fan_speed()
    if not find_cpu_thermal_zone_file():
        print('[WARNING] Unable to collect CPU temperature. Assuming high', file=sys.stderr)

    if is_nvme_drive_present():
        if not read_nvme_temperatures():
            print('[WARNING] NVME is present, but temperature could not be found. Assuming high', file=sys.stderr)
        
    return True


def handle_usr1(signum, frame):
    print(adjust_result)


def handle_shutdown(signum, frame):
    print(f"Received signal {signum}")
    sys.exit(0)


def cleanup(fan_):
    print("\nStopping script. Resetting fan to full speed...")
    fan_.duty_cycle = 1.0
    fan_.close()


def run_controller(pwm_chip, args):
    # Initialize the PWM controller (Maps to /sys/class/pwm/pwmchipX/pwm0)
    try:
        fan = PWM(chip=pwm_chip, channel=0)
        atexit.register(cleanup, fan)
    except Exception as e:
        print(f"""[ERROR] Cannot accessi PWM chip: {e}
Ensure the overlay is enabled and you are running as root/sudo."""
, file=sys.stderr)
        sys.exit(110)
    
    # High-level configuration setups
    fan.period = FREQUENCY
    fan.polarity = "normal"   # Handled cleanly without order conflicts
 
    # Set initial duty cycle
    fan.duty_cycle = DUTY_CYCLE_DEFAULT
    fan.enable()              # Starts the PWM generation
    print(f"Fan initialized at {fan.duty_cycle} speed.")

    # Loop to adjust the fan
    adjust_result_previous = None
    while True:
        global adjust_result
        adjust_result = adjust_fan_speed(fan, DUTY_CYCLE_DEFAULT, args)
        adjust_result['unixtime'] = int(time.time())  # populate every time

        if args.debug or args.log:
            if args.debug:
                adjust_current = adjust_result
            else:
                # Remove volatile keys to further compare; to display only diff, not every call
                exclude = {'cpu_temp', 'nvme_temp', 'unixtime'}
                adjust_current = {k: v for k, v in adjust_result.items() if k not in exclude}

            # Compare with previous result and rewrite it, to show only changing data in log
            if adjust_current != adjust_result_previous:
                adjust_result_previous = adjust_current
                print(adjust_current)
            elif args.debug:
                print(adjust_current)

        time.sleep(args.interval)
 

def handle_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', "--device", type=str,
                        help="PWM device name")
    parser.add_argument('-l', "--list", action='store_true',
                        default=False,
                        help="List all initialized devices")
    parser.add_argument('-s', "--interval", type=int,
                        default=10,
                        help="Polling sleep interval in seconds")
    parser.add_argument('-C', "--not-collect-cpu", action='store_false', dest='collect_cpu',
                        default=True,
                        help="Do NOT collect the CPU. Do not treat it's absence as a mulfunction")
    parser.add_argument('-L', "--log", action='store_true',
                        default=False,
                        help="Collect log, but only diff of duty cycle")
    parser.add_argument('-D', "--debug", action='store_true',
                        default=False,
                        help="Collect every message, not just diff")
    parser.add_argument('--unsecure', action='store_true',
                        default=False,
                        help='Skip AppArmor confinement check. Strongly discouraged')

    return parser.parse_args()


def fail_if_not_confined(argv0):
    '''Attempt to create unpredictably named file to determine if process is confined by Mandatory Access Control.
       Only covers confinement, not necessarily enforcement'''
    profile_basename = pathlib.Path(argv0).stem
    random_tail = ''.join(random.choice(string.ascii_letters) for i in range(8))
    path = f'/dev/shm/{profile_basename}.am_i_confined.{random_tail}'

    try:
        with open(path, 'w', encoding="UTF-8") as f:
            f.write("DELETEME\n")
    except Exception:  # expected behavior
        pass

    # Check if not running as shebang
    launcher = os.environ.get('_', '')
    if pathlib.Path(launcher).name == 'python3':
        print(f"""[ERROR] This script is only designed to run with shebang
Incorrect:     'python3 {argv0}'
Correct usage: '{argv0}'\n"""
, file=sys.stderr)

    f = pathlib.Path(path)
    if f.is_file():
        f.unlink()
        raise EnvironmentError(f'''The process is not confined by AppArmor. Refusing to function. Expected action:\n
$ sudo install -m 600 -o root -g root apparmor.d/{profile_basename} /etc/apparmor.d/
$ sudo apparmor_parser --add /etc/apparmor.d/{profile_basename}''')

    return None


if __name__ == "__main__":

    args = handle_args()
    if not args.unsecure:
        fail_if_not_confined(sys.argv[0])

    if not PERIPHERY_LOADED:
        print("""[CRITICAL] The 'python3-periphery' module is not installed.
\nTo resolve this on Debian-based system (Orange Pi / Raspberry Pi)
install it natively using system's package manager:
------------------------------------------------------------
  sudo apt update
  sudo apt install python3-periphery
------------------------------------------------------------
\n*Note: Avoid using 'pip install python3-periphery' on modern Debian/Ubuntu
to prevent breaking PEP-668 Externally Managed Environment blocks.""", file=sys.stderr)
        sys.exit(113)

    signal.signal(signal.SIGUSR1, handle_usr1)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    pwm_dict = get_pwmchip_address_map()
    all_pwms = '\n'.join(pwm_dict.keys())
    if args.list:
        print(all_pwms)
        sys.exit(0)

    if not pre_flight_hardware_check():
        sys.exit(108)

    if not args.device:
        print("'--device' is mandatory")
        print('Available devices:\n')
        print(all_pwms)
        sys.exit(109)

    print("Mapped PWM Chip Addresses:")
    print(pwm_dict)

    if not args.device in pwm_dict:
        print(f"'{args.device}' no such device", file=sys.stderr)
        sys.exit(111)
    else:
        print('Using:', args.device)

    print("\nProceeding to start the Fan Controller...")
    run_controller(pwm_dict[args.device], args)
