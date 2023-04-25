import logging

from .const import DOMAIN, CONF_PUBLICAPI, CONF_DEBUG, CONF_RECONNECT, LIGHTWAVE_LINK2,  LIGHTWAVE_ENTITIES, \
    LIGHTWAVE_WEBHOOK, LIGHTWAVE_WEBHOOKID, LIGHTWAVE_LINKID, SERVICE_RECONNECT, SERVICE_WHDELETE, SERVICE_UPDATE
from homeassistant.const import (CONF_USERNAME, CONF_PASSWORD)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

async def handle_webhook(hass, webhook_id, request):
    """Handle webhook callback."""
    for entry_id in hass.data[DOMAIN]:
        link = hass.data[DOMAIN][entry_id][LIGHTWAVE_LINK2]
        body = await request.json()
        _LOGGER.debug("Received webhook: %s ", body)
        link.process_webhook_received(body)
        for ent in hass.data[DOMAIN][entry_id][LIGHTWAVE_ENTITIES]:
            if ent.hass is not None:
                ent.async_schedule_update_ha_state(True)

def async_central_callback(**kwargs):
    _LOGGER.debug("Central callback")

async def async_setup(hass, config):

    async def service_handle_reconnect(call):
        _LOGGER.debug("Received service call reconnect")
        for entry_id in hass.data[DOMAIN]:
            link = hass.data[DOMAIN][entry_id][LIGHTWAVE_LINK2]
            if link._websocket is not None:
                await link._websocket.close()

    async def service_handle_update_states(call):
        _LOGGER.debug("Received service call update states")
        for entry_id in hass.data[DOMAIN]:
            link = hass.data[DOMAIN][entry_id][LIGHTWAVE_LINK2]
            await link.async_update_featureset_states()
            for ent in hass.data[DOMAIN][entry_id][LIGHTWAVE_ENTITIES]:
                if ent.hass is not None:
                    ent.async_schedule_update_ha_state(True)

    async def service_handle_delete_webhook(call):
        _LOGGER.debug("Received service call delete webhook")
        wh_name = call.data.get("webhookid")
        for entry_id in hass.data[DOMAIN]:
            link = hass.data[DOMAIN][entry_id][LIGHTWAVE_LINK2]
            await link.async_delete_webhook(wh_name)

    hass.services.async_register(DOMAIN, SERVICE_RECONNECT, service_handle_reconnect)
    hass.services.async_register(DOMAIN, SERVICE_WHDELETE, service_handle_delete_webhook)
    hass.services.async_register(DOMAIN, SERVICE_UPDATE, service_handle_update_states)
    
    return True

async def async_setup_entry(hass, config_entry):

    import sys, os
    dir_path = os.path.dirname(os.path.realpath(__file__))
    sys.path.append(dir_path)

    from lightwave2 import lightwave2

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(config_entry.entry_id, {})
    email = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]
    config_entry.add_update_listener(reload_lw)

    publicapi = config_entry.options.get(CONF_PUBLICAPI, False)
    if publicapi:
        _LOGGER.warning("Using Public API, this is experimental - if you have issues turn this off in the integration options")
        link = lightwave2.LWLink2Public(email, password)
    else:
        link = lightwave2.LWLink2(email, password)

    debugmode = config_entry.options.get(CONF_DEBUG, False)

    if debugmode:
        _LOGGER.warning("Logging turned on")
        _LOGGER.setLevel(logging.DEBUG)
        logging.getLogger("lightwave2").setLevel(logging.DEBUG)

    force_reconnect_secs = config_entry.options.get(CONF_RECONNECT, False)
    _LOGGER.debug("Forced reconnection setting: %s ", force_reconnect_secs)

    if not await link.async_connect(max_tries = 1, force_keep_alive_secs=force_reconnect_secs):
        return False
    await link.async_get_hierarchy()

    hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_LINK2] = link
    hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_ENTITIES] = []
    if not publicapi:
        url = None
        _LOGGER.debug("Register central callback")
        await link.async_register_callback(async_central_callback)
    else:
        webhook_id = hass.components.webhook.async_generate_id()
        hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_WEBHOOKID] = webhook_id
        _LOGGER.debug("Generated webhook: %s ", webhook_id)
        hass.components.webhook.async_register(
            'lightwave2', 'Lightwave webhook', webhook_id, handle_webhook)
        url = hass.components.webhook.async_generate_url(webhook_id)
        _LOGGER.debug("Webhook URL: %s ", url)
        await link.async_register_webhook_all(url, LIGHTWAVE_WEBHOOK, overwrite = True)

    hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_WEBHOOK] = url

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    for featureset_id, hubname in link.get_hubs():
        device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            configuration_url = "https://my.lightwaverf.com/a/login",
            entry_type=dr.DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, featureset_id)},
            manufacturer= "Lightwave RF",
            name=hubname,
            model=link.featuresets[featureset_id].product_code
        )
        hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_LINKID] = featureset_id

    # Ensure every device associated with this config entry still exists
    # otherwise remove the device (and thus entities).
    for device_entry in dr.async_entries_for_config_entry(
        device_registry, config_entry.entry_id
    ):
        for identifier in device_entry.identifiers:
            _LOGGER.debug("Identifier found in Home Assistant device registry %s ", identifier[1])
            if identifier[1] in link.featuresets:
                _LOGGER.debug("Identifier exists in Lightwave config")
                break
        else:
            _LOGGER.debug("Identifier does not exist in Lightwave config, removing device")
            device_registry.async_remove_device(device_entry.id)
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, config_entry.entry_id
    ):
        _LOGGER.debug("Entity registry item %s", entity_entry)
        _LOGGER.debug("Entity gen2 %s", entity_registry.async_get(entity_entry.entity_id))

    forward_setup = hass.config_entries.async_forward_entry_setup
    hass.async_create_task(forward_setup(config_entry, "switch"))
    hass.async_create_task(forward_setup(config_entry, "light"))
    hass.async_create_task(forward_setup(config_entry, "climate"))
    hass.async_create_task(forward_setup(config_entry, "cover"))
    hass.async_create_task(forward_setup(config_entry, "binary_sensor"))
    hass.async_create_task(forward_setup(config_entry, "sensor"))
    hass.async_create_task(forward_setup(config_entry, "lock"))

    return True

async def async_remove_entry(hass, config_entry):
    if hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_WEBHOOK] is not None:
        hass.components.webhook.async_unregister(hass.data[DOMAIN][config_entry.entry_id][LIGHTWAVE_WEBHOOKID])
    await hass.config_entries.async_forward_entry_unload(config_entry, "switch")
    await hass.config_entries.async_forward_entry_unload(config_entry, "light")
    await hass.config_entries.async_forward_entry_unload(config_entry, "climate")
    await hass.config_entries.async_forward_entry_unload(config_entry, "cover")
    await hass.config_entries.async_forward_entry_unload(config_entry, "binary_sensor")
    await hass.config_entries.async_forward_entry_unload(config_entry, "sensor")
    await hass.config_entries.async_forward_entry_unload(config_entry, "lock")

async def reload_lw(hass, config_entry):

    await async_remove_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)