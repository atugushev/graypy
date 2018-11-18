#!/usr/bin/python
# -*- coding: utf-8 -*-

"""Logging Handlers that send messages in Graylog Extended Log Format (GELF)"""

import abc
import datetime
import json
import logging
import math
import random
import socket
import ssl
import struct
import sys
import traceback
import zlib
from logging.handlers import DatagramHandler, SocketHandler


WAN_CHUNK = 1420
LAN_CHUNK = 8154

if sys.version_info[0] == 3:  # check if python3+
    data, text = bytes, str
else:
    data, text = str, unicode  # pylint: disable=undefined-variable

# fixes for using ABC
if sys.version_info >= (3, 4):  # check if python3.4+
    ABC = abc.ABC
else:
    ABC = abc.ABCMeta(str('ABC'), (), {})

try:
    import httplib
except ImportError:
    import http.client as httplib

SYSLOG_LEVELS = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


class BaseGELFHandler(logging.Handler, ABC):
    """Abstract class noting the basic components of a GLEFHandler"""

    def __init__(self, chunk_size=WAN_CHUNK,
                 debugging_fields=True, extra_fields=True, fqdn=False,
                 localname=None, facility=None, level_names=False,
                 compress=True):
        """Initialize the BaseGELFHandler.

        :param chunk_size: Message chunk size. Messages larger than this
            size will be sent to graylog in multiple chunks. Defaults to
            ``WAN_CHUNK=1420``.
        :param debugging_fields: Send debug fields if true (the default).
        :param extra_fields: Send extra fields on the log record to graylog
            if set to :obj:`True`. (:obj:`True` by default)
        :param fqdn: Use fully qualified domain name of localhost as source
            host (:meth:`socket.getfqdn`).
        :param localname: Use specified hostname as source host.
        :param facility: Replace facility with specified value. If specified,
            record.name will be passed as `logger` parameter.
        :param level_names: Allows the use of string error level names instead
            of numerical values. (:obj:`False` by default)
        :param compress: Use message compression. (:obj:`True` by default)
        """
        logging.Handler.__init__(self)
        self.debugging_fields = debugging_fields
        self.extra_fields = extra_fields
        self.chunk_size = chunk_size

        if fqdn and localname:
            raise ValueError(
                "cannot specify 'fqdn' and 'localname' arguments together")

        self.fqdn = fqdn
        self.localname = localname
        self.facility = facility
        self.level_names = level_names
        self.compress = compress

    def makePickle(self, record):
        gelf_dict = self._make_gelf_dict(record)
        packed = self._pack_gelf_dict(gelf_dict)
        frame = zlib.compress(packed) if self.compress else packed
        return frame

    def _make_gelf_dict(self, record):
        """Create a dictionary representing a Graylog GELF log from a
        python :class:`logging.LogRecord`"""
        # construct the base GELF format
        gelf_dict = {
            'version': "1.0",
            'host': BaseGELFHandler._resolve_host(self.fqdn, self.localname),
            'short_message': self.formatter.format(record) if self.formatter else record.getMessage(),
            'timestamp': record.created,
            'level': SYSLOG_LEVELS.get(record.levelno, record.levelno),
            'facility': self.facility or record.name,
        }

        # add in specified optional extras
        self._add_full_message(gelf_dict, record)
        if self.level_names:
            self._add_level_names(gelf_dict, record)
        if self.facility is not None:
            self._set_custom_facility(gelf_dict, self.facility, record)
        if self.debugging_fields:
            self._add_debugging_fields(gelf_dict, record)
        if self.extra_fields:
            self._add_extra_fields(gelf_dict, record)
        return gelf_dict

    @staticmethod
    def _add_level_names(gelf_dict, record):
        """Add the ``level_name`` field to the ``gelf_dict`` which notes
        the logging level via the string error level names instead of
        numerical values"""
        gelf_dict['level_name'] = logging.getLevelName(record.levelno)

    @staticmethod
    def _set_custom_facility(gelf_dict, facility_value, record):
        """Set the ``gelf_dict``'s ``facility`` field to the specified value
        also add the the extra ``_logger`` field containing the log
        records name"""
        gelf_dict.update({"facility": facility_value, '_logger': record.name})

    @staticmethod
    def _add_full_message(gelf_dict, record):
        """Add the ``full_message`` field to the ``gelf_dict`` if any
        traceback information exists within the logging record"""
        # if a traceback exists add it to the log as the full_message field
        full_message = None
        # format exception information if present
        if record.exc_info:
            full_message = '\n'.join(
                traceback.format_exception(*record.exc_info))
        # use pre-formatted exception information in cases where the primary
        # exception information was removed, eg. for LogRecord serialization
        if record.exc_text:
            full_message = record.exc_text
        if full_message:
            gelf_dict["full_message"] = full_message

    @staticmethod
    def _resolve_host(fqdn=None, localname=None):
        """Resolve the ``host`` GELF field"""
        if fqdn:
            return socket.getfqdn()
        elif localname:
            return localname
        return socket.gethostname()

    @staticmethod
    def _add_debugging_fields(gelf_dict, record):
        """Add debugging fields to the given ``gelf_dict``"""
        gelf_dict.update({
            'file': record.pathname,
            'line': record.lineno,
            '_function': record.funcName,
            '_pid': record.process,
            '_thread_name': record.threadName,
        })
        # record.processName was added in Python 2.6.2
        pn = getattr(record, 'processName', None)
        if pn is not None:
            gelf_dict['_process_name'] = pn

    @staticmethod
    def _add_extra_fields(gelf_dict, record):
        """Add extra fields to the given ``gelf_dict``

        However, this does not add additional fields in to ``message_dict``
        that are either duplicated from standard :class:`logging.LogRecord`
        attributes, duplicated from the python logging module source
        (e.g. ``exc_text``), or violate GLEF format (i.e. ``id``).

        .. seealso::

            The list of standard :class:`logging.LogRecord` attributes can be
            found at:

                http://docs.python.org/library/logging.html#logrecord-attributes
        """

        # skip_list is used to filter additional fields in a log message.
        skip_list = (
            'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
            'funcName', 'id', 'levelname', 'levelno', 'lineno', 'module',
            'msecs', 'message', 'msg', 'name', 'pathname', 'process',
            'processName', 'relativeCreated', 'thread', 'threadName')

        for key, value in record.__dict__.items():
            if key not in skip_list and not key.startswith('_'):
                gelf_dict['_%s' % key] = value

    @staticmethod
    def _pack_gelf_dict(gelf_dict):
        """Convert a given ``gelf_dict`` to a JSON-encoded string, thus,
        creating an uncompressed GELF log ready for consumption by graylog.

        Since we cannot be 100% sure of what is contained in the ``gelf_dict``
        we have to do some sanitation.
        """
        gelf_dict = BaseGELFHandler._sanitize_to_unicode(gelf_dict)
        packed = json.dumps(gelf_dict, separators=',:', default=BaseGELFHandler._object_to_json)
        return packed.encode('utf-8')

    @staticmethod
    def _sanitize_to_unicode(obj):
        """Convert all strings records of the object to unicode"""
        if isinstance(obj, dict):
            return dict((BaseGELFHandler._sanitize_to_unicode(k), BaseGELFHandler._sanitize_to_unicode(v)) for k, v in obj.items())
        if isinstance(obj, (list, tuple)):
            return obj.__class__([BaseGELFHandler._sanitize_to_unicode(i) for i in obj])
        if isinstance(obj, data):
            obj = obj.decode('utf-8', errors='replace')
        return obj

    @staticmethod
    def _object_to_json(obj):
        """Convert objects that cannot be natively serialized into JSON
        into their string representation

        For datetime based objects convert them into their ISO formatted
        string as specified by :meth:`datetime.datetime.isoformat`.
        """
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return repr(obj)


