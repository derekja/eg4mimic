#!/usr/bin/env python3
"""EG4 Chargeverter "Narada" battery emulator (Modbus RTU slave)

What this does
- Listens on an RS-485 serial port and behaves like a minimal battery BMS.
- Implements Modbus RTU slave ID 0x01, function 0x03 (Read Holding Registers).
- Specifically supports the Chargeverter poll we observed:
    01 03 00 13 00 11 74 03
  which requests registers 0x0013..0x0023 (17 registers).
- Injects SOC into registers 0x0015 and 0x0018 from a live-updated text file (soc.txt).
  Changing soc.txt between e.g. 53 and 91 caused the Chargeverter to start/stop the generator.

Usage (on the Pi)
    sudo apt update
    sudo apt install -y python3-serial
    echo 53 > soc.txt
    ./eg4_cv_emulator.py --port /dev/ttySC0 --baud 9600 --slave 0x01 --parity N

Then in another shell:
    echo 91 > soc.txt
    echo 53 > soc.txt

Notes
- If you see lots of BADCRC messages, try --parity E (8E1).
- This script assumes the RS-485 adapter uses RTS for DE/RE direction control.
"""

import argparse
import datetime as dt
import time
from typing import Optional, List

import serial
from serial.rs485 import RS485Settings


def ts() -> str:
    return dt.datetime.now().isoformat(timespec="milliseconds")


def crc16_modbus(data: bytes) -> int:
    # Modbus RTU CRC16: poly 0xA001, init 0xFFFF; transmitted LSB first.
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def regs_to_bytes(regs: List[int]) -> bytes:
    out = bytearray()
    for r in regs:
        out.append((r >> 8) & 0xFF)
        out.append(r & 0xFF)
    return bytes(out)


class GapFramer:
    """Frame bytes by idle gap; sufficient for Modbus RTU in practice."""
    def __init__(self, gap_s: float):
        self.gap_s = gap_s
        self.buf = bytearray()
        self.last_rx: Optional[float] = None

    def feed(self, chunk: bytes, now: float) -> Optional[bytes]:
        if chunk:
            self.buf.extend(chunk)
            self.last_rx = now
            return None
        if self.buf and self.last_rx is not None and (now - self.last_rx) >= self.gap_s:
            out = bytes(self.buf)
            self.buf.clear()
            return out
        return None


def load_soc(path: str, default: int) -> int:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        v = int(float(raw))
        return max(0, min(100, v))
    except Exception:
        return default


def log_line(fp, s: str):
    if fp is not None:
        fp.write(s + "\n")
        fp.flush()


