import msgpack

from msgpackrpc import Loop
from msgpackrpc import message
from msgpackrpc.error import RPCError
from msgpackrpc.future import Future
from msgpackrpc.transport import tcp
from msgpackrpc.compat import iteritems

from tornado.ioloop import IOLoop

class Session(object):
    """\
    Session processes send/recv request of the message, by using underlying
    transport layer.

    self._request_table(request table) stores the relationship between messageid and
    corresponding future. When the new requets are sent, the Session generates
    new message id and new future. Then the Session registers them to request table.

    When it receives the message, the Session lookups the request table and set the
    result to the corresponding future.
    """

    def __init__(self, address, timeout, loop=None, builder=tcp, reconnect_limit=5, pack_encoding='utf-8', unpack_encoding=None, tornado=False):
        """\
        :param address: address of the server.
        :param loop:    context object.
        :param builder: builder for creating transport layer
        """

        self._loop = loop or Loop()
        self._address = address
        self._timeout = timeout
        self._transport = builder.ClientTransport(self, self._address, reconnect_limit, encodings=(pack_encoding, unpack_encoding))
        self._generator = _NoSyncIDGenerator()
        self._request_table = {}
        self._tornado = tornado
        self._request_callbacks = {}

    @property
    def address(self):
        return self._address

    def call(self, method, *args):
        return self.send_request(method, args).get()

    def call_async(self, method, *args):
        return self.send_request(method, args)

    def call_with_callback(self, method, *args, **kwargs): # tomotaka
        callback = kwargs['callback']
        msgid = next(self._generator)
        self._request_callbacks[msgid] = callback
        self._transport.send_message([message.REQUEST, msgid, method, args])

    def send_request(self, method, args):
        # need lock?
        msgid = next(self._generator)
        future = Future(self._loop, self._timeout)
        self._request_table[msgid] = future
        self._transport.send_message([message.REQUEST, msgid, method, args])
        return future

    def notify(self, method, *args):
        def callback():
            self.stop_loop()
        
        self._transport.send_message([message.NOTIFY, method, args], callback=callback)
        self.start_loop()

    def close(self):
        if self._transport:
            self._transport.close()
        self._transport = None
        self._request_table = {}
        self._request_callbacks = {}

    def on_connect_failed(self, reason):
        """
        The callback called when the connection failed.
        Called by the transport layer.
        """
        # set error for all requests
        for msgid, future in iteritems(self._request_table):
            future.set_error(reason)

        self._request_table = {}
        self.close()
        self.stop_loop()

    def on_response(self, msgid, error, result):
        """\
        The callback called when the message arrives.
        Called by the transport layer.
        """

        if (not msgid in self._request_table) and (not msgid in self._request_callbacks):
            # TODO: Check timed-out msgid?
            #raise RPCError("Unknown msgid: id = {0}".format(msgid))
            return

        if msgid in self._request_table:
            future = self._request_table.pop(msgid)

            if error is not None:
                future.set_error(error)
            else:
                future.set_result(result)
        elif msgid in self._request_callbacks:
            callback = self._request_callbacks.pop(msgid)
            
            if error is not None:
                callback(None)
            else:
                callback(result)

        self.stop_loop()

    def on_timeout(self, msgid):
        future = self._request_table.pop(timeout)
        future.set_error("Request timed out")

    def step_timeout(self):
        timeouts = []
        for msgid, future in iteritems(self._request_table):
            if future.step_timeout():
                timeouts.append(msgid)

        if len(timeouts) == 0:
            return

        if not self._tornado:
            self._loop.stop()
        for timeout in timeouts:
            future = self._request_table.pop(timeout)
            future.set_error("Request timed out")
        if not self._tornado:
            self._loop.start()

    def start_loop(self):
        if not self._tornado:
            self._loop.start()

    def stop_loop(self):
        if not self._tornado:
            self._loop.stop()


def _NoSyncIDGenerator():
    """
    Message ID Generator.

    NOTE: Don't use in multithread. If you want use this
    in multithreaded application, use lock.
    """
    counter = 0
    while True:
        yield counter
        counter += 1
        if counter > (1 << 30):
            counter = 0
