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
https://example.com/flamingo/bincall/
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

This method returns a random byte array.

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

- If the peer does answer the call, it will send text frame "call-start" and
start relaying data between the two users. Specifically, it will keep receiving
binary frames from the client and forward them to the peer and vice versa until
either side closes the connection.

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

- If nothing fails, it will send text frame "call-start" and start relaying data
between the two users. Specifically, it will keep receiving binary frames from
the client and forward them to the peer and vice versa until either side closes
the connection.

### URL parameters

- **auth:** same as in "authenticate"
- **peer:** user ID of the peer

### Example

```
wss://example.com/bincall/pickup?auth=CmNhcnJvdC1tYW4KdmVyeWNhcnJvdA==&peer=test
```
