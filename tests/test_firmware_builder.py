import hashlib
import tempfile
import unittest
from pathlib import Path

from firmware_builder import (
    FACTORY_MD5_START,
    FACTORY_PAYLOAD_LENGTH,
    FACTORY_PAYLOAD_START,
    FIRMWARE_DESTINATION,
    FIRMWARE_LENGTH,
    FIRMWARE_SOURCE,
    FLASH_SIZE,
    ISP_DESTINATION,
    ISP_LENGTH,
    ISP_SOURCE,
    FirmwareError,
    build_image,
    write_image,
)


def make_test_inputs() -> tuple[bytes, bytes]:
    backup = bytearray(b"\xff" * FLASH_SIZE)
    payload_prefix = (
        b"{mac:001122334455,pin:\x00\x00,devId:unit-id,"
        b"devName:CT3CY" + b"\x00" * 24 + b",hwVer:2.0" + b"\x00" * 8 + b",QRCode:"
    )
    payload = payload_prefix + b"\x00" * (FACTORY_PAYLOAD_LENGTH - len(payload_prefix) - 1) + b"}"
    factory = bytearray(b"\xff" * 0x2800)
    factory[:8] = b"M1CT\x05\x59\x00\x01"
    factory[8 : 8 + FACTORY_PAYLOAD_LENGTH] = payload
    factory[8 + FACTORY_PAYLOAD_LENGTH : 8 + FACTORY_PAYLOAD_LENGTH + 16] = hashlib.md5(
        payload
    ).digest()
    backup[0x2D800:0x30000] = factory

    upgrade = bytearray(b"\xff" * (ISP_SOURCE + ISP_LENGTH))
    upgrade[FIRMWARE_SOURCE : FIRMWARE_SOURCE + 4] = bytes.fromhex("27 05 19 56")
    upgrade[ISP_SOURCE : ISP_SOURCE + len(b"\x00\x00\x00\x01TL-IPC43AN-4_4.0\x00")] = (
        b"\x00\x00\x00\x01TL-IPC43AN-4_4.0\x00"
    )
    return bytes(backup), bytes(upgrade)


class FirmwareBuilderTests(unittest.TestCase):
    def test_builds_image_updates_factory_fields_and_checksum(self) -> None:
        backup, upgrade = make_test_inputs()
        image = build_image(backup, upgrade)

        self.assertEqual(len(image), FLASH_SIZE)
        self.assertEqual(
            image[FIRMWARE_DESTINATION : FIRMWARE_DESTINATION + FIRMWARE_LENGTH],
            upgrade[FIRMWARE_SOURCE : FIRMWARE_SOURCE + FIRMWARE_LENGTH],
        )
        self.assertEqual(
            image[ISP_DESTINATION : ISP_DESTINATION + ISP_LENGTH],
            upgrade[ISP_SOURCE : ISP_SOURCE + ISP_LENGTH],
        )
        payload = image[FACTORY_PAYLOAD_START : FACTORY_PAYLOAD_START + FACTORY_PAYLOAD_LENGTH]
        self.assertIn(b",devName:TL-IPC43AN-4", payload)
        self.assertIn(b",hwVer:4.0", payload)
        self.assertEqual(
            hashlib.md5(payload).digest(),
            image[FACTORY_MD5_START : FACTORY_MD5_START + 16],
        )
        self.assertEqual(image[:0x2D800], backup[:0x2D800])
        self.assertEqual(image[0x358F0:0x60000], backup[0x358F0:0x60000])

    def test_rejects_bad_original_checksum(self) -> None:
        backup, upgrade = make_test_inputs()
        bad_backup = bytearray(backup)
        bad_backup[FACTORY_MD5_START] ^= 0x01

        with self.assertRaisesRegex(FirmwareError, "MD5"):
            build_image(bytes(bad_backup), upgrade)

    def test_rejects_wrong_backup_size(self) -> None:
        _, upgrade = make_test_inputs()
        with self.assertRaisesRegex(FirmwareError, "8 MiB"):
            build_image(b"short", upgrade)

    def test_rejects_wrong_upgrade_identity(self) -> None:
        backup, upgrade = make_test_inputs()
        bad_upgrade = bytearray(upgrade)
        bad_upgrade[ISP_SOURCE : ISP_SOURCE + 4] = b"NOPE"

        with self.assertRaisesRegex(FirmwareError, "ISP"):
            build_image(backup, bytes(bad_upgrade))

    def test_refuses_to_write_over_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "backup.bin"
            source.write_bytes(b"input")
            with self.assertRaisesRegex(FirmwareError, "input"):
                write_image(source, b"\x00" * FLASH_SIZE, source_paths=(source,))
            self.assertEqual(source.read_bytes(), b"input")

    def test_refuses_existing_output_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.bin"
            output.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                write_image(output, b"\x00" * FLASH_SIZE)
            self.assertEqual(output.read_bytes(), b"keep")

    def test_writes_image_and_overwrites_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.bin"
            output.write_bytes(b"old")
            image = b"\xaa" * FLASH_SIZE

            write_image(output, image, overwrite=True)

            self.assertEqual(output.read_bytes(), image)


if __name__ == "__main__":
    unittest.main()
