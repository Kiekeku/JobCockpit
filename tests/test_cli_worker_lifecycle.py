import logging
from threading import Event

import pytest

from jobcockpit import cli
from jobcockpit.core.config import Settings


@pytest.mark.parametrize("command,module,worker_class", [
    ("publication", "jobcockpit.services.publication", "PublicationWorker"),
    ("source", "jobcockpit.ingestion.source_reconciliation", "SourceBackgroundWorker"),
])
@pytest.mark.parametrize("outcome,exit_code", [("died", 1), ("stopped", 0), ("interrupt", 0)])
def test_worker_exit_status_distinguishes_failure_from_shutdown(
    monkeypatch, caplog, command, module, worker_class, outcome, exit_code,
):
    caplog.set_level(logging.ERROR, logger=cli.__name__)
    stopped = []

    class Thread:
        def is_alive(self):
            return outcome == "interrupt"

        def join(self, _timeout):
            raise KeyboardInterrupt

    class Worker:
        def __init__(self, _settings):
            self.thread = Thread()
            self.stop_event = Event()
            if outcome == "stopped":
                self.stop_event.set()

        def start(self):
            pass

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(f"{module}.{worker_class}", Worker)
    monkeypatch.setattr(cli, "init_database", lambda settings: None)
    settings = Settings(_env_file=None, publication_enabled=True)
    assert cli.main(["worker", command], settings=settings) == exit_code
    assert stopped == [True]
    assert ("stopped unexpectedly" in caplog.text) is (outcome == "died")
