import asyncio
import uuid
import json
import datetime
import aiohttp

import logging

_LOGGER = logging.getLogger(__name__)

AUTH_SERVER = "https://auth.lightwaverf.com/v2/lightwaverf/autouserlogin/lwapps"
TRANS_SERVER = "wss://v1-linkplus-app.lightwaverf.com"
VERSION = "1.6.8"
MAX_RETRIES = 5
PUBLIC_AUTH_SERVER = "https://auth.lightwaverf.com/token"
PUBLIC_API = "https://publicapi.lightwaverf.com/v1/"
RGB_FLOOR = int("0x0", 16) #Previously the Lightwave app floored RGB values, but it doesn't any more

#TODO adapt async_connect calls to respond to connection failure

class _LWRFWebsocketMessage:
    _tran_id = 0
    _sender_id = str(uuid.uuid4())

    def __init__(self, opclass, operation):
        self._message = {"class": opclass, "operation": operation, "version": 1, "senderId": self._sender_id,
                         "transactionId": _LWRFWebsocketMessage._tran_id}
        _LWRFWebsocketMessage._tran_id += 1
        self._message["direction"] = "request"
        self._message["items"] = []

    def additem(self, newitem):
        self._message["items"].append(newitem._item)

    def json(self):
        return json.dumps(self._message)

class _LWRFWebsocketMessageItem:
    _item_id = 0

    def __init__(self, payload=None):
        if payload is None:
            payload = {}
        self._item = {"itemId": _LWRFWebsocketMessageItem._item_id}
        _LWRFWebsocketMessageItem._item_id += 1
        self._item["payload"] = payload


class LWRFFeatureSet:

    def __init__(self):
        self.link = None
        self.featureset_id = None
        self.name = None
        self.product_code = None
        self.features = {}

    def has_feature(self, feature): return feature in self.features.keys()

    def is_switch(self): return (self.has_feature('switch')) and not (self.has_feature('dimLevel'))
    def is_light(self): return self.has_feature('dimLevel')
    def is_climate(self): return self.has_feature('targetTemperature')
    def is_trv(self): return self.has_feature('valveSetup')
    def is_cover(self): return self.has_feature('threeWayRelay')
    def is_energy(self): return (self.has_feature('energy')) and (self.has_feature('rssi'))
    def is_windowsensor(self): return self.has_feature('windowPosition')
    def is_motionsensor(self): return self.has_feature('movement')
    def is_hub(self): return self.has_feature('buttonPress')

    def is_gen2(self): return (self.has_feature('upgrade') or self.has_feature('uiButton') or self.is_hub())
    def reports_power(self): return self.has_feature('power')
    def has_led(self): return self.has_feature('rgbColor')

class LWRFFeature:

    def __init__(self):
        self.featureset = None
        self.id = None
        self.name = None
        self._state = None

    @property
    def state(self):
        return self._state

    async def set_state(self, value):
        await self.featureset.link.async_write_feature(self.id, value)

