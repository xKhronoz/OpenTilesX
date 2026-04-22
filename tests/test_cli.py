import os
import unittest
from unittest import mock

from tests.helpers import minimal_config
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

    def test_render_worker_supervisor_spawns_configured_processes(self):
        class FakeProcess:
            created = []

            def __init__(self, target, args):
                self.target = target
                self.args = args
                self.started = False
                FakeProcess.created.append(self)

            def start(self):
                self.started = True

            def join(self):
                return None

            def is_alive(self):
                return False

            def terminate(self):
                return None

        with mock.patch("tile_server.cli.AppConfig.from_env", return_value=minimal_config(render_worker_processes=2)):
            with mock.patch("tile_server.cli.JobStore"):
                with mock.patch("tile_server.cli._apply_active_bundle", side_effect=lambda config, store: config):
                    with mock.patch("tile_server.cli.multiprocessing.Process", FakeProcess):
                        with mock.patch("tile_server.cli.signal.signal"):
                            self.assertEqual(cli._render_worker(), 0)

        self.assertEqual(len(FakeProcess.created), 2)
        self.assertTrue(all(process.started for process in FakeProcess.created))

    def test_prepare_external_data_uses_manager(self):
        with mock.patch("tile_server.cli.AppConfig.from_env", return_value=minimal_config()):
            with mock.patch("tile_server.cli.ExternalDataManager") as manager_cls:
                manager_cls.return_value.prepare_bundle.return_value = {"available": True, "mode": "vendored"}
                self.assertEqual(
                    cli._prepare_external_data(
                        [
                            "--style-dir",
                            "/opt/openstreetmap-carto-default",
                            "--external-data-uri",
                            "/data/external-data/osm-carto-cache",
                        ]
                    ),
                    0,
                )

        manager_cls.return_value.prepare_bundle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
