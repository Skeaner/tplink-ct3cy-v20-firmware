#!/usr/bin/env python3
"""Build a CT3CY V2.0 image from a programmer backup and TL-IPC upgrade file."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox

FLASH_SIZE = 0x800000
FIRMWARE_SOURCE = 0x800
FIRMWARE_DESTINATION = 0x60000
FIRMWARE_LENGTH = 0x7A0000
ISP_SOURCE = 0x7B1084
ISP_DESTINATION = 0x30000
ISP_LENGTH = 0x58F0
FACTORY_START = 0x2D800
FACTORY_END = 0x30000
FACTORY_PAYLOAD_START = 0x2D808
FACTORY_PAYLOAD_LENGTH = 0x559
FACTORY_MD5_START = FACTORY_PAYLOAD_START + FACTORY_PAYLOAD_LENGTH
FACTORY_MD5_END = FACTORY_MD5_START + 16
ISP_MARKER = b"\x00\x00\x00\x01TL-IPC43AN-4_4.0\x00"


class FirmwareError(ValueError):
    """Raised when an input does not match the supported firmware layout."""


def _replace_padded_field(
    payload: bytearray, key: bytes, expected: str, replacement: str, next_key: bytes
) -> None:
    key_position = payload.find(key)
    if key_position < 0 or payload.find(key, key_position + 1) >= 0:
        raise FirmwareError(f"Factory field {key.decode('ascii')} is missing or ambiguous.")

    value_start = key_position + len(key)
    value_end = payload.find(next_key, value_start)
    if value_end < 0:
        raise FirmwareError(f"Factory field boundary after {key.decode('ascii')} was not found.")

    old_value = payload[value_start:value_end]
    expected_bytes = expected.encode("ascii")
    replacement_bytes = replacement.encode("ascii")
    if not old_value.startswith(expected_bytes) or any(old_value[len(expected_bytes) :]):
        raise FirmwareError(
            f"Expected factory value {expected!r} with zero padding for {key.decode('ascii')}."
        )
    if len(replacement_bytes) > len(old_value):
        raise FirmwareError(f"Replacement does not fit factory field {key.decode('ascii')}.")

    payload[value_start:value_end] = replacement_bytes + bytes(
        len(old_value) - len(replacement_bytes)
    )


def build_image(backup: bytes, upgrade: bytes) -> bytes:
    """Validate the inputs and return a complete modified 8 MiB flash image."""
    if len(backup) != FLASH_SIZE:
        raise FirmwareError(f"Programmer backup must be exactly 8 MiB; got {len(backup):,} bytes.")
    if len(upgrade) < ISP_SOURCE + ISP_LENGTH:
        raise FirmwareError("Official firmware file is too short for the documented firmware/ISP regions.")
    if backup[FACTORY_START:FACTORY_START + 4] != b"M1CT":
        raise FirmwareError("Backup does not contain the expected M1CT factory-info marker.")
    if backup[FACTORY_START + 4:FACTORY_START + 8] != bytes.fromhex("05 59 00 01"):
        raise FirmwareError("Backup has an unexpected M1CT header; refusing to modify it.")

    factory_payload = backup[
        FACTORY_PAYLOAD_START : FACTORY_PAYLOAD_START + FACTORY_PAYLOAD_LENGTH
    ]
    stored_md5 = backup[FACTORY_MD5_START:FACTORY_MD5_END]
    if len(factory_payload) != FACTORY_PAYLOAD_LENGTH or len(stored_md5) != 16:
        raise FirmwareError("M1CT payload or checksum is truncated.")
    if factory_payload[-1:] != b"}":
        raise FirmwareError("M1CT payload does not end with the expected '}' byte.")
    if hashlib.md5(factory_payload).digest() != stored_md5:
        raise FirmwareError("Programmer backup M1CT MD5 check failed.")

    firmware_slice = upgrade[FIRMWARE_SOURCE : FIRMWARE_SOURCE + FIRMWARE_LENGTH]
    isp_slice = upgrade[ISP_SOURCE : ISP_SOURCE + ISP_LENGTH]
    if len(firmware_slice) != FIRMWARE_LENGTH or firmware_slice[:4] != bytes.fromhex("27 05 19 56"):
        raise FirmwareError("Official firmware has an unexpected image header at offset 0x800.")
    if not isp_slice.startswith(ISP_MARKER):
        raise FirmwareError("ISP region does not identify TL-IPC43AN-4 V4.0.")

    image = bytearray(backup)
    image[FIRMWARE_DESTINATION : FIRMWARE_DESTINATION + FIRMWARE_LENGTH] = firmware_slice
    image[ISP_DESTINATION : ISP_DESTINATION + ISP_LENGTH] = isp_slice

    factory_payload = bytearray(
        image[FACTORY_PAYLOAD_START : FACTORY_PAYLOAD_START + FACTORY_PAYLOAD_LENGTH]
    )
    _replace_padded_field(factory_payload, b",devName:", "CT3CY", "TL-IPC43AN-4", b",hwVer:")
    _replace_padded_field(factory_payload, b",hwVer:", "2.0", "4.0", b",QRCode:")
    image[FACTORY_PAYLOAD_START : FACTORY_PAYLOAD_START + FACTORY_PAYLOAD_LENGTH] = factory_payload
    image[FACTORY_MD5_START:FACTORY_MD5_END] = hashlib.md5(factory_payload).digest()

    return bytes(image)


def write_image(
    output_path: str | Path,
    image: bytes,
    source_paths: tuple[str | Path, ...] = (),
    overwrite: bool = False,
) -> None:
    """Write an image without allowing either input file to be used as output."""
    destination = Path(output_path).expanduser().resolve()
    protected_sources = {Path(path).expanduser().resolve() for path in source_paths}
    if destination in protected_sources:
        raise FirmwareError("Output path must not be the same as either input file.")
    if len(image) != FLASH_SIZE:
        raise FirmwareError("Refusing to write an image that is not exactly 8 MiB.")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"Output folder does not exist: {destination.parent}")

    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                          dir=destination.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(image)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary_path.read_bytes() != image:
            raise OSError("Temporary output failed read-back verification.")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Output file already exists: {destination}")
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


class FirmwareBuilderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CT3CY V2.0 Firmware Builder")
        self.root.resizable(False, False)

        self.backup_path = tk.StringVar()
        self.upgrade_path = tk.StringVar()
        self._add_file_row(0, "编程器备份文件", self.backup_path, self._select_backup)
        self._add_file_row(1, "官方 TL-IPC 固件", self.upgrade_path, self._select_upgrade)

        tk.Label(
            root,
            text="仅生成固件文件，不会写入/刷写设备。原始输入文件不会被修改。",
            anchor="w",
        ).grid(row=2, column=0, columnspan=3, padx=10, pady=(4, 8), sticky="w")
        tk.Button(root, text="验证并生成固件…", command=self._generate).grid(
            row=3, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="e"
        )

    def _add_file_row(
        self, row: int, label: str, variable: tk.StringVar, command: Callable[[], None]
    ) -> None:
        tk.Label(self.root, text=label).grid(row=row, column=0, padx=10, pady=6, sticky="w")
        tk.Entry(self.root, textvariable=variable, width=64).grid(
            row=row, column=1, padx=6, pady=6
        )
        tk.Button(self.root, text="选择…", command=command).grid(
            row=row, column=2, padx=(0, 10), pady=6
        )

    def _select_backup(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 8 MiB 编程器备份",
            filetypes=(("固件文件", "*.bin"), ("所有文件", "*.*")),
        )
        if path:
            self.backup_path.set(path)

    def _select_upgrade(self) -> None:
        path = filedialog.askopenfilename(
            title="选择官方 TL-IPC43AN-4 V4.0 升级固件",
            filetypes=(("固件文件", "*.bin"), ("所有文件", "*.*")),
        )
        if path:
            self.upgrade_path.set(path)

    def _generate(self) -> None:
        backup_name = self.backup_path.get().strip()
        upgrade_name = self.upgrade_path.get().strip()
        if not backup_name or not upgrade_name:
            messagebox.showerror("缺少输入文件", "请先选择编程器备份和官方固件。", parent=self.root)
            return

        output_name = filedialog.asksaveasfilename(
            title="选择新固件保存位置",
            initialfile="CT3CY_V2.0_TL-IPC43AN-4_V4.0.bin",
            defaultextension=".bin",
            filetypes=(("固件文件", "*.bin"), ("所有文件", "*.*")),
        )
        if not output_name:
            return

        overwrite = False
        if Path(output_name).exists():
            overwrite = messagebox.askyesno(
                "确认覆盖",
                "所选输出文件已存在。确认覆盖该文件吗？",
                parent=self.root,
            )
            if not overwrite:
                return

        try:
            backup = Path(backup_name).read_bytes()
            upgrade = Path(upgrade_name).read_bytes()
            image = build_image(backup, upgrade)
            write_image(
                output_name,
                image,
                source_paths=(backup_name, upgrade_name),
                overwrite=overwrite,
            )
        except (OSError, FirmwareError) as error:
            messagebox.showerror("生成失败", str(error), parent=self.root)
            return

        digest = hashlib.sha256(image).hexdigest()
        messagebox.showinfo(
            "生成完成",
            f"固件已保存：\n{Path(output_name)}\n\n"
            f"大小：{len(image):,} 字节\n"
            f"SHA-256：{digest}",
            parent=self.root,
        )


def main() -> None:
    root = tk.Tk()
    FirmwareBuilderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