class LWLink2:

    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None, device_id=None):

        self.featuresets = {}
        self._authtoken = None

        self._username = username
        self._password = password

        self._auth_method = auth_method
        self._api_token = api_token
        self._refresh_token = refresh_token

        self._session = aiohttp.ClientSession()
        self._token_expiry = None

        self._callback = []

        # Websocket only variables:
        self._device_id = (device_id or "PLW2:") + str(uuid.uuid4())
        self._websocket = None

        self._group_ids = []

        # Next two variables are used to synchronise responses to requests
        self._transactions = {}
        self._response = None

        asyncio.ensure_future(self._consumer_handler())

    async def _async_sendmessage(self, message, _retry=1, redact = False):

        if not self._websocket or self._websocket.closed:
            _LOGGER.info("async_sendmessage: Websocket closed, reconnecting")
            await self.async_connect()
            _LOGGER.info("async_sendmessage: Connection reopened")

        if redact:
            _LOGGER.debug("async_sendmessage: [contents hidden for security]")
        else:
            _LOGGER.debug("async_sendmessage: Sending: %s", message.json())

        await self._websocket.send_str(message.json())
        _LOGGER.debug("async_sendmessage: Message sent, waiting for acknowledgement from server")
        waitflag = asyncio.Event()
        self._transactions[message._message["transactionId"]] = waitflag
        waitflag.clear()
        try:
            await asyncio.wait_for(waitflag.wait(), timeout=5.0)
            _LOGGER.debug("async_sendmessage: Response received: %s", str(self._response))
        except asyncio.TimeoutError:
            _LOGGER.debug("async_sendmessage: Timeout waiting for response to : %s", message._message["transactionId"])
            self._transactions.pop(message._message["transactionId"])
            self._response = None

        if self._response:
            return self._response
        elif _retry >= MAX_RETRIES:
            if redact:
                _LOGGER.warning("Exceeding MAX_RETRIES, abandoning send. Failed message %s", message.json())
            else:
                _LOGGER.warning("Exceeding MAX_RETRIES, abandoning send. Failed message not shown as contains sensitive info")
            
            _LOGGER.warning("async_sendmessage: Exceeding MAX_RETRIES, abandoning send. Failed message %s", )
            return None
        else:
            _LOGGER.info("async_sendmessage: Send failed, resending message (attempt %s)", _retry + 1)
            return await self._async_sendmessage(message, _retry + 1, redact)

    async def _consumer_handler(self):
        while True:
            _LOGGER.debug("consumer_handler: Starting consumer handler")
            try:
                mess = await self._websocket.receive()
                _LOGGER.debug("consumer_handler: Received %s", mess)
            except AttributeError:  # websocket is None if not set up, just wait for a while
                _LOGGER.debug("consumer_handler: websocket not ready, sleeping for 0.1 sec")
                await asyncio.sleep(0.1)
            except Exception as exp:
                _LOGGER.warning("consumer_handler: unhandled exception ('{}')".format(exp))
            else:
                if mess.type == aiohttp.WSMsgType.TEXT:
                    message = mess.json()
                    # Some transaction IDs don't work, this is a workaround
                    if message["class"] == "feature" and (
                            message["operation"] == "write" or message["operation"] == "read"):
                        message["transactionId"] = message["items"][0]["itemId"]
                    # now parse the message
                    if message["transactionId"] in self._transactions:
                        _LOGGER.debug("consumer_handler: Response matched for transaction %s", message["transactionId"])
                        self._response = message
                        self._transactions[message["transactionId"]].set()
                        self._transactions.pop(message["transactionId"])
                    elif message["direction"] == "notification" and message["class"] == "group" \
                            and message["operation"] == "event":
                        await self.async_get_hierarchy()
                    elif message["direction"] == "notification" and message["operation"] == "event":
                        if "featureId" in message["items"][0]["payload"]:
                            feature_id = message["items"][0]["payload"]["featureId"]
                            feature = self.get_feature_by_featureid(feature_id)
                            value = message["items"][0]["payload"]["value"]
                            
                            if feature is None:
                                _LOGGER.debug("consumer_handler: feature is None: %s)", feature_id)
                            else:
                                prev_value = feature.state

                                feature._state = value
                                cblist = [c.__name__ for c in self._callback]
                                _LOGGER.debug("consumer_handler: Event received (%s %s %s), calling callbacks %s", feature_id, feature, value, cblist)
                                for func in self._callback:
                                    func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)
                        else:
                            _LOGGER.warning("consumer_handler: Unhandled event message: %s", message)
                    else:
                        _LOGGER.warning("consumer_handler: Received unhandled message: %s", message)

                elif mess.type == aiohttp.WSMsgType.CLOSED:
                    # We're not going to get a response, so clear response flag to allow _send_message to unblock
                    _LOGGER.info("consumer_handler: Websocket closed in message handler")
                    self._response = None
                    self._websocket = None
                    for key, flag in self._transactions.items():
                        flag.set()
                    self._transactions = {}
                    #self._authtoken = None
                    asyncio.ensure_future(self.async_connect())
                    _LOGGER.info("consumer_handler: Websocket reconnect requested by message handler")

    async def async_register_callback(self, callback):
        _LOGGER.debug("async_register_callback: Register callback '%s'", callback.__name__)
        self._callback.append(callback)

    async def async_get_hierarchy(self):
        _LOGGER.debug("async_get_hierarchy: Reading hierarchy")
        readmess = _LWRFWebsocketMessage("user", "rootGroups")
        readitem = _LWRFWebsocketMessageItem()
        readmess.additem(readitem)
        response = await self._async_sendmessage(readmess)

        self._group_ids = []
        for item in response["items"]:
            self._group_ids = self._group_ids + item["payload"]["groupIds"]

        _LOGGER.debug("async_get_hierarchy: Reading groups {}".format(self._group_ids))
        await self._async_read_groups()

        await self.async_update_featureset_states()

    async def _async_read_groups(self):
        self.featuresets = {}
        for groupId in self._group_ids:
            readmess = _LWRFWebsocketMessage("group", "hierarchy")
            readitem = _LWRFWebsocketMessageItem({"groupId": groupId})

            readmess.additem(readitem)
            hierarchy_response = await self._async_sendmessage(readmess)

            readmess2 = _LWRFWebsocketMessage("group", "read")
            readitem2 = _LWRFWebsocketMessageItem({"groupId": groupId,
                                         "devices": True,
                                         "features": True,
                                        #  "automations": True,
                                         "subgroups": True,
                                         "subgroupDepth": 10
                                         })
            
            readmess2.additem(readitem2)
            group_read_response = await self._async_sendmessage(readmess2)

            devices = list(group_read_response["items"][0]["payload"]["devices"].values())
            features = list(group_read_response["items"][0]["payload"]["features"].values())

            featuresets = list(hierarchy_response["items"][0]["payload"]["featureSet"])
            
            self.get_featuresets(featuresets, devices, features)

    async def async_update_featureset_states(self):
        for x in self.featuresets.values():
            for y in x.features.values():
                value = await self.async_read_feature(y.id)
                if value["items"][0]["success"] == True:
                    y._state = value["items"][0]["payload"]["value"]
                else:
                    _LOGGER.warning("update_featureset_states: failed to read feature ({}), returned {}".format(y.id, value))

    async def async_write_feature(self, feature_id, value):
        readmess = _LWRFWebsocketMessage("feature", "write")
        readitem = _LWRFWebsocketMessageItem({"featureId": feature_id, "value": value})
        readmess.additem(readitem)
        await self._async_sendmessage(readmess)

    async def async_write_feature_by_name(self, featureset_id, featurename, value):
        await self.featuresets[featureset_id].features[featurename].set_state(value)

    async def async_read_feature(self, feature_id):
        readmess = _LWRFWebsocketMessage("feature", "read")
        readitem = _LWRFWebsocketMessageItem({"featureId": feature_id})
        readmess.additem(readitem)
        return await self._async_sendmessage(readmess)

    def get_featureset_by_featureid(self, feature_id):
        for x in self.featuresets.values():
            for y in x.features.values():
                if y.id == feature_id:
                    return x
        return None

    def get_feature_by_featureid(self, feature_id):
        for x in self.featuresets.values():
            for y in x.features.values():
                if y.id == feature_id:
                    return y
        return None

    async def async_turn_on_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "switch", 1)

    async def async_turn_off_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "switch", 0)

    async def async_set_brightness_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "dimLevel", level)

    async def async_set_temperature_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "targetTemperature", int(level * 10))

    async def async_set_valvelevel_by_featureset_id(self, featureset_id, level):
        await self.async_write_feature_by_name(featureset_id, "valveLevel", int(level * 20))

    async def async_cover_open_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 1)

    async def async_cover_close_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 2)

    async def async_cover_stop_by_featureset_id(self, featureset_id):
        await self.async_write_feature_by_name(featureset_id, "threeWayRelay", 0)

    async def async_set_led_rgb_by_featureset_id(self, featureset_id, color):
        red = (color & int("0xFF0000", 16)) >> 16
        if red != 0:
            red = min(max(red, RGB_FLOOR), 255)
        green = (color & int("0xFF00", 16)) >> 8
        if green != 0:
            green = min(max(green, RGB_FLOOR), 255)
        blue = (color & int("0xFF", 16))
        if blue != 0:
            blue = min(max(blue , RGB_FLOOR), 255)
        newcolor = (red << 16) + (green << 8) + blue
        await self.async_write_feature_by_name(featureset_id, "rgbColor", newcolor)

    def get_switches(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_switch()]

    def get_lights(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_light()]

    def get_climates(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_climate()]

    def get_covers(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_cover()]

    def get_energy(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_energy()]

    def get_windowsensors(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_windowsensor()]

    def get_motionsensors(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_motionsensor()]

    def get_hubs(self):
        return [(x.featureset_id, x.name) for x in self.featuresets.values() if x.is_hub()]

    #########################################################
    # Connection
    #########################################################

    async def async_connect(self, max_tries=5, force_keep_alive_secs=0):
        retry_delay = 2
        for x in range(0, max_tries):
            try:
                result = await self._connect_to_server()
                if force_keep_alive_secs > 0:
                    asyncio.ensure_future(self.async_force_reconnect(force_keep_alive_secs))
                return result
            except Exception as exp:
                if x < max_tries-1:
                    _LOGGER.warning("async_connect: Cannot connect (exception '{}'). Waiting {} seconds to retry".format(repr(exp), retry_delay))
                    _LOGGER.warning("async_connect: IKB -  Cannot connect (exception '{}'). Waiting {} seconds to retry".format(exp, retry_delay))
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(2 * retry_delay, 120)
                else:
                    _LOGGER.warning("async_connect: Cannot connect (exception '{}'). No more retry".format(repr(exp), retry_delay))
        _LOGGER.warning("async_connect: Cannot connect, max_tries exceeded, aborting")
        return False

    async def async_force_reconnect(self, secs):
        while True:
            await asyncio.sleep(secs)
            _LOGGER.debug("async_force_reconnect: time elapsed, forcing a reconnection")
            await self._websocket.close()


    async def _connect_to_server(self):
        if (not self._websocket) or self._websocket.closed:
            _LOGGER.debug("connect_to_server: Connecting to websocket")
            self._websocket = await self._session.ws_connect(TRANS_SERVER, heartbeat=10)
        return await self._authenticate_websocket()

    async def _authenticate_websocket(self):
        if not self._authtoken:
            await self._get_access_token()
        if self._authtoken:
            authmess = _LWRFWebsocketMessage("user", "authenticate")
            authpayload = _LWRFWebsocketMessageItem({"token": self._authtoken, "clientDeviceId": self._device_id})
            authmess.additem(authpayload)
            response = await self._async_sendmessage(authmess, redact = True)
            if not response["items"][0]["success"]:
                if response["items"][0]["error"]["code"] == "200":
                    # "Channel is already authenticated" - Do nothing
                    pass
                elif response["items"][0]["error"]["code"] == 405:
                    # "Access denied" - bogus token, let's reauthenticate
                    # Lightwave seems to return a string for 200 but an int for 405!
                    _LOGGER.info("authenticate_websocket: Authentication token rejected, regenerating and reauthenticating")
                    self._authtoken = None
                    await self._authenticate_websocket()
                elif response["items"][0]["error"]["message"] == "user-msgs: Token not valid/expired.":
                    _LOGGER.info("authenticate_websocket: Authentication token expired, regenerating and reauthenticating")
                    self._authtoken = None
                    await self._authenticate_websocket()
                else:
                    _LOGGER.warning("authenticate_websocket: Unhandled authentication error {}".format(response["items"][0]["error"]["message"]))
            return response
        else:
            return None

    async def _get_access_token(self):
        if self._auth_method == "username":
            await self._get_access_token_username()
        elif self._auth_method == "api":
            await self._get_access_token_api()
        else:
            raise ValueError("auth_method must be 'username' or 'api'")

    async def _get_access_token_username(self):
        _LOGGER.debug("get_access_token_username: Requesting authentication token (using username and password)")
        authentication = {"email": self._username, "password": self._password, "version": VERSION}
        async with self._session.post(AUTH_SERVER, headers={"x-lwrf-appid": "ios-01"}, json=authentication) as req:
            if req.status == 200:
                _LOGGER.debug("get_access_token_username: Received response: [contents hidden for security]")
                #_LOGGER.debug("get_access_token_username: Received response: {}".format(await req.json()))
                self._authtoken = (await req.json())["tokens"]["access_token"]
            elif req.status == 404:
                _LOGGER.warning("get_access_token_username: Authentication failed - if network is ok, possible wrong username/password")
                self._authtoken = None
            else:
                _LOGGER.warning("get_access_token_username: Authentication failed, : status {}".format(req.status))
                self._authtoken = None

    # TODO check for token expiry
    async def _get_access_token_api(self):
        _LOGGER.debug("get_access_token_api: Requesting authentication token (using API key and refresh token)")
        authentication = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        async with self._session.post(PUBLIC_AUTH_SERVER,
                            headers={"authorization": "basic " + self._api_token},
                            json=authentication) as req:
            _LOGGER.debug("get_access_token_api: Received response: [contents hidden for security]")
            #_LOGGER.debug("get_access_token_api: Received response: {}".format(await req.text()))
            if req.status == 200:
                self._authtoken = (await req.json())["access_token"]
                self._refresh_token = (await req.json())["refresh_token"]
                self._token_expiry = datetime.datetime.now() + datetime.timedelta(seconds=(await req.json())["expires_in"])
            else:
                _LOGGER.warning("get_access_token_api: No authentication token (status_code '{}').".format(req.status))
                raise ConnectionError("No authentication token: {}".format(await req.text()))

    #########################################################
    # Convenience methods for non-async calls
    #########################################################

    def _sendmessage(self, message):
        return asyncio.get_event_loop().run_until_complete(self._async_sendmessage(message))

    def get_hierarchy(self):
        return asyncio.get_event_loop().run_until_complete(self.async_get_hierarchy())

    def update_featureset_states(self):
        return asyncio.get_event_loop().run_until_complete(self.async_update_featureset_states())

    def write_feature(self, feature_id, value):
        return asyncio.get_event_loop().run_until_complete(self.async_write_feature(feature_id, value))

    def write_feature_by_name(self, featureset_id, featurename, value):
        return asyncio.get_event_loop().run_until_complete(self.async_write_feature_by_name(self, featureset_id, featurename, value))

    def read_feature(self, feature_id):
        return asyncio.get_event_loop().run_until_complete(self.async_read_feature(feature_id))

    def turn_on_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_turn_on_by_featureset_id(featureset_id))

    def turn_off_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_turn_off_by_featureset_id(featureset_id))

    def set_brightness_by_featureset_id(self, featureset_id, level):
        return asyncio.get_event_loop().run_until_complete(self.async_set_brightness_by_featureset_id(featureset_id, level))

    def set_temperature_by_featureset_id(self, featureset_id, level):
        return asyncio.get_event_loop().run_until_complete(self.async_set_temperature_by_featureset_id(featureset_id, level))

    def cover_open_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_open_by_featureset_id(featureset_id))

    def cover_close_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_close_by_featureset_id(featureset_id))

    def cover_stop_by_featureset_id(self, featureset_id):
        return asyncio.get_event_loop().run_until_complete(self.async_cover_stop_by_featureset_id(featureset_id))

    def connect(self):
        return asyncio.get_event_loop().run_until_complete(self.async_connect())

    def get_from_lw_ar_by_id(self, ar, id, label):
        for x in ar:
            if x[label] == id:
                return x
        return None

    def get_featuresets(self, featuresets, devices, features):
        for y in featuresets:
            new_featureset = LWRFFeatureSet()
            new_featureset.link = self
            new_featureset.featureset_id = y["groupId"]
            
            device = self.get_from_lw_ar_by_id(devices, y["deviceId"], "deviceId")

            new_featureset.product_code = device["productCode"]

            new_featureset.name = y["name"]

            for z in y["features"]:
                feature = LWRFFeature()
                feature.id = z
                feature.featureset = new_featureset

                _feature = self.get_from_lw_ar_by_id(features, z, 'featureId')
                feature.name = _feature["attributes"]["type"]
                new_featureset.features[_feature["attributes"]["type"]] = feature

            self.featuresets[y["groupId"]] = new_featureset


