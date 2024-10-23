import logging

import gi

gi.require_version("Soup", "3.0")

from gi.repository import Gio, GLib, Soup  # type: ignore

log = logging.getLogger(__name__)


from miflora.metrics import MiFloraFirmwareBattery, MiFloraSensor


def _firmware_battery_to_prometheus(fb: MiFloraFirmwareBattery) -> str:
    return f"""
    # TYPE miflora_battery gauge
    # help remaining Battery in percent
    miflora_battery_percent{{alias="{fb.alias}", address="{fb.address}", firmware="{fb.firmware}"}} {fb.battery_percent}
    """


def _sensor_to_prometheus(miflora: MiFloraSensor) -> str:
    return f"""
    # TYPE miflora_temperature_celsius gauge
    # HELP Temperature in ℃"
    miflora_temperature_celsius{{alias="{miflora.alias}", address="{miflora.address}"}} {miflora.temperature_celsius}
    # TYPE miflora_brightness_lux gauge
    # help Brightness in Lux
    miflora_brightness_lux{{alias="{miflora.alias}", address="{miflora.address}"}} {miflora.brightness_lux}
    # TYPE miflora_soil_conductivity gauge
    # help Soil Electrical Conductivity
    miflora_soil_conductivity{{alias="{miflora.alias}", address="{miflora.address}"}} {miflora.soil_conductivity}
    # TYPE miflora_soil_moisture_percent gauge
    # help Soil Moisture
    miflora_soil_moisture_percent{{alias="{miflora.alias}", address="{miflora.address}"}} {miflora.soil_moisture_percent}
    # TYPE miflora_rssi gauge
    # help Received Signal Strength Indicator
    miflora_rssi{{alias="{miflora.alias}", address="{miflora.address}"}} {miflora.rssi}
    """


class PushGateway:
    url: str
    user: str | None
    password: str | None
    _session: Soup.Session

    def __init__(self, url: str, user: str | None = None, password: str | None = None):
        self.url = url
        self.user = user
        self.password = password
        self._session = Soup.Session()

        if log.getEffectiveLevel() == logging.DEBUG:
            logger = Soup.Logger.new(Soup.LoggerLogLevel.BODY)
            self._session.add_feature(logger)

    def send_sensor(self, sensordata: MiFloraSensor):
        body = _sensor_to_prometheus(sensordata)
        self._send_message(body)

    def send_battery(self, fb: MiFloraFirmwareBattery):
        body = _firmware_battery_to_prometheus(fb)
        self._send_message(body)

    def _send_message(self, body: str):
        uri = GLib.Uri.parse(self.url, GLib.UriFlags.NONE)
        message = Soup.Message.new_from_uri("POST", uri)
        if self.user and self.password:
            assert self._session
            auth_manager = self._session.get_feature(Soup.AuthManager)
            assert auth_manager
            auth = Soup.Auth.new(Soup.AuthBasic, message, "Basic")
            assert auth
            auth.authenticate(self.user, self.password)
            auth_manager.use_auth(message.get_uri(), auth)  # type: ignore

        message.set_request_body_from_bytes("application/x-www-form-urlencoded", GLib.Bytes.new(body.encode()))

        def response(session: Soup.Session, aresult: Gio.Task):
            session.send_and_read_finish(aresult)
            message = session.get_async_result_message(aresult)
            assert message
            status = message.get_status()
            if status != Soup.Status.OK:
                print(f"Error Posting to '{self.url}': {Soup.Status.get_phrase(status)}")
                return

        self._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, callback=response)
