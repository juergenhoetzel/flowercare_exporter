import asyncio
import logging
from typing import Any, AsyncIterator, Callable

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
    _connect_lock = asyncio.Lock()  # prevent concurrent connections
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
        self._services_discovered = False

    async def connect(self) -> bool:
        "Connect async to the device"
        async with MiFlora._connect_lock:
            try:
                log.debug(f"Starting connection to  {self}")
                await self._device_proxy.call(
                    "Connect",
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                )
                return True
            except GLib.Error as err:
                log.warning(f"Failed to connect to {self}: {err}")
            return False

    async def disconnect(self):
        await self._device_proxy.call(
            "Disconnect",  # type: ignore
            None,
            Gio.DBusCallFlags.NONE,
            -1,
        )

    async def blink(self):
        self._device_mode_proxy.call(  # type: ignore
            "WriteValue",
            GLib.Variant(
                "(aya{sv})",
                (
                    b"\xfd\xff",
                    {},
                ),
            ),
            Gio.DBusCallFlags.NONE,
            -1,
        )

    async def read_firmware_battery(
        self,
    ):
        val = (
            await self._firmware_battery_proxy.call(
                "ReadValue",  # type: ignore
                GLib.Variant("(a{sv})", ({},)),
                Gio.DBusCallFlags.NONE,
                -1,
            )
        ).unpack()[0]
        batt = val[0]
        firmware = bytes(val[2:]).decode()
        return MiFloraFirmwareBattery(batt, firmware, self.alias, self.address, self.rssi)

    async def read_sensor(self) -> MiFloraSensor:
        await self._device_mode_proxy.call(
            "WriteValue",  # type: ignore
            GLib.Variant(
                "(aya{sv})",
                (
                    b"\xa0\x1f",
                    dict(),
                ),
            ),
            Gio.DBusCallFlags.NONE,
            -1,
        )
        log.info(f"Changed to real time data mode: {self.alias}")
        val = (
            await self._sensor_proxy.call(
                "ReadValue",  # type: ignore
                GLib.Variant("(a{sv})", (dict(),)),
                Gio.DBusCallFlags.NONE,
                -1,
            )
        ).unpack()[0]

        log.debug(f"Got bytes from {self.alias} real time data: {bytes(val).hex()} ")
        if len(val) == 16:
            temp_celsius = int.from_bytes(val[0:2], byteorder="little") / 10
            brightness_lux = int.from_bytes(val[3:7], byteorder="little")
            moisture_percent = val[7]
            soil_conductivity_µS_cm = int.from_bytes(val[8:10], byteorder="little")
            return MiFloraSensor(
                temp_celsius,
                brightness_lux,
                soil_conductivity_μS_cm,
                moisture_percent,
                self.alias,
                self.address,
                self.rssi,
            )
        else:
            raise ValueError(f"Invalid data length {len(val)} read from {self.alias}: {bytes(val).hex()}")


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
        removed_cb: Callable[[MiFlora], None] = lambda miflora: log.debug(f"{miflora} vanished"),
    ):
        self.alias_mapping = alias_mapping
        self._removed_cb = removed_cb
        self._bluez_object_manager = Gio.DBusObjectManagerClient.new_for_bus_sync(
            Gio.BusType.SYSTEM, Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START, "org.bluez", "/", None, None, None
        )
        self._bluez_object_manager.connect("interface-proxy-properties-changed", self._properties_changed)
        self._bluez_object_manager.connect("object-removed", self._object_removed)

    async def setup_adapter(self):
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
        try:
            await adapter_proxy.call(  # type: ignore
                "SetDiscoveryFilter",
                GLib.Variant(
                    "(a{sv})",
                    ({"UUIDs": GLib.Variant("as", [MIFLORA_UUID]), "Pattern": GLib.Variant.new_string("C4:7C:8D")},),
                ),
                Gio.DBusCallFlags.NONE,
                -1,
            )
        except GLib.Error as err:
            raise RuntimeError(f"Failed to SetDiscoveryFilter: {err}")
        self._adapter_proxy = adapter_proxy
        self._adapter_props_proxy = adapter.get_interface("org.freedesktop.DBus.Properties")
        await self.set_discovery()

    async def scan_mifloras(self) -> AsyncIterator[MiFlora]:
        queue = asyncio.Queue()

        def _object_added(__ignored_client__, dbus_object: Gio.DBusObject):
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
                    queue.put_nowait(miflora)
            elif dbus_object.get_interface("org.bluez.GattCharacteristic1"):
                self._gatt_handler(dbus_object)

        self._bluez_object_manager.connect("object-added", _object_added)
        while miflora := await queue.get():
            yield miflora

    async def set_discovery(self, on=True) -> bool:
        method = "StartDiscovery" if on else "StopDiscovery"
        try:
            await self._adapter_proxy.call(method, None, Gio.DBusCallFlags.NO_AUTO_START, 500)  # type: ignore
            return True
        except GLib.Error as error:
            log.warning(f"{method}: {error.message}")
        return False

    def _object_removed(self, __client__, dbus_object: Gio.DBusObject):
        object_path = dbus_object.get_object_path()
        if miflora := self.mifloras.get("object_path"):
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
                miflora._services_discovered = True  # FIXME: Use condition variable?
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
