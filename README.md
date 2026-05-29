# sbc-fan-control
Single-executable fan controller. Should work with every `pwmchip` fan.

## Features
- No dependencies except `python3-periphery` from package manager
- Adjusts speed based on both CPU and NVME temperatures
- Smooth curves - no sudden changes in noise
- Hysteresis - wont switch back and forth degrading the fan
- Works with multiple fans (with separate instance/service)
- Falls back at full speed in case of failure
- Multi-layered security

## Installation
```sh
$ sudo apt install python3-periphery                                              # install systemd module for python
$ git clone https://github.com/nobody43/sbc-fan-control.git
$ cd sbc-fan-control
$ sudo install -m 644 -o root -g root apparmor.d/sbc-fan-control /etc/apparmor.d/ # install AppArmor profile for executable
$ sudo apparmor_parser --add /etc/apparmor.d/sbc-fan-control                      # confine profile for executable
$ sudo install -m 755 -o root -g root sbc-fan-control.py /usr/local/bin/          # install the executable
$ sudo install -m 644 -o root -g root systemd/system/sbc-fan-control@.service /etc/systemd/system/  # install service unit
```

## Usage
1. You need to load PWM overlay in your boot config. Differs on every board - refer to documentation.
2. Determine available PWM chips/fans: `sudo sbc-fan-control.py --list`
3. Test without service: `sudo sbc-fan-control.py --device febf0020.pwm`
4. Enable the service: `sudo systemctl enable sbc-fan-control@febf0020.pwm.service`

## Deinstallation
```sh
$ sudo apt purge python3-periphery
$ sudo systemctl stop sbc-fan-control@febf0020.pwm.service
$ sudo systemctl disable sbc-fan-control@febf0020.pwm.service
$ sudo rm /etc/systemd/system/sbc-fan-control@.service
$ sudo rm /usr/local/bin/sbc-fan-control.py
$ sudo apparmor_parser --remove /etc/apparmor.d/sbc-fan-control
$ sudo rm /etc/apparmor.d/sbc-fan-control
```

## Tested on
- Orange Pi 5 Plus
- *tell me about others*
