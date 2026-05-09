# bincall 1.0 Specification

**bincall** (lowercase) is a basic binary call service. It allows users to call
other users and transfer bytes in realtime. What those bytes mean is not
bincall's concern.

1. Everyone is free to build and run their own version of a bincall server or
client.

2. A user is allowed to be in multiple calls at the same time.

3. Two unique users can only be in a single call with each other at a time. The
server must guarantee to prevent this from happening.

4. The server may serve static HTML pages, web apps, or anything else except for
the bincall API URL path which must handled as specified in this document. The
bincall API path is up to the server maintainer, for example:

```
https://example.com/panda/bincall
```

5. bincall uses unusual ways of designing an API. It avoids using custom URL
paths for methods (e.g. `https://example.com/bincall/method-name`), custom HTTP
headers, and some other features. These design choices have been made to make it
possible to implement bincall servers in strict environments that barely provide
enough functionality to make something resembling a server.

6. The server may provide additional methods, accept extra URL parameters, and
include more fields than specified in returned JSON objects to support extended
or custom functionality, as long as their names begin with an underscore (e.g.
"_custom-method"). However, it must ensure that clients relying solely on the
base API do not experience any loss of functionality, whether partial or
complete.

7. The server must relay raw byte data between parties in a call accurately and
without corruption. Byte order and values must never be altered, intentionally
or otherwise. If either the server or the client experiences irreversible data
loss or corruption at any point (whether incoming or outgoing), it must
terminate the connection immediately.

8. The server must store user data (ID, password hash, etc.) in a private and
secure place. It must only store salted hashes of user passwords and not their
raw values.

9. All HTTP(S) responses use status code 200.

10. All HTTP(S) requests use the GET method unless specified otherwise.

11. Every request must have a `method` field in its URL parameters specifying
which bincall method to call. Example:

```
https://example.com/bincall?method=dummy
```

# API

## auth

This method takes authentication parameters (user ID and password) and checks
if they are correct.

- If authentication is successful, it returns JSON `{ authResult: 'ok' }`.

- If authentication fails, it returns a JSON object where `authResult`
stores the fail reason.

- If no existing user matches the ID, the server can either try to create a new
one or provide a different method (e.g. "create-user") for creating users. If it
successfully creates a user within this method ("auth"), it must return
`{ authResult: 'ok-created' }`. If it fails to do so or provides a separate
method for user creation, it must return a JSON object where `authResult` stores
the fail reason.

### URL parameters

- **cred:** a base64-encoded representation of a byte array in the following
format:
1. [1 byte] number of bytes in the UTF-8 representation of the user ID
2. [N bytes] UTF-8 representation of the user ID
3. [1 byte] number of bytes in the UTF-8 representation of the password
4. [N bytes] UTF-8 representation of the password

### Example

```
https://example.com/bincall?method=auth&cred=BHRlc3QKdmVyeXNlY3VyZQ==
```

## delete-acc

This method deletes a user's account after authentication.

- Returns a JSON object where `authResult` stores the authentication result as
a string ("ok" if successful) and `deleteResult` stores the account deletion
result as a string ("ok" if successful, otherwise, the fail reason).

- If authentication fails, the JSON object will not have a `deleteResult` field.

- When an account is deleted, all calls in which that user participates must be
terminated immediately.

### URL parameters

- **cred:** same as in "auth"

### Example

```
https://example.com/bincall?method=delete-acc&cred=BHRlc3QKdmVyeXNlY3VyZQ==
```

## dummy

This method returns a random piece of plain text or HTML. The number of
characters is suggested but not required to be randomly generated in the range
[10, 1000].

- The response headers must set `Content-Type` to `text/plain` or `text/html`
depending on the content returned.

### Example

```
https://example.com/bincall?method=dummy
```

## whos-calling

This method returns the list of recent (not older than 60 seconds), unanswered
incoming calls for a user after authentication.

- Returns a JSON object where `authResult` stores the authentication result as
a string ("ok" if successful) and `calls` stores an array of `IncomingCall`
objects.

- If authentication fails, the JSON object will not have a `calls` field.

- An `IncomingCall` has the following structure:
```typescript
interface IncomingCall {
    caller: string;
    timestamp: number;
}
```

