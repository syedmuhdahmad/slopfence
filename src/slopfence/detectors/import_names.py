"""Which PyPI distributions provide an import name (#16).

Most distributions are imported under their own name, or a predictable variant
of it (``python-dateutil`` -> ``dateutil``, ``pyyaml`` -> ``yaml``,
``google-cloud-storage`` -> ``google``). ``provides`` handles those by rule.
``IMPORT_NAMES`` lists the rest: imports whose distribution has an unrelated
name. Its keys are also known to be real, so they are never looked up on PyPI.

``is_stdlib`` says which imports Python itself provides, on any version of it.
"""

from __future__ import annotations

import re
import sys

from slopfence.registry import normalize

IMPORT_NAMES: dict[str, tuple[str, ...]] = {
    "_pytest": ("pytest",),
    "adafruit_blinka": ("adafruit-blinka",),
    "allauth": ("django-allauth",),
    "apiclient": ("google-api-python-client",),
    "argon2": ("argon2-cffi",),
    "attr": ("attrs",),
    "Bio": ("biopython",),
    "board": ("adafruit-blinka",),
    "bs4": ("beautifulsoup4",),
    "bson": ("pymongo",),
    "busio": ("adafruit-blinka",),
    "cairo": ("pycairo",),
    "community": ("python-louvain",),
    "corsheaders": ("django-cors-headers",),
    "crispy_forms": ("django-crispy-forms",),
    "Crypto": ("pycryptodome", "pycrypto"),
    "Cryptodome": ("pycryptodomex",),
    "cv2": (
        "opencv-python",
        "opencv-contrib-python",
        "opencv-python-headless",
        "opencv-contrib-python-headless",
    ),
    "dateutil": ("python-dateutil",),
    "debug_toolbar": ("django-debug-toolbar",),
    "decouple": ("python-decouple",),
    "digitalio": ("adafruit-blinka",),
    "distutils": ("setuptools",),
    "django_filters": ("django-filter",),
    "dns": ("dnspython",),
    "docx": ("python-docx",),
    "dotenv": ("python-dotenv",),
    "editor": ("python-editor",),
    "engineio": ("python-engineio",),
    "environ": ("django-environ",),
    "factory": ("factory-boy",),
    "faiss": ("faiss-cpu", "faiss-gpu"),
    "fitz": ("pymupdf",),
    "gi": ("pygobject",),
    "git": ("gitpython",),
    "google": ("protobuf", "google-api-core", "googleapis-common-protos"),
    "googleapiclient": ("google-api-python-client",),
    "gridfs": ("pymongo",),
    "grpc": ("grpcio",),
    "igraph": ("python-igraph", "igraph"),
    "imblearn": ("imbalanced-learn",),
    "jose": ("python-jose",),
    "jwt": ("pyjwt",),
    "kafka": ("kafka-python",),
    "keras": ("keras", "tensorflow"),
    "ldap": ("python-ldap",),
    "Levenshtein": ("levenshtein", "python-levenshtein"),
    "libfuturize": ("future",),
    "libpasteurize": ("future",),
    "magic": ("python-magic",),
    "memcache": ("python-memcached",),
    "mpl_toolkits": ("matplotlib",),
    "multipart": ("python-multipart",),
    "MySQLdb": ("mysqlclient",),
    "nacl": ("pynacl",),
    "newspaper": ("newspaper3k",),
    "OpenGL": ("pyopengl",),
    "OpenSSL": ("pyopenssl",),
    "osgeo": ("gdal",),
    "past": ("future",),
    "PIL": ("pillow",),
    "pkg_resources": ("setuptools",),
    "pptx": ("python-pptx",),
    "psycopg2": ("psycopg2", "psycopg2-binary"),
    "pylab": ("matplotlib",),
    "pythoncom": ("pywin32",),
    "pywintypes": ("pywin32",),
    "pywt": ("pywavelets",),
    "rest_framework": ("djangorestframework",),
    "rest_framework_simplejwt": ("djangorestframework-simplejwt",),
    "RPi": ("rpi-gpio",),
    "serial": ("pyserial",),
    "skimage": ("scikit-image",),
    "sklearn": ("scikit-learn",),
    "skopt": ("scikit-optimize",),
    "slugify": ("python-slugify",),
    "socketio": ("python-socketio",),
    "socks": ("pysocks",),
    "storages": ("django-storages",),
    "telegram": ("python-telegram-bot",),
    "tflite_runtime": ("tflite-runtime",),
    "umap": ("umap-learn",),
    "usb": ("pyusb",),
    "websocket": ("websocket-client",),
    "win32api": ("pywin32",),
    "win32com": ("pywin32",),
    "win32con": ("pywin32",),
    "win32gui": ("pywin32",),
    "win32process": ("pywin32",),
    "wx": ("wxpython",),
    "xdist": ("pytest-xdist",),
    "Xlib": ("python-xlib",),
    "yaml": ("pyyaml",),
    "zmq": ("pyzmq",),
    # Imported under their own name, but too common to look up every time.
    "jinja2": ("jinja2",),
    "lxml": ("lxml",),
    "markdown": ("markdown",),
    "pydantic_core": ("pydantic-core",),
    "pytest": ("pytest",),
    "sentry_sdk": ("sentry-sdk",),
    "setuptools": ("setuptools",),
    "six": ("six",),
    "tensorflow": ("tensorflow",),
    "torch": ("torch",),
    "torchvision": ("torchvision",),
    "typing_extensions": ("typing-extensions",),
}

