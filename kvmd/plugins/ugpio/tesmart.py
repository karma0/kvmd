# ========================================================================== #
#                                                                            #
#    KVMD - The main Pi-KVM daemon.                                          #
#                                                                            #
#    Copyright (C) 2018-2021  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import multiprocessing
import errno
import time

from typing import Tuple
from typing import Dict
from typing import Optional

import socket

from ...logging import get_logger

from ... import aiotools
from ... import aiomulti
from ... import aioproc

from ...yamlconf import Option

from ...validators.basic import valid_int_f1
from ...validators.net import valid_ip_or_host

from . import GpioDriverOfflineError
from . import BaseUserGpioDriver


# =====
class Plugin(BaseUserGpioDriver):  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        instance_name: str,
        notifier: aiotools.AioNotifier,

        host: str,
        port: int,
    ) -> None:

        super().__init__(instance_name, notifier)

        self.__host = host
        self.__port = port

        self.__ctl_queue: "multiprocessing.Queue[int]" = multiprocessing.Queue()
        self.__channel_queue: "multiprocessing.Queue[Optional[int]]" = multiprocessing.Queue()
        self.__channel: Optional[int] = -1

        self.__proc: Optional[multiprocessing.Process] = None
        self.__stop_event = multiprocessing.Event()

    @classmethod
    def get_plugin_options(cls) -> Dict:
        return {
            "host": Option("192.168.1.10", type=valid_ip_or_host, unpack_as="ip_or_host"),
            "port": Option(5000, type=valid_int_f1),
        }

    def register_input(self, pin: int, debounce: float) -> None:
        _ = pin
        _ = debounce

    def register_output(self, pin: int, initial: Optional[bool]) -> None:
        _ = pin
        _ = initial

    def prepare(self) -> None:
        assert self.__proc is None
        self.__proc = multiprocessing.Process(target=self.__socket_worker, daemon=True)
        self.__proc.start()

    async def run(self) -> None:
        while True:
            (got, channel) = await aiomulti.queue_get_last(self.__channel_queue, 1)
            if got and self.__channel != channel:
                self.__channel = channel
                await self._notifier.notify()

    def cleanup(self) -> None:
        if self.__proc is not None:
            if self.__proc.is_alive():
                get_logger(0).info("Stopping %s daemon ...", self)
                self.__stop_event.set()
            if self.__proc.exitcode is not None:
                self.__proc.join()

    def read(self, pin: int) -> bool:
        if not self.__is_online():
            raise GpioDriverOfflineError(self)
        return (self.__channel == pin)

    def write(self, pin: int, state: bool) -> None:
        if not self.__is_online():
            raise GpioDriverOfflineError(self)
        if state and (0 < pin <= 16):
            self.__ctl_queue.put_nowait(pin)

    # =====

    def __is_online(self) -> bool:
        return (
            self.__proc is not None
            and self.__proc.is_alive()
            and self.__channel is not None
        )

    def __socket_worker(self) -> None:
        logger = aioproc.settle(str(self), f"gpio-tesmart-{self._instance_name}")
        while not self.__stop_event.is_set():
            try:
                with self.__get_socket() as connection:
                    data = b""
                    self.__channel_queue.put_nowait(-1)

                    # Request and then recieve the state.
                    self.__request_channel(connection)

                    (channel, data) = self.__recv_channel(connection, data)
                    if channel is not None:
                        self.__channel_queue.put_nowait(channel)

                    (got, channel) = aiomulti.queue_get_last_sync(self.__ctl_queue, 0.1)  # type: ignore
                    if got:
                        assert channel is not None
                        self.__send_channel(connection, channel)

            except Exception as err:
                self.__channel_queue.put_nowait(None)
                if isinstance(err, OSError) and err.errno == errno.ENOENT:  # pylint: disable=no-member
                    logger.error("Missing %s socket device: %s", self, self.__host)
                else:
                    logger.exception("Unexpected %s error", self)
                time.sleep(1)

    def __get_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.__host, self.__port))
        return sock

    def __recv_channel(self, connection: socket.socket, data: bytes) -> Tuple[Optional[int], bytes]:
        channel: Optional[int] = None
        data += connection.recv(6)
        channel = int(data[-2])
        return (channel, data)

    def __request_channel(self, connection: socket.socket) -> None:
        connection.sendall(bytes([0xAA, 0xBB, 0x03, 0x10, 0x00, 0xEE]))

    def __send_channel(self, connection: socket.socket, channel: int) -> None:
        connection.sendall(bytes([0xAA, 0xBB, 0x03, 0x01, channel, 0xEE]))

    def __str__(self) -> str:
        return f"TESmart({self._instance_name})"

    __repr__ = __str__
