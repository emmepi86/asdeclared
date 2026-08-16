"""The primary was migrated to parse_v2. The retry was not."""


def run(parser, payload):
    return parser.parse_v2(payload)


def retry(parser, payload):
    # exercised precisely when run() just failed
    return parser.parse_v1(payload)
