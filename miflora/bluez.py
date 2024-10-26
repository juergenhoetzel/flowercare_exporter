import logging
from threading import Lock
from typing import Any, Callable

import gi

from .metrics import MiFloraFirmwareBattery, MiFloraSensor

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # type: ignore


def log_gerror_handler(log_message: str):
    error_handler: Callable[[Any, GLib.Error, Any], None] = lambda _obj, error, user_data: log.warning(
        f"{log_message}: {error.message}"
    )
    return error_handler


MIFLORA_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"  # FIXME: Duplicated
MIFLORA_SERVICE_HISTORY_UUID = "00001206-0000-1000-8000-00805f9b34fb"  # unused

log = logging.getLogger(__name__)


def _error_handler(prefix: str):
    return lambda __obj__, error, __userdata__: log.error(f"{prefix} error: {error.message}")


class MiFlora:
    # internal
    _connect_lock = Lock()  # prevent concurrent connections
    _connect_device: str | None = None  # current connect device mac for debugging
    _device_proxy: Gio.DBusInterface
    _firmware_battery_proxy: Gio.DBusInterface
    _device_mode_proxy: Gio.DBusInterface
    _sensor_proxy: Gio.DBusInterface
    on_services_disovered: Callable[["MiFlora"], None] = lambda miflora: log.debug(
        f"Miflora {miflora.address} services discovered"
    )

    def __str__(self):
        return f"{self.alias} ({self.address})"

    def __init__(self, object_path, alias, address, rssi):
        self.object_path = object_path
        self.alias = alias
        self.address = address
        self.rssi = rssi

    def connect(self):
        "Connect async to the device"

        def connect_failed_cb(*args):
            if MiFlora._connect_device == self.address:  # Device might be vanished meanwhile
                MiFlora._connect_lock.release()
                MiFlora._connect_device = ""
            log_gerror_handler(f" Connect {self}")(*args)

        if MiFlora._connect_lock.acquire(blocking=False):
            log.debug(f"Starting connection to  {self}")
            MiFlora._connect_device = self.address
            self._device_proxy.Connect(
                result_handler=lambda *__args__: log.info(f"{self} connected"),
                error_handler=connect_failed_cb,
                timeout=20 * 1000,  # FIXME: Hardcoded 20 seconds
            )  # type: ignore
        else:
            log.warning(
                f"Connection to {MiFlora._connect_device} in progress, waiting for 5 before retry connect to {self}"
            )
            GLib.timeout_add_seconds(5, self.connect)
        return False  # FIXME: Just use timeout_add_once?

    def disconnect(self):
        def disconnected(*__args__):
            log.debug(f"Disconnected {self.alias}, Releasing lock")
            MiFlora._connect_lock.release()
            MiFlora._connect_device = None

        self._device_proxy.Disconnect(  # type: ignore
            result_handler=disconnected,
            error_handler=_error_handler("Disconnect"),
        )

    def blink(self):
        log.info(f"About to blink: {self.alias}")
        self._device_mode_proxy.WriteValue(  # type: ignore
            "(aya{sv})",
            b"\xfd\xff",
            dict(),
            result_handler=lambda object, _result, _userdata: log.debug("Blinked!"),
            error_handler=_error_handler("Blink"),
        )

    def read_firmware_battery(
        self,
        callback: Callable[["MiFlora", MiFloraFirmwareBattery], None] = lambda _, fb: log.debug(f"Got {fb}"),
    ):
        def _firmware_battery_callback(val):
            batt = val[0]
            firmware = bytes(val[2:]).decode()
            callback(self, MiFloraFirmwareBattery(batt, firmware, self.alias, self.address, self.rssi))

        self._firmware_battery_proxy.ReadValue(  # type: ignore
            "(a{sv})",
            dict(),
            result_handler=lambda _obj, val, _userdata: _firmware_battery_callback(val),
            error_handler=_error_handler("Firmware battery read value"),
        )

    def read_sensor(
        self,
        callback: Callable[["MiFlora", MiFloraSensor], None] = lambda _, sensor_data: log.debug(f"{sensor_data}"),
    ):
        def _sensor_read_callback(val):
            log.debug(f"Got bytes from {self.alias} real time data: {bytes(val).hex()} ")
            if len(val) == 16:
                temp_celsius = int.from_bytes(val[0:2], byteorder="little") / 10
                brightness_lux = int.from_bytes(val[3:7], byteorder="little")
                moisture_percent = val[7]
                soil_conductivity_µS_cm = int.from_bytes(val[8:10], byteorder="little")
                sensor_data = MiFloraSensor(
                    temp_celsius,
                    brightness_lux,
                    soil_conductivity_μS_cm,
                    moisture_percent,
                    self.alias,
                    self.address,
                    self.rssi,
                )
                callback(self, sensor_data)
            else:
                raise ValueError(f"Invalid data length {len(val)} read from {self.alias}: {bytes(val).hex()}")

        def _data_mode_changed_callback():
            log.info(f"Changing to real time data mode: {self.alias}")
            self._sensor_proxy.ReadValue(  # type: ignore
                "(a{sv})",
                dict(),
                result_handler=lambda _obj, result, _userdata: _sensor_read_callback(result),
                error_handler=_error_handler("Sensor read value"),
            )

        self._device_mode_proxy.WriteValue(  # type: ignore
            "(aya{sv})",
            b"\xa0\x1f",
            dict(),
            result_handler=lambda _obj, _result, _userdata: _data_mode_changed_callback(),
            error_handler=_error_handler("Changing Sensor mode"),
        )


