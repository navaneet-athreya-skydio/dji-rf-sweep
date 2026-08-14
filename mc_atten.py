#!/usr/bin/env python3
"""Read/set Mini-Circuits RC4DAT-8G-120H attenuation over USB (no network, no root).

Usage:
    python3 /tmp/mc_atten.py                 # read all 4 channels
    python3 /tmp/mc_atten.py 30              # set ALL channels to 30 dB
    python3 /tmp/mc_atten.py 2 45            # set channel 2 to 45 dB
"""
import ctypes as C
import sys
import time

lib = C.CDLL("libusb-1.0.so.0")
ctxp = C.c_void_p
devhp = C.c_void_p
lib.libusb_init.argtypes = [C.POINTER(ctxp)]
lib.libusb_open_device_with_vid_pid.restype = devhp
lib.libusb_open_device_with_vid_pid.argtypes = [ctxp, C.c_uint16, C.c_uint16]
lib.libusb_detach_kernel_driver.argtypes = [devhp, C.c_int]
lib.libusb_claim_interface.argtypes = [devhp, C.c_int]
lib.libusb_release_interface.argtypes = [devhp, C.c_int]
lib.libusb_close.argtypes = [devhp]
lib.libusb_interrupt_transfer.argtypes = [devhp, C.c_ubyte, C.POINTER(C.c_ubyte), C.c_int, C.POINTER(C.c_int), C.c_uint]

MAX_ATTEN = 121.95

c = ctxp()
lib.libusb_init(C.byref(c))
h = lib.libusb_open_device_with_vid_pid(c, 0x20CE, 0x0023)
if not h:
    raise SystemExit("Mini-Circuits attenuator not found on USB")
try:
    lib.libusb_detach_kernel_driver(h, 0)
except Exception:
    pass
lib.libusb_claim_interface(h, 0)


def write(cmd):
    buf = (C.c_ubyte * 64)()
    for i, x in enumerate(cmd.encode()[:64]):
        buf[i] = x
    n = C.c_int(0)
    lib.libusb_interrupt_transfer(h, 0x01, buf, 64, C.byref(n), 1000)


def read():
    buf = (C.c_ubyte * 64)()
    n = C.c_int(0)
    rc = lib.libusb_interrupt_transfer(h, 0x81, buf, 64, C.byref(n), 1000)
    if rc != 0:
        return f"<rc={rc}>"
    out = []
    for x in bytes(buf[: n.value])[1:]:
        if x in (0, 255):
            break
        out.append(chr(x))
    return "".join(out).strip()


def query(cmd):
    # write+read twice to absorb the device's one-response lag
    write(cmd)
    time.sleep(0.08)
    read()
    write(cmd)
    time.sleep(0.08)
    return read()


def set_chan(ch, val):
    val = max(0.0, min(MAX_ATTEN, float(val)))
    return query(f"*:CHAN:{ch}:SETATT:{val};")


def read_all():
    return {ch: query(f"*:CHAN:{ch}:ATT?") for ch in (1, 2, 3, 4)}


args = sys.argv[1:]
if len(args) == 0:
    print("Current attenuation (dB):")
    for ch, v in read_all().items():
        print(f"  CH{ch}: {v}")
elif len(args) == 1:
    val = float(args[0])
    print(f"Setting ALL channels to {val} dB ...")
    for ch in (1, 2, 3, 4):
        set_chan(ch, val)
    for ch, v in read_all().items():
        print(f"  CH{ch}: {v}")
elif len(args) == 2:
    ch, val = int(args[0]), float(args[1])
    print(f"Setting CH{ch} to {val} dB ...")
    set_chan(ch, val)
    print(f"  CH{ch}: {query(f'*:CHAN:{ch}:ATT?')}")
else:
    print(__doc__)

lib.libusb_release_interface(h, 0)
lib.libusb_close(h)
