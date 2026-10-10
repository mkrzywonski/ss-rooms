import logging
import re


class RedactTokenPaths(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r"/verify/[^/\s]+", "/verify/[redacted]", message)
        message = re.sub(r"/accounts/reset/[^/\s]+/[^/\s]+", "/accounts/reset/[redacted]", message)
        message = re.sub(
            r"/account/email/confirm/[^/\s]+", "/account/email/confirm/[redacted]", message
        )
        record.msg = message
        record.args = ()
        return True
