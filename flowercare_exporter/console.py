import json
from dataclasses import asdict

from miflora.metrics import MiFloraFirmwareBattery, MiFloraSensor


class Console:
    async def send_battery(self, fb: MiFloraFirmwareBattery):
        print(json.dumps(asdict(fb)))

    async def send_sensor(self, sensordata: MiFloraSensor):
        print(json.dumps(asdict(sensordata)))
