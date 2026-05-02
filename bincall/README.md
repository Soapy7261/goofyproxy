# bincall Specification

**bincall** (lowercase) is a basic binary call service. It allows users to call
other users and transfer bytes in realtime. What those bytes mean is not
bincall's concern.

1. Everyone is free to build and run their own version of a bincall server or
client.

2. A user is allowed to be in multiple calls at the same time.

3. Two unique users can only be in a single call with each other at a time. The
server must guarantee to prevent this from happening.

4. The server may serve static HTML pages, web apps, or anything else for URL
paths outside the bincall API. However, if the request path matches one of the
bincall API methods, it must handle it as specified in this document. The root
path of the bincall API is up to you, for example:

```
https://example.com/panda/bincall/
```

5. The server may provide additional methods, accept extra URL parameters, and
include more fields than specified in returned JSON objects to support extended
or custom functionality. However, it must ensure that clients relying solely on
the base API do not experience any loss of functionality, whether partial or
complete.

# API

## authenticate

This method takes authentication parameters (user ID and password) and checks
if they are correct.

- If authentication is successful, it returns JSON `{ authResult: 'ok' }`.

- If authentication fails, it returns a JSON object where `authResult`
stores the fail reason.

- If no existing user matches the ID, the server can either try to create a new
one or provide a different method (e.g. "create-user") for creating users. If it
successfully creates a user within this method ("authenticate"), it must return
`{ authResult: 'ok-created' }`. If it fails to do so or provides a separate
method for user creation, it must return a JSON object where `authResult` stores
the fail reason.

### URL parameters

- **auth:** a base64-encoded representation of a byte array in the following
format:
1. [1 byte] number of bytes in the UTF-8 representation of the user ID
2. [N bytes] UTF-8 representation of the user ID
3. [1 byte] number of bytes in the UTF-8 representation of the password
4. [N bytes] UTF-8 representation of the password

### Example

```
https://example.com/bincall/authenticate?auth=BHRlc3QKdmVyeXNlY3VyZQ==
```

## delete-acc

This method deletes a user's account after authentication.

- Returns a JSON object where `authResult` stores the authentication result as
a string ("ok" if successful) and `deleteResult` stores the account deletion
result as a string ("ok" if successful, otherwise, the fail reason).

- If authentication fails, the JSON object will not have a `deleteResult` field.

### URL parameters

- **auth:** same as in "authenticate"

### Example

```
https://example.com/bincall/delete-acc?auth=BHRlc3QKdmVyeXNlY3VyZQ==
```

## dummy

This method returns a random byte array. The number of bytes is suggested but
not required to be randomly generated in the range [10, 1000].

### Example

```
https://example.com/bincall/dummy
```

## whos-calling

This method returns the list of recent (not older than 60 seconds) incoming
calls for a user after authentication.

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

- **auth:** same as in "authenticate"

### Example

```
https://example.com/bincall/whos-calling?auth=BHRlc3QKdmVyeXNlY3VyZQ==
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

- If the user and the peer are already in a different call, it will send text
frame "already-in-call" and close the connection.

- If the peer does answer the call, it will generate a random string as the call
key, send text frame "call-start-" followed by the key (e.g.
"call-start-pretty-flamingo-425"), and start relaying data between the two users
using ARC4 encryption which is insecure and only used for obfuscating the data.
For relaying data, the server will keep receiving binary frames from the
WebSocket client, decrypt them with ARC4 using the call key, and forward them to
the peer using any method applicable (global variables, sockets, files,
distributed networks, etc.). At the same time, the server will also keep
receiving binary packets from the peer, encrypt them with ARC4 using the call
key, and send them to the WebSocket client. The process will continue until
either side (the WebSocket client or the peer) closes the connection. If the
peer closes the connection first, the server must forcefully terminate the
WebSocket client's connection with no closing handshake. If the WebSocket client
closes the connection first, the server must inform the peer that the call has
ended using any method applicable.

Example implementations of the ARC4 encryption algorithm are provided later in
the document.

### URL parameters

- **auth:** same as in "authenticate"
- **peer:** user ID of the peer

### Example

```
wss://example.com/bincall/call?auth=BHRlc3QKdmVyeXNlY3VyZQ==&peer=carrot-man
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

- If the user and the peer are already in a different call, it will send text
frame "already-in-call" and close the connection.

- If nothing fails, it will generate a random string as the call key, send text
frame "call-start-" followed by the key (e.g. "call-start-angry-chicken-912"),
and start relaying data between the two users
using ARC4 encryption which is insecure and only used for obfuscating the data.
For relaying data, the server will keep receiving binary frames from the
WebSocket client, decrypt them with ARC4 using the call key, and forward them to
the peer using any method applicable (global variables, sockets, files,
distributed networks, etc.). At the same time, the server will also keep
receiving binary packets from the peer, encrypt them with ARC4 using the call
key, and send them to the WebSocket client. The process will continue until
either side (the WebSocket client or the peer) closes the connection. If the
peer closes the connection first, the server must forcefully terminate the
WebSocket client's connection with no closing handshake. If the WebSocket client
closes the connection first, the server must inform the peer that the call has
ended using any method applicable.

Example implementations of the ARC4 encryption algorithm are provided later in
the document.

### URL parameters

- **auth:** same as in "authenticate"
- **peer:** user ID of the peer

### Example

```
wss://example.com/bincall/pickup?auth=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test
```

# ARC4

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