class GELFUDPHandler(BaseGELFHandler, DatagramHandler):
    """Graylog Extended Log Format UDP handler"""

    def __init__(self, host, port=12202, **kwargs):
        """Initialize the GELFUDPHandler

        :param host: The host of the graylog server.
        :param port: The port of the graylog server (default ``12202``).
        """
        BaseGELFHandler.__init__(self, **kwargs)
        DatagramHandler.__init__(self, host, port)

    def send(self, s):
        if len(s) < self.chunk_size:
            DatagramHandler.send(self, s)
        else:
            for chunk in ChunkedGELF(s, self.chunk_size):
                DatagramHandler.send(self, chunk)


class GELFTCPHandler(BaseGELFHandler, SocketHandler):
    """Graylog Extended Log Format TCP handler"""

    def __init__(self, host, port=12201, **kwargs):
        """Initialize the GELFTCPHandler

        :param host: The host of the graylog server.
        :param port: The port of the graylog server (default ``12201``).
        """
        BaseGELFHandler.__init__(self, compress=False, **kwargs)
        SocketHandler.__init__(self, host, port)

    def makePickle(self, record):
        """Add a null terminator to a GELFTCPHandler's pickles as a TCP frame
        object needs to be null terminated"""
        return BaseGELFHandler.makePickle(self, record) + b'\x00'


