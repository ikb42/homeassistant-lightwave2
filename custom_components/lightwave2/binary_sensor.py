import logging
from .const import LIGHTWAVE_LINK2, LIGHTWAVE_ENTITIES, CONF_HOMEKIT
try:
    from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
except ImportError:
    from homeassistant.components.binary_sensor import BinarySensorDevice as BinarySensorEntity
from homeassistant.components.binary_sensor import (DEVICE_CLASS_WINDOW, DEVICE_CLASS_PLUG, DEVICE_CLASS_MOTION)
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN

DEPENDENCIES = ['lightwave2']
_LOGGER = logging.getLogger(__name__)

#TODO, refactor this in the style of the sensors module
SENSORS = [
    BinarySensorEntityDescription(
        key="windowPosition",
        device_class=DEVICE_CLASS_WINDOW,
        name="Window Position",
    ),
    BinarySensorEntityDescription(
        key="outletInUse",
        device_class=DEVICE_CLASS_PLUG,
        name="Socket In Use",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="movement",
        device_class=DEVICE_CLASS_MOTION,
        name="Movement",
    )
]

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Find and return LightWave sensors."""

    sensors = []
    link = hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_LINK2]

    homekit = config_entry.options.get(CONF_HOMEKIT, False)

    #for featureset_id, featureset in link.featuresets.items():
    #    for description in SENSORS:
    #        if featureset.has_feature(description.key):
    #            sensors.append(LWRF2BinarySensor(featureset.name, featureset_id, link, description, hass, homekit))

    for featureset_id, name in link.get_windowsensors():
        sensors.append(LWRF2BinarySensor(name, featureset_id, link, hass, homekit))

    for featureset_id, name in link.get_switches():
        if link.featuresets[featureset_id].has_feature('outletInUse'):
            sensors.append(LWRF2SocketBinarySensor(name, featureset_id, link, hass, homekit))

    for featureset_id, name in link.get_motionsensors():
        sensors.append(LWRF2MovementBinarySensor(name, featureset_id, link))

    hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_ENTITIES].extend(sensors)
    async_add_entities(sensors)

class LWRF2BinarySensor(BinarySensorEntity):
    """Representation of a LightWaveRF window sensor."""

    def __init__(self, name, featureset_id, link, hass, homekit):
        self._name = name
        self._hass = hass
        _LOGGER.debug("Adding sensor: %s ", self._name)
        self._featureset_id = featureset_id
        self._lwlink = link
        self._homekit = homekit
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["windowPosition"].state
        self._gen2 = self._lwlink.featuresets[self._featureset_id].is_gen2()
        for featureset_id, hubname in link.get_hubs():
            self._linkid = featureset_id

    async def async_added_to_hass(self):
        """Subscribe to events."""
        await self._lwlink.async_register_callback(self.async_update_callback)
        registry = er.async_get(self._hass)
        entity_entry = registry.async_get(self.entity_id)
        if self._homekit:
            if entity_entry is not None and not entity_entry.hidden:
                registry.async_update_entity(
                    self.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
                )
        else:
            if entity_entry.hidden_by == er.RegistryEntryHider.INTEGRATION:
                registry.async_update_entity(self.entity_id, hidden_by=None)

    @callback
    def async_update_callback(self, **kwargs):
        """Update the component's state."""
        self.async_schedule_update_ha_state(True)

    @property
    def should_poll(self):
        """Lightwave2 library will push state, no polling needed"""
        return False

    @property
    def assumed_state(self):
        """Gen 2 devices will report state changes, gen 1 doesn't"""
        return not self._gen2

    async def async_update(self):
        """Update state"""
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["windowPosition"].state

    @property
    def name(self):
        """Lightwave switch name."""
        return self._name

    @property
    def unique_id(self):
        """Unique identifier. Provided by hub."""
        return self._featureset_id

    @property
    def is_on(self):
        """Lightwave switch is on state."""
        return self._state

    @property
    def device_class(self):
        return DEVICE_CLASS_WINDOW

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""

        attribs = {}

        for featurename, feature in self._lwlink.featuresets[self._featureset_id].features.items():
            attribs['lwrf_' + featurename] = feature.state

        attribs['lrwf_product_code'] = self._lwlink.featuresets[self._featureset_id].product_code

        return attribs

    @property
    def device_info(self):
        return {
            'identifiers': {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._featureset_id)
            },
            'name': self.name,
            'manufacturer': "Lightwave RF",
            'model': self._lwlink.featuresets[self._featureset_id].product_code,
            'via_device': (DOMAIN, self._linkid)
        }

