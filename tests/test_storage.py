import tempfile
import unittest

from tile_server.storage import FilesystemTileStorage
from tile_server.tiles import TileError, TileRef


class StorageTests(unittest.TestCase):
    def test_filesystem_storage_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = FilesystemTileStorage(tmp, "v1")
            tile = TileRef("default", 0, 0, 0)
            storage.put(tile, b"png-data")
            obj = storage.get(tile)
            self.assertIsNotNone(obj)
            self.assertEqual(obj.data, b"png-data")

    def test_tile_validation_rejects_path_escape_layer(self):
        with self.assertRaises(TileError):
            TileRef("../bad", 0, 0, 0).validate()

    def test_tile_path_parses_default_and_named_layers(self):
        self.assertEqual(TileRef.from_segments("default", ["0", "0", "0.png"]), TileRef("default", 0, 0, 0))
        self.assertEqual(TileRef.from_segments("default", ["overlay", "1", "1", "1.png"]), TileRef("overlay", 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