class GELFTLSHandler(GELFTCPHandler):
    """Graylog Extended Log Format TCP handler with TLS support"""

    def __init__(self, host, port=12204, validate=False, ca_certs=None, certfile=None,
                 keyfile=None, **kwargs):
        """Initialize the GELFTLSHandler

        :param host: The host of the graylog server.
        :param port: The port of the graylog server (default ``12204``).
        :param validate: if true, validate server certificate.
            In that case specifying ``ca_certs`` is required.
        :param ca_certs: path to CA bundle file.
        :param certfile: path to the client certificate file.
        :param keyfile: path to the client private key. If the private key is
            stored with the certificate, this parameter can be ignored
        """

        if validate and ca_certs is None:
            raise ValueError('CA bundle file path must be specified')

        if keyfile is not None and certfile is None:
            raise ValueError('certfile must be specified')

        GELFTCPHandler.__init__(self, host=host, port=port, **kwargs)

        self.ca_certs = ca_certs
        self.reqs = ssl.CERT_REQUIRED if validate else ssl.CERT_NONE
        self.certfile = certfile
        self.keyfile = keyfile if keyfile else certfile

    def makeSocket(self, timeout=1):
        """Override SocketHandler.makeSocket, to allow creating wrapped
        TLS sockets"""
        plain_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        if hasattr(plain_socket, 'settimeout'):
            plain_socket.settimeout(timeout)

        wrapped_socket = ssl.wrap_socket(
            plain_socket,
            ca_certs=self.ca_certs,
            cert_reqs=self.reqs,
            keyfile=self.keyfile,
            certfile=self.certfile
        )
        wrapped_socket.connect((self.host, self.port))

        return wrapped_socket


# TODO: add https?
class GELFHTTPHandler(BaseGELFHandler):
    """Graylog Extended Log Format HTTP handler"""

    def __init__(self, host, port=12203, compress=True, path='/gelf',
                 timeout=5, **kwargs):
        """Initialize the GELFHTTPHandler

        :param host: GELF HTTP input host
        :param port: GELF HTTP input port
        :param compress: compress message before sending it to the server
            or not
        :param path: path of the HTTP input
            (http://docs.graylog.org/en/latest/pages/sending_data.html#gelf-via-http)
        :param timeout: amount of seconds that HTTP client should wait before
            it discards the request if the server doesn't respond
        """

        BaseGELFHandler.__init__(self, compress=compress, **kwargs)

        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self.headers = {}

        if compress:
            self.headers['Content-Encoding'] = 'gzip,deflate'

    def emit(self, record):
        """Emit the GELF record to graylog via an HTTP POST request"""
        data = self.makePickle(record)
        connection = httplib.HTTPConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout
        )
        connection.request('POST', self.path, data, self.headers)


class ChunkedGELF(object):
    """Class that chunks a message into a GLEF compatible chunks"""

    def __init__(self, message, size):
        """Initialize the ChunkedGELF message class

        :param message: The message to chunk.
        :param size: The size of the chunks.
        """
        self.message = message
        self.size = size
        self.pieces = struct.pack('B', int(math.ceil(len(message) * 1.0 / size)))
        self.id = struct.pack('Q', random.randint(0, 0xFFFFFFFFFFFFFFFF))

    def message_chunks(self):
        return (self.message[i:i + self.size] for i
                in range(0, len(self.message), self.size))

    def encode(self, sequence, chunk):
        return b''.join([
            b'\x1e\x0f',
            self.id,
            struct.pack('B', sequence),
            self.pieces,
            chunk
        ])

    def __iter__(self):
        for sequence, chunk in enumerate(self.message_chunks()):
            yield self.encode(sequence, chunk)