class LWLink2Public(LWLink2):

    def __init__(self, username=None, password=None, auth_method="username", api_token=None, refresh_token=None):

        self.featuresets = {}
        self._authtoken = None

        self._username = username
        self._password = password

        self._auth_method = auth_method
        self._api_token = api_token
        self._refresh_token = refresh_token

        self._session = aiohttp.ClientSession()
        self._token_expiry = None

        self._callback = []

    # TODO add retries/error checking to public API requests
    async def _async_getrequest(self, endpoint, _retry=1):
        _LOGGER.debug("async_getrequest: Sending API GET request to {}".format(endpoint))
        async with self._session.get(PUBLIC_API + endpoint,
                                     headers= {"authorization": "bearer " + self._authtoken}
                                      ) as req:
            _LOGGER.debug("async_getrequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_getrequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_getrequest(endpoint, _retry)

            return await req.json()

    async def _async_postrequest(self, endpoint, body="", _retry=1):
        _LOGGER.debug("async_postrequest: Sending API POST request to {}: {}".format(endpoint, body))
        async with self._session.post(PUBLIC_API + endpoint,
                                      headers= {"authorization": "bearer " + self._authtoken},
                                      json=body) as req:
            _LOGGER.debug("async_postrequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_postrequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_postrequest(endpoint, body, _retry)
            if not(req.status == 401 and (await req.json())['message'] == 'Unauthorized'):
                return await req.json()
        try:
            _LOGGER.info("async_postrequest: POST failed due to unauthorized connection, retrying connect")
            await self.async_connect()
            async with self._session.post(PUBLIC_API + endpoint,
                                          headers={
                                              "authorization": "bearer " + self._authtoken},
                                          json=body) as req:
                _LOGGER.debug("async_postrequest: Received API response {} {} {}".format(req.status, await req.text(), await req.json(content_type=None)))
                return await req.json()
        except:
            return False

    async def _async_deleterequest(self, endpoint, _retry=1):
        _LOGGER.debug("async_deleterequest: Sending API DELETE request to {}".format(endpoint))
        async with self._session.delete(PUBLIC_API + endpoint,
                                     headers= {"authorization": "bearer " + self._authtoken}
                                      ) as req:
            _LOGGER.debug("async_deleterequest: Received API response {} {} {}".format(req.status, req.raw_headers, await req.text()))
            if (req.status == 429): #Rate limited
                _LOGGER.debug("async_deleterequest: rate limited, wait and retry")
                await asyncio.sleep(1)
                await self._async_deleterequest(endpoint, _retry)
            return await req.json()

    async def async_get_hierarchy(self):

        self.featuresets = {}
        req = await self._async_getrequest("structures")
        for struct in req["structures"]:
            response = await self._async_getrequest("structure/" + struct)

            for x in response["devices"]:
                for y in x["featureSets"]:
                    _LOGGER.debug("async_get_hierarchy: Creating device {}".format(y))
                    new_featureset = LWRFFeatureSet()
                    new_featureset.link = self
                    new_featureset.featureset_id = y["featureSetId"]
                    new_featureset.product_code = x["productCode"]
                    new_featureset.name = y["name"]

                    for z in y["features"]:
                        _LOGGER.debug("async_get_hierarchy: Adding device features {}".format(z))
                        feature = LWRFFeature()
                        feature.id = z["featureId"]
                        feature.featureset = new_featureset
                        feature.name = z["type"]
                        new_featureset.features[z["type"]] = feature

                    self.featuresets[y["featureSetId"]] = new_featureset

        await self.async_update_featureset_states()

    async def async_register_webhook(self, url, feature_id, ref, overwrite = False):
        if overwrite:
            req = await self._async_deleterequest("events/" + ref)
        payload = {"events": [{"type": "feature", "id": feature_id}],
                    "url": url,
                    "ref": ref}
        req = await self._async_postrequest("events", payload)
        #TODO: test for req = 200

    async def async_register_webhook_list(self, url, feature_id_list, ref, overwrite = False):
        if overwrite:
            req = await self._async_deleterequest("events/" + ref)
        feature_list = []
        for feat in feature_id_list:
            feature_list.append({"type": "feature", "id": feat})
        payload = {"events": feature_list,
                    "url": url,
                    "ref": ref}
        req = await self._async_postrequest("events", payload)
        #TODO: test for req = 200

    async def async_register_webhook_all(self, url, ref, overwrite = False):
        if overwrite:
            webhooks = await self._async_getrequest("events")
            for wh in webhooks:
                if ref in wh["id"]:
                    await self._async_deleterequest("events/" + wh["id"])
        feature_list = []
        for x in self.featuresets.values():
            for y in x.features.values():
                feature_list.append(y.id)
        MAX_REQUEST_LENGTH = 200
        feature_list_split = [feature_list[i:i + MAX_REQUEST_LENGTH] for i in range(0, len(feature_list), MAX_REQUEST_LENGTH)]
        index = 1
        for feat_list in feature_list_split:
            f_list = []
            for feat in feat_list:
                f_list.append({"type": "feature", "id": feat})
            payload = {"events": f_list,
                "url": url,
                "ref": ref+str(index)}
            req = await self._async_postrequest("events", payload)
            index += 1
        #TODO: test for req = 200

    async def async_get_webhooks(self):
        webhooks = await self._async_getrequest("events")
        wh_list = []
        for wh in webhooks:
            wh_list.append(wh["id"])
        return wh_list

    async def delete_all_webhooks(self):
        webhooks = await self._async_getrequest("events")
        for wh in webhooks:
            await self._async_deleterequest("events/" + wh["id"])

    async def async_delete_webhook(self, ref):
        req = await self._async_deleterequest("events/" + ref)
        #TODO: test for req = 200

    def process_webhook_received(self, body):

        featureid = body['triggerEvent']['id']
        feature = self.get_feature_by_featureid(featureid)
        value = body['payload']['value']
        prev_value = feature.state
        feature._state = value
        
        cblist = [c.__name__ for c in self._callback]
        _LOGGER.debug("process_webhook_received: Event received (%s %s %s), calling callbacks %s", featureid, feature, value, cblist)
        for func in self._callback:
            func(feature=feature.name, feature_id=feature.id, prev_value = prev_value, new_value = value)

    async def async_update_featureset_states(self):
        feature_list = []

        for x in self.featuresets.values():
            for y in x.features.values():
                feature_list.append({"featureId": y.id})

        #split up the feature list into chunks as the public API doesn't like requests that are too long
        #if the request is too long, will get 404 response {"message":"Structure not found"} or a 500 Internal Server Error
        #a value of 200 used to work, but for at least one user this results in a 500 error now, so setting it to 150
        MAX_REQUEST_LENGTH = 150
        feature_list_split = [feature_list[i:i + MAX_REQUEST_LENGTH] for i in range(0, len(feature_list), MAX_REQUEST_LENGTH)]
        for feat_list in feature_list_split:
            body = {"features": feat_list}
            req = await self._async_postrequest("features/read", body)

            for featuresetid in self.featuresets:
                for featurename in self.featuresets[featuresetid].features:
                    if self.featuresets[featuresetid].features[featurename].id in req:
                        self.featuresets[featuresetid].features[featurename]._state = req[self.featuresets[featuresetid].features[featurename].id]

    async def async_write_feature(self, feature_id, value):
        payload = {"value": value}
        await self._async_postrequest("feature/" + feature_id, payload)

    async def async_read_feature(self, feature_id):
        req = await self._async_getrequest("feature/" + feature_id)
        return req["value"]

    #########################################################
    # Connection
    #########################################################

    async def _connect_to_server(self):
        await self._get_access_token()
        return True

    async def async_force_reconnect(self, secs):
        _LOGGER.debug("async_force_reconnect: not implemented for public API, skipping")




