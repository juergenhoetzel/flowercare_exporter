import argparse
import asyncio
import dataclasses
import logging
import os
import sys

import gi
import gi.events  # type: ignore
from gi.repository import GLib  # type: ignore

from miflora.metrics import Exporter, MiFloraFirmwareBattery, MiFloraSensor

gi.require_version("Gio", "2.0")

from miflora.bluez import MiFlora, MiFloraManager

from .graphite import Graphite
from .prometheus import PushGateway

log = logging.getLogger(__name__)


def _get_alias_mapping(args: argparse.Namespace) -> dict[str, str]:
    return dict([alias_s.split("=") for alias_s in args.alias])


async def metrics(args: argparse.Namespace):
    metrics_received: set[str] = set()  # set of macs
    exporters: list[Exporter] = []

    if args.graphite_url:
        exporters.append(Graphite(args.graphite_url, os.getenv("METRICS_USER"), os.getenv("METRICS_PASSWORD")))

    if args.prometheus_url:
        exporters.append(PushGateway(args.prometheus_url, os.getenv("METRICS_USER"), os.getenv("METRICS_PASSWORD")))

    async def export_metrics(miflora: MiFlora):
        firmwarebattery = await miflora.read_firmware_battery()
        await asyncio.wait([asyncio.create_task(exporter.send_battery(firmwarebattery)) for exporter in exporters])

        sensordata = await miflora.read_sensor()
        print(dataclasses.asdict(sensordata))
        await miflora.disconnect()
        metrics_received.add(miflora.address)
        await asyncio.wait([asyncio.create_task(exporter.send_sensor(sensordata)) for exporter in exporters])

    mifloramanager = MiFloraManager(_get_alias_mapping(args))
    await mifloramanager.setup_adapter()
    try:
        async with asyncio.timeout(args.timeout):
            async for miflora in mifloramanager.scan_mifloras():
                log.debug(f"Added {miflora}")
                print(miflora)
                if miflora.address in metrics_received:
                    log.info(f"Not connecting to {miflora.address} (metric already collected)")
                elif await miflora.connect():
                    while not miflora._services_discovered:
                        await asyncio.sleep(1)  # FIXME: Polling, use Condition variable?
                        log.debug("Waiting for services")
                    log.debug("Service discovered")
                    await export_metrics(miflora)
    except TimeoutError:
        print(f"Received data from {len(metrics_received)} MiFloras!")


async def blink(args: argparse.Namespace):
    blinked: set[str] = set()  # set of macs
    alias_mapping = _get_alias_mapping(args)
    mifloramanager = MiFloraManager(_get_alias_mapping(args))
    await mifloramanager.setup_adapter()
    try:
        async with asyncio.timeout(args.timeout):
            async for miflora in mifloramanager.scan_mifloras():
                if miflora.address in blinked:
                    log.debug(f"Already blinked {miflora}")
                elif await miflora.connect():
                    while not miflora._services_discovered:
                        await asyncio.sleep(1)  # FIXME: Polling, use Condition variable?
                        log.debug(f"Waiting for services on {miflora}")
                    await miflora.blink()
                    log.debug(f"Blinked {miflora}")
                    blinked.add(miflora.address)
    except TimeoutError:
        print(f"Blinked {len(blinked)} MiFloras!")


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
    policy = gi.events.GLibEventLoopPolicy()
    mainloop = policy.get_event_loop()

    try:
        mainloop.run_until_complete(blink(args) if args.command == "blink" else metrics(args))

    except KeyboardInterrupt:
        ...
