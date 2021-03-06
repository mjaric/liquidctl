"""liquidctl driver for Gigabyte RGB Fusion 2.0 USB controllers.

RGB Fusion 2.0
--------------

RGB Fusion 2.0 is a lighting system that supports 12 V non-addressable RGB and
5 V addressable ARGB lighting accessories, along side RGB/ARGB memory modules
and other elements on the motherboard itself.  It is built into motherboards
that contain the RGB Fusion 2.0 logo, typically from Gigabyte.

These motherboards use one of many possible ITE Tech controller chips, which
are connected to the host via SMBus or USB, depending on the motherboard/chip
model.  This driver supports a few of the USB controllers.

Driver
------

This driver implements the following features available at the hardware level:

 - initialization
 - control of lighting modes and colors
 - reporting of firmware version

Channel names
-------------

As much as we would like to use descriptive channel names, currently it is not
practical to do so, since the correspondence between the hardware channels and
the corresponding features on the motherboard is not stable.  Hence, lighting
channels are given generic names: led1, led2, etc.

At this time, 7 lighting channels are defined; a 'sync' channel is also
provided, which applies the specified setting to all lighting channels.

Each user may need to create a table that associates generic channel names to
specific areas or headers on their motherboard. For example, a map for the
Gigabyte Z490 Vision D might look like this:

 - led1: This is the LED next to the IO panel
 - led2: This is one of two 12V RGB headers
 - led3: This is the LED on the PCH chip ("Designare" on Vision D)
 - led4: This is an array of LEDs behind the PCI slots on *back side* of motherboard
 - led5: This is second 12V RGB header
 - led6: This is one of two 5V addressable RGB headers
 - led7: This is second 5V addressable RGB header

The driver supports 6 color modes: off, static, pulse, flash, double-flash and
color-cycle.

The more elaborate color/animation schemes supported by the motherboard on the
addressable headers are not currently supported.

For color modes pulse, flash, double-flash and color-cycle, the speed of color
change is governed by the --speed parameter, one of the possible values:
slowest, slower, normal (default), faster, fastest or ludicrous.

Caveats
-------

On wake-from-sleep, the ITE controller will be reset and all color modes will
revert to static blue.  On macOS, the "sleepwatcher" utility can be installed
via Homebrew along with a script to be run on wake that will issue the
necessary liquidctl commands to restore desired lighting effects.  Similar
solutions may be used on Windows and Linux.

Copyright (C) 2020–2020  CaseySJ, Jonas Malaco and contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from collections import namedtuple
import logging
import sys

from liquidctl.driver.usb import UsbHidDriver
from liquidctl.util import clamp

LOGGER = logging.getLogger(__name__)

_REPORT_ID = 0xcc
_INIT_CMD = 0x60
_READ_LENGTH = 64
_WRITE_LENGTH = 64  # TODO double check, should probably be 65 (64 + report ID)

_COLOR_CHANNELS = {
    'led1': (0x20, 0x01),
    'led2': (0x21, 0x02),
    'led3': (0x22, 0x04),
    'led4': (0x23, 0x08),
    'led5': (0x24, 0x10),
    'led6': (0x25, 0x20),
    'led7': (0x26, 0x40),
}
# note: an eight channel is presumed to exist

_PULSE_SPEEDS = {
    'slowest':                          (0x40, 0x06, 0x40, 0x06, 0x20, 0x03),
    'slower':                           (0x78, 0x05, 0x78, 0x05, 0xbc, 0x02),
    'normal':                           (0xb0, 0x04, 0xb0, 0x04, 0xf4, 0x01),
    'faster':                           (0xe8, 0x03, 0xe8, 0x03, 0xf4, 0x01),
    'fastest':                          (0x84, 0x03, 0x84, 0x03, 0xc2, 0x01),
    'ludicrous':                        (0x20, 0x03, 0x20, 0x03, 0x90, 0x01),
}

_FLASH_SPEEDS = {
    'slowest':                          (0x64, 0x00, 0x64, 0x00, 0x60, 0x09),
    'slower':                           (0x64, 0x00, 0x64, 0x00, 0x90, 0x08),
    'normal':                           (0x64, 0x00, 0x64, 0x00, 0xd0, 0x07),
    'faster':                           (0x64, 0x00, 0x64, 0x00, 0x08, 0x07),
    'fastest':                          (0x64, 0x00, 0x64, 0x00, 0x40, 0x06),
    'ludicrous':                        (0x64, 0x00, 0x64, 0x00, 0x78, 0x05),
}

_DOUBLE_FLASH_SPEEDS = {
    'slowest':                          (0x64, 0x00, 0x64, 0x00, 0x28, 0x0a),
    'slower ':                          (0x64, 0x00, 0x64, 0x00, 0x60, 0x09),
    'normal':                           (0x64, 0x00, 0x64, 0x00, 0x90, 0x08),
    'faster':                           (0x64, 0x00, 0x64, 0x00, 0xd0, 0x07),
    'fastest':                          (0x64, 0x00, 0x64, 0x00, 0x08, 0x07),
    'ludicrous':                        (0x64, 0x00, 0x64, 0x00, 0x40, 0x06),
}

_COLOR_CYCLE_SPEEDS = {
    'slowest':                          (0x78, 0x05, 0xb0, 0x04, 0x00, 0x00),
    'slower':                           (0x7e, 0x04, 0x1a, 0x04, 0x00, 0x00),
    'normal':                           (0x52, 0x03, 0xee, 0x02, 0x00, 0x00),
    'faster':                           (0xf8, 0x02, 0x94, 0x02, 0x00, 0x00),
    'fastest':                          (0x26, 0x02, 0xc2, 0x01, 0x00, 0x00),
    'ludicrous':                        (0xcc, 0x01, 0x68, 0x01, 0x00, 0x00),
}

_ColorMode = namedtuple('_ColorMode', ['name', 'value', 'pulses', 'flash_count',
                                       'cycle_count', 'max_brightness', 'takes_color',
                                       'speed_values'])

_COLOR_MODES = {
    mode.name: mode
    for mode in [
        _ColorMode('off', 0x01, pulses=False, flash_count=0, cycle_count=0,
                   max_brightness=0, takes_color=False, speed_values=None),
        _ColorMode('static', 0x01, pulses=False, flash_count=0, cycle_count=0,
                   max_brightness=90, takes_color=True, speed_values=None),
        _ColorMode('pulse', 0x02, pulses=True, flash_count=0, cycle_count=0,
                   max_brightness=90, takes_color=True, speed_values=_PULSE_SPEEDS),
        _ColorMode('flash', 0x03, pulses=True, flash_count=1, cycle_count=0,
                   max_brightness=100, takes_color=True, speed_values=_FLASH_SPEEDS),
        _ColorMode('double-flash', 0x03, pulses=True, flash_count=2, cycle_count=0,
                   max_brightness=100, takes_color=True, speed_values=_DOUBLE_FLASH_SPEEDS),
        _ColorMode('color-cycle', 0x04, pulses=False, flash_count=0, cycle_count=7,
                   max_brightness=100, takes_color=False, speed_values=_COLOR_CYCLE_SPEEDS),
    ]
}

class RGBFusion2Driver(UsbHidDriver):
    """liquidctl driver for Gigabyte RGB Fusion 2.0 USB controllers."""

    SUPPORTED_DEVICES = [
        (0x048d, 0x5702, None, 'Gigabyte RGB Fusion 2.0 5702 Controller (experimental)', {}),
        (0x048d, 0x8297, None, 'Gigabyte RGB Fusion 2.0 8297 Controller (experimental)', {}),
    ]

    @classmethod
    def probe(cls, handle, **kwargs):
        """Probe `handle` and yield corresponding driver instances.

        These devices have multiple top-level HID usages.  On Windows and Mac
        each usage results in a different HID handle and, specifically on
        Windows, only one of them is usable.  So HidapiDevice handles matching
        other usages have to be ignored.
        """

        if (not sys.platform.startswith('linux')) and (handle.hidinfo['usage'] != _REPORT_ID):
            return
        yield from super().probe(handle, **kwargs)

    def initialize(self, **kwargs):
        """Initialize the device.

        Returns a list of `(property, value, unit)` tuples, containing the
        firmware version and other useful information provided by the hardware.
        """

        self._send_feature_report([_REPORT_ID, _INIT_CMD])
        data = self._get_feature_report(_REPORT_ID)
        self.device.release()
        assert data[0] == _REPORT_ID and data[1] == 0x01

        null = data.index(0, 12)
        dev_name = str(bytes(data[12:null]), 'ascii', errors='ignore')
        fw_version = tuple(data[4:8])
        return [
            ('Hardware name', dev_name, ''),
            ('Firmware version', '%d.%d.%d.%d' % fw_version, ''),
            ('LED channnels', data[3], '')
        ]

    def get_status(self, **kwargs):
        """Get a status report.

        Currently returns an empty list, but this behavior is not guaranteed as
        in the future the device may start to report useful information.  A
        non-empty list would contain `(property, value, unit)` tuples.
        """

        return []

    def set_color(self, channel, mode, colors, speed='normal', **kwargs):
        """Set the color mode for a specific channel.

        Up to seven individual channels are available, named 'led1' through
        'led7'.  In addition to these, the 'sync' channel can be used to apply
        the same settings to all channels.

        The table bellow summarizes the available channels.

        | Mode         | Colors required | Speed is customizable |
        | ------------ | --------------- | --------------------- |
        | off          |            zero |                    no |
        | static       |             one |                    no |
        | pulse        |             one |                   yes |
        | flash        |             one |                   yes |
        | double-flash |             one |                   yes |
        | color-cycle  |            zero |                   yes |

        `colors` should be an iterable of zero or one `[red, blue, green]`
        triples, where each red/blue/green component is a value in the range
        0–255.

        `speed`, when supported by the `mode`, can be one of: `slowest`,
        `slow`, `normal` (default), `faster`, `fastest` or `ludicrous`.
        """

        mode = _COLOR_MODES[mode.lower()]
        colors = iter(colors)
        channel = channel.lower()
        speed = speed.lower()

        if mode.takes_color:
            try:
                r, g, b = next(colors)
                single_color = (b, g, r)
            except StopIteration:
                raise ValueError(f'One color required for mode={mode.name}')
        else:
            single_color = (0, 0, 0)
        remaining = sum(1 for _ in colors)
        if remaining:
            LOGGER.warning('too many colors for mode=%s, dropping %d', mode.name, remaining)

        brightness = clamp(100, 0, mode.max_brightness)  # hardcode this for now
        data = [_REPORT_ID, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                  0x00, 0x00, 0x00, mode.value, brightness, 0x00]
        data += single_color
        data += [0x00, 0x00, 0x00, 0x00, 0x00]
        if mode.speed_values:
            data += mode.speed_values[speed]
        else:
            data += [0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        data += [0x00, 0x00, mode.cycle_count, int(mode.pulses), mode.flash_count]

        if channel == 'sync':
            selected_channels = _COLOR_CHANNELS.values()
        else:
            selected_channels = (_COLOR_CHANNELS[channel],)
        for addr1, addr2 in selected_channels:
            data[1:3] = addr1, addr2
            self._send_feature_report(data)
        self._execute_report()
        self.device.release()

    def reset_all_channels(self):
        """Reset all LED channels."""
        for addr1, _ in _COLOR_CHANNELS.values():
            self._send_feature_report([_REPORT_ID, addr1, 0])
        self._execute_report()

    def _get_feature_report(self, report_id):
        return self.device.get_feature_report(report_id, _READ_LENGTH)

    def _send_feature_report(self, data):
        padding = [0x0]*(_WRITE_LENGTH - len(data))
        self.device.send_feature_report(data + padding)

    def _execute_report(self):
        """Request for the previously sent lighting settings to be applied."""
        self._send_feature_report([_REPORT_ID, 0x28, 0xff])


# Acknowledgements by CaseySJ
#
# Thanks to SgtSixPack for capturing USB traffic on 0x8297 and testing the driver on Windows.
