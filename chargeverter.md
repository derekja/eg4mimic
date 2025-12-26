# EG4 Chargeverter comms + battery emulation (status as of 2025-12-26)

This document captures the current state of reverse engineering / emulation work for the **EG4 Chargeverter** RS-485 “Narada” battery interface, and provides enough information to reproduce the setup and generator-control behavior.

## Outcome achieved
We successfully emulated a battery BMS such that the Chargeverter:
- polls our emulator over RS-485
- receives valid replies (Modbus RTU)
- uses the emulated SOC to **start/stop the generator** (two-wire start relay logic)

In practice:
- SOC ≈ **53%** → generator **starts / continues charging**
- SOC ≈ **91%** → generator **stops / stops charging**

This is driven by editing a simple file (`soc.txt`) while the emulator is running.

---

## Hardware used

### Raspberry Pi side
- **Raspberry Pi 4B**
- **Waveshare RS-485 HAT** based on SC16IS752 (UART over SPI)
  - Creates serial devices like: `/dev/ttySC0` and `/dev/ttySC1`
  - One RS-485 port was used for the Chargeverter bus

### Chargeverter bus wiring
The RS-485 interface uses:
- RJ45 pins (Chargeverter RS-485): **pins 1 & 2** (common EG4 convention)
- RS-485 terminals on the interface board: **A**, **B**, **G**
  - In many RS-485 docs: `A` ≈ D- (inverting), `B` ≈ D+ (non-inverting)
  - If polarity is wrong, swap **A/B**

Termination:
- Many RS-485 boards have a jumper labeled **120R** vs **NC**:
  - **NC = termination OFF**
  - Use termination only if you are at an end of a long bus and see signal integrity issues.

---

## Pi configuration notes (SC16IS752 HAT)

### Overlay
We enabled the SC16IS752 overlay for **SPI1** (SPI0 did not produce working UART devices in our case).

In `/boot/config.txt` (or `/boot/firmware/config.txt` depending on OS):
- Keep any existing overlay lines (e.g., `dtoverlay=vc4-kms-v3d`)
- Add the SC16IS752 overlay as an additional line

Example:
```ini
dtparam=spi=on
dtoverlay=vc4-kms-v3d
dtoverlay=sc16is752-spi1,int_pin=24
```

After reboot, confirm devices exist:
```bash
ls -l /dev/ttySC*
```

Permissions (typical):
```bash
ls -l /dev/ttySC*
# usually root:dialout
sudo usermod -aG dialout $USER
# logout/login or reboot
```

---

## Confirming you are seeing real Chargeverter traffic
A definitive test:
1. Observe traffic (sniffer or emulator RX logs).
2. Unplug the Chargeverter RS-485 cable.
3. Traffic stops immediately → you were seeing real Chargeverter frames (not echo/noise).

---

## Frame/protocol information

### Key finding: it is Modbus RTU
Despite being described as “EG4 Narada”, the observed frames are **Modbus RTU**:
- Address (slave id): 1 byte
- Function: 1 byte
- Data: N bytes
- CRC16-MODBUS: 2 bytes, **LSB first**
- Silent gap framing (t3.5)

Background reference:
- https://en.wikipedia.org/wiki/Modbus

### Chargeverter poll (confirmed)
The Chargeverter repeatedly sends this request:

```text
01 03 00 13 00 11 74 03
```

Interpretation:
- slave id: `0x01`
- function: `0x03` (Read Holding Registers)
- start address: `0x0013`
- count: `0x0011` (= 17 registers; 0x0013..0x0023)
- CRC (LSB first): `74 03`

Observed bus settings that worked:
- **9600 baud**
- **8N1** (no parity) worked; if you see CRC failures try **8E1**.

---

## Which registers matter for generator control (confirmed experimentally)
Within the requested window (0x0013..0x0023), the following registers were used by our emulator to control Chargeverter behavior:

- Register **0x0015**: SOC (%)
- Register **0x0018**: SOC (%), mirror/duplicate
- Register **0x0017**: SOH (%) was left at 100 (0x0064)

When we set both SOC registers to:
- **53** → Chargeverter starts/keeps generator running
- **91** → Chargeverter stops generator

---

## Emulator program

### Files
- `eg4_cv_emulator.py` — Modbus RTU slave emulator with live SOC injection via `soc.txt`
- `chargeverter.md` — this document

### Install requirements (Pi)
We avoided pip/venv issues by using Debian packages:

```bash
sudo apt update
sudo apt install -y python3-serial
```

### Run emulator (Pi)
```bash
echo 53 > soc.txt
chmod +x eg4_cv_emulator.py
./eg4_cv_emulator.py --port /dev/ttySC0 --baud 9600 --slave 0x01 --parity N
```

Flip SOC live from another shell (no restart required):
```bash
echo 91 > soc.txt
echo 53 > soc.txt
```

If you see lots of `BADCRC`, rerun with parity even:
```bash
./eg4_cv_emulator.py --port /dev/ttySC0 --baud 9600 --slave 0x01 --parity E
```

---

## Capturing frames on macOS using the EG4 USB↔RJ45 (RS-485) adapter

### Identifying the chipset
On macOS, run:
```bash
system_profiler SPUSBDataType
```

In our setup, the adapter enumerated as:
- Vendor ID: **0x1a86** (WCH)
- Product ID: **0x7523** (CH340/CH341 family)

### Driver installation (CH34x / CH340)
WCH driver repository (macOS):
- https://github.com/WCHSoftGroup/ch34xser_macos

General CH340 driver overview:
- https://learn.sparkfun.com/tutorials/how-to-install-ch340-drivers/all

Install notes:
1. Install the provided macOS driver package.
2. Approve in **System Settings → Privacy & Security** if blocked.
3. Reboot.
4. Confirm you now have a serial device such as:
   - `/dev/cu.wchusbserial*`

This can be used with serial tools/scripts to record request/response frames and build a more complete register map if needed.

---

## Next planned step
Replace `soc.txt` with a real SOC source:
- read SOC from the inverter over Modbus (second RS-485 port), or
- another trusted SOC measurement.

The Chargeverter interface can continue to be satisfied by serving registers 0x0013..0x0023 with consistent values and live-updated SOC.