def main():
    ap = argparse.ArgumentParser(description="EG4 Chargeverter battery emulator (Modbus RTU slave)")
    ap.add_argument("--port", required=True, help="e.g. /dev/ttySC0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", choices=["N", "E", "O"], default="N")
    ap.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    ap.add_argument("--slave", type=lambda x: int(x, 0), default=0x01, help="slave id (default 0x01)")
    ap.add_argument("--soc-file", default="soc.txt", help="file containing SOC percent (e.g. 53 or 91)")
    ap.add_argument("--default-soc", type=int, default=53)
    ap.add_argument("--gap-ms", type=float, default=3.0, help="RTU idle gap delimiter (ms)")
    ap.add_argument("--log", default="", help="optional log file path")
    ap.add_argument("--quiet", action="store_true", help="reduce printing (still prints SOC changes)")
    args = ap.parse_args()

    parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        parity=parity_map[args.parity],
        stopbits=serial.STOPBITS_ONE if args.stopbits == 1 else serial.STOPBITS_TWO,
        bytesize=serial.EIGHTBITS,
        timeout=0.0,  # non-blocking
    )

    # RS-485 direction control (RTS toggles DE on most HATs/adapters)
    ser.rs485_mode = RS485Settings(
        rts_level_for_tx=True,
        rts_level_for_rx=False,
        delay_before_tx=0.0,
        delay_before_rx=0.0,
        loopback=False,
    )

    # Minimal register map covering 0x0000..0x0026 (39 regs).
    # Chargeverter poll: start=0x0013 count=0x0011 (regs 0x0013..0x0023)
    # We respond to any read fully within this map.
    base = [0] * 0x27

    # Seed plausible / consistent values. The key ones are SOC + SOH.
    base[0x0013] = 0x0017
    base[0x0014] = 0x0018
    base[0x0015] = args.default_soc   # SOC (patched live)
    base[0x0016] = 0x0032             # arbitrary but plausible
    base[0x0017] = 0x0064             # SOH = 100
    base[0x0018] = args.default_soc   # SOC duplicate (patched live)
    # Remaining registers default 0 for now.

    framer = GapFramer(args.gap_ms / 1000.0)

    logfp = open(args.log, "a", buffering=1) if args.log else None
    log_line(logfp, f"{ts()} START port={args.port} baud={args.baud} parity={args.parity} slave=0x{args.slave:02X}")

    if not args.quiet:
        print(f"{ts()} emulator up: slave=0x{args.slave:02X} port={args.port} baud={args.baud} parity={args.parity}")
        print(f"{ts()} edit {args.soc_file} to 53 or 91 to change SOC live")

    last_soc = None
    poll_ok = 0
    poll_badcrc = 0
    last_rate_t = time.time()

    try:
        while True:
            now = time.time()

            # Update SOC from file
            soc = load_soc(args.soc_file, args.default_soc)
            if soc != last_soc:
                print(f"{ts()} SOC={soc}%")
                log_line(logfp, f"{ts()} SOC={soc}")
                last_soc = soc

            # Read bytes and frame by idle gap
            chunk = ser.read(4096)
            pkt = framer.feed(chunk, now)
            if pkt is None:
                time.sleep(0.001)
            else:
                if len(pkt) < 8:
                    if not args.quiet:
                        print(f"{ts()} RX short len={len(pkt)} hex={pkt.hex(' ')}")
                    log_line(logfp, f"{ts()} RX short len={len(pkt)} hex={pkt.hex(' ')}")
                    continue

                # CRC validate
                body = pkt[:-2]
                want = pkt[-2] | (pkt[-1] << 8)
                got = crc16_modbus(body)
                if got != want:
                    poll_badcrc += 1
                    if not args.quiet:
                        print(f"{ts()} BADCRC len={len(pkt)} hex={pkt.hex(' ')}")
                    log_line(logfp, f"{ts()} BADCRC len={len(pkt)} hex={pkt.hex(' ')} got=0x{got:04X} want=0x{want:04X}")
                    continue

                poll_ok += 1
                sid, func = pkt[0], pkt[1]
                if sid != (args.slave & 0xFF):
                    continue

                if not args.quiet:
                    print(f"{ts()} RX {pkt.hex(' ')}")
                log_line(logfp, f"{ts()} RX {pkt.hex(' ')}")

                if func != 0x03:
                    # Illegal function
                    resp = bytes([sid, func | 0x80, 0x01])
                    c = crc16_modbus(resp)
                    resp2 = resp + bytes([c & 0xFF, (c >> 8) & 0xFF])
                    ser.write(resp2)
                    if not args.quiet:
                        print(f"{ts()} TX EXC01 {resp2.hex(' ')}")
                    log_line(logfp, f"{ts()} TX EXC01 {resp2.hex(' ')}")
                    continue

                start = (pkt[2] << 8) | pkt[3]
                count = (pkt[4] << 8) | pkt[5]

                if start + count > len(base):
                    # Illegal data address
                    resp = bytes([sid, 0x83, 0x02])
                    c = crc16_modbus(resp)
                    resp2 = resp + bytes([c & 0xFF, (c >> 8) & 0xFF])
                    ser.write(resp2)
                    if not args.quiet:
                        print(f"{ts()} TX EXC02 start=0x{start:04X} count={count}")
                    log_line(logfp, f"{ts()} TX EXC02 start=0x{start:04X} count={count} hex={resp2.hex(' ')}")
                    continue

                regs = base[start:start + count].copy()

                # Patch SOC live into known candidate registers if included in this read
                if start <= 0x0015 < start + count:
                    regs[0x0015 - start] = soc
                if start <= 0x0018 < start + count:
                    regs[0x0018 - start] = soc

                payload = regs_to_bytes(regs)
                resp = bytes([sid, 0x03, len(payload)]) + payload
                c = crc16_modbus(resp)
                resp2 = resp + bytes([c & 0xFF, (c >> 8) & 0xFF])
                ser.write(resp2)

                if not args.quiet:
                    print(f"{ts()} TX start=0x{start:04X} count={count} soc={soc} bytes={len(resp2)}")
                log_line(logfp, f"{ts()} TX start=0x{start:04X} count={count} soc={soc} bytes={len(resp2)} hex={resp2.hex(' ')}")

            # periodic rate report
            if not args.quiet and (time.time() - last_rate_t) > 5.0:
                dt_s = time.time() - last_rate_t
                rate_ok = poll_ok / dt_s
                rate_bad = poll_badcrc / dt_s
                print(f"{ts()} rate ok={rate_ok:.1f}/s badcrc={rate_bad:.1f}/s")
                poll_ok = 0
                poll_badcrc = 0
                last_rate_t = time.time()

    except KeyboardInterrupt:
        pass
    finally:
        log_line(logfp, f"{ts()} STOP")
        if logfp:
            logfp.close()
        ser.close()


if __name__ == "__main__":
    main()