# Modules that are part of the standard library of some Python 3 and not of every one
# that slopfence runs on (3.10 to 3.14). Most were removed: ``imp``, ``asyncore``,
# ``distutils``, ``lib2to3``, the modules of PEP 594, and the ones that went before
# Python 3.10 (``formatter``, ``parser``, ``symbol``, ``macpath``...). The others were
# added after 3.10 (``tomllib``, ``compression``, ``annotationlib``).
# ``sys.stdlib_module_names`` only knows the Python that is running, and code is often
# written for another one.
STDLIB_ON_OTHER_PYTHONS = frozenset(
    {
        "aifc", "annotationlib", "asynchat", "asyncore", "audioop", "binhex", "cgi",
        "cgitb", "chunk", "compression", "crypt", "distutils", "dummy_threading",
        "formatter", "fpectl", "imghdr", "imp", "lib2to3", "macpath", "mailcap", "msilib",
        "nis", "nntplib", "os2emxpath", "ossaudiodev", "parser", "pipes", "smtpd", "sndhdr",
        "spwd", "sunau", "symbol", "telnetlib", "tomllib", "uu", "xdrlib",
        "_android_support", "_apple_support", "_ast_unparse", "_bootsubprocess",
        "_colorize", "_compression", "_crypt", "_dummy_thread", "_hmac", "_interpchannels",
        "_interpqueues", "_interpreters", "_ios_support", "_msi", "_opcode_metadata",
        "_py_warnings", "_pydatetime", "_pylong", "_pyrepl", "_remote_debugging", "_sha2",
        "_sha256", "_sha512", "_suggestions", "_sysconfig", "_tokenize", "_types",
        "_typing", "_wmi", "_zstd",
    }
)  # fmt: skip

_PREFIXES = ("python-", "py-", "py")
_SUFFIXES = ("-python", "python", "-py", "-py3", "-binary", "-headless")


def is_stdlib(module: str) -> bool:
    """Whether a top-level module comes with Python: with the one that is running, or
    with any other Python 3.

    The answer must not depend on the Python that slopfence runs on. ``import imp``
    is no invented package on Python 3.14, where the module is gone, and
    ``import tomllib`` is none on Python 3.10, which does not have it yet.
    """
    return (
        module in sys.stdlib_module_names
        or module in sys.builtin_module_names
        or module == "__future__"
        or module in STDLIB_ON_OTHER_PYTHONS
    )


def _squash(name: str) -> str:
    """Lowercase with all separators removed (``Speech_Recognition`` -> ``speechrecognition``)."""
    return re.sub(r"[-_.]+", "", name).lower()


def provides(distribution: str, module: str) -> bool:
    """Whether installing ``distribution`` plausibly provides ``import module``."""
    dist, mod = normalize(distribution), normalize(module)
    if _squash(dist) == _squash(mod):
        return True
    if dist in IMPORT_NAMES.get(module, ()):
        return True
    # Namespace packages: google-cloud-storage, azure-storage-blob, zope.interface.
    if dist.startswith(mod + "-"):
        return True
    for prefix in _PREFIXES:
        if dist.startswith(prefix) and _squash(dist[len(prefix) :]) == _squash(mod):
            return True
    return any(
        dist.endswith(suffix) and _squash(dist[: -len(suffix)]) == _squash(mod)
        for suffix in _SUFFIXES
    )
