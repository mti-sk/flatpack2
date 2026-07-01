# Disclaimer

## Experimental software

flatpack2 has been tested on real hardware and works as documented in the
README (see "Hardware confirmed working" in the session state summary).
Despite that, this project is **experimental software based on reverse
engineering** of an undocumented, proprietary CAN protocol. Eltek never
published a public specification for the Flatpack2 command set used here;
the frame formats, arbitration IDs and value encodings were determined by
observing traffic on specific hardware samples and may not hold for every
unit, firmware revision, or Flatpack2 variant in existence.

The README's "Known issues" section lists specific behaviors that are
either unverified or only partially tested (e.g. multi-PSU STATUS dispatch,
the DETECT→RAMP→CC charger sequence, and passive/back-feed voltage
readings). Treat anything not explicitly marked as hardware-confirmed as
unverified.

## No warranty

This software is provided **"as is"**, without warranty of any kind,
express or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, accuracy, and
non-infringement. There is no guarantee that the software is free of
defects, that it will operate without interruption, or that it correctly
interprets or controls the connected hardware in every situation.

## No liability

To the maximum extent permitted by applicable law, the author(s) and
contributors of this project accept **no liability whatsoever** for any
direct, indirect, incidental, special, consequential, or exemplary damages
arising from the use of, or inability to use, this software or the
information in this repository - including but not limited to:

- damage to the PSU, batteries, connected loads, or other equipment,
- data loss,
- fire, electrical damage, or personal injury,
- financial loss,

even if advised of the possibility of such damage.

By using this software, you accept that **you do so entirely at your own
risk**.

## Electrical and battery safety

This software can command a power supply to output voltages and currents
that, if misconfigured, can damage a connected battery or load, or create
a fire/safety hazard - particularly when used to charge lithium-based
batteries (e.g. the included LiFePO4 charger). You are solely responsible
for:

- verifying that the configuration (voltage, current, OVP, power rating,
  charger parameters) matches your actual hardware and battery
  specifications before connecting anything,
- using appropriate external protection (fuses, BMS, disconnects) that
  does not depend solely on this software behaving correctly,
- complying with all applicable electrical safety codes and regulations
  in your jurisdiction,
- your own competence, or seeking qualified assistance, when working with
  DC power systems of this voltage/current class.

The `power_rating` configuration option changes enforced software limits
only; it does not change what the physical hardware is actually capable
of or rated for. Setting it incorrectly does not make a lower-rated unit
safe to run at a higher power, nor does it protect a unit if set too low.

## No affiliation

This project is an independent, community effort and is **not affiliated
with, endorsed by, or supported by Eltek** or any successor/rights holder
of the Flatpack2 product line. "Flatpack2" and any other hardware/brand
names mentioned are used solely to identify compatibility and are the
property of their respective owners.

## License vs. this disclaimer

This project is licensed under the MIT License (see `LICENSE`), which
already contains its own "as is" / no-warranty language. This document
does not replace the license; it expands on the same principle in more
concrete terms specific to controlling power hardware.

## Contributions

Bug reports, hardware test results, and pull requests are welcome and
genuinely help close the gaps listed under "Known issues." Reported
findings do not change the disclaimer above for any other user's setup.
