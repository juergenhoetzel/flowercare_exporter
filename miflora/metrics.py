from dataclasses import dataclass
from typing import Protocol


@dataclass
class MiFloraSensor:
    temperature_celsius: float
    brightness_lux: float
    soil_conductivity: float
    soil_moisture_percent: float
    alias: str
    address: str
    rssi: float


@dataclass
class MiFloraFirmwareBattery:
    battery_percent: float
    firmware: str
    alias: str
    address: str
    rssi: float


class Exporter(Protocol):
    async def send_sensor(self, sensordata: MiFloraSensor) -> None: ...
    async def send_battery(self, fb: MiFloraFirmwareBattery) -> None: ...
