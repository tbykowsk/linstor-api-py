from .linstorapi import LinstorError, LinstorNetworkError, LinstorTimeoutError
from .linstorapi import ObjectIdentifier
from .linstorapi import ApiCallResponse, ErrorReport
from .linstorapi import Linstor
from . import sharedconsts as consts

VERSION = "0.2.1"

try:
    from linstor.consts_githash import GITHASH
except ImportError:
    GITHASH = 'GIT-hash: UNKNOWN'
