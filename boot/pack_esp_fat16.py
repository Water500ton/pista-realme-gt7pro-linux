#!/usr/bin/env python3
"""Build a UFS-4K-native (4096 bytes/sector) FAT16 ESP image for recovery_b (100 MB)."""
import os
import struct
import sys

IMG_SIZE = 104_857_600  # 100.00 MiB = exact recovery_b size
BPS = 4096              # UFS logical sector size
SPC = 1                 # 1 sector per cluster -> 4096 B/cluster (~25,569 clusters -> valid FAT16)
RESERVED = 1
NUM_FATS = 2
ROOT_ENTRIES = 512      # 512 * 32 = 16384 B = 4 sectors
ROOT_SECTORS = (ROOT_ENTRIES * 32) // BPS
TOTAL_SECTORS = IMG_SIZE // BPS

# FAT16 size: ~25600 entries * 2 bytes = 51200 B -> 13 sectors per FAT
FAT_SECTORS = ((TOTAL_SECTORS * 2) + BPS - 1) // BPS
FIRST_DATA_SECTOR = RESERVED + NUM_FATS * FAT_SECTORS + ROOT_SECTORS
TOTAL_CLUSTERS = (TOTAL_SECTORS - FIRST_DATA_SECTOR) // SPC
assert 4085 <= TOTAL_CLUSTERS < 65525, f"Invalid FAT16 cluster count: {TOTAL_CLUSTERS}"


def make_83(name: str) -> bytes:
    if "." in name:
        base, ext = name.rsplit(".", 1)
    else:
        base, ext = name, ""
    base = base.upper().encode("ascii")
    ext = ext.upper().encode("ascii")
    assert len(base) <= 8 and len(ext) <= 3, f"Not 8.3 name: {name}"
    return base.ljust(8, b" ") + ext.ljust(3, b" ")


def dir_entry(name83: bytes, attr: int, first_cluster: int, size: int) -> bytes:
    return struct.pack(
        "<11sBBBHHHHHHHL",
        name83,
        attr,
        0,
        0,
        0,
        0x5979,  # date
        0x5979,
        0,
        0,
        0x5979,
        first_cluster & 0xFFFF,
        size,
    )


def build_esp(out_path: str, boot_files: list[tuple[str, bytes]]) -> None:
    img = bytearray(IMG_SIZE)

    # Boot Sector (BPB)
    bpb = bytearray(BPS)
    bpb[0:3] = b"\xeb\x3c\x90"
    bpb[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", bpb, 11, BPS)
    bpb[13] = SPC
    struct.pack_into("<H", bpb, 14, RESERVED)
    bpb[16] = NUM_FATS
    struct.pack_into("<H", bpb, 17, ROOT_ENTRIES)
    struct.pack_into("<H", bpb, 19, TOTAL_SECTORS if TOTAL_SECTORS < 65536 else 0)
    bpb[21] = 0xF8
    struct.pack_into("<H", bpb, 22, FAT_SECTORS)
    struct.pack_into("<H", bpb, 24, 63)
    struct.pack_into("<H", bpb, 26, 255)
    struct.pack_into("<I", bpb, 28, 0)
    struct.pack_into("<I", bpb, 32, 0 if TOTAL_SECTORS < 65536 else TOTAL_SECTORS)
    bpb[36] = 0x80
    bpb[38] = 0x29
    struct.pack_into("<I", bpb, 39, 0x50495354)
    bpb[43:54] = b"PISTA_ESP  "
    bpb[54:62] = b"FAT16   "
    bpb[510:512] = b"\x55\xaa"
    img[0:BPS] = bpb

    fat = [0] * (FAT_SECTORS * BPS // 2)
    fat[0] = 0xFFF8
    fat[1] = 0xFFFF
    next_cluster = 2

    def alloc_chain(data: bytes) -> int:
        nonlocal next_cluster
        if len(data) == 0:
            return 0
        n = (len(data) + BPS - 1) // BPS
        start = next_cluster
        for i in range(n):
            c = start + i
            fat[c] = (c + 1) if i + 1 < n else 0xFFFF
            off = (FIRST_DATA_SECTOR + (c - 2) * SPC) * BPS
            chunk = data[i * BPS : (i + 1) * BPS]
            img[off : off + len(chunk)] = chunk
        next_cluster += n
        return start

    # Allocate cluster 2 for /BOOT directory
    boot_dir_cluster = next_cluster
    fat[boot_dir_cluster] = 0xFFFF
    next_cluster += 1

    # Root directory entries: Volume Label + BOOT dir
    root_off = (RESERVED + NUM_FATS * FAT_SECTORS) * BPS
    root_entries = bytearray()
    root_entries += dir_entry(b"PISTA_ESP  ", 0x08, 0, 0)
    root_entries += dir_entry(make_83("BOOT"), 0x10, boot_dir_cluster, 0)
    img[root_off : root_off + len(root_entries)] = root_entries

    # /BOOT directory entries: ".", "..", and files
    boot_entries = bytearray()
    boot_entries += dir_entry(b".          ", 0x10, boot_dir_cluster, 0)
    boot_entries += dir_entry(b"..         ", 0x10, 0, 0)
    for name, data in boot_files:
        c = alloc_chain(data)
        boot_entries += dir_entry(make_83(name), 0x20, c, len(data))
        print(f"  \\boot\\{name:12s}  {len(data):10d} B  start_cluster={c}")

    assert len(boot_entries) <= BPS
    boot_dir_off = (FIRST_DATA_SECTOR + (boot_dir_cluster - 2) * SPC) * BPS
    img[boot_dir_off : boot_dir_off + len(boot_entries)] = boot_entries

    # Write FAT1 and FAT2
    fat_bytes = struct.pack(f"<{len(fat)}H", *fat)
    for fidx in range(NUM_FATS):
        foff = (RESERVED + fidx * FAT_SECTORS) * BPS
        img[foff : foff + len(fat_bytes)] = fat_bytes

    with open(out_path, "wb") as f:
        f.write(img)

    used_mb = (next_cluster - 2) * BPS / (1024 * 1024)
    print(f"Built {out_path}: {len(img)} B ({TOTAL_CLUSTERS} clusters, {used_mb:.2f} MiB used)")


if __name__ == "__main__":
    base = "out/alpine_arm64"
    files = [
        ("IMAGE.EFI", open(f"{base}/Image.efi", "rb").read()),
        ("INITRD.IMG", open(f"{base}/initrd.img", "rb").read()),
        ("PISTA.DTB", open(f"{base}/pista.dtb", "rb").read()),
        ("CMDLINE.TXT", open(f"{base}/cmdline.txt", "rb").read()),
    ]
    build_esp("dist/esp_recovery_b.img", files)
