Write a Node.js TypeScript app that does the following:

0. For managing users, we will use the file system (root directory specified as a constant in the code, e.g. ./users). We will only have a handful of users (2-5) so a database is overkill and not needed here. Every user will be a directory and have a user.json file inside the directory (e.g. ./users/carrot-man/user.json) describing its id, password hash, and incoming calls list.

1. Start an HTTPS server on port 443. Always drop non-secure HTTP.

2. Serve a default HTML page from a file (the path is a constant in the code) unless a special path is asked for in the request (mentioned below).

3. If the request path matches "/wsio/prepare", do the following:

  3.1. Read URL params named "auth" and "peer".

  3.2. "peer" doesn't need any further proccessing, but "auth" should be decoded with base64 into raw bytes. The first byte represents the number of bytes in the UTF-8 representation of "user_id", so read that many more bytes from the raw bytes and decode with UTF-8 to get user_id. After that, another single byte represents the number of bytes in the UTF-8 representation of "password", so read that many bytes and decode with UTF-8 to get password.

  3.3. Validate peer (user iD validation explained at the end) and return this json if validation fails: { "auth_result": 0, "peer_check_result": 0 }. Then, check if a user exists with ID matching peer. If not, return json { "auth_result": 0, "peer_check_result": 0 }.

  3.4. Validate user_id. If failed, return json { "auth_result": 0, "peer_check_result": 1 }. Then, check if a user exists with ID matching user_id. If it exists, hash the password and compare with the stored hash. If the password checks out, return json { "auth_result": 1, "peer_check_result": 1 }. If the password is incorrect, return json { "auth_result": 0, "peer_check_result": 1 }. If a user with ID matching user_id doesn't exist in the first place, create one with the given password after validating it (hash it, of course) and return json { "auth_result": 2, "peer_check_result": 1 }. If the password couldn't be validated, return json { "auth_result": 3, "peer_check_result": 1 }.

4. If the request path matches "/wsio/dummy", return N random bytes (N varying randomly from 10 to 1000).

