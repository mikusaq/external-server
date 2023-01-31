from abc import ABC, abstractmethod

import external_server.protobuf.InternalProtocol_pb2 as internal_protocol
import external_server.protobuf.ExternalProtocol_pb2 as external_protocol


class MessageCreator(ABC):

    def create_connect_response(self, session_id: str, connect_response_type: external_protocol.ConnectResponse.Type)\
            -> external_protocol.ConnectResponse:
        connect_response = external_protocol.ConnectResponse()
        connect_response.sessionId = session_id
        connect_response.type = connect_response_type
        return connect_response

    def create_status_response(self, session_id: str, message_counter: int) -> external_protocol.StatusResponse:
        status_response = external_protocol.StatusResponse()
        status_response.sessionId = session_id
        status_response.type = external_protocol.StatusResponse.Type.OK
        status_response.messageCounter = message_counter
        return status_response

    def create_external_command(self, session_id: str, counter: int,
                                status: internal_protocol.DeviceStatus) -> external_protocol.Command:
        command = external_protocol.Command()
        command.sessionId = session_id
        command.messageCounter = counter
        command.device.CopyFrom(status.device)
        command.deviceCommand.CopyFrom(self._create_internal_command(status))
        return command

    @abstractmethod
    def _create_internal_command(self, status: internal_protocol.DeviceStatus) -> internal_protocol.DeviceCommand:
        pass