class LWRF2SocketBinarySensor(BinarySensorEntity):
    """Representation of a LightWaveRF socket sensor."""

    def __init__(self, name, featureset_id, link, hass, homekit):
        self._name = f"{name} Plug Sensor"
        self._hass = hass
        _LOGGER.debug("Adding sensor: %s ", self._name)
        self._featureset_id = featureset_id
        self._lwlink = link
        self._homekit = homekit
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["outletInUse"].state
        for featureset_id, hubname in link.get_hubs():
            self._linkid = featureset_id

    async def async_added_to_hass(self):
        """Subscribe to events."""
        await self._lwlink.async_register_callback(self.async_update_callback)
        registry = er.async_get(self._hass)
        entity_entry = registry.async_get(self.entity_id)
        if self._homekit and self._gen2:
            if entity_entry is not None and not entity_entry.hidden:
                registry.async_update_entity(
                    self.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
                )
        else:
            if entity_entry.hidden_by == er.RegistryEntryHider.INTEGRATION:
                registry.async_update_entity(self.entity_id, hidden_by=None)

    @callback
    def async_update_callback(self, **kwargs):
        """Update the component's state."""
        self.async_schedule_update_ha_state(True)

    @property
    def should_poll(self):
        """Lightwave2 library will push state, no polling needed"""
        return False

    @property
    def assumed_state(self):
        return False

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    async def async_update(self):
        """Update state"""
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["outletInUse"].state

    @property
    def name(self):
        """Lightwave switch name."""
        return self._name

    @property
    def unique_id(self):
        """Unique identifier. Provided by hub."""
        return self._featureset_id

    @property
    def is_on(self):
        """Lightwave switch is on state."""
        return self._state

    @property
    def device_class(self):
        return DEVICE_CLASS_PLUG

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""

        attribs = {}

        for featurename, feature in self._lwlink.featuresets[self._featureset_id].features.items():
            attribs['lwrf_' + featurename] = feature.state

        attribs['lrwf_product_code'] = self._lwlink.featuresets[self._featureset_id].product_code

        return attribs

    @property
    def device_info(self):
        return {
            'identifiers': { (DOMAIN, self._featureset_id)},
            'name': self.name,
            'manufacturer': "Lightwave RF",
            'model': self._lwlink.featuresets[self._featureset_id].product_code,
            'via_device': (DOMAIN, self._linkid)
        }

class LWRF2MovementBinarySensor(BinarySensorEntity):
    """Representation of a LightWaveRF movement sensor."""

    def __init__(self, name, featureset_id, link):
        self._name = f"{name} Movement Sensor"
        _LOGGER.debug("Adding sensor: %s ", self._name)
        self._featureset_id = featureset_id
        self._lwlink = link
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["movement"].state
        for featureset_id, hubname in link.get_hubs():
            self._linkid = featureset_id

    async def async_added_to_hass(self):
        """Subscribe to events."""
        await self._lwlink.async_register_callback(self.async_update_callback)

    @callback
    def async_update_callback(self, **kwargs):
        """Update the component's state."""
        self.async_schedule_update_ha_state(True)

    @property
    def should_poll(self):
        """Lightwave2 library will push state, no polling needed"""
        return False

    @property
    def assumed_state(self):
        return False

    async def async_update(self):
        """Update state"""
        self._state = \
            self._lwlink.featuresets[self._featureset_id].features["movement"].state

    @property
    def name(self):
        """Lightwave switch name."""
        return self._name

    @property
    def unique_id(self):
        """Unique identifier. Provided by hub."""
        return self._featureset_id

    @property
    def is_on(self):
        """Lightwave switch is on state."""
        return self._state

    @property
    def device_class(self):
        return DEVICE_CLASS_MOTION

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""

        attribs = {}

        for featurename, feature in self._lwlink.featuresets[self._featureset_id].features.items():
            attribs['lwrf_' + featurename] = feature.state

        attribs['lrwf_product_code'] = self._lwlink.featuresets[self._featureset_id].product_code

        return attribs

    @property
    def device_info(self):
        return {
            'identifiers': { (DOMAIN, self._featureset_id)},
            'name': self.name,
            'manufacturer': "Lightwave RF",
            'model': self._lwlink.featuresets[self._featureset_id].product_code,
            'via_device': (DOMAIN, self._linkid)
        }