5. If the request path matches "/wsio/call" and asks for a WebSocket upgrade, start the WebSocket connection and do the following:

  5.1. Read URL params "auth" and "peer" and decode auth exactly as before. Verify that user_id matches an existing user and verify the password. If failed, send WebSocket text frame saying "auth-failed" and close the connection.

  5.2. Find user with ID matching peer_id and append { caller: user_id, timestamp: NOW, answered: false } to its "incoming_calls" field (which is an array). If no user matches peer_id or peer_id===user_id, send text frame "peer-not-found" and close.

  5.3. Poll the incoming_calls field of the peer every 1 second. If the element with our ID and the timestamp we used is removed or the "answered" field stays false for longer than 60 seconds, send text frame "call-failed" and close.

  5.4. If answered becomes true, remove the element in the peer's incoming_calls array to avoid clutter. Then, send text frame "call-start".

  5.5. Create a new empty file named `${peer}@${call.timestamp}-0` in a directory named "call-outbox" inside the user's directory (e.g. "./users/the-caller/call-outbox/the-callee@1777303873807-0") (note that the 0 at the end is the packet index and the 0th packet is always empty). Then, wait for a file named `${user_id}@${call.timestamp}-0` to be created in the call-outbox directory of the peer. If it doesn't get created after 60 seconds, close the WebSocket connection. Once it does get created, set variables in_packet_idx and out_packet_idx both to 1. Then, start a receive loop like the following:
    1. Check files in the peer's call-outbox directory whose names start with `${user_id}@${call.timestamp}-`. Extract the number after the dash at the end which is the packet index. If it's smaller than in_packet_idx, delete the file. If it's equal to in_packet_idx, read its raw bytes and send it as a binary frame in the WebSocket connection and increment in_pakcet_idx and delete the file. If the number is larger than in_packet_idx, do nothing. If the filename ends with "-close" (e.g. the-caller@1777303873807-close), don't read its content, instead close the WebSocket connection and then break the loop (and don't forget to delete the file itself).
    2. If the WebSocket connection is closed, stop the loop.
    3. Sleep a little if needed for async (or don't if it's unnecessary)
    4. Repeat
  And in the WebSocket's message event, whenever we receive a binary frame, write the raw bytes to a new file named `${peer}@${call.timestamp}-${out_packet_idx}` in our call-outbox directory and then increment out_packet_idx.
  If the WebSocket connection gets closed from the other side (by the original client), create an empty file named `${peer}@${call.timestamp}-${out_packet_idx}-close`.

6. If the request path matches "/wsio/whos-calling", read URL param "auth", decode user_id and password, and verify. If user_id doesn't exist or the password is incorrect, return json { "auth_result": 0 }. If verified, get the incoming_calls field of the user matching user_id, filter by "timestamp > 60 seconds ago && !answered" and return json { "auth_result": 1, "calls": [...] } containing the list of incoming calls (each with a user_id and a timestamp). Make sure to remove any elements whose timestamps are older than 60 seconds.

7. If the request path matches "/wsio/pickup" and asks for a WebSocket upgrade, start the WebSocket connection and do the following:

  7.1. Read URL params "auth" and "peer" and authenticate just like before. If user_id doesn't exist or the password is incorrect, send text frame "auth-failed" and close. If no user matches peer_id, send text frame "peer-not-found" and close.

  7.2. Get the peer's incoming_calls field and find element with user_id matching peer. If not found, send text frame "no-call" and close. Set answered to true. Send WebSocket text frame "call-start".

  7.3. Create a new empty file named `${peer}@${call.timestamp}-0` in a directory named "call-outbox" inside the user's directory (e.g. "./users/the-callee/call-outbox/the-caller@1777303873807-0") (note that the 0 at the end is the packet index and the 0th packet is always empty). Then, wait for a file named `${user_id}@${call.timestamp}-0` to be created in the call-outbox directory of the peer. If it doesn't get created after 60 seconds, close the WebSocket connection. Once it does get created, set variables in_packet_idx and out_packet_idx both to 1. Then, start a receive loop like the following:
    1. Check files in the peer's call-outbox directory whose names start with `${user_id}@${call.timestamp}-`. Extract the number after the dash at the end which is the packet index. If it's smaller than in_packet_idx, delete the file. If it's equal to in_packet_idx, read its raw bytes and send it as a binary frame in the WebSocket connection and increment in_pakcet_idx and delete the file. If the number is larger than in_packet_idx, do nothing. If the filename ends with "-close" (e.g. the-callee@1777303873807-close), don't read its content, instead close the WebSocket connection and then break the loop (and don't forget to delete the file itself).
    2. If the WebSocket connection is closed, stop the loop.
    3. Sleep a little if needed for async (or don't if it's unnecessary)
    4. Repeat
  And in the WebSocket's message event, whenever we receive a binary frame, write the raw bytes to a new file named `${peer}@${call.timestamp}-${out_packet_idx}` in our call-outbox directory and then increment out_packet_idx.
  If the WebSocket connection gets closed from the other side (by the original client), create an empty file named `${peer}@${call.timestamp}-${out_packet_idx}-close`.

All user IDs in the requests (e.g. user_id decoded from auth, or peer) must be validated as the following:
1. Must be a non-empty string
2. Cannot contain more than 64 characters
3. Only the following characters are allowed: a-Z, 0-9, '-' (dash), and '_' (underscore).
4. The first and last characters cannot be a dash or underscore.

When creating a new user, the passowrd must be validated as the following:
1. Must be a non-empty string
2. Length must be in the [10, 64] range (at least 10 chars but not longer than 64).
3. No more than 3 adjacent identical characters, e.g. "4444" is not allowed but "444" is fine.

The code must be human-readable and non-cryptic. Avoid empty placeholders or shortcuts.
