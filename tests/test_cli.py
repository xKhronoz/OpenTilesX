import os
import unittest
from unittest import mock

from tile_server import cli


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class HealthcheckTest(unittest.TestCase):
    def test_http_healthcheck_uses_listen_port(self):
        with mock.patch.dict(os.environ, {"LISTEN_PORT": "18080"}):
            with mock.patch("tile_server.cli.urllib.request.urlopen", return_value=_Response()) as urlopen:
                self.assertEqual(cli._http_healthcheck(), 0)

        urlopen.assert_called_once_with("http://127.0.0.1:18080/healthz", timeout=2)


if __name__ == "__main__":
    unittest.main()
