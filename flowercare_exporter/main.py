import argparse
import dataclasses
import logging
import os
import sys
from functools import partial
from threading import Lock

import gi
from gi.repository import GLib  # type: ignore

from miflora.metrics import Exporter, MiFloraFirmwareBattery, MiFloraSensor

gi.require_version("Gio", "2.0")

from miflora.bluez import MiFlora, MiFloraManager

from .graphite import Graphite
from .prometheus import PushGateway

log = logging.getLogger(__name__)


def _get_alias_mapping(args: argparse.Namespace) -> dict[str, str]:
    return dict([alias_s.split("=") for alias_s in args.alias])


def metrics(mainloop: GLib.MainLoop, args: argparse.Namespace):
    mifloramanager = MiFloraManager(_get_alias_mapping(args))
    metrics_received: set[str] = set()  # set of macs
    exporters: list[Exporter] = []

    _connect_lock = Lock()  # prevent concurrent connections
    _connect_device: set[str] = set()  # current device macs we are connecting to

    if args.graphite_url:
        exporters.append(Graphite(args.graphite_url, os.getenv("METRICS_USER"), os.getenv("METRICS_PASSWORD")))

    if args.prometheus_url:
        exporters.append(PushGateway(args.prometheus_url, os.getenv("METRICS_USER"), os.getenv("METRICS_PASSWORD")))

    def _sensor_received(miflora: MiFlora, sensordata: MiFloraSensor):
        print(dataclasses.asdict(sensordata))
        miflora.disconnect()  # all data received
        _connect_device.remove(miflora.address)
        _connect_lock.release()  # allow next connection
        metrics_received.add(miflora.address)
        for exporter in exporters:
            exporter.send_sensor(sensordata)

    def _firmware_battery_received(miflora: MiFlora, firmwarebattery: MiFloraFirmwareBattery):
        print(dataclasses.asdict(firmwarebattery))
        miflora.read_sensor(_sensor_received)
        for exporter in exporters:
            exporter.send_battery(firmwarebattery)

    def get_metrics(miflora: MiFlora):
        miflora.read_firmware_battery(_firmware_battery_received)

    def _concurrent_connect(miflora: MiFlora, retry=3):
        if _connect_lock.acquire(blocking=False):
            log.debug(f"Starting connection to  {miflora.address} (retry={retry}).")
            _connect_device.add(miflora.address)
            miflora.connect(lambda miflora: _concurrent_connect(miflora, retry=retry - 1))
        elif retry:
            log.warning(
                f"Connection(s) to {','.join(_connect_device)} in progress, sleeping for 5 seconds before connecting  to {miflora.address}"
            )
            GLib.timeout_add_seconds(5, partial(_concurrent_connect, miflora, retry=retry))
        return False  # no auto recall

    def miflora_added(__manager__, miflora: MiFlora):
        if miflora.address in metrics_received:
            log.info(f"Not connecting to {miflora.address} (metric already collected)")
        else:
            miflora.on_services_disovered = get_metrics
            _concurrent_connect(miflora)

    def quit():
        log.info(f"Received Data from {len(metrics_received)} MiFloras")
        mainloop.quit()

    mifloramanager.on_miflora_added = miflora_added
    mifloramanager.setup_adapter()  # trigger events
    mifloramanager.start_discovery()
    GLib.timeout_add_seconds(args.timeout, quit)


def blink(mainloop: GLib.MainLoop, args: argparse.Namespace):
    alias_mapping = _get_alias_mapping(args)

    def miflora_added(__manager__, miflora: MiFlora):
        miflora.on_services_disovered = MiFlora.blink
        miflora.connect(lambda miflora: log.error(f"Failed to connect to {miflora.address}"))

    mifloramanager = MiFloraManager(alias_mapping)
    mifloramanager.on_miflora_added = miflora_added
    mifloramanager.setup_adapter()  # trigger events


def main():
    parser = argparse.ArgumentParser(
        prog="miflora_exporter",
        description="Miflora plant sensor exporter",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    loglevels = {
        "DEBUG": logging.debug,
        "INFO": logging.info,
        "WARNING": logging.warning,
        "ERROR": logging.error,
        "CRITICAL": logging.critical,
    }
    parser.add_argument("-g", "--graphite-url", type=str, required=False, help="Post Metrics to Graphite Metrics URL")
    parser.add_argument(
        "-p", "--prometheus-url", type=str, required=False, help="Post Metrics to Prometheus Pushgateway URL"
    )
    parser.add_argument(
        "-l",
        "--log-level",
        choices=loglevels.keys(),
        help="log level; one of: CRITICAL, ERROR, WARNING, INFO, DEBUG",
        default="WARNING",
    )
    parser.add_argument(
        "-a",
        "--alias",
        default=[],
        type=str,
        action="append",
        help="Set aliases for specified device (e.g.: C4:7C:8D:XX:YY:ZZ=Bromeliad). Can be repeated",
    )

    parser.add_argument("-t", "--timeout", type=int, default=60, help="Scan timeout in seconds")
    subparsers = parser.add_subparsers(
        title="subcommands", required=True, dest="command", description="valid subcommands", help="Operation mode"
    )
    subparsers.add_parser("metrics")
    subparsers.add_parser("blink")
    args = parser.parse_args(sys.argv[1:])

    logging.basicConfig(
        level=args.log_level,
        format=("%(asctime)s %(levelname)-8s %(message)s" if sys.stdout.isatty() else "%(levelname)-8s %(message)s"),
    )
    mainloop = GLib.MainLoop()

    match args.command:
        case "metrics":
            metrics(mainloop, args)
        case "blink":
            blink(mainloop, args)
    try:
        mainloop.run()
    except KeyboardInterrupt:
        mainloop.quit()
