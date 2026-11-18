"""Shared message sizes and request timeouts; Lua copies are checked by tests."""

# CC:Tweaked's http.websocket_message defaults to 131072 bytes in both directions.
# bridge.lua's MAX_MESSAGE leaves margin below that cap for results it sends;
# it halves output/data until the encoded result fits. Independently, relay.py's
# MAX_REQ_BYTES leaves margin below CC's cap for requests it sends: json.dumps
# escapes to ASCII, so bytes == characters; larger requests get error kind too_large.
# bridge_client.py starts push chunks at PUSH_CHUNK_CHARS, halving until the
# JSON-escaped chunk fits PUSH_CHUNK_JSON_BYTES (stopping at PUSH_CHUNK_MIN_CHARS).
# REQUEST_ENVELOPE_ALLOWANCE reserves room below MAX_REQ_BYTES for the path and
# other request fields. OUTPUT_LIMIT / READ_LIMIT cap captured output / one read
# before encoding; the bridge's halving handles the remaining result size overhead.
# RELAY_WS_MAX_MSG_SIZE is bigger than CC's cap so the relay is not the bottleneck:
# aiohttp closes the whole websocket on oversized input, dropping the computer
# instead of returning an error. MAX_MESSAGE, OUTPUT_LIMIT, READ_LIMIT and
# DEFAULT_TIMEOUT are mirrored in bridge.lua; tests/test_limits.py checks they match.
CC_WEBSOCKET_CAP = 131_072
MAX_MESSAGE = 120_000
MAX_REQ_BYTES = 100_000
PUSH_CHUNK_CHARS = 60_000
PUSH_CHUNK_JSON_BYTES = 75_000
PUSH_CHUNK_MIN_CHARS = 500
REQUEST_ENVELOPE_ALLOWANCE = 20_000
OUTPUT_LIMIT = 60_000
READ_LIMIT = 60_000
RELAY_WS_MAX_MSG_SIZE = 256 * 1024
# The file-system tools (cc_lua.FS_PRELUDE) drop list entries until their reply, as the bridge encodes it, fits
# FS_REPLY_LIMIT: below MAX_MESSAGE, because the bridge can shorten output and file data but not return values.
FS_REPLY_LIMIT = 100_000

# Bridge timer < relay wait < HTTP client timeout, so each layer can report its
# own timeout error before the outer layer gives up. Margins add to the request timeout.
# NETWORK_TIMEOUT is cc_network's default: it walks every peripheral (and its methods) in one request.
DEFAULT_TIMEOUT = 30
NETWORK_TIMEOUT = 60
RELAY_TIMEOUT_MARGIN = 5
CLIENT_TIMEOUT_MARGIN = 15
