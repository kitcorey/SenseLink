# Copyright 2020, Charles Powell

import websockets
import json
import logging
import asyncio
import dpath.util
from socket import gaierror
from asyncio_mqtt import Client, MqttError
from contextlib import AsyncExitStack
from typing import Dict

# Independently set WS logger
wslogger = logging.getLogger('websockets')
wslogger.setLevel(logging.ERROR)


def safekey(d, keypath, default=None):
    try:
        val = dpath.util.get(d, keypath)
        return val
    except KeyError:
        return default


class HASSController:
    ws = None
    event_rq_id = 1
    bulk_rq_id = 2
    data_sources = []

    def __init__(self, url, auth_token):
        self.url = url
        self.auth_token = auth_token

    def connect(self):
        # Create task
        asyncio.create_task(self.client_handler())

    async def client_handler(self):
        logging.info(f"Starting websocket client to URL: {self.url}")
        try:
            async with websockets.connect(self.url) as websocket:
                self.ws = websocket
                # Wait for incoming message from server
                while True:
                    try:
                        message = await websocket.recv()
                        logging.debug(f"Received message: {message}")
                        await self.on_message(websocket, message)
                    except websockets.exceptions.ConnectionClosed as err:
                        logging.error(f"Lost connection to websocket server ({err})")
                        logging.info(f"Reconnecting in 10...")
                        await asyncio.sleep(10)
                        asyncio.create_task(self.client_handler())
                        return False
        except (websockets.exceptions.WebSocketException, gaierror) as err:
            logging.error(f"Unable to connect to server at {self.url} ({type(err)}:{err})")
            logging.info(f"Attempting to reconnect in 10...")
            await asyncio.sleep(10)
            asyncio.create_task(self.client_handler())

    async def on_message(self, ws, message):
        # Authentication with HASS Websockets
        message = json.loads(message)

        if 'type' in message and message['type'] == 'auth_required':
            logging.info("Authentication requested")
            auth_response = {'type': 'auth', 'access_token': self.auth_token}
            await ws.send(json.dumps(auth_response))

        elif 'type' in message and message['type'] == "auth_invalid":
            logging.error("Authentication failed")

        elif 'type' in message and message['type'] == "auth_ok":
            logging.info("Authentication successful")
            # Authentication successful
            # Send subscription command
            events_command = {
                "id": self.event_rq_id,
                "type": "subscribe_events",
                "event_type": "state_changed"
            }
            await ws.send(json.dumps(events_command))
            logging.info("Event update request sent")

            # Request full status update to get current value
            events_command = {
                "id": self.bulk_rq_id,
                "type": "get_states",
            }
            await ws.send(json.dumps(events_command))
            logging.info("All states request sent")

        elif 'type' in message and message['id'] == self.event_rq_id:
            # Look for state_changed events
            logging.debug("Potential event update received")
            # Check for data
            if not safekey(message, 'event/data'):
                return
            # Notify attached data sources
            for ds in self.data_sources:
                ds.parse_incremental_update(message['event']['data'])

        elif 'type' in message and message['id'] == self.bulk_rq_id:
            # Look for state_changed events
            logging.info("Bulk update received")
            if message.get('result') is None:
                return
            # Extract data
            bulk_update = message.get('result')
            logging.debug(f"Entity update received: {bulk_update}")
            # Loop through statuses
            for status in bulk_update:
                # Notify attached data sources
                for ds in self.data_sources:
                    ds.parse_bulk_update(status)
        else:
            logging.debug(f"Unknown/unhandled message received: {message}")


class MQTTTopic:
    handlers = []

    def __init__(self, topic, handler):
        self.topic = topic
        self.handlers.append(handler)

    def __hash__(self):
        return hash(self.topic)


class MQTTController:
    data_sources = []
    user = None
    password = None
    client = None
    topics: Dict[str, MQTTTopic] = {}
    tasks = set()

    def __init__(self, host, port=1883, username=None, password=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def connect(self):
        # Create task
        asyncio.create_task(self.client_handler())

    async def client_handler(self):
        logging.info(f"Starting MQTT client to URL: {self.host}")
        reconnect_interval = 5  # [seconds]
        while True:
            try:
                await self.listen()
            except MqttError as error:
                logging.error(f'Disconnected from MQTT broker, reconnecting in {reconnect_interval}... ({error}')
            finally:
                await asyncio.sleep(reconnect_interval)

    async def listen(self):
        async with AsyncExitStack() as stack:
            # Track tasks
            stack.push_async_callback(self.cancel_tasks, self.tasks)

            # Connect to the MQTT broker
            client = Client(self.host, self.port, username=self.username, password=self.password)
            await stack.enter_async_context(client)

            # Add tasks for each data source handler
            for ds in self.data_sources:
                # Get handlers from data source
                ds_handlers = ds.handlers()
                # Iterate through and add handlers for each topic
                # TODO: This could maybe just be handled by tuples, i.e. the data source
                #  could just return (topic_str, [h1, h2, h3]), and then the handlers get
                #  appended to the existing list. Not sure a custom class adds any value
                for ds_hl in ds_handlers:
                    if ds_hl.topic in self.topics:
                        # Add these handlers to existing top level topic handler
                        topic = self.topics[ds_hl.topic]
                        topic.handlers.append(ds_hl.handlers)
                    else:
                        # Add this instance as a new top level handler
                        self.topics[ds_hl.topic] = ds_hl

            # Add handlers for each topic as a filtered topic
            all_topics = []
            for topic_string, topic in self.topics.items():
                all_topics.append(topic)
                manager = client.filtered_messages(topic_string)
                messages = await stack.enter_async_context(manager)
                for hl in topic.handlers:
                    task = asyncio.create_task(hl(messages))
                    self.tasks.add(task)

            # Subscribe to all topics
            await client.subscribe(all_topics)

            # Gather all tasks
            await asyncio.gather(*self.tasks)

    async def cancel_tasks(self):
        for task in self.tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    pass