### URL parameters

- **cred:** same as in "auth"

### Example

```
https://example.com/bincall?method=whos-calling&cred=BHRlc3QKdmVyeXNlY3VyZQ==
```

## call (WebSocket request)

This method can only be used in a WebSocket request. After authenticating the
user, it sends an incoming call to the peer and waits for them to answer the
call.

- If authentication fails, it will send a WebSocket text frame saying
"auth-failed" and close the connection.

- If the peer ID is invalid, it will send text frame "peer-not-found" and close
the connection.

- If the peer doesn't answer the call in 60 seconds, it will send text frame
"call-failed" and close the connection.

- If the authenticated user already has an active call with the specified peer,
the server will send text frame "already-in-call-with-peer" and close the
connection.

- If the peer does answer the call, it will generate a random string as the call
key, send text frame "call-start#ID#KEY" where ID is replaced with a unique
integral identifier for the call and KEY is replaced with the call key (e.g.
"call-start#2384230072#pretty-flamingo-4255"), and start relaying data between
the two users using ARC4 encryption which is insecure and only used for
obfuscating the data. For relaying data, the server will keep receiving binary
frames (packets) from the client, decrypt them with ARC4 using the call key, and
forward them to the peer using any method applicable (global variables, sockets,
databases, files, distributed networks, etc.). At the same time, the server will
also keep receiving binary packets from the peer, encrypt them with ARC4 using
the call key, and send their data to the client. The process will continue until
either side (the client or the peer) closes the connection.

- The server is allowed to concatenate several packets, potentially changing the
original starting and ending indices, before relaying them, so users must not
expect packet boundaries to be preserved.

- If the peer closes the connection first, the server must forcefully terminate
the WebSocket client's connection with no closing handshake. If the WebSocket
client closes the connection first, the server must inform the peer that the
call has ended using any method applicable (global variables, sockets,
databases, files, distributed networks, etc.).

Example implementations of the ARC4 encryption algorithm are provided later in
the document.

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer

### Example

```
wss://example.com/bincall?method=call&cred=BHRlc3QKdmVyeXNlY3VyZQ==&peer=carrot-man
```

## pickup (WebSocket request)

This method can only be used in a WebSocket request. After authenticating the
user, it answers an incoming call from the peer.

- If authentication fails, it will send a WebSocket text frame saying
"auth-failed" and close the connection.

- If the peer ID is invalid, it will send text frame "peer-not-found" and close
the connection.

- If there is no incoming call from the peer, it will send text frame "no-call"
and close the connection.

- If the authenticated user already has an active call with the specified peer,
the server will send text frame "already-in-call-with-peer" and close the
connection.

- If nothing fails, it will generate a random string as the call
key, send text frame "call-start#ID#KEY" where ID is replaced with a unique
integral identifier for the call and KEY is replaced with the call key (e.g.
"call-start#2384230072#angry-chicken-9120"), and start relaying data between the
two users using ARC4 encryption which is insecure and only used for obfuscating
the data. For relaying data, the server will keep receiving binary frames
(packets) from the client, decrypt them with ARC4 using the call key, and
forward them to the peer using any method applicable (global variables, sockets,
databases, files, distributed networks, etc.). At the same time, the server will
also keep receiving binary packets from the peer, encrypt them with ARC4 using
the call key, and send their data to the client. The process will continue until
either side (the client or the peer) closes the connection.

- The server is allowed to concatenate several packets, potentially changing the
original starting and ending indices, before relaying them, so users must not
expect packet boundaries to be preserved.

- If the peer closes the connection first, the server must forcefully terminate
the WebSocket client's connection with no closing handshake. If the WebSocket
client closes the connection first, the server must inform the peer that the
call has ended using any method applicable (global variables, sockets,
databases, files, distributed networks, etc.).

Example implementations of the ARC4 encryption algorithm are provided later in
the document.

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer

### Example

```
wss://example.com/bincall?method=pickup&cred=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test
```

## call-http (HTTP(S) request)

This method provides the same functionality as "call" but uses HTTP(S) requests
instead of a single, persistent WebSocket connection. It returns a JSON object
where `result` stores the value of the text frame we would send in "call" (e.g.
"auth-failed" or "call-start#ID#KEY").