class MiFloraManager:
    mifloras: dict[str, MiFlora] = dict()

    def _get_matching_miflora(self, dbus_object) -> MiFlora | None:
        found_list = [
            self.mifloras[device_object_path]
            for device_object_path in self.mifloras.keys()
            if dbus_object.get_object_path().startswith(device_object_path)
        ]
        if found_list:
            return found_list[0]

    def __init__(
        self,
        alias_mapping: dict[str, str],
        added_cb: Callable[[MiFlora], None],
        removed_cb: Callable[[MiFlora], None] = lambda miflora: log.debug(f"{miflora} vanished"),
    ):
        self.alias_mapping = alias_mapping
        self._added_cb = added_cb
        self._removed_cb = removed_cb
        self._bluez_object_manager = Gio.DBusObjectManagerClient.new_for_bus_sync(
            Gio.BusType.SYSTEM, Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START, "org.bluez", "/", None, None, None
        )
        self._bluez_object_manager.connect("object-added", self._object_added)
        self._bluez_object_manager.connect("interface-proxy-properties-changed", self._properties_changed)
        self._bluez_object_manager.connect("object-removed", self._object_removed)

    def setup_adapter(self):
        adapters = [
            object for object in self._bluez_object_manager.get_objects() if object.get_interface("org.bluez.Adapter1")
        ]
        if not adapters:
            raise RuntimeError("No bluez adapters found! start bluetooth service!")
        adapter = adapters[0]
        adapter_proxy = adapter.get_interface("org.bluez.Adapter1")
        adapter_props_proxy = adapter.get_interface("org.freedesktop.DBus.Properties")
        if not (adapter_proxy and adapter_props_proxy):
            raise RuntimeError("No usable bluez adapters found!")
        # Ensure Adapter is powered on
        adapter_props_proxy.Set(  # type: ignore
            "(ssv)", "org.bluez.Adapter1", "Powered", GLib.Variant.new_boolean(True)
        )
        log.debug(f"Using adapter {adapter.get_object_path()}")
        # Turn on MiFlora scanning
        adapter_proxy.SetDiscoveryFilter(  # type: ignore
            "(a{sv})", {"UUIDs": GLib.Variant("as", [MIFLORA_UUID]), "Pattern": GLib.Variant.new_string("C4:7C:8D")}
        )  # type: ignore
        self._adapter_proxy = adapter_proxy
        self._adapter_props_proxy = adapter.get_interface("org.freedesktop.DBus.Properties")
        self.start_discovery()

    def start_discovery(self):
        assert self._adapter_proxy

        def error_handler(_obj, error: GLib.Error, _userdata):
            if not (
                Gio.DBusError.is_remote_error(error)
                and Gio.DBusError.get_remote_error(error) == "org.bluez.Error.InProgress"
            ):
                log.warn(f"Error: {error.message}")

        self._adapter_proxy.StartDiscovery(  # type: ignore
            error_handler=error_handler, result_handler=lambda *args: log.debug("Scan enabled")
        )
        # result_handler=log_gerror_handler("StartDiscovery failed")

    def stop_discovery(self, stopped_cb: Callable[[], None]):
        """Async stop Discovery, ignoring all errors"""
        assert self._adapter_proxy
        self._adapter_proxy.StopDiscovery(  # type: ignore
            result_handler=lambda *args: stopped_cb()
        )

    def _object_removed(self, __client__, dbus_object: Gio.DBusObject):
        object_path = dbus_object.get_object_path()
        if miflora := self.mifloras.get("object_path"):
            if miflora._connect_device == miflora.address:
                log.debug(f"{miflora} vanished: Releasing lock (connect will never succeed)")
                MiFlora._connect_device = None
                MiFlora._connect_lock.release()
            self._removed_cb(miflora)
            del self.mifloras[object_path]

    def _gatt_handler(self, dbus_object):
        if miflora := self._get_matching_miflora(dbus_object):
            props_proxy = dbus_object.get_interface("org.freedesktop.DBus.Properties")
            gatt_char_proxy = dbus_object.get_interface("org.bluez.GattCharacteristic1")
            uuid = props_proxy.Get("(ss)", "org.bluez.GattCharacteristic1", "UUID")
            match uuid:
                case "00001a02-0000-1000-8000-00805f9b34fb":  # FIRMWARE_BATTERY
                    miflora._firmware_battery_proxy = gatt_char_proxy
                case "00001a00-0000-1000-8000-00805f9b34fb":  # DEVICE_MODE
                    miflora._device_mode_proxy = gatt_char_proxy
                case "00001a01-0000-1000-8000-00805f9b34fb":  # SENSORS
                    miflora._sensor_proxy = gatt_char_proxy

    def _object_added(self, _ignored_client, dbus_object: Gio.DBusObject):
        object_path = dbus_object.get_object_path()
        if (
            dbus_object.get_interface("org.bluez.Device1")
            and (props_proxy := dbus_object.get_interface("org.freedesktop.DBus.Properties"))
            and (device_proxy := dbus_object.get_interface("org.bluez.Device1"))
        ):
            uuids = props_proxy.Get(  # type: ignore
                "(ss)",
                "org.bluez.Device1",
                "UUIDs",
            )

            if MIFLORA_UUID in uuids:
                address = props_proxy.Get("(ss)", "org.bluez.Device1", "Address")  # type: ignore
                log.info(f"Miflora found :{address}")
                if alias := self.alias_mapping.get(address):
                    props_proxy.Set("(ssv)", "org.bluez.Device1", "Alias", GLib.Variant.new_string(alias))  # type: ignore
                else:
                    alias = props_proxy.Get("(ss)", "org.bluez.Device1", "Alias")  # type: ignore
                rssi = props_proxy.Get("(ss)", "org.bluez.Device1", "RSSI")  # type: ignore
                miflora = MiFlora(object_path, alias, address, rssi)
                self.mifloras[object_path] = MiFlora(object_path, alias, address, rssi)
                miflora._device_proxy = device_proxy
                self.mifloras[object_path] = miflora
                self._added_cb(miflora)

        elif dbus_object.get_interface("org.bluez.GattCharacteristic1"):
            self._gatt_handler(dbus_object)

    def _properties_changed(
        self,
        _client,
        dbus_object: Gio.DBusObject,
        _interface_proxy,
        changed_properties_variant,
        _invalidated_properties_variant,
    ):
        object_path = dbus_object.get_object_path()
        changed_properties = changed_properties_variant.unpack()
        # wait for resolved services
        if miflora := self._get_matching_miflora(dbus_object):
            if changed_properties.get("ServicesResolved"):
                log.debug(f"Services discovered: {object_path}, changed properties {changed_properties}")
                miflora.on_services_disovered(miflora)
        elif MIFLORA_UUID in changed_properties.get("ServiceData", {}):
            log.debug(
                f"Removing already discovered MiFlora {object_path} (previous session?), to rediscover services"
            )  # New devices are found using _object_added callback
            (self)._adapter_proxy.RemoveDevice(  # type: ignore
                "(o)",
                object_path,
                result_handler=lambda __obj__, __res__, __user__: log.debug(f"Removed {object_path}"),
                error_handler=_error_handler("Remove Device"),
            )
