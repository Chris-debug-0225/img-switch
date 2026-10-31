"""Use the bundled CA roots when the caller has not chosen a CA file."""
import os
import sys

if 'SSL_CERT_FILE' not in os.environ:
    os.environ['SSL_CERT_FILE'] = os.path.join(sys._MEIPASS, 'certifi', 'cacert.pem')