- Since the peer might take a while to answer the call, the server is allowed to
send any character except an opening curly bracket ({) or square bracket ([) to
the client every few seconds to prevent firewalls or other authoritative systems
from closing the connection before a response is ever sent. Clients must ignore
these characters and only start parsing after they read an opening curly bracket
({) or square bracket ([).

- If the call starts successfully, the client must call method "http-chunk"
regularly to send data to the peer and/or receive new data from it.

- The interval at which the client calls "http-chunk" must not be above 30
seconds.

- If the client does not call "http-chunk" for over 30 seconds, the server must
end the call.

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer

### Example

```
https://example.com/bincall?method=call-http&cred=BHRlc3QKdmVyeXNlY3VyZQ==&peer=carrot-man
```

## pickup-http (HTTP(S) request)

This method provides the same functionality as "pickup" but uses HTTP(S)
requests instead of a single, persistent WebSocket connection. It returns a
JSON object where `result` stores the value of the text frame we would send in
"pickup" (e.g. "auth-failed" or "call-start#ID#KEY").

- If the call starts successfully, the client must call method "http-chunk"
regularly to send data to the peer and/or receive new data from it.

- The interval at which the client calls "http-chunk" must not be above 30
seconds.

- If the client does not call "http-chunk" for over 30 seconds, the server must
end the call.

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer

### Example

```
https://example.com/bincall?method=pickup-http&cred=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test
```

## http-chunk (HTTP(S) request)

This method is called regularly by a client after a successful call start using
method "call-http" or "pickup-http". After authentication and finding the call
based on URL parameter "call-id", it relays data from the client to the peer and
vice versa.

- The request uses the POST HTTP method.

- The request and response headers must set `Content-Type` to
`application/octet-stream` and `Content-Length` to the number of bytes in the
request or response body (may be 0).

- The request body contains optional data to send to the peer.

- The response body uses the following binary format:

1. [2 bytes] number of bytes in the UTF-8 representation of the status message
2. [N bytes] UTF-8 representation of the status message
3. [4 bytes] number of bytes in data relayed from the peer
4. [N bytes] data relayed from the peer, encrypted with ARC4 using the call key

- If the request fails for any reason (authentication failure, invalid user ID,
etc.), the server must mention the fail reason in the status message. Otherwise,
it must use status message `ok` unless another value is specified based on the
rules below.

- If the request body is not empty, the server must decrypt it with ARC4 using
the call key and forward it to the peer using any method applicable.

- The client can ask the server to end the call by setting URL parameter
`end` to `1`. The server must then inform the peer that the call has ended using
any method applicable.

- If the call has ended or is not found based on given call ID, the server must
use status message `end`.

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer
- **call-id:** call ID included in the "call-start" message

### Example

```
https://example.com/bincall?method=http-chunk&cred=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test&call-id=2384230072
```

## http-chunk-b85 (HTTP(S) request)

This method provides the same functionality as "http-chunk" but uses ZeroMQ
Base‑85 (Z85) encoding to transfer data in text.

- The request uses the POST HTTP method.

- The request and response headers must set `Content-Type` to `text/plain` and
`Content-Length` to the number of characters in the body (may be 0).

- The request body contains optional data to send to the peer encoded in ZeroMQ
Base‑85 (Z85).

- The response body uses the same format as in "http-chunk" but encodes the data
in ZeroMQ Base‑85 (Z85).

### URL parameters

- **cred:** same as in "auth"
- **peer:** user ID of the peer
- **call-id:** call ID included in the "call-start" message

### Example

```
https://example.com/bincall?method=http-chunk-b85&cred=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test&call-id=2384230072
```

## connection-modes

This method returns a JSON object where `connectionModes` stores the list of
support connections modes by the server.

1. If WebSocket is supported (methods "call" and "pickup"), it must include
`websocket`.

2. If HTTP requests with binary data are supported for calls (methods
"call-http", "pickup-http", and "http-chunk"), it must include `http`.

3. If HTTP requests with Base85-encoded data are supported for calls (methods
"call-http", "pickup-http", and "http-chunk-b85"), it must include `http-b85`.

At least one mode must be supported (the list cannot be empty). Here's an
example:

```json
{
    connectionModes: ["websocket", "http", "http-b85"]
}
```

The server may support additional connection modes and list them here, as long
as the following requirements are met:

1. At least one of the standard modes mentioned above must be included.
2. Any custom or extended modes must begin with an underscore (e.g., `_udp`) to
prevent conflict with future modes in the official API.

### Example

```
https://example.com/bincall?method=connection-modes
```

# User ID and Password Validation

When creating a new user, the server must validate user IDs as the following:

1. A user ID must be a non-empty string with 1 to 64 characters.
2. It may only contain characters from the Latin alphabet (a-Z), Latin digits
(0-9), an underscore (\_), and a dash (-).
3. The first and last characters cannot be an underscore or dash.

When creating a new user, the server must validate passwords as the following:

1. A password must be a non-empty string with 10 to 64 unicode characters.
2. It must not contain more than 3 adjacent identical characters. For example,
"dolphin1112" is allowed but "dolphin1111" is not.

The server may implement more, stricter criteria for user ID and password
validation.

# Connection Modes

At least one of these sets of methods must be implemented:

1. "call" and "pickup" which use a single, persistent WebSocket connection for
calls.
2. "call-http", "pickup-http", and "http-chunk" which use regular HTTP requests
for calls.

# Call Keys

The server must use different calls keys for each user in a call to avoid direct
relaying of data between them.

# Appendix 1: ARC4 Implementation Example

Here's an example implementation of the ARC4 encryption algorithm in Python.

```python
def _rc4_crypt(data: bytes, key: bytes) -> bytes:
    # Key-scheduling algorithm (KSA)
    S = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % key_len]) & 0xFF
        S[i], S[j] = S[j], S[i]

    # Pseudo-random generation algorithm (PRGA)
    i = j = 0
    out = bytearray(len(data))
    for idx, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        k = S[(S[i] + S[j]) & 0xFF]
        out[idx] = byte ^ k
    return bytes(out)


def insecure_encrypt(data: bytes, key: str) -> bytes:
    """
    encrypt (obfuscate) binary data with the given key.

    NOTE: this is not a secure algorithm and is only used to prevent plain text
    triggers, especially when using non-secure HTTP connections.
    """
    return _rc4_crypt(data, key.encode('utf-8'))


def insecure_decrypt(data: bytes, key: str) -> bytes:
    """
    decrypt (identical operation for a stream cipher)

    NOTE: this is not a secure algorithm and is only used to prevent plain text
    triggers, especially when using non-secure HTTP connections.
    """
    return _rc4_crypt(data, key.encode('utf-8'))
```

And here's an example implementation in TypeScript.

```typescript
function rc4Crypt(data: Buffer, key: Buffer): Buffer {
    // KSA
    const S = Buffer.alloc(256);
    for (let i = 0; i < 256; i++) S[i] = i;
    let j = 0;
    const keyLen = key.length;
    for (let i = 0; i < 256; i++) {
        j = (j + S[i] + key[i % keyLen]) & 0xFF;
        // swap without temporary variable using destructuring
        [S[i], S[j]] = [S[j], S[i]];
    }

    // PRGA
    const out = Buffer.alloc(data.length);
    let i = 0;
    j = 0;
    for (let idx = 0; idx < data.length; idx++) {
        i = (i + 1) & 0xFF;
        j = (j + S[i]) & 0xFF;
        [S[i], S[j]] = [S[j], S[i]];
        const k = S[(S[i] + S[j]) & 0xFF];
        out[idx] = data[idx] ^ k;
    }
    return out;
}

// NOTE: this is not a secure algorithm and is only used to prevent plain text
// triggers, especially when using non-secure HTTP connections.
export function insecureEncrypt(data: Buffer, key: string): Buffer {
    return rc4Crypt(data, Buffer.from(key, 'utf-8'));
}

// NOTE: this is not a secure algorithm and is only used to prevent plain text
// triggers, especially when using non-secure HTTP connections.
export function insecureDecrypt(data: Buffer, key: string): Buffer {
    return rc4Crypt(data, Buffer.from(key, 'utf-8'));
}
```
