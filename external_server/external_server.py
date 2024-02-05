import logging
import time
import sys

sys.path.append("lib/fleet-protocol/protobuf/compiled/python")

import ExternalProtocol_pb2 as external_protocol
import InternalProtocol_pb2 as internal_protocol
from external_server.checker import CommandMessagesChecker, SessionTimeoutChecker, OrderChecker
from external_server.exceptions import (
    ConnectSequenceException,
    CommunicationException,
    StatusTimeOutExc,
    ClientDisconnectedExc,
    CommandResponseTimeOutExc,
)
from external_server.message_creator import MessageCreator
from external_server.mqtt_client import MqttClient
from external_server.utils import check_file_exists, device_repr
from external_server.external_server_api_client import ExternalServerApiClient
from external_server.command_waiting_thread import CommandWaitingThread
from external_server.config import Config
from external_server.structures import GeneralErrorCodes, DisconnectTypes, TimeoutType
from external_server.event_queue import EventQueueSingleton, EventType


class ExternalServer:
    def __init__(self, config: Config) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)

        self._config = config
        self._session_id = ""

        self._event_queue = EventQueueSingleton()

        self._session_checker = SessionTimeoutChecker(self._config.mqtt_timeout)
        self._command_checker = CommandMessagesChecker(self._config.timeout)
        self._status_order_checker = OrderChecker(self._config.timeout)
        self._connected_devices = list()
        self._not_connected_devices = list()
        self._mqtt_client = MqttClient(self._config.company_name, self._config.car_name)

        self._modules = dict()
        self._modules_command_threads = dict()
        config_modules = config.modules
        for module_number in config_modules:
            self._modules[int(module_number)] = ExternalServerApiClient(
                config_modules[module_number], self._config.company_name, self._config.car_name
            )
            self._modules[int(module_number)].init()
            
            if not self._modules[int(module_number)].device_initialized():
                self._logger.error(
                    f"Module {module_number}: Error occurred in init function. Check the configuration file."
                )
                raise RuntimeError(f"Module {module_number}: Error occurred in init function. Check the configuration file.")

            if self._modules[int(module_number)].get_module_number() != int(module_number):
                self._logger.error(
                    f"Module number {self._modules[int(module_number)].get_module_number()} returned from API does not match with module number {int(module_number)} in config"
                )
                raise RuntimeError(f"Module number returned from API does not match with module number in config {self._modules[int(module_number)].get_module_number()}/{int(module_number)}")
            self._modules_command_threads[int(module_number)] = CommandWaitingThread(
                self._modules[int(module_number)]
            )

    def set_tls(self, ca_certs: str, certfile: str, keyfile: str) -> None:
        "Set tls security to mqtt client"
        if not check_file_exists(ca_certs):
            raise FileNotFoundError(ca_certs)
        if not check_file_exists(certfile):
            raise FileNotFoundError(certfile)
        if not check_file_exists(keyfile):
            raise FileNotFoundError(keyfile)
        self._mqtt_client.set_tls(ca_certs, certfile, keyfile)

    def start(self) -> None:
        self._mqtt_client.init()
        for module_number in self._modules:
            self._modules_command_threads[module_number].start()

        while True:
            try:
                if not self._mqtt_client.is_connected:
                    self._mqtt_client.connect(self._config.mqtt_address, self._config.mqtt_port)
                    self._mqtt_client.start()
                self._init_sequence()
                self._normal_communication()
            except ConnectSequenceException:
                self._logger.error("Connect sequence failed")
            except ConnectionRefusedError:
                self._logger.error(
                    f"Unable to connect to MQTT broker on {self._config.mqtt_address}:{self._config.mqtt_port}, trying again"
                )
                time.sleep(self._config.sleep_duration_after_connection_refused)
            except ClientDisconnectedExc:  # if 30 seconds any message has not been received
                self._logger.error("Client timed out")
            except CommunicationException:
                pass
            finally:
                self._clear_context()

    def _init_sequence(self) -> None:
        self._init_seq_connect()
        self._init_seq_status()
        self._init_seq_command()
        self._event_queue.clear()
        self._logger.info("Connect sequence has been succesfully finished")

    def _init_seq_connect(self) -> None:
        received_msg = self._mqtt_client.get()
        if received_msg == False:
            self._mqtt_client.stop()
            raise ConnectSequenceException()
        if not received_msg.HasField("connect"):
            self._logger.error("Connect message has been expected")
            raise ConnectSequenceException()
        self._logger.info("Connect message has been received")
        self._session_id = received_msg.connect.sessionId

        devices = received_msg.connect.devices
        for device in devices:
            if device.module not in self._modules:
                self._logger.warning(f"Module {device.module} not supported, communication with it will be ignored")
                self._not_connected_devices.append(device)
                continue

            if (
                self._modules[device.module].is_device_type_supported(device.deviceType)
                == GeneralErrorCodes.NOT_OK
            ):
                self._logger.warning(
                    f"Device type {device.deviceType} not supported by module {device.module}, device will probably not work properly"
                )

            rc = self._connect_device(device)
            if rc != GeneralErrorCodes.OK:
                self._logger.warning(
                    f"Failed to connect device with module number {device.module}, ignoring device"
                )

        sent_msg = self._create_connect_response(external_protocol.ConnectResponse.Type.OK)
        self._mqtt_client.publish(sent_msg)

    def _init_seq_status(self) -> None:
        for _ in range(len(self._connected_devices) + len(self._not_connected_devices)):
            status_msg = self._mqtt_client.get(timeout=self._config.mqtt_timeout)
            if status_msg == False:
                raise ConnectSequenceException()
            if status_msg == None or not status_msg.HasField("status"):
                self._logger.error("Status message has been expected")
                raise ConnectSequenceException()

            device = status_msg.status.deviceStatus.device
            if not self._is_device_in_list(device, self._connected_devices):
                self._logger.warning(
                    f"Received status from not connected device, unique identificator:"
                    f" {device.module}/{device.deviceType}/{device.deviceRole}"
                )
            if status_msg.status.deviceState != external_protocol.Status.DeviceState.CONNECTING:
                self._logger.error(
                    f"Status device state is different, received: {status_msg.status.deviceState}"
                    f" expected: {external_protocol.Status.DeviceState.CONNECTING}"
                )
                raise ConnectSequenceException()

            self._status_order_checker.check(status_msg.status)
            self._status_order_checker.get_status()  # Checked status is not needed in Init sequence
            self._logger.info(
                f"Received Status message, messageCounter: {status_msg.status.messageCounter}"
                f" error: {status_msg.status.errorMessage}"
            )

            if device not in self._not_connected_devices:
                if len(status_msg.status.errorMessage) > 0:
                    rc = self._modules[device.module].forward_error_message(
                        device, status_msg.status.errorMessage
                    )
                    if rc != GeneralErrorCodes.OK:
                        self._logger.error(
                            f"Module {device.module}: Error occurred in forward_error_message function, rc: {rc}"
                        )

                rc = self._modules[device.module].forward_status(
                    device, status_msg.status.deviceStatus.statusData
                )
                self._check_forward_status_rc(device.module, rc)

            sent_msg = self._create_status_response(status_msg.status)
            self._mqtt_client.publish(sent_msg)

    def _init_seq_command(self) -> None:
        devices_with_no_command = self._connected_devices.copy()
        for module in self._modules:
            module_commands = []
            rc = 0

            while rc != None:
                rc = self._modules_command_threads[module].pop_command()
                if rc != None:
                    command, for_device = rc
                    module_commands.append([command, for_device])

            for command, for_device in module_commands:
                if not self._is_device_in_list(
                    for_device, devices_with_no_command
                ) and self._is_device_in_list(for_device, self._connected_devices):
                    self._logger.error(
                        f"Command for {for_device.deviceName} device was returned from API more than once"
                    )
                elif not self._is_device_in_list(for_device, devices_with_no_command):
                    self._logger.warning(
                        f"Command returned from module {module}'s API is intended for not connected device, command won't be sent"
                    )
                else:
                    command_counter = self._command_checker.counter
                    external_command = MessageCreator.create_external_command(
                        self._session_id, command_counter, for_device, command
                    )
                    self._logger.info(f"Sending Command message, messageCounter: {command_counter}")
                    self._mqtt_client.publish(external_command)
                    self._command_checker.add_command(external_command.command, True)
                    devices_with_no_command.remove(for_device)

        for device in devices_with_no_command + self._not_connected_devices:
            command_counter = self._command_checker.counter
            external_command = MessageCreator.create_external_command(
                self._session_id, command_counter, device, None
            )
            self._logger.warning(
                f"No command was returned from API for device {device.deviceName}, sending empty command for this device"
            )
            self._logger.info(f"Sending Command message, messageCounter: {command_counter}")
            self._mqtt_client.publish(external_command)
            self._command_checker.add_command(external_command.command, True)

        for _ in range(len(self._connected_devices) + len(self._not_connected_devices)):
            received_msg = self._mqtt_client.get(timeout=self._config.mqtt_timeout)
            if received_msg == False:
                raise ConnectSequenceException()
            if received_msg == None or not received_msg.HasField("commandResponse"):
                self._logger.error("Command response message has been expected")
                raise ConnectSequenceException()
            commands = self._command_checker.pop_commands(
                received_msg.commandResponse.messageCounter
            )
            for command, returned_from_api in commands:
                if returned_from_api and command.deviceCommand.device in self._connected_devices:
                    rc = self._modules[command.deviceCommand.device.module].command_ack(
                        command.deviceCommand.commandData, command.deviceCommand.device
                    )
                    self._check_command_ack_rc(command.deviceCommand.device.module, rc)

    def _normal_communication(self) -> None:
        self._session_checker.start()
        while True:
            event = self._event_queue.get()
            if event.event == EventType.RECEIVED_MESSAGE:
                received_msg = self._mqtt_client.get(timeout=None)
                if received_msg is not None:
                    if received_msg.HasField("connect"):
                        self._handle_connect(received_msg.connect.sessionId)

                    elif received_msg.HasField("status"):
                        self._handle_status(received_msg.status)

                    elif received_msg.HasField("commandResponse"):
                        self._handle_command_response(received_msg.commandResponse)
            elif event.event == EventType.MQTT_BROKER_DISCONNECTED:
                raise CommunicationException()
            elif event.event == EventType.TIMEOUT_OCCURRED:
                if event.data == TimeoutType.SESSION_TIMEOUT:
                    raise ClientDisconnectedExc()
                elif event.data == TimeoutType.MESSAGE_TIMEOUT:
                    raise StatusTimeOutExc()
                elif event.data == TimeoutType.COMMAND_TIMEOUT:
                    raise CommandResponseTimeOutExc()
                else:
                    self._logger.error(
                        "Internal error: Received Event TimeoutOccurred without TimeoutType"
                    )
            elif event.event == EventType.COMMAND_AVAILABLE:
                if isinstance(event.data, int):
                    self._handle_command(event.data)
                else:
                    self._logger.error(
                        "Internal error: Received Event CommandAvailable without module number"
                    )

    def _handle_connect(self, received_msg_session_id: str) -> None:
        self._logger.warning("Received Connect message")
        if self._session_id == received_msg_session_id:
            self._logger.error("Connected session has sent Connect message")
            CommunicationException()
        sent_msg = self._create_connect_response(
            external_protocol.ConnectResponse.Type.ALREADY_LOGGED
        )
        self._mqtt_client.publish(sent_msg)

    def _handle_status(self, received_status: external_protocol.Status) -> None:
        self._logger.info(
            f"Received Status message, messageCounter: {received_status.messageCounter}"
            f" error: {received_status.errorMessage}"
        )
        self._reset_session_checker_if_session_id_is_ok(received_status.sessionId)
        self._status_order_checker.check(received_status)

        while (status := self._status_order_checker.get_status()) is not None:
            device = status.deviceStatus.device
            
            if (device.module not in self._modules):
                self._logger.warning(
                    f"Received status for device with unknown module number {device.module}"
                )
                continue
            if (
                self._modules[device.module].is_device_type_supported(device.deviceType)
                == GeneralErrorCodes.NOT_OK
            ):
                self._logger.error(
                    f"Device type {device.deviceType} not supported by module {device.module}"
                )
                continue

            if status.deviceState == external_protocol.Status.DeviceState.RUNNING:
                if not self._is_device_in_list(device, self._connected_devices):
                    self._logger.error(f"Device {device_repr(device)} is not connected")
                    continue
                rc = self._modules[device.module].forward_status(
                    device, status.deviceStatus.statusData
                )
                self._check_forward_status_rc(device.module, rc)
            elif status.deviceState == external_protocol.Status.DeviceState.CONNECTING:
                if self._is_device_in_list(device, self._connected_devices):
                    self._logger.error(f"Device {device_repr(device)} is already connected")
                    continue
                self._connect_device(device)
                rc = self._modules[device.module].forward_status(
                    device, status.deviceStatus.statusData
                )
                self._check_forward_status_rc(device.module, rc)
            elif status.deviceState == external_protocol.Status.DeviceState.DISCONNECT:
                if not self._is_device_in_list(device, self._connected_devices):
                    self._logger.error(f"Device {device_repr(device)} is not connected")
                    continue
                rc = self._modules[device.module].forward_status(
                    device, status.deviceStatus.statusData
                )
                self._check_forward_status_rc(device.module, rc)
                self._disconnect_device(DisconnectTypes.announced, device)

            if len(status.errorMessage) > 0:
                self._logger.error(
                    f"Status for device {device_repr(device)} contains error message"
                )

            status_response = self._create_status_response(status)
            self._mqtt_client.publish(status_response)

    def _handle_command_response(self, command_response: external_protocol.CommandResponse) -> None:
        self._reset_session_checker_if_session_id_is_ok(command_response.sessionId)

        device_not_connected = (
            command_response.type == external_protocol.CommandResponse.Type.DEVICE_NOT_CONNECTED
        )
        commands = self._command_checker.pop_commands(command_response.messageCounter)
        for command, _ in commands:
            rc = self._modules[command.deviceCommand.device.module].command_ack(
                command.deviceCommand.commandData, command.deviceCommand.device
            )
            self._check_command_ack_rc(command.deviceCommand.device.module, rc)
            if device_not_connected and command.messageCounter == command_response.messageCounter:
                self._disconnect_device(DisconnectTypes.announced, command.deviceCommand.device)
                self._logger.warning(
                    f"Received Command response, which announces that device {command.deviceCommand.device.deviceName} was disconnected"
                )

    def _handle_command(self, module_num: int) -> None:
        command_counter = self._command_checker.counter
        command, for_device = self._modules_command_threads[module_num].pop_command()

        if not self._is_device_in_list(for_device, self._connected_devices):
            self._logger.warning(
                f"Command returned from module {module_num}'s API is intended for not connected device, command won't be sent"
            )
            return

        self._logger.info(f"Sending Command message, messageCounter: {command_counter}")
        external_command = MessageCreator.create_external_command(
            self._session_id, command_counter, for_device, command
        )

        if len(external_command.command.deviceCommand.commandData) == 0:
            self._logger.warning(
                f"Command data for device {external_command.command.deviceCommand.device.deviceName} is empty"
            )

        self._command_checker.add_command(external_command.command, True)

        if external_command.command.deviceCommand.device.module != module_num:
            self._logger.warning(
                f"Device id returned from module {module_num}'s API has different module number"
            )
            if self._config.send_invalid_command:
                self._logger.warning("Sending Command message with possibly invalid device")
                self._mqtt_client.publish(external_command)
            else:
                self._logger.warning("The Command will not be sent")
                self._command_checker.pop_commands(external_command.command.messageCounter)
        else:
            self._mqtt_client.publish(external_command)

    def _create_connect_response(
        self, connect_response_type: int
    ) -> external_protocol.ExternalServer:
        self._logger.info(
            f"Sending Connect response message, response type: {connect_response_type}"
        )
        return MessageCreator.create_connect_response(self._session_id, connect_response_type)

    def _create_status_response(
        self, status: external_protocol.Status
    ) -> external_protocol.ExternalServer:
        module = status.deviceStatus.device.module
        if module not in self._modules:
            self._logger.warning(f"Module {module} is not supported")
        self._logger.info(
            f"Sending Status response message, messageCounter: {status.messageCounter}"
        )
        return MessageCreator.create_status_response(status.sessionId, status.messageCounter)

    def _connect_device(self, device: internal_protocol.Device) -> int:
        rc = self._modules[device.module].device_connected(device)
        self._adjust_connection_state_of_module_thread(device.module, True)
        if rc == GeneralErrorCodes.OK:
            self._logger.info(
                f"Connected device unique identificator: {device.module}/{device.deviceType}/{device.deviceRole} named as {device.deviceName}"
            )
            self._connected_devices.append(device)
        else:
            self._logger.error(
                f"Device with unique identificator: {device.module}/{device.deviceType}/{device.deviceRole} "
                f"could not be connected, because response from api: {rc}"
            )
        return rc

    def _disconnect_device(
        self, disconnect_types: DisconnectTypes, device: internal_protocol.Device
    ) -> None:
        try:
            self._connected_devices.remove(device)
        except ValueError:
            return

        rc = self._modules[device.module].device_disconnected(disconnect_types, device)
        self._check_device_disconnected_rc(device.module, rc)
        self._adjust_connection_state_of_module_thread(device.module, False)

    def _is_device_in_list(self, device: internal_protocol.Device, list) -> bool:
        for list_device in list:
            if (
                list_device.module == device.module
                and list_device.deviceType == device.deviceType
                and list_device.deviceRole == device.deviceRole
            ):  # External server do not care about deviceName and priority
                return True

        return False

    def _adjust_connection_state_of_module_thread(self, device_module: int, connected: bool):
        for device in self._connected_devices:
            if device_module == device.module:
                # There is connected device with same module, no need to adjust the module thread
                return

        if connected:
            self._modules_command_threads[device_module].connection_established = True
        else:
            self._modules_command_threads[device_module].connection_established = False

    def _check_forward_status_rc(self, module_num: int, rc: int) -> None:
        if rc != GeneralErrorCodes.OK:
            self._logger.error(
                f"Module {module_num}: Error occurred in forward_status function, rc: {rc}"
            )

    def _check_device_disconnected_rc(self, module_num: int, rc: int) -> None:
        if rc != GeneralErrorCodes.OK:
            self._logger.error(
                f"Module {module_num}: Error occurred in device_disconnected function, rc: {rc}"
            )

    def _check_command_ack_rc(self, module_num: int, rc: int) -> None:
        if rc != GeneralErrorCodes.OK:
            self._logger.error(
                f"Module {module_num}: Error occurred in command_ack function, rc: {rc}"
            )

    def _reset_session_checker_if_session_id_is_ok(self, msg_session_id: str) -> None:
        if self._session_id == msg_session_id:
            self._session_checker.reset()

    def _clear_context(self) -> None:
        self._mqtt_client.stop()
        self._command_checker.reset()
        self._session_checker.stop()
        self._status_order_checker.reset()

        for device in self._connected_devices:
            rc = self._modules[device.module].device_disconnected(DisconnectTypes.timeout, device)
            self._check_device_disconnected_rc(device.module, rc)

        for command_thread in self._modules_command_threads.values():
            command_thread.connection_established = False

        self._connected_devices.clear()
        self._not_connected_devices.clear()
        self._event_queue.clear()

    def _clear_modules(self) -> None:
        for module_number in self._modules_command_threads:
            self._modules_command_threads[module_number].stop()

        for module_num in self._modules:
            self._modules_command_threads[module_num].wait_for_join()
            rc = self._modules[module_num].destroy()
            if rc != GeneralErrorCodes.OK:
                self._logger.error(
                    f"Module {module_num}: Error occurred in destroy function, rc: {rc}"
                )

        self._modules.clear()

    def stop(self) -> None:
        self._clear_modules()
        self._logger.info("Server stopped